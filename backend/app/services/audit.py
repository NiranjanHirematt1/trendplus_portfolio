"""
Admin audit trail.

Records every mutating admin action into `admin_audit_log` (see
sql/migration_v12_admin_audit_2fa.sql). Deliberately best-effort: a failure
to write an audit row must never mask or roll back the action the admin
actually performed, so callers can await this without a try/except and any
DB error is swallowed + logged here.
"""
import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def log_admin_action(
    pool,
    *,
    action: str,
    actor_label: str,
    admin_id: Optional[int] = None,
    target_type: Optional[str] = None,
    target_id: Optional[Any] = None,
    metadata: Optional[dict] = None,
    ip_address: Optional[str] = None,
) -> None:
    """Insert one audit row. Never raises."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """insert into admin_audit_log
                     (admin_id, actor_label, action, target_type, target_id, metadata, ip_address)
                   values ($1, $2, $3, $4, $5, $6::jsonb, $7)""",
                admin_id,
                actor_label,
                action,
                target_type,
                None if target_id is None else str(target_id),
                json.dumps(metadata or {}),
                ip_address,
            )
    except Exception as e:  # pragma: no cover - audit must never break the action
        logger.warning("Failed to write admin audit log (%s): %s", action, e)
