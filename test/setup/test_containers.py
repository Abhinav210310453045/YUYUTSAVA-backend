"""Starting a container is not finished until the thing inside it answers.

The failure this exists for: ``docker compose up -d`` returns as soon as the
container exists, which is well before Postgres accepts a connection — so a
setup that trusted it would report success and leave a chat that dies on
``connection timeout expired``.

What must hold:

* the commands are the ones the repo's own compose files define, including the
  mermaid companion Kroki needs and the profile Langfuse lives behind;
* "started" means the service's own probe answers, not that docker returned;
* every failure says what to run to see it happen, because a wizard cannot show
  a container's logs.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from yuyutsava.config.checks import Check
from yuyutsava.setup.containers import SERVICES, DockerContainers


def _done(code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker"], code, out, err)


class _Containers(unittest.TestCase):
    def setUp(self) -> None:
        self.slept: list[float] = []
        self.said: list[str] = []
        self.clock = [0.0]

    def containers(self) -> DockerContainers:
        def sleep(seconds: float) -> None:
            self.slept.append(seconds)
            self.clock[0] += seconds

        return DockerContainers(self.said.append, now=lambda: self.clock[0], sleep=sleep)


class TheCommands(_Containers):
    def test_they_are_the_ones_the_compose_files_define(self) -> None:
        containers = self.containers()
        self.assertEqual(containers.command("postgres"), "docker compose up -d postgres")
        self.assertEqual(
            containers.command("kroki"),
            "docker compose -f docker-compose.kroki.yml up -d kroki mermaid",
        )
        self.assertEqual(containers.command("langfuse"), "docker compose --profile langfuse up -d")

    def test_kroki_starts_the_companion_mermaid_needs(self) -> None:
        # Kroki alone answers /health and then 503s every mermaid request.
        self.assertIn("mermaid", SERVICES["kroki"].services)

    def test_every_service_names_the_probe_that_proves_it_works(self) -> None:
        from yuyutsava.config.checks import PROBES

        for name, service in SERVICES.items():
            with self.subTest(service=name):
                self.assertIn(service.check, PROBES)
                self.assertTrue(service.purpose, "a person deciding needs to know what it is for")

    def test_something_it_cannot_start_is_said_not_guessed(self) -> None:
        found = self.containers().start("nothing-like-this")
        self.assertFalse(found.ok)
        self.assertIn("nothing-like-this", found.problem)


class StartedMeansWorking(_Containers):
    def test_it_waits_for_the_probe_not_for_docker(self) -> None:
        answers = [
            Check("Postgres", problem="not yet", fix="wait"),
            Check("Postgres", problem="not yet", fix="wait"),
            Check("Postgres"),
        ]
        with (
            patch("yuyutsava.setup.containers.checks.check_docker", return_value=Check("Docker")),
            patch.object(subprocess, "run", return_value=_done()),
            patch("yuyutsava.config.checks.run", side_effect=answers),
        ):
            found = self.containers().start("postgres")
        self.assertTrue(found.ok)
        self.assertEqual(len(self.slept), 2, "it waited until the probe answered")

    def test_a_container_that_never_becomes_ready_says_the_probes_reason(self) -> None:
        never = Check("Postgres", problem="connection timeout expired", fix="look at the logs")
        with (
            patch("yuyutsava.setup.containers.checks.check_docker", return_value=Check("Docker")),
            patch.object(subprocess, "run", return_value=_done()),
            patch("yuyutsava.config.checks.run", return_value=never),
        ):
            found = self.containers().start("postgres")
        self.assertFalse(found.ok)
        self.assertIn("connection timeout expired", found.problem)
        self.assertIn("not ready", found.problem)

    def test_a_stopped_engine_is_reported_before_anything_is_run(self) -> None:
        stopped = Check("Docker", problem="not responding", fix="Start Docker Desktop.")
        with (
            patch("yuyutsava.setup.containers.checks.check_docker", return_value=stopped),
            patch.object(subprocess, "run", side_effect=AssertionError("must not run docker")),
        ):
            found = self.containers().start("postgres")
        self.assertFalse(found.ok)
        self.assertIn("Docker Desktop", found.fix)


class EveryFailureSaysWhatToRun(_Containers):
    def test_a_compose_failure_quotes_dockers_last_word_and_the_command(self) -> None:
        with (
            patch("yuyutsava.setup.containers.checks.check_docker", return_value=Check("Docker")),
            patch.object(
                subprocess, "run", return_value=_done(1, err="Error response from daemon: no space")
            ),
        ):
            found = self.containers().start("postgres")
        self.assertIn("no space", found.problem)
        self.assertIn("docker compose up -d postgres", found.fix)

    def test_a_compose_that_hangs_is_given_up_on_with_the_command(self) -> None:
        with (
            patch("yuyutsava.setup.containers.checks.check_docker", return_value=Check("Docker")),
            patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(["docker"], 1.0)),
        ):
            found = self.containers().start("kroki")
        self.assertFalse(found.ok)
        self.assertIn("docker-compose.kroki.yml", found.fix)


class StartingTheEngine(_Containers):
    def test_it_waits_for_the_engine_after_asking_for_it(self) -> None:
        answers = [
            Check("Docker", problem="not yet", fix="wait"),
            Check("Docker", note="Docker Engine 29.6.1"),
        ]
        with (
            patch("yuyutsava.setup.containers.launch_docker_desktop", return_value=True),
            patch("yuyutsava.setup.containers.checks.check_docker", side_effect=answers),
        ):
            found = self.containers().launch_engine()
        self.assertTrue(found.ok)
        self.assertTrue(any("waiting for Docker" in line for line in self.said))

    def test_an_engine_that_will_not_come_up_says_to_open_it(self) -> None:
        with (
            patch("yuyutsava.setup.containers.launch_docker_desktop", return_value=True),
            patch(
                "yuyutsava.setup.containers.checks.check_docker",
                return_value=Check("Docker", problem="not responding", fix="start it"),
            ),
        ):
            found = self.containers().launch_engine()
        self.assertFalse(found.ok)
        self.assertIn("Docker Desktop", found.fix)

    def test_where_there_is_no_app_to_open_it_says_so_rather_than_guessing(self) -> None:
        with patch("yuyutsava.setup.containers.launch_docker_desktop", return_value=False):
            found = self.containers().launch_engine()
        self.assertFalse(found.ok)
        self.assertIn("yourself", found.fix)


if __name__ == "__main__":
    unittest.main()
