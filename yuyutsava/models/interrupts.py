"""
Typed interrupt payload models passed to LangGraph's interrupt().

Every call to interrupt() in this system uses one of these three models.
engine._prompt_permission() dispatches on the `type` discriminator field.

  UserQuestionInterrupt         — agent asks the user a free-text question (tr_ask_user)
  TaskRunnerPermissionInterrupt — gateway asks user to approve a filesystem operation
  PermissionRequestInterrupt    — PermissionMiddleware asks user to approve a raw execute call
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class UserQuestionInterrupt(BaseModel):
    """Payload produced by tr_ask_user — a free-text question to the user."""

    type:     Literal["user_question"] = "user_question"
    question: str
    options:  list[str] = Field(default_factory=list)

    def to_interrupt_dict(self) -> dict:
        return self.model_dump()


class TaskRunnerPermissionInterrupt(BaseModel):
    """Payload produced by the TaskRunnerAgent gateway when a PROMPT rule fires."""

    type:              Literal["task_runner_permission"] = "task_runner_permission"
    operation:         str          # OperationType.value, e.g. "delete"
    paths:             list[str]
    zone:              str          # FilesystemZone.value, e.g. "workspace"
    reason:            str
    requesting_agent:  str
    parent_agent:      str | None = None
    task_id:           str
    task_description:  str
    risk_level:        str          # "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"

    def to_interrupt_dict(self) -> dict:
        return self.model_dump()


class PermissionRequestInterrupt(BaseModel):
    """Payload produced by PermissionMiddleware when a raw execute call triggers a check."""

    type:    Literal["permission_request"] = "permission_request"
    command: str
    reason:  str

    def to_interrupt_dict(self) -> dict:
        return self.model_dump()
