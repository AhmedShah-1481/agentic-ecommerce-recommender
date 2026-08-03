"""Password hashing, signed session cookies, and auth dependencies.

Deliberately minimal per the brief: bcrypt + a signed httponly cookie.
No passlib - it is unmaintained and breaks against bcrypt 5.x.
"""

from __future__ import annotations

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User

SESSION_COOKIE = "ll_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days
BCRYPT_MAX_BYTES = 72  # bcrypt truncates beyond this; hash first to stay safe

_serializer = URLSafeTimedSerializer(settings.secret_key, salt="learnloop-session")


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    raw = password.encode("utf-8")[:BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        raw = password.encode("utf-8")[:BCRYPT_MAX_BYTES]
        return bcrypt.checkpw(raw, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------
# Session cookie
# --------------------------------------------------------------------------
def issue_session(user: User) -> str:
    return _serializer.dumps({"uid": user.id})


def read_session(token: str) -> int | None:
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
        return int(data["uid"])
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError):
        return None


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=not settings.debug,
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE)


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------
def optional_user_id(request: Request) -> int | None:
    """User id straight from the signed cookie - no database touch.

    Event ingest only needs the id to stamp on the batch, and it is the busiest
    endpoint in the app. Depending on `optional_user` there would check out a
    connection and run a SELECT for every flush, which is exactly the request-path
    work the ingest design exists to avoid. The cookie is signed, so the id is
    as trustworthy here as it is after a lookup.
    """
    token = request.cookies.get(SESSION_COOKIE)
    return read_session(token) if token else None


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """Current user or None - used by public pages that adapt when signed in."""
    uid = optional_user_id(request)
    if uid is None:
        return None
    return db.get(User, uid)


def current_user(user: User | None = Depends(optional_user)) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="login required",
            headers={"Location": "/login"},
        )
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user
