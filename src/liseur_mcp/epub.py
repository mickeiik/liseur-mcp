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


def _opf_path(archive: zipfile.ZipFile) -> str:
    container = ElementTree.fromstring(archive.read(_CONTAINER_PATH))
    for rootfile in _iter_local(container, "rootfile"):
        full_path = rootfile.get("full-path")
        if full_path:
            return full_path
    raise ValueError("EPUB has no rootfile in META-INF/container.xml")


def parse_epub(data: bytes) -> tuple[str | None, list[Chapter]]:
    """Return the book title and its spine documents as chapters."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        opf_path = _opf_path(archive)
        package = ElementTree.fromstring(archive.read(opf_path))
        opf_dir = posixpath.dirname(opf_path)
        manifest = {
            item.get("id"): item.get("href")
            for item in _iter_local(package, "item")
            if item.get("id") and item.get("href")
        }
        title = next(
            (element.text for element in _iter_local(package, "title") if element.text), None
        )
        chapters: list[Chapter] = []
        for itemref in _iter_local(package, "itemref"):
            idref = itemref.get("idref")
            href = manifest.get(idref) if idref else None
            if not href:
                continue
            path = posixpath.normpath(posixpath.join(opf_dir, href.split("#", 1)[0]))
            try:
                document = archive.read(path)
            except KeyError:
                continue
            extractor = _TextExtractor()
            extractor.feed(document.decode("utf-8", errors="replace"))
            text = extractor.text
            if not text:
                continue
            chapter_title = extractor.chapter_title or f"Chapter {len(chapters) + 1}"
            chapters.append(Chapter(len(chapters), chapter_title, text))
        return title, chapters
