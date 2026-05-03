"""Public auth routes — login, logout, first-run setup. Mounted on its own
unauthenticated router so the require_auth dependency doesn't loop back."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import runtime_settings
from .auth import (
    SESSION_COOKIE,
    clear_session_cookie,
    hash_password,
    set_session_cookie,
    verify_password,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _has_password_set() -> bool:
    return bool(runtime_settings.get("auth_password_hash", ""))


@router.get("/login")
async def login_page(request: Request, next: str = "/", error: str | None = None):
    if not _has_password_set():
        # First-run; force them through setup instead.
        return RedirectResponse("/setup", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next, "error": error},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
) -> RedirectResponse:
    expected_user = runtime_settings.get("auth_username", "")
    hashed = runtime_settings.get("auth_password_hash", "")
    if username.strip() != expected_user or not verify_password(password, hashed):
        return RedirectResponse(f"/login?error=invalid&next={next}", status_code=303)
    # Avoid open-redirect: only honor `next` if it's a path on this app.
    if not next.startswith("/") or next.startswith("//"):
        next = "/"
    response = RedirectResponse(next, status_code=303)
    set_session_cookie(response, username.strip())
    return response


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    return response


@router.get("/setup")
async def setup_page(request: Request, error: str | None = None):
    if _has_password_set():
        # Setup is a one-time flow — once a password exists, this page is gone.
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "setup.html",
        {"error": error, "default_username": runtime_settings.get("auth_username", "admin")},
    )


@router.post("/setup")
async def setup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
) -> RedirectResponse:
    if _has_password_set():
        return RedirectResponse("/login", status_code=303)
    username = username.strip()
    if not username or len(password) < 6 or password != password_confirm:
        msg = "weak" if len(password) < 6 else ("mismatch" if password != password_confirm else "missing")
        return RedirectResponse(f"/setup?error={msg}", status_code=303)
    runtime_settings.set("auth_username", username)
    runtime_settings.set("auth_password_hash", hash_password(password))
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, username)
    return response
