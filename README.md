# hermes-calibre-library

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that lets the agent search
your [calibre](https://calibre-ebook.com/) e-book libraries and read their EPUB and TXT books, on
your own machine and without internet access. It reads a library the way calibre leaves it on disk:
the `metadata.db` database and the `Author/Title (id)/` folders beside it. calibre itself does not
need to be installed or running.

Useful for questions like "which of my books are by this author", "what is the third book in this
series", "find the passage about X in my copy of Y", or "summarise chapter 4", answered from the
books you own.

This plugin is not affiliated with calibre or Kovid Goyal. It uses only Python's standard library.

## Tools

| Tool | What it does |
|---|---|
| `calibre_search` | Finds books. Every word of the query must appear in a book's title, authors, series, tags, publisher or identifiers (ISBN, ASIN, ...). Matching ignores case and accents, so `goncalves` finds Gonçalves. Filters narrow by author, series, exact tag, file format or language. It can sort by relevance, title, date added or publication date. Each result has the library, book id, title, authors, series and number, tags, formats, languages, publication date and rating in stars. |
| `calibre_book` | Everything calibre records about one book: publisher, dates, identifiers, its description as plain text, and each file with format, size, path inside the library, whether it is on disk and whether `calibre_read` can read it. |
| `calibre_read` | A book's text from its EPUB (preferred) or TXT file. EPUB documents are joined in reading order with headings marked `#`. Long books come back in parts (`next_offset`), and the first part lists where each chapter starts. TXT files are decoded as UTF-8, or as Windows-1252 when they are not valid UTF-8; the result says which. |

The tools are offered to the model only while at least one library is configured.

## Install

```bash
hermes plugins install calibre-library --enable
```

No dependencies: the plugin uses only the Python standard library (`sqlite3`, `zipfile`,
`xml.etree`, `html.parser`).

## Configure

```yaml
plugins:
  entries:
    calibre-library:
      settings:
        paths:                       # calibre library folders, each holding a metadata.db
          - "~/Calibre Library"
          - /data/books/Work Library
        max_chars: 12000             # optional; characters per calibre_read call (1000-100000)
```

Point `paths` at the folder you chose for the library in calibre, the one that holds
`metadata.db`. Each library's id is its folder name with spaces
and punctuation turned into `_`, for example `Calibre_Library`. With one library configured the
tools need no library argument. Folders without a `metadata.db` and duplicate ids are reported
under `unavailable` and do not hide the other libraries.

## What it does on your machine (disclosure)

- **Reads only the libraries you configure.** In each one it opens `metadata.db` and, for
  `calibre_read`, the one book file asked for. A book whose recorded folder leads outside the
  library (for example through `..`) is refused, and `calibre_book` reports it instead of a path.
- **Writes nothing.** `metadata.db` is opened read-only for each call and closed again:
  - calibre keeps the database in rollback-journal mode; both real libraries tested here
    (schema versions 23 and 25) do. In that mode `mode=ro` writes and creates nothing, and
    holds SQLite's shared read lock only while the call reads. A change calibre is making at
    that moment is therefore seen either whole or not at all: SQLite answers a commit attempted
    during those milliseconds with its usual "database is busy" until the read ends.
  - If a library's database is in WAL mode, plain `mode=ro` would write the `-shm` file. It is
    then opened with `immutable=1`, and refused while its `-wal` file still holds changes.
  - Nothing is cached between calls. The tests check every connection's open mode, that each one
    is closed, and that no file in any test library changes.
- **No network.** The test suite refuses to open any socket while all three tools run.
- **EPUB files are read in place.** Nothing is extracted to disk. Only the documents the EPUB's
  package lists are read. A package that declares a DTD or entities is refused (EPUB needs
  neither, and it is how XML entity bombs work). A single document that unpacks to more than
  32 MB, or a book over 128 MB in total, is refused too.
- **No credentials, no shell commands, no background work.**
- **Content is your books.** Book text and descriptions are returned to the model as they are.
  Scripts and styles are stripped and HTML is reduced to plain text.

## Limits

- Only EPUB and TXT can be read as text. PDF, MOBI, AZW3, CBR and other formats are listed by
  `calibre_book` but `calibre_read` declines them.
- Custom columns are not shown yet.
- Search covers titles, authors, series, tags, publishers and identifiers, not descriptions or
  the books' full text. Use `calibre_read` to read a book.
- EPUB spine documents that are not HTML (for example SVG or image pages) are skipped and counted
  in `skipped_documents`.
- Covers are not returned.

## Development

```bash
python -m pip install pytest
python -m pytest -c tests/pytest.ini tests
CALIBRE_LIBRARY_REAL="$HOME/Calibre Library" python -m pytest -c tests/pytest.ini tests -k real_calibre -s
CALIBRE_REAL_EPUBS=~/epubs python -m pytest -c tests/pytest.ini tests -k real_epub -s
hermes plugins validate .
```

The tests build calibre libraries with calibre's own table and column names and with values shaped
like a real library's. These include calibre's "no date" value (0101-01-01), an empty series
number, half-star ratings, accented names, a Windows-1252 TXT with CRLF line ends and
multi-chapter EPUB 3 books. They also cover what must be refused: a book folder leading out of
the library, an EPUB declaring entities, a zip bomb, a broken zip and a WAL-mode library with
unsaved changes.

`CALIBRE_LIBRARY_REAL` (library folders separated like `PATH`) runs a test against real
libraries. It opens every book, checks that every file the database lists is on disk, reads
every EPUB and TXT to the end, and checks that no file in the library changed.
`CALIBRE_REAL_EPUBS` runs the EPUB reader over a folder of real `.epub` files.

## License

MIT
