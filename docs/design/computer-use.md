# Computer use — design (not implemented)

Status: **design only.** Nothing in this document is built. It exists so the
implementation is a day's work rather than a week's rediscovery, and so the
decision that shapes it — accessibility tree before pixels — is recorded with
its reasoning.

## What happened

A user asked the agent to read their Spotify Liked Songs, then to "do it
through CUA". It opened a new Chrome profile, opened the Spotify web player,
and then reloaded the page over and over. The user watched it and wrote: *"it
was not able to control the browser app through the chrome"*, and what they
expected was the agent driving the **Spotify desktop app** by its GUI.

The agent was not confused about the goal. It had no way to click.

## Why it degenerated into reloading a page

Four independent facts formed a closed loop:

1. **There is no GUI verb.** Fifty-seven tools, and not one that clicks,
   types, scrolls, focuses a window or reads a UI element.
   `docs/guides/windows.md:57` caps the native channel at "GUI **launch**",
   which is exactly right about today's capability.
2. **The skill it correctly found ends before the hard part.** The user-level
   skill `~/.yuyutsava/skills/chrome-cdp-user-auth` launches Chrome with
   `--remote-debugging-port=9222 --user-data-dir=…` (hence the new profile —
   `~/.yuyutsava/chrome_debug_profile/` is on disk, first written at the time
   of the run), connects Playwright over CDP, then **yields control to the
   human** for MFA. Its verification step is "capture screenshots", which the
   agent cannot look at (see below). It covers authentication, not interaction.
3. **The fallback skill prescribes a remedy that does not exist for this
   target.** `macos-open-app-or-url` says, correctly, *"Act on the page, not
   just show it → build the URL that IS the action"*, and forbids settling for
   a page that merely contains the thing. That works for YouTube. Spotify
   Liked Songs is an authenticated, virtual-scrolled list with no action URL.
   Correct instruction, no applicable move.
4. **The only move left reports success while doing nothing.** Relaunching
   Chrome when Chrome is already running is the silent no-op that same skill
   documents at its top: `open -a … --args` is ignored and exits 0. Because
   `tool-failure-recovery` keys on `status=denied|error`, the anti-thrash guard
   never engaged. A loop that reports success cannot break itself.

## What is already installed and permitted

This is the surprising part: the primitives are all present and the hardest
permission is already granted. Verified by import on this machine.

| Need | Present as | Note |
|---|---|---|
| Screen capture | `Quartz.CGWindowListCreateImage`; `screencapture` CLI | the CLI is not in the shell denylist |
| Synthetic click / keystroke | `Quartz.CGEventCreateMouseEvent` + `CGEventPost`; `pynput` Controllers | `pynput` is a declared dependency, used today only as a *listener* |
| Accessibility tree | `ApplicationServices.AXUIElementCreateApplication` | via `pyobjc-framework-cocoa`, declared |
| **Accessibility permission** | **already granted** — `AXIsProcessTrusted()` is True | the `hotkey`/`appfocus` event sources need it, and `docs/reference/configuration.md:280` already tells users to grant it |
| Window / app focus | `NSWorkspace`, already wired in `events/sources/appfocus.py` | |
| Browser control | Playwright + all browsers installed | **undeclared** — see the hygiene note |
| Image encode/resize | `pillow` | declared under the `visuals` extra |
| Consent-gated per-OS capability | `platform/elevation.py` + `get_elevation_provider()` | the pattern to copy |
| Telling the model what the host can do | `HostProfile` → `prompt_block()` → `tr_sysinfo` | has `elevation_mechanism`; would gain `gui_backend` |
| Approval surface | `agents/task_runner/permissions.py` tables + the consent registry | |

Two things are genuinely missing, and only two:

- **No image path into the model.** `core/tool_result.guard_tool_result` is
  `str → str`, and nothing in the tree ever constructs a multimodal content
  block (`image_url`, `{"type": "image"}`, a base64 source — zero occurrences).
  The configured models are all natively vision-capable; this is plumbing, not
  a model limit. Until it exists, **a screenshot tool is write-only**: the
  agent can take one and show it to the user, and can never see it itself.
