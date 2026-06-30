# YUYUTSAVA Voice Interface — What Was Built & How to Test It

> **Status:** All 7 phases are **code-complete and statically verified**
> (`py_compile`, `vite build`, `npx tsc --noEmit`). What remains is **live
> end-to-end verification** against a running daemon and **on-device mobile
> testing** — this document is your checklist for that.
>
> **Source plan (single source of truth):**
> [`~/.claude/plans/hey-i-want-to-cryptic-spark.md`]($HOME/.claude/plans/hey-i-want-to-cryptic-spark.md)
> — read the top **IMPLEMENTATION STATUS** block there for the per-phase done/pending detail.
>
> **Repos**
> - Backend: `$REPO` (branch `yuyutsava-daemon`)
> - Mobile: `$MOBILE_REPO` (branch `main`)
> - Electron app: `YUYUTSAVA-backend/electron-app`

---

## 0. The big picture

The goal was a **Siri-like voice mode**: talk to the orchestrator by voice, get
**text + playable audio** replies, with a hotkey + wake-word that pops a small
animated mic. The hard constraint: **reuse the existing CLI deepagent** — the
Voice Agent is the *same* agent the CLI drives; only the human↔agent I/O changes.
Orchestration, delegation, async subagents, proposals, HITL all work exactly as
they do in the terminal.

**How the reuse works (one paragraph):** the conversational turn loop was
extracted from the CLI REPL into `yuyutsava/conversation/service.py`
(`ConversationService`) — an I/O-agnostic loop over `astream_agent_iter` with two
pluggable hooks: `on_event` (output) and `ask_handler` (HITL). The daemon hosts
it via a lazy `ConversationManager` that builds **one** shared agent bundle on
first use and **attaches** to the daemon's existing async-subagent host (no second
host). The Electron app and the mobile app both drive it over
`WS /ws/converse` (also mounted at `/v1/ws/converse`). Text and voice are the
same socket; voice just layers STT in / TTS out on top.

---

## 1. Prerequisites & setup

### 1.1 Backend / daemon

```bash
cd $REPO
uv sync                       # installs deps; voice extras = webrtcvad-wheels etc.
# Optional voice extras if not already present:
uv sync --extra voice
```

Voice providers are **config-driven, not hardcoded** (Settings → "Voice" group):

| Concern    | Default          | Alternatives        | Env keys |
|------------|------------------|---------------------|----------|
| STT        | `faster_whisper` | `groq`              | `STT_PROVIDER`, `FASTER_WHISPER_MODEL`, `GROQ_WHISPER_MODEL` |
| TTS        | `piper`          | `elevenlabs`        | `TTS_PROVIDER`, `PIPER_MODEL`, `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` |
| Wake word  | `hey_jarvis`     | alexa, hey_mycroft… | `WAKE_WORDS`, `WAKE_THRESHOLD` |

> **Zero-config note:** if no Piper model is set, TTS falls back to macOS `say`,
> so you can hear replies with no setup on a Mac. STT (faster-whisper) downloads
> its model on first use.

Start the daemon:

```bash
uv run yuyutsava daemon            # add --verbose for streaming logs
# IMPORTANT: do NOT pass --no-ui — that kills the web API the apps connect to.
```

The daemon serves on `http://127.0.0.1:7654` by default. Loopback binds are
auth-exempt; a non-loopback bind needs `YUYUTSAVA_API_TOKEN`.

### 1.2 Storage backend (SQLite vs Postgres)

Everything works in **zero-config SQLite** mode. To test the durable path:

```bash
export YUYUTSAVA_STORAGE_BACKEND=postgres
# migrations auto-apply on boot; voice persistence needs migration v11 (see §7)
```

### 1.3 Electron app

```bash
cd $REPO/electron-app
npm install
npm run dev          # or your usual launch script
```

### 1.4 Mobile app (needs a custom dev build — NOT Expo Go)

The mobile voice path uses a native audio module
(`react-native-live-audio-stream`), so Expo Go won't work. Build a dev client:

```bash
cd $MOBILE_REPO
npm install
npx expo run:ios        # or: npx expo run:android   (creates the custom dev build)
```

In the app's **Settings**, set the **Daemon URL** to your machine's tailnet/LAN
IP (e.g. `http://100.x.y.z:7654`) and the bearer token if the daemon requires one.

---

## 2. What was built, phase by phase

