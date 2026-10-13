#!/usr/bin/env python
"""Install YUYUTSAVA from this checkout: the one way in, for the desktop app and the terminal.

Everything this prints stays ASCII on purpose. A Windows console runs cp1252 or
cp437, and an em-dash arrives there as a replacement glyph -- an installer that
renders as mojibake on the machine it is trying to rescue is not much of a guide.

    python scripts/install.py              # both: the desktop app and the terminal command
    python scripts/install.py --ui         # the desktop app only
    python scripts/install.py --cli        # the terminal command only
    python scripts/install.py --extra dev  # plus the test tools
    python scripts/install.py --lean       # no optional features
    python scripts/install.py --doctor     # check this machine, install nothing
    python scripts/install.py --dry-run    # print the commands only

Run it again after pulling: a changed lock is picked up the same way. Both
installs come from ``uv.lock``, with the same features:

* ``--ui`` — **the checkout's ``.venv``**, which the desktop app runs its daemon
  in (``uv run``). Synced frozen (never re-resolved) and *inexact*: nothing
  already installed is ever taken away. A plain ``uv sync`` is exact, and
  uninstalls every optional feature its command line does not name — voice,
  vision, memory — so it is never the way to install.
* ``--cli`` — **the ``yuyutsava`` command on your PATH**: the lock exported as
  constraints and the checkout installed under them, editable, so the command
  runs this checkout's code on exactly the locked dependencies. ``pipx install
  -e .`` or a bare ``uv tool install -e .`` resolve the newest versions their
  ranges allow, which is how a daemon came to run mcp 2.2.0 against a lock of
  1.26.0 and every MCP server failed (R-BUG-28).

Both are pinned to the interpreter in ``.python-version`` (see
``python_version``) — ``uv sync`` reads that file itself, but a uv *tool*
environment is not project-scoped, so without ``--python`` the terminal install
lands on whatever newest interpreter the machine happens to have.

**When the machine is the problem.** Most installs that fail here fail for a
reason that has nothing to do with this project: no uv, a checkout inside a
cloud-synced folder, long paths off, a full disk, a cache a sync filter has
taken ownership of. Nobody should have to diagnose their own machine, so:

* ``preflight`` runs the cheap probes first and reports each one as a problem
  *and* a fix. A fatal one exits **2** before any work is done.
* ``link_mode`` removes the biggest Windows failure class outright.
* ``diagnose`` reads what uv actually printed and translates it. Where there is
  a safe repair, ``--heal`` decides what happens: ``ask`` (the default — says
  exactly what it would run and waits for you), ``auto`` (no prompt, for CI and
  for a GUI that has already asked), ``never`` (exit **3** and print
  ``heal: <command>`` for the caller to confirm on its own).

This script runs *before* the package is installed, so it is stdlib-only and
cannot import from ``yuyutsava/``. By default both installs get every feature
the product has (``PRODUCT_EXTRAS``). The daemon warns at start-up when an
install has drifted from the lock.

If an earlier terminal install came from pipx, remove it first (``pipx
uninstall yuyutsava``): both put a ``yuyutsava`` executable in the same bin
directory. Needs uv.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Every feature a user of the product has: each model provider's SDK, voice, the
#: webcam source, semantic memory on Postgres, charts. ``dev`` (tests) and
#: ``streamlit`` are for working on YUYUTSAVA itself; add them with ``--extra``.
PRODUCT_EXTRAS = ("all-providers", "voice", "vision", "memory", "visuals")

#: Oldest uv that understands every flag used here (``--link-mode`` on
#: ``tool install``, ``cache clean <package>``).
MIN_UV = (0, 5, 0)

#: Free space the full feature set needs, with room for uv's cache to grow.
MIN_FREE_GB = 3.0

#: Lines of a child's output kept back for ``diagnose`` — uv's errors, and the
#: context above them, are always near the end.
TAIL_LINES = 80

#: How many times one run may repair the machine before it gives up and says so.
MAX_HEALS = 3

#: Seconds ``--doctor`` may spend sampling uv's cache; it holds tens of
#: thousands of files and a full walk is not worth the wait.
CACHE_SCAN_SECONDS = 3.0

#: Directory names that mean a cloud filter owns the tree. A ``.venv`` under one
#: of these is the root cause of the hardlink failures below: the filter takes
#: ownership of every file it syncs, and a hardlink shares one inode, so uv's
#: *cache* becomes cloud-owned too and Windows then refuses to link from it.
CLOUD_FOLDERS = (
    "onedrive",
    "dropbox",
    "google drive",
    "my drive",
    "icloud drive",
    "creative cloud files",
)


@dataclass(frozen=True)
class Issue:
    """One thing about this machine that will, or might, break the install."""

    #: What is wrong, in the user's terms — never uv's.
    problem: str
    #: What to do about :attr:`problem`. Never empty.
    fix: str
    #: True when the install cannot proceed at all.
    fatal: bool = False


@dataclass(frozen=True)
class Remedy:
    """What a failed uv run means, and what would make a retry work."""

    problem: str
    fix: str
    #: A command that repairs the condition, if one is safe to offer.
    heal: tuple[str, ...] = ()
    #: One line saying what running :attr:`heal` costs, shown before asking.
    cost: str = ""


def python_version() -> str:
    """The interpreter both installs use: ``.python-version``, the one source of truth.

    ``uv sync`` reads that file on its own; ``uv tool install`` does not — a tool
    environment is not project-scoped, so without ``--python`` uv takes the
    newest interpreter on the machine. On a box whose newest is 3.13 that makes
    the install *unresolvable*: the ``voice`` extra pins ``onnxruntime<1.20``
    (openwakeword's pretrained models score ~0.0 under >=1.20) and 1.19.2 ships
    no cp313 wheel. Passing it explicitly also rebuilds a ``.venv`` that an
    earlier install put on another version; uv fetches the pinned interpreter if
    the machine hasn't got it.
    """
    for line in (REPO / ".python-version").read_text(encoding="utf-8").splitlines():
        if (version := line.strip()) and not version.startswith("#"):
            return version
    raise SystemExit(".python-version is empty: it is the interpreter both installs use")


def link_mode() -> list[str]:
    """Copy out of uv's cache on Windows instead of hardlinking into it.

    uv's default is a hardlink, which is faster and saves disk. But a hardlink
    shares an inode with the cache, and once any cloud-sync filter has taken
    ownership of one end — an earlier install linked into a OneDrive folder —
    Windows refuses the link from either end, with ``os error 396``. The copy
    costs disk and a little time; it removes the whole failure class. macOS and
    Linux keep uv's default: APFS clone and reflink are both safe. An operator
    who has set ``UV_LINK_MODE`` has already chosen, so leave them alone.
    """
    if sys.platform != "win32" or os.environ.get("UV_LINK_MODE"):
        return []
    return ["--link-mode", "copy"]


def ui_commands(extras: list[str]) -> list[list[str]]:
    """The desktop app's ``.venv``: the lock, frozen and inexact."""
    return [
        [
            "uv",
            "sync",
            "--frozen",
            "--inexact",
            "--python",
            python_version(),
            *link_mode(),
            *_flags(extras),
        ]
    ]


def cli_commands(extras: list[str], constraints: Path) -> list[list[str]]:
    """The terminal command: export the lock, install the checkout under it."""
    target = f".[{','.join(extras)}]" if extras else "."
    python = python_version()
    return [
        [
            "uv",
            "export",
            "--frozen",
            "--no-hashes",
            "--no-emit-project",
            "--no-dev",
            # The same interpreter the tool environment gets, so the constraints'
            # environment markers are evaluated for the environment they constrain.
            # No --link-mode: an export installs nothing.
            "--python",
            python,
            *_flags(extras),
            "--output-file",
            str(constraints),
        ],
        [
            "uv",
            "tool",
            "install",
            "--force",
            "--editable",
            target,
            "--python",
            python,
            *link_mode(),
            "--constraints",
            str(constraints),
        ],
    ]


def _flags(extras: list[str]) -> list[str]:
    return [flag for extra in extras for flag in ("--extra", extra)]


# --- Probing the machine ---------------------------------------------------


def preflight() -> list[Issue]:
    """Everything about this machine that will, or might, break the install.

    Collects; never raises. A probe that cannot answer — no registry key, a
    drive that will not report its size — tells us nothing, which is not the
    same as a problem, so it is skipped. Cheap enough to run every install.
    """
    issues: list[Issue] = []
    probes = (
        _uv_installed,
        _uv_recent_enough,
        _checkout_outside_cloud_sync,
        _long_paths_enabled,
        _enough_disk,
    )
    for probe in probes:
        try:
            issue = probe()
        except Exception:  # noqa: BLE001 — a probe that cannot answer must not stop the install
            continue
        if issue is not None:
            issues.append(issue)
    return issues


def _uv_installed() -> Issue | None:
    if shutil.which("uv"):
        return None
    install = (
        'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
        if sys.platform == "win32"
        else "curl -LsSf https://astral.sh/uv/install.sh | sh"
    )
    return Issue(
        problem="uv is not installed, or is not on PATH",
        fix=f"install it, then open a new terminal: {install}",
        fatal=True,
    )


def _uv_recent_enough() -> Issue | None:
    found = _uv_version()
    if found is None or found >= MIN_UV:
        return None
    return Issue(
        problem=f"uv {'.'.join(str(p) for p in found)} is older than this install needs",
        fix=f"update it: uv self update (need {'.'.join(str(p) for p in MIN_UV)} or newer)",
    )


def _checkout_outside_cloud_sync() -> Issue | None:
    folder = cloud_folder(REPO)
    if folder is None:
        return None
    return Issue(
        problem=f"this checkout is inside {folder}, which a cloud-sync app keeps in sync",
        fix=(
            "move it to a path that is not synced, or exclude .venv from sync: "
            "sync turns installed files into cloud placeholders, which breaks uv's "
            "cache for every project on this machine (os error 396)"
        ),
    )


def _long_paths_enabled() -> Issue | None:
    if sys.platform != "win32":
        return None
    enabled = _registry_dword(
        r"HKLM\SYSTEM\CurrentControlSet\Control\FileSystem", "LongPathsEnabled"
    )
    if enabled is None or enabled:
        return None
    return Issue(
        problem="Windows long-path support is off, and paths inside .venv run deep",
        fix=(
            "turn it on in an admin terminal, then reboot: reg add "
            r'"HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" '
            "/v LongPathsEnabled /t REG_DWORD /d 1 /f"
        ),
    )


def _enough_disk() -> Issue | None:
    free_gb = shutil.disk_usage(REPO).free / 1024**3
    if free_gb >= MIN_FREE_GB:
        return None
    return Issue(
        problem=f"{free_gb:.1f}GB free on this drive, below the {MIN_FREE_GB:.0f}GB an install needs",
        fix="free up space, or run `uv cache prune` to drop unreachable cache entries",
    )


def cloud_folder(path: Path) -> str | None:
    """The cloud-synced folder ``path`` sits inside, named, or None.

    Both halves matter: OneDrive exports its root in the environment (and
    redirects Desktop/Documents there by default on consumer Windows), while
    Dropbox and Google Drive only show up as a directory name in the path.
    """
    try:
        resolved = path.resolve()
    except OSError:
        return None
    for key in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial", "Dropbox"):
        root = os.environ.get(key, "").strip()
        if not root:
            continue
        try:
            resolved.relative_to(Path(root).resolve())
        except (OSError, ValueError):
            continue
        return root
    for part in resolved.parts:
        if part.strip().lower() in CLOUD_FOLDERS:
            return part
    return None


def _uv_version() -> tuple[int, ...] | None:
    """``uv --version`` as a tuple, or None when uv cannot be asked."""
    out = _ask(["uv", "--version"])
    if out is None:
        return None
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(part) for part in found.groups()) if found else None


