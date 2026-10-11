"""``/models`` — the app's view of ``models.toml``: read it, set up providers, change it.

GET    /models                          — status, and the revision to change it from
GET    /models/catalog                  — how each provider type is set up
GET    /models/detected                 — what this machine already has
POST   /models/check                    — log in to a provider before saving it (free)
GET    /models/providers/{name}/models  — what a saved provider lists now (free)
POST   /models/setup                    — providers, aliases, the default and tasks, at once
PUT    /models/default                  — the default model
PUT    /models/tasks                    — tasks' model, or back to the default
POST   /models/aliases?revision=        — name a model
DELETE /models/aliases/{name}?revision= — remove a name
DELETE /models/providers/{name}?revision=
GET    /models/sessions/{id}            — one conversation's own models
PUT    /models/sessions/{id}            — change them (from its next turn)
POST   /models/test                     — one short message: billable, only when asked

Changes broadcast a ``models`` stream item through the daemon's watcher, so every
window sees them — and so do changes made in the CLI or by hand.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, Request

from yuyutsava.daemon.web.deps import get_session_store
from yuyutsava.daemon.web.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from yuyutsava.daemon.web.schemas.models import (
    CheckProviderIn,
    CheckProviderOut,
    DefaultIn,
    DiscoveryOut,
    FindingOut,
    ModelsStatusOut,
    NewAliasIn,
    ProviderFormOut,
    RemoveLeftoversIn,
    SecurityIn,
    SessionModelsIn,
    SessionModelsOut,
    SetupIn,
    TasksIn,
    TestMessageIn,
    TestMessageOut,
)
from yuyutsava.daemon.web.services.models_service import ModelsService
from yuyutsava.llm.config.edits import (
    ConfigEditError,
    Edit,
    add_model,
    follow_default,
    remove_model,
    remove_provider,
    require_presence,
    use,
)
from yuyutsava.llm.config.loader import ModelsConfigError
from yuyutsava.llm.config.store import StaleWrite
from yuyutsava.llm.factory import chat_model
from yuyutsava.llm.failures import describe_model_failure
from yuyutsava.llm.selection import reference_for
from yuyutsava.platform.user_presence import PresenceNotConfirmed
from yuyutsava.storage.models import ModelChoice
from yuyutsava.storage.sessions.store import SessionNotFound

router = APIRouter(prefix="/models", tags=["models"])


def get_models_service(request: Request) -> ModelsService:
    service = getattr(request.app.state, "models_service", None)
    if service is None:
        raise ServiceUnavailableError("the models service is not running")
    return service


async def _write(service: ModelsService, edit: Edit, revision: str) -> ModelsStatusOut:
    try:
        status = await asyncio.to_thread(service.write, edit, revision)
    except StaleWrite as exc:
        raise ConflictError(f"{exc}") from exc
    except (ConfigEditError, ModelsConfigError) as exc:
        raise ValidationError(str(exc)) from exc
    except PresenceNotConfirmed as exc:
        raise ForbiddenError(f"not changed: {exc}") from exc
    return ModelsStatusOut(**status)


@router.get("", response_model=ModelsStatusOut, summary="models.toml as it is now")
async def models_status(service: ModelsService = Depends(get_models_service)) -> ModelsStatusOut:
    return ModelsStatusOut(**await asyncio.to_thread(service.status))


@router.get("/catalog", response_model=list[ProviderFormOut], summary="How each provider is set up")
async def models_catalog(service: ModelsService = Depends(get_models_service)) -> list[Any]:
    return service.catalog()


@router.get("/detected", response_model=list[FindingOut], summary="What this machine already has")
async def models_detected(service: ModelsService = Depends(get_models_service)) -> list[Any]:
    return await asyncio.to_thread(service.detected)


@router.post("/check", response_model=CheckProviderOut, summary="Log in to a provider (free)")
async def models_check(
    body: CheckProviderIn, service: ModelsService = Depends(get_models_service)
) -> CheckProviderOut:
    try:
        result = await asyncio.to_thread(service.check, body.name, body.connection, body.secrets)
    except ValueError as exc:  # a secret that cannot be stored under that name
        raise ValidationError(str(exc)) from exc
    except PresenceNotConfirmed as exc:
        raise ForbiddenError(f"the key was not stored: {exc}") from exc
    return CheckProviderOut(**result)


@router.get(
    "/providers/{name}/models",
    response_model=DiscoveryOut,
    summary="What a saved provider lists now (free)",
)
async def provider_models(
    name: str, service: ModelsService = Depends(get_models_service)
) -> DiscoveryOut:
    try:
        return DiscoveryOut(**await asyncio.to_thread(service.provider_models, name))
    except KeyError as exc:
        raise NotFoundError(f"no provider named {name!r}") from exc


@router.post(
    "/leftovers/remove",
    response_model=ModelsStatusOut,
    summary="Remove model variables models.toml replaced from .env",
)
async def models_remove_leftovers(
    body: RemoveLeftoversIn, service: ModelsService = Depends(get_models_service)
) -> ModelsStatusOut:
    return ModelsStatusOut(**await asyncio.to_thread(service.remove_leftovers, body.names))


@router.put(
    "/security",
    response_model=ModelsStatusOut,
    summary="Whether changing a key needs the owner confirmed present",
)
async def models_security(
    body: SecurityIn, service: ModelsService = Depends(get_models_service)
) -> ModelsStatusOut:
    return await _write(service, require_presence(body.require_user_presence), body.revision)


@router.post("/setup", response_model=ModelsStatusOut, summary="Write a whole setup at once")
async def models_setup(
    body: SetupIn, service: ModelsService = Depends(get_models_service)
) -> ModelsStatusOut:
    try:
        edit = service.setup_edit(body.model_dump(exclude_none=True))
    except ConfigEditError as exc:
        raise ValidationError(str(exc)) from exc
    return await _write(service, edit, body.revision)


@router.put("/default", response_model=ModelsStatusOut, summary="Set the default model")
async def models_default(
    body: DefaultIn, service: ModelsService = Depends(get_models_service)
) -> ModelsStatusOut:
    return await _write(service, use(body.reference), body.revision)


@router.put("/tasks", response_model=ModelsStatusOut, summary="Give tasks a model, or the default")
async def models_tasks(
    body: TasksIn, service: ModelsService = Depends(get_models_service)
) -> ModelsStatusOut:
    try:
        edit = (
            use(body.reference, tasks=body.tasks) if body.reference else follow_default(body.tasks)
        )
    except ConfigEditError as exc:
        raise ValidationError(str(exc)) from exc
    return await _write(service, edit, body.revision)


@router.post("/aliases", response_model=ModelsStatusOut, summary="Name a model")
async def models_add_alias(
    body: NewAliasIn, revision: str, service: ModelsService = Depends(get_models_service)
) -> ModelsStatusOut:
    settings = body.model_dump(exclude_none=True, exclude={"name", "provider", "id"})
    try:
        edit = add_model(body.name, provider=body.provider, model_id=body.id, **settings)
    except ConfigEditError as exc:
        raise ValidationError(str(exc)) from exc
    return await _write(service, edit, revision)


@router.delete("/aliases/{name}", response_model=ModelsStatusOut, summary="Remove a model's name")
async def models_remove_alias(
    name: str, revision: str, service: ModelsService = Depends(get_models_service)
) -> ModelsStatusOut:
    return await _write(service, remove_model(name), revision)


@router.delete("/providers/{name}", response_model=ModelsStatusOut, summary="Remove a provider")
async def models_remove_provider(
    name: str, revision: str, service: ModelsService = Depends(get_models_service)
) -> ModelsStatusOut:
    return await _write(service, remove_provider(name), revision)


async def _session_models(request: Request, session_id: str) -> SessionModelsOut:
    sessions = get_session_store(request)
    try:
        choice = (await sessions.get(session_id)).models
    except SessionNotFound as exc:
        raise NotFoundError(f"no conversation {session_id!r}") from exc
    task_models = getattr(request.app.state, "task_models", None)
    config = task_models.config if task_models is not None else None
    # As models.toml names it — an alias or <provider>/<model id> — like the
    # choices the app offers, so its chooser can mark the one in use.
    effective = choice.model or (reference_for(config, "chat") if config is not None else "")
    return SessionModelsOut(
        session_id=session_id,
        model=choice.model,
        subagents=dict(choice.subagents),
        effective=effective,
    )


@router.get(
    "/sessions/{session_id}", response_model=SessionModelsOut, summary="A conversation's models"
)
async def session_models(session_id: str, request: Request) -> SessionModelsOut:
    return await _session_models(request, session_id)


@router.put(
    "/sessions/{session_id}",
    response_model=SessionModelsOut,
    summary="Change a conversation's models, from its next turn",
)
async def set_session_models(
    session_id: str, body: SessionModelsIn, request: Request
) -> SessionModelsOut:
    task_models = getattr(request.app.state, "task_models", None)
    for reference in (body.model, *body.subagents.values()):
        if reference and task_models is not None:
            try:
                task_models.resolve(reference)
            except KeyError as exc:
                raise ValidationError(f"models.toml has no model {reference!r}") from exc
    sessions = get_session_store(request)
    choice = ModelChoice(model=body.model, subagents=tuple(sorted(body.subagents.items())))
    try:
        await sessions.set_model_choice(session_id, choice)
    except SessionNotFound as exc:
        raise NotFoundError(f"no conversation {session_id!r}") from exc
    return await _session_models(request, session_id)


@router.post("/test", response_model=TestMessageOut, summary="Send one short message (billable)")
async def models_test(body: TestMessageIn, request: Request) -> TestMessageOut:
    task_models = getattr(request.app.state, "task_models", None)
    if task_models is None:
        raise ServiceUnavailableError("no models are loaded")
    try:
        target = task_models.resolve(body.reference)
    except KeyError as exc:
        raise ValidationError(f"models.toml has no model {body.reference!r}") from exc

    started = time.monotonic()
    try:
        reply = await chat_model(target, temperature=0.0).ainvoke("Reply with just: OK")
    except Exception as exc:  # noqa: BLE001 — the point is to report how the call failed
        return TestMessageOut(
            ok=False,
            reference=body.reference,
            problem=describe_model_failure(exc) or f"{type(exc).__name__}: {exc}",
        )
    text = reply.content if isinstance(reply.content, str) else str(reply.content)
    return TestMessageOut(
        ok=True,
        reference=body.reference,
        reply=text.strip()[:200],
        seconds=round(time.monotonic() - started, 2),
    )
