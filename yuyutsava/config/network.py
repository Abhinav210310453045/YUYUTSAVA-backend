"""Where the daemon listens by default.

One value for every place that needs it — the daemon's own config, the attach
command's fallback, and the Settings catalog — so they cannot drift apart. The
remote-attach client once defaulted to a different port entirely (R-BUG-04).
"""

from __future__ import annotations

#: The daemon's web/API port when ``YUYUTSAVA_DAEMON_PORT`` is not set.
DEFAULT_DAEMON_PORT = 7654

#: The URL a local client falls back to when no discovery file says otherwise.
DEFAULT_DAEMON_URL = f"http://127.0.0.1:{DEFAULT_DAEMON_PORT}"
