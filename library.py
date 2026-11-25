"""Read calibre libraries: the ``metadata.db`` SQLite database and the book files beside it.

Nothing is written. ``metadata.db`` is opened read-only per call and closed again; book files are
opened only inside the library folder. Nothing is cached between calls, so one profile's
``paths`` never serve another profile's books.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Any

from . import text as booktext

DEFAULT_MAX_CHARS = 12_000
MAX_CHARS_LIMIT = 100_000
READABLE_FORMATS = ("EPUB", "TXT")  # preference order when no format is asked for
SORTS = ("relevance", "title", "added", "published")
_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")
_REQUIRED_TABLES = {"books", "authors", "books_authors_link", "data"}
_UNDEFINED_YEAR = 101  # calibre stores "no date" as 0101-01-01


class SettingsError(ValueError):
    """This plugin's settings cannot be used as written."""


class LookupFailed(ValueError):
    """A library, book or format the model asked for does not exist or cannot be read."""


def configured_paths(value: Any) -> list[Path]:
    """Validate ``settings.paths``: calibre library folders (each holding a metadata.db)."""
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(p, str) and p.strip() for p in value):
        raise SettingsError("settings.paths must be a list of calibre library folders")
    return [Path(p.strip()).expanduser() for p in value]


def max_chars(value: Any) -> int:
    if value in (None, ""):
        return DEFAULT_MAX_CHARS
    if not isinstance(value, int) or isinstance(value, bool) or not 1_000 <= value <= MAX_CHARS_LIMIT:
        raise SettingsError(f"settings.max_chars must be an integer from 1000 to {MAX_CHARS_LIMIT}")
    return value


def discover(paths: list[Path]) -> tuple[dict[str, Path], list[str]]:
    """Map library id (the folder name) to its folder, plus problems found on the way."""
    libraries: dict[str, Path] = {}
    problems: list[str] = []
    for path in paths:
        if not (path / "metadata.db").is_file():
            problems.append(f"{path}: not a calibre library (no metadata.db in this folder)")
            continue
        library = _ID_RE.sub("_", path.resolve().name).strip("_") or "library"
        if library in libraries:
            problems.append(f"{path}: skipped, library id {library!r} is already {libraries[library]}")
            continue
        libraries[library] = path
    return libraries, problems


def fold(value: Any) -> str:
    """Case- and accent-insensitive form used for matching: 'Gonçalves' and 'GONCALVES' meet."""
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _contains(haystack, needle) -> int:
    return int(needle in fold(haystack))


def _starts(haystack, needle) -> int:
    return int(fold(haystack).startswith(needle))


def _same(value, folded) -> int:
    return int(fold(value) == folded)


def _connect(folder: Path) -> sqlite3.Connection:
    """``metadata.db`` opened so that nothing is written.

    calibre keeps the database in rollback-journal mode. There ``mode=ro`` writes nothing and takes
    SQLite's normal shared lock for the moment a call reads, so a write calibre is making at the
    same time is never seen half-done. For a database in WAL mode ``mode=ro`` would write the
    ``-shm`` file, so it is opened ``immutable=1`` instead, and refused while its ``-wal`` file
    still holds changes.
    """
    db = folder / "metadata.db"
    with open(db, "rb") as fh:
        header = fh.read(100)
    if len(header) < 100 or not header.startswith(b"SQLite format 3\x00"):
        raise LookupFailed(f"{folder}: metadata.db is not a SQLite database")
    uri = db.resolve().as_uri() + "?mode=ro"
    if header[18] == 2:  # file format write version 2 = WAL
        wal = folder / "metadata.db-wal"
        if wal.is_file() and wal.stat().st_size > 0:
            raise LookupFailed(f"{folder}: metadata.db has unsaved changes in metadata.db-wal "
                               "(is calibre writing it?); try again once it is closed")
        uri += "&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not _REQUIRED_TABLES <= tables:
            raise LookupFailed(f"{folder}: metadata.db is not a calibre library database")
        conn.create_function("contains", 2, _contains, deterministic=True)
        conn.create_function("starts", 2, _starts, deterministic=True)
        conn.create_function("same", 2, _same, deterministic=True)
    except BaseException:
        conn.close()
        raise
    return conn