### Phase 1 — DB-backed session `origin`
- `origin` column on sessions (SQLite store v2 auto-migrate + PG migration **v10**),
  threaded through the session layer and `GET /sessions?origin=`.
- `ConversationService` extracted (`yuyutsava/conversation/`); CLI REPL refactored
  onto it. `build_cli_agent_stack` → `build_agent_stack` (alias kept).

### Phase 2 — Daemon conversation host + WS + Electron text chat
- `ConversationManager` (lazy bundle, attaches to daemon async host) +
  `WS /ws/converse` (streaming, inline-ask HITL, barge-in/interrupt, `origin` query).
- Electron text chat live: `api/converse.js`, `hooks/useConverse.js`, real `ChatPanel.jsx`.

### Phase 2.1 — Chat refinements
- App chat uses `origin="ui"` (distinct from terminal `cli`).
- Sessions split into **CLI / UI Chats** columns; UI rows click → continue-in-UI (resume).
- Renderer `lib/tokenSmoother.js` for smooth char-by-char streaming.

### Phase 3 — Reusable sound/announcer subsystem
- `yuyutsava/audio_io/` — `earcons.py` (open/close/listening/done/error tones),
  `announcer.py` (serialized speak/earcon/stop queue, graceful when audio absent).
- `io/audio.stop_playback()` for barge-in. `VoiceChannel` speaks via the Announcer.
- Renderer mirror `electron-app/src/renderer/audio/index.js` (`audioPlayer` singleton).

### Phase 4 — Voice pipeline on the WS
- `audio_io/vad.py` (VAD segmenter), `audio_io/sentence.py` (token→sentence chunker),
  `audio_io/synth.py` (text→PCM), `daemon/web/voice_pipeline.py`.
- WS frames in: `audio` / `audio_end`. Out: `speech_started` / `transcript` /
  `speaking_start` / `audio_chunk` / `speaking_end`. **Barge-in** = speech during a
  turn cancels it + its TTS.
- Renderer `audio/capture.js` (AudioWorklet @16 kHz).

### Phase 5 — Voice UI, overlay, wake word
- **5a** — Main "Voice" panel (`components/voice/VoicePanel.jsx`): pulsing bluish
  mic, scrollable thread, ▶ replay, inline HITL, dismissible wake-word note.
- **5b** — Mini overlay (`overlay.js`/`VoiceOverlay.jsx`) + global hotkey
  (`Ctrl/Cmd+Shift+Y`, override `VOICE_HOTKEY`) + wake-word → SSE `event:"wake"` bridge.
- **5c** — Wake-word **onboarding** (first-run modal `WakeWordOnboarding.jsx`); choosing
  a word hot-applies via the events source params (no restart).
- **5d** — **Mobile voice** (`yuyutsava-mobile`): `src/api/converse.ts`,
  `src/audio/*`, `src/hooks/useConverse.ts`, `src/screens/VoiceScreen.tsx` + a 🎙️ tab.
  Reuses the same `/ws/converse` protocol; no backend changes.

### Phase 6 — Sessions Voice column + audio/text persistence
- **6a** — Third **Voice** column in Sessions (origin=voice); voice rows click →
  continue-in-UI **and** a "Copy CLI resume" button.
- **6b** — Persistence + resume-history:
  - New `voice_messages` table (PG migration **v11** + standalone SQLite store
    `storage/voice_store.py`) — one row per spoken turn (role/modality/text/audio_blob/sample_rate).
  - Agent TTS WAVs saved under `~/.yuyutsava/blobs/voice/<thread_id>/`
    (`audio_io/blobs.py`); **session-scoped** (deleted on session delete, *not*
    TTL-swept — so replay survives indefinitely). User side stores transcript text
    only (raw user audio off by default).
  - Endpoints: `GET /sessions/{id}/messages`, `GET /sessions/{id}/audio/{seq}`;
    `DELETE /sessions/{id}` now also drops voice rows + clips.
  - **Resume-history render** (closes the earlier "resume opens empty" gap): the
    Electron + mobile `useConverse` fetch `/sessions/{id}/messages` on resume and
    hydrate the bubbles; `replay()` plays in-session PCM **or** a persisted `audio_url`.

### Phase 7 — Settings editors
- `config_schema.py` Voice group: `depends_key` gating (only the active provider's
  fields show); `WAKE_WORDS` marked hot-apply.
- Electron `components/settings/WakeWordsEditor.jsx` — chip add/remove + presets +
  custom; hot-applies via the voice events source, Save persists to `.env`.
