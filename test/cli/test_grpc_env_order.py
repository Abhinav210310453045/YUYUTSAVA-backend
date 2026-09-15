"""The entry points set GRPC_VERBOSITY before gRPC can read it.

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
    "import sys\n"
    "import yuyutsava.cli.cli, yuyutsava.daemon.main\n"
    "print(sorted(m for m in sys.modules if m == 'grpc' or m.startswith('grpc.')))\n"
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
        self.assertEqual(result.stdout.strip().splitlines()[-1], "[]")


if __name__ == "__main__":
    unittest.main()
