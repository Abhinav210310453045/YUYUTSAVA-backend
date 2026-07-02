"""Resolve the *live* per-token price of the configured model from its provider.

The cost ledger (:class:`yuyutsava.daemon.usage.UsageRecorder` via
:func:`yuyutsava.core.model_router.estimate_cost_usd`) prices a call by
longest-prefix match against a table that merges the built-in ``PRICES`` with
``~/.yuyutsava/model_prices.json``. Historically that meant hand-editing the
JSON whenever the model changed — and a model the table doesn't know (e.g. a
freshly switched OpenRouter model) silently costs ``$0``.

This module removes the hand-editing: given the active provider
:class:`~yuyutsava.core.config.LlmSettings`, it fetches the exact input/output
price for ``settings.model`` *from the provider selected in env* and caches it
into ``model_prices.json`` so the existing ledger picks it up unchanged. A
model/provider swap is reflected automatically on the next refresh.

Provider support:
  * **OpenRouter** — ``GET {base_url}/models`` exposes ``pricing.prompt`` /
    ``pricing.completion`` (USD **per token**) per model id. Exact and live.
  * **Ollama** — local inference, ``(0.0, 0.0)``.
  * **Anthropic / Groq** — no public per-model pricing API, so we leave the
    static table / user override as the source (returns ``None`` here).

Everything here is best-effort and never raises: a failed fetch just leaves the
last cached (or static) price in place.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx

from yuyutsava.storage.paths import state_dir

logger = logging.getLogger("yuyutsava.core.pricing")

# The map consumed by ``model_router.load_price_table`` (prefix -> [in, out]).
PRICE_FILE = "model_prices.json"
# Sidecar recording when each model was last fetched, so we can honor a TTL
# without polluting the price file the ledger reads.
_META_FILE = ".model_prices_cache.json"

_DEFAULT_TTL_SEC = 24 * 3600
_HTTP_TIMEOUT_SEC = 6.0
_PER_MILLION = 1_000_000

Price = tuple[float, float]  # (input_usd_per_1M, output_usd_per_1M)


# --------------------------------------------------------------------------- #
# Provider detection (by base_url — keeps this decoupled from config classes)  #
# --------------------------------------------------------------------------- #


def _is_openrouter(base_url: str) -> bool:
    return "openrouter.ai" in (base_url or "")


def _is_ollama(base_url: str) -> bool:
    b = (base_url or "").lower()
    return "11434" in b or "ollama" in b


# --------------------------------------------------------------------------- #
# Fetch                                                                        #
# --------------------------------------------------------------------------- #


def _fetch_openrouter_price(base_url: str, api_key: str, model: str) -> Price | None:
    """Exact ``(in, out)`` per-1M price for ``model`` from OpenRouter, or None."""
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        resp = httpx.get(url, headers=headers, timeout=_HTTP_TIMEOUT_SEC)
        resp.raise_for_status()
        data = resp.json().get("data") or []
    except Exception:
        logger.warning("pricing: OpenRouter /models fetch failed", exc_info=True)
        return None
    for entry in data:
        if entry.get("id") != model:
            continue
        pricing = entry.get("pricing") or {}
        try:
            # OpenRouter reports USD *per token* as strings; scale to per-1M.
            prompt = float(pricing.get("prompt"))
            completion = float(pricing.get("completion"))
        except (TypeError, ValueError):
            logger.info("pricing: unparseable OpenRouter pricing for %r", model)
            return None
        return (prompt * _PER_MILLION, completion * _PER_MILLION)
    logger.info("pricing: model %r not found in OpenRouter catalog", model)
    return None


def resolve_model_price(settings: object) -> Price | None:
    """Live ``(in, out)`` per-1M price for ``settings.model`` from its provider.

    Returns None when the provider has no queryable price API (Anthropic/Groq)
    or the model is unknown — callers then fall back to the static table.
    """
    base_url = str(getattr(settings, "base_url", "") or "")
    model = str(getattr(settings, "model", "") or "")
    api_key = str(getattr(settings, "api_key", "") or "")
    if not model:
        return None
    if _is_openrouter(base_url):
        return _fetch_openrouter_price(base_url, api_key, model)
    if _is_ollama(base_url):
        return (0.0, 0.0)  # local inference is free
    return None


# --------------------------------------------------------------------------- #
# Cache into model_prices.json                                                 #
# --------------------------------------------------------------------------- #


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_json_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
    tmp.replace(path)


def refresh_price_cache(
    settings: object,
    *,
    ttl_sec: int = _DEFAULT_TTL_SEC,
    dir_path: Path | None = None,
) -> Price | None:
    """Ensure ``model_prices.json`` holds a live price for ``settings.model``.

    Skips the network when a fresh (< ``ttl_sec``) price for this exact model is
    already cached *and* present in the price file. Merges into the file rather
    than overwriting it, so a hand-authored entry is preserved on the next
    ``load_price_table`` (file entries win by the same key). Never raises;
    returns the ``(in, out)`` price now in effect, or None.
    """
    try:
        model = str(getattr(settings, "model", "") or "")
        if not model:
            return None
        base = dir_path or state_dir()
        price_path = base / PRICE_FILE
        meta_path = base / _META_FILE
        prices = _read_json(price_path)
        meta = _read_json(meta_path)
        now = time.time()

        cached = meta.get(model)
        if (
            isinstance(cached, dict)
            and (now - float(cached.get("ts", 0))) < ttl_sec
            and model in prices
        ):
            pair = prices[model]
            return (float(pair[0]), float(pair[1]))

        price = resolve_model_price(settings)
        if price is None:
            # No live price — defer to whatever the file/static table already has.
            if model in prices:
                pair = prices[model]
                return (float(pair[0]), float(pair[1]))
            return None

        prices[model] = [price[0], price[1]]
        meta[model] = {"ts": now, "in": price[0], "out": price[1]}
        _write_json_atomic(price_path, prices)
        _write_json_atomic(meta_path, meta)
        logger.info(
            "pricing: cached %s = $%.4f in / $%.4f out per 1M tokens",
            model, price[0], price[1],
        )
        return price
    except Exception:
        logger.warning("pricing: refresh_price_cache failed", exc_info=True)
        return None
