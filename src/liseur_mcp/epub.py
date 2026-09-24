"""Minimal EPUB 2/3 text extraction with the standard library.

Reads the container, follows the OPF spine, and turns each XHTML document
into plain text. Navigation documents, stylesheets, scripts and images are
ignored. Good enough to hand a book's chapters to an agent; it is not a
rendering engine.
"""

from __future__ import annotations

import io
import posixpath
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import unquote
from xml.etree import ElementTree

_CONTAINER_PATH = "META-INF/container.xml"
_BLOCK_TAGS = frozenset(
    {
        "address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption",
        "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li",
        "main", "nav", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
    }
)
_SKIP_TAGS = frozenset({"head", "script", "style"})
_CHAPTER_MEDIA_TYPES = frozenset(
    {"application/xhtml+xml", "text/html", "application/xml", "text/xml"}
)
# ``ZipInfo.file_size`` comes from the archive and is attacker-controlled, so it
# cannot gate a read: CPython's ``read(-1)`` decompresses the whole stream and
# only then slices. These caps are enforced on the bytes actually read, via a
# capped ``handle.read(cap + 1)``. That bound holds only for DEFLATE, whose
# ``max_length`` CPython honours inside the zlib call; the BZIP2 and LZMA
# branches decompress with no limit and slice afterwards, so entries using them
# are refused rather than read (see ``_read_entry``). A real container.xml is a
# couple of KiB and a real OPF well under a few hundred KiB, so the metadata cap
# is deliberately far tighter than the per-document one.
_MAX_METADATA_BYTES = 1 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
_MAX_BOOK_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class Chapter:
    index: int
    title: str
    text: str


class _TextExtractor(HTMLParser):
    """Collect visible text, using block boundaries as paragraph breaks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._title: list[str] = []
        self._skip_depth = 0
        self._title_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            # A document missing </head> would otherwise swallow its whole body.
            self._skip_depth = 0
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._title_depth += 1
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self._title.append(data)
        elif not self._skip_depth:
            self._parts.append(data)

    @property
    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self._parts).splitlines())
        return "\n\n".join(line for line in lines if line)

    @property
    def chapter_title(self) -> str:
        return " ".join("".join(self._title).split())


def _local_name(tag: str) -> str:
    return tag.rpartition("}")[2]


def _iter_local(root: ElementTree.Element, name: str) -> Iterator[ElementTree.Element]:
    return (element for element in root.iter() if _local_name(element.tag) == name)


def _read_entry(archive: zipfile.ZipFile, name: str, cap: int) -> bytes:
    """Read one entry, bounding decompressed output to ``cap`` bytes."""
    # ``getinfo`` raises ``KeyError`` for a missing entry, which callers rely on
    # (``parse_epub`` uses ``_has_entry`` to fall back between href spellings).
    info = archive.getinfo(name)
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise ValueError(
            f"EPUB entry {name!r} uses unsupported compression "
            f"(type {info.compress_type}); the cap cannot bound it, so it is refused"
        )
    try:
        with archive.open(name) as handle:
            data = handle.read(cap + 1)
    except zipfile.BadZipFile as exc:
        # A forged (too small) declared size makes CPython stop at the lie and
        # fail the end-of-stream CRC check instead of reading the real bytes;
        # reject the entry rather than trust the metadata.
        raise ValueError(f"EPUB entry {name!r} could not be read: {exc}") from exc
    if len(data) > cap:
        raise ValueError(f"EPUB entry {name!r} expands past the {cap}-byte cap")
    return data


def _has_entry(archive: zipfile.ZipFile, name: str) -> bool:
    try:
        archive.getinfo(name)
    except KeyError:
        return False
    return True


def _opf_path(archive: zipfile.ZipFile) -> str:
    container = ElementTree.fromstring(
        _read_entry(archive, _CONTAINER_PATH, _MAX_METADATA_BYTES)
    )
    for rootfile in _iter_local(container, "rootfile"):
        full_path = rootfile.get("full-path")
        if full_path:
            return full_path
    raise ValueError("EPUB has no rootfile in META-INF/container.xml")


def parse_epub(data: bytes) -> tuple[str | None, list[Chapter]]:
    """Return the book title and its spine documents as chapters."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        opf_path = _opf_path(archive)
        package = ElementTree.fromstring(_read_entry(archive, opf_path, _MAX_METADATA_BYTES))
        opf_dir = posixpath.dirname(opf_path)
        manifest = {
            item.get("id"): (item.get("href"), item.get("media-type"))
            for item in _iter_local(package, "item")
            if item.get("id") and item.get("href")
        }
        title = next(
            (element.text for element in _iter_local(package, "title") if element.text), None
        )
        book_name = title or opf_path
        total = 0
        chapters: list[Chapter] = []
        for itemref in _iter_local(package, "itemref"):
            idref = itemref.get("idref")
            entry = manifest.get(idref) if idref else None
            if entry is None:
                continue
            href, media_type = entry
            if not href:
                continue
            declared_type = media_type.split(";", 1)[0].strip().lower() if media_type else ""
            if declared_type and declared_type not in _CHAPTER_MEDIA_TYPES:
                continue
            raw_href = href.split("#", 1)[0]
            # Try the percent-decoded path first, then the raw one: both
            # conventions occur in the wild, and some archives literally store
            # the percent-encoded name.
            candidates = (
                posixpath.normpath(posixpath.join(opf_dir, unquote(raw_href))),
                posixpath.normpath(posixpath.join(opf_dir, raw_href)),
            )
            path = next((name for name in candidates if _has_entry(archive, name)), None)
            if path is None:
                continue
            document = _read_entry(archive, path, _MAX_DOCUMENT_BYTES)
            if total + len(document) > _MAX_BOOK_BYTES:
                raise ValueError(
                    f"EPUB {book_name!r} exceeds the {_MAX_BOOK_BYTES}-byte book budget"
                )
            total += len(document)
            extractor = _TextExtractor()
            extractor.feed(document.decode("utf-8", errors="replace"))
            text = extractor.text
            if not text:
                continue
            chapter_title = extractor.chapter_title or f"Chapter {len(chapters) + 1}"
            chapters.append(Chapter(len(chapters), chapter_title, text))
        return title, chapters