def _date(value: Any) -> str | None:
    """``YYYY-MM-DD`` from calibre's timestamp text, or None for its 'no date' value."""
    text = str(value or "")
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not match or int(match.group(1)) <= _UNDEFINED_YEAR:
        return None
    return match.group(0)


def _number(value: Any) -> float | int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _column(conn, sql: str, book: int) -> list:
    return [row[0] for row in conn.execute(sql, (book,))]


def _summary(conn, row) -> dict[str, Any]:
    book, title, series_index, pubdate = row
    authors = _column(conn, "SELECT a.name FROM books_authors_link l JOIN authors a ON a.id = l.author "
                            "WHERE l.book = ? ORDER BY l.id", book)
    series = conn.execute("SELECT s.name FROM books_series_link l JOIN series s ON s.id = l.series "
                          "WHERE l.book = ?", (book,)).fetchone()
    tags = _column(conn, "SELECT t.name FROM books_tags_link l JOIN tags t ON t.id = l.tag "
                         "WHERE l.book = ? ORDER BY t.name COLLATE NOCASE", book)
    formats = _column(conn, "SELECT format FROM data WHERE book = ? ORDER BY format", book)
    languages = _column(conn, "SELECT g.lang_code FROM books_languages_link l JOIN languages g "
                              "ON g.id = l.lang_code WHERE l.book = ? ORDER BY l.item_order", book)
    rating = conn.execute("SELECT r.rating FROM books_ratings_link l JOIN ratings r ON r.id = l.rating "
                          "WHERE l.book = ?", (book,)).fetchone()
    entry: dict[str, Any] = {"book_id": book, "title": title, "authors": authors}
    if series:
        entry["series"] = series[0]
        index = _number(series_index)
        if index is not None:
            entry["series_index"] = index
    if tags:
        entry["tags"] = tags
    entry["formats"] = formats
    if languages:
        entry["languages"] = languages
    published = _date(pubdate)
    if published:
        entry["published"] = published
    if rating and rating[0]:
        entry["rating"] = _number(rating[0] / 2)  # calibre stores 0-10, i.e. half stars
    return entry


def _word_matches(word_param: str) -> str:
    """SQL that is true when one folded word appears in a book's title, authors, series, tags, publisher or ids."""
    return (
        f"(contains(b.title, {word_param})"
        f" OR EXISTS (SELECT 1 FROM books_authors_link l JOIN authors a ON a.id = l.author WHERE l.book = b.id AND contains(a.name, {word_param}))"
        f" OR EXISTS (SELECT 1 FROM books_series_link l JOIN series s ON s.id = l.series WHERE l.book = b.id AND contains(s.name, {word_param}))"
        f" OR EXISTS (SELECT 1 FROM books_tags_link l JOIN tags t ON t.id = l.tag WHERE l.book = b.id AND contains(t.name, {word_param}))"
        f" OR EXISTS (SELECT 1 FROM books_publishers_link l JOIN publishers p ON p.id = l.publisher WHERE l.book = b.id AND contains(p.name, {word_param}))"
        f" OR EXISTS (SELECT 1 FROM identifiers i WHERE i.book = b.id AND contains(i.val, {word_param})))"
    )


