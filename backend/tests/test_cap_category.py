"""Regression test for the 2026-08-24 outage.

refresh_master_data.py seeded every Nifty500 member with CAP_CATEGORY="Other".
symbols.cap_category has CHECK (cap_category in ('Large Cap','Mid Cap','Small Cap','')),
so the first nightly run after the weekly master refresh aborted the whole
symbols upsert with CheckViolationError and no trend_results were written.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import pytest
from backend.app.core.sector_mapping import (
    VALID_CAP_CATEGORIES,
    normalize_cap_category,
)

# Mirrors the CHECK constraint in scripts/supabase_schema.sql.
DB_ALLOWED = {"Large Cap", "Mid Cap", "Small Cap", ""}


@pytest.mark.parametrize("good", VALID_CAP_CATEGORIES)
def test_valid_categories_pass_through(good):
    assert normalize_cap_category(good) == good


@pytest.mark.parametrize("bad", [
    "Other",          # the exact value that broke production
    "other",
    "Micro Cap",
    "Mega Cap",
    "large cap",      # wrong case is not silently upgraded
    "Large",
    " Large  Cap ",   # internal double space — not a real category
    None,
    float("nan"),
    "nan", "NaN", "None", "null",
    "", "   ",
    123,
])
def test_everything_else_becomes_empty(bad):
    assert normalize_cap_category(bad) == ""


@pytest.mark.parametrize("val", [
    "Other", None, "nan", "", "Large Cap", "Mid Cap", "Small Cap",
    "Micro Cap", 123, float("nan"), "  Small Cap  ",
])
def test_output_always_satisfies_db_constraint(val):
    """The property that actually matters: nothing can violate the CHECK."""
    assert normalize_cap_category(val) in DB_ALLOWED


def test_surrounding_whitespace_is_tolerated():
    assert normalize_cap_category("  Large Cap  ") == "Large Cap"


def test_refresh_master_data_no_longer_emits_other():
    """The source fix — belt and braces alongside the writer-side guard."""
    src = (ROOT / "scripts" / "refresh_master_data.py").read_text(encoding="utf-8")
    assert '"CAP_CATEGORY": "Other"' not in src


def test_every_symbols_writer_sanitizes():
    """Any future writer that forgets the guard fails here, not at 19:17 IST."""
    writers = [
        ROOT / "scripts" / "compute_today.py",
        ROOT / "scripts" / "backfill_to_supabase.py",
        ROOT / "backend" / "app" / "services" / "engine_db.py",
    ]
    for w in writers:
        src = w.read_text(encoding="utf-8")
        if "cap_category" not in src:
            continue
        assert "normalize_cap_category" in src, f"{w.name} writes cap_category unguarded"
