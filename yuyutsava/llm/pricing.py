"""Model prices: the static price table, and the live lookup that keeps it current.

**The table.** ``PRICES`` holds USD per **1M** input/output tokens, keyed by
model-name prefix (longest prefix wins). :func:`load_price_table` merges it with
``~/.yuyutsava/model_prices.json``, and :func:`estimate_cost_usd` prices a call
from the result for the cost ledger (:class:`yuyutsava.policy.builtin.usage.UsagePolicy`)
and the context meter. Extend it through the JSON file::

    {"my-model-prefix": [0.50, 1.50]}

**The live lookup.** A model the table doesn't know (e.g. a freshly switched
OpenRouter model) silently costs ``$0``. :func:`refresh_price_cache` asks the
model's own provider for its current price
(:meth:`yuyutsava.llm.base.Provider.live_price` — OpenRouter publishes every
model's price, a local Ollama costs nothing, the rest have no price API) and
caches it into ``model_prices.json``, so the table picks it up unchanged.

Everything here is best-effort and never raises: a failed fetch just leaves the
last cached (or static) price in place.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from yuyutsava.storage.paths import state_dir

if TYPE_CHECKING:
    from yuyutsava.llm.target import ModelTarget

logger = logging.getLogger("yuyutsava.llm.pricing")

# The file load_price_table() merges over PRICES (prefix -> [in, out]).
PRICE_FILE = "model_prices.json"
# Sidecar recording when each model was last fetched, so we can honor a TTL
# without polluting the price file the ledger reads.
_META_FILE = ".model_prices_cache.json"

_DEFAULT_TTL_SEC = 24 * 3600

Price = tuple[float, float]  # (input_usd_per_1M, output_usd_per_1M)

#: Asks a model's provider for its current price; ``None`` when it publishes none.
LivePrice = Callable[["ModelTarget"], Price | None]


# --------------------------------------------------------------------------- #
# Cache into model_prices.json                                                 #
# --------------------------------------------------------------------------- #


class _UnreadableFile(Exception):
    """The file exists but does not hold a JSON object."""


def _read_json(path: Path) -> dict:
    """The JSON object in *path*; ``{}`` when there is no file.

    An existing file that cannot be read or parsed, or holds something other
    than an object, raises :class:`_UnreadableFile` — "missing" is safe to
    write over, "unreadable" is not.
    """
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise _UnreadableFile(str(exc)) from exc
    if not isinstance(obj, dict):
        raise _UnreadableFile(f"expected a JSON object, found {type(obj).__name__}")
    return obj


def _write_json_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
    tmp.replace(path)


def refresh_price_cache(
    target: ModelTarget,
    *,
    live_price: LivePrice,
    ttl_sec: int = _DEFAULT_TTL_SEC,
    dir_path: Path | None = None,
) -> Price | None:
    """Ensure ``model_prices.json`` holds a live price for *target*'s model.

    *live_price* is the provider's own lookup — ``provider_for(target).live_price``.

    Skips the network when a fresh (< ``ttl_sec``) price for this exact model is
    already cached *and* present in the price file. Merges into the file rather
    than overwriting it, so a hand-authored entry is preserved on the next
    ``load_price_table`` (file entries win by the same key); a file it cannot
    read is left untouched. Never raises; returns the ``(in, out)`` price now in
    effect, or None.
    """
    try:
        model = target.model
        if not model:
            return None
        base = dir_path or state_dir()
        price_path = base / PRICE_FILE
        meta_path = base / _META_FILE
        try:
            prices = _read_json(price_path)
        except _UnreadableFile as exc:
            # Writing now would replace every hand-authored price with one
            # entry. Leave the file for the user; the built-in table serves.
            logger.warning(
                "pricing: %s is unreadable (%s) — leaving it untouched; fix or "
                "delete it to cache live prices",
                price_path,
                exc,
            )
            return None
        try:
            meta = _read_json(meta_path)
        except _UnreadableFile:
            meta = {}  # our own cache of fetch times: safe to rebuild
        now = time.time()

        cached = meta.get(model)
        if (
            isinstance(cached, dict)
            and (now - float(cached.get("ts", 0))) < ttl_sec
            and model in prices
        ):
            pair = prices[model]
            return (float(pair[0]), float(pair[1]))

        price = live_price(target)
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
            model,
            price[0],
            price[1],
        )
        return price
    except Exception:
        logger.warning("pricing: refresh_price_cache failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# The price table: per-million prices by model-name prefix, and the estimates
# built on it.
# ---------------------------------------------------------------------------


# USD per 1M input/output tokens, keyed by model-name prefix. Local models
# (Ollama) cost nothing; unknown models estimate to 0.0 — an unknown row in
# llm_usage with est_cost_usd=0 is a prompt to extend model_prices.json,
# not a billing claim.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-opus-4": (5.00, 25.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "llama-3.3-70b": (0.59, 0.79),
    "llama-3.1-8b": (0.05, 0.08),
    # Cloud models reachable via the new providers (approximate list prices —
    # longest-prefix wins, so these override the generic local-Ollama zeros
    # below, e.g. "mistral-large-latest" → mistral-large, not mistral).
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "mistral-large": (2.00, 6.00),
    "command-r-plus": (2.50, 10.00),
    "command-r": (0.15, 0.60),
    # Common local-Ollama families — explicit zeros so a price-table reader
    # can tell "known free" from "unknown".
    "llama3": (0.0, 0.0),
    "gemma": (0.0, 0.0),
    "qwen": (0.0, 0.0),
    "mistral": (0.0, 0.0),
    "nomic-embed": (0.0, 0.0),
}


def load_price_table() -> dict[str, tuple[float, float]]:
    """``PRICES`` merged with ``~/.yuyutsava/model_prices.json`` (file wins).

    A malformed file is logged and ignored — pricing must never break a run.
    """
    table = dict(PRICES)
    path = state_dir() / "model_prices.json"
    if not path.exists():
        return table
    try:
        raw = json.loads(path.read_text())
        for prefix, pair in raw.items():
            in_usd, out_usd = float(pair[0]), float(pair[1])
            table[str(prefix)] = (in_usd, out_usd)
    except Exception:
        logger.exception("model_prices.json unreadable — using built-in PRICES")
    return table


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    prices: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Estimated spend for one call. Longest matching prefix wins; 0.0 unknown.

    An unpriced model returns ``0.0`` — we genuinely do not know, and inventing
    a number would be worse than admitting it. But ``0.0`` is indistinguishable
    from a free call once the ledger sums it, so :func:`_warn_unpriced_once`
    says so rather than letting the cost column quietly read zero forever.

    Found by running the daemon: the price table ships ``gemini-2.5-flash`` and
    the configured model was ``gemini-3.5-flash``, which prefix-matches nothing.
    Every call was being costed at $0.00 with no indication anywhere.
    """
    best = _best_price(model, prices)
    if best is None:
        _warn_unpriced_once(model)
        return 0.0
    return (input_tokens * best[0] + output_tokens * best[1]) / 1_000_000


