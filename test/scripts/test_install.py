"""``scripts/install.py`` is the one way to install, for the desktop app and the terminal.

What must hold:

* the app's ``.venv`` is synced from the lock, frozen and **inexact** — a plain
  ``uv sync`` uninstalls every optional feature it does not name;
* the terminal command is installed editable under the lock exported as
  constraints, never re-resolved (R-BUG-28);
* both get the product's features by default; ``--ui`` / ``--cli`` pick one;
* both are pinned to the interpreter in ``.python-version`` — uv's own default
  is the newest interpreter on the machine, which the ``voice`` extra cannot
  resolve on 3.13+ (``onnxruntime<1.20`` has no cp313 wheel);
* Windows copies out of uv's cache instead of hardlinking into it;
* a failure caused by the *machine* is reported as a problem and a fix, and
  nothing is repaired without the user agreeing to it;
* everything the installer prints is ASCII, so a cp1252 console can render it;
* no doc tells anyone to run a bare ``uv sync``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from test.support.paths import REPO

_spec = importlib.util.spec_from_file_location("install", REPO / "scripts" / "install.py")
install = importlib.util.module_from_spec(_spec)
# Register before executing: @dataclass resolves a field's type through
# sys.modules[cls.__module__], which is None for a module loaded only by path.
sys.modules[_spec.name] = install
_spec.loader.exec_module(install)


# The two installs this project has actually lost on a user's Windows machine,
# quoted from the terminal. Every diagnosis is tested against the real thing.
HARDLINK_FAILURE = r"""
warning: Failed to hardlink files; falling back to full copy. This may lead to degraded performance.
error: Failed to install: jsonpatch-1.33-py2.py3-none-any.whl (jsonpatch==1.33)
  Caused by: failed to hardlink file from C:\Users\abhis\AppData\Local\uv\cache\archive-v0\juipZRM\jsonpatch-1.33.dist-info\AUTHORS to C:\Users\abhis\AppData\Roaming\uv\tools\yuyutsava\Lib\site-packages\jsonpatch-1.33.dist-info\AUTHORS: The cloud operation cannot be performed on a file with incompatible hardlinks. (os error 396)
