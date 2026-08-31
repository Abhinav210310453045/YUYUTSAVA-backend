# YUYUTSAVA OS-Invariance + System Warden Architecture

> Status: **Code-complete, macOS-verified.** All layers (L0–L5 + TTS + Electron packaging)
> are implemented and pass standalone checks on macOS; the daemon boots and the tr_* tools
> run through the full path. Remaining: behavioral testing on an actual Windows host
> (daemon lifecycle, tr_execute PowerShell, UAC elevation, nsis build) — see Verification.
>
> Canonical design + task doc for running YUYUTSAVA natively on Windows while keeping
> macOS/Linux working — and, more broadly, for turning the daemon into an OS-invariant
> *system warden* that can administer any host. Note: dedicated gap-filler tools
> (tr_move/copy/mkdir/unzip/edit) were **deferred** — `tr_run_python` covers them portably
> without new permission-engine surface.

## Context & Vision

YUYUTSAVA is not a dev tool — it is a **resident system agent** ("living soul"): the CLI
is its conversational form, the daemon is the permanent warden of the machine. A user on
*any* OS must be able to say "cure this problem" and the agent must actually administer the
system — services, installs (`.msi`), diagnostics (`sfc`, `DISM`), settings — the way a
human admin would via Win+R / `.msc` consoles on Windows or `launchctl` / `defaults` on macOS.

OS-invariance is step one of that mission. The governing thesis:

> **OS-specific *knowledge* lives in the model + skills (data).
> OS-specific *code* lives in exactly one thin platform layer.
> Every system action flows through the TaskRunner pipeline: zones → permission → consent → audit.**

The LLM already knows PowerShell, bash, `msiexec`, `launchctl`, `systemctl` deeply — that
knowledge IS the universal adapter ("talks to any system"). The codebase's job is only to
give it (a) a *correct, OS-native socket* to speak through, (b) a *passport* telling it
exactly what system it's on, and (c) a *license* (permission/consent spine) governing what
it may do. That's how we get any-OS capability **without duplicating logic per OS**.

**Locked decisions:**
- **Native Windows** (no WSL/Docker requirement). Docker mode remains an opt-in hermetic mode.
- **deepagents built-ins stay hidden** — `tool_filter_middleware.py` already suppresses
  `read_file/write_file/edit_file/execute/grep/ls/glob`. **`tr_*` is the sole system
  gateway**; all new capability lands there so zoning/permission/consent always apply. This
  is the bottom line: nothing touches the system except through TaskRunner.
- **Deterministic operations become pure Python** (no latency concern — removes a subprocess
  spawn; cost is dominated by LLM + network).
- **Portable scripting language = Python**, not bash (guaranteed present via
  `sys.executable`, bit-identical across OSes). Native shell remains for genuinely OS-native
  work — which, per the warden vision, is a **first-class capability**, not an escape hatch.
- **Elevation = per-operation now** (UAC / admin-osascript / pkexec) via a **standalone
  reusable module**; a persistent privileged helper is documented as deferred Phase 2.
- **System administration = knowledge-driven + skills**, no structured `tr_service`/`tr_package`
  driver layer (can be added later per-domain if consent granularity demands it).

---

## The Layer Model

```
 L5  SAFETY SPINE      zones + permission middleware + consent grants + audit  (per-OS aware)
 L4  WARDEN SKILLS     platform-tagged skill playbooks (data, semantic recall) ← capability growth
 L3  NATIVE CHANNEL    tr_execute → PowerShell (win) / bash (posix); elevation; GUI launch
 L2  PORTABLE SCRIPT   tr_run_python (sys.executable, argv-exec, zero quoting)
 L1  DETERMINISTIC     tr_grep / tr_fetch_url / file ops — pure Python, OS-invariant
 L0  PLATFORM SUBSTRATE  yuyutsava/platform/: compat (locks/process/signals) + HostProfile
```

Rule of thumb the prompts teach the model: *known operation → L1; custom logic → L2;
OS-native administration → L3 (guided by L4 skills); everything gated by L5.*

---

## L0 — Platform substrate (`yuyutsava/platform/`)

> New package. Every OS-specific primitive lives here so the rest of the codebase stays
> OS-invariant, and so future subsystems can reuse these standard components.

