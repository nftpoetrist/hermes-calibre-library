"""calibre libraries for Hermes: search your e-book catalog and read EPUB and TXT books.

Configure ``plugins.entries.calibre-library.settings.paths`` with calibre library folders (see
README.md). The plugin registers three fixed tools, ``calibre_search``, ``calibre_book`` and
``calibre_read``. They are offered to the model only while at least one path is configured. The
library database is opened read-only and nothing goes over the network.
"""
from __future__ import annotations

import json
import logging

from . import library as calibre
from . import schemas

logger = logging.getLogger(__name__)

TOOLSET = "calibre_library"


def _json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _error(message: str) -> str:
    return _json({"error": message})


def _text_arg(args: dict, key: str):
    """(value or None, error or None) for an optional non-empty string argument."""
    value = args.get(key)
    if value is None:
        return None, None
    if not isinstance(value, str) or not value.strip():
        return None, _error(f"{key} must be a non-empty string")
    return value.strip(), None


def _int_arg(args: dict, key: str, default=None, minimum=None, maximum=None):
    value = args.get(key, default)
    if value is None:
        return None, _error(f"{key} is required")
    if not isinstance(value, int) or isinstance(value, bool) or (minimum is not None and value < minimum) \
            or (maximum is not None and value > maximum):
        bounds = f" from {minimum} to {maximum}" if maximum is not None else (f" of at least {minimum}" if minimum is not None else "")
        return None, _error(f"{key} must be an integer{bounds}")
    return value, None


def register(ctx):
    def settings():
        paths = calibre.configured_paths(ctx.get_config("paths", default=[]))
        if not paths:
            raise calibre.SettingsError(
                "no calibre libraries are configured (plugins.entries.calibre-library.settings.paths)")
        return paths, calibre.max_chars(ctx.get_config("max_chars", default=None))

    def libraries():
        paths, limit_chars = settings()
        found, problems = calibre.discover(paths)
        return found, problems, limit_chars

    def run(name, work):
        try:
            return _json(work())
        except (calibre.SettingsError, calibre.LookupFailed) as exc:
            return _error(str(exc))
        except Exception as exc:
            logger.warning("%s failed: %s", name, exc)
            return _error(f"{name} failed: {exc}")

    def search(args: dict, **kwargs) -> str:
        query = args.get("query", "")
        if not isinstance(query, str):
            return _error("query must be a string")
        filters = {}
        for key in ("author", "series", "tag", "format", "language"):
            filters[key], problem = _text_arg(args, key)
            if problem:
                return problem
        library, problem = _text_arg(args, "library")
        if problem:
            return problem
        sort = args.get("sort", "relevance")
        if sort not in calibre.SORTS:
            return _error(f"sort must be one of {', '.join(calibre.SORTS)}")
        limit, problem = _int_arg(args, "limit", 10, 1, 50)
        if problem:
            return problem

        def work():
            found, problems, _ = libraries()
            return calibre.search(found, problems, query.strip(), filters, library, sort, limit)
        return run("calibre_search", work)

    def book(args: dict, **kwargs) -> str:
        book_id, problem = _int_arg(args, "book_id", minimum=1)
        if problem:
            return problem
        library, problem = _text_arg(args, "library")
        if problem:
            return problem

        def work():
            found, _problems, _ = libraries()
            return calibre.book(found, library, book_id)
        return run("calibre_book", work)

    def read(args: dict, **kwargs) -> str:
        book_id, problem = _int_arg(args, "book_id", minimum=1)
        if problem:
            return problem
        library, problem = _text_arg(args, "library")
        if problem:
            return problem
        fmt, problem = _text_arg(args, "format")
        if problem:
            return problem
        offset, problem = _int_arg(args, "offset", 0, 0)
        if problem:
            return problem

        def work():
            found, _problems, limit_chars = libraries()
            return calibre.read(found, library, book_id, fmt, offset, limit_chars)
        return run("calibre_read", work)

    def available() -> bool:
        try:
            return bool(settings()[0])
        except calibre.SettingsError:
            return False

    for name, schema, handler, description in (
        ("calibre_search", schemas.CALIBRE_SEARCH, search, "Search calibre e-book libraries"),
        ("calibre_book", schemas.CALIBRE_BOOK, book, "Show a calibre book's details"),
        ("calibre_read", schemas.CALIBRE_READ, read, "Read a calibre book's EPUB or TXT text"),
    ):
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler,
                          check_fn=available, description=description)
