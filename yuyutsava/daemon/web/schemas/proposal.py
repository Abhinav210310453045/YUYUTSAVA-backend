"""Pydantic schemas for proposal + ask endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ProposalDecisionStr = Literal[
    "approve", "approve_remember", "modify", "skip", "skip_remember",
]


class ProposalRespondIn(BaseModel):
    decision: ProposalDecisionStr = Field(..., description="User's decision on the proposal")
    edited_instruction: str | None = Field(
        None,
        description="New instruction when decision='modify'; ignored otherwise",
    )


class AskRespondIn(BaseModel):
    response: str = Field(..., description="User's free-text reply to the ask prompt")


class OkOut(BaseModel):
    ok: bool = True
    note: str | None = None
