"""
Simple in-memory login rate limiting / lockout for the admin panel.

Keeps a sliding window of recent failed attempts keyed by "<username>|<ip>".
After MAX_FAILS failures within WINDOW_SECS, the key is locked out for
LOCKOUT_SECS. A successful login clears the key.

In-memory (per-process) on purpose — matches the existing lightweight stack
(no Redis). Good enough to blunt online password guessing; if the API is ever
run with multiple workers, each worker enforces its own share of the budget.
"""
import threading
import time
from collections import defaultdict
from typing import Deque
from collections import deque

MAX_FAILS = 5          # failures allowed within the window before lockout
WINDOW_SECS = 300      # 5 minutes
LOCKOUT_SECS = 900     # 15 minutes lockout once tripped

_lock = threading.Lock()
_fails: dict[str, Deque[float]] = defaultdict(deque)
_locked_until: dict[str, float] = {}


def _key(username: str, ip: str) -> str:
    return f"{(username or '').lower()}|{ip or '-'}"


def check_locked(username: str, ip: str) -> int:
    """Return remaining lockout seconds (0 if not locked)."""
    key = _key(username, ip)
    now = time.time()
    with _lock:
        until = _locked_until.get(key, 0)
        if until > now:
            return int(until - now) + 1
        if until:
            # lockout expired — clear it
            _locked_until.pop(key, None)
            _fails.pop(key, None)
    return 0


def record_failure(username: str, ip: str) -> None:
    """Record a failed attempt; trip a lockout if the threshold is crossed."""
    key = _key(username, ip)
    now = time.time()
    with _lock:
        dq = _fails[key]
        dq.append(now)
        # drop attempts outside the window
        cutoff = now - WINDOW_SECS
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= MAX_FAILS:
            _locked_until[key] = now + LOCKOUT_SECS


def clear(username: str, ip: str) -> None:
    """Clear failures/lockout after a successful login."""
    key = _key(username, ip)
    with _lock:
        _fails.pop(key, None)
        _locked_until.pop(key, None)
