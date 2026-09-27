# 09 — Electron app

The desktop app in `electron-app/`: React 18 renderer, Electron main process,
Vite build. About 15.8k lines.
Rules: [01 §14](01-standards.md). Numbers: [02](02-metrics.md).

**Headline.** The app has no lint, formatting, type or test tooling at all, so
nothing guards its largest files. One class holds ~800 lines of conversation
state. Four components exceed 500 lines, and a dead duplicate component shares
a name with the live one.

---

## R-UI-01 — No lint, format, type or test tooling

**Severity** P1 · **Wave** W1 (lint) + W7 (tests) · **Status** **Fixed** (W1 lint, W7 tests, 2026-09-27).
- W1 added ESLint (`rules-of-hooks` as an error, warnings under a ratcheted ceiling) and Prettier.
- W7 added vitest 2.1.9 (`npm test`, Node environment) with 32 tests: 25 characterize the conversation store frame by frame and action by action, and 7 cover the API client's base URL, JSON calls, 204, and error and raw-body helpers.
- CI runs them in the `electron` job. A failing test gives exit code 1, and each store test has a negative control.
- Later in W7 the lint gate reached zero: every silent `catch` states its reason and `no-empty` is an error; the event stream no longer swallows handler exceptions; hook dependencies are complete; `--max-warnings` went 33 → 0. A 300-line `max-lines` error applies to every source file but tests, with a shrink-only allowlist (`max-lines-allowlist.json`, checked by `scripts/check-max-lines-allowlist.js` in `npm run lint`). Tests: 54 (plus the multi-select, the resize helper and the event stream).

**Evidence.**
- `electron-app/package.json` defines only `dev`, `build`, `dist` and `electron`.
- There is no ESLint or Prettier configuration, no TypeScript, no `PropTypes`
  (zero uses), and no test file.
- CI builds the renderer and nothing else (`.github/workflows/ci.yml`, job
  `electron`).

**Consequence.** Rules-of-hooks violations, missing effect dependencies, unused
components and unreachable code are invisible until a user hits them. Splitting
the 922-line conversation store (`R-UI-02`) would happen with no safety net.

**Fix.**
- ESLint flat config with `eslint-plugin-react` and `eslint-plugin-react-hooks`
  (`rules-of-hooks` as an error; the rest ratcheted), Prettier, and
  `npm run lint` in CI.
- vitest for the conversation store and the API client before either is split.

**Verification.** `npm run lint` and `npm test` green in CI. A negative control
(a hook called conditionally) fails the lint job.

---

## R-UI-02 — One class holds the conversation state

**Severity** P1 · **Wave** W7 · **Status** **Fixed** (W7, 2026-09-27). The 1097-line `store.js` is five modules:
- `messages.js` (107 lines): the rows, list patches, turn-start dedupe, history and the ask queue, as pure functions. It has 9 unit tests.
- `frames.js` (271): one handler per daemon frame type, in a table, with `applyFrame` owning the seq cursor.
- `session.js` (636): `ConversationSession`, i.e. the transport, the actions, voice and playback.
- `sessions.js` (48): the session map, acquire, key and eviction. A session tells the map it was disposed through a hook, so there is no import cycle.
- `presence.js` (73): the speaker and the visible threads.

The 25 characterization tests, written first against the unsplit store, pass unchanged. Verified in Electron: a chat turn with an artifact tile and the usage meter, and an inline permission ask. With the chat not visible, the ask was routed to a toast and the Inbox; approved in the app, the turn resumed and deleted the file.

**Evidence.** `src/renderer/conversations/store.js` is 922 lines. The class
`ConversationSession` spans `:95-892`. It holds:
- reference counting (`refs`), listeners and an idle-eviction timer (`:103-107`)
- the rendered snapshot state
- the event handling that mutates that state

The module's remaining functions add speaker and visible-thread subscriptions
(`:46-91`) and session acquisition and eviction (`:898-922`).

**Consequence.** Every chat surface (chat panel, voice, TODO-card tinker chat)
depends on this one class. A change to how one event kind renders is made inside
an 800-line class with no tests.

**Fix.** vitest tests first. Then split into:
- a pure event reducer (`(state, event) → state`), the part that deserves the
  most tests;
- the transport subscription;
- the lifecycle (acquire, release, idle eviction).

**Verification.** Reducer unit tests. Drive the app's chat, voice and TODO-card
chat and compare the results.

---

## R-UI-03 — Components over 500 lines

**Severity** P1 · **Wave** W7 · **Status** **Fixed** (W7, 2026-09-28). Each named component is split by responsibility, as pure moves verified in Electron by pixel diffs of fixed states (0 differing pixels) and, where state moved, by driving the writes and checking them through the API:

