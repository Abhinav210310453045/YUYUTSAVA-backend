"""How a background subagent's question gets to the person at the terminal.

`R-DRY-05`: the REPL and the one-shot run each wired this themselves, and the
copies were not equivalent. The REPL asks first whether a daemon is running and
defers to it; the one-shot path always started its own watcher.

That is a defect, not untidiness. Attaching to a running daemon's host still
hands this process a URL *and* a mirror, so "there is a host" is not the same
question as "is it mine" — and a one-shot run that had attached put a second
watcher on the daemon's host. Both discover the same interrupt, both route it,
and the de-duplication inside a watcher is per-process and cannot see the other
one: the user is asked twice, and whichever answer lands first resumes the run
while the other waits on a future nobody resolves.
"""

from __future__ import annotations

import ast
import pathlib
import unittest
from unittest import mock

from yuyutsava.cli.background_approvals import BackgroundApprovals, loopback_url


class _Bundle:
    def __init__(self, url: str | None, mirror: object | None) -> None:
        self.async_host_url = url
        self.async_task_mirror = mirror


class WhoAnswers(unittest.IsolatedAsyncioTestCase):
    async def test_no_host_means_nothing_can_ask(self) -> None:
        approvals = await BackgroundApprovals.open(_Bundle(None, object()))
        self.assertIsNone(approvals.watcher)
        self.assertIsNone(approvals.remote)
        self.assertIsNone(approvals.bridge)

    async def test_a_running_daemon_is_deferred_to(self) -> None:
        """It owns the host and the one idempotent decision pipeline, so an
        approval answered in the app and one answered here stay in sync."""
        started = []

        class Remote:
            def __init__(self, *_a, **_k) -> None: ...
            async def start(self) -> None:
                started.append(True)

        with (
            mock.patch(
                "yuyutsava.cli.background_approvals.read_daemon_discovery",
                return_value={"web_url": "http://0.0.0.0:7654"},
            ),
            mock.patch("yuyutsava.cli.background_approvals.CliRemoteHitl", Remote),
        ):
            approvals = await BackgroundApprovals.open(_Bundle("http://h", object()))
        self.assertIsNotNone(approvals.remote)
        self.assertIsNone(approvals.watcher, "no local watcher while a daemon owns the host")
        self.assertEqual(started, [True])

    async def test_a_local_watcher_only_without_a_daemon(self) -> None:
        class Watcher:
            def __init__(self, **_k) -> None: ...
            async def start(self) -> None: ...

        with (
            mock.patch("yuyutsava.cli.background_approvals.read_daemon_discovery", return_value={}),
            mock.patch("yuyutsava.cli.background_approvals.AsyncTaskHealthWatcher", Watcher),
        ):
            approvals = await BackgroundApprovals.open(_Bundle("http://h", object()))
        self.assertIsNotNone(approvals.watcher)
        self.assertIsNone(approvals.remote)

    async def test_a_host_with_no_mirror_asks_nobody(self) -> None:
        with mock.patch(
            "yuyutsava.cli.background_approvals.read_daemon_discovery", return_value={}
        ):
            approvals = await BackgroundApprovals.open(_Bundle("http://h", None))
        self.assertIsNone(approvals.watcher)


class ShuttingDown(unittest.IsolatedAsyncioTestCase):
    async def test_a_watcher_that_raises_does_not_stop_teardown(self) -> None:
        """Teardown runs on the way out of a turn that already worked."""

        class Boom:
            async def shutdown(self) -> None:
                raise RuntimeError("no")

            async def stop(self) -> None:
                raise RuntimeError("no")

        await BackgroundApprovals(watcher=Boom(), remote=Boom()).aclose()

    async def test_closing_with_nothing_open_is_harmless(self) -> None:
        await BackgroundApprovals().aclose()

    async def test_rendering_with_no_bridge_is_harmless(self) -> None:
        await BackgroundApprovals().render_between_turns()


class TheDaemonUrlIsMadeLoopback(unittest.TestCase):
    """A daemon may advertise a wildcard bind with bearer auth enforced;
    connecting from 127.0.0.1 is exempt."""

    def test_wildcards_are_rewritten(self) -> None:
        self.assertEqual(loopback_url("http://0.0.0.0:7654"), "http://127.0.0.1:7654")
        self.assertEqual(loopback_url("http://[::]:7654"), "http://127.0.0.1:7654")

    def test_a_real_host_is_left_alone(self) -> None:
        self.assertEqual(loopback_url("http://192.168.1.5:7654"), "http://192.168.1.5:7654")


class BothEntryPointsAskTheSameWay(unittest.TestCase):
    ROOT = pathlib.Path(__file__).resolve().parent.parent.parent / "yuyutsava" / "cli"

    def test_neither_builds_its_own_watcher(self) -> None:
        """The one-shot path did, which is how it ended up with two on one
        host. Anything that constructs a watcher outside this module is that
        bug coming back."""
        offenders = []
        for path in sorted(self.ROOT.rglob("*.py")):
            if path.name == "background_approvals.py":
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                    if name == "AsyncTaskHealthWatcher":
                        offenders.append(f"{path.relative_to(self.ROOT.parent)}:{node.lineno}")
        self.assertEqual(offenders, [], f"watchers built outside the opener: {offenders}")

    def test_both_go_through_the_opener(self) -> None:
        for name in ("chat.py", "chat_repl.py"):
            source = (self.ROOT / "commands" / name).read_text()
            with self.subTest(entry=name):
                self.assertIn("BackgroundApprovals", source)


if __name__ == "__main__":
    unittest.main()
