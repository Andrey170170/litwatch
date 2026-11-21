"""PDFs built by hand, of a page or of several, for the archive's PDF text and its page views."""


def a_pdf(*lines: str) -> bytes:
    """A one-page PDF with `lines` set in Helvetica, built by hand."""
    return pages_pdf(list(lines))


def pages_pdf(*pages: list[str]) -> bytes:
    """A PDF of `pages`, each the lines set on it in Helvetica (none: a blank page)."""
    n = len(pages)
    # 1: the catalog, 2: the page tree, 3: the font, then each page and its content.
    kids = b" ".join(b"%d 0 R" % (4 + 2 * i) for i in range(n))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % n,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, lines in enumerate(pages):
        shown = b" ".join(b"(" + line.encode() + b") Tj 0 -16 Td" for line in lines)
        content = b"BT /F1 12 Tf 72 720 Td " + shown + b" ET" if lines else b""
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
            b"/Resources << /Font << /F1 3 0 R >> >> >>" % (5 + 2 * i)
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)