def _registry_dword(key: str, name: str) -> int | None:
    """One REG_DWORD, through ``reg query``.

    The house way of asking Windows a question is its CLI, not an API binding
    (see ``yuyutsava/platform/files.py``, which shells out to icacls) — and it
    keeps this script importable on every OS.
    """
    out = _ask(["reg", "query", key, "/v", name])
    if out is None:
        return None
    found = re.search(rf"{re.escape(name)}\s+REG_DWORD\s+0x([0-9a-fA-F]+)", out)
    return int(found.group(1), 16) if found else None


def _ask(cmd: list[str]) -> str | None:
    """Run a short read-only command for its stdout; None if it cannot run."""
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


# --- Running, and reading what went wrong ----------------------------------


def _run(cmd: list[str]) -> tuple[int, str]:
    """Run ``cmd``, streaming its output, keeping the tail for :func:`diagnose`.

    The pipe costs uv its progress bars — it renders plain lines when its output
    is not a terminal — and buys the only thing that makes a failure explainable
    afterwards: what it actually said.
    """
    print("$", shlex.join(cmd))
    tail: deque[str] = deque(maxlen=TAIL_LINES)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        print(f"could not run {cmd[0]}: {exc}")
        return 127, str(exc)
    with proc:
        for line in proc.stdout or ():
            print(line, end="")
            tail.append(line)
    return proc.returncode, "".join(tail)


