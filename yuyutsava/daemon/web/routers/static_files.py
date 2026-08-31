"""Static index + asset files for the bundled web UI."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

from yuyutsava.daemon.web.exceptions import NotFoundError

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

router = APIRouter(tags=["ui"])


@router.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@router.get("/static/{name}", include_in_schema=False)
async def static_file(name: str) -> FileResponse:
    path = (_STATIC_DIR / name).resolve()
    if not str(path).startswith(str(_STATIC_DIR.resolve())) or not path.is_file():
        raise NotFoundError(f"static asset {name!r} not found")
    return FileResponse(path)
