# Handoff — Phases 2 & 3

Paste the whole of this file into a new chat as the opening prompt.

---

## Your task

Continue a three-phase build in `$REPO` (branch
`yuyutsava-daemon`). **Phase 1 is complete and verified.** You are implementing
**Phase 2, then Phase 3**, pausing for my review at the phase boundary.

**Read the approved plan first:**
`$HOME/.claude/plans/currently-when-we-are-giggly-brook.md`

That file holds the full context, the problem statement and the agreed design.
Everything below is the delta: what Phase 1 actually built, the design decisions
I settled in conversation (not all of which are obvious from the code), and the
verified file/line references you'll need.

---

## The goal, in one paragraph

Conversation runs (chat, voice, tinker) are owned by the WebSocket handler's
local scope, so any disconnect — closing a tinker pane, switching cards, a
renderer reload — hard-cancels the agent mid-turn. Background agents don't have
this problem because they're owned by a daemon-lifetime task and addressed by a
persisted id. **Phase 2 gives conversations that same ownership model: the
socket becomes a viewer, not the owner.** **Phase 3** then makes every ask from
every agent one durable record that reaches me wherever I am — without ever
leaking into a different session's view.

---

## State of the world: Phase 1 (done, don't redo)

A real navigation model now exists. New files:

- `electron-app/src/renderer/nav/NavProvider.jsx` — one back stack **per tab**.
  `useNav()` → `{ activePanel, route, params, routeKey, depth, canGoBack,
  topRouteOf, push, replace, pop, popToRoot, switchTab }`. A route is
  `{ panel, params }`; params are serializable scalars carrying *identity only*.
  `push` adds depth, `replace` is a lateral move (no depth). Persists the whole
  tree to `yy.nav.v1`, restored on an in-run reload via the app run-id gate.
  Also binds `⌘[` / `Alt+←` / mouse button 3.
- `electron-app/src/renderer/nav/useViewState.js` — `useViewState(slot, initial,
  scope?)` is `useState` that outlives unmount, backed by a module-level LRU
  (60 scopes). **Scope defaults to the active panel, not the routeKey** — params
  change as you drill in and panel state must not reset underneath that. Also
  exports `dropViewState(scope)` and `useScrollRestore(ready, scope?, slot?)`.
- `electron-app/src/renderer/components/layout/BackButton.jsx` — `variant="icon"`
  (titlebar) and `variant="labelled"` (panel headers). Disabled at a tab's root.

Migrated to it: `App.jsx` (now `App` → `NavProvider` → `AppShell`),
`Titlebar.jsx`, `TodosPanel.jsx`, `TodoCardView.jsx`, `ArtifactsPanel.jsx`,
`SettingsPanel.jsx`, `SettingsSection.jsx`, `SessionsPanel.jsx`.

Two things Phase 2 depends on:

- `TodoCardView` keeps its tinker chat selection in `params.chat`
  (`undefined` = unresolved, `'new'` = fresh chat, else a session id) and sets it
  with `replace`, so back still goes card → board.
- `ChatPanel` is still `key`ed per chat at `TodoCardView.jsx` and still
  unmounts when the think pane closes. Phase 1 restores *where you were*;
  restoring the *conversation* is Phase 2's job.

---

## Design decisions I settled (these are not derivable from the code)

**Phase 2**

- Detached voice keeps playing. When I'm not on the owning chat/voice view, a
  **single play/pause button** (triangle ▶ / box ■) appears in the titlebar
  **beside the existing voice-mode toggle**, for as long as audio is audible.
  Two buttons, different purposes, same cause.
- Explicit cancel (the Stop button / `interrupt` frame) stays the only way to
  kill a turn.

**Phase 3 — ask routing is by OWNERSHIP.** This is the part people get wrong:

> A pop-up for permission is the feature where a user who is working on some
> other work can grant permission. **No permission prompt should ever appear in
> another running session's path.**

An ask belongs to exactly one owning surface. Rendering is a function of
`(owner, where I am)`:

| Where I am | What I get |
|---|---|
| On the owning view | **Inline** ask block there — the only place it renders inline |
| Elsewhere in the app (another view, another chat) | A **simple notification** that *that particular agent* needs an ask/permission, + the inbox entry. Answer via the overlay or the inbox. Never inline in the non-owning session |
| Not on the YUYUTSAVA UI at all | **Overlay** popup |
| Background task | Asks in the Proposals/Inbox tab, and follows the same overlay procedure when needed |
| CLI-owned ask, CLI not in focus | Ask through the overlay; the task stays interrupted until answered |
| Always, while pending | Listed in the **Inbox**, which lives in the Proposals section |

- **Nothing ever expires.** Agents wait indefinitely. State is persisted at the
  interrupt and resumes only when I reply — including across a daemon restart.