def diagnose(output: str) -> Remedy | None:
    """What a failed uv run means for this machine, in the user's terms.

    Keyed on what uv prints, because that is the only evidence there is by the
    time it has failed. None means "this is not a host problem" — the error is
    uv's own and its message already stands on its own.
    """
    low = output.lower()
    if any(
        sign in low for sign in ("incompatible hardlinks", "os error 396", "failed to hardlink")
    ):
        return _cloud_cache_remedy(output)
    if any(sign in low for sign in ("os error 5", "access is denied", "used by another process")):
        return Remedy(
            problem="a file being installed is locked by another program",
            fix=(
                "stop the daemon (`yuyutsava daemon --stop`), close the desktop app, "
                "and if it persists let your antivirus finish scanning, then run this again"
            ),
        )
    if "matching python abi tag" in low:
        return Remedy(
            problem="the interpreter does not match what the lock needs",
            fix=(
                f".python-version pins {python_version()} for both installs; a dependency here "
                "ships no wheel for it, so either that pin or the dependency has to move"
            ),
        )
    if any(sign in low for sign in ("no space left", "os error 112")):
        return Remedy(
            problem="the drive filled up during the install",
            fix="free up space, then run this again; `uv cache prune` reclaims the most",
        )
    if any(sign in low for sign in ("failed to fetch", "error sending request", "certificate")):
        return Remedy(
            problem="the package index could not be reached",
            fix=(
                "check the connection, and on a corporate network set HTTPS_PROXY and "
                "SSL_CERT_FILE for your proxy's certificate, then run this again"
            ),
        )
    if any(sign in low for sign in ("lockfile is out of date", "does not match the lockfile")):
        return Remedy(
            problem="uv.lock does not match pyproject.toml",
            fix="run `uv lock` and commit the result; the lock is what both installs come from",
        )
    return None