### 0a. compat primitives *(hard blockers)*
- **`filelock.py`** — replace `fcntl.flock` with `portalocker` (new dep) in
  `storage/base.py` (migration lock), `daemon/singleton.py` (single-daemon lock),
  `async_subagents/host_lock.py` (host election). Keep stale-lock recovery + `atexit`
  cleanup; only the lock primitive changes. Expose blocking + non-blocking acquire.
- **`process.py`** — `pid_alive` (`psutil.pid_exists`), `terminate`/`kill` (psutil, replaces
  `os.kill SIGTERM/SIGKILL` in `daemon/main.py` `_cmd_stop`), `spawn_detached` (POSIX
  `start_new_session` / Windows `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`), `kill_tree`
  (psutil `children(recursive=True)`; replaces `os.getpgid` + `os.killpg` in `_shutdown_ui`).
  Event-source children (`events/sources/voice.py`, `webcam.py`, `_voice_proc.py`,
  `_webcam_proc.py`) route through the same helpers; register `SIGBREAK` on Windows.
  `daemon/lifecycle.py` keeps its guards; SIGHUP hot-reload is POSIX-only (acceptable).
- Deps: `psutil`, `httpx`, `requests` already present; add `portalocker` (+ `pyttsx3` under
  voice extra). Mark `pyobjc-framework-cocoa` with `; sys_platform == 'darwin'`.

### 0c. asyncio event-loop policy *(hard blocker — Windows Postgres)*
On Windows, `asyncio.run` builds the default **ProactorEventLoop**, but psycopg's
async pool (`AsyncConnectionPool`, `AsyncPostgresSaver`) refuses to run on it
("cannot use the 'ProactorEventLoop'"), so the entire Postgres/pgvector storage
layer is unreachable on a native-Windows daemon. The Selector loop that psycopg
requires is mutually exclusive on Windows with `asyncio.create_subprocess_exec`
(the Proactor loop is the only one that can spawn subprocesses) — which L2/L3
use for PowerShell.

**Resolution (`yuyutsava/aio/run.py`):** install `WindowsSelectorEventLoopPolicy`
once at every process entry (`daemon/main.py`, `cli/cli.py`, `cli/commands/{prefs,attach}.py`),
guarded to `win32` so POSIX is a byte-for-byte passthrough to `asyncio.run`. It is
process-global on purpose so the AsyncSubagentHost's own thread loop (which touches
psycopg via background subagents) is a Selector loop too — safe because
`langgraph_api`/`langgraph_runtime_inmem`/uvicorn spawn no asyncio subprocesses.

**Compensating change (`platform/process.run_capture`):** every daemon subprocess
call site is a *one-shot* `spawn → communicate → return`, which converts to a
blocking `subprocess.run` in a worker thread — loop-agnostic. On Windows
`run_capture` uses that thread; on POSIX it keeps the native asyncio path. Routed
through it: `agents/task_runner/executor.py` (`execute_run`/`execute_python`),
`daemon/resources.py` (`_docker_stats`), `platform/elevation.py` (UAC).

**Known limitation:** *streaming*, long-lived subprocesses still need the Proactor
loop and are therefore unavailable on native Windows for now — the voice
(`events/sources/voice.py`) and webcam (`webcam.py`) event sources (both disabled
in `events_config.json`) and Docker-sandbox mode (`core/docker_sandbox_backend.py`).
Enabling them on Windows is future work (thread + blocking readline, or a dedicated
Proactor helper loop for streaming children).

### 0b. HostProfile — the "OS passport"  *(keystone of the warden design)*
`hostprofile.py`: a dataclass built once at startup and cached.
- OS family / version / arch; canonical shell (+ how to invoke it); detected package
  managers (winget/choco/brew/apt); service manager (SCM/launchd/systemd); elevation
  mechanism (UAC / admin-osascript / pkexec-sudo); key paths (home, temp,
  program-files/applications, `state_dir`); `is_windows`/`is_macos`/`is_linux`.
- Also owns `system_critical_prefixes()`, `temp_zone_prefixes()`, `default_shell()` so L5
  and L3 read platform facts from one place.
- **Injected into every agent system prompt** (orchestrator + task_runner via
  `core/prompts.py` / `agents/base_sub_agent.py`) — the model always knows which system it
  speaks to and which dialect to emit.
- Exposed as **`tr_sysinfo`** so agents/subagents can re-query live details (versions, disk,
  managers) on demand.

---

## L1 — Deterministic OS-invariant tools (pure Python, zero shell)

