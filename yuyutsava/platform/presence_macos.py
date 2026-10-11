"""The macOS user-presence check, run as its own process: Touch ID, an Apple Watch or the login password.

``python -m yuyutsava.platform.presence_macos <reason>`` asks macOS to confirm the
owner is at this Mac (``LAPolicyDeviceOwnerAuthentication``: one system dialog,
biometrics where there are any, the login password always). ``--can`` only asks
whether that is possible here. :mod:`yuyutsava.platform.user_presence` runs it
and reads the exit status:

* ``0`` — confirmed (with ``--can``: possible here);
* ``1`` — refused or cancelled;
* ``2`` — not possible here (no password set, no window server, not macOS);
* ``3`` — no answer within the time allowed.

Its own process because LocalAuthentication answers only while the asking
thread's run loop runs — a thread here spins one; the daemon's event loop and
the CLI's prompt_toolkit never have to.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

try:
    import LocalAuthentication
    from Foundation import NSDate, NSRunLoop
except ImportError:  # not macOS, or pyobjc absent: the check is not possible here
    LocalAuthentication = None

CONFIRMED, REFUSED, UNAVAILABLE, TIMED_OUT = 0, 1, 2, 3

#: How long the dialog may wait for an answer.
_WAIT_SECONDS = 120.0

#: The errors that mean "not possible here" rather than "refused".
_UNAVAILABLE_CODES = frozenset({-5, -6, -7, -1004})  # no passcode, no biometry, not interactive


def main(argv: list[str]) -> int:
    if LocalAuthentication is None:
        return UNAVAILABLE
    policy = LocalAuthentication.LAPolicyDeviceOwnerAuthentication
    context = LocalAuthentication.LAContext.alloc().init()
    possible, _ = context.canEvaluatePolicy_error_(policy, None)
    if not possible:
        return UNAVAILABLE
    if argv[:1] == ["--can"]:
        return CONFIRMED
    reason = argv[0] if argv else "confirm it is you"
    answered = threading.Event()
    outcome: dict[str, int] = {}

    def reply(success: bool, error: Any) -> None:  # error: an NSError, or None
        if success:
            outcome["status"] = CONFIRMED
        elif error is not None and error.code() in _UNAVAILABLE_CODES:
            outcome["status"] = UNAVAILABLE
        else:
            outcome["status"] = REFUSED
        answered.set()

    context.evaluatePolicy_localizedReason_reply_(policy, reason, reply)
    deadline = time.monotonic() + _WAIT_SECONDS
    while not answered.is_set() and time.monotonic() < deadline:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
    if not answered.is_set():
        context.invalidate()
        return TIMED_OUT
    return outcome["status"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
