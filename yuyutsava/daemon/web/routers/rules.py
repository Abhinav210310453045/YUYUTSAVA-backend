"""Consent-rule CRUD."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends

from yuyutsava.daemon.web.deps import get_hub
from yuyutsava.daemon.web.exceptions import ServiceUnavailableError

router = APIRouter(tags=["rules"])


@router.get("/rules", summary="List active consent rules")
async def list_rules(hub=Depends(get_hub)) -> list[dict[str, Any]]:
    return [asdict(r) for r in await hub.store.list_consent_rules()]


@router.delete("/rules/{rule_id}", summary="Revoke a consent rule")
async def delete_rule(rule_id: str, hub=Depends(get_hub)) -> dict[str, int]:
    conn = hub.store._conn  # type: ignore[attr-defined]
    if conn is None:
        raise ServiceUnavailableError("store not started")
    cur = conn.execute("DELETE FROM consent_rules WHERE rule_id=?", (rule_id,))
    conn.commit()
    return {"deleted": cur.rowcount}
