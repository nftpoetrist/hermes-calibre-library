"""End-to-end tests against calibre libraries built with calibre's own table and column names.

The fixture library has the tables calibre's ``metadata.db`` has for books, authors, series, tags,
publishers, languages, ratings, comments, identifiers and files, with values shaped like a real
library's: calibre's "no date" (0101-01-01), an empty series_index, ratings stored as half stars,
accented names, a cp1252 TXT with CRLF line ends and multi-chapter EPUBs. It also holds what the
plugin must refuse: a book path leading out of the library, an EPUB declaring entities, a zip bomb,
a broken zip and a WAL-mode library with unsaved changes.

``CALIBRE_LIBRARY_REAL`` (library folders, separated like PATH) runs a test against real calibre
libraries, and ``CALIBRE_REAL_EPUBS`` (a folder of .epub files) one against real EPUB files.
"""
import hashlib
import importlib
import json
import os
import socket
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SCHEMA = [  # calibre's column names for the tables this plugin reads
    "CREATE TABLE books (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL DEFAULT 'Unknown', "
    "sort TEXT, timestamp TIMESTAMP, pubdate TIMESTAMP, series_index REAL NOT NULL DEFAULT 1.0, "
    "author_sort TEXT, isbn TEXT DEFAULT '', lccn TEXT DEFAULT '', path TEXT NOT NULL DEFAULT '', "
    "flags INTEGER NOT NULL DEFAULT 1, uuid TEXT, has_cover BOOL DEFAULT 0, last_modified TIMESTAMP)",
    "CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL, sort TEXT, link TEXT NOT NULL DEFAULT '')",
    "CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, author INTEGER NOT NULL)",
    "CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL)",
    "CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, tag INTEGER NOT NULL)",
    "CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT NOT NULL, sort TEXT)",
    "CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, series INTEGER NOT NULL)",
    "CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, sort TEXT)",
    "CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, publisher INTEGER NOT NULL)",
    "CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT NOT NULL)",
    "CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, lang_code INTEGER NOT NULL, "
    "item_order INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INTEGER)",
    "CREATE TABLE books_ratings_link (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, rating INTEGER NOT NULL)",
    "CREATE TABLE comments (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, text TEXT NOT NULL)",
    "CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, format TEXT NOT NULL, "
    "uncompressed_size INTEGER NOT NULL, name TEXT NOT NULL)",
    "CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, type TEXT NOT NULL DEFAULT 'isbn', "
    "val TEXT NOT NULL)",
]

CHAPTERS = [
    ("Chapter One: Down the Rabbit-Hole", "Alice was beginning to get very tired of sitting by her sister."),
    ("Chapter Two: The Pool of Tears", "Curiouser and curiouser! cried Alice."),
    ("Chapter Three: A Caucus-Race", "They were indeed a queer-looking party."),
]
LONG_PARAGRAPHS = [f"Paragraph {i}: " + " ".join(f"word{i}-{j}" for j in range(40)) for i in range(60)]


