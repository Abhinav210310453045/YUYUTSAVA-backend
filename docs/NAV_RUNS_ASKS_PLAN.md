# Detached runs, a real navigation stack, and unified durable asks

## Context

Three related defects, all rooted in the same mistake: **things that should be owned by the daemon (or by the app) are instead owned by a React component's lifetime.**

1. **Conversation runs die with the socket.** `yuyutsava/daemon/web/routers/converse.py:821-830` cancels the in-flight turn in the WebSocket handler's `finally`. The turn task is a local variable (`converse.py:738`), so only that connection can see it. Any disconnect — closing a tinker pane, switching TODO cards, a renderer reload, a flaky socket — hard-cancels the agent mid-node. Background agents don't have this problem because they're owned by a daemon-lifetime task (`orchestrator_loop.py:116-126`) or a separate thread (`async_subagents/host.py:81`) and addressed by a persisted `task_id`. Chat/voice/tinker should behave the same way: **the socket is a viewer, not the owner.** (Also: `webPreferences` in `electron-app/src/main/index.js:32-37` never sets `backgroundThrottling: false`, so a minimized window freezes the token smoother, the playback poll and the WS ping — streaming *looks* dead even when it isn't.)

2. **No navigation model.** The entire router is one `useState` string (`App.jsx:30`) plus a `key={activePanel}` remount (`App.jsx:222-240`). Every drill-down lives in component-local `useState` — `TodosPanel.openId:229`, `TodoCardView.chatSel:57`, `thinkOpen:52`, `selected:68`, `ArtifactsPanel.expanded:112`. Going to Settings from an open TODO card unmounts the whole subtree; coming back lands on the board. The only back button in the app is a hand-rolled one at `TodoCardView.jsx:367-377`.

3. **Asks are split, lossy, and invisible.** Two unrelated transports: hub/SSE asks (`stream_service.py:318-329`) have an `ask_id`, reach `ProposalsPanel`, and **block forever with no way to rediscover them** (no `GET /asks`, no replay — a reconnecting client is blind); converse-WS asks (`converse.py:359-373`) have **no id at all**, are invisible to every other surface, and silently auto-reject after 300 s. Toasts can't carry buttons (`InWindowToast.jsx:23`), and the always-on-top overlay — the one surface that can reach the user anywhere — receives nothing.

**Outcome:** a turn keeps running no matter what the UI does; navigation has a proper per-tab back stack with preserved view state; and every ask from every agent is one durable record that reaches the user wherever they are, without ever leaking into a different session's view.

---

## Design decisions (settled)

| Decision | Choice |
|---|---|
| Back button | Global titlebar chevron **and** in-panel header chevron |
| Back at a tab's root | Disabled — per-tab stacks, never jumps tabs |
| Detached voice audio | Keeps playing; a global ▶/■ transport appears in the titlebar beside the voice-mode toggle |
| Ask expiry | **Never.** Persisted at the interrupt, resumed only when answered |
| Ask durability | Survives daemon restart (DB row + `Command(resume=…)` re-entry) |
| Overlay | Auto-shows via `showInactive()` when the main window isn't focused; never steals focus |
| Ask routing | By **ownership**, see below |

### Ask display rules — the separation of concern

An ask belongs to exactly one **owning surface**: the chat / voice / tinker thread, the background task, or the CLI session that raised it. Rendering is a pure function of `(owner, where the user is)`:

| Where the user is | What they get |
|---|---|
| On the owning view, app focused | **Inline** ask block in that view (only place it renders inline) |
| Elsewhere in the app (another view, another chat) | **Notification toast**: "TinkerAgent needs permission → Open" + inbox entry. Click opens the overlay. **Never** rendered inside the non-owning session |
| App unfocused / hidden / another Space | **Overlay auto-shows** (`showInactive`), answerable in one click, + OS banner |
| Any of the above | Always listed in the **Inbox** (Proposals tab) until answered |
| CLI-owned ask, CLI focused | CLI prompts as today; the overlay + inbox also carry it, first answer wins |

The overlay's **X** closes the window without answering — the ask stays pending in the inbox. Every ask surface (inline, overlay, inbox) uses the same expand/collapse card: collapsed shows agent + one-line summary + options; expanded shows the full command, all paths, reason, risk/zone, session id and agent path.

---

## Phase 1 — Navigation stack & view state

