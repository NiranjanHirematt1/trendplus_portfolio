"""
Tests for the multi-horizon period-return pipeline in analytics_engine.py
and for writer/DB column parity.

Run:
    cd trendplus
    python -m pytest backend/tests/test_period_changes.py -v
(pytest is not otherwise wired into this repo; install with `pip install pytest`.)
"""
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0, str(ROOT))

from analytics_engine import (          # noqa: E402
    PERIOD_CHANGES,
    NEW_PERIOD_CHANGES,
    FLEXIBLE_FILL_MAX_SESSIONS,
    compute_period_change,
    compute_all_period_changes,
    compute_period_changes,
    fill_flexible_period_changes,
)


def _pivot(rows: dict, ncols: int) -> pd.DataFrame:
    """Build a symbols × dates close pivot from {symbol: [values]}."""
    cols = pd.date_range("2025-01-01", periods=ncols, freq="D")
    return pd.DataFrame(rows, index=cols).T  # index=symbol, columns=dates


# ─────────────────────────────────────────────────────────────────────
#  compute_period_change — exact N-session offset
# ─────────────────────────────────────────────────────────────────────
def test_period_change_exact_offsets():
    # 6 sessions: 10,20,30,40,50,60  → today = 60
    piv = _pivot({"AAA": [10, 20, 30, 40, 50, 60]}, 6)
    # 1 session back = 50 → +20%
    assert compute_period_change(piv, 1)["AAA"] == pytest.approx(20.0)
    # 3 sessions back = 30 → +100%
    assert compute_period_change(piv, 3)["AAA"] == pytest.approx(100.0)
    # 5 sessions back = 10 → +500%
    assert compute_period_change(piv, 5)["AAA"] == pytest.approx(500.0)


def test_period_change_insufficient_history_is_nan():
    piv = _pivot({"AAA": [100, 110, 120]}, 3)   # only 3 columns
    # 3-session lookback needs 4 columns → NaN
    assert np.isnan(compute_period_change(piv, 3)["AAA"])
    # 252-session lookback → NaN
    assert np.isnan(compute_period_change(piv, 252)["AAA"])


def test_compute_period_change_matches_legacy_12d_and_5d_anomaly():
    """chg_12d is a true 12-session move; chg_5d legacy is a 6-session move."""
    n = 20
    vals = list(np.linspace(100, 200, n))
    piv = _pivot({"AAA": vals}, n)
    matrix_dates = piv.columns[-12:]
    chg_12d, chg_5d = compute_period_changes(piv, matrix_dates)
    # chg_12d == generic 12-session change
    assert chg_12d["AAA"] == pytest.approx(compute_period_change(piv, 12)["AAA"])
    # chg_5d (labelled "5d") actually equals the SIX-session change — documented anomaly
    assert chg_5d["AAA"] == pytest.approx(compute_period_change(piv, 6)["AAA"])
    # ...and is NOT the true 5-session change
    assert chg_5d["AAA"] != pytest.approx(compute_period_change(piv, 5)["AAA"])


# ─────────────────────────────────────────────────────────────────────
#  compute_all_period_changes — registry-driven new horizons
# ─────────────────────────────────────────────────────────────────────
def test_compute_all_period_changes_columns_and_nan():
    n = 5   # chg_3d valid (needs 4 cols), everything longer NaN
    piv = _pivot({"AAA": [10, 20, 30, 40, 50]}, n)
    df = compute_all_period_changes(piv)
    assert list(df.columns) == NEW_PERIOD_CHANGES
    # chg_3d: 3 sessions back from 50 = 20 → +150%
    assert df.loc["AAA", "chg_3d"] == pytest.approx(150.0)
    for col in ["chg_1m", "chg_2m", "chg_3m", "chg_6m", "chg_12m"]:
        assert np.isnan(df.loc["AAA", col])


# ─────────────────────────────────────────────────────────────────────
#  fill_flexible_period_changes — short filled, long left NaN
# ─────────────────────────────────────────────────────────────────────
def test_flexible_fill_short_horizon_filled_long_left_nan():
    n = 30
    # Symbol appears only in the last two columns (short history)
    row = [np.nan] * (n - 2) + [100.0, 110.0]
    piv = _pivot({"BBB": row}, n)

    chg_3d  = compute_period_change(piv, PERIOD_CHANGES["chg_3d"])   # NaN (no col at -4)
    chg_1m  = compute_period_change(piv, PERIOD_CHANGES["chg_1m"])   # NaN
    assert np.isnan(chg_3d["BBB"])
    assert np.isnan(chg_1m["BBB"])

    filled = fill_flexible_period_changes(piv, {"chg_3d": chg_3d, "chg_1m": chg_1m})
    # chg_3d (3 <= 12) filled from earliest close in trailing 3-col window: (110/100-1)*100 = 10
    assert filled["chg_3d"]["BBB"] == pytest.approx(10.0)
    # chg_1m (21 > 12) NOT filled — stays NaN
    assert np.isnan(filled["chg_1m"]["BBB"])


def test_flexible_fill_never_touches_existing_values():
    n = 10
    piv = _pivot({"CCC": list(range(1, n + 1))}, n)   # 1..10, dense
    chg_3d = compute_period_change(piv, 3)
    before = chg_3d["CCC"]
    filled = fill_flexible_period_changes(piv, {"chg_3d": chg_3d})
    assert filled["chg_3d"]["CCC"] == pytest.approx(before)


def test_flexible_fill_cap_constant_sane():
    # Long horizons must be excluded from the flexible fill.
    assert PERIOD_CHANGES["chg_12d"] <= FLEXIBLE_FILL_MAX_SESSIONS
    assert PERIOD_CHANGES["chg_1m"]  > FLEXIBLE_FILL_MAX_SESSIONS


# ─────────────────────────────────────────────────────────────────────
#  Writer / DB column parity
# ─────────────────────────────────────────────────────────────────────
NEW_COLS = {"chg_3d", "chg_1m", "chg_2m", "chg_3m", "chg_6m", "chg_12m"}


def _insert_columns(path: Path) -> set:
    """Extract the trend_results INSERT column list from a writer's source."""
    src = path.read_text(encoding="utf-8")
    m = re.search(
        r"insert\s+into\s+trend_results\s*\((.*?)\)\s*values",
        src, re.IGNORECASE | re.DOTALL,
    )
    assert m, f"no trend_results INSERT found in {path.name}"
    cols = []
    for tok in m.group(1).split(","):
        tok = tok.strip()
        if tok and re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", tok):
            cols.append(tok)
    return set(cols)


def test_new_columns_present_in_all_three_writers():
    for rel in ["scripts/compute_today.py",
                "backend/app/services/engine_db.py",
                "scripts/compute_all_dates.py"]:
        cols = _insert_columns(ROOT / rel)
        missing = NEW_COLS - cols
        assert not missing, f"{rel} missing period columns: {missing}"


def test_full_writers_have_identical_column_sets():
    """compute_today.py and engine_db.py are the two full writers — identical set."""
    a = _insert_columns(ROOT / "scripts/compute_today.py")
    b = _insert_columns(ROOT / "backend/app/services/engine_db.py")
    assert a == b, f"writer column drift: only-today={a-b}  only-engine={b-a}"


def test_migration_and_schema_have_new_columns():
    mig = (ROOT / "sql/migration_v11_period_returns.sql").read_text(encoding="utf-8").lower()
    sch = (ROOT / "scripts/supabase_schema.sql").read_text(encoding="utf-8").lower()
    for col in NEW_COLS:
        assert col in mig, f"migration missing {col}"
        assert col in sch, f"schema create-table missing {col}"