def _best_price(
    model: str, prices: dict[str, tuple[float, float]] | None = None
) -> tuple[float, float] | None:
    """Longest matching prefix's ``($/1M in, $/1M out)``, or ``None``."""
    table = prices if prices is not None else PRICES
    best: tuple[float, float] | None = None
    best_len = -1
    for prefix, pair in table.items():
        if model.startswith(prefix) and len(prefix) > best_len:
            best, best_len = pair, len(prefix)
    return best


def is_priced(model: str, prices: dict[str, tuple[float, float]] | None = None) -> bool:
    """Whether a cost figure for *model* means anything.

    A caller that only has ``est_cost_usd`` cannot tell "this call was free"
    from "we have no price for this model", and the difference matters as soon
    as the number is put in front of someone. Ask this first and label the
    figure ``unpriced`` when it is False; unlike :func:`estimate_cost_usd` this
    never warns, so a display can call it per render.
    """
    return _best_price(model, prices) is not None


#: Models already reported as unpriced. Per-call warning would be one line per
#: model call — noise an operator learns to scroll past, which is the same
#: failure as saying nothing.
_UNPRICED_SEEN: set[str] = set()


def _warn_unpriced_once(model: str) -> None:
    """Say, once, that this model's spend is not being counted."""
    if not model or model in _UNPRICED_SEEN:
        return
    _UNPRICED_SEEN.add(model)
    # DEBUG, not WARNING. Every surface now reports this where it belongs — the
    # CLI panel and the app aside show "unpriced" on the cost row,
    # GET /usage/summary lists `unpriced_models`, and the dashboard says what
    # to do about it in its notices column. As a WARNING it was a yellow line
    # landing on top of the user's prompt, restating what the screen already
    # said. Kept for operators running with DEBUG.
    logger.debug(
        "no price entry for model %r — its llm_usage rows will record "
        "est_cost_usd=0.00 and the usage totals will under-report. Add it to "
        '~/.yuyutsava/model_prices.json as {"%s": [<$/1M in>, <$/1M out>]} '
        '(prefixes match, so "%s" would cover variants).',
        model,
        model,
        model.rsplit("-", 1)[0] if "-" in model else model,
    )


def unpriced_models() -> frozenset[str]:
    """Models seen this process with no price entry. For diagnostics/tests."""
    return frozenset(_UNPRICED_SEEN)