| Component | Before → after | Split into |
|---|---|---|
| `chat/ChatPanel.jsx` | 864 → 280 | `ThreadItems`, `ChatHeader`, `ContextChips`, `ChatComposer`, `useContextColumn`, `useFollowOutput` |
| `voice/VoicePanel.jsx` | 775 → 239 | `FloatingMic`, `VoiceBubble`, `VoiceChrome`; the context column reuses `useContextColumn` |
| `todos/ThinkBoard.jsx` | 1028 → 296 | `todos/thinkboard/`: `NoteCard`, `NoteComposer`, `ObjectiveCard`, `useObjectiveActions`, `useColumnWidth`, `ColumnHeader`, `common` |
| `todos/TodoCardView.jsx` | 913 → 260 | `todos/card/`: `CardHeader`, `ObjectiveComposer`, `CardMeta`, `TinkerPane`, `useCardSelection` over `selection.js` (unit-tested), `useCollapsedObjectives`, `useCardAttachments` |
| `settings/SettingsPanel.jsx` | 509 → 274 | `DaemonControls`, `schemaField`, `fallbackSchema` |

The five resize implementations (activity rail, context column, board columns, tinker pane, attachments drawer) became one `lib/resize.js`; a drag probe over every handle, including the clamps and the persisted sizes, matched before and after. ESLint now caps every source file at 300 lines; nine files are on the shrink-only allowlist (`R-UI-01`).

**Evidence.**

| Component | Lines | Notes |
|---|---|---|
| `components/todos/ThinkBoard.jsx` | 742 | 37 inline style objects |
| `components/todos/TodoCardView.jsx` | 713 | 27 inline style objects, 4 effects |
| `components/chat/ChatPanel.jsx` | 626 | 40 inline style objects, 5 effects |
| `components/voice/VoicePanel.jsx` | 509 | 34 inline style objects, 5 effects |

Next in line: `SettingsPanel.jsx` 399, `ActivityLog.jsx` 397,
`asks/AskCard.jsx` 359, `TodosPanel.jsx` 357, `UsageSettings.jsx` 337,
`VoiceOverlay.jsx` 330.

**Fix.** Split each by responsibility (view pieces, a state hook, API calls) to at
most 300 lines per file (STD-UI-2).

---

## R-UI-04 — A dead component with a live component's name

**Severity** P2 · **Wave** W7 · **Status** **Fixed** (W7, 2026-09-27). Deleted; `asks/AskCard.jsx` is the only ask card.

**Evidence.** `components/proposals/AskCard.jsx` (214 lines) has zero importers.
All four importers — `AskOverlay.jsx:2`, `ChatPanel.jsx:19`, `VoicePanel.jsx:9`,
`ProposalsPanel.jsx:3` — import `components/asks/AskCard.jsx`, whose header calls
itself "the one ask card".

**Consequence.** A developer searching for `AskCard` can edit the dead copy and
see no effect.

**Fix.** Delete `components/proposals/AskCard.jsx`.

---

## R-UI-05 — HTTP calls outside the API layer

**Severity** P2 · **Wave** W7 · **Status** **Fixed** (W7, 2026-09-27). `api/client.js` gained `fetchBlob`, `fetchText` and `fetchArrayBuffer` over one `_get` that raises on a non-OK status. Before, the visual copy and download read a 404's JSON body as the image. No `fetch` is left outside `api/`. Verified live in Electron: the Markdown preview, the HTML source toggle, audio `playUrl` (a served WAV plays, and a missing one names its 404), and Copy image (the clipboard holds `image/png`).

**Evidence.** `fetch` bypasses `src/renderer/api/` at:
- `components/artifacts/VisualActions.jsx:56,73`
- `components/todos/artifactBlocks/TextBlock.jsx:19`
- `components/todos/artifactBlocks/SandboxBlock.jsx:158`
- `audio/index.js:185`

**Consequence.** Base-URL handling, authentication headers and error handling in
`api/client.js` do not apply to these calls.

**Fix.** Add helpers in `api/client.js` (`fetchVisualBlob`, `fetchBlockSource`,
`fetchAudio`) and call them.

---

## R-UI-06 — Inline styles bypass the theme system

**Severity** P2 · **Wave** W7 · **Status** **Fixed for the worst five; the rest carried** (W7, 2026-09-28). The five files with the most inline style objects after the splits each have a co-located stylesheet with classes on the theme's custom properties, and keep inline only what is computed per render:

| File | Inline objects | Pattern |
|---|---|---|
| `settings/UsageSettings.jsx` | 38 → 3 | `usage-*`; table cells by modifier |
| `todos/TodosPanel.jsx` | 31 → 6 | `todos-*`, `todo-card-*`; status accents inline |
| `artifacts/ArtifactsPanel.jsx` | 29 → 2 | a card sets `--kind`; the stylesheet derives every tint with `color-mix` |
| `background-tasks/TaskDetail.jsx` | 34 → 7 | booleans become modifiers (`--mono`, `--error`, `--live`) |
| `proposals/ProposalCard.jsx` | 24 → 0 | button variants as classes |

