"""The TODO router declares its board instead of looking it up.

Phase 3 step 3.4, continuing finding AA.

Nineteen handlers in `daemon/web/routers/todos.py` each called
`get_default_exchange()` in their body. Three costs, all of them the service
locator's:

* the dependency appeared in **no signature** — a reader had to open every
  handler to learn the router touches the board at all;
* no handler could be exercised against a different board, so testing meant
  setting a module global and remembering to restore it;
* one process, one board, structurally.

`board()` is now a FastAPI dependency. It resolves from `app.state` when the
daemon installed one and falls back to the process global otherwise — the same
additive shape as `AppContext`: unmigrated paths keep working, and the honest
path exists.

The test that matters is `test_override_replaces_the_board`: it swaps the board
via `dependency_overrides` with **no global set at all**, which is the property
the whole step is about.

Run:  .venv/bin/python test/web/test_todo_board_dependency.py
"""

from __future__ import annotations

import inspect
import unittest

from fastapi import FastAPI

from yuyutsava.daemon.web.routers.todos import board, router


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


class EveryHandlerDeclaresIt(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()
        self.routes = [
            r for r in self.app.routes if getattr(r, "path", "").startswith("/todos")
        ]

    def test_routes_were_found(self) -> None:
        """Negative control — the checks below are vacuous with no routes."""
        self.assertGreaterEqual(len(self.routes), 15)

    def test_no_handler_looks_the_board_up_itself(self) -> None:
        """A handler that uses the board must take it as a parameter."""
        import ast
        import pathlib
        import re

        src = (
            pathlib.Path(__file__).resolve().parents[2]
            / "yuyutsava/daemon/web/routers/todos.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(src)
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if node.name == "board":
                continue  # the dependency itself is allowed the fallback
            if not any(
                isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                and isinstance(d.func.value, ast.Name) and d.func.value.id == "router"
                for d in node.decorator_list
            ):
                continue
            seg = ast.get_source_segment(src, node) or ""
            if re.search(r"\bget_default_exchange\(\)", seg):
                offenders.append(node.name)
        self.assertEqual(
            offenders, [],
            f"these handlers still resolve the board themselves: {offenders}. "
            f"Take it as `ex: TodoExchange = Depends(board)` — otherwise it is "
            f"invisible in the signature and cannot be overridden in a test.",
        )

    def test_no_function_in_the_module_uses_ex_without_taking_it(self) -> None:
        """Checked across **every** function, not just decorated handlers.

        The first version of this check inspected only ``@router``-decorated
        handlers — and three module-level helpers
        (``_require_note_on_card``, ``_require_objective_on_card``,
        ``_require_attachment_on_card``) were rewritten to use ``ex`` without
        ever receiving it. Nothing caught it until the attachment routes 500'd
        in the running daemon with ``NameError: name 'ex' is not defined``.
        Scoping a check to the shape you happened to change is how that
        happens.
        """
        import ast
        import pathlib

        src = (
            pathlib.Path(__file__).resolve().parents[2]
            / "yuyutsava/daemon/web/routers/todos.py"
        ).read_text(encoding="utf-8")
        offenders: list[str] = []
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name == "board":
                continue
            params = {a.arg for a in node.args.args + node.args.kwonlyargs}
            uses = any(
                isinstance(x, ast.Name) and x.id == "ex" and isinstance(x.ctx, ast.Load)
                for stmt in node.body for x in ast.walk(stmt)
            )
            if uses and "ex" not in params:
                offenders.append(f"{node.name} (line {node.lineno})")
        self.assertEqual(
            offenders, [],
            f"these functions reference `ex` without receiving it — a NameError "
            f"at request time:\n  {offenders}",
        )


class EveryRouteActuallyResponds(unittest.TestCase):
    """Call every GET route. Static checks alone let a NameError reach production.

    The signature checks above all passed while three helpers referenced an
    undefined ``ex``, because the calls were inside helper bodies rather than
    handler signatures. Only executing the route catches that.
    """

    def test_no_get_route_raises(self) -> None:
        from fastapi.testclient import TestClient

        app = _app()
        probes = [
            "/todos",
            "/todos/snapshot",
            "/todos/attachments",
            "/todos/no-such-card",
            "/todos/no-such-card/events",
            "/todos/no-such-card/chats",
            "/todos/no-such-card/attachments/no-such-attachment",
            "/todos/no-such-card/attachments/no-such-attachment/bundle/x.js",
        ]
        with TestClient(app, raise_server_exceptions=False) as client:
            for path in probes:
                with self.subTest(path=path):
                    resp = client.get(path)
                    self.assertLess(
                        resp.status_code, 500,
                        f"{path} raised: {resp.text[:300]}",
                    )


class OverrideSeam(unittest.TestCase):
    """The point of the step: a different board, with no global involved."""

    def test_override_replaces_the_board(self) -> None:
        from fastapi.testclient import TestClient

        class _FakeBoard:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def board_snapshot(self):
                self.calls.append("board_snapshot")
                return {"cards": [], "generated_ts": 0.0}

        fake = _FakeBoard()
        app = _app()
        app.dependency_overrides[board] = lambda: fake
        with TestClient(app) as client:
            resp = client.get("/todos/snapshot")
        self.assertEqual(
            fake.calls, ["board_snapshot"],
            "the override was ignored — the handler reached some other board, "
            "which means the dependency is not actually the seam",
        )
        self.assertNotEqual(
            resp.status_code, 500,
            f"handler errored through the override: {resp.text[:200]}",
        )

    def test_app_state_wins_over_the_global(self) -> None:
        """The daemon installs its board on ``app.state``; that must be used."""
        from unittest.mock import Mock

        from fastapi import Request

        sentinel = object()
        request = Mock(spec=Request)
        request.app.state.todo_exchange = sentinel
        self.assertIs(
            board(request), sentinel,
            "board() ignored app.state and fell through to the global — the "
            "daemon's own exchange would be bypassed",
        )

    def test_falls_back_to_the_global_when_unset(self) -> None:
        """Unmigrated / standalone paths must keep working."""
        from unittest.mock import Mock

        from fastapi import Request

        from yuyutsava.todoboard.exchange import get_default_exchange

        request = Mock(spec=Request)
        request.app.state.todo_exchange = None
        self.assertIs(board(request), get_default_exchange())


if __name__ == "__main__":
    unittest.main(verbosity=2)
