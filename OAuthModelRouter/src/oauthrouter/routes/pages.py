"""Static HTML page routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _static_html_response(filename: str) -> HTMLResponse:
    """Serve a static HTML file from src/oauthrouter/static."""
    html_path = STATIC_DIR / filename
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/portal", response_class=HTMLResponse)
async def portal_page() -> HTMLResponse:
    """Serve the token management web portal."""
    return _static_html_response("portal.html")


@router.get("/help", response_class=HTMLResponse)
async def help_page() -> HTMLResponse:
    """Serve the built-in dashboard and endpoint walkthrough."""
    return _static_html_response("help.html")