def _search_one(conn, query: str, filters: dict[str, str | None], sort: str, limit: int):
    where, params = [], []
    for word in query.split():
        where.append(_word_matches("?"))
        params += [fold(word)] * _word_matches("?").count("?")
    if filters.get("author"):
        where.append("EXISTS (SELECT 1 FROM books_authors_link l JOIN authors a ON a.id = l.author "
                     "WHERE l.book = b.id AND contains(a.name, ?))")
        params.append(fold(filters["author"]))
    if filters.get("series"):
        where.append("EXISTS (SELECT 1 FROM books_series_link l JOIN series s ON s.id = l.series "
                     "WHERE l.book = b.id AND contains(s.name, ?))")
        params.append(fold(filters["series"]))
    if filters.get("tag"):
        where.append("EXISTS (SELECT 1 FROM books_tags_link l JOIN tags t ON t.id = l.tag "
                     "WHERE l.book = b.id AND same(t.name, ?))")
        params.append(fold(filters["tag"]))
    if filters.get("format"):
        where.append("EXISTS (SELECT 1 FROM data d WHERE d.book = b.id AND d.format = ? COLLATE NOCASE)")
        params.append(filters["format"])
    if filters.get("language"):
        where.append("EXISTS (SELECT 1 FROM books_languages_link l JOIN languages g ON g.id = l.lang_code "
                     "WHERE l.book = b.id AND g.lang_code = ? COLLATE NOCASE)")
        params.append(filters["language"])
    clause = " WHERE " + " AND ".join(where) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM books b{clause}", params).fetchone()[0]
    order = {
        "title": "b.sort COLLATE NOCASE, b.id",
        "added": "b.timestamp DESC, b.id DESC",
        "published": "b.pubdate DESC, b.id DESC",
    }.get(sort)
    order_params: list[Any] = []
    if order is None:  # relevance: titles starting with the query, then titles holding it, then the rest
        folded = fold(query)
        order = ("CASE WHEN starts(b.title, ?) THEN 0 WHEN contains(b.title, ?) THEN 1 ELSE 2 END, "
                 "b.sort COLLATE NOCASE, b.id") if folded else "b.sort COLLATE NOCASE, b.id"
        order_params = [folded, folded] if folded else []
    rows = conn.execute(f"SELECT b.id, b.title, b.series_index, b.pubdate FROM books b{clause} "
                        f"ORDER BY {order} LIMIT ?", [*params, *order_params, limit]).fetchall()
    return [_summary(conn, row) for row in rows], total


def search(libraries: dict[str, Path], problems: list[str], query: str, filters: dict[str, str | None],
           library: str | None, sort: str, limit: int) -> dict[str, Any]:
    if library is not None and library not in libraries:
        raise LookupFailed(f"no library {library!r}; configured: {', '.join(libraries) or 'none'}")
    targets = {library: libraries[library]} if library is not None else libraries
    per_library, unavailable, counts = [], list(problems), {}
    for name, folder in targets.items():
        try:
            with closing(_connect(folder)) as conn:
                books, counts[name] = _search_one(conn, query, filters, sort, limit)
        except (LookupFailed, sqlite3.Error, OSError) as exc:
            unavailable.append(str(exc) if isinstance(exc, LookupFailed) else f"{folder}: {exc}")
            continue
        per_library.append([{"library": name, **book} for book in books])
    results = []  # interleave so one big library does not crowd out the rest
    for rank in range(limit):
        for books in per_library:
            if rank < len(books) and len(results) < limit:
                results.append(books[rank])
    payload: dict[str, Any] = {"results": results, "matches": counts}
    if unavailable:
        payload["unavailable"] = unavailable
    return payload


def _pick_library(libraries: dict[str, Path], library: str | None) -> tuple[str, Path]:
    if library is None:
        if len(libraries) == 1:
            return next(iter(libraries.items()))
        raise LookupFailed(f"library is required when several are configured: {', '.join(libraries) or 'none'}")
    if library not in libraries:
        raise LookupFailed(f"no library {library!r}; configured: {', '.join(libraries) or 'none'}")
    return library, libraries[library]


def _book_row(conn, book_id: int):
    row = conn.execute("SELECT id, title, series_index, pubdate, path, timestamp, has_cover, uuid "
                       "FROM books WHERE id = ?", (book_id,)).fetchone()
    if row is None:
        raise LookupFailed(f"no book {book_id} in this library; use calibre_search to find one")
    return row


def _book_file(folder: Path, book_path: str, name: str, fmt: str) -> Path:
    """The file calibre keeps for one format; nothing outside the library folder is opened."""
    root = folder.resolve()
    candidate = (root / book_path / f"{name}.{fmt.lower()}").resolve()
    if not candidate.is_relative_to(root):
        raise LookupFailed(f"the {fmt} file of this book points outside the library")
    return candidate


