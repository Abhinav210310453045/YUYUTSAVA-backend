from __future__ import annotations

import asyncio
import shutil
from typing import Any

from fastapi import APIRouter, Depends

from yuyutsava.daemon.web.deps import get_skill_registry
from yuyutsava.daemon.web.exceptions import NotFoundError, ServiceUnavailableError

router = APIRouter(tags=["skills"])


@router.get("/skills", summary="List discovered skills")
async def list_skills(reg=Depends(get_skill_registry)) -> list[dict[str, Any]]:
    if reg is None:
        return []
    # scan() walks dirs (os.scandir) and may mkdir → off-loop (blockbuster-safe).
    skills = await asyncio.to_thread(reg.scan)
    return [
        {"name": s.name, "description": s.description, "scope": s.scope, "agent": s.agent}
        for s in skills
    ]


@router.delete("/skills/{name}", summary="Delete a personal-scope skill")
async def delete_skill(name: str, reg=Depends(get_skill_registry)) -> dict[str, str]:
    if reg is None:
        raise ServiceUnavailableError("skill registry not available")
    personal_dir = reg._home_dir / name  # type: ignore[attr-defined]
    if not personal_dir.exists():
        raise NotFoundError(f"personal skill {name!r} not found")
    # rmtree does scandir + unlink + rmdir → off-loop (blockbuster-safe).
    await asyncio.to_thread(shutil.rmtree, personal_dir)
    reg._cache = None  # type: ignore[attr-defined]
    return {"deleted": name}