Hand-rolled, ~200 LOC, zero new deps (the app has only `react`/`react-dom`/`react-markdown`/`remark-gfm`). A router library would force restructuring every panel and still not give per-tab stacks for free.

**New `electron-app/src/renderer/nav/NavProvider.jsx`**
- State: `{ activePanel, stacks: { [panel]: Route[] } }` where `Route = { panel, params }` and params are serializable only.
- `useNav()` → `{ route, params, depth, canGoBack, push, replace, pop, switchTab }`.
- `push` adds a level (back unwinds it); `replace` swaps the top without adding depth — used for lateral moves like picking a different tinker chat, so back from a card still goes to the board rather than through every chat you opened.
- Persist the whole tree to `yy.nav.v1`, restored under the **existing** run-id gate (`App.jsx:47-64`) so a fresh launch still opens at Chat. This retires `PANEL_KEY`, `pendingTodoRestore`, `consumeRestoredCard` (`App.jsx:73-77`) and `yy.todo.openId`.

**New `electron-app/src/renderer/nav/useViewState.js`**
- `useViewState(slot, initial)` — component state backed by a module-level `Map` keyed by `routeKey + slot`, so ephemeral view state survives unmount but is dropped when `NavProvider` genuinely pops that route. This is the tier that fixes "I lost my place": scroll offsets, `thinkOpen`, `selectMode`/`selected`, `attOpen`, `activityOpen`, unsaved title/composer drafts, settings accordions.

**New `electron-app/src/renderer/components/layout/BackButton.jsx`**
- 28×28 icon button copying the titlebar recipe verbatim (`Titlebar.jsx:96-113`) with a chevron `<polyline points="15 18 9 12 15 6"/>` matching `navIcons.jsx:5-50`. Rendered in `Titlebar.jsx` immediately after line 48 (left of the wordmark, inside the 80px traffic-light pad, `WebkitAppRegion:'no-drag'`), and reused in panel headers where a labelled variant reads better (replaces `TodoCardView.jsx:367-377`). Disabled/dimmed at `depth === 1`.
- Bind `Cmd/Ctrl+[`, `Alt+←`, and mouse button 3.

**Panel migrations — identity becomes route params, everything else becomes view state**

| Component | Becomes a route param (`push`/`replace`) | Becomes `useViewState` |
|---|---|---|
| `todos/TodosPanel.jsx:229` | `cardId` (replaces `openId` + the early return at :280-287) | list scroll |
| `todos/TodoCardView.jsx:44-90` | `chat` (from `chatSel`, via `replace`) | `thinkOpen`, `attOpen`, `selectMode`, `selected`, `activityOpen`, `expandedAtt`, `title` draft |
| `artifacts/ArtifactsPanel.jsx:111-118` | `artifactId` / `visualId` (so **back closes the modal**, alongside Esc) | `sessionId` filter, scroll |
| `settings/SettingsPanel.jsx:64-71` | — | unsaved form edits, `SettingsSection.jsx:4` accordion state |
| `sessions/SessionsPanel.jsx` | `switchTab('chat'\|'voice')` + `resumeId` param (replaces `onOpenSession`, `App.jsx:92-98`) | scroll |

Chat and Voice keep the `visited` mount-hold (`App.jsx:241-250`) — after Phase 2 it's belt-and-braces, but it also preserves DOM scroll.

**Verify:** open a TODO card → open a tinker chat → send a message → go to Settings → back arrow returns to the *card with that chat still open, messages intact*; a second back returns to the board; back is disabled there. Reload the renderer mid-card and land in the same place. `npm run build` in `electron-app/`.

---

## Phase 2 — Runs owned by the daemon

### Server

**New `yuyutsava/daemon/turn_registry.py`** — a `TurnRun` per thread, modelled directly on the background-task ring already in `stream_service.py:207-265` (mirror `TASK_RING_SIZE` / `MAX_TRACKED_TASKS` naming):

```
TurnRun: run_id, thread_id, session_id, origin, agent, card_id,
         task: asyncio.Task, seq: int, ring: deque[dict],
         subscribers: set[asyncio.Queue], status, error, pending_ask_id, started_at, ended_at
```

