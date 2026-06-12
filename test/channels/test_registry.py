"""ChannelPluginRegistry lifecycle: enable/disable/reload, idempotence.

Run:  uv run python -m unittest test.channels.test_registry -v
"""

from __future__ import annotations

import unittest

from yuyutsava.channels.config import ChannelConfig, ChannelsConfig
from yuyutsava.channels.plugin import ChannelPlugin, InboundSink
from yuyutsava.channels.registry import ChannelPluginRegistry
from yuyutsava.daemon.channels import ChannelEvent, ChannelRouter, LogPayload


class FakeChannelPlugin(ChannelPlugin):
    name = "fake"
    plugin_id = "fake"
    capabilities = frozenset({"notify", "invoke"})

    instances: list["FakeChannelPlugin"] = []

    def __init__(self, params: dict) -> None:
        self.params = params
        self.started = False
        self.stopped = False
        self.sink: InboundSink | None = None
        self.events: list[ChannelEvent] = []
        FakeChannelPlugin.instances.append(self)

    @classmethod
    def from_config(cls, params: dict) -> "FakeChannelPlugin":
        return cls(params)

    async def start(self, inbound: InboundSink) -> None:
        self.sink = inbound
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def post_event(self, ev: ChannelEvent) -> None:
        self.events.append(ev)

    async def post_proposal(self, p):
        raise NotImplementedError

    async def post_ask(self, a):
        raise NotImplementedError


class _Dummy:
    """Stands in for sink collaborators the fake plugin never touches."""


def make_sink() -> InboundSink:
    return InboundSink(task_submission=_Dummy(), decision_service=_Dummy())


def make_registry(
    *, enabled: bool = True, router: ChannelRouter | None = None,
) -> tuple[ChannelPluginRegistry, ChannelRouter]:
    router = router if router is not None else ChannelRouter(channels=[])
    config = ChannelsConfig(channels={
        "fake": ChannelConfig(name="fake", enabled=enabled, params={"knob": 1}),
    })
    registry = ChannelPluginRegistry(
        router=router, sink=make_sink(), config=config,
        plugin_factories={"fake": FakeChannelPlugin.from_config},
    )
    return registry, router


class RegistryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeChannelPlugin.instances = []

    async def test_enable_joins_router_fanout(self) -> None:
        registry, router = make_registry()
        await registry.start_all()
        self.assertTrue(registry.is_running("fake"))
        plugin = FakeChannelPlugin.instances[-1]
        self.assertTrue(plugin.started)
        self.assertEqual(plugin.params, {"knob": 1})
        self.assertIs(plugin.sink.__class__, InboundSink)

        ev = ChannelEvent(payload=LogPayload(text="hello"))
        await router.post_event(ev)
        self.assertEqual(plugin.events, [ev])

    async def test_double_enable_is_noop(self) -> None:
        registry, _router = make_registry()
        self.assertTrue(await registry.enable("fake"))
        self.assertFalse(await registry.enable("fake"))
        # One running instance only ("never two pollers for one token").
        started = [p for p in FakeChannelPlugin.instances if p.started]
        self.assertEqual(len(started), 1)

    async def test_disable_removes_and_stops(self) -> None:
        registry, router = make_registry()
        await registry.enable("fake")
        plugin = FakeChannelPlugin.instances[-1]
        self.assertTrue(await registry.disable("fake"))
        self.assertTrue(plugin.stopped)
        self.assertIsNone(router.find("fake"))
        self.assertFalse(registry.is_running("fake"))
        # Disabled fan-out: no events delivered.
        await router.post_event(ChannelEvent(payload=LogPayload(text="x")))
        self.assertEqual(plugin.events, [])
        # Double-disable is a no-op.
        self.assertFalse(await registry.disable("fake"))

    async def test_start_all_skips_disabled(self) -> None:
        registry, router = make_registry(enabled=False)
        await registry.start_all()
        self.assertFalse(registry.is_running("fake"))
        self.assertIsNone(router.find("fake"))

    async def test_reload_applies_config_diff(self) -> None:
        registry, router = make_registry(enabled=True)
        await registry.start_all()
        first = FakeChannelPlugin.instances[-1]

        await registry.reload(ChannelsConfig(channels={
            "fake": ChannelConfig(name="fake", enabled=False),
        }))
        self.assertTrue(first.stopped)
        self.assertFalse(registry.is_running("fake"))

        await registry.reload(ChannelsConfig(channels={
            "fake": ChannelConfig(name="fake", enabled=True, params={"knob": 2}),
        }))
        self.assertTrue(registry.is_running("fake"))
        fresh = FakeChannelPlugin.instances[-1]
        self.assertIsNot(fresh, first)
        self.assertEqual(fresh.params, {"knob": 2})

    async def test_unknown_plugin_raises(self) -> None:
        registry, _router = make_registry()
        with self.assertRaises(KeyError):
            await registry.enable("whatsapp")

    async def test_router_name_collision_rolls_back(self) -> None:
        router = ChannelRouter(channels=[])
        squatter = FakeChannelPlugin({})
        router.register(squatter)
        registry, _ = make_registry(router=router)
        with self.assertRaises(RuntimeError):
            await registry.enable("fake")
        loser = FakeChannelPlugin.instances[-1]
        self.assertIsNot(loser, squatter)
        self.assertTrue(loser.stopped)
        self.assertFalse(registry.is_running("fake"))

    async def test_start_all_survives_bad_plugin(self) -> None:
        def broken_factory(params: dict) -> ChannelPlugin:
            raise ValueError("token missing")

        router = ChannelRouter(channels=[])
        config = ChannelsConfig(channels={
            "broken": ChannelConfig(name="broken", enabled=True),
            "fake": ChannelConfig(name="fake", enabled=True),
        })
        registry = ChannelPluginRegistry(
            router=router, sink=make_sink(), config=config,
            plugin_factories={
                "broken": broken_factory,
                "fake": FakeChannelPlugin.from_config,
            },
        )
        await registry.start_all()  # must not raise
        self.assertFalse(registry.is_running("broken"))
        self.assertTrue(registry.is_running("fake"))

    async def test_snapshot_shape(self) -> None:
        registry, _router = make_registry()
        await registry.enable("fake")
        rows = registry.snapshot()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "fake")
        self.assertTrue(row["available"])
        self.assertTrue(row["enabled"])
        self.assertTrue(row["running"])
        self.assertEqual(row["capabilities"], ["invoke", "notify"])


class RouterRegisterTests(unittest.IsolatedAsyncioTestCase):
    async def test_register_unregister_idempotent(self) -> None:
        router = ChannelRouter(channels=[])
        a = FakeChannelPlugin({})
        self.assertTrue(router.register(a))
        self.assertFalse(router.register(FakeChannelPlugin({})))  # same name
        self.assertIs(router.find("fake"), a)
        self.assertIs(router.unregister("fake"), a)
        self.assertIsNone(router.unregister("fake"))
        self.assertIsNone(router.find("fake"))


if __name__ == "__main__":
    unittest.main()
