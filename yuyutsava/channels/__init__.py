"""Channel Invoke Plugin framework (Phase 3).

Channels become runtime-managed plugins: outbound (the existing
:class:`~yuyutsava.daemon.channels.UserChannel` contract) plus inbound
(submit tasks, answer proposals/asks), enabled/disabled at runtime through
:class:`~yuyutsava.channels.registry.ChannelPluginRegistry` with no daemon
restart.

Layout:
    plugin.py    — ``ChannelPlugin`` ABC + ``InboundSink`` (the only daemon
                   surface a plugin sees)
    config.py    — ``ChannelsConfig`` (~/.yuyutsava/channels_config.json)
    registry.py  — lifecycle: enable/disable/reload, ChannelRouter wiring
    telegram/    — reference plugin (Bot API long-polling)

``yuyutsava/daemon/channels.py`` keeps the ChannelRouter and the message
dataclasses — this package only adds the plugin machinery on top.
"""

from yuyutsava.channels.plugin import ChannelPlugin, InboundSink

__all__ = ["ChannelPlugin", "InboundSink"]
