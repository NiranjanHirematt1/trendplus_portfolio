"""
Admin Panel API — user approval workflow, user management, dashboard stats.

Fully separate from:
  - app.api.admin        (engine/data admin, protected by X-Admin-Secret header)
  - app.api.auth          (normal user registration/login)

Admin accounts live in their own `admins` table and are never mixed with `users`.
All routes below (except /login) require a valid admin bearer token.

POST /api/admin/panel/login                          — admin login
GET  /api/admin/panel/dashboard                       — summary cards
GET  /api/admin/panel/pending-users                   — list pending registrations
POST /api/admin/panel/pending-users/{id}/approve      — approve → moves into `users`
POST /api/admin/panel/pending-users/{id}/reject       — reject
DELETE /api/admin/panel/pending-users/{id}            — delete a pending registration
GET  /api/admin/panel/users                           — list approved users
POST /api/admin/panel/users/{id}/enable               — enable an account
POST /api/admin/panel/users/{id}/disable              — disable an account
DELETE /api/admin/panel/users/{id}                    — delete a user
POST /api/admin/panel/users/{id}/reset-password       — admin sets a temporary password
"""
import logging
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import current_admin, require_superadmin, client_ip
from app.core.database import get_pool
from app.models.auth import AdminLoginRequest, TotpVerifyRequest
from app.services import rate_limit
from app.services.audit import log_admin_action
from app.services.security import (
    create_token, hash_password, verify_password,
    generate_totp_secret, totp_provisioning_uri, verify_totp,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/login", summary="Admin login")
async def admin_login(payload: AdminLoginRequest, request: Request, pool=Depends(get_pool)):
    ip = client_ip(request)

    locked = rate_limit.check_locked(payload.username, ip)
    if locked:
        raise HTTPException(
            429,
            f"Too many failed attempts. Try again in {locked} seconds.",
        )

    async with pool.acquire() as conn:
        admin = await conn.fetchrow(
            "select id, username, password_hash, role, totp_secret, totp_enabled "
            "from admins where username = $1",
            payload.username,
        )
    if not admin or not verify_password(payload.password, admin["password_hash"]):
        rate_limit.record_failure(payload.username, ip)
        await log_admin_action(
            pool, action="admin.login_failed", actor_label=payload.username,
            admin_id=admin["id"] if admin else None, ip_address=ip,
            metadata={"reason": "bad_credentials"},
        )
        raise HTTPException(401, "Invalid admin credentials")

    # 2FA: required once enrolled; enforced for enrolled admins of any role.
    if admin["totp_enabled"]:
        if not payload.totp_code:
            raise HTTPException(401, "A 2FA code is required for this account", headers={"X-2FA-Required": "1"})
        if not verify_totp(admin["totp_secret"], payload.totp_code):
            rate_limit.record_failure(payload.username, ip)
            await log_admin_action(
                pool, action="admin.login_failed", actor_label=admin["username"],
                admin_id=admin["id"], ip_address=ip, metadata={"reason": "bad_totp"},
            )
            raise HTTPException(401, "Invalid 2FA code")

    rate_limit.clear(payload.username, ip)
    token = create_token(str(admin["id"]), token_type="admin", expires_minutes=60 * 12)
    await log_admin_action(
        pool, action="admin.login", actor_label=admin["username"],
        admin_id=admin["id"], ip_address=ip,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "admin": {
            "id": admin["id"],
            "username": admin["username"],
            "role": admin["role"],
            "totp_enabled": admin["totp_enabled"],
        },
        # Superadmins should complete 2FA enrollment; surfaced so the UI can nudge.
        "totp_setup_required": admin["role"] == "superadmin" and not admin["totp_enabled"],
    }


# ── 2FA (TOTP) enrollment ───────────────────────────────────────────────
@router.post("/2fa/enroll", summary="Begin TOTP 2FA enrollment")
async def totp_enroll(request: Request, admin=Depends(current_admin), pool=Depends(get_pool)):
    """Generate (or regenerate) a TOTP secret. Not active until verified via /2fa/verify."""
    secret = generate_totp_secret()
    async with pool.acquire() as conn:
        await conn.execute(
            "update admins set totp_secret = $1, totp_enabled = false where id = $2",
            secret, admin["id"],
        )
    return {
        "secret": secret,
        "otpauth_uri": totp_provisioning_uri(secret, admin["username"]),
        "message": "Scan the QR/secret in your authenticator app, then confirm a code at /2fa/verify.",
    }


@router.post("/2fa/verify", summary="Confirm and activate TOTP 2FA")
async def totp_verify(payload: TotpVerifyRequest, request: Request,
                      admin=Depends(current_admin), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("select totp_secret from admins where id = $1", admin["id"])
        if not row or not row["totp_secret"]:
            raise HTTPException(400, "Start enrollment first (/2fa/enroll)")
        if not verify_totp(row["totp_secret"], payload.code):
            raise HTTPException(401, "Invalid 2FA code")
        await conn.execute("update admins set totp_enabled = true where id = $1", admin["id"])
    await log_admin_action(
        pool, action="admin.2fa_enabled", actor_label=admin["username"],
        admin_id=admin["id"], ip_address=client_ip(request),
    )
    return {"message": "Two-factor authentication is now enabled."}


@router.post("/2fa/disable", summary="Disable TOTP 2FA")
async def totp_disable(payload: TotpVerifyRequest, request: Request,
                       admin=Depends(current_admin), pool=Depends(get_pool)):
    """Disabling requires a current code to prove possession of the device."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "select role, totp_secret, totp_enabled from admins where id = $1", admin["id"],
        )
        if not row or not row["totp_enabled"]:
            raise HTTPException(400, "2FA is not enabled")
        if not verify_totp(row["totp_secret"], payload.code):
            raise HTTPException(401, "Invalid 2FA code")
        await conn.execute(
            "update admins set totp_enabled = false, totp_secret = null where id = $1", admin["id"],
        )
    await log_admin_action(
        pool, action="admin.2fa_disabled", actor_label=admin["username"],
        admin_id=admin["id"], ip_address=client_ip(request),
    )
    return {"message": "Two-factor authentication disabled."}


# ── Current admin identity ──────────────────────────────────────────────
@router.get("/me", summary="Current admin identity")
async def whoami(admin=Depends(current_admin)):
    return {
        "id": admin["id"], "username": admin["username"],
        "role": admin["role"], "totp_enabled": admin["totp_enabled"],
    }


# ── Audit log ───────────────────────────────────────────────────────────
@router.get("/audit-log", summary="Admin audit trail", dependencies=[Depends(current_admin)])
async def audit_log(
    action: str | None = None,
    actor: str | None = None,
    limit: int = 100,
    offset: int = 0,
    pool=Depends(get_pool),
):
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    where, args = [], []
    if action:
        args.append(action); where.append(f"action = ${len(args)}")
    if actor:
        args.append(f"%{actor.lower()}%"); where.append(f"lower(actor_label) like ${len(args)}")
    clause = ("where " + " and ".join(where)) if where else ""
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"select count(*) from admin_audit_log {clause}", *args)
        rows = await conn.fetch(
            f"""select id, admin_id, actor_label, action, target_type, target_id,
                       metadata, ip_address, created_at
                from admin_audit_log {clause}
                order by created_at desc
                limit {limit} offset {offset}""",
            *args,
        )
    return {"total": total, "count": len(rows), "data": [dict(r) for r in rows]}


@router.get("/dashboard", summary="Dashboard summary cards", dependencies=[Depends(current_admin)])
async def dashboard(pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        stats = await conn.fetchrow(
            """
            select
                (select count(*) from users)                                  as total_users,
                (select count(*) from pending_users where status = 'pending')  as pending_users,
                (select count(*) from users where active = true)               as active_users,
                (select count(*) from users where active = false)              as disabled_users
            """
        )
    return dict(stats)


# ── Pending users ──────────────────────────────────────────────────────
@router.get("/pending-users", summary="List pending registrations", dependencies=[Depends(current_admin)])
async def list_pending_users(pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "select id, full_name, email, phone, status, created_at "
            "from pending_users order by created_at desc"
        )
    return {"count": len(rows), "data": [dict(r) for r in rows]}


@router.post("/pending-users/{pending_id}/approve", summary="Approve a pending registration")
async def approve_pending_user(pending_id: int, request: Request,
                               admin=Depends(current_admin), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        async with conn.transaction():
            pending = await conn.fetchrow(
                "select id, full_name, email, phone, password_hash from pending_users "
                "where id = $1 for update",
                pending_id,
            )
            if not pending:
                raise HTTPException(404, "Pending registration not found")

            dup = await conn.fetchval(
                "select id from users where lower(email) = lower($1) or phone = $2",
                pending["email"], pending["phone"],
            )
            if dup:
                raise HTTPException(409, "A user with this email or phone already exists")

            user = await conn.fetchrow(
                """
                insert into users (full_name, email, phone, password_hash, active, approved_at, created_at)
                values ($1, $2, $3, $4, true, now(), now())
                returning id, full_name, email, phone, active, created_at, approved_at
                """,
                pending["full_name"], pending["email"], pending["phone"], pending["password_hash"],
            )
            await conn.execute("delete from pending_users where id = $1", pending_id)
    logger.info("Admin %s approved user %s", admin["username"], user["email"])
    await log_admin_action(
        pool, action="user.approve", actor_label=admin["username"], admin_id=admin["id"],
        target_type="user", target_id=user["id"], ip_address=client_ip(request),
        metadata={"email": user["email"], "pending_id": pending_id},
    )
    return {"message": "User approved", "user": dict(user)}


@router.post("/pending-users/{pending_id}/reject", summary="Reject a pending registration")
async def reject_pending_user(pending_id: int, request: Request,
                              admin=Depends(current_admin), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        result = await conn.execute(
            "update pending_users set status = 'rejected' where id = $1", pending_id,
        )
    if result == "UPDATE 0":
        raise HTTPException(404, "Pending registration not found")
    await log_admin_action(
        pool, action="user.reject", actor_label=admin["username"], admin_id=admin["id"],
        target_type="pending_user", target_id=pending_id, ip_address=client_ip(request),
    )
    return {"message": "Registration rejected"}


@router.delete("/pending-users/{pending_id}", summary="Delete a pending registration")
async def delete_pending_user(pending_id: int, request: Request,
                              admin=Depends(current_admin), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        result = await conn.execute("delete from pending_users where id = $1", pending_id)
    if result == "DELETE 0":
        raise HTTPException(404, "Pending registration not found")
    await log_admin_action(
        pool, action="user.delete_pending", actor_label=admin["username"], admin_id=admin["id"],
        target_type="pending_user", target_id=pending_id, ip_address=client_ip(request),
    )
    return {"message": "Pending registration deleted"}


# ── Existing users ──────────────────────────────────────────────────────
@router.get("/users", summary="List approved users", dependencies=[Depends(current_admin)])
async def list_users(pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "select id, full_name, email, phone, created_at, last_login_at, active "
            "from users order by created_at desc"
        )
    return {"count": len(rows), "data": [dict(r) for r in rows]}


@router.post("/users/{user_id}/enable", summary="Enable a user account")
async def enable_user(user_id: uuid.UUID, request: Request,
                      admin=Depends(current_admin), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        result = await conn.execute("update users set active = true where id = $1", user_id)
    if result == "UPDATE 0":
        raise HTTPException(404, "User not found")
    await log_admin_action(
        pool, action="user.enable", actor_label=admin["username"], admin_id=admin["id"],
        target_type="user", target_id=user_id, ip_address=client_ip(request),
    )
    return {"message": "User enabled"}


@router.post("/users/{user_id}/disable", summary="Disable a user account")
async def disable_user(user_id: uuid.UUID, request: Request,
                       admin=Depends(current_admin), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        result = await conn.execute("update users set active = false where id = $1", user_id)
    if result == "UPDATE 0":
        raise HTTPException(404, "User not found")
    await log_admin_action(
        pool, action="user.disable", actor_label=admin["username"], admin_id=admin["id"],
        target_type="user", target_id=user_id, ip_address=client_ip(request),
    )
    return {"message": "User disabled"}


@router.delete("/users/{user_id}", summary="Delete a user account (superadmin only)")
async def delete_user(user_id: uuid.UUID, request: Request,
                      admin=Depends(require_superadmin), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        result = await conn.execute("delete from users where id = $1", user_id)
    if result == "DELETE 0":
        raise HTTPException(404, "User not found")
    await log_admin_action(
        pool, action="user.delete", actor_label=admin["username"], admin_id=admin["id"],
        target_type="user", target_id=user_id, ip_address=client_ip(request),
    )
    return {"message": "User deleted"}


@router.post("/users/{user_id}/reset-password",
             summary="Admin resets a user's password to a temporary one (superadmin only)")
async def reset_password(user_id: uuid.UUID, request: Request,
                         admin=Depends(require_superadmin), pool=Depends(get_pool)):
    temp_password = secrets.token_urlsafe(9)
    async with pool.acquire() as conn:
        result = await conn.execute(
            "update users set password_hash = $1 where id = $2", hash_password(temp_password), user_id,
        )
    if result == "UPDATE 0":
        raise HTTPException(404, "User not found")
    await log_admin_action(
        pool, action="user.reset_password", actor_label=admin["username"], admin_id=admin["id"],
        target_type="user", target_id=user_id, ip_address=client_ip(request),
        metadata={"note": "temporary password issued"},  # never log the actual password
    )
    return {
        "message": "Password reset. Share this temporary password with the user securely; "
                    "they should change it after logging in.",
        "temporary_password": temp_password,
    }