- **No operation type for a GUI act.** `models/operations.py` has eight
  members, all filesystem/shell. A GUI verb would have to impersonate
  `EXECUTE` on the `/host` sentinel, which makes "click a button" and "run a
  shell command" indistinguishable in the audit log and in the user's approval
  prompt. Workable as a stopgap, wrong as a design.

## The decision: accessibility tree first

Read the UI as **text**, act on **elements**, not coordinates.

```
gui_tree(app="Spotify", depth=3)
  → window "Spotify"
      group "sidebar"
        row "Liked Songs"            [ax:412]
      table "tracklist"
        row 1  "Gangsta's Paradise"  "Ihsan Dincer"
        row 2  …

gui_click(ax="ax:412")
gui_type(text="…")
gui_keys("cmd+f")
```

Why this and not screenshots-plus-vision:

- **It needs no new plumbing.** Text in, text out — it fits the existing
  `str → str` tool contract, so it can ship without touching the multimodal
  gap.
- **It is more reliable than pixels.** An element reference survives a window
  move, a resize and a theme change; a coordinate does not.
- **It is cheaper.** No image per step.
- **It answers the actual question.** The AX tree of Spotify's desktop app
  exposes the track list, which is what the user asked for and which
  Spotify's AppleScript dictionary does not expose at all (it has playback
  controls and the current track, and no library access — so AppleScript would
  never have answered this question either).

Screenshots stay in the design, but as an artefact **for the user** — proof of
what the agent saw — registered through the existing `visuals` store. When the
image-into-context path is built, vision becomes a fallback for what AX cannot
express (canvas apps, games), not the primary mechanism.

## Shape of the implementation

1. **`yuyutsava/platform/gui.py`** — a `GuiProvider` protocol
   (`tree()`, `click()`, `type_text()`, `keys()`, `screenshot()`) with
   `get_gui_provider()` selecting a macOS implementation over Quartz/AX,
   and a `NullGuiProvider` elsewhere. Beside `elevation.py`, which is the
   established precedent for a capability that is per-OS, consent-gated from
   above, and reached through a narrow interface.
2. **`OperationType.GUI`** in `models/operations.py`, with rows in
   `permissions.py`: `EXTERNAL + GUI → PROMPT` at `CRITICAL`, and an
   `_ALTERNATIVES` entry pointing at the read-only `gui_tree`. Consent grants
   scope per app, not globally — "let it drive Spotify" must not mean "let it
   drive Mail".
3. **`HostProfile.gui_backend`** (`"quartz-ax" | None`), surfaced in
   `prompt_block()` and `tr_sysinfo`, so the model is *told* the capability
   exists. The agent could already have taken a screenshot via `tr_execute`
   and never knew; discoverability is half the failure.
4. **A `gui_*` tool family** registered like every other, so lazy discovery
   and `tool_search` work unchanged. `gui_tree` is read-only and auto-allowed;
   the acting verbs prompt.
5. **A bundled skill** — the user's explicit requirement, *"provide everything
   for the agent as a skill"*. `platforms: [macos]`, covering: read the tree
   before acting; address elements, never coordinates; one approval for a
   sequence rather than a prompt per click; how to wait for a UI to settle;
   when to prefer the app's own local data (`macos-app-local-data` already
   answers the Spotify question without any GUI at all); and when to prefer
   the browser. Plus the specific trap from this incident: **a tool that
   reports success while nothing happened is a failure** — check the tree
   changed, do not re-issue.
6. **Fix `chrome-cdp-user-auth`** to continue past authentication into
   interaction, and stop telling the agent to verify with screenshots it
   cannot see.

## Verification when it is built

Read-only first: `gui_tree` against Finder and Spotify, asserting the track
list is reachable and that a denied `gui_click` returns alternatives rather
than silence. Then one scripted end-to-end — open Spotify, click Liked Songs,
read the first ten rows — run with the consent prompt visible, because the
whole point is that the user stays in the loop for anything that acts.

## Hygiene note, independent of all this

**Playwright is installed in `.venv` but declared in neither `pyproject.toml`
nor `uv.lock`.** Any capability resting on it today disappears on the next
`uv sync`. Either declare it or stop depending on it.