def _cloud_cache_remedy(output: str) -> Remedy:
    package = _failed_package(output)
    permanent = (
        "keep .venv folders out of cloud-synced directories, and set UV_LINK_MODE=copy "
        "so uv copies out of its cache instead of linking into it"
    )
    if package is None:
        return Remedy(
            problem="a cloud-sync app owns files inside uv's cache, so they cannot be linked",
            fix=f"clear the cache with `uv cache clean`, then run this again. Longer term: {permanent}",
        )
    return Remedy(
        problem=f"a cloud-sync app owns uv's cached copy of {package}, so it cannot be linked",
        fix=f"drop that cache entry and let uv fetch it again. Longer term: {permanent}",
        heal=("uv", "cache", "clean", package),
        cost=f"this only drops the cached copy of {package}; uv downloads it again",
    )


def _failed_package(output: str) -> str | None:
    """The package uv named in ``Failed to install: … (name==version)``."""
    found = re.search(r"Failed to install:[^\n]*?\(([A-Za-z0-9._-]+)==", output)
    return found.group(1) if found else None


# --- Reporting -------------------------------------------------------------


def _say(problem: str, fix: str) -> None:
    """One problem and its remedy, the way the rest of the product says it."""
    print(f"!  {problem}")
    print(f"   fix: {fix}")


