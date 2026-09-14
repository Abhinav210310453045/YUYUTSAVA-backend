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

**Severity** P1 · **Wave** W1 (lint) + W7 (tests) · **Status** Open

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

**Severity** P1 · **Wave** W7 · **Status** Open

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

**Severity** P1 · **Wave** W7 · **Status** Open

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

**Severity** P2 · **Wave** W7 · **Status** Open

**Evidence.** `components/proposals/AskCard.jsx` (214 lines) has zero importers.
All four importers — `AskOverlay.jsx:2`, `ChatPanel.jsx:19`, `VoicePanel.jsx:9`,
`ProposalsPanel.jsx:3` — import `components/asks/AskCard.jsx`, whose header calls
itself "the one ask card".

**Consequence.** A developer searching for `AskCard` can edit the dead copy and
see no effect.

**Fix.** Delete `components/proposals/AskCard.jsx`.

---

## R-UI-05 — HTTP calls outside the API layer

**Severity** P2 · **Wave** W7 · **Status** Open

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

**Severity** P2 · **Wave** W7 · **Status** Open

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

**Severity** P2 · **Wave** W7 · **Status** Open

**Evidence.**
- The dev-server URL `http://localhost:5173` appears in `src/main/index.js:48` and
  `src/main/overlay.js:75`.
- The daemon address is built in `src/renderer/api/client.js:1-6`,
  `src/main/daemon.js:270` and `src/main/ipc-handlers.js:14`.

**Fix.** One `src/main/config.js` for the main process; the renderer receives the
daemon address through the existing preload bridge (STD-UI-6).

---

## R-UI-08 — Hook files with the wrong extension

**Severity** P2 · **Wave** W7 · **Status** Open

**Evidence.** `src/renderer/hooks/useAsks.jsx` contains no JSX, while its
siblings mix `.js` (`useConverse.js`) and `.jsx`.

**Fix.** Rename `useAsks.jsx` to `useAsks.js` and update its importers (STD-UI-3).

---

## Not findings

- The build warns that `babel-*.js` is 2.3 MB. It is already code-split: it
  loads only through the dynamic `import('@babel/standalone')` in
  `SandboxBlock.jsx:67`, when a JSX artifact block runs.
- Seven `console.*` calls in the renderer and main process are within reason
  for a desktop app.
