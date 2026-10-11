"""The in-memory LangGraph server and its SDK still take the keywords we pass.

Background subagents run on ``langgraph_api.cli.run_server`` inside the
process (``async_subagents/host.py``), and the watchers drive that server
through ``langgraph_sdk``, always by keyword. langgraph-api ships a minor every
few weeks and calls anything two minors old end of life, so it is upgraded
often. ``run_server`` takes ``**kwargs``: a keyword it renamed is swallowed, not
rejected, and the server quietly stops honouring it (``allow_blocking``, say).
A keyword the SDK dropped raises only inside a watcher's background poll. This
names the keyword that moved, before the upgrade lands.
"""

from __future__ import annotations

import inspect
import unittest

from yuyutsava.async_subagents.host import AsyncSubagentHost

#: ``(client attribute, method, keywords)`` — what the watchers and the
#: interrupt middleware in ``yuyutsava/async_subagents`` pass, sync and async.
SDK_CALLS = (
    ("threads", "get", ("thread_id",)),
    ("threads", "search", ("limit",)),
    ("threads", "update_state", ("thread_id", "values")),
    ("runs", "create", ("thread_id", "assistant_id", "command", "multitask_strategy")),
    ("runs", "list", ("thread_id", "limit")),
    ("runs", "get", ("thread_id", "run_id")),
    ("runs", "cancel", ("thread_id", "run_id")),
)

#: What both client factories are called with.
CLIENT_KEYWORDS = ("url", "headers")


def unnamed_keywords(fn, keywords) -> list[str]:
    """The *keywords* that *fn* does not declare by name.

    A ``**kwargs`` catch-all does not count: a keyword that lands there is one
    the function no longer knows.
    """
    named = {
        name
        for name, param in inspect.signature(fn).parameters.items()
        if param.kind not in (param.VAR_POSITIONAL, param.VAR_KEYWORD)
    }
    return sorted(set(keywords) - named)


def unnamed_sdk_keywords(client_module) -> list[str]:
    """``Class.method(keyword)`` for each SDK keyword *client_module* no longer declares."""
    classes = {
        "threads": ("ThreadsClient", "SyncThreadsClient"),
        "runs": ("RunsClient", "SyncRunsClient"),
    }
    moved = []
    for attr, method, keywords in SDK_CALLS:
        for cls_name in classes[attr]:
            fn = getattr(getattr(client_module, cls_name), method, None)
            if fn is None:
                moved.append(f"{cls_name}.{method}")
                continue
            moved += [f"{cls_name}.{method}({kw})" for kw in unnamed_keywords(fn, keywords)]
    for factory in ("get_client", "get_sync_client"):
        moved += [
            f"{factory}({kw})"
            for kw in unnamed_keywords(getattr(client_module, factory), CLIENT_KEYWORDS)
        ]
    return moved


def test_run_server_declares_every_keyword_the_host_passes() -> None:
    from langgraph_api.cli import run_server  # noqa: PLC0415 — the server is under test

    kwargs = AsyncSubagentHost(graphs={"general-purpose": object()}).server_kwargs()
    moved = unnamed_keywords(run_server, kwargs)
    assert not moved, f"langgraph_api.cli.run_server no longer declares {moved}"


def test_the_sdk_declares_every_keyword_the_watchers_pass() -> None:
    import langgraph_sdk.client  # noqa: PLC0415 — the SDK is what is under test, imported where it is checked

    moved = unnamed_sdk_keywords(langgraph_sdk.client)
    assert not moved, f"langgraph_sdk.client no longer declares {moved}"


def test_a_latest_graph_is_called_once_per_run_as_a_factory() -> None:
    """``LatestGraph`` relies on langgraph-api calling a registered callable per run.

    That is how a subagent's MCP server that came up after the host started
    reaches its next background run. If the server stopped treating a callable
    as a factory, every run would get the graph object itself — not a graph.
    """
    from langgraph_api._factory_utils import classify_factory, invoke_factory, is_factory

    from yuyutsava.async_subagents.host import LatestGraph

    generation = [0]
    built: list[object] = []

    def build() -> object:
        built.append(object())
        return built[-1]

    latest = LatestGraph(build, lambda: generation[0])
    classify_factory(latest, "contract-latest-graph")
    assert is_factory("contract-latest-graph"), "a callable is no longer treated as a factory"
    first = invoke_factory(latest, "contract-latest-graph", {}, None)
    assert first is built[0], "the factory's graph is not what a run gets"
    generation[0] = 1  # a server came up
    second = invoke_factory(latest, "contract-latest-graph", {}, None)
    assert second is built[1] and len(built) == 2, "the next run did not get a rebuilt graph"


class TheCheckFails(unittest.TestCase):
    """Negative controls: a renamed keyword is named, even behind ``**kwargs``."""

    def test_a_keyword_swallowed_by_kwargs_is_named(self) -> None:
        def run_server(host, port, allow_blocking_io=False, **kwargs):
            pass

        self.assertEqual(
            unnamed_keywords(run_server, {"host": 1, "port": 2, "allow_blocking": True}),
            ["allow_blocking"],
        )

    def test_a_renamed_sdk_keyword_is_named(self) -> None:
        def method(
            self, *, thread_id, run_id, assistant_id, command, multitask_strategy, limit, values
        ):
            pass

        def renamed_get(self, thread, run_id):
            pass

        def factory(*, url=None, headers=None):
            pass

        client = type(
            "client",
            (),
            {
                name: type(name, (), {m: method for _, m, _ in SDK_CALLS})
                for name in ("ThreadsClient", "SyncThreadsClient", "RunsClient", "SyncRunsClient")
            }
            | {"get_client": factory, "get_sync_client": factory},
        )
        client.RunsClient.get = renamed_get
        self.assertEqual(unnamed_sdk_keywords(client), ["RunsClient.get(thread_id)"])


if __name__ == "__main__":
    unittest.main()