- `ConversationManager` (`conversation_manager.py:129`) upgrades `_busy_threads: set[str]` into `_runs: dict[str, TurnRun]`. `try_begin_turn`/`end_turn` (`:377-395`) become `start_turn()` / run completion — the same mutual-exclusion guarantee, but now holding a real handle.
- The turn task is created **by the manager on the daemon loop**, not in the WS handler's scope.
- `run.emit(ev)` stamps a monotonic `seq`, appends to the ring, and fans out to every attached queue (non-blocking; the ring is the truth, so a slow subscriber just falls behind and catches up on replay).
- `attach(thread_id, since_seq) -> (replay, queue)` / `detach(queue)`.
- Retention: finished runs kept ~5 min, capped, then swept — so a client reconnecting just after completion still receives `turn_end`.
- `audio_chunk` frames are marked ephemeral: fanned out live, **never** stored in the ring (they'd be megabytes). The persisted WAV written by `_persist_voice_message` (`converse.py:422-440`) is already the replay path and already surfaces as `audio_url`.

**`converse.py` becomes a viewer**
- Delete the `turn_task.cancel()` in the `finally` (`:825-830`); disconnect only detaches. Keep `voice.close()` — `VoicePipeline` is genuinely per-connection (it owns mic frames).
- `_on_event` (`:353-357`) routes through `run.emit`; per-connection `_send` stays for connection-scoped frames (`pong`, mic state).
- Connect handshake gains `since_seq`; `hello` gains `run: {run_id, status, seq}` followed by ring replay, then live.
- Explicit cancel is unchanged and remains the *only* way to stop a turn: the `interrupt` frame (`:718-721`) plus a new `POST /conversations/{thread_id}/cancel` for parity with `POST /tasks/{id}/cancel`.

### Client

**New `electron-app/src/renderer/conversations/store.js`** — module-level `Map<key, ConversationSession>`, key `origin|agent|card|threadId`. A `ConversationSession` owns the `ConverseClient`, `messages`, `busy`, `hello`, `pendingAsk`, `lastSeq`, and a listener set; `retain()`/`release()` refcount, with released-but-busy sessions kept alive (idle ones disconnected after ~10 min).

`hooks/useConverse.js` becomes a thin subscriber over that store (`useSyncExternalStore`), **keeping its current return signature** so `ChatPanel`/`VoicePanel` need no changes. The `useEffect` teardown at `:377` stops calling `client.disconnect()`. Reconnect sends `since_seq` so the replay fills the gap instead of the current give-up at `:340-352`.

**`electron-app/src/main/index.js:32-37`** — add `backgroundThrottling: false` to `webPreferences` (same for the overlay window). Fixes the literal "minimize and it stalls" symptom.

**New `components/layout/PlaybackButton.jsx`** — mounted in `Titlebar.jsx` beside the voice-mode toggle. Subscribes to the existing `audioPlayer` singleton (`renderer/audio/index.js:216`, which already has `isPlaying/isPaused/pause/resume/stop`; add a small listener emitter rather than a new poll). Renders ■ while audible (click → `pause()`), ▶ while paused (click → `resume()`), hidden when nothing is playing; tooltip names the speaking session and clicking the label navigates there.

**Verify:** start a long tinker turn → close the think pane / switch cards / go to Settings → reopen: the turn is still streaming and the missed tokens replay. Minimize mid-turn, restore: smooth continuation. Voice reply mid-sentence → leave the panel: audio continues and the titlebar ■/▶ appears. Reload the renderer mid-turn (tray → restart is heavier; use devtools reload): the run survives and re-attaches. Stop button still cancels immediately.

---

## Phase 3 — One durable ask, everywhere it belongs

### Server

1. **One record.** Extend `AskPrompt` (`channels.py:236`) and `StreamAskItem.to_wire_dict` (`stream_service.py:97-108`) to carry the structured `interrupt_value` (today it's dropped at the wire boundary, which is why clients can't render a full command) plus ownership: `surface` (`chat|voice|tinker|background|cli`), `thread_id`, `card_id`, `task_id`, `agent_path`, `agent_label`. Keep the existing `title`/`body`/`options` from `interrupt_format.py` as the collapsed summary.
2. **Converse asks join the hub.** Replace the closure at `converse.py:359-373` with the same `channels.post_ask` path, so a chat/tinker/voice ask gets an `ask_id`, reaches SSE, and is answerable via `POST /ask/{id}/respond` → `DecisionService.respond_ask` (`decision_service.py:123-130`). The WS `ask` frame still goes to the owning connection (now carrying `ask_id`) so the inline UI is unchanged. **Delete `_ASK_TIMEOUT_SEC`** (`converse.py:73`) — nothing auto-rejects.
3. **Durability.** New `pending_asks` table (SQLite `state.db` + PG migration v19, following the existing migration pattern): `ask_id, created_ts, surface, thread_id, card_id, task_id, interrupt_id, agent_path, title, body, options, payload_json, status, answered_ts, response`. Written **before** broadcasting, marked resolved on answer. `interrupt_id` is the `it_id` from `streaming.py:456-462`, needed so multi-interrupt resumes map correctly.
4. **Rediscovery.** New `GET /asks?status=pending` in `routers/proposals.py` (where `/ask/{id}/respond` already lives). This closes the real hole: `WebHub.broadcast` silently drops on `QueueFull` (`stream_service.py:254-257`), and today a dropped or missed frame means the ask is gone forever. With hydration on connect it self-heals.
5. **Restart resume.** On boot, load pending rows. When an answer arrives with no in-memory future, a new `AskResumeService` re-enters the owner: conversation threads start a detached run via the Phase 2 registry with `Command(resume=<decision>)` (LangGraph already checkpointed the graph at `interrupt()`); async subagent tasks reuse the existing `runs.create(command={"resume": replies})` path (`async_subagents/watcher.py:702-800`). Consent scope handling (`parse_consent_decision`, `consent/models.py:81-95`) is untouched.
6. **Fan-out, not first-accepting-channel.** `ChannelRouter.post_ask` (`channels.py:364-378`) stops picking one channel for UI surfaces: one record is broadcast, first answer anywhere wins, the rest resolve via the existing `ask_resolved` broadcast (`stream_service.py:267-277`). CLI keeps its own inline prompt *and* appears in overlay + inbox.

### Client

- **`hooks/useAsks.jsx`** — the single source of pending asks: `GET /asks` hydration on connect + SSE `ask`/`ask_resolved` + optimistic resolve. Feeds the badge count already wired to the tray (`useSSE.jsx:245-247`).
- **`components/asks/AskCard.jsx`** — one shared presentational card with the **expand/collapse disclosure**, used by all three surfaces. Collapsed: agent label, one-line summary, option buttons. Expanded: full command, all paths, reason, `zone · risk`, session id, agent path. Replaces the ad-hoc key-probing at `ChatPanel.jsx:240-243` and its duplicate at `VoicePanel.jsx:428-450`, and fixes their vocabulary drift (they send `yes`/`no` and silently drop the session/project scope options that `AskCard.jsx:22-27` offers).
- **`hooks/useAskRouting.js`** — the ownership table above implemented in exactly one place, so no ask can ever leak into a non-owning session's view.
- **Inbox** — `ProposalsPanel.jsx` gains an Asks section fed by `useAsks` (nav label becomes "Inbox"), same expandable card, with session/agent attribution.
- **Actionable toast** — `useNotifications.jsx` currently only auto-generates text toasts from SSE and doesn't even export `pushToast` (`:26-31`). Export it, and give `InWindowToast.jsx` an action row so the "agent X needs permission → Open" pointer is clickable.
- **Overlay** — `renderer/overlay.jsx` (today 14 lines, voice only) gains an asks subscription and `<AskOverlay>` using the shared card, plus the **X** that hides the window and leaves the ask pending. New IPC `overlay:show-ask` / `overlay:hide-ask` in `main/overlay.js`, reusing its existing `showInactive()` + always-on-top/all-Spaces setup and the dock bounce from `main/notifications.js:25-55`.

**Verify:** trigger a `tr_ask_user` from a tinker chat, then (a) stay on it → inline card only, nothing in another chat; (b) switch to Settings → toast + inbox entry, no inline card anywhere; (c) unfocus the app → overlay pops without stealing focus, answer there, watch the inline card and inbox entry both resolve. Press X on the overlay → still pending in the inbox, answer it there. Trigger a permission ask from a background task and confirm the same three surfaces. Restart the daemon with an ask pending → it's still in the inbox; answering it resumes the agent. Confirm `approve`/`session`/`project` still record consent grants (`consent/registry.py:92-119`).

---

## Notes

- Migrations, ask records and the run registry all follow patterns already in the repo — the background-task ring (`stream_service.py`), the task registry (`task_registry.py`), and `DecisionService`'s multi-waiter map. Nothing here is a new architecture; it's giving conversations the ownership model background work already has.
- Phase boundaries are review gates: I stop after each for you to try it.