"""

ABI_FAILURE = """
  x No solution found when resolving dependencies:
  `-> Because onnxruntime==1.19.2 has no wheels with a matching Python ABI tag (e.g., `cp313`)
      and yuyutsava[voice]==0.1.0 depends on onnxruntime==1.19.2, we can conclude that
      yuyutsava[voice]==0.1.0 cannot be used.
"""


def _pyproject() -> str:
    return (REPO / "pyproject.toml").read_text(encoding="utf-8")


def _parts(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def _printed(*argv: str) -> list[str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        install.main([*argv, "--dry-run"])
    return [line for line in out.getvalue().splitlines() if line.startswith("$ ")]


class TheUi(unittest.TestCase):
    def test_syncs_the_apps_venv_frozen_and_never_strips_it(self) -> None:
        [sync] = install.ui_commands(["voice", "memory"])
        self.assertEqual(sync[:2], ["uv", "sync"])
        self.assertIn("--frozen", sync)
        self.assertIn("--inexact", sync, "an exact sync uninstalls every feature not named")
        extras = [sync[i + 1] for i, a in enumerate(sync) if a == "--extra"]
        self.assertEqual(extras, ["voice", "memory"])


class TheCli(unittest.TestCase):
    def test_exports_the_lock_frozen_and_installs_under_it(self) -> None:
        # str(), not the literal: a Path renders with the OS's own separator.
        constraints = Path("/t/c.txt")
        export, tool = install.cli_commands(["vertex"], constraints)
        self.assertEqual(export[:2], ["uv", "export"])
        self.assertIn("--frozen", export)
        self.assertEqual(export[export.index("--extra") + 1], "vertex")
        self.assertEqual(export[export.index("--output-file") + 1], str(constraints))
        self.assertEqual(tool[:3], ["uv", "tool", "install"])
        self.assertIn("--editable", tool)
        self.assertIn(".[vertex]", tool)
        self.assertEqual(tool[tool.index("--constraints") + 1], str(constraints))

    def test_no_extras_installs_the_base_package(self) -> None:
        export, tool = install.cli_commands([], Path("/t/c.txt"))
        self.assertNotIn("--extra", export)
        self.assertIn(".", tool)


class TheInterpreter(unittest.TestCase):
    def test_both_installs_pin_the_projects_interpreter(self) -> None:
        pinned = install.python_version()
        [sync] = install.ui_commands(["voice"])
        export, tool = install.cli_commands(["voice"], Path("/t/c.txt"))
        for cmd in (sync, export, tool):
            with self.subTest(cmd=cmd[1]):
                self.assertIn(
                    "--python",
                    cmd,
                    "uv's default is the newest interpreter on the machine; the voice "
                    "extra pins onnxruntime<1.20, which has no cp313 wheel",
                )
                self.assertEqual(cmd[cmd.index("--python") + 1], pinned)

    def test_the_pin_comes_from_python_version_and_uv_can_request_it(self) -> None:
        pinned = install.python_version()
        self.assertEqual(pinned, (REPO / ".python-version").read_text(encoding="utf-8").strip())
        self.assertRegex(pinned, r"^\d+\.\d+(\.\d+)?$")
        floor = re.search(r'requires-python\s*=\s*">=(\d+\.\d+)', _pyproject()).group(1)
        self.assertGreaterEqual(_parts(pinned)[:2], _parts(floor), "the pin must satisfy the floor")


class TheLinkMode(unittest.TestCase):
    """Windows copies out of uv's cache; a hardlink into it is what broke (os error 396)."""

    def test_windows_copies_and_other_platforms_keep_uvs_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UV_LINK_MODE", None)
            with patch.object(install.sys, "platform", "win32"):
                self.assertEqual(install.link_mode(), ["--link-mode", "copy"])
            with patch.object(install.sys, "platform", "darwin"):
                self.assertEqual(install.link_mode(), [], "APFS clone is safe; don't pay the copy")

    def test_an_operator_who_set_uv_link_mode_has_already_chosen(self) -> None:
        with (
            patch.dict(os.environ, {"UV_LINK_MODE": "hardlink"}),
            patch.object(install.sys, "platform", "win32"),
        ):
            self.assertEqual(install.link_mode(), [])

    def test_it_reaches_both_installs_but_never_the_export(self) -> None:
        mode = install.link_mode()
        [sync] = install.ui_commands(["voice"])
        export, tool = install.cli_commands(["voice"], Path("/t/c.txt"))
        for cmd in (sync, tool):
            with self.subTest(cmd=cmd[1]):
                self.assertEqual([a for a in cmd if a in mode], mode)
        self.assertNotIn("--link-mode", export, "an export installs nothing")


class TheDiagnosis(unittest.TestCase):
    """What uv printed, translated into something the person at the keyboard can act on."""

    def test_the_hardlink_failure_names_the_package_to_drop(self) -> None:
        remedy = install.diagnose(HARDLINK_FAILURE)
        self.assertIsNotNone(remedy)
        self.assertIn("cloud", remedy.problem)
        self.assertEqual(remedy.heal, ("uv", "cache", "clean", "jsonpatch"))
        self.assertTrue(remedy.cost, "a repair must say what it costs before it is offered")

    def test_a_hardlink_failure_without_a_named_package_offers_no_repair(self) -> None:
        remedy = install.diagnose("failed to hardlink file from A to B (os error 396)")
        self.assertIsNotNone(remedy)
        self.assertEqual(remedy.heal, (), "never clear the whole cache on the user's behalf")
        self.assertIn("uv cache clean", remedy.fix)

    def test_the_abi_failure_points_at_the_interpreter_pin(self) -> None:
        remedy = install.diagnose(ABI_FAILURE)
        self.assertIsNotNone(remedy)
        self.assertIn(".python-version", remedy.fix)

    def test_uvs_own_errors_are_left_to_speak_for_themselves(self) -> None:
        self.assertIsNone(install.diagnose("error: Failed to parse `pyproject.toml`"))

    def test_every_remedy_carries_a_fix(self) -> None:
        samples = (
            HARDLINK_FAILURE,
            ABI_FAILURE,
            "error: Access is denied. (os error 5)",
            "error: No space left on device (os error 112)",
            "error: failed to fetch: https://pypi.org/simple/anyio/",
            "error: The lockfile is out of date",
        )
        for sample in samples:
            with self.subTest(sample=sample.strip().splitlines()[0][:40]):
                remedy = install.diagnose(sample)
                self.assertIsNotNone(remedy, "a known host failure must be recognised")
                self.assertTrue(remedy.problem and remedy.fix)


class ThePreflight(unittest.TestCase):
    """Probe the machine before doing any work, and never raise while doing it."""

    def test_every_issue_states_a_problem_and_a_fix(self) -> None:
        for issue in install.preflight():
            with self.subTest(problem=issue.problem):
                self.assertTrue(issue.problem and issue.fix)

    def test_a_missing_uv_is_fatal_and_says_how_to_get_it(self) -> None:
        with patch.dict(os.environ, {"PATH": ""}):
            issue = install._uv_installed()
        self.assertIsNotNone(issue, "nothing here works without uv")
        self.assertTrue(issue.fatal)
        self.assertIn("astral.sh", issue.fix)

    def test_a_probe_that_explodes_is_skipped_not_fatal(self) -> None:
        def boom() -> install.Issue | None:
            raise OSError("the drive went away mid-probe")

        with patch.object(install, "_enough_disk", boom):
            install.preflight()  # must not raise

    def test_it_recognises_a_checkout_inside_a_cloud_folder(self) -> None:
        synced = Path.home() / "OneDrive" / "Desktop" / "project"
        self.assertIsNotNone(install.cloud_folder(synced))
        with patch.dict(os.environ, {"Dropbox": str(Path.home())}):
            self.assertIsNotNone(install.cloud_folder(Path.home() / "code"))

    def test_an_ordinary_checkout_is_not_flagged(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(install.cloud_folder(REPO))


class _Tty(io.StringIO):
    """A stdin that claims to be a terminal, so the prompt path is reachable in a test."""

    def isatty(self) -> bool:
        return True


class TheRepair(unittest.TestCase):
    """Nothing on the user's machine is changed without them saying so."""

    def _failing_step(self, output: str) -> list[list[str]]:
        # A real child that fails the way uv failed -- no mock stands in for it.
        return [[sys.executable, "-c", f"print({output!r}); raise SystemExit(1)"]]

    def test_heal_never_exits_3_and_names_the_command_for_its_caller(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = install._install(self._failing_step(HARDLINK_FAILURE), "never")
        self.assertEqual(code, 3, "3 means: a repair exists, ask the user yourself")
        self.assertIn("heal: uv cache clean jsonpatch", out.getvalue())

    def test_a_failure_it_cannot_explain_returns_the_childs_own_code(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = install._install(self._failing_step("error: something else entirely"), "never")
        self.assertEqual(code, 1)

    def test_without_a_terminal_it_declines_rather_than_assuming_consent(self) -> None:
        remedy = install.diagnose(HARDLINK_FAILURE)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(install.sys, "stdin", io.StringIO()):
            self.assertFalse(install._agreed(remedy))
        self.assertIn("uv cache clean jsonpatch", out.getvalue(), "say what it would have run")

    def test_a_terminal_that_says_yes_is_obeyed(self) -> None:
        remedy = install.diagnose(HARDLINK_FAILURE)
        with (
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(install.sys, "stdin", _Tty()),
            patch.object(install, "input", lambda _: "y", create=True),
        ):
            self.assertTrue(install._agreed(remedy))


class WhatItPrints(unittest.TestCase):
    def test_everything_is_ascii_so_a_cp1252_console_can_render_it(self) -> None:
        spoken = [install.main.__doc__ or "", install.__doc__.split("\n\n")[0]]
        for issue in install.preflight():
            spoken += [issue.problem, issue.fix]
        for sample in (HARDLINK_FAILURE, ABI_FAILURE, "error: Access is denied. (os error 5)"):
            remedy = install.diagnose(sample)
            spoken += [remedy.problem, remedy.fix, remedy.cost]
        for text in spoken:
            with self.subTest(text=text[:50]):
                self.assertTrue(text.isascii(), "a Windows console renders this as mojibake")

    def test_doctor_reports_without_installing_anything(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = install.doctor()
        printed = out.getvalue()
        self.assertNotIn("$ uv", printed, "doctor diagnoses; it does not install")
        self.assertEqual(code, 0, "a warning is not a failure: this machine can still install")
        for label in ("checkout", "pinned", "uv", "link mode", "cache"):
            self.assertIn(label, printed)

    def test_doctor_fails_only_when_something_is_fatal(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()), patch.dict(os.environ, {"PATH": ""}):
            self.assertEqual(install.doctor(), 1, "no uv means no install, on any machine")


class WhatRuns(unittest.TestCase):
    def test_no_flag_installs_both_the_app_first(self) -> None:
        steps = _printed()
        self.assertTrue(steps[0].startswith("$ uv sync"))
        self.assertTrue(any(s.startswith("$ uv tool install") for s in steps))

    def test_ui_and_cli_each_install_only_their_own(self) -> None:
        self.assertEqual([s.split()[2] for s in _printed("--ui")], ["sync"])
        self.assertEqual([s.split()[2] for s in _printed("--cli")], ["export", "tool"])

    def test_the_products_features_by_default_lean_none_and_extra_adds(self) -> None:
        default = _printed("--ui")[0]
        for extra in install.PRODUCT_EXTRAS:
            self.assertIn(f"--extra {extra}", default)
        self.assertNotIn("--extra", _printed("--ui", "--lean")[0])
        self.assertIn("--extra dev", _printed("--ui", "--extra", "dev")[0])


class TheDocs(unittest.TestCase):
    def test_never_tell_anyone_to_run_a_bare_uv_sync(self) -> None:
        bare = re.compile(r"^\s*uv sync\b", re.M)  # a command line to copy; prose may warn
        for doc in ("README.md", "CONTRIBUTING.md"):
            with self.subTest(doc=doc):
                self.assertIsNone(bare.search((REPO / doc).read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
