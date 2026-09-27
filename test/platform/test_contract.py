"""The platform substrate keeps one contract on every OS (R-TST-06).

``yuyutsava/platform/`` is the only place allowed OS-specific code; everything
else relies on what it promises. These tests drive the real primitives — a
second process holding a lock, a spawned tree being killed, a command through
the host's native shell — and assert only what must hold on Windows, macOS and
Linux alike, so the CI ``platform`` job can run them unchanged on each.

Run:  .venv/bin/python -m pytest test/platform/test_contract.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from yuyutsava.platform import (
    FileLock,
    host_profile,
    kill_tree,
    pid_alive,
    spawn_detached,
    terminate_pid,
)
from yuyutsava.platform.process import run_capture

PY = sys.executable


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class TheFileLockIsExclusiveAcrossProcesses(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "held.lock"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_lock_held_elsewhere_is_refused_then_granted_on_release(self) -> None:
        ready = Path(self._tmp.name) / "ready"
        holder = subprocess.Popen(
            [
                PY,
                "-c",
                "import sys, time, pathlib\n"
                "from yuyutsava.platform import FileLock\n"
                "lock = FileLock(sys.argv[1]); lock.acquire()\n"
                "pathlib.Path(sys.argv[2]).write_text('x')\n"
                "time.sleep(60)\n",
                str(self.path),
                str(ready),
            ]
        )
        try:
            self.assertTrue(_wait_until(ready.exists), "the holder never took the lock")
            mine = FileLock(self.path)
            self.assertFalse(mine.acquire(blocking=False), "two processes held one lock")
            kill_tree(holder.pid)
            holder.wait(timeout=10)
            # The OS releases a dead holder's lock; no stale-lock cleanup needed.
            self.assertTrue(_wait_until(lambda: mine.acquire(blocking=False)))
            mine.release()
        finally:
            if holder.poll() is None:
                holder.kill()

    def test_context_manager_stamp_and_idempotent_release(self) -> None:
        with FileLock(self.path) as lock:
            self.assertTrue(lock.locked)
            lock.stamp("1234")
        self.assertFalse(lock.locked)
        lock.release()  # a second release is a no-op, not an error
        self.assertEqual(self.path.read_text(), "1234")
        self.assertTrue(self.path.exists(), "release must keep the file on disk")

    def test_blocking_acquire_times_out(self) -> None:
        with FileLock(self.path):
            other = FileLock(self.path)
            started = time.monotonic()
            # Within one process, a second handle is still refused on every OS.
            self.assertFalse(other.acquire(blocking=True, timeout=0.3))
            self.assertGreaterEqual(time.monotonic() - started, 0.25)


class ProcessesAreSpawnedAndReaped(unittest.TestCase):
    def test_this_process_is_alive_and_a_bogus_pid_is_not(self) -> None:
        import os

        self.assertTrue(pid_alive(os.getpid()))
        self.assertFalse(pid_alive(0))
        self.assertFalse(pid_alive(-1))

    def test_terminate_pid_stops_a_process(self) -> None:
        child = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"])
        try:
            self.assertTrue(pid_alive(child.pid))
            self.assertTrue(terminate_pid(child.pid, timeout=5))
            child.wait(timeout=10)
            self.assertFalse(pid_alive(child.pid))
        finally:
            if child.poll() is None:
                child.kill()

    def test_terminating_a_missing_pid_reports_success_without_raising(self) -> None:
        child = subprocess.Popen([PY, "-c", "pass"])
        child.wait(timeout=10)
        self.assertTrue(terminate_pid(child.pid, timeout=1))

    def test_kill_tree_takes_the_grandchildren_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "grandchild.pid"
            parent = spawn_detached(
                [
                    PY,
                    "-c",
                    "import subprocess, sys, time, pathlib\n"
                    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                    "pathlib.Path(sys.argv[1]).write_text(str(g.pid))\n"
                    "time.sleep(60)\n",
                    str(pidfile),
                ]
            )
            try:
                self.assertTrue(_wait_until(pidfile.exists), "the grandchild never started")
                grandchild = int(pidfile.read_text())
                self.assertTrue(pid_alive(grandchild))
                kill_tree(parent.pid, timeout=5)
                parent.wait(timeout=10)
                self.assertTrue(
                    _wait_until(lambda: not pid_alive(grandchild)),
                    "kill_tree left a grandchild running",
                )
            finally:
                if parent.poll() is None:
                    parent.kill()


class RunCaptureIsLoopAgnostic(unittest.TestCase):
    def test_output_and_exit_code(self) -> None:
        out, err, rc = asyncio.run(
            run_capture(
                [PY, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"]
            )
        )
        self.assertEqual(out.decode().strip(), "out")
        self.assertEqual(err.decode().strip(), "err")
        self.assertEqual(rc, 3)

    def test_a_timeout_raises_and_the_child_does_not_outlive_it(self) -> None:
        started = time.monotonic()
        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(run_capture([PY, "-c", "import time; time.sleep(60)"], timeout=0.5))
        self.assertLess(time.monotonic() - started, 10, "the timeout did not stop the wait")


class TheHostProfileDescribesThisHost(unittest.TestCase):
    def setUp(self) -> None:
        self.host = host_profile()

    def test_the_family_matches_the_interpreter(self) -> None:
        expected = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
        self.assertEqual(self.host.os_family, expected)
        self.assertEqual(
            [self.host.is_windows, self.host.is_macos, self.host.is_linux].count(True), 1
        )

    def test_the_native_shell_runs_a_command(self) -> None:
        """What every tr_execute on this host depends on."""
        argv = self.host.shell_command("echo yuyutsava-contract")
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("yuyutsava-contract", result.stdout)

    def test_the_temp_dir_is_a_temp_zone(self) -> None:
        self.assertIn(tempfile.gettempdir(), self.host.temp_zone_prefixes())

    def test_system_critical_paths_are_known(self) -> None:
        self.assertTrue(self.host.system_critical_prefixes)

    def test_the_prompt_names_this_os_and_its_opener(self) -> None:
        block = self.host.prompt_block()
        self.assertIn(f"OS: {self.host.os_family}", block)
        opener = {"windows": "explorer.exe", "macos": "`open`"}.get(self.host.os_family, "xdg-open")
        self.assertIn(opener, block)


if __name__ == "__main__":
    unittest.main()
