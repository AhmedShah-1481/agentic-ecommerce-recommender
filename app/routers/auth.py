"""Registration, login, logout.

Two front doors onto one set of rules. The form handlers render Jinja templates
and redirect, which is what a browser navigation wants; the `/api/auth/*`
handlers take and return JSON, which is what the React storefront wants. Both
sit on the same `_registration_error` / `_create_user` / `_authenticate`
helpers, deliberately: if the two paths each carried their own validation, they
would eventually disagree about what a valid password is, and the looser one
would be the one that mattered.

Both issue the same signed session cookie. There is no JWT and no second
mechanism - a JSON login is the same login.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User
from app.schemas import LoginIn, PublicUser, RegisterIn
from app.security import (
    clear_session_cookie,
    hash_password,
    issue_session,
    optional_user,
    set_session_cookie,
    verify_password,
)
from app.templating import templates

router = APIRouter(tags=["auth"])
api_router = APIRouter(prefix="/api/auth", tags=["auth"])

MIN_PASSWORD_LENGTH = 8

# Shown for both "no such account" and "wrong password". Distinguishing them
# turns the login form into an oracle for which email addresses are registered.
BAD_CREDENTIALS = "Incorrect email or password."


def _form_login_target(user: User, next: str = "") -> str:
    """Where a *form* sign-in lands.

    Step 3 turned the customer pages into JSON, so `/dashboard` - the old default
    here - stopped existing, and signing in through this form landed on a 404.
    There is no server-rendered shopper page to send anyone to any more: an admin
    wants the panel, and everybody else wants the storefront, which is a different
    origin entirely.

    An explicit `next` still wins, so `/login?next=/admin/sync` works, but only
    when it is a same-site path. Anything else is discarded rather than followed -
    an open redirect on a login page is a ready-made phishing vector.
    """
    if next.startswith("/") and not next.startswith("//"):
        return next
    return "/admin" if user.is_admin else settings.storefront_url


def _normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def _registration_error(db: Session, email: str, password: str) -> str | None:
    """The one place registration rules live. `email` must already be normalised."""
    if "@" not in email or "." not in email.split("@")[-1]:
        return "Enter a valid email address."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if db.scalar(select(User).where(func.lower(User.email) == email)) is not None:
        return "That email is already registered."
    return None


def _create_user(db: Session, email: str, password: str, name: str) -> User:
    user = User(
        email=email,
        password_hash=hash_password(password),
        name=(name or "").strip()[:120] or email.split("@")[0],
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(func.lower(User.email) == _normalise_email(email)))
    if user is None or not verify_password(password, user.password_hash):
        return None
    return user


def _public_user(user: User) -> PublicUser:
    return PublicUser(
        id=user.id,
        email=user.email,
        name=user.name,
        role=user.role,
        is_admin=user.is_admin,
    )


# --------------------------------------------------------------------------
# Server-rendered forms
# --------------------------------------------------------------------------
@router.get("/register", response_class=HTMLResponse)
async def register_form(request: Request):
    return templates.TemplateResponse(request, "auth/register.html", {"title": "Create account"})


@router.post("/register", response_class=HTMLResponse)
async def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    name: str = Form(default=""),
    db: Session = Depends(get_db),
):
    email = _normalise_email(email)
    error = _registration_error(db, email, password)

    if error:
        return templates.TemplateResponse(
            request,
            "auth/register.html",
            {"title": "Create account", "error": error, "email": email, "name": name},
            status_code=400,
        )

    user = _create_user(db, email, password, name)
    response = RedirectResponse(_form_login_target(user), status_code=303)
    set_session_cookie(response, issue_session(user))
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = ""):
    return templates.TemplateResponse(
        request, "auth/login.html", {"title": "Sign in", "next": next}
    )


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = _authenticate(db, email, password)

    if user is None:
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {"title": "Sign in", "error": BAD_CREDENTIALS, "email": email, "next": next},
            status_code=401,
        )

    response = RedirectResponse(_form_login_target(user, next), status_code=303)
    set_session_cookie(response, issue_session(user))
    return response


@router.post("/logout")
async def logout():
    # Back to the form, not to `/`: the API root is a JSON signpost now, and
    # signing out of the admin panel should not drop a JSON blob in the browser.
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    return response


# --------------------------------------------------------------------------
# JSON auth, for the React storefront
# --------------------------------------------------------------------------
@api_router.get("/me")
async def api_me(user: User | None = Depends(optional_user)):
    """Who the cookie says you are, or `null`.

    Not `api_user`, on purpose. "Am I signed in?" is a question the header asks
    on every cold page load, and "nobody" is a valid answer to it rather than an
    error - answering 401 would put a red line in the console on every anonymous
    visit and push the client into treating a normal state as a failure.
    """
    return {"user": _public_user(user) if user else None}


@api_router.post("/login")
async def api_login(payload: LoginIn, response: Response, db: Session = Depends(get_db)):
    user = _authenticate(db, payload.email, payload.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=BAD_CREDENTIALS)

    # No `next` parameter here. Where to go after signing in is the client's
    # routing decision, and accepting a redirect target over JSON would recreate
    # the open-redirect risk the form handler guards against for no benefit.
    set_session_cookie(response, issue_session(user))
    return {"user": _public_user(user)}


@api_router.post("/register", status_code=status.HTTP_201_CREATED)
async def api_register(payload: RegisterIn, response: Response, db: Session = Depends(get_db)):
    email = _normalise_email(payload.email)
    error = _registration_error(db, email, payload.password)
    if error:
        # 400 rather than 422: these are the app's own rules, not a malformed
        # body, and the client shows `detail` to the user as-is.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error)

    user = _create_user(db, email, payload.password, payload.name)
    set_session_cookie(response, issue_session(user))
    return {"user": _public_user(user)}


@api_router.post("/logout")
async def api_logout():
    """Idempotent: logging out without a session is a success, not a 401. A
    client whose cookie already expired is trying to reach exactly this state."""
    response = JSONResponse({"user": None})
    clear_session_cookie(response)
    return response