All routed through the existing `OperationRequest → agent._execute → executor` path so
zone/permission checks are untouched (same pattern as `tr_ls`/`tr_glob`):
- **`tr_grep`** → new `executor.execute_grep()` (`os.walk` + `re`, via `asyncio.to_thread`
  like `execute_glob`); new `OperationType.SEARCH` branch in `agent.py` `_execute`. Honors
  `context_lines`/`case_insensitive`/`max_matches`; skips binaries (NUL sniff), size-caps,
  ignores `.git`/`node_modules` — that pruning, not C, is what makes grep fast.
- **`tr_fetch_url`** → `executor.execute_fetch()` on `httpx` (stream, follow_redirects,
  browser UA, retry ×2). Existing magic-byte / HTML-interstitial verification stays. The
  POSIX `shlex.quote` quoting in both tools disappears entirely.
- **Gap-fillers** so common ops never need a shell: `tr_move`, `tr_copy`, `tr_mkdir`,
  `tr_unzip` (`shutil`/`zipfile`), and **`tr_edit_file`** (surgical old→new replace) —
  needed because built-in `edit_file` is suppressed and full-file rewrites waste tokens.

## L2 — Portable scripting: `tr_run_python`
- Executes a workspace/sandbox script via `create_subprocess_exec([sys.executable, script])`
  — **argv, not a shell string**: zero quoting/escaping on any OS.
- Same executor/permission/timeout plumbing as `tr_execute_in_sandbox`.
- Rewrite the script-lifecycle recipe in the `tr_execute_in_sandbox` docstring and prompts
  from ".sh" to ".py": *write script → tr_run_python → read stdout → delete*. Replaces
  "write a bash script" with one language identical on every OS.

## L3 — Native system channel (first-class warden capability)
- **Shell selection** in `executor.py` `execute_run`: POSIX → `bash -c` (fallback `sh`);
  Windows → **PowerShell** (`pwsh` if present, else
  `powershell.exe -NoProfile -NonInteractive -Command`). Never `cmd.exe` as primary:
  PowerShell IS the Windows admin surface — everything Win+R/`.msc` consoles expose has a
  PowerShell/CLI form (`services.msc`→`Get-Service`/`Restart-Service`, `.msi`→`msiexec /i /qn`,
  events→`Get-WinEvent`, repair→`sfc /scannow`, `DISM`, registry→`reg`/`Get-ItemProperty`).
  The model fluently speaks all of these once the HostProfile passport says where it is.
- **Elevation** — standalone reusable module `yuyutsava/platform/elevation.py`:
  - `ElevationProvider` interface (`run_elevated(command, timeout) -> ShellResult`,
    `is_elevated() -> bool`, `mechanism_name`) with per-OS impls: Windows →
    `Start-Process -Verb RunAs` (UAC), stdout/stderr captured via temp files; macOS →
    `osascript … with administrator privileges`; Linux → `pkexec`/`sudo`.
  - `tr_execute(elevated=True)` is merely the *first consumer* — the module is tool-agnostic
    and importable by any future subsystem (installer flows, self-update, helper service)
    without touching TaskRunner.
  - Classified **CRITICAL**: always a fresh user consent, never auto-granted/cached. Each
    elevated run emits a structured audit record (command, mechanism, outcome) so the
    consent/audit trail is already shaped for a future privileged-helper backend.
- **GUI/native launches** (open a folder, launch an app): model uses the native idiom
  through the same channel (`explorer.exe`/`start` | `open` | `xdg-open`).
- `tr_execute_in_sandbox` gets the same shell selection (sandbox cwd; network policy unchanged).
- Docker mode untouched (container `sh -s` is already OS-invariant by construction).

## L4 — Warden capability packs = platform-tagged skills (data, not code)
How capability grows "for any system without duplication":
- Add a `platforms:` key to skill frontmatter (parser at `skills/registry.py`
  `_parse_frontmatter`); `SkillMeta` carries it; registry / `SkillInjector` / semantic
  recall filter to the current HostProfile OS.
- Ship starter playbooks under `yuyutsava/skills/bundled/`: e.g. `windows/system-triage`
  (Get-WinEvent, sfc, DISM), `windows/service-management`, `windows/software-install`
  (winget/msiexec), `macos/system-triage` (log show, launchctl), `macos/settings`
  (defaults/systemsetup). Each encodes native commands, checks, and rollback notes —
  recalled semantically when the user asks for a "cure".