- The overlay has an **X** that closes the window *without* rejecting; the ask
  stays pending in the inbox.
- **Every** ask/permission surface — inline, overlay, and inbox — has the same
  **expand/collapse** control: collapsed shows the agent and a one-line summary
  with the options; expanded shows the full command, all paths, reason,
  risk/zone, session id and agent path. Cards must show what session the ask
  belongs to and what it's for.
- Overlay auto-shows with `showInactive()` when the main window isn't focused —
  never steals focus.
- Fan out to all UI surfaces; first answer anywhere wins. Voice TTS does **not**
  speak asks unless enabled.
- "Just like Claude Code."

---

## Phase 2 — runs owned by the daemon

### Server

**New `yuyutsava/daemon/turn_registry.py`.** Model it on the background-task
replay ring that already exists in
`yuyutsava/daemon/web/services/stream_service.py` — `TASK_RING_SIZE = 500` at
:209, `MAX_TRACKED_TASKS = 64` at :212, ring append at :245-247, `task_events()`
at :259. Mirror that naming.

```
TurnRun: run_id, thread_id, session_id, origin, agent, card_id,
         task: asyncio.Task, seq: int, ring: deque[dict],
         subscribers: set[asyncio.Queue], status, error,
         pending_ask_id, started_at, ended_at
```

- `yuyutsava/daemon/conversation_manager.py:129` — `self._busy_threads: set[str]`
  becomes `_runs: dict[str, TurnRun]`. `try_begin_turn` (:377-390) and `end_turn`
  (:393-395) become `start_turn()` / run completion: same mutual-exclusion
  guarantee, but now holding a real handle instead of a bare thread id.
- The turn task is created **by the manager on the daemon loop**, not in the WS
  handler's scope.
- `run.emit(ev)` stamps a monotonic `seq`, appends to the ring, fans out to every
  attached queue (non-blocking — the ring is the truth, a slow subscriber catches
  up on replay).
- `attach(thread_id, since_seq) -> (replay, queue)` / `detach(queue)`.
- Retention: keep finished runs ~5 min, capped, then sweep — so a client that
  reconnects just after completion still receives `turn_end`.
- `audio_chunk` frames are **ephemeral**: fanned out live, never stored in the
  ring (megabytes). The persisted WAV from `_persist_voice_message`
  (`converse.py:422`) is already the replay path and already surfaces as
  `audio_url`.

**`yuyutsava/daemon/web/routers/converse.py` becomes a viewer.**

- The cancellation lives at **:819-830** — `except WebSocketDisconnect: pass` /
  `finally:` … `turn_task.cancel()`. Delete that cancel; disconnect only
  detaches. **Keep** `voice.close()` — `VoicePipeline` is genuinely
  per-connection (it owns mic frames).
- Turn tasks are created at **:738** (`user_text`) and **:648** (`_spawn_turn`,
  the voice path). Both move to the registry.
- `_on_event` (:353-357) routes through `run.emit`. Keep the per-connection
  `_send` for connection-scoped frames (`pong`, mic state).
- Connect handshake gains `since_seq`; the `hello` frame (:293) gains
  `run: {run_id, status, seq}`, then ring replay, then live.
- Add `POST /conversations/{thread_id}/cancel` for parity with
  `POST /tasks/{id}/cancel`.
- Note there is a **second, separate** `WebSocketDisconnect` handler at :216 for
  the dictate-only path — don't confuse them.

### Client

**New `electron-app/src/renderer/conversations/store.js`** — module-level
`Map<key, ConversationSession>`, key `origin|agent|card|threadId`. A session owns
the `ConverseClient`, `messages`, `busy`, `hello`, `pendingAsk`, `lastSeq` and a
listener set; `retain()`/`release()` refcount, released-but-busy sessions stay
alive, idle ones disconnect after ~10 min.

`electron-app/src/renderer/hooks/useConverse.js` becomes a thin subscriber over
that store (`useSyncExternalStore`), **keeping its current return signature** so
`ChatPanel`/`VoicePanel` need no changes. Specifically:

- The teardown at **:377** (`return () => { cancelled = true; client.disconnect()
  … }`) must stop disconnecting.
- The give-up on drop at **:340-352** (`onDisconnected` finalizes the bubble and
  clears `busy`) becomes a reconnect that sends `since_seq` and replays the gap.
- `newSession()` (:489) and the `resetNonce` dance still need to work.

**`electron-app/src/main/index.js:32-37`** — add `backgroundThrottling: false` to
`webPreferences` (and the overlay window in `main/overlay.js`). Without it a
minimized window freezes the `TokenSmoother` interval, the playback poll and the
WS ping, so streaming *looks* dead even when the daemon is fine. This is the
literal "minimize and it stalls" symptom.

