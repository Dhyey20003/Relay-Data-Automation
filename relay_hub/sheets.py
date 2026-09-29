"""Spreadsheet reading and safe spreadsheet writing.

Reading: XLSX files are opened with openpyxl, which never executes formulas or
macros. The value Excel last calculated and saved for a formula cell is used,
and the cell is flagged so the preview shows it came from a formula. CSV files
are read as plain text so identifiers keep their leading zeros.

Writing: every exported text cell is written as a string (never a formula).
Text that begins with =, @, +, -, tab or carriage return is prefixed with an
apostrophe so it cannot turn into a formula if the file is re-saved as CSV and
opened in a spreadsheet program (formula-injection protection).
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
import zipfile
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill

MAX_PREVIEW_ROWS = 25
_NUMBERISH = re.compile(r"^[+-]?\d[\d,]*(\.\d+)?$")


class SheetError(ValueError):
    pass


class CellValue:
    """A read cell: value plus flags (numeric, formula)."""
    __slots__ = ("value", "numeric", "formula")

    def __init__(self, value, numeric=False, formula=False):
        self.value, self.numeric, self.formula = value, numeric, formula


def _check_xlsx(path: Path) -> None:
    if not zipfile.is_zipfile(path):
        raise SheetError("The file is not a valid .xlsx workbook. Older .xls files must be saved as .xlsx first.")
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        if "xl/workbook.xml" not in names:
            raise SheetError("The file is not a valid Excel workbook.")
        total = sum(i.file_size for i in zf.infolist())
        if total > 1024 * 1024 * 1024:
            raise SheetError("The workbook expands to more than 1 GB and was rejected.")


def list_sheets(path: Path) -> list[str]:
    if path.suffix.lower() == ".csv":
        return ["CSV"]
    _check_xlsx(path)
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def _decode_csv(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise SheetError("Could not read the CSV file's text encoding.")


def read_rows(path: Path, sheet: str | None = None, max_rows: int | None = None) -> list[list[CellValue]]:
    """Return all rows as lists of CellValue (row index 0 == spreadsheet row 1)."""
    if path.suffix.lower() == ".csv":
        text = _decode_csv(path)
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        out = []
        for i, row in enumerate(csv.reader(io.StringIO(text), dialect)):
            if max_rows and i >= max_rows:
                break
            out.append([CellValue(v) for v in row])
        return out
    _check_xlsx(path)
    values_wb = load_workbook(path, read_only=True, data_only=True)
    formulas_wb = load_workbook(path, read_only=True, data_only=False)
    try:
        name = sheet or values_wb.sheetnames[0]
        if name not in values_wb.sheetnames:
            raise SheetError(f"Worksheet '{name}' was not found.")
        vs, fs = values_wb[name], formulas_wb[name]
        out = []
        for i, (vrow, frow) in enumerate(zip(vs.iter_rows(values_only=True), fs.iter_rows(values_only=True))):
            if max_rows and i >= max_rows:
                break
            cells = []
            for j, v in enumerate(vrow):
                f = frow[j] if j < len(frow) else None
                is_formula = isinstance(f, str) and f.startswith("=")
                numeric = isinstance(v, (int, float)) and not isinstance(v, bool)
                cells.append(CellValue(v, numeric=numeric, formula=is_formula))
            out.append(cells)
        return out
    finally:
        values_wb.close()
        formulas_wb.close()


def display(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def preview(path: Path, sheet: str | None) -> list[list[str]]:
    return [[display(c.value) for c in row] for row in read_rows(path, sheet, MAX_PREVIEW_ROWS)]


# ---------------------------------------------------------------- writing

def safe_text(value: str) -> str:
    if not value:
        return value
    first = value[0]
    if first in ("=", "@", "\t", "\r"):
        return "'" + value
    if first in ("+", "-") and not _NUMBERISH.match(value):
        return "'" + value
    return value


def _cell(ws, value, bold=False):
    if isinstance(value, bool):
        value = "Yes" if value else "No"
    if isinstance(value, (dt.date, dt.datetime)):
        c = WriteOnlyCell(ws, value=value)
        c.number_format = "yyyy-mm-dd"
    elif isinstance(value, (int, float)):
        c = WriteOnlyCell(ws, value=value)
    elif value is None:
        c = WriteOnlyCell(ws, value=None)
    else:
        c = WriteOnlyCell(ws, value=safe_text(str(value)))
        c.data_type = "s"  # never let openpyxl interpret text as a formula
    if bold:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="3B4A9C")
        c.alignment = Alignment(vertical="center")
    return c


def write_xlsx(path: Path, sheets: list[tuple[str, list[str], list[list]]], widths: dict | None = None) -> Path:
    """sheets = [(title, headers, rows)]. ISO date strings in date columns become real dates."""
    wb = Workbook(write_only=True)
    for title, headers, rows in sheets:
        ws = wb.create_sheet(title=re.sub(r"[\[\]:*?/\\]", "_", title)[:31] or "Sheet")
        for i, h in enumerate(headers):
            ws.column_dimensions[_col_letter(i)].width = (widths or {}).get(h, max(12, min(40, len(h) + 4)))
        ws.freeze_panes = "A2"
        ws.append([_cell(ws, h, bold=True) for h in headers])
        for row in rows:
            ws.append([_cell(ws, v) for v in row])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def write_csv(path: Path, headers: list[str], rows: list[list]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow([safe_text(h) for h in headers])
        for row in rows:
            w.writerow(["" if v is None else safe_text(display(v)) for v in row])
    return path