- **Decision locked: knowledge-driven + skills** — no structured per-domain tool layer.
  L3+L4 covers everything with zero per-OS code; structured wrappers can be added later.

## L5 — Safety spine (the warden's license)
- **Zones per-OS**: `SYSTEM_CRITICAL_PREFIXES` in `agents/task_runner/zones.py` and
  `core/permission_middleware.py` sourced from HostProfile (POSIX `/etc,/usr/bin,…`; Windows
  `C:\Windows`, `C:\Windows\System32`, `C:\Program Files*`, drivers/etc hosts…). Fix the
  temp-zone check in `cli/commands/chat_repl.py` (`/tmp,/var/folders`) →
  `tempfile.gettempdir()` + `state_dir()`.
- **Per-OS dangerous-pattern tables** in permission middleware: keep the POSIX set; add a
  Windows set (`format`, `del /s /q C:\`, `Remove-Item -Recurse -Force C:\`,
  `reg delete HKLM`, `bcdedit`, `vssadmin delete shadows`, `diskpart clean`,
  `Set-ExecutionPolicy Bypass`, `Stop-Service` on critical services, IEX-from-web). Select
  the active table by HostProfile.
- **Consent integration**: routine warden ops flow through the existing `yuyutsava/consent/`
  grants (once/session/project, risk-gated) so the daemon isn't nagging for every step of a
  cure; `elevated`/destructive = CRITICAL = always ask.
- Prompt text mentioning `/tmp` (`core/prompts.py`, `agents/base_sub_agent.py`) → dynamic
  scratch dir.

---

## Also in scope
- **TTS fallback**: `io/tts.py` `tts_from_env` — darwin→`say`, win32→`pyttsx3`/SAPI,
  else Piper; keeps zero-config voice on Windows.
- **Electron packaging**: add `win:` (nsis + portable, x64/arm64) + `.ico` to
  `electron-app/electron-builder.config.js`; main process already `isPosix`-guarded; verify
  `_spawnPath` covers Windows `uv` locations.
- Windows daemon spawn in `daemon/main.py` `_open_electron_when_ready` via
  `platform.process.spawn_detached`.

---

## Phase 2 (deferred): Elevated Helper

Documented now so the L5 pipeline is shaped for it, but **not built** in this effort.

A persistent privileged helper (Windows service / macOS LaunchDaemon, installed once with
admin rights) can later implement the same `ElevationProvider` interface behind hardened
IPC — swapping the *backend*, not the callers. This gives a frictionless "cure it while I
sleep" warden. Requirements before it can ship:
- Code signing (Authenticode on Windows, Developer ID + notarization on macOS).
- A hardened IPC contract (authenticated local socket / named pipe; only vetted, structured
  operations — never arbitrary shell — cross the trust boundary).
- Full audit of every privileged operation (already emitted per-op by the L3 elevation module).
- Install/uninstall + auto-update story for the helper itself.

---

## Sequencing
1. **Step 0** ✅ this doc. *(done)*
2. **L0**: `yuyutsava/platform/` (compat + HostProfile) + deps — zero behavior change on macOS.
3. **L5 wiring**: zones/permission/consent become HostProfile-driven.
4. **L1+L2**: pure-Python `tr_grep`/`tr_fetch_url`/gap-fillers + `tr_run_python` + prompt recipe swap.
5. **L3**: shell selection + elevation module + `tr_sysinfo` + OS-passport prompt injection.
6. **L4**: `platforms:` frontmatter + first Windows/macOS triage playbooks.
7. TTS fallback + Electron win packaging.

---

## Verification
- **CI**: add a `windows-latest` job (fast standalone checks per repo convention, not heavy pytest).
- **Windows daemon lifecycle**: start `--no-ui` → `--status` → `--stop`; lockfile clears, no orphans.
- **Windows tools, no bash present**: `tr_grep` over a temp workspace (line numbers feed
  `tr_read_file`); `tr_fetch_url` (magic-byte check still rejects an HTML interstitial);
  `tr_run_python` script lifecycle; `tr_execute` with a PowerShell one-liner
  (`Get-Service | Select -First 3`); `elevated=True` triggers UAC + CRITICAL consent.
- **Locks**: daemon + CLI chat simultaneously on Windows — migration lock serializes, second
  daemon exits cleanly.
- **Skills**: a `platforms: [windows]` skill is recalled on Windows, absent on macOS.
- **macOS/Linux stay green** throughout (platform layer must not regress POSIX behavior).
- **Electron**: `npx electron-builder --win` → installer boots, spawns daemon via `uv`, UI connects.

---

## Full inventory of OS-specific call sites (reference)

Collected during the code audit. Use as an implementation checklist.

### `fcntl` file locking (POSIX-only — hard block)
| File | Lines | What |
|------|-------|------|
| `yuyutsava/storage/base.py` | 18, 55, 59, 81, 87 | `migration_lock` / `amigration_lock` |
| `yuyutsava/daemon/singleton.py` | 24, 118, 151 | daemon single-instance lock |
| `yuyutsava/async_subagents/host_lock.py` | 35, 152, 193, 217 | async-host election lock |

### Signals & process groups (POSIX-only)
| File | Lines | What |
|------|-------|------|
| `yuyutsava/daemon/main.py` | 178, 206 | `os.kill(pid, SIGTERM/SIGKILL)` in `_cmd_stop` |
| `yuyutsava/daemon/main.py` | 266 | `subprocess.Popen(..., start_new_session=True)` (npm dev) |
| `yuyutsava/daemon/main.py` | 289, 293, 297 | `os.getpgid` / `os.killpg` in `_shutdown_ui` |
| `yuyutsava/daemon/singleton.py` | 49 | `os.kill(pid, 0)` liveness |
| `yuyutsava/async_subagents/host_lock.py` | 59 | `os.kill(pid, 0)` liveness |
| `yuyutsava/daemon/lifecycle.py` | 31, 53/55 | `SIGINT`/`SIGTERM` + `SIGHUP` handlers |
| `yuyutsava/events/sources/voice.py` | 224 | `proc.send_signal(SIGTERM)` |
| `yuyutsava/events/sources/webcam.py` | 214 | `proc.send_signal(SIGTERM)` |
| `yuyutsava/events/sources/_voice_proc.py` | 126 | `signal.signal(SIGTERM, …)` |
| `yuyutsava/events/sources/_webcam_proc.py` | 81 | `signal.signal(SIGTERM, …)` |

### Shell/binary dependencies (bash tools)
| File | Lines | Binary / concern |
|------|-------|------------------|
| `yuyutsava/agents/task_runner/executor.py` | 83 | `asyncio.create_subprocess_shell` (host shell) |
| `yuyutsava/agents/task_runner/tools.py` | 409–411 | `grep -rn` command string → L1 pure Python |
| `yuyutsava/agents/task_runner/tools.py` | 613–616 | `curl -fSL` + POSIX `shlex.quote` → L1 httpx |
| `yuyutsava/io/tts.py` | 54 | `piper` binary (cross-platform if installed) |
| `yuyutsava/io/tts.py` | 150, 165–166 | macOS `say` (darwin-gated) |
| `yuyutsava/visuals/diagrams.py` | 65 | Graphviz `dot` (has Kroki fallback) |
| `yuyutsava/core/docker_sandbox_backend.py` | 166, 206, 252, 338, 412, 424, 464 | `docker` CLI + `sh -s` (Linux container — safe) |

### Hardcoded POSIX paths
| File | Lines | Path(s) |
|------|-------|---------|
| `yuyutsava/agents/task_runner/zones.py` | 19–29 | `/etc,/sys,/proc,/dev,/boot,/root,/usr/bin,/usr/sbin,/var/log` |
| `yuyutsava/core/permission_middleware.py` | 38–69, 116–119 | `/etc,/usr,/bin,/sbin,/lib` regexes + prefixes |
| `yuyutsava/cli/commands/chat_repl.py` | 586 | `/tmp,/var/folders,/private/tmp` zone check |
| `yuyutsava/core/prompts.py` | 80 | `/tmp` in prompt text |
| `yuyutsava/agents/base_sub_agent.py` | 137 | `/tmp` in prompt text |

### Already Windows-safe (guards in place — no change)
- Electron `daemon.js` (`isPosix` gates detached + signals), `tray.js`, `notifications.js` `win32` branches.
- `events/sources/appfocus.py` (darwin-gated NSWorkspace), `io/tts.py` `say` (darwin-gated).
- `sounddevice` / `soundfile` / `onnxruntime` / `openwakeword` / `webrtcvad-wheels` — all ship Windows wheels.
- `os.dup/os.dup2/os.devnull`, `tempfile.*`, `Path.home()/state_dir()`, `socket.AF_INET` port-picking.
