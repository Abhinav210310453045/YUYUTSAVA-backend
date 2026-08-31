"""Persistence contract for consent grants.

A thin protocol so the registry doesn't depend on the concrete storage layer.
The daemon's events ``Store`` implements it (state.db ``consent_grants`` table);
a CLI process without a store simply runs session-only (grants in memory).

SESSION-scope grants never touch the store — they live in the registry's memory
and disappear with the process. Only PROJECT / PERSISTENT grants are persisted.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from yuyutsava.consent.models import Grant


@runtime_checkable
class ConsentStore(Protocol):
    async def put_consent_grant(self, grant: Grant) -> None: ...

    async def delete_consent_grant(self, grant_id: str) -> None: ...

    def list_consent_grants(self) -> list[Grant]: ...