**New `electron-app/src/renderer/components/layout/PlaybackButton.jsx`** — mounted
in `Titlebar.jsx` beside the voice-mode toggle. The shared `audioPlayer`
singleton is at `electron-app/src/renderer/audio/index.js:216` and already has
`isPlaying()`, `isPaused()`, `pause()`, `resume()`, `stop()`, `getAnalyser()` —
add a small listener emitter rather than another poll. ■ while audible (click →
pause), ▶ while paused (click → resume), hidden when nothing is playing; tooltip
names the speaking session, clicking the label navigates there via `useNav()`.

### Verify Phase 2

Start a long tinker turn → close the think pane / switch cards / go to Settings →
reopen: still streaming, missed tokens replay. Minimize mid-turn, restore:
smooth continuation. Voice reply mid-sentence → leave the panel: audio continues
and the titlebar ■/▶ appears. Reload the renderer mid-turn: the run survives and
re-attaches. Stop button still cancels immediately.

---

## Phase 3 — one durable ask, everywhere it belongs

There are currently **two unrelated ask transports that never meet**:

| | Hub/SSE (`AskPrompt`) | Converse WebSocket |
|---|---|---|
| Producer | `ChannelRouter.post_ask` (`yuyutsava/daemon/channels.py:364-378`) | closure `_ask_handler` (`converse.py:359-373`) |
| Wire | SSE `ask` on `GET /stream` | WS frame `{"type":"ask","payload":…}` |
| Answer | `POST /ask/{ask_id}/respond` (`routers/proposals.py:42-56`) | WS `ask_response` (`converse.py:713-716`) |
| Identity | `ask_id` (uuid4) | **none** |
| Timeout | **none** — blocks forever, unrediscoverable | 300 s → auto `"reject"` |
| Visible in | `ProposalsPanel` → `AskCard` | that one chat panel only |

### Server

1. **One record.** Extend `AskPrompt` (`channels.py:236`) and
   `StreamAskItem.to_wire_dict` (`stream_service.py:97-108`) to carry the
   structured `interrupt_value` — today it's dropped at the wire boundary, which
   is exactly why clients can't render a full command for expand/collapse — plus
   ownership: `surface` (`chat|voice|tinker|background|cli`), `thread_id`,
   `card_id`, `task_id`, `agent_path`, `agent_label`. Keep `title`/`body`/
   `options` from `yuyutsava/daemon/interrupt_format.py` as the collapsed summary.
2. **Converse asks join the hub.** Replace the closure at `converse.py:359-373`
   with the same `channels.post_ask` path so every conversation ask gets an
   `ask_id` and is answerable via `DecisionService.respond_ask`
   (`yuyutsava/daemon/web/services/decision_service.py:123-130`). The WS `ask`
   frame still goes to the owning connection (now carrying `ask_id`).
   **Delete `_ASK_TIMEOUT_SEC`** (`converse.py:73`, used at :369).
3. **Durability.** New `pending_asks` table (SQLite `state.db` + a PG migration —
   the last one was v18, so v19): `ask_id, created_ts, surface, thread_id,
   card_id, task_id, interrupt_id, agent_path, title, body, options,
   payload_json, status, answered_ts, response`. Written **before** broadcasting.
   `interrupt_id` is the `it_id` collected in
   `yuyutsava/core/streaming.py:456-462`, needed so multi-interrupt resumes map
   correctly.
4. **Rediscovery.** New `GET /asks?status=pending` in `routers/proposals.py`.
   This closes a real hole: `WebHub.broadcast` silently drops on `QueueFull`
   (`stream_service.py:254-257`), and asks carry no `task_id` so the per-task
   replay ring can't help. Hydration on connect makes it self-healing.
5. **Restart resume.** Load pending rows on boot. When an answer arrives with no
   in-memory future in `WebHub.pending_asks` (`stream_service.py:224`), a new
   `AskResumeService` re-enters the owner: conversation threads start a detached
   run via the Phase 2 registry with `Command(resume=<decision>)` (LangGraph has
   already checkpointed the graph at `interrupt()`); async subagent tasks reuse
   the existing `runs.create(command={"resume": replies})` path in
   `yuyutsava/async_subagents/watcher.py:702-800`.
6. **Fan-out.** `ChannelRouter.post_ask` stops picking one channel for UI
   surfaces. First answer anywhere wins; the rest resolve through the existing
   `ask_resolved` broadcast (`stream_service.py:267-277`).

Consent is untouched and already rides this path: `parse_consent_decision`
(`yuyutsava/consent/models.py:81-95`), grants in `yuyutsava/consent/registry.py:92-119`.
The `approve` / `session` / `project` / `reject` options **are** the consent
scope selector — don't lose them.

### Client

