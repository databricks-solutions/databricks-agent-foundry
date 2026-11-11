"""Tests for the shared file-extraction core.

stdlib-only formats (txt/md/json/csv) + caps + dispatch are covered
unconditionally; the optional binary parsers (xlsx/docx/pdf) are guarded
with ``importorskip`` so the suite passes whether or not the
``extraction`` extra is installed.
"""

from __future__ import annotations

import io
import json

import pytest

from payloads.agent.runtime.extraction import (
    ExtractedFile,
    ExtractionError,
    extract_file,
)


class TestTextFormats:
    def test_txt(self) -> None:
        result = extract_file(b"hello world", "notes.txt")
        assert isinstance(result, ExtractedFile)
        assert result.format == "txt"
        assert result.text == "hello world"
        assert result.truncated is False

    def test_md_uses_text_parser(self) -> None:
        result = extract_file(b"# Title", "readme.md")
        assert result.format == "md"
        assert result.text == "# Title"

    def test_non_utf8_bytes_degrade_gracefully(self) -> None:
        result = extract_file(b"caf\xe9", "x.txt")
        assert result.format == "txt"
        assert "\ufffd" in result.text  # U+FFFD replacement char

    def test_json_pretty_printed(self) -> None:
        result = extract_file(b'{"b":1,"a":2}', "data.json")
        assert result.format == "json"
        assert json.loads(result.text) == {"b": 1, "a": 2}
        assert "\n" in result.text  # re-emitted with indent

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ExtractionError, match="invalid JSON"):
            extract_file(b"{not json}", "bad.json")


class TestCsv:
    def test_csv_to_markdown(self) -> None:
        result = extract_file(b"a,b\n1,2\n3,4", "t.csv")
        assert result.format == "csv"
        assert "| a | b |" in result.text
        assert "| 1 | 2 |" in result.text
        assert result.truncated is False

    def test_csv_row_cap_marks_truncated(self) -> None:
        body = "h\n" + "\n".join(str(i) for i in range(10))
        result = extract_file(body.encode(), "t.csv", max_rows=3)
        assert result.truncated is True

    def test_csv_escapes_pipe(self) -> None:
        result = extract_file(b"a|b,c\n1,2", "t.csv")
        assert r"a\|b" in result.text


class TestCaps:
    def test_char_cap_truncates_with_notice(self) -> None:
        result = extract_file(b"x" * 100, "big.txt", max_chars=10)
        assert result.truncated is True
        assert result.text.startswith("x" * 10)
        assert "truncated" in result.text

    def test_under_cap_not_truncated(self) -> None:
        result = extract_file(b"short", "x.txt", max_chars=1000)
        assert result.truncated is False


class TestDispatch:
    def test_unsupported_format(self) -> None:
        with pytest.raises(ExtractionError, match="unsupported"):
            extract_file(b"...", "image.png")

    def test_no_extension(self) -> None:
        with pytest.raises(ExtractionError, match="unsupported"):
            extract_file(b"...", "Makefile")

    def test_extension_is_case_insensitive(self) -> None:
        result = extract_file(b"hi", "NOTES.TXT")
        assert result.format == "txt"


class TestZipSafety:
    """The zip-bomb guard runs before openpyxl/python-docx, so these are
    deterministic regardless of whether the optional parsers are installed."""

    def test_non_zip_xlsx_rejected(self) -> None:
        with pytest.raises(ExtractionError, match="not a valid"):
            extract_file(b"definitely not a zip", "fake.xlsx")

    def test_too_many_entries_rejected(self) -> None:
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for i in range(1001):  # > MAX_ZIP_ENTRIES
                archive.writestr(f"f{i}.xml", b"x")
        with pytest.raises(ExtractionError, match="too many entries"):
            extract_file(buffer.getvalue(), "bomb.docx")

    def test_oversized_uncompressed_total_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import zipfile

        from payloads.agent.runtime.extraction import safety

        monkeypatch.setattr(safety, "MAX_ZIP_UNCOMPRESSED_BYTES", 100)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("big.xml", b"x" * 101)
        with pytest.raises(ExtractionError, match="decompressed size"):
            extract_file(buffer.getvalue(), "bomb.xlsx")

    def test_high_compression_ratio_rejected(self) -> None:
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("zeros.xml", b"\0" * (1024 * 1024))  # deflates ~1000:1 > MAX_ZIP_COMPRESSION_RATIO
        with pytest.raises(ExtractionError, match="compression ratio"):
            extract_file(buffer.getvalue(), "bomb.docx")


class TestBinaryFormats:
    def test_xlsx_to_markdown(self) -> None:
        openpyxl = pytest.importorskip("openpyxl")
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Sheet1"
        sheet.append(["name", "qty"])
        sheet.append(["widget", 5])
        buffer = io.BytesIO()
        workbook.save(buffer)

        result = extract_file(buffer.getvalue(), "s.xlsx")
        assert result.format == "xlsx"
        assert "## Sheet1" in result.text
        assert "| name | qty |" in result.text
        assert "| widget | 5 |" in result.text

    def test_docx_paragraphs(self) -> None:
        docx = pytest.importorskip("docx")
        document = docx.Document()
        document.add_paragraph("First para")
        document.add_paragraph("Second para")
        buffer = io.BytesIO()
        document.save(buffer)

        result = extract_file(buffer.getvalue(), "d.docx")
        assert result.format == "docx"
        assert "First para" in result.text
        assert "Second para" in result.text

    def test_pdf_dispatch(self) -> None:
        pypdf = pytest.importorskip("pypdf")
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buffer = io.BytesIO()
        writer.write(buffer)

        result = extract_file(buffer.getvalue(), "f.pdf")
        assert result.format == "pdf"
