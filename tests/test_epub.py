from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree

import pytest

import liseur_mcp.epub as epub
from liseur_mcp.epub import parse_epub

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

OPF = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Test Book</dc:title>
  </metadata>
  <manifest>
    <item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="c1"/>
    <itemref idref="c2"/>
  </spine>
</package>"""


def _book() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", OPF)
        archive.writestr(
            "OEBPS/ch1.xhtml",
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>First</title>'
            "<style>p { color: red }</style></head><body><h1>One</h1>"
            "<p>Hello <em>world</em>.</p><p>Second &amp; last.</p>"
            "<script>alert(1)</script></body></html>",
        )
        archive.writestr(
            "OEBPS/ch2.xhtml",
            "<html><head><title></title></head><body><p>Another chapter.</p></body></html>",
        )
    return buffer.getvalue()


def test_parse_epub_reads_spine_in_order() -> None:
    title, chapters = parse_epub(_book())
    assert title == "Test Book"
    assert [chapter.title for chapter in chapters] == ["First", "Chapter 2"]
    assert "One" in chapters[0].text
    assert "Hello world." in chapters[0].text
    assert "Second & last." in chapters[0].text
    assert "color: red" not in chapters[0].text
    assert "alert" not in chapters[0].text
    assert chapters[1].text == "Another chapter."


def test_parse_epub_namespaces_are_optional() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        # No namespace anywhere: some hand-made EPUBs look like this.
        archive.writestr(
            "META-INF/container.xml",
            '<container version="1.0"><rootfiles>'
            '<rootfile full-path="book.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "book.opf",
            "<package><metadata><title>Naked</title></metadata>"
            '<manifest><item id="x" href="x.xhtml"/></manifest>'
            '<spine><itemref idref="x"/></spine></package>',
        )
        archive.writestr("x.xhtml", "<html><body><p>Body.</p></body></html>")
    title, chapters = parse_epub(buffer.getvalue())
    assert title == "Naked"
    assert chapters[0].text == "Body."


def test_parse_epub_unquotes_percent_encoded_hrefs() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Encoded</dc:title>'
            "</metadata>"
            "<manifest>"
            '<item id="c1" href="Text/Chapter%201.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="c2" href="Text/Chapter%202.xhtml" media-type="application/xhtml+xml"/>'
            "</manifest>"
            '<spine><itemref idref="c1"/><itemref idref="c2"/></spine>'
            "</package>",
        )
        archive.writestr(
            "OEBPS/Text/Chapter 1.xhtml", "<html><body><p>One here.</p></body></html>"
        )
        archive.writestr(
            "OEBPS/Text/Chapter 2.xhtml", "<html><body><p>Two here.</p></body></html>"
        )
    title, chapters = parse_epub(buffer.getvalue())
    assert title == "Encoded"
    assert [chapter.text for chapter in chapters] == ["One here.", "Two here."]


def test_parse_epub_missing_head_end_still_yields_body() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr(
            "OEBPS/content.opf",
            "<package><metadata><title>Headless</title></metadata>"
            '<manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
            "</manifest><spine><itemref idref=\"c1\"/></spine></package>",
        )
        archive.writestr(
            "OEBPS/c1.xhtml",
            "<html><head><title>T</title><body><p>Real text.</p></body></html>",
        )
    title, chapters = parse_epub(buffer.getvalue())
    assert title == "Headless"
    assert chapters[0].text == "Real text."


def test_parse_epub_skips_non_text_spine_items() -> None:
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 16
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Mixed</dc:title>'
            "</metadata>"
            "<manifest>"
            '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="pic" href="pic.png" media-type="image/png"/>'
            "</manifest>"
            '<spine><itemref idref="c1"/><itemref idref="pic"/></spine>'
            "</package>",
        )
        archive.writestr("OEBPS/c1.xhtml", "<html><body><p>Only text.</p></body></html>")
        archive.writestr("OEBPS/pic.png", png)
    title, chapters = parse_epub(buffer.getvalue())
    assert title == "Mixed"
    assert len(chapters) == 1
    assert chapters[0].text == "Only text."
    assert "PNG" not in chapters[0].text
    assert "IHDR" not in chapters[0].text


def test_parse_epub_accepts_a_media_type_with_parameters_or_caps() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr(
            "OEBPS/content.opf",
            "<package><metadata><title>Params</title></metadata>"
            "<manifest>"
            '<item id="c1" href="c1.xhtml" media-type="application/XHTML+XML; charset=utf-8"/>'
            '<item id="pic" href="pic.png" media-type="image/png"/>'
            "</manifest>"
            '<spine><itemref idref="c1"/><itemref idref="pic"/></spine></package>',
        )
        archive.writestr("OEBPS/c1.xhtml", "<html><body><p>Still text.</p></body></html>")
        archive.writestr("OEBPS/pic.png", b"\x89PNG\r\n\x1a\n")
    title, chapters = parse_epub(buffer.getvalue())
    assert title == "Params"
    assert [chapter.text for chapter in chapters] == ["Still text."]


def test_parse_epub_refuses_oversized_document() -> None:
    oversized = "x" * (17 * 1024 * 1024)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr(
            "OEBPS/content.opf",
            "<package><metadata><title>Heavy</title></metadata>"
            '<manifest><item id="big" href="big.xhtml" media-type="application/xhtml+xml"/>'
            "</manifest><spine><itemref idref=\"big\"/></spine></package>",
        )
        archive.writestr("OEBPS/big.xhtml", f"<html><body><p>{oversized}</p></body></html>")
    with pytest.raises(ValueError) as excinfo:
        parse_epub(buffer.getvalue())
    assert "big.xhtml" in str(excinfo.value)


def _forge_declared_size(data: bytes, name: str, declared: int) -> bytes:
    """Rewrite the central-directory uncompressed size for one entry.

    The reader takes entry metadata from the central directory, so a forged
    (small) declared size is what the old declared-size guards trusted.
    """
    buf = bytearray(data)
    offset = 0
    while True:
        offset = buf.find(b"PK\x01\x02", offset)
        assert offset >= 0, f"central directory record for {name} not found"
        name_len = int.from_bytes(buf[offset + 28 : offset + 30], "little")
        extra_len = int.from_bytes(buf[offset + 30 : offset + 32], "little")
        comment_len = int.from_bytes(buf[offset + 32 : offset + 34], "little")
        entry_name = bytes(buf[offset + 46 : offset + 46 + name_len])
        if entry_name == name.encode():
            buf[offset + 24 : offset + 28] = declared.to_bytes(4, "little")
            return bytes(buf)
        offset += 46 + name_len + extra_len + comment_len


def _single_doc_book(name: str, href: str, body: str, title: str = "Book") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr(
            "OEBPS/content.opf",
            f'<package><metadata><title>{title}</title></metadata>'
            f'<manifest><item id="c1" href="{href}" media-type="application/xhtml+xml"/>'
            "</manifest>"
            '<spine><itemref idref="c1"/></spine></package>',
        )
        archive.writestr(name, body)
    return buffer.getvalue()


def test_parse_epub_rejects_forged_declared_size() -> None:
    body = "<html><body><p>" + "x" * (32 * 1024 * 1024) + "</p></body></html>"
    data = _single_doc_book("OEBPS/big.xhtml", "big.xhtml", body, title="Forged")
    data = _forge_declared_size(data, "OEBPS/big.xhtml", 1000)
    with pytest.raises(ValueError) as excinfo:
        parse_epub(data)
    assert "big.xhtml" in str(excinfo.value)


def test_parse_epub_rejects_oversized_container() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<container version="1.0"><rootfiles>'
            '<rootfile full-path="book.opf"/></rootfiles></container>'
            + "x" * (1 * 1024 * 1024 + 1),
        )
        archive.writestr("book.opf", "<package/>")
    with pytest.raises(ValueError) as excinfo:
        parse_epub(buffer.getvalue())
    assert "container.xml" in str(excinfo.value)


def test_parse_epub_rejects_oversized_opf() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<container version="1.0"><rootfiles>'
            '<rootfile full-path="book.opf"/></rootfiles></container>',
        )
        archive.writestr("book.opf", "<package>" + "x" * (1 * 1024 * 1024 + 1))
    with pytest.raises(ValueError) as excinfo:
        parse_epub(buffer.getvalue())
    assert "book.opf" in str(excinfo.value)


def test_parse_epub_accepts_metadata_just_under_the_cap() -> None:
    # A container padded to just under 1 MiB must still parse: the tighter
    # metadata cap must not be so low that real books break.
    pad = "x" * (1 * 1024 * 1024 - len(CONTAINER) - 16)
    container = CONTAINER.replace("</container>", f"<!--{pad}--></container>")
    assert len(container) < 1 * 1024 * 1024
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", OPF)
        archive.writestr("OEBPS/ch1.xhtml", "<html><body><p>Under cap.</p></body></html>")
        archive.writestr("OEBPS/ch2.xhtml", "<html><body><p>Two.</p></body></html>")
    title, chapters = parse_epub(buffer.getvalue())
    assert title == "Test Book"
    assert [chapter.text for chapter in chapters] == ["Under cap.", "Two."]


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_parse_epub_refuses_non_deflate_metadata_compression(method: int) -> None:
    # The cap only bounds DEFLATE output; a BZIP2/LZMA entry must be refused
    # before decompression, so a highly compressible payload never expands.
    payload = "x" * (4 * 1024 * 1024)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", payload, compress_type=method)
        archive.writestr("book.opf", "<package/>")
    with pytest.raises(ValueError) as excinfo:
        parse_epub(buffer.getvalue())
    message = str(excinfo.value)
    assert "container.xml" in message
    assert "unsupported compression" in message


def test_parse_epub_enforces_book_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(epub, "_MAX_BOOK_BYTES", 20 * 1024 * 1024)
    body = "<html><body><p>" + "x" * (15 * 1024 * 1024) + "</p></body></html>"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr(
            "OEBPS/content.opf",
            '<package><metadata><title>Budget</title></metadata>'
            "<manifest>"
            '<item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="b" href="b.xhtml" media-type="application/xhtml+xml"/>'
            "</manifest>"
            '<spine><itemref idref="a"/><itemref idref="b"/></spine></package>',
        )
        archive.writestr("OEBPS/a.xhtml", body)
        archive.writestr("OEBPS/b.xhtml", body)
    # Forge the declared sizes up so the declared-size guard the old code used
    # gives the wrong answer: only a budget on the bytes actually read trips.
    data = buffer.getvalue()
    data = _forge_declared_size(data, "OEBPS/a.xhtml", 32 * 1024 * 1024)
    data = _forge_declared_size(data, "OEBPS/b.xhtml", 32 * 1024 * 1024)
    with pytest.raises(ValueError) as excinfo:
        parse_epub(data)
    assert "Budget" in str(excinfo.value)


def test_parse_epub_budget_counts_bytes_actually_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Declared sizes lie LARGE here, so their sum (3 x 15 MiB) exceeds the
    # 20 MiB budget while the bytes actually read sum to a few KiB. A correct
    # (bytes-read) budget accepts the book; a declared-size budget would refuse.
    monkeypatch.setattr(epub, "_MAX_BOOK_BYTES", 20 * 1024 * 1024)
    body = "<html><body><p>" + "y" * 1024 + "</p></body></html>"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr(
            "OEBPS/content.opf",
            '<package><metadata><title>Honest</title></metadata>'
            "<manifest>"
            '<item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="b" href="b.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>'
            "</manifest>"
            '<spine><itemref idref="a"/><itemref idref="b"/><itemref idref="c"/></spine>'
            "</package>",
        )
        archive.writestr("OEBPS/a.xhtml", body)
        archive.writestr("OEBPS/b.xhtml", body)
        archive.writestr("OEBPS/c.xhtml", body)
    data = buffer.getvalue()
    for name in ("OEBPS/a.xhtml", "OEBPS/b.xhtml", "OEBPS/c.xhtml"):
        data = _forge_declared_size(data, name, 15 * 1024 * 1024)
    title, chapters = parse_epub(data)
    assert title == "Honest"
    assert len(chapters) == 3
    assert all(chapter.text == "y" * 1024 for chapter in chapters)


def test_parse_epub_finds_raw_percent_named_entry() -> None:
    data = _single_doc_book(
        "OEBPS/Chapter%201.xhtml",
        "Chapter%201.xhtml",
        "<html><body><p>Literal percent name.</p></body></html>",
    )
    _, chapters = parse_epub(data)
    assert [chapter.text for chapter in chapters] == ["Literal percent name."]


@pytest.mark.parametrize("media_type", ["application/xml", "text/xml"])
def test_parse_epub_parses_xml_spine_items(media_type: str) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr(
            "OEBPS/content.opf",
            "<package><metadata><title>Xml</title></metadata>"
            f'<manifest><item id="c1" href="c1.xml" media-type="{media_type}"/></manifest>'
            '<spine><itemref idref="c1"/></spine></package>',
        )
        archive.writestr("OEBPS/c1.xml", "<html><body><p>XML text.</p></body></html>")
    _, chapters = parse_epub(buffer.getvalue())
    assert [chapter.text for chapter in chapters] == ["XML text."]


def _bomb(tag: str, depth: int) -> str:
    """A deeply nested single-tag tree (no closing root tag of its own)."""
    return f"<{tag}>" * depth + f"</{tag}>" * depth


def test_parse_epub_refuses_element_bomb_in_opf() -> None:
    # ~900 KiB of tiny nested tags: under the 1 MiB byte cap, but the tree
    # ``fromstring`` built from it peaked at tens of MiB. The element bound
    # refuses it while the bytes are still being parsed.
    opf = f"<package><metadata><title>Bomb</title></metadata>{_bomb('a', 149_000)}</package>"
    assert len(opf) < 1 * 1024 * 1024
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", opf)
    with pytest.raises(ValueError) as excinfo:
        parse_epub(buffer.getvalue())
    message = str(excinfo.value)
    assert "content.opf" in message
    assert str(epub._MAX_METADATA_ELEMENTS) in message


def test_parse_epub_refuses_element_bomb_in_container() -> None:
    container = f"<container>{_bomb('a', 149_000)}</container>"
    assert len(container) < 1 * 1024 * 1024
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("book.opf", "<package/>")
    with pytest.raises(ValueError) as excinfo:
        parse_epub(buffer.getvalue())
    message = str(excinfo.value)
    assert "container.xml" in message
    assert str(epub._MAX_METADATA_ELEMENTS) in message


def test_parse_epub_accepts_a_large_legal_opf() -> None:
    # A real omnibus OPF: thousands of manifest items and itemrefs, but only a
    # handful of distinct documents. The element bound must sit above this.
    docs = 10
    items = 2_000
    manifest = "".join(
        f'<item id="m{i}" href="doc{i % docs}.xhtml" '
        f'media-type="application/xhtml+xml"/>'
        for i in range(items)
    )
    spine = "".join(f'<itemref idref="m{i}"/>' for i in range(items))
    opf = (
        "<package><metadata><title>Omnibus</title></metadata>"
        f"<manifest>{manifest}</manifest><spine>{spine}</spine></package>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", opf)
        for i in range(docs):
            archive.writestr(
                f"OEBPS/doc{i}.xhtml",
                f'<html><head><title>Doc {i}</title></head>'
                f"<body><p>Body of doc {i}.</p></body></html>",
            )
    title, chapters = parse_epub(buffer.getvalue())
    assert title == "Omnibus"
    assert len(chapters) == items
    assert chapters[0].title == "Doc 0"
    assert chapters[0].text == "Body of doc 0."
    assert chapters[items - 1].title == f"Doc {(items - 1) % docs}"
    assert chapters[items - 1].text == f"Body of doc {(items - 1) % docs}."


@pytest.mark.parametrize("broken", ["container", "opf"])
def test_parse_epub_rejects_malformed_metadata(broken: str) -> None:
    container = '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>'
    opf = '<package><metadata><title>Broken</title></metadata></package>'
    if broken == "container":
        container = "<container><rootfiles></container>"
    else:
        opf = "<package><metadata></package>"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("book.opf", opf)
    with pytest.raises(ElementTree.ParseError):
        parse_epub(buffer.getvalue())
