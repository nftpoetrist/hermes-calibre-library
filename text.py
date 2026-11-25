"""Turn book files and calibre's HTML comments into plain text, with the standard library only.

EPUB is read from the zip in place: ``META-INF/container.xml`` names the package document, whose
spine gives the reading order. Only members the package names are read, each one capped in size,
and nothing is extracted to disk. TXT is decoded as UTF-8 when it is valid UTF-8, else as cp1252
(calibre libraries hold plenty of older Latin-1 / Windows text files).
"""
from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_BOOK_BYTES = 128 * 1024 * 1024
MAX_TXT_BYTES = 64 * 1024 * 1024
MAX_CHAPTERS = 300
_HTML_TYPES = {"application/xhtml+xml", "text/html"}
_WS_RE = re.compile(r"[ \t\r\f\v]+")


class BookUnreadable(ValueError):
    """The file cannot be read as the format it claims to be."""


class _TextExtractor(HTMLParser):
    """HTML to readable text: headings marked with #, list items with -, no markup."""

    _SKIP = {"script", "style", "noscript", "head", "template", "svg"}
    _BLOCK = {"p", "div", "section", "article", "br", "tr", "table", "blockquote", "dd", "dt",
              "figcaption", "caption", "ul", "ol", "dl", "hr", "pre", "details", "summary", "header",
              "footer", "aside", "main", "nav"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.first_heading: str | None = None
        self._heading: list[str] | None = None
        self._skip = 0
        self._pre = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "pre":
            self._pre += 1
        if tag in self._BLOCK:
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
            if self.first_heading is None and self._heading is None:
                self._heading = []
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(self._skip - 1, 0)
            return
        if self._skip:
            return
        if tag == "pre":
            self._pre = max(self._pre - 1, 0)
        if re.fullmatch(r"h[1-6]", tag) and self._heading is not None:
            self.first_heading = _WS_RE.sub(" ", "".join(self._heading)).strip() or None
            self._heading = None
        if tag in self._BLOCK or re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._heading is not None:
            self._heading.append(data)
        self.parts.append(data if self._pre else _WS_RE.sub(" ", data.replace("\n", " ")))

    def text(self) -> str:
        out, blank = [], False
        for line in "".join(self.parts).split("\n"):
            line = line.strip()
            if line:
                out.append(line)
                blank = False
            elif not blank and out:
                out.append("")
                blank = True
        while out and not out[-1]:
            out.pop()
        return "\n".join(out)


def html_to_text(html_text: str) -> tuple[str, str | None]:
    """Plain text and the first heading's text."""
    parser = _TextExtractor()
    parser.feed(html_text)
    parser.close()
    return parser.text(), parser.first_heading


def txt_text(path: Path) -> tuple[str, str]:
    """A TXT book's text and the encoding it was decoded with."""
    if path.stat().st_size > MAX_TXT_BYTES:
        raise BookUnreadable(f"the TXT file is larger than {MAX_TXT_BYTES // (1024 * 1024)} MB")
    raw = path.read_bytes()
    try:
        text, encoding = raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        text, encoding = raw.decode("cp1252", errors="replace"), "cp1252"
    return text.replace("\r\n", "\n").replace("\r", "\n").strip(), encoding


def _xml(data: bytes, what: str) -> ET.Element:
    """Parse a small EPUB XML file. A DTD is refused: neither file needs one, and it is how entity bombs get in."""
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise BookUnreadable(f"{what} declares a DTD or entities, which EPUB does not need; not reading it")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise BookUnreadable(f"{what} is not well-formed XML: {exc}") from None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class _Epub:
    def __init__(self, archive: zipfile.ZipFile):
        self.archive = archive
        self.read_bytes = 0

    def member(self, name: str) -> bytes:
        try:
            info = self.archive.getinfo(name)
        except KeyError:
            raise BookUnreadable(f"the EPUB has no {name}") from None
        if info.file_size > MAX_MEMBER_BYTES:
            raise BookUnreadable(f"{name} unpacks to more than {MAX_MEMBER_BYTES // (1024 * 1024)} MB")
        self.read_bytes += info.file_size
        if self.read_bytes > MAX_BOOK_BYTES:
            raise BookUnreadable(f"the EPUB's text unpacks to more than {MAX_BOOK_BYTES // (1024 * 1024)} MB")
        return self.archive.read(info)


def epub_text(path: Path) -> tuple[str, list[dict], int]:
    """The book's text in spine order, where each spine document starts, and how many were skipped.

    Spine documents that are not (X)HTML, such as SVG or image pages, are skipped and counted.
    """
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise BookUnreadable(f"not a readable EPUB (zip) file: {exc}") from None
    with archive:
        epub = _Epub(archive)
        container = _xml(epub.member("META-INF/container.xml"), "META-INF/container.xml")
        rootfile = next((el.get("full-path") for el in container.iter() if _local(el.tag) == "rootfile"
                         and el.get("full-path")), None)
        if not rootfile:
            raise BookUnreadable("META-INF/container.xml names no package document")
        package = _xml(epub.member(rootfile), rootfile)
        base = posixpath.dirname(rootfile)
        manifest = {el.get("id"): (el.get("href") or "", (el.get("media-type") or "").lower())
                    for el in package.iter() if _local(el.tag) == "item" and el.get("id")}
        spine = [el.get("idref") for el in package.iter() if _local(el.tag) == "itemref"]
        parts, chapters, skipped, length = [], [], 0, 0
        for idref in spine:
            href, media = manifest.get(idref, ("", ""))
            if media not in _HTML_TYPES or not href:
                skipped += 1
                continue
            name = posixpath.normpath(posixpath.join(base, unquote(urlsplit(href).path)))
            text, heading = html_to_text(epub.member(name).decode("utf-8", errors="replace"))
            if not text:
                continue
            if parts:
                length += 2  # the blank line joining two documents
            if len(chapters) < MAX_CHAPTERS:
                chapters.append({"offset": length, "title": heading or text.split("\n", 1)[0][:80]})
            parts.append(text)
            length += len(text)
        return "\n\n".join(parts), chapters, skipped
