"""Tabular formats: csv (stdlib) and xlsx (openpyxl, optional).

Both render to a Markdown table so the model reads columns/rows clearly.
Row count is capped during parse (O2); the caller applies the char cap
on top.
"""

from __future__ import annotations

import csv
import io

from payloads.agent.runtime.extraction.models import ExtractionError
from payloads.agent.runtime.extraction.safety import guard_zip


def parse_csv(data: bytes, max_rows: int) -> tuple[str, bool]:
    stream = io.StringIO(data.decode("utf-8", errors="replace"))
    rows: list[list[str]] = []
    truncated = False
    # ``csv.Error`` (e.g. NUL bytes or stray newlines from a binary file
    # renamed ``.csv``) is a user-safe rejection, not an unexpected crash.
    try:
        for index, row in enumerate(csv.reader(stream)):
            if index >= max_rows:
                truncated = True
                break
            rows.append([_cell_to_str(value) for value in row])
    except csv.Error as exc:
        raise ExtractionError(f"file is not valid CSV: {exc}") from exc
    return _rows_to_markdown(rows), truncated


def parse_xlsx(data: bytes, max_rows: int) -> tuple[str, bool]:
    # Reject decompression-bomb-shaped archives before openpyxl touches
    # the zip+XML container (§7).  XXE on the stdlib XML path is defused by
    # the extractor worker; the hard upload size cap is enforced upstream.
    guard_zip(data)
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ExtractionError(
            "xlsx extraction requires the 'openpyxl' package (install agent-foundry[extraction])",
        ) from exc
    # read_only streams rows without materialising the whole sheet;
    # data_only returns computed values rather than formula strings.
    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sections: list[str] = []
    truncated = False
    for sheet in workbook.worksheets:
        rows: list[list[str]] = []
        for index, row in enumerate(sheet.iter_rows(values_only=True)):
            if index >= max_rows:
                truncated = True
                break
            rows.append([_cell_to_str(value) for value in row])
        if rows:
            sections.append(f"## {sheet.title}\n\n{_rows_to_markdown(rows)}")
    workbook.close()
    return "\n\n".join(sections), truncated


def _cell_to_str(value: object) -> str:
    return "" if value is None else str(value)


def _md_cell(value: str) -> str:
    # Keep cell content from breaking the Markdown table grid.
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _rows_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    header = rows[0]
    width = len(header)
    lines = [
        "| " + " | ".join(_md_cell(cell) for cell in header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    for row in rows[1:]:
        # Pad/truncate ragged rows to the header width so the grid stays valid.
        cells = (row + [""] * width)[:width]
        lines.append("| " + " | ".join(_md_cell(cell) for cell in cells) + " |")
    return "\n".join(lines)
