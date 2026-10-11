# Contributing to YUYUTSAVA

Thanks for your interest. This is an alpha project whose internals still move,
so the most useful contributions right now are bug reports with reproductions,
and small focused pull requests.

---

## Development setup

```bash
git clone https://github.com/Abhinav210310453045/YUYUTSAVA-backend.git
cd YUYUTSAVA-backend
python scripts/install.py --extra dev   # the app, the command, every feature, the test tools

yuyutsava models setup                  # connect a model provider
```

`ollama` needs no API key and costs nothing, which makes it the easiest
provider for development:

```bash
LLM_PROVIDER=ollama
OLLAMA_MODEL=llama3.2:3b
```

`scripts/install.py` is the only install step, for contributors too: it syncs
the `.venv` from the lock without removing anything (a plain `uv sync` is exact
and strips every optional feature it isn't told about), and installs the
`yuyutsava` command. `--lean` skips the optional features; `--extra` adds one —
see `[project.optional-dependencies]` in `pyproject.toml`.

For the desktop app:

```bash
cd electron-app
npm install
npm run dev
```

---

## Branching and pull requests

`main` is the only long-lived branch. It is protected, always releasable, and
every change reaches it through a pull request.

```
main ────●────●────●────●───────  protected, squash-merged, tagged
          ↖    ↖    ↖    ↖
        feat/  fix/  docs/ chore/     short-lived, one PR each
```

**Branch names** use a type prefix:

| Prefix | For |
|---|---|
| `feat/` | A new capability |
| `fix/` | A bug fix |
| `docs/` | Documentation only |
| `refactor/` | Behaviour-preserving restructuring |
| `perf/` | Performance work |
| `test/` | Tests only |
| `chore/` | Build, deps, tooling |

**One logical change per PR.** A PR that adds a feature, renames a module and
bumps a dependency is three PRs. Large mechanical changes (a reformat, a
codemod) must be their own PR, clearly labelled, so a semantic change can never
hide inside a large diff.

**PR titles are [Conventional Commits](https://www.conventionalcommits.org/):**

```
feat(voice): gate wake-word detection behind VAD
fix(storage): stop dropping task_id on usage rows
docs(architecture): correct the transport frame table
```

PRs are **squash-merged**, so the PR title becomes the commit message on `main`.
Write it for someone reading `git log` in a year.

**In the PR body, say why.** What was wrong or missing, what you changed, and
how you verified it. If you could not verify something, say that too — an
honest "untested on Windows" is far more useful than silence.

---

## Verifying your change

What CI runs, and what you should run locally:

```bash
uv pip install -e '.[dev]'                      # once: pytest
python -m pytest -m "not pg and not billable and not slow"   # the hermetic tests CI runs
python -m compileall yuyutsava                  # syntax across the package
uvx ruff@0.16.5 check yuyutsava test scripts    # lint gate — must be clean
uvx ruff@0.16.5 format yuyutsava test scripts   # formatting — CI runs it with --check
python scripts/check_lint_ratchet.py            # ratcheted lint counts may not grow
uv build --wheel                                # packaging metadata still valid
cd electron-app && npm run lint && npm test && npm run build   # the desktop app
```

In `electron-app/`, `npm run lint` allows **no** ESLint warnings and caps every
source file at 300 lines; the few files still over it are listed in
`max-lines-allowlist.json`, which may only shrink (the check fails when a listed
file no longer needs its entry). `npm run format:check` is Prettier.

The whole suite, including the Postgres tests, is `python -m pytest`. The
Postgres tests use their own database (`yuyutsava_test`, created on first use,
or `YUYUTSAVA_TEST_PG_DSN`) and refuse the application's. [test/README.md](test/README.md) covers the markers, the layout and
the shared helpers.

The lint rules, and why each exists, are in
[docs/code-review/v0.1.0/01-standards.md](docs/code-review/v0.1.0/01-standards.md).
When a change removes ratcheted violations, run
`python scripts/check_lint_ratchet.py --update` in the same commit: CI fails when
a count falls without the baseline moving, so the ratchet can only tighten.

For agent behaviour, run it. Most changes here are only really testable by
driving the CLI or the daemon:

```bash
uv run yuyutsava --verbose "a task that exercises your change"
uv run yuyutsava daemon --verbose
```

The checks in `scripts/` each prove one invariant — what it proves, when to
run it and what it needs are listed in [scripts/README.md](scripts/README.md).
Run `scripts/verify_imports.py` after moving an import or editing an
`__init__.py`, and `scripts/verify_framework_contract.py` after any dependency
change.

> **Careful with `scripts/smoke_async.py`** — it makes real, billable LLM calls. It
> is guarded and refuses to run unless `YUYUTSAVA_ALLOW_BILLABLE=1` is set.
> Do not remove that guard.

---

## Dependency ceilings are deliberate

`pyproject.toml` pins upper bounds on `deepagents`, `langgraph`, `langchain`
and friends. This is not laziness. The codebase couples to framework internals,
and **some of those couplings fail silently on upgrade** — see
[`F-T04` in the legacy ledger](docs/code-review/v0.1.0/10-legacy-ledger.md#f-t04).

Do not relax a ceiling without running `scripts/verify_framework_contract.py`
against the new version. That run is the acceptance criterion, and a PR raising
a ceiling should say in its body that it passed.

**Upgrading langgraph-api** — the chat REPL says when it is end of life (two
minors behind; it ships a minor every few weeks). Upgrade it through the lock,
with its family, never with `pip install -U`, which moves one package off
`uv.lock` and on its own stops at an older minor:

```bash
uv lock --upgrade-package langgraph-api --upgrade-package langgraph-runtime-inmem \
        --upgrade-package langgraph-cli --upgrade-package langgraph --upgrade-package langgraph-sdk
python scripts/install.py --extra dev
.venv/bin/python scripts/verify_framework_contract.py
uv run python scripts/async_subagents_e2e.py   # the in-process server, end to end; no model calls
```

---

## Code conventions

The standard, rule by rule and with how each is enforced, is
[docs/code-review/v0.1.0/01-standards.md](docs/code-review/v0.1.0/01-standards.md).
The points that matter most day to day:

- **Match the surrounding code.** Comment density, naming and idiom vary by
  subpackage; follow the file you are in rather than a global house style.
- **Comments explain *why*.** This codebase's existing comments are unusually
  good at recording the reason a constraint exists (see the dependency pins).
  Keep that up — a comment restating the code is noise, one recording a
  hard-won reason is the most valuable line in the file.
- **`yuyutsava/platform/` is the only place OS-specific primitives live.** If
  you find yourself writing `sys.platform` elsewhere, that is the signal to add
  it to the platform substrate instead.
- **OS-invariance is a real constraint.** When changing something with
  platform-specific behaviour, keep all the platform branches, even the ones
  you cannot test.
- **`yuyutsava/ports/` must stay dependency-free.** It is the acyclic layer both
  sides of a dependency cycle import.
- **Findings are keyed and cited from source.** Code-review findings are `R-*`
  (in `docs/code-review/v0.1.0/`); the retired structural review's `F-*` IDs
  resolve in its [legacy ledger](docs/code-review/v0.1.0/10-legacy-ledger.md).
  The decisions behind the structure are in
  [docs/architecture/decisions/](docs/architecture/decisions/README.md). If you
  resolve a finding, update the citation and its status.

---

## Known gaps

Being upfront so you are not surprised:

- **CI runs the hermetic tests only.** Tests marked `pg` (a live Postgres),
  `billable` or `slow` are deselected there; run them locally before a change
  to storage.
- **The Python lint gate is partial.** Defect, import-placement and
  error-handling rules are gated at zero; four noisier rules (`C901`,
  `PLC0415`, `PLR0913`, `T201`) are ratcheted, so their counts may only fall.
  The desktop app's lint is at zero warnings. Please do not bundle a reformat
  into an unrelated PR.
- **No type checker runs.** Neither package is type-checked in CI, and some
  builders still take `Any` (carried in the code review).
- **Interfaces are unstable.** This is alpha. Expect things to move.
- **The code review is honest about the debt.** Start at
  [docs/code-review/v0.1.0/00-executive-summary.md](docs/code-review/v0.1.0/00-executive-summary.md)
  and the carried items in
  [11-remediation-plan.md](docs/code-review/v0.1.0/11-remediation-plan.md#carried)
  if you want to know where the structural problems are before you pick
  something up.

---

## Reporting bugs

Open an issue with what you ran, what happened, and what you expected. The
things that make a report actionable here:

- Your `LLM_PROVIDER` and model
- CLI or daemon mode
- OS
- Relevant output from `--verbose`

Please scrub API keys, absolute paths containing your username, and anything
from a real conversation before pasting logs.

For security issues, do **not** open a public issue — see
[SECURITY.md](SECURITY.md).

---

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), per section 5 of that license.
