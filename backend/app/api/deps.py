import uuid

from fastapi import Depends, Header, HTTPException
from app.core.database import get_pool
from app.services.security import decode_token


async def current_user(authorization: str | None = Header(None), pool=Depends(get_pool)):
    """Resolves the logged-in user from a bearer access token. Disabled accounts are rejected."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Authentication required")
    payload = decode_token(authorization.split(" ", 1)[1], expected_type="access")
    if not payload:
        raise HTTPException(401, "Invalid or expired session")
    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except (ValueError, KeyError):
        raise HTTPException(401, "Invalid session")
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "select id, full_name, email, phone, active, created_at, approved_at, last_login_at "
            "from users where id = $1",
            user_id,
        )
    if not user:
        raise HTTPException(401, "User not found")
    if not user["active"]:
        raise HTTPException(403, "Your account has been disabled. Contact an administrator.")
    return dict(user)


async def current_admin(authorization: str | None = Header(None), pool=Depends(get_pool)):
    """Resolves the logged-in admin from a bearer admin token. Kept fully separate from user auth.
    Admin ids are plain bigserial integers (the `admins` table is unrelated to `users`)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Admin authentication required")
    payload = decode_token(authorization.split(" ", 1)[1], expected_type="admin")
    if not payload:
        raise HTTPException(401, "Invalid or expired admin session")
    try:
        admin_id = int(payload["sub"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(401, "Invalid admin session")
    async with pool.acquire() as conn:
        admin = await conn.fetchrow("select id, username, created_at from admins where id = $1", admin_id)
    if not admin:
        raise HTTPException(401, "Admin not found")
    return dict(admin)
