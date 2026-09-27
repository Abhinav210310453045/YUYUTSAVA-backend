# Tests

## Running them

pytest is the runner of record. It also runs the `unittest`-style files.

```sh
uv pip install -e '.[dev]'                     # once: pytest
.venv/bin/python -m pytest                     # everything
.venv/bin/python -m pytest test/storage        # one package
.venv/bin/python -m pytest -m "not pg and not billable and not slow"   # what CI runs
```

Each test file's docstring ends with the command that runs just that file.

## Markers

Markers are declared in `pyproject.toml`. `--strict-markers` rejects a misspelt one.

| Marker | Meaning | How it is applied |
|---|---|---|
| `pg` | Needs a live Postgres at `YUYUTSAVA_PG_DSN` | `test.support.pg.requires_pg`. It also **skips** the test when nothing answers, so the mark and the skip cannot drift apart |
| `billable` | Makes real, paid model calls | `@pytest.mark.billable`. The test tree has none: the one smoke script is `scripts/smoke_async.py` |
| `slow` | Takes seconds, not milliseconds | `@pytest.mark.slow` |

**Point `YUYUTSAVA_PG_DSN` at a scratch database before running `pg` tests.**
Unset, it falls back to the application's default, which is the real one:

```sh
YUYUTSAVA_PG_DSN=postgresql://yuyutsava:yuyutsava@127.0.0.1:5435/<scratch-db> \
  .venv/bin/python -m pytest -m pg
```

## Layout

- **The tree mirrors the package.** A test for `yuyutsava/<pkg>/<mod>.py` lives
  in `test/<pkg>/`. Tests for modules at the package root (`text`, `wav`, `ansi`,
  `run_context`) sit at the root of `test/`.
- **Shared helpers live in `test/support/`**, never beside the tests that use
  them. Import them as packages (`from test.support.pg import requires_pg`);
  no test changes `sys.path`.

  | Module | Holds |
  |---|---|
  | `paths.py` | `REPO` and `PACKAGE`, for tests that read the repository's files. Use these instead of counting `Path(__file__).parents[N]`, which breaks when a test moves |
  | `pg.py` | `pg_dsn()`, `PG_UP`, `requires_pg` |
  | `stores.py` | `fake_board()`, `scratch_visual_store()` |
  | `fake_todo_store.py` | `FakeTodoStore`, an in-memory board store |
  | `ws.py` | `collect_until()`. It reads a test WebSocket under a deadline, so a hang fails the test instead of stopping the suite |
  | `agent_fingerprint.py` | Builds each master agent offline and fingerprints its structure (`test/core/test_agent_fingerprint.py`) |

- **Tests that guard structure** (the layer map, `ports` being a leaf) are in
  `test/architecture/`.

### Exceptions

- **`test/mcp_layer/`** tests `yuyutsava/mcp/`. A `test/mcp/` package would shadow
  the MCP SDK's `mcp` package for every test in the run.
- **`test/framework_contract/`** holds the deepagents/LangGraph couplings that
  fail silently on upgrade. `scripts/verify_framework_contract.py` also runs it
  as a standalone gate.

## Rules

- **No real services in a unit test.** Pass fakes through the seam the code
  actually uses, and assert the fake was called. A fake that nothing reads
  silently turns a unit test into a live call (`R-TST-08`).
- **No machine or personal state.** Tests that read `~/.yuyutsava` or the host
  set a scratch `YUYUTSAVA_HOME` and pin what varies. The agent fingerprint used
  to hash its author's own memories and skills (`R-TST-15`).
- **Every new gate is negative-controlled.** Break the thing it guards once and
  watch it fail, before trusting it to pass.
