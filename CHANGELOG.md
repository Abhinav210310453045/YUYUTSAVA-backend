# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/).
While the version stays `0.x`, minor bumps may contain breaking changes.

## [Unreleased]

### Changed
- `main` now carries the full development history and the current code. It had
  been stalled four months behind the working branch.
- Relicensed from MIT to Apache-2.0. Revisions up to and including `8896902`
  remain available under MIT; see [NOTICE](NOTICE).
- `streamlit` moved from a base dependency to the `streamlit` extra — it pulled
  pydeck, altair, tornado and gitpython into every install and is imported
  nowhere in the tree.
- Documentation restructured into `docs/{architecture,reference,guides,design}`
  with an index at `docs/README.md`.
- README rewritten to describe the daemon, desktop app, voice, TODO board,
  memory, MCP and all twelve providers.

### Fixed
- `.gitignore` matched `diagrams/` at any depth, so `docs/diagrams/` had been
  silently ignored since 2026-06-14 and two intentional assets were never
  committed. The rule is now anchored to `/diagrams/`.
- `scripts/verify_diagrams.py` resolved architecture docs by their old
  repo-root paths and would no longer find them.
- 21 pre-existing broken documentation links, including 17 written as
  `yuyutsava/…` instead of `../yuyutsava/…`.
- `test/test_async.py` hardcoded an absolute home-directory path three times
  and could not run from any other checkout.
- `yuyutsava --help` still advertised "Uses Groq or OpenRouter" long after the
  provider layer grew to twelve providers.

### Removed
- Tracked build and run artefacts: `.DS_Store`, a stray
  `electron-app/.langgraph_api/*.pckl`, a leftover agent deliverable, and a
  stale branch-topology diagram.

---

## [0.1.0] — unreleased

The first public release. Development ran from 2026-04-10 in a private
repository; this entry summarises what exists at the point of opening it up
rather than itemising that history.

### Added

**Agent core** — a task-runner gateway mediating every filesystem and shell
call through a zone and permission model, with configurable auto-approve
policy and daily caps.

**Two operating modes** — a one-shot/interactive CLI, and an always-on daemon
with an Electron desktop client, sharing one agent stack.

**LLM provider layer** — twelve providers behind one factory: Groq, OpenRouter,
Ollama, OpenAI and any OpenAI-compatible host; Anthropic, Google Gemini, Vertex
AI, AWS Bedrock, Azure OpenAI, Mistral and Cohere via native SDKs. Per-role
overrides let triage run on a cheap local model while the main agent does not.

**Background subagents** — long jobs detach and run independently; completion
wakes the orchestrator on the parent thread instead of blocking a turn.

**Event-driven triage** — filesystem, clipboard, hotkey and app-focus sources
feed a bus, with a cheap triage model deciding what reaches the expensive one.

**Voice** — speech in and out over the same WebSocket as text chat, with wake
word, VAD and barge-in. Configurable STT (faster-whisper, Groq) and TTS (Piper,
ElevenLabs), with a zero-config macOS `say` fallback.

**TODO board** — a persistent planning surface with a dedicated TinkerAgent,
pluggable artifact blocks (including JSX-sandbox and audio), attachments and
card-pinned chat.

**Memory and retrieval** — pgvector-backed semantic memory and skill recall
over a shared retrieval base, with context compaction and tool-result
offloading.

**MCP** — a client manager for stdio and SSE servers, per-agent tool scoping,
`SIGHUP` hot reload, and an in-tree DeepFace server as a worked example.

**Visuals** — charts, styled tables, syntax-highlighted code, math and diagrams
rendered to images the agent can return.

**Storage** — SQLite by default, PostgreSQL for durability and semantic search,
behind a single dialect adapter.

**Cross-platform** — OS-specific primitives confined to `yuyutsava/platform/`,
with Windows and Linux support alongside macOS.

**`/v1` HTTP API** — a frozen contract for external clients, with bearer auth
for non-loopback binds.

[Unreleased]: https://github.com/Abhinav210310453045/YUYUTSAVA-backend/commits/main
