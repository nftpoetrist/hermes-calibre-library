"""Tool schemas the model reads."""

CALIBRE_SEARCH = {
    "name": "calibre_search",
    "description": (
        "Search the user's calibre e-book libraries on this machine. Every word of the query must "
        "appear in a book's title, authors, series, tags, publisher or identifiers (ISBN etc.); "
        "matching ignores case and accents. Filters narrow by author, series, exact tag, file "
        "format or language. With no query and no filter, lists books in the chosen sort order. "
        "Returns each book's library, book_id, title, authors, series, tags, formats, languages, "
        "publication date and rating; pass library and book_id to calibre_book or calibre_read."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words to look for, e.g. 'tolkien hobbit' or an ISBN."},
            "author": {"type": "string", "description": "Only books with an author whose name contains this."},
            "series": {"type": "string", "description": "Only books in a series whose name contains this."},
            "tag": {"type": "string", "description": "Only books with exactly this tag (case and accents ignored)."},
            "format": {"type": "string", "description": "Only books with a file in this format, e.g. EPUB, PDF, MOBI."},
            "language": {"type": "string", "description": "Only books in this language, as calibre stores it (ISO 639-2, e.g. eng, deu, fra)."},
            "library": {"type": "string", "description": "Search only this library. Default: every configured library."},
            "sort": {
                "type": "string",
                "enum": ["relevance", "title", "added", "published"],
                "description": "relevance (default: titles starting with the query first), title, added (newest first) or published (newest first).",
            },
            "limit": {"type": "integer", "description": "Maximum number of results (1-50, default 10).", "minimum": 1, "maximum": 50},
        },
    },
}

CALIBRE_BOOK = {
    "name": "calibre_book",
    "description": (
        "Show everything calibre records about one book: title, authors, series, tags, publisher, "
        "publication and added dates, languages, rating, identifiers (ISBN, ASIN, ...), its "
        "description as plain text, and each file with its format, size, path inside the library "
        "and whether calibre_read can read it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "book_id": {"type": "integer", "description": "book_id from calibre_search."},
            "library": {"type": "string", "description": "Library from calibre_search. May be left out when only one library is configured."},
        },
        "required": ["book_id"],
    },
}

CALIBRE_READ = {
    "name": "calibre_read",
    "description": (
        "Read a book's text from its EPUB or TXT file in the calibre library (EPUB is preferred "
        "when both exist). EPUB chapters are joined in reading order; headings are marked with #. "
        "Long books come back in parts: pass the returned next_offset as offset to read on. The "
        "first part also lists where each chapter starts, as offsets you can jump to. PDF, MOBI, "
        "AZW3 and other formats cannot be read as text."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "book_id": {"type": "integer", "description": "book_id from calibre_search."},
            "library": {"type": "string", "description": "Library from calibre_search. May be left out when only one library is configured."},
            "format": {"type": "string", "enum": ["EPUB", "TXT"], "description": "Which file to read. Default: EPUB if the book has one, else TXT."},
            "offset": {"type": "integer", "description": "Character offset to start at (default 0).", "minimum": 0},
        },
        "required": ["book_id"],
    },
}
