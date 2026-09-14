"""Channel plugins: chat surfaces the daemon can enable and disable at runtime.

A plugin sends outbound messages through the
:class:`~yuyutsava.daemon.channels.UserChannel` contract and handles inbound ones —
submitting tasks, answering proposals and asks — through
:class:`~yuyutsava.channels.plugin.InboundSink`, the only daemon surface it sees.
:class:`~yuyutsava.channels.registry.ChannelPluginRegistry` enables, disables and
reloads plugins without a restart, configured by ``~/.yuyutsava/channels_config.json``
(:mod:`yuyutsava.channels.config`). ``telegram/`` is the reference plugin.

This package re-exports nothing: import a name from the module that owns it.
"""