def _epub(path: Path, docs, *, opf_extra="", container=None, opf_doctype=False, spine_extra=""):
    """A real EPUB 3 zip: mimetype first, container.xml, a package document in a subfolder."""
    items, refs = [], []
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        z.writestr("META-INF/container.xml", container or (
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>"))
        for i, (name, media, body, linear) in enumerate(docs):
            items.append(f'<item id="d{i}" href="{name}" media-type="{media}"/>')
            linear_attr = ' linear="no"' if linear is False else ""
            refs.append(f'<itemref idref="d{i}"{linear_attr}/>')
            z.writestr(f"OPS/{name.replace('%20', ' ')}", body)
        doctype = '<!DOCTYPE package [<!ENTITY a "aaaaaaaaaa">]>' if opf_doctype else ""
        z.writestr("OPS/package.opf", (
            f'<?xml version="1.0"?>{doctype}<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            f"<metadata/><manifest>{''.join(items)}{opf_extra}</manifest>"
            f"<spine>{''.join(refs)}{spine_extra}</spine></package>"))
    return path


def _xhtml(title, body):
    return (f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head>'
            f"<title>{title}</title><style>p{{color:red}}</style></head><body>{body}</body></html>")


class _Library:
    """Writes a calibre library: metadata.db plus Author/Title (id)/name.ext files."""

    def __init__(self, root: Path, journal="delete"):
        self.root = root
        root.mkdir(parents=True)
        self.conn = sqlite3.connect(root / "metadata.db")
        self.conn.execute(f"PRAGMA journal_mode={journal}")
        for statement in SCHEMA:
            self.conn.execute(statement)
        self._ids: dict[tuple[str, str], int] = {}

    def _lookup(self, table, column, value):
        key = (table, value)
        if key not in self._ids:
            cur = self.conn.execute(f"INSERT INTO {table}({column}) VALUES (?)", (value,))
            self._ids[key] = cur.lastrowid
        return self._ids[key]

    def add(self, book_id, title, authors, *, sort=None, folder=None, files=(), series=None, series_index=1.0,
            tags=(), publisher=None, languages=(), rating=None, comment=None, identifiers=(),
            pubdate="0101-01-01 00:00:00+00:00", added="2026-01-02 10:00:00+00:00"):
        folder = folder or f"{authors[0]}/{title} ({book_id})"
        self.conn.execute("INSERT INTO books(id, title, sort, timestamp, pubdate, series_index, path, uuid, has_cover) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                          (book_id, title, sort or title, added, pubdate, series_index, folder, f"uuid-{book_id}"))
        for author in authors:
            self.conn.execute("INSERT INTO books_authors_link(book, author) VALUES (?, ?)",
                              (book_id, self._lookup("authors", "name", author)))
        if series:
            self.conn.execute("INSERT INTO books_series_link(book, series) VALUES (?, ?)",
                              (book_id, self._lookup("series", "name", series)))
        for tag in tags:
            self.conn.execute("INSERT INTO books_tags_link(book, tag) VALUES (?, ?)", (book_id, self._lookup("tags", "name", tag)))
        if publisher:
            self.conn.execute("INSERT INTO books_publishers_link(book, publisher) VALUES (?, ?)",
                              (book_id, self._lookup("publishers", "name", publisher)))
        for order, code in enumerate(languages):
            self.conn.execute("INSERT INTO books_languages_link(book, lang_code, item_order) VALUES (?, ?, ?)",
                              (book_id, self._lookup("languages", "lang_code", code), order))
        if rating is not None:
            self.conn.execute("INSERT INTO books_ratings_link(book, rating) VALUES (?, ?)",
                              (book_id, self._lookup("ratings", "rating", rating)))
        if comment:
            self.conn.execute("INSERT INTO comments(book, text) VALUES (?, ?)", (book_id, comment))
        for kind, value in identifiers:
            self.conn.execute("INSERT INTO identifiers(book, type, val) VALUES (?, ?, ?)", (book_id, kind, value))
        name = f"{title} - {authors[0]}"
        for fmt, content in files:
            self.conn.execute("INSERT INTO data(book, format, uncompressed_size, name) VALUES (?, ?, ?, ?)",
                              (book_id, fmt, len(content) if isinstance(content, bytes) else 0, name))
            target = (self.root / folder / f"{name}.{fmt.lower()}")
            if content is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if callable(content):
                content(target)
            else:
                target.write_bytes(content)

    def close(self):
        self.conn.commit()
        self.conn.close()


def _alice_epub(path):
    docs = [("cover.svg", "image/svg+xml", "<svg xmlns='http://www.w3.org/2000/svg'><text>Cover</text></svg>", None)]
    docs += [(f"text/ch{i}.xhtml", "application/xhtml+xml", _xhtml(t, f"<h1>{t}</h1><p>{p}</p><script>steal()</script>"), None)
             for i, (t, p) in enumerate(CHAPTERS, 1)]
    docs.append(("text/notes%20page.xhtml", "application/xhtml+xml", _xhtml("Notes", "<h2>Notes</h2><ul><li>A footnote.</li></ul>"), False))
    docs.append(("text/empty.xhtml", "application/xhtml+xml", _xhtml("Blank", "<div></div>"), None))
    _epub(path, docs, opf_extra='<item id="img" href="i.png" media-type="image/png"/>')


def _zip_bomb(path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="p.opf"/></rootfiles></container>')
        z.writestr("p.opf", '<package><manifest><item id="a" href="big.xhtml" media-type="application/xhtml+xml"/>'
                            '</manifest><spine><itemref idref="a"/></spine></package>')
        z.writestr("big.xhtml", b"\0" * (40 * 1024 * 1024))


@pytest.fixture(scope="session")
def libraries(tmp_path_factory):
    base = tmp_path_factory.mktemp("calibre")
    (base / "secret.txt").write_text("TOP SECRET outside the library")
    main = _Library(base / "Calibre Library")
    main.add(1, "Alice's Adventures in Wonderland", ["Lewis Carroll"], sort="Alice's Adventures in Wonderland",
             files=[("EPUB", _alice_epub), ("PDF", b"%PDF-1.4 not really")], series="Wonderland", series_index=1.0,
             tags=["Classics", "Fantasy"], publisher="Macmillan", languages=["eng"], rating=10,
             comment="<div><p>A girl falls <b>down</b> a rabbit hole.</p><ul><li>Illustrated</li></ul></div>",
             identifiers=[("isbn", "9780141439761"), ("goodreads", "6324090")],
             pubdate="1865-11-26 00:00:00+00:00", added="2026-03-01 09:00:00+00:00")
    main.add(2, "Through the Looking-Glass", ["Lewis Carroll"], files=[("EPUB", lambda p: _epub(p, [
        ("a.xhtml", "application/xhtml+xml", _xhtml("Looking", "<p>" + "</p><p>".join(LONG_PARAGRAPHS) + "</p>"), None)]))],
             series="Wonderland", series_index=2.0, tags=["classics"], added="2026-03-02 09:00:00+00:00")
    main.add(3, "Der Buchtitel", ["Frodo Beutlin", "Hector Gonçalves"], sort="Buchtitel, Der",
             files=[("TXT", "Er hörte leise Schritte.\r\nDas bedeutete nichts Gutes.\r\n".encode("cp1252"))],
             tags=["Gênot"], languages=["deu", "eng"], series="Djüngel", series_index="",
             added="2026-03-03 09:00:00+00:00")
    main.add(4, "Notes in UTF-8", ["Ünal Öz"], files=[("TXT", "﻿Kısa notlar — ğüşıöç\n".encode("utf-8"))],
             added="2026-03-04 09:00:00+00:00")
    main.add(5, "Only a Comic", ["Asterix Lionherd"], files=[("CBR", b"Rar!"), ("MOBI", b"BOOKMOBI")], rating=4)
    main.add(6, "Escape", ["Mallory"], folder="../outside (6)", files=[("TXT", None)])
    (base / "outside (6)").mkdir()
    (base / "outside (6)" / "Escape - Mallory.txt").write_text("should never be read")
    main.add(7, "Gone", ["Nobody"], files=[("EPUB", None)])
    main.add(8, "Entity Book", ["Eve"], files=[("EPUB", lambda p: _epub(p, [("a.xhtml", "application/xhtml+xml", _xhtml("x", "<p>x</p>"), None)], opf_doctype=True))])
    main.add(9, "Bomb", ["Eve"], files=[("EPUB", _zip_bomb)])
    main.add(10, "Broken Zip", ["Eve"], files=[("EPUB", b"PK\x03\x04 not a real zip")])
    main.add(11, "100%_done", ["Percy"], files=[("TXT", b"wildcards")])
    main.close()
    second = _Library(base / "Work Books")
    second.add(1, "Alice in Spreadsheets", ["Bob Tables"], files=[("TXT", b"rows and columns")], tags=["Work"])
    second.close()
    wal = _Library(base / "WAL Library", journal="wal")
    wal.add(1, "Write-Ahead Alice", ["Wally"], files=[("TXT", b"logged")])
    wal.close()
    (base / "WAL Library" / "metadata.db-wal").write_bytes(b"")
    busy = _Library(base / "Busy Library", journal="wal")
    busy.add(1, "Busy", ["Writer"], files=[("TXT", b"busy")])
    busy.close()
    _pending_wal(base / "Busy Library" / "metadata.db", tmp_path_factory.mktemp("busy"))
    (base / "Not a library").mkdir()
    return base


def _pending_wal(db: Path, scratch: Path):
    work = scratch / "metadata.db"
    work.write_bytes(db.read_bytes())
    writer = sqlite3.connect(work)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("UPDATE books SET title = 'Busier'")
    writer.commit()
    for suffix in ("", "-wal", "-shm"):
        (db.parent / f"metadata.db{suffix}").write_bytes((scratch / f"metadata.db{suffix}").read_bytes())
    writer.close()
    assert (db.parent / "metadata.db-wal").stat().st_size > 0


def _files(folder: Path):
    return {p.relative_to(folder).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(folder.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def plugin():
    sys.path.insert(0, str(ROOT.parent))
    try:
        return importlib.import_module(ROOT.name)
    finally:
        sys.path.remove(str(ROOT.parent))


class _Ctx:
    """The slice of Hermes's PluginContext this plugin uses."""

    def __init__(self, settings):
        self.settings, self.tools = settings, {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **kw):
        self.tools[kw["name"]] = kw


def _tools(plugin, settings):
    ctx = _Ctx(settings)
    plugin.register(ctx)
    return ctx.tools


def _run(tool, args):
    return json.loads(tool["handler"](args))


@pytest.fixture
def main(plugin, libraries):
    return _tools(plugin, {"paths": [str(libraries / "Calibre Library")]})


@pytest.fixture
def every(plugin, libraries):
    return _tools(plugin, {"paths": [str(libraries / name) for name in
                                     ("Calibre Library", "Work Books", "WAL Library", "Busy Library", "Not a library")]})


def test_registers_exactly_the_declared_tools(plugin):
    manifest = (ROOT / "plugin.yaml").read_text()
    registered = _tools(plugin, {})
    assert sorted(registered) == ["calibre_book", "calibre_read", "calibre_search"]
    for name, tool in registered.items():
        assert f"- {name}" in manifest and not tool.get("is_async")


def test_tools_are_offered_only_while_paths_are_configured(plugin, libraries):
    assert _tools(plugin, {})["calibre_search"]["check_fn"]() is False
    assert _tools(plugin, {"paths": []})["calibre_search"]["check_fn"]() is False
    assert _tools(plugin, {"paths": [str(libraries / "Calibre Library")]})["calibre_read"]["check_fn"]() is True


def test_search_summarises_each_book(main):
    # 'rabbit' is only in the description, which is not searched
    assert _run(main["calibre_search"], {"query": "rabbit wonderland macmillan"})["results"] == []
    found = _run(main["calibre_search"], {"query": "wonderland macmillan"})
    assert found["results"] == [{
        "library": "Calibre_Library", "book_id": 1, "title": "Alice's Adventures in Wonderland",
        "authors": ["Lewis Carroll"], "series": "Wonderland", "series_index": 1, "tags": ["Classics", "Fantasy"],
        "formats": ["EPUB", "PDF"], "languages": ["eng"], "published": "1865-11-26", "rating": 5}]
    assert found["matches"] == {"Calibre_Library": 1}


def test_calibre_no_date_empty_series_index_and_half_star_ratings(main):
    der = _run(main["calibre_search"], {"query": "buchtitel"})["results"][0]
    assert "published" not in der and der["series"] == "Djüngel" and "series_index" not in der
    assert der["languages"] == ["deu", "eng"] and der["authors"] == ["Frodo Beutlin", "Hector Gonçalves"]
    comic = _run(main["calibre_search"], {"query": "comic"})["results"][0]
    assert comic["rating"] == 2 and comic["formats"] == ["CBR", "MOBI"]


def test_matching_ignores_case_and_accents(main):
    assert [b["book_id"] for b in _run(main["calibre_search"], {"query": "GONCALVES"})["results"]] == [3]
    assert [b["book_id"] for b in _run(main["calibre_search"], {"query": "djungel"})["results"]] == [3]
    assert [b["book_id"] for b in _run(main["calibre_search"], {"tag": "genot"})["results"]] == [3]
    assert [b["book_id"] for b in _run(main["calibre_search"], {"author": "unal oz"})["results"]] == [4]


def test_query_words_can_match_different_fields_and_identifiers(main):
    assert [b["book_id"] for b in _run(main["calibre_search"], {"query": "carroll looking"})["results"]] == [2]
    assert [b["book_id"] for b in _run(main["calibre_search"], {"query": "9780141439761"})["results"]] == [1]
    assert [b["book_id"] for b in _run(main["calibre_search"], {"query": "%_"})["results"]] == [11]


def test_filters_narrow_the_results(main):
    assert [b["book_id"] for b in _run(main["calibre_search"], {"tag": "Classics", "sort": "title"})["results"]] == [1, 2]
    assert [b["book_id"] for b in _run(main["calibre_search"], {"tag": "Class"})["results"]] == []
    assert [b["book_id"] for b in _run(main["calibre_search"], {"series": "wonder", "sort": "title"})["results"]] == [1, 2]
    assert [b["book_id"] for b in _run(main["calibre_search"], {"format": "mobi"})["results"]] == [5]
    assert [b["book_id"] for b in _run(main["calibre_search"], {"language": "DEU"})["results"]] == [3]
    assert [b["book_id"] for b in _run(main["calibre_search"], {"query": "alice", "format": "pdf"})["results"]] == [1]


def test_sort_orders(main):
    ids = lambda args: [b["book_id"] for b in _run(main["calibre_search"], {**args, "limit": 4})["results"]]
    assert ids({"sort": "added"}) == [4, 3, 2, 1]
    assert ids({"sort": "title"}) == [11, 1, 9, 10]
    assert ids({"sort": "published"})[0] == 1  # the only book with a date; calibre's "no date" sorts last


def test_relevance_puts_titles_starting_with_the_query_first(main):
    found = _run(main["calibre_search"], {"query": "der"})["results"]
    assert [b["book_id"] for b in found][0] == 3
    looking = _run(main["calibre_search"], {"query": "through", "sort": "relevance"})["results"]
    assert looking[0]["book_id"] == 2


def test_searching_every_library_interleaves_and_reports_unusable_ones(every, libraries):
    found = _run(every["calibre_search"], {"query": "alice", "limit": 4})
    assert [(b["library"], b["book_id"]) for b in found["results"]] == [
        ("Calibre_Library", 1), ("Work_Books", 1), ("WAL_Library", 1)]
    assert found["matches"] == {"Calibre_Library": 1, "Work_Books": 1, "WAL_Library": 1}
    problems = "\n".join(found["unavailable"])
    assert "unsaved changes in metadata.db-wal" in problems
    assert f"{libraries / 'Not a library'}: not a calibre library" in problems
    assert len(found["unavailable"]) == 2


def test_book_shows_everything_calibre_records(main):
    alice = _run(main["calibre_book"], {"book_id": 1})
    assert alice["publisher"] == "Macmillan" and alice["added"] == "2026-03-01"
    assert alice["identifiers"] == {"goodreads": "6324090", "isbn": "9780141439761"}
    assert alice["description"] == "A girl falls down a rabbit hole.\n\n- Illustrated"
    assert alice["files"] == [
        {"format": "EPUB", "size": 0, "readable": True,
         "file": "Lewis Carroll/Alice's Adventures in Wonderland (1)/Alice's Adventures in Wonderland - Lewis Carroll.epub",
         "present": True},
        {"format": "PDF", "size": 19, "readable": False,
         "file": "Lewis Carroll/Alice's Adventures in Wonderland (1)/Alice's Adventures in Wonderland - Lewis Carroll.pdf",
         "present": True}]
    assert alice["has_cover"] is False


def test_book_reports_missing_and_escaping_files(main):
    assert _run(main["calibre_book"], {"book_id": 7})["files"][0]["present"] is False
    escape = _run(main["calibre_book"], {"book_id": 6})["files"][0]
    assert "points outside the library" in escape["problem"] and "file" not in escape


def test_read_an_epub_in_reading_order_with_chapters(main):
    page = _run(main["calibre_read"], {"book_id": 1})
    text = page["text"]
    assert page["format"] == "EPUB" and page["skipped_documents"] == 1 and page["next_offset"] is None
    assert text.startswith("# Chapter One: Down the Rabbit-Hole\n\nAlice was beginning")
    assert "steal()" not in text and "color:red" not in text
    titles = [c["title"] for c in page["chapters"]]
    assert titles == [t for t, _ in CHAPTERS] + ["Notes"]
    for chapter in page["chapters"]:
        assert text[chapter["offset"]:].startswith("#"), chapter
        assert chapter["offset"] == 0 or text[chapter["offset"] - 2:chapter["offset"]] == "\n\n"
    assert text.endswith("## Notes\n\n- A footnote.")


def test_long_books_are_read_in_parts_that_join_up(plugin, libraries):
    read = _tools(plugin, {"paths": [str(libraries / "Calibre Library")], "max_chars": 1000})["calibre_read"]
    parts, offset = [], 0
    while offset is not None:
        part = _run(read, {"book_id": 2, "offset": offset})
        assert len(part["text"]) <= 1000 and ("chapters" in part) == (offset == 0)
        parts.append(part["text"])
        offset = part["next_offset"]
    whole = "".join(parts)
    assert len(parts) > 2 and len(whole) == part["total_chars"]
    assert all(p in whole for p in LONG_PARAGRAPHS)


def test_txt_books_are_decoded_as_utf8_or_cp1252(main):
    latin = _run(main["calibre_read"], {"book_id": 3})
    assert (latin["format"], latin["encoding"]) == ("TXT", "cp1252")
    assert latin["text"] == "Er hörte leise Schritte.\nDas bedeutete nichts Gutes."
    utf8 = _run(main["calibre_read"], {"book_id": 4})
    assert (utf8["encoding"], utf8["text"]) == ("utf-8", "Kısa notlar — ğüşıöç")


@pytest.mark.parametrize("args, message", [
    ({"book_id": 5}, "has no EPUB or TXT file to read (it has CBR, MOBI)"),
    ({"book_id": 1, "format": "PDF"}, "PDF cannot be read as text"),
    ({"book_id": 1, "format": "TXT"}, "has no TXT file; it has EPUB, PDF"),
    ({"book_id": 6}, "points outside the library"),
    ({"book_id": 7}, "missing from the library folder"),
    ({"book_id": 8}, "declares a DTD or entities"),
    ({"book_id": 9}, "unpacks to more than 32 MB"),
    ({"book_id": 10}, "not a readable EPUB (zip) file"),
    ({"book_id": 99}, "no book 99"),
    ({}, "book_id is required"),
    ({"book_id": 0}, "book_id must be an integer"),
    ({"book_id": 1, "offset": -1}, "offset must be"),
    ({"book_id": 1, "library": "nope"}, "no library 'nope'"),
])
def test_read_errors_are_reported_not_raised(main, args, message):
    assert message in _run(main["calibre_read"], args)["error"]


def test_nothing_outside_the_library_is_read(main, libraries, monkeypatch):
    opened = []
    real_open = open

    def tracking_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", tracking_open)
    _run(main["calibre_read"], {"book_id": 6})
    _run(main["calibre_book"], {"book_id": 6})
    assert not any("outside" in path for path in opened), opened


def test_library_is_required_only_when_several_are_configured(every):
    assert "library is required" in _run(every["calibre_book"], {"book_id": 1})["error"]
    work = _run(every["calibre_book"], {"book_id": 1, "library": "Work_Books"})
    assert work["title"] == "Alice in Spreadsheets"
    assert _run(every["calibre_read"], {"book_id": 1, "library": "WAL_Library"})["text"] == "logged"
    assert "unsaved changes" in _run(every["calibre_read"], {"book_id": 1, "library": "Busy_Library"})["error"]


def test_search_and_book_argument_errors(main):
    assert "limit must be" in _run(main["calibre_search"], {"limit": 51})["error"]
    assert "sort must be one of" in _run(main["calibre_search"], {"sort": "random"})["error"]
    assert "tag must be" in _run(main["calibre_search"], {"tag": " "})["error"]
    assert "query must be a string" in _run(main["calibre_search"], {"query": 3})["error"]
    assert "no library" in _run(main["calibre_search"], {"library": "nope"})["error"]
    assert "book_id is required" in _run(main["calibre_book"], {})["error"]


@pytest.mark.parametrize("settings, message", [
    ({}, "no calibre libraries are configured"),
    ({"paths": [1]}, "must be a list"),
    ({"paths": ["/tmp"], "max_chars": 10}, "max_chars"),
])
def test_invalid_settings_are_reported(plugin, settings, message):
    assert message in _run(_tools(plugin, settings)["calibre_search"], {})["error"]


def test_duplicate_library_ids_are_reported(plugin, libraries, tmp_path):
    twin = tmp_path / "Calibre Library"
    twin.mkdir()
    (twin / "metadata.db").write_bytes((libraries / "Calibre Library" / "metadata.db").read_bytes())
    found = _run(_tools(plugin, {"paths": [str(libraries / "Calibre Library"), str(twin)]})["calibre_search"], {"query": "alice"})
    assert [b["library"] for b in found["results"]] == ["Calibre_Library"]
    assert "already" in found["unavailable"][0] and str(twin) in found["unavailable"][0]


def test_every_connection_is_read_only_and_closed_and_nothing_changes(plugin, every, libraries, monkeypatch):
    library = importlib.import_module(f"{ROOT.name}.library")
    opened, real_connect = [], sqlite3.connect

    class Tracked(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    def connect(database, *args, **kwargs):
        conn = real_connect(database, *args, factory=Tracked, **kwargs)
        opened.append((database, kwargs.get("uri"), conn))
        return conn

    monkeypatch.setattr(library.sqlite3, "connect", connect)
    before = _files(libraries)
    _run(every["calibre_search"], {"query": "alice"})
    _run(every["calibre_book"], {"book_id": 1, "library": "Calibre_Library"})
    _run(every["calibre_read"], {"book_id": 1, "library": "Calibre_Library"})
    _run(every["calibre_read"], {"book_id": 1, "library": "WAL_Library"})
    _run(every["calibre_read"], {"book_id": 1, "library": "Busy_Library"})
    assert len(opened) >= 6
    for database, uri, conn in opened:
        assert uri is True and database.startswith("file://"), database
        if "WAL%20Library" in database:
            assert database.endswith("?mode=ro&immutable=1"), database
        else:
            assert database.endswith("?mode=ro"), database
        assert conn.closed, database
    assert any("WAL%20Library" in d for d, _, _ in opened)
    assert _files(libraries) == before  # the WAL library's -wal and -shm included


def test_nothing_goes_over_the_network(main, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the plugin opened a network socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert _run(main["calibre_search"], {"query": "alice"})["results"]
    assert _run(main["calibre_book"], {"book_id": 1})["title"]
    assert _run(main["calibre_read"], {"book_id": 1})["text"]


@pytest.mark.skipif(not os.environ.get("CALIBRE_LIBRARY_REAL"), reason="set CALIBRE_LIBRARY_REAL to calibre library folders")
def test_real_calibre_libraries(plugin):
    """Every book shows, every file the database lists is found, every EPUB/TXT reads to the end."""
    paths = [Path(p) for p in os.environ["CALIBRE_LIBRARY_REAL"].split(os.pathsep) if p]
    before = {p: _files(p) for p in paths}
    report = []
    for path in paths:
        tools = _tools(plugin, {"paths": [str(path)]})
        listed = _run(tools["calibre_search"], {"sort": "title", "limit": 50})
        assert "unavailable" not in listed, listed
        stats = {"library": path.name, "books": listed["matches"], "files": 0, "read": 0, "chars": 0}
        for summary in listed["results"]:
            detail = _run(tools["calibre_book"], {"book_id": summary["book_id"]})
            assert "error" not in detail, detail
            for item in detail["files"]:
                stats["files"] += 1
                assert item.get("present") is True, (summary, item)
                if item["readable"]:
                    offset, chars = 0, 0
                    while offset is not None:
                        part = _run(tools["calibre_read"], {"book_id": summary["book_id"], "format": item["format"], "offset": offset})
                        assert "error" not in part, part
                        chars += len(part["text"])
                        offset = part["next_offset"]
                    assert chars == part["total_chars"] > 0
                    stats["read"] += 1
                    stats["chars"] += chars
        report.append(stats)
    print(json.dumps(report, indent=1))
    assert {p: _files(p) for p in paths} == before


@pytest.mark.skipif(not os.environ.get("CALIBRE_REAL_EPUBS"), reason="set CALIBRE_REAL_EPUBS to a folder of .epub files")
def test_real_epub_files():
    text_module = importlib.import_module(f"{ROOT.name}.text")
    report = []
    for epub in sorted(Path(os.environ["CALIBRE_REAL_EPUBS"]).glob("*.epub")):
        text, chapters, skipped = text_module.epub_text(epub)
        for chapter in chapters:
            assert chapter["offset"] == 0 or text[chapter["offset"] - 2:chapter["offset"]] == "\n\n", (epub.name, chapter)
            assert text[chapter["offset"]:chapter["offset"] + 1].strip(), (epub.name, chapter)
        assert text or skipped, epub.name
        report.append({"epub": epub.name, "chars": len(text), "chapters": len(chapters), "skipped": skipped})
    print(json.dumps(report, indent=1, ensure_ascii=False))
    assert report
