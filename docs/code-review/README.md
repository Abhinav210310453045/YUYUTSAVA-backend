# Code reviews

Versioned, evidence-based reviews of the YUYUTSAVA codebase. Each version
assesses one pinned commit, states its findings once with a reproducible
measurement, and carries a remediation plan. After its fixes land, a version is
edited only to record status — the next review gets a new version.

| Version | Reviewed commit | Scope | Status |
|---|---|---|---|
| [v0.1.0](v0.1.0/README.md) | `f4ec45c` — `main` on 2026-09-14, after the context-telemetry merge | `yuyutsava/`, `test/`, `scripts/`, `electron-app/` | Fixes in progress on branch `code-improvement` |

Release tags and review versions share a number line: `v0.1.0-simple-cli` is
the first release tag, and Code Review v0.1.0 is the first review of that line.

## Reproducing the numbers

Every metric in a review comes from a committed command, so a later review
measures the same thing the same way:

```bash
.venv/bin/python scripts/measure_code_health.py      # AST/text metrics (never imports the package)
.venv/bin/python scripts/verify_imports.py           # every module imports first, alone
uvx ruff@0.16.5 check yuyutsava test scripts --statistics
```
