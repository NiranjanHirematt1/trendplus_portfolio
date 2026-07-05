from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
import re
from typing import Any

import pandas as pd

SYMBOL_KEYS = ("symbol", "stock name", "ticker", "scrip", "security", "instrument", "stock")
QTY_KEYS = ("quantity", "quantity available", "qty", "holding qty", "net qty", "available qty")
PRICE_KEYS = ("average buy price", "avg buy price", "average price", "avg price", "buy price", "cost price")
DATE_KEYS = ("buy date", "purchase date", "acquisition date", "trade date", "date")
ISIN_KEYS = ("isin",)
HEADER_SCAN_LIMIT = 60  # broker exports often have a metadata/summary preamble before the real table


@dataclass
class ImportRow:
    symbol: str
    quantity: Decimal
    avg_buy_price: Decimal
    buy_date: date | None
    requires_confirmation: bool
    source_row: int
    isin: str | None = None


def _norm_col(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _find_column(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    normalized = {_norm_col(c): c for c in columns}
    for candidate in candidates:
        if candidate in normalized:
            return normalized[candidate]
    for col in columns:
        ncol = _norm_col(col)
        if any(key in ncol for key in candidates):
            return col
    return None


def _row_matches_keys(cells: list[Any], candidates: tuple[str, ...]) -> bool:
    for cell in cells:
        ncell = _norm_col(cell)
        if not ncell:
            continue
        if ncell in candidates or any(key in ncell for key in candidates):
            return True
    return False


def _locate_header_row(raw: "pd.DataFrame") -> int:
    """Broker exports (Zerodha, Groww, Upstox, etc.) commonly prepend a client-name/
    summary block before the actual holdings table. Scan for the first row that looks
    like a real header (has a symbol-like cell AND a quantity-like cell AND a price-like
    cell) instead of assuming row 0 is the header."""
    limit = min(len(raw), HEADER_SCAN_LIMIT)
    for i in range(limit):
        cells = raw.iloc[i].tolist()
        if (_row_matches_keys(cells, SYMBOL_KEYS)
                and _row_matches_keys(cells, QTY_KEYS)
                and _row_matches_keys(cells, PRICE_KEYS)):
            return i
    return 0  # fall back to the original assumption if nothing better is found


def _read_table(filename: str, content: bytes) -> "pd.DataFrame":
    lower = filename.lower()

    # ---------------- CSV ----------------
    if lower.endswith(".csv"):
        raw = pd.read_csv(BytesIO(content), header=None)

        header_row = _locate_header_row(raw)
        header = raw.iloc[header_row].tolist()

        columns = [
            str(v).strip() if pd.notna(v) and str(v).strip()
            else f"col_{i}"
            for i, v in enumerate(header)
        ]

        df = raw.iloc[header_row + 1:].copy()
        df.columns = columns
        return df.reset_index(drop=True)

    # ---------------- Excel ----------------
    elif lower.endswith((".xlsx", ".xls")):

        xls = pd.ExcelFile(BytesIO(content))

        for sheet in xls.sheet_names:

            try:
                raw = pd.read_excel(
                    xls,
                    sheet_name=sheet,
                    header=None,
                    dtype=str
                )
            except Exception:
                continue

            if raw.empty:
                continue

            header_row = _locate_header_row(raw)

            header = raw.iloc[header_row].tolist()

            columns = [
                str(v).strip() if pd.notna(v) and str(v).strip()
                else f"col_{i}"
                for i, v in enumerate(header)
            ]

            symbol_col = _find_column(columns, SYMBOL_KEYS)
            qty_col = _find_column(columns, QTY_KEYS)
            price_col = _find_column(columns, PRICE_KEYS)

            if symbol_col and qty_col and price_col:

                df = raw.iloc[header_row + 1:].copy()
                df.columns = columns

                return df.reset_index(drop=True)

        raise ValueError(
            "Could not find a holdings table in any worksheet."
        )

    else:
        raise ValueError(
            "Unsupported file type. Upload CSV, XLS or XLSX."
        )


def _decimal(value: Any, field: str, row_num: int) -> Decimal:
    if pd.isna(value):
        raise ValueError(f"Row {row_num}: {field} is required")
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    try:
        parsed = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        raise ValueError(f"Row {row_num}: invalid {field}")
    if parsed <= 0:
        raise ValueError(f"Row {row_num}: {field} must be greater than zero")
    return parsed


def _date(value: Any) -> date | None:
    if value is None or pd.isna(value) or str(value).strip() == "":
        return None
    parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        raise ValueError(f"Invalid date: {value}")
    return parsed.date()


def parse_portfolio_file(filename: str, content: bytes) -> tuple[list[ImportRow], list[str]]:
    if not content:
        raise ValueError("Uploaded file is empty")
    try:
        df = _read_table(filename, content)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Could not read the uploaded file. Check that it is a valid broker export.") from exc

    if df.empty:
        raise ValueError("Uploaded file does not contain holdings")
    df = df.dropna(how="all")
    columns = list(df.columns)
    symbol_col = _find_column(columns, SYMBOL_KEYS)
    qty_col = _find_column(columns, QTY_KEYS)
    price_col = _find_column(columns, PRICE_KEYS)
    date_col = _find_column(columns, DATE_KEYS)
    isin_col = _find_column(columns, ISIN_KEYS)
    missing = []
    if not symbol_col:
        missing.append("Symbol")
    if not qty_col:
        missing.append("Quantity")
    if not price_col:
        missing.append("Average Buy Price")
    if missing:
        raise ValueError("Missing required columns: " + ", ".join(missing))

    rows: list[ImportRow] = []
    warnings: list[str] = []
    for idx, row in df.iterrows():
        row_num = int(idx) + 2
        raw_symbol = row.get(symbol_col)
        if pd.isna(raw_symbol) or str(raw_symbol).strip() == "":
            continue
        symbol = re.sub(r"[^A-Za-z0-9&\-]", "", str(raw_symbol).strip().upper())
        try:
            isin = None
            if isin_col:
                raw_isin = row.get(isin_col)
                if pd.notna(raw_isin) and str(raw_isin).strip():
                    isin = str(raw_isin).strip().upper()
            quantity = _decimal(row.get(qty_col), "quantity", row_num)
            price = _decimal(row.get(price_col), "average buy price", row_num)
        except ValueError as exc:
            # Don't let one bad/edge-case row (e.g. a locked-in bond showing 0 available
            # quantity) abort the whole file — skip it and keep going.
            warnings.append(f"Row {row_num} skipped for {symbol}: {exc}")
            continue
        buy_date = None
        if date_col:
            try:
                buy_date = _date(row.get(date_col))
            except ValueError as exc:
                warnings.append(f"Row {row_num}: {exc}")
        requires_confirmation = buy_date is None
        if requires_confirmation:
            warnings.append(f"Row {row_num}: purchase date missing for {symbol}")
        rows.append(ImportRow(symbol, quantity, price, buy_date, requires_confirmation, row_num, isin))
    if not rows:
        raise ValueError("No valid holding rows were found")
    return rows, warnings