Verified in Electron by dumping every element's full computed style, geometry and text in fixed states, frozen and with animations and transitions live, before and after: identical in all 19 states (`TaskDetail` and `ProposalCard` mounted in the running app with fixed data). App-wide, inline style objects went 650 → 512 and `className` uses 40 → 187. Literal colours stayed literal: turning them into theme tokens changes pixels under the other themes, a design decision rather than a move. Carried: the remaining 512 objects, file by file, the same way.

**Evidence.**
- **`style={{…}}` counts:** `ChatPanel.jsx` 40, `UsageSettings.jsx` 38,
  `ThinkBoard.jsx` 37, `VoicePanel.jsx` 34, `TaskDetail.jsx` 34, `TodosPanel.jsx` 31,
  `ArtifactsPanel.jsx` 29, `TodoCardView.jsx` 27, `ProposalCard.jsx` 24,
  `ActivityLog.jsx` 23.
- **Hard-coded values:** many are fixed sizes and spacing, e.g.
  `ArtifactsPanel.jsx:86` `fontSize: 9`.

**Consequence.** The six named themes restyle through `:root[data-theme]` custom
properties. Literal values in inline styles cannot follow a theme or a density
change, and they repeat.

**Fix.** Classes built on theme tokens for static styling (worst five files
first). Inline styles only for values computed at render time (STD-UI-4).

---

## R-UI-07 — Configuration literals scattered

**Severity** P2 · **Wave** W7 · **Status** **Fixed** (W7, 2026-09-27). `src/main/config.js` holds the dev-server URL, the daemon host, the default port and `daemonUrl()`. `index.js`, `overlay.js`, `daemon.js` and `ipc-handlers.js` use it, and the renderer's one fallback says what it mirrors. `test/config/test_default_daemon_port.py` holds the JavaScript default equal to `config/network.py`, and is negative-controlled.

**Evidence.**
- The dev-server URL `http://localhost:5173` appears in `src/main/index.js:48` and
  `src/main/overlay.js:75`.
- The daemon address is built in `src/renderer/api/client.js:1-6`,
  `src/main/daemon.js:270` and `src/main/ipc-handlers.js:14`.

**Fix.** One `src/main/config.js` for the main process; the renderer receives the
daemon address through the existing preload bridge (STD-UI-6).

---

## R-UI-08 — Hook files with the wrong extension

**Severity** P2 · **Wave** W7 · **Status** **Fixed** (W7, 2026-09-27). `hooks/useAsks.js`; its three importers follow (one relative, from `useNotifications.jsx`, found by the build). The other `.jsx` hooks render JSX.

**Evidence.** `src/renderer/hooks/useAsks.jsx` contains no JSX, while its
siblings mix `.js` (`useConverse.js`) and `.jsx`.

**Fix.** Rename `useAsks.jsx` to `useAsks.js` and update its importers (STD-UI-3).

---

## R-UI-09 — A card's hover glow never shows

**Severity** P2 · **Wave** — (found in W7) · **Status** Open: a visible change, so the owner decides.

**Evidence.** `SessionRow.jsx` and `TodosPanel.jsx`'s `TodoCard` use `className="hover-bulge"` and set `--bulge-glow`, which `.hover-bulge:hover` turns into a glowing `box-shadow` (`styles/globals.css`). Both also set `boxShadow` inline, and an inline declaration outranks the `:hover` rule, so the hover glow is always overridden. `SessionRow`'s comment says `--bulge-glow` "drives the hover glow".

**Fix.** Move the resting shadow out of the inline style (into the card's class, or a `--card-shadow` the `:hover` rule composes), so hovering shows the glow. Check it under each theme.

---

## R-UI-10 — The Visuals session picker loads once

**Severity** P2 · **Wave** — (found in W7) · **Status** Open.

**Evidence.** `ArtifactsPanel.jsx` lists sessions in a mount-only effect. A session started while the panel is open (or after it was first shown, since view state keeps it mounted) is missing from the picker until the panel remounts.

**Fix.** Refresh the session list with the panel's ↻, or when it becomes visible.

---

## Found and fixed during W7

- **An objective card lost its phase bar** after a note was dragged over it or it was ticked: the `border` shorthand changed and reset the 3px `borderLeft` (41ad4985).
- **The event stream swallowed handler exceptions** along with malformed frames (c3e2512c).
- **The visual action buttons' accent border** never applied (`var(--x)55` is invalid CSS), and reverting it reset the border (7d9012f5); 36 such `${var}HH` sites became `alpha()` (9a467865).

---

## Not findings

- The build warns that `babel-*.js` is 2.3 MB. It is already code-split: it
  loads only through the dynamic `import('@babel/standalone')` in
  `SandboxBlock.jsx:67`, when a JSX artifact block runs.
- Seven `console.*` calls in the renderer and main process are within reason
  for a desktop app.
