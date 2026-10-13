"""A probe says whether a setting works, and never costs anything unasked.

What must hold:

* every answer carries both what is wrong and what to do about it — the shape
  a provider login check already returns, so no surface has to learn a second;
* a probe never raises at its caller, whatever the machine does, because the
  wizard asking "does this work?" must not be how a setup dies;
* the ones that can spend money or a credit are named, and do nothing until
  they are asked to.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from yuyutsava.config import checks
from yuyutsava.config.checks import COSTS_A_CREDIT, PROBES, Check


class EveryProbe(unittest.TestCase):
    def test_a_problem_always_comes_with_a_fix(self) -> None:
        # Every probe driven down its failure path at once, with nothing real
        # reached: a probe that cannot say what to do is the defect here.
        refused = httpx.ConnectError("refused")
        with (
            patch.object(httpx, "post", side_effect=refused),
            patch.object(httpx, "get", side_effect=refused),
            patch("yuyutsava.config.checks._ran", return_value=(1, "cannot connect")),
            patch("psycopg.connect", side_effect=OSError("connection timeout expired")),
        ):
            for name in PROBES:
                if name in COSTS_A_CREDIT:
                    continue
                with self.subTest(probe=name):
                    found = checks.run(name)
                    self.assertFalse(found.ok, "the failure path is what is being read")
                    self.assertTrue(found.fix, f"{name} says what is wrong but not what to do")
                    self.assertTrue(found.name, f"{name} does not say what it probed")

    def test_postgres_says_how_to_start_it_and_how_to_do_without(self) -> None:
        with patch("psycopg.connect", side_effect=OSError("connection timeout expired")):
            found = checks.check_postgres("postgresql://u:p@127.0.0.1:5435/yuyutsava")
        self.assertIn("127.0.0.1:5435/yuyutsava", found.name)
        self.assertIn("docker compose up -d postgres", found.fix)
        self.assertIn("sqlite", found.fix, "a person stuck here needs the way round it too")

    def test_it_never_raises_at_its_caller(self) -> None:
        def explode(**_kw: object) -> Check:
            raise RuntimeError("the machine did something nobody expected")

        with patch.dict(PROBES, {"kroki": explode}):
            found = checks.run("kroki")
        self.assertFalse(found.ok)
        self.assertIn("RuntimeError", found.problem)
        self.assertIn("bug in YUYUTSAVA", found.fix)

    def test_a_name_that_is_not_a_probe_is_a_problem_not_an_exception(self) -> None:
        found = checks.run("not-a-probe")
        self.assertFalse(found.ok)
        self.assertIn("not-a-probe", found.problem)


class NothingIsSpentUnasked(unittest.TestCase):
    def test_a_key_check_sends_nothing_by_default(self) -> None:
        with patch.object(httpx, "post", side_effect=AssertionError("nothing may be sent")):
            found = checks.check_tavily("tvly-looks-right")
        self.assertTrue(found.ok)
        self.assertIn("nothing was sent", found.note)

    def test_the_ones_that_cost_are_named(self) -> None:
        self.assertEqual(COSTS_A_CREDIT, frozenset({"tavily", "exa"}))

    def test_a_missing_key_says_where_to_get_one(self) -> None:
        self.assertIn("tavily.com", checks.check_tavily("").fix)
        self.assertIn("exa.ai", checks.check_exa("").fix)

    def test_a_key_of_the_wrong_shape_is_doubted_but_not_rejected(self) -> None:
        found = checks.check_tavily("sk-this-is-an-openai-key")
        self.assertFalse(found.ok)
        self.assertIn("tvly-", found.problem)

    def test_a_live_check_says_the_credit_was_spent(self) -> None:
        answer = httpx.Response(200, request=httpx.Request("POST", "https://api.tavily.com/search"))
        with patch.object(httpx, "post", return_value=answer):
            found = checks.check_tavily("tvly-x", live=True)
        self.assertTrue(found.ok)
        self.assertIn("spent", found.note)


class WhatTheySay(unittest.TestCase):
    def test_kroki_names_the_companion_container_on_a_503(self) -> None:
        # Kroki answers /health without its mermaid companion and then 503s
        # every mermaid request, which is the failure people actually hit.
        answer = httpx.Response(503, request=httpx.Request("POST", "http://localhost:8000"))
        with patch.object(httpx, "post", return_value=answer):
            found = checks.check_kroki("http://localhost:8000")
        self.assertFalse(found.ok)
        self.assertIn("mermaid", found.fix)
        self.assertIn("docker-compose.kroki.yml", found.fix)

    def test_an_unreachable_kroki_names_the_command_that_starts_it(self) -> None:
        with patch.object(httpx, "post", side_effect=httpx.ConnectError("refused")):
            found = checks.check_kroki("http://localhost:8000")
        self.assertIn("docker compose", found.fix)
        self.assertIn("http://localhost:8000", found.name)

    def test_langfuse_warns_about_the_port_everything_else_wants(self) -> None:
        with patch.object(httpx, "get", side_effect=httpx.ConnectError("refused")):
            found = checks.check_langfuse("http://localhost:3000")
        self.assertIn("3000", found.fix)

    def test_docker_being_absent_is_told_apart_from_docker_being_stopped(self) -> None:
        with patch("yuyutsava.config.checks.shutil.which", return_value=None):
            missing = checks.check_docker()
        self.assertIn("not installed", missing.problem)
        self.assertIn("get-docker", missing.fix)
        with (
            patch("yuyutsava.config.checks.shutil.which", return_value="docker"),
            patch("yuyutsava.config.checks._ran", return_value=(1, "cannot connect")),
        ):
            stopped = checks.check_docker()
        self.assertIn("not responding", stopped.problem)
        self.assertIn("Docker Desktop", stopped.fix)


if __name__ == "__main__":
    unittest.main()