- Mobile `components/WakeWordsEditor.tsx` + `fetchEventsConfig`/`setVoiceWakeWords`
  → a "Voice — wake words" card in Settings.

---

## 3. Test plan — Electron (desktop)

Run these in order. Each row is a pass/fail check.

### 3.1 Text chat (Phase 2 / 2.1)
1. Start the daemon, open the Electron app, go to **Chat**.
2. Type a message → tokens **stream in smoothly** (char-by-char, not jumpy chunks).
3. Ask for something that needs background work (e.g. "research X and report back")
   → confirm an **async subagent** spawns (visible in the activity log / Tasks),
   proving orchestrator delegation is reused.
4. Trigger a permission/question → an **inline ask** appears in the chat; answer it
   in-band; the turn continues.
5. Press the interrupt/stop control mid-turn → the turn **cancels** cleanly.

### 3.2 Voice loop (Phase 4 / 5a)
1. Go to the **Voice** panel. The mic pulses (bluish aura) while listening.
2. Speak a short request → after you stop, a **transcript** bubble appears, then the
   agent **replies in text and speaks it** (you hear audio).
3. **Barge-in:** start talking while the agent is speaking → it **stops** and takes
   your new utterance.
4. **VAD auto-stop:** stop talking → silence ends the utterance and runs the turn
   (no button press needed).
5. Press **▶** on an assistant bubble → it **replays** that turn's spoken audio.

### 3.3 Overlay + hotkey + wake word (Phase 5b / 5c)
1. First run with no `WAKE_WORDS`: the **onboarding modal** appears in the Voice
   panel. Pick `hey_jarvis` → Enable. ("Not now" just dismisses.)
2. Minimize / unfocus the app, press **Ctrl/Cmd+Shift+Y** → the **animated
   transparent overlay** pops bottom-right with an open sound (not a white box).
3. Say your **wake word** (mic permitted) → the overlay/voice panel activates
   (SSE `event:"wake"` round-trip).
4. Say a stop phrase ("thank you" / "ok then" / "stop") or wait ~14s or press Esc →
   the overlay **dismisses** with a close sound.

### 3.4 Sessions: Voice column + resume (Phase 6)
1. After a voice conversation, open **Sessions** → it appears under the **Voice**
   column (CLI runs under **CLI**, app text chats under **UI Chats**).
2. On a Voice row: **click it** → it opens in-app and **re-renders the past turns**
   (not empty!). Press **▶** on an old assistant bubble → it **replays the stored
   TTS audio** (this is the cross-restart persistence — works even after a daemon
   restart).
3. On a Voice row: **"Copy CLI resume"** → paste in a terminal → it resumes the
   thread as a CLI chat.
4. Continue the conversation in-app → new turns append onto the same thread.
5. **Delete** a voice session → confirm its rows and on-disk clips are gone
   (`~/.yuyutsava/blobs/voice/<thread_id>/` removed).

### 3.5 Settings (Phase 7)
1. Settings → **Voice** group. Switch **TTS_PROVIDER** piper↔elevenlabs → only the
   relevant fields (Piper model vs ElevenLabs key/voice) show. Same for **STT_PROVIDER**.
2. Switch a provider and **Save** → you get a **restart prompt** (provider changes
   need a daemon restart). Restart → next voice turn uses the new provider.
3. **Wake words editor:** add a preset chip and a custom word; remove one. These
   **hot-apply with no restart** (the editor pushes to the voice events source).
   Re-trigger the wake word to confirm the new word works.

---

## 4. Test plan — Mobile (custom dev build)

> Needs the dev build from §1.4 and the daemon reachable at the configured URL.
> **Not yet device-verified** — these are the checks to run.

1. **Connection:** Settings → set Daemon URL + token → **Test connection** shows
   "✓ connected — yuyutsava <version>".
2. **Text chat:** on the 🎙️ Voice screen, use the text composer → tokens stream;
   replies appear.
3. **Voice:** tap the mic (grant the permission prompt) → speak → transcript +
   spoken reply. (TTS plays buffered **per-turn**, not per-sentence gapless — known
   limit.)