def _agreed(remedy: Remedy) -> bool:
    """Say exactly what would run, and let the user decide."""
    print(f"   I can run:  {shlex.join(remedy.heal)}")
    if remedy.cost:
        print(f"   {remedy.cost}")
    if not sys.stdin.isatty():
        print("   nothing was changed; re-run with --heal=auto to allow it")
        return False
    try:
        answer = input("   run it and retry? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


def _install(steps: list[list[str]], heal: str) -> int:
    """Run the steps in order, repairing a known host problem if allowed to."""
    healed = 0
    index = 0
    while index < len(steps):
        code, tail = _run(steps[index])
        if code == 0:
            index += 1
            continue
        remedy = diagnose(tail)
        if remedy is None:
            return code
        print()
        _say(remedy.problem, remedy.fix)
        if not remedy.heal:
            return code
        if heal == "never":
            # The caller asks on our behalf, then re-runs with --heal=auto.
            print(f"heal: {shlex.join(remedy.heal)}")
            return 3
        if healed >= MAX_HEALS:
            print(f"   repaired {healed} times already this run; stopping instead of looping")
            return code
        if heal == "ask" and not _agreed(remedy):
            return code
        healed += 1
        heal_code, _ = _run(list(remedy.heal))
        if heal_code != 0:
            return heal_code
    return 0


def doctor() -> int:
    """What this machine looks like to the installer, and what would go wrong.

    Exits non-zero only for something *fatal* — the question it answers is "can
    this machine install?", and a warning means yes, with a caveat. That makes
    it something CI can run as an assertion on all three platforms rather than a
    report nobody reads.
    """
    found = _uv_version()
    cache_line, cache_issue = _cache_report()
    print(f"checkout   : {REPO}")
    print(f"running    : python {sys.version.split()[0]} ({sys.platform})")
    print(f"pinned     : python {python_version()}  (.python-version)")
    print(f"uv         : {'.'.join(str(p) for p in found) if found else 'not found'}")
    print(f"link mode  : {' '.join(link_mode()[1:]) or 'uv default'}")
    print(f"cache      : {cache_line}")
    print()

    issues = preflight()
    if cache_issue is not None:
        issues.append(cache_issue)
    if not issues:
        print("no problems found")
        return 0
    for issue in issues:
        _say(issue.problem, issue.fix)
    fatal = sum(1 for issue in issues if issue.fatal)
    print()
    print(f"{len(issues)} problem{'s' if len(issues) != 1 else ''}, {fatal} of them fatal")
    return 1 if fatal else 0


def _cache_report() -> tuple[str, Issue | None]:
    """uv's cache, and whether a cloud filter has taken any of it over.

    Only ``--doctor`` pays for this: the cache holds tens of thousands of files,
    so the walk is sampled against a clock rather than completed.
    """
    out = _ask(["uv", "cache", "dir"])
    if out is None or not out.strip():
        return "not found (is uv installed?)", None
    cache = Path(out.strip())
    if sys.platform != "win32":
        return str(cache), None
    owned, seen = _cloud_placeholders(cache)
    if not seen:
        return f"{cache} (empty)", None
    if not owned:
        return f"{cache} ({seen} files sampled, none cloud-owned)", None
    return (
        f"{cache} ({owned} of {seen} sampled files are cloud-owned)",
        Issue(
            problem=(
                f"a cloud-sync app owns files inside uv's cache ({owned} of {seen} sampled): "
                "an install that hardlinks out of it will fail with os error 396"
            ),
            fix=(
                "installs from here copy instead of linking, so this is survivable; to clear it, "
                "`uv cache clean` and keep .venv folders out of synced directories"
            ),
        ),
    )


def _cloud_placeholders(root: Path, budget: float = CACHE_SCAN_SECONDS) -> tuple[int, int]:
    """``(cloud-owned, files seen)`` under ``root``, giving up after ``budget`` seconds."""
    seen = owned = 0
    deadline = time.monotonic() + budget
    stack = [root]
    while stack and time.monotonic() < deadline:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
                continue
            seen += 1
            try:
                attributes = entry.stat(follow_symlinks=False).st_file_attributes
            except (AttributeError, OSError):
                continue
            if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                owned += 1
    return owned, seen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ui", action="store_true", help="install the desktop app's environment")
    parser.add_argument("--cli", action="store_true", help="install the terminal command")
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        help="another optional-dependency group to include (repeatable), e.g. dev",
    )
    parser.add_argument(
        "--lean", action="store_true", help="none of the optional features, only what --extra names"
    )
    parser.add_argument(
        "--doctor", action="store_true", help="report on this machine and install nothing"
    )
    parser.add_argument(
        "--heal",
        choices=("ask", "auto", "never"),
        default="ask",
        help="repair a known machine problem and retry: ask (default), auto, or never (exit 3)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the commands only")
    args = parser.parse_args(argv)
    if args.doctor:
        return doctor()
    extras = list(dict.fromkeys([*(() if args.lean else PRODUCT_EXTRAS), *args.extra]))
    both = not (args.ui or args.cli)

    with tempfile.TemporaryDirectory() as tmp:
        steps = []
        if args.ui or both:
            steps += ui_commands(extras)
        if args.cli or both:
            steps += cli_commands(extras, Path(tmp) / "constraints.txt")
        if args.dry_run:
            # The commands, and nothing else: no probing, no machine state.
            for cmd in steps:
                print("$", shlex.join(cmd))
            return 0
        issues = preflight()
        for issue in issues:
            _say(issue.problem, issue.fix)
        if any(issue.fatal for issue in issues):
            return 2
        if issues:
            print()
        return _install(steps, args.heal)


if __name__ == "__main__":
    sys.exit(main())
