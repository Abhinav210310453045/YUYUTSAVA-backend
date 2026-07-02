#!/usr/bin/env python
"""Register the env-configured LLM model + its live price in self-hosted Langfuse.

Langfuse computes per-generation cost by matching the generation's model name
against its own model-definitions table. A model it doesn't know (e.g. a freshly
switched OpenRouter model) shows tokens but **no cost**. This script closes that
gap using the *same* dynamic price source as the internal ledger
(:func:`yuyutsava.core.pricing.resolve_model_price`) — nothing is hardcoded.

It resolves the active provider/model from env (``LLM_PROVIDER`` + the provider's
``*_MODEL``), fetches that model's exact input/output price from the provider,
and creates a Langfuse model definition whose ``matchPattern`` matches the exact
model id. Re-runnable: any prior user-created definition with the same name is
removed first, so swapping the env model re-registers with fresh pricing.

Usage:
    uv run python scripts/langfuse_register_model.py

Reads LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY (and the LLM
provider vars) from the environment / a loaded ``.env``.
"""

from __future__ import annotations

import os
import re
import sys

import httpx

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

from yuyutsava.core.config import llm_settings_from_env
from yuyutsava.core.pricing import resolve_model_price

_PER_MILLION = 1_000_000


def _die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def main() -> None:
    if load_dotenv:
        load_dotenv()

    host = (os.getenv("LANGFUSE_HOST") or "").rstrip("/")
    pk = os.getenv("LANGFUSE_PUBLIC_KEY") or ""
    sk = os.getenv("LANGFUSE_SECRET_KEY") or ""
    if not (host and pk and sk):
        _die("LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY must be set")

    settings = llm_settings_from_env()
    model = str(getattr(settings, "model", "") or "")
    if not model:
        _die("could not determine the configured model from env")

    price = resolve_model_price(settings)
    if price is None:
        _die(
            f"no live price available for {model!r} from its provider "
            f"(only OpenRouter/Ollama are auto-priced; set model_prices.json for others)"
        )
    # Ledger prices are USD per 1M tokens; Langfuse wants USD per token.
    input_price = price[0] / _PER_MILLION
    output_price = price[1] / _PER_MILLION

    auth = (pk, sk)
    match_pattern = f"(?i)^{re.escape(model)}$"

    with httpx.Client(base_url=host, auth=auth, timeout=15.0) as client:
        # Remove any prior user-created definition for this exact model so the
        # script is idempotent (Langfuse has no upsert; POST always appends).
        removed = 0
        try:
            resp = client.get("/api/public/models", params={"limit": 100})
            resp.raise_for_status()
            for m in resp.json().get("data", []):
                if m.get("modelName") == model and not m.get("isLangfuseManaged", False):
                    mid = m.get("id")
                    if mid:
                        d = client.delete(f"/api/public/models/{mid}")
                        if d.status_code < 300:
                            removed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"warning: could not prune existing definitions: {exc}", file=sys.stderr)

        body = {
            "modelName": model,
            "matchPattern": match_pattern,
            "unit": "TOKENS",
            "inputPrice": input_price,
            "outputPrice": output_price,
        }
        resp = client.post("/api/public/models", json=body)
        if resp.status_code >= 300:
            _die(f"Langfuse model create failed: {resp.status_code} {resp.text}")

    print(
        f"registered Langfuse model '{model}' "
        f"(removed {removed} stale def(s)); "
        f"input ${input_price:.10f}/tok, output ${output_price:.10f}/tok "
        f"(= ${price[0]:.4f}/${price[1]:.4f} per 1M). "
        f"New traces will show cost."
    )


if __name__ == "__main__":
    main()