def book(libraries: dict[str, Path], library: str | None, book_id: int) -> dict[str, Any]:
    name, folder = _pick_library(libraries, library)
    with closing(_connect(folder)) as conn:
        row = _book_row(conn, book_id)
        entry = {"library": name, **_summary(conn, row[:4])}
        publisher = conn.execute("SELECT p.name FROM books_publishers_link l JOIN publishers p "
                                 "ON p.id = l.publisher WHERE l.book = ?", (book_id,)).fetchone()
        if publisher:
            entry["publisher"] = publisher[0]
        identifiers = {t: v for t, v in conn.execute(
            "SELECT type, val FROM identifiers WHERE book = ? ORDER BY type", (book_id,))}
        if identifiers:
            entry["identifiers"] = identifiers
        added = _date(row[5])
        if added:
            entry["added"] = added
        comment = conn.execute("SELECT text FROM comments WHERE book = ?", (book_id,)).fetchone()
        if comment and comment[0]:
            entry["description"] = booktext.html_to_text(comment[0])[0]
        files = []
        for fmt, size, file_name in conn.execute(
                "SELECT format, uncompressed_size, name FROM data WHERE book = ? ORDER BY format", (book_id,)):
            item = {"format": fmt, "size": size, "readable": fmt.upper() in READABLE_FORMATS}
            try:
                path = _book_file(folder, row[4], file_name, fmt)
                item["file"] = path.relative_to(folder.resolve()).as_posix()
                item["present"] = path.is_file()
            except LookupFailed as exc:
                item["problem"] = str(exc)
            files.append(item)
        entry["files"] = files
        entry["has_cover"] = bool(row[6])
    return entry


def read(libraries: dict[str, Path], library: str | None, book_id: int, fmt: str | None,
         offset: int, limit_chars: int) -> dict[str, Any]:
    """A book's text, ``limit_chars`` at a time from ``offset``."""
    name, folder = _pick_library(libraries, library)
    with closing(_connect(folder)) as conn:
        row = _book_row(conn, book_id)
        formats = {f.upper(): file_name for f, file_name in conn.execute(
            "SELECT format, name FROM data WHERE book = ?", (book_id,))}
    if fmt is not None:
        fmt = fmt.upper()
        if fmt not in formats:
            raise LookupFailed(f"book {book_id} has no {fmt} file; it has {', '.join(sorted(formats)) or 'no files'}")
        if fmt not in READABLE_FORMATS:
            raise LookupFailed(f"{fmt} cannot be read as text; readable formats are {', '.join(READABLE_FORMATS)}")
    else:
        fmt = next((f for f in READABLE_FORMATS if f in formats), None)
        if fmt is None:
            raise LookupFailed(f"book {book_id} has no EPUB or TXT file to read "
                               f"(it has {', '.join(sorted(formats)) or 'no files'})")
    path = _book_file(folder, row[4], formats[fmt], fmt)
    if not path.is_file():
        raise LookupFailed(f"the {fmt} file of book {book_id} is missing from the library folder")
    extra: dict[str, Any] = {}
    try:
        if fmt == "EPUB":
            text, chapters, skipped = booktext.epub_text(path)
            if skipped:
                extra["skipped_documents"] = skipped
        else:
            text, encoding = booktext.txt_text(path)
            chapters = []
            extra["encoding"] = encoding
    except booktext.BookUnreadable as exc:
        raise LookupFailed(f"book {book_id} ({fmt}): {exc}") from None
    chunk = text[offset:offset + limit_chars]
    end = offset + len(chunk)
    payload: dict[str, Any] = {"library": name, "book_id": book_id, "title": row[1], "format": fmt,
                               "text": chunk, "offset": offset, "total_chars": len(text),
                               "next_offset": end if end < len(text) else None, **extra}
    if offset == 0 and chapters:
        payload["chapters"] = chapters
    return payload
