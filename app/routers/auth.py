"""Registration, login, logout."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import (
    clear_session_cookie,
    hash_password,
    issue_session,
    set_session_cookie,
    verify_password,
)
from app.templating import templates

router = APIRouter(tags=["auth"])

MIN_PASSWORD_LENGTH = 8


def _normalise_email(email: str) -> str:
    return (email or "").strip().lower()


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
    error = None

    if "@" not in email or "." not in email.split("@")[-1]:
        error = "Enter a valid email address."
    elif len(password) < MIN_PASSWORD_LENGTH:
        error = f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    elif db.scalar(select(User).where(func.lower(User.email) == email)) is not None:
        error = "That email is already registered."

    if error:
        return templates.TemplateResponse(
            request,
            "auth/register.html",
            {"title": "Create account", "error": error, "email": email, "name": name},
            status_code=400,
        )

    user = User(
        email=email,
        password_hash=hash_password(password),
        name=(name or "").strip()[:120] or email.split("@")[0],
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    response = RedirectResponse("/dashboard", status_code=303)
    set_session_cookie(response, issue_session(user))
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/dashboard"):
    return templates.TemplateResponse(
        request, "auth/login.html", {"title": "Sign in", "next": next}
    )


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/dashboard"),
    db: Session = Depends(get_db),
):
    user = db.scalar(select(User).where(func.lower(User.email) == _normalise_email(email)))

    if user is None or not verify_password(password, user.password_hash):
        # Same message either way: distinguishing them leaks which emails exist.
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {
                "title": "Sign in",
                "error": "Incorrect email or password.",
                "email": email,
                "next": next,
            },
            status_code=401,
        )

    # Only ever redirect within this site - an open redirect here would be a
    # ready-made phishing vector off the login page.
    target = next if next.startswith("/") and not next.startswith("//") else "/dashboard"
    response = RedirectResponse(target, status_code=303)
    set_session_cookie(response, issue_session(user))
    return response


@router.post("/logout")
async def logout():
    response = RedirectResponse("/", status_code=303)
    clear_session_cookie(response)
    return response
