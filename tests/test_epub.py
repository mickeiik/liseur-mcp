from __future__ import annotations

import io
import zipfile

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