4. **Barge-in / stop:** tap stop while it's speaking → audio stops.
5. **Replay:** tap **▶** on an assistant bubble → it replays.
6. **Wake words (Settings):** the **Voice — wake words** card lists current words;
   add/remove → hot-applies on the daemon. (TTS/STT provider switches are **not**
   editable from the phone — they live in the host's `.env`.)

---

## 5. Test plan — CLI parity (regression)

The refactor must not change terminal behavior:

```bash
uv run yuyutsava chat --verbose          # interactive REPL behaves identically
uv run yuyutsava "<one-shot task>"        # one-shot run
uv run yuyutsava --resume <id> "<next>"   # resume a thread
```
Confirm streaming, HITL prompts, and delegation are unchanged from before.

> ⚠️ **Test-runner caution:** avoid the full `pytest` suite for quick checks — the
> agent-stack / create_app imports pull in langgraph/deepagents and take minutes.
> Targeted tests exist (`test/web/test_converse_ws.py`, `test/web/test_voice_ws.py`)
> but even those import the app. For fast iteration use `python3 -m py_compile`,
> `npx vite build`, `npx tsc --noEmit`.

---

## 6. End-to-end smoke (the one flow that exercises everything)

1. Start daemon (`uv run yuyutsava daemon --verbose`), open Electron.
2. Onboard a wake word → minimize → hotkey opens the overlay → say a request by voice.
3. Make the request need background work → confirm an async subagent runs.
4. Get a spoken + text reply; barge-in once; finish the turn.
5. Open Sessions → the convo is under **Voice**; reopen it → history + ▶ replay work.
6. Restart the daemon → reopen the same session → ▶ replay **still** plays (proves
   persisted audio). 
7. (Postgres mode) repeat 1–6 with `YUYUTSAVA_STORAGE_BACKEND=postgres`.

---

## 7. Database / migrations to confirm

| Backend  | What to check |
|----------|----------------|
| SQLite   | `~/.yuyutsava/state.db` has a `voice_messages` table; `sessions.db` sessions have an `origin` column. |
| Postgres | Migrations apply to **v11** on boot; `voice_messages` table exists with a `thread_id` FK CASCADE; `sessions.origin` exists (v10). |

Quick splits to eyeball:
```sql
SELECT origin, count(*) FROM sessions GROUP BY origin;     -- cli / ui / voice
SELECT role, count(*) FROM voice_messages GROUP BY role;   -- user / assistant
```
Audio blobs live at `~/.yuyutsava/blobs/voice/<thread_id>/*.wav`.

---

## 8. Known limitations (by design — not bugs)

1. **No live partial transcript.** faster-whisper isn't truly streaming, so STT
   emits a **final** transcript only at end of utterance (no word-by-word caption).
2. **Mobile TTS is per-turn buffered**, not per-sentence gapless; **no mobile
   overlay** (foreground screen only); **not yet device-verified**.
3. **Provider switches need a daemon restart** (the daemon reads `*_from_env` per
   new voice connection, but its `os.environ` only changes on restart; Save writes
   `.env`, it doesn't mutate the live process). **Only wake words hot-apply.**
4. **Mobile can't edit the host's `.env`** remotely → only wake words are editable
   from the phone; TTS/STT provider changes are desktop-only.
5. **One shared agent bundle** per daemon for all conversations (isolated by
   `thread_id`); fine for single-user, worth idle-evicting later for memory.
6. Raw **user audio is not stored** by default (privacy/size) — only the STT
   transcript text. Agent TTS audio is always stored (that's what ▶ replays).

---

## 9. If something breaks — where to look

| Symptom | Likely file |
|---------|-------------|
| WS won't connect from Electron | CSP `connect-src` in `electron-app/src/renderer/index.html`; daemon running w/o `--no-ui` |
| No audio out | `audio_io/announcer.py` / `synth.py`; TTS provider config; macOS `say` fallback |
| No transcript / mic dead | `audio_io/vad.py`; renderer `audio/capture.js`; mic permission |
| Resume opens empty | `GET /sessions/{id}/messages`; `useConverse` resume fetch; `voice_store`/`transcript_store` on `app.state` |
| ▶ replay silent after restart | blob exists under `blobs/voice/`? `GET /sessions/{id}/audio/{seq}`; `audioPlayer.playUrl` |
| Wake word not firing | events source `voice` enabled + `params.wake_words`; `WAKE_THRESHOLD`; openWakeWord model present |
| Settings won't apply | `reload_class` (most need restart); wake words use the events-source push |

---

*Plan & live status:* [`~/.claude/plans/hey-i-want-to-cryptic-spark.md`]($HOME/.claude/plans/hey-i-want-to-cryptic-spark.md)