- **`hooks/useAsks.jsx`** — single source of pending asks: `GET /asks` hydration
  + SSE `ask` / `ask_resolved` + optimistic resolve. `useSSE.jsx` already
  reduces `ASK` / `REMOVE_ASK` (:91-107) and feeds the tray badge (:245-247).
- **`components/asks/AskCard.jsx`** — one shared card with the expand/collapse
  disclosure, used by inline / overlay / inbox. Replaces the ad-hoc key-probing
  at `ChatPanel.jsx:240-243` and its duplicate at `VoicePanel.jsx:428-450`, which
  today send `'yes'`/`'no'` and silently drop the scope options that
  `components/proposals/AskCard.jsx:22-27` offers.
- **`hooks/useAskRouting.js`** — the ownership table above, implemented in
  exactly one place.
- **Inbox** — `ProposalsPanel.jsx` gains an Asks section (nav label → "Inbox").
- **Actionable toast** — `hooks/useNotifications.jsx` doesn't even export
  `pushToast` (it's a local callback at :26-31) and `InWindowToast.jsx:23` is
  text-only with click-to-dismiss. Export it and give the toast an action row.
- **Overlay** — `renderer/overlay.jsx` is 14 lines and mounts only
  `<VoiceOverlay />`; it has no SSE/ask plumbing at all. Add an asks
  subscription + `<AskOverlay>` with the shared card and the X. New IPC
  `overlay:show-ask` / `overlay:hide-ask` in `electron-app/src/main/overlay.js`,
  reusing its existing `showInactive()` + always-on-top/all-Spaces setup
  (`overlay.js:71-76`) and the dock bounce in `main/notifications.js:25-55`.

### Verify Phase 3

Trigger a `tr_ask_user` from a tinker chat, then: (a) stay on it → inline card
only, nothing in any other chat; (b) switch to Settings → notification + inbox
entry, **no inline card anywhere**; (c) unfocus the app → overlay pops without
stealing focus, answer there, watch the inline card and inbox entry both resolve.
Press X on the overlay → still pending in the inbox, answer it there. Repeat for
a background task. Restart the daemon with an ask pending → still in the inbox,
and answering it resumes the agent. Confirm `approve`/`session`/`project` still
record consent grants.

---

## How to run and drive the app (hard-won — reuse this)

`npm run build` in `electron-app/` is **not** a sufficient check: the bundler
treats unknown identifiers as globals, so a missing import compiles clean and
then blanks the renderer at runtime. Phase 1 hit exactly that. Always drive the
real app.

```bash
cd $REPO/electron-app
npx vite --port 5173 > /tmp/yy-vite.log 2>&1 &
# The env -u is essential: ELECTRON_RUN_AS_NODE is set in this environment and
# makes `app` undefined ("Cannot read properties of undefined (reading 'setName')").
env -u ELECTRON_RUN_AS_NODE NODE_ENV=development \
  node_modules/electron/dist/Electron.app/Contents/MacOS/Electron . \
  --remote-debugging-port=9222 > /tmp/yy-electron.log 2>&1 &
```

Then drive it over CDP with plain Node 24 (global `WebSocket`, zero deps) —
`Runtime.evaluate` for state/clicks, `Page.captureScreenshot` for shots. Two
throwaway drivers from Phase 1 are in that session's scratchpad; rewriting them
is ~60 lines. Gotchas: pick the `http://localhost:5173/` page target (skip
`devtools://` and the overlay); `Runtime.enable` **replays historical console
entries**, so an error you see right after connecting may be stale — confirm
against the live DOM before believing it.

The app does **not** auto-start the daemon unless it's managing one, so it will
sit at "disconnected" — fine for UI work, but Phases 2 and 3 need a real daemon.
**Ask me before starting it**; it watches my workspace and runs agents.

---

## Repo conventions

- All renderer styling is inline `style={{}}` objects — no CSS modules. Theme
  vars in `electron-app/src/renderer/styles/theme.css`: `--neon-green` is the
  brand accent (not always green — 6 themes), and `--accent-rgb` is the same
  colour as an `R, G, B` triplet so components write `rgba(var(--accent-rgb), α)`.
  Titlebar icon buttons: 28×28, `borderRadius: 6`, see `Titlebar.jsx:96-113`.
  Panel headers: `padding: '14px 24px'`, `borderBottom: '1px solid
  var(--border-subtle)'`, `background: 'var(--bg-bar)'`.
- I work uncommitted in parallel on this same checkout. **Stage files
  explicitly; never `git add -A`.** Don't commit or push unless I ask.
- Don't run the full pytest suite or app-importing tests — the langgraph import
  is very slow. Prefer fast standalone Python checks plus the vite build.
- `TodoExchange` is the only path to the TODO board. I use the board for real —
  don't touch my cards.
- Pause for my review at the Phase 2 → Phase 3 boundary.
