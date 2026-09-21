"""The entry points set GRPC_VERBOSITY before gRPC can read it.

Both call ``config.logs.quiet_grpc_fork_logs`` — one place holds the variable
and the reason (`R-DRY-05`); what stays at each entry point is *when* it is
called, which is the whole requirement.

``cli/cli.py`` and ``daemon/main.py`` set the variable after their imports.
gRPC reads it when it is first imported, so that is early enough only while
importing the entry points does not import gRPC. Checked in a fresh
interpreter, because this process may already have gRPC loaded.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_PROBE = (
    "import os, sys\n"
    "import yuyutsava.cli.cli, yuyutsava.daemon.main\n"
    "print(sorted(m for m in sys.modules if m == 'grpc' or m.startswith('grpc.')))\n"
    "print(os.environ.get('GRPC_VERBOSITY'))\n"
)

_OVERRIDE_PROBE = (
    "import os\n"
    "os.environ['GRPC_VERBOSITY'] = 'DEBUG'\n"
    "import yuyutsava.cli.cli\n"
    "print(os.environ['GRPC_VERBOSITY'])\n"
)


class GrpcEnvOrderTest(unittest.TestCase):
    def test_importing_the_entry_points_does_not_import_grpc(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", _PROBE],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        lines = result.stdout.strip().splitlines()
        self.assertEqual(lines[-2], "[]", "gRPC was imported before the variable was set")
        self.assertEqual(lines[-1], "ERROR", "the variable was not set at all")

    def test_an_operator_debugging_grpc_can_still_override_it(self) -> None:
        """It is a setdefault for this reason, and a setdefault that has become
        an assignment looks identical until someone needs the logs."""
        result = subprocess.run(
            [sys.executable, "-c", _OVERRIDE_PROBE],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(result.stdout.strip().splitlines()[-1], "DEBUG")


if __name__ == "__main__":
    unittest.main()
