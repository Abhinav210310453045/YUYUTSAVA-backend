"""A background subagent reports its spend, never the panel's window.

The context meter's window figures describe *the conversation* — what is in the
prompt the user is talking to. A background subagent runs on its own
host-minted thread with its own message list, so if it publishes window
segments, the split-view panel jumps to a different conversation's shape
mid-delegation and then stays there. Its spend still accumulates, which is the
part that belongs to the user.

`ContextMeterPolicy(window=False)` is what prevents it, and every background
host has to pass it. Two of the three did (`R-DRY-05`): the CLI chat path did
not, so a background subagent launched from `yuyutsava chat` redrew the panel
with its own window.

This is asserted over the source because the alternative is standing up three
agent hosts and a real model. The invariant is narrow and mechanical — a
background middleware is the one built with ``role=f"{sa.name}-bg"`` — so the
check is too.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent / "yuyutsava"


def _background_middleware_calls() -> list[tuple[str, int, ast.Call]]:
    """Every ``context_middleware(...)`` built for a background subagent."""
    found: list[tuple[str, int, ast.Call]] = []
    for path in sorted(_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name != "context_middleware":
                continue
            role = next((k for k in node.keywords if k.arg == "role"), None)
            if role is None:
                continue
            # role=f"{sa.name}-bg" — a JoinedStr ending in the "-bg" marker.
            if not isinstance(role.value, ast.JoinedStr):
                continue
            tail = role.value.values[-1]
            if isinstance(tail, ast.Constant) and str(tail.value).endswith("-bg"):
                found.append((str(path.relative_to(_ROOT.parent)), node.lineno, node))
    return found


class EveryBackgroundHostOptsOutOfTheWindow(unittest.TestCase):
    def test_the_background_hosts_are_found_at_all(self) -> None:
        """A rename that makes this test vacuous must fail, not pass."""
        calls = _background_middleware_calls()
        self.assertGreaterEqual(
            len(calls), 1, "no background context middleware found — has the role marker changed?"
        )

    def test_each_one_sets_meter_window_false(self) -> None:
        offenders = []
        for where, line, call in _background_middleware_calls():
            kw = next((k for k in call.keywords if k.arg == "meter_window"), None)
            ok = kw is not None and isinstance(kw.value, ast.Constant) and kw.value.value is False
            if not ok:
                offenders.append(f"{where}:{line}")
        self.assertEqual(
            offenders,
            [],
            "background hosts publishing the panel's window: " + ", ".join(offenders),
        )


class TheFlagStillDoesWhatTheNameSays(unittest.TestCase):
    """Guard the other half: the assertion above is only worth anything while
    ``window=False`` actually suppresses the window publish."""

    def test_a_window_less_policy_measures_nothing_before_the_call(self) -> None:
        import asyncio

        from yuyutsava.context.meter.policy import ContextMeterPolicy

        windowless = ContextMeterPolicy(
            settings=object(), role="sub-bg", model_name="m", window=False
        )
        # before_model returns early, so nothing is staged to publish.
        self.assertIsNone(asyncio.run(windowless.before_model(object())))
        self.assertIsNone(windowless._pending)

    def test_a_window_owning_policy_does_try_to_measure(self) -> None:
        """The negative half: if window=True measured nothing either, the
        assertion above would pass for the wrong reason."""
        import asyncio

        from yuyutsava.context.meter.policy import ContextMeterPolicy

        owner = ContextMeterPolicy(settings=object(), role="agent", model_name="m")
        # A bare object is not a Turn, so measuring fails and is swallowed —
        # what matters is that it was attempted at all.
        with self.assertLogs("yuyutsava.context.meter", level="DEBUG") as logs:
            asyncio.run(owner.before_model(object()))
        self.assertTrue(
            any("measuring failed" in m for m in logs.output),
            "window=True must reach the measurement",
        )


if __name__ == "__main__":
    unittest.main()
