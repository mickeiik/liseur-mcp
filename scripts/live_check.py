#!/usr/bin/env python3
"""Shape-check a live liseur-sync through the client the server uses.

Read-only: it calls the same read paths the tools do and asserts the shapes
those tools depend on -- not their values, which are expected to change. A
renamed or dropped field here is exactly what would surface as a broken tool
later, so run it before a release, and schedule it wherever the instance is
reachable. The workflows in this repository do not run it: they target local
stubs and dummy credentials.

    LISEUR_URL=https://books.example.com LISEUR_TOKEN=... \
        uv run python scripts/live_check.py

Exit 0 when every shape holds, 1 when one does not (every failure is reported,
not just the first), 2 when the environment is missing. It never writes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from liseur_mcp.client import LiseurClient
from liseur_mcp.epub import parse_epub

# The same ceiling get_book_text enforces, so a pathological book fails here
# rather than after a scheduled run has buffered it.
MAX_BOOK_BYTES = 128 * 1024 * 1024

_failures: list[str] = []


def check(ok: bool, description: str, detail: Any = "") -> bool:
    if ok:
        print(f"ok:   {description}")
    else:
        message = f"{description} — {detail}" if detail else description
        _failures.append(message)
        print(f"FAIL: {message}")
    return ok


def list_of_dicts(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def missing_keys(records: list[dict[str, Any]], keys: set[str]) -> list[str]:
    return sorted({key for record in records for key in keys - record.keys()})


def token_from_environment() -> str | None:
    direct = os.environ.get("LISEUR_TOKEN")
    if direct:
        return direct
    path = os.environ.get("LISEUR_TOKEN_FILE")
    if path:
        return Path(path).read_text(encoding="utf-8").removesuffix("\n")
    return None


async def run_checks(client: LiseurClient) -> None:
    token = await client.token_info()
    if check(isinstance(token, dict), "GET /v1/token is an object", type(token).__name__):
        absent = missing_keys(
            [token], {"id", "account_id", "device_id", "name", "scopes", "session_active_ms"}
        )
        check(
            not absent,
            "token carries id/account_id/device_id/name/scopes/session_active_ms",
            f"missing {absent}",
        )
        check(
            isinstance(token.get("account_id"), str),
            "token account_id is a string",
            type(token.get("account_id")).__name__,
        )
        scopes = token.get("scopes")
        check(
            isinstance(scopes, list) and all(isinstance(scope, str) for scope in scopes),
            "token scopes is a list of strings",
            type(scopes).__name__,
        )

    folders = await client.folders()
    if not check(list_of_dicts(folders), "GET /v1/folders is a list of objects"):
        return
    if not folders:
        print("note: no folders on this account; nothing further to check")
        return
    absent = missing_keys(folders, {"folder_id", "name", "kind"})
    check(not absent, "every folder carries folder_id/name/kind", f"missing {absent}")

    folder_id = folders[0].get("folder_id")
    if not isinstance(folder_id, str):
        return

    books = await client.books(folder_id, limit=5)
    if not check(list_of_dicts(books), "GET /v1/folders/{id}/books is a list of objects"):
        return
    absent = missing_keys(books, {"book_id"})
    check(not absent, "every book carries book_id", f"missing {absent}")
    if not books:
        print("note: the first folder has no books; skipping the book-level checks")
    else:
        await check_book(client, books[0], folder_id)

    summary = await client.insights_summary("30d")
    check(
        isinstance(summary, dict),
        "GET /v1/insights/summary is an object",
        type(summary).__name__,
    )

    works = await client.insights_works("30d")
    check(isinstance(works, dict), "GET /v1/insights/works is an object", type(works).__name__)
    check(
        list_of_dicts(works.get("works", [])),
        "GET /v1/insights/works carries an object list under works",
        type(works.get("works")).__name__,
    )

    await check_annotations(client)


async def check_book(client: LiseurClient, book: dict[str, Any], folder_id: str) -> None:
    book_id = book.get("book_id")
    if not isinstance(book_id, str):
        return
    title = str(book.get("title") or "")[:40] or "a"

    results = await client.search(folder_id, title, limit=5)
    check(isinstance(results, dict), "GET /v1/folders/{id}/search is an object")
    check(
        list_of_dicts(results.get("books", [])),
        "the search answer carries an object list under books",
        type(results.get("books")).__name__,
    )

    record = await client.book(book_id)
    check(
        isinstance(record, dict) and isinstance(record.get("book_id"), str),
        "GET /v1/books/{id} is an object carrying book_id",
    )

    resolution = await client.resolve_book(book_id)
    check(
        isinstance(resolution, dict) and "confidence" in resolution,
        "POST /v1/books/{id}/resolve carries confidence",
    )
    if resolution.get("confidence") != "low":
        work_id = resolution.get("work_id")
        check(isinstance(work_id, str), "a non-low resolution carries work_id")
        if isinstance(work_id, str):
            annotations = await client.work_annotations(work_id)
            check(
                list_of_dicts(annotations),
                "GET /v1/works/{id}/annotations carries an object list under annotations",
            )

    data = await client.download(book_id, max_bytes=MAX_BOOK_BYTES)
    check(isinstance(data, bytes) and len(data) > 0, "GET /v1/books/{id}/download gives bytes")
    try:
        title_of_book, chapters = parse_epub(data)
    except Exception as exc:  # noqa: BLE001 - the parser's failure is the finding
        check(
            False,
            f"the EPUB of {book_id!r} parses into chapters",
            f"{type(exc).__name__}: {exc}",
        )
        return
    check(bool(chapters), f"the EPUB of {book_id!r} parses into chapters", title_of_book)
    if chapters:
        first = chapters[0]
        check(
            isinstance(first.index, int)
            and isinstance(first.title, str)
            and isinstance(first.text, str),
            "every chapter carries index/title/text",
        )
        check(len(first.text[:200]) > 0, "the first chapter has readable text")


async def check_annotations(client: LiseurClient) -> None:
    try:
        rows = await client.annotation_changes()
    except (KeyError, TypeError) as exc:
        # The client walks rows by id and seq; a rename there breaks it here.
        check(False, "GET /v1/annotations/changes is walkable by id/seq", repr(exc))
        return
    if not check(list_of_dicts(rows), "GET /v1/annotations/changes yields objects"):
        return
    absent = missing_keys(rows, {"id", "seq"})
    check(not absent, "every annotation change carries id/seq", f"missing {absent}")


async def main() -> int:
    url = os.environ.get("LISEUR_URL", "").strip().rstrip("/")
    token = token_from_environment()
    if not url or not token:
        print(
            "set LISEUR_URL and LISEUR_TOKEN (or LISEUR_TOKEN_FILE) to the instance to check",
            file=sys.stderr,
        )
        return 2
    timeout = float(os.environ.get("LISEUR_TIMEOUT_SECONDS", "30"))

    client = LiseurClient(url, token, timeout)
    try:
        await run_checks(client)
    except Exception as exc:  # noqa: BLE001 - a canary must report, never traceback
        # An upstream error mid-check is itself a finding: name it and exit 1
        # rather than dying with a stack trace a cron log cannot use.
        _failures.append(f"the check could not complete: {type(exc).__name__}: {exc}")
    finally:
        await client.aclose()

    if _failures:
        print(f"\n{len(_failures)} shape(s) drifted; the tools that read them would fail:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("\nlive check passed: every shape the tools read is intact")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
