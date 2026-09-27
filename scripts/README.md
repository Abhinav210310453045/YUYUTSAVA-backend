# Maintainer scripts

Run from the repository root with the project's interpreter, for example
`.venv/bin/python scripts/verify_imports.py`. None needs `sys.path` changes: the
package is installed editable. The one exception is `verify_framework_contract.py`,
which also imports two test modules and says so.

**Exit codes.** Every gate below uses the same contract: **0** means the
property holds; **non-zero** means it does not, and the output says where.
Measurement and one-off tools exit 0 unless they crash.

## Gates — run them, trust their exit code

| Script | Proves | Run it when | Needs |
|---|---|---|---|
| `check_lint_ratchet.py` | Ratcheted lint counts equal `lint_ratchet.json`, and every `noqa: BLE001` states a reason. `--update` records a clean-up and refuses an increase | Every commit (CI runs it) | uv (ruff pinned in `pyproject.toml`) |
| `verify_imports.py` | Every `yuyutsava` module imports on its own, in a fresh interpreter. `--save` and `--compare` keep a timing baseline | After moving an import, splitting or moving a module, or editing an `__init__.py` | — |
| `verify_framework_contract.py` | The deepagents/LangGraph couplings that fail silently on upgrade still hold. Runs `test/framework_contract/` and `test/policy/test_filesystem_prompt_override.py` | After any dependency change: `uv sync`, `uv lock --upgrade`, a bumped pin | — |
| `verify_loop_affinity.py` | One chat model and one pool per event loop, across two real loops in two threads | After touching `aio/`, the Postgres pool, the embedder or MCP marshalling | — |
| `verify_diagrams.py` | Every Mermaid block in the docs renders | After editing a diagram | A Kroki server (default `http://127.0.0.1:8000`) |
| `async_subagents_e2e.py` | The async-subagent slice end to end: host, watcher, channels, `/cli/attach`, SSE, ask and resume. It prints ✓/✗ per check | After touching `async_subagents/`, `daemon/channels.py` or the ask path | — (uses a mock graph, no model) |

## Measurements — read their output

| Script | Measures |
|---|---|
| `measure_code_health.py` | The metrics the code reviews track: in-function imports, silent excepts, prints outside `cli/`, globals and more. `--details N` shows the worst N, `--json` gives machine-readable output |
| `measure_framework_surface.py` | How much of the code is built inside the agent framework (ADR-004) |
| `benchmark_policy_adapter.py` | What the policy layer costs per model and tool call (ADR-004's hot-path risk) |
| `print_agent_matrix.py` | The capability matrix of the three masters, and each by-design divergence (ADR-001) |

## One-off tools

| Script | Does | Needs |
|---|---|---|
| `migrate_sessions_sqlite_to_pg.py` | Copies CLI sessions from `~/.yuyutsava/sessions.db` into Postgres, once, after switching backends. Idempotent | Postgres |
| `langfuse_register_model.py` | Registers the configured model and its live price in a self-hosted Langfuse, so traces show cost | Langfuse credentials in the environment |
| `smoke_async.py` | **Makes real, billable model calls.** Two concurrent turns, proving `astream_agent` is async. Refuses unless `YUYUTSAVA_ALLOW_BILLABLE=1` | Model credentials, and money |

## Not a script

- `pg-init/01-create-langfuse-db.sql` is `docker compose` infrastructure. The
  Postgres container runs it on first start to create Langfuse's database.
- `lint_ratchet.json` is `check_lint_ratchet.py`'s baseline. Only
  `--update` writes it.
