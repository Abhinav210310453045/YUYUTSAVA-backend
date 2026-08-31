# Contributing to YUYUTSAVA

Thanks for your interest. This is an alpha project whose internals still move,
so the most useful contributions right now are bug reports with reproductions,
and small focused pull requests.

---

## Development setup

```bash
git clone https://github.com/Abhinav210310453045/YUYUTSAVA-backend.git
cd YUYUTSAVA-backend
uv sync

cp .env.example .env      # set ONE provider key
```

`ollama` needs no API key and costs nothing, which makes it the easiest
provider for development:

```bash
LLM_PROVIDER=ollama
OLLAMA_MODEL=llama3.2:3b
```

Optional extras are installed per feature — `uv sync --extra voice`,
`--extra visuals`, `--extra memory`, `--extra vertex`, and so on. See
`[project.optional-dependencies]` in `pyproject.toml`.

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

There is no full test gate yet (see *Known gaps* below). What CI runs, and what
you should run locally:

```bash
python -m compileall yuyutsava        # syntax across the package
uv build --wheel                      # packaging metadata still valid
cd electron-app && npm run build      # renderer builds
```

For agent behaviour, run it. Most changes here are only really testable by
driving the CLI or the daemon:

```bash
uv run yuyutsava --verbose "a task that exercises your change"
uv run yuyutsava daemon --verbose
```

Some checks in `scripts/` verify specific invariants:

| Script | Checks |
|---|---|
| `scripts/verify_framework_contract.py` | The deepagents/LangGraph couplings that fail silently on upgrade |
| `scripts/verify_loop_affinity.py` | One chat model per event loop |
| `scripts/verify_diagrams.py` | Mermaid blocks in the architecture docs actually render |

> **Careful with `test/test_async.py`** — it makes real, billable LLM calls. It
> is guarded and refuses to run unless `YUYUTSAVA_ALLOW_BILLABLE=1` is set.
> Do not remove that guard.

---

## Dependency ceilings are deliberate

`pyproject.toml` pins upper bounds on `deepagents`, `langgraph`, `langchain`
and friends. This is not laziness. The codebase couples to framework internals,
and **some of those couplings fail silently on upgrade** — see
[docs/architecture/review/04-findings-thirdparty-coupling.md](docs/architecture/review/04-findings-thirdparty-coupling.md).

Do not relax a ceiling without running `scripts/verify_framework_contract.py`
against the new version. That run is the acceptance criterion, and a PR raising
a ceiling should say in its body that it passed.

---

## Code conventions

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
- Architecture-review findings are keyed (`F-S07`, `F-T01`, …) and cited from
  source docstrings. If you resolve one, update the citation.

---

## Known gaps

Being upfront so you are not surprised:

- **No test gate in CI.** `test/` exists and is substantial, but an unknown
  subset needs API keys or a live Postgres, so it has never run in CI. Making a
  hermetic subset run is a welcome contribution.
- **No lint gate.** The codebase predates any ruff configuration and would not
  pass one today. Adoption is planned as its own change; please do not bundle a
  reformat into an unrelated PR.
- **Interfaces are unstable.** This is alpha. Expect things to move.
- **The architecture review is honest about the debt.** Start at
  [docs/architecture/review/00-executive-summary.md](docs/architecture/review/00-executive-summary.md)
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
