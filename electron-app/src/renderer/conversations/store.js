import { ConverseClient } from '../api/converse'
import { getBase, getSessionMessages } from '../api/client'
import { TokenSmoother } from '../lib/tokenSmoother'
import { audioPlayer, base64ToInt16 } from '../audio'
import { MicCapture } from '../audio/capture'

// Conversations outlive the components that show them.
//
// A ChatPanel unmounts constantly — closing the think pane, switching TODO
// cards, navigating to Settings, remounting on a chat switch. Before this
// store, its `useConverse` hook owned the socket, so every one of those killed
// the connection and (server-side) the turn with it. Now the daemon owns the
// run (see yuyutsava/daemon/turn_registry.py) and this module owns the *view*
// of it: a ConversationSession per conversation, held in a module-level Map,
// retained/released by whoever is currently rendering it.
//
// A released-but-busy session stays connected — that is the entire point:
// the agent is still working and we want its frames when the view comes back.
// An idle one lets go of the socket after IDLE_DISCONNECT_MS.

// Sessions with nothing looking at them and no turn running are disconnected
// and dropped after this long.
const IDLE_DISCONNECT_MS = 10 * 60 * 1000
// Hard cap on retained conversations (idle ones evicted oldest-first).
const MAX_SESSIONS = 24

let _mid = 0
const nextId = () => `m${++_mid}`

// The server composes board-selection references into the stored user text;
// the user only ever typed what follows it.
const SELECTION_RE = /^<selection-context>\n[\s\S]*?\n<\/selection-context>\n\n/
const stripSelection = (t) => (t || '').replace(SELECTION_RE, '')

export function conversationKey({ origin = 'cli', agent = null, card = null, resumeId = null } = {}) {
  return `${origin}|${agent || ''}|${card || ''}|${resumeId || ''}`
}

// ---------------------------------------------------------------------------
// Who is currently speaking — read by the titlebar transport button
// ---------------------------------------------------------------------------

let speaker = null
const speakerListeners = new Set()

export function subscribeSpeaker(fn) {
  speakerListeners.add(fn)
  return () => speakerListeners.delete(fn)
}
export function getSpeaker() { return speaker }

function setSpeaker(next) {
  if (speaker?.key === next?.key && speaker?.label === next?.label) return
  speaker = next
  for (const fn of speakerListeners) { try { fn(speaker) } catch { /* ignore */ } }
}

// ---------------------------------------------------------------------------
// Which conversations the user can actually SEE right now
// ---------------------------------------------------------------------------
//
// Ask routing needs "am I looking at the conversation that owns this ask?" —
// which is not the same question as "am I on the Chat tab". Two different chats
// both live on the Chat panel, and a card can hold many tinker threads. Getting
// that wrong in the permissive direction leaks a prompt into someone else's
// session; getting it wrong the other way silently swallows the notification
// and the agent waits forever. So visibility is tracked per *thread*, reported
// by the panel that is rendering it.

const visibleThreads = new Map()   // thread_id -> refcount
const visibleListeners = new Set()

export function subscribeVisibleThreads(fn) {
  visibleListeners.add(fn)
  return () => visibleListeners.delete(fn)
}

export function isThreadVisible(threadId) {
  return !!threadId && visibleThreads.has(threadId)
}

export function visibleThreadIds() { return [...visibleThreads.keys()] }

function setThreadVisible(threadId, visible) {
  if (!threadId) return
  const n = visibleThreads.get(threadId) || 0
  if (visible) visibleThreads.set(threadId, n + 1)
  else if (n <= 1) visibleThreads.delete(threadId)
  else visibleThreads.set(threadId, n - 1)
  for (const fn of visibleListeners) { try { fn() } catch { /* ignore */ } }
}

// ---------------------------------------------------------------------------

export class ConversationSession {
  constructor(key, { origin = 'cli', resumeId = null, agent = null, card = null } = {}) {
    this.key = key
    this.origin = origin
    this.agent = agent
    this.card = card
    this.resumeId = resumeId

    this.refs = 0
    this.listeners = new Set()
    this.idleTimer = null
    this.disposed = false
    this.touchedAt = Date.now()

    // Everything a subscriber renders. Replaced (never mutated) so
    // useSyncExternalStore can compare by identity.
    this.state = {
      messages: [],
      connected: false,
      busy: false,
      pendingAsk: null,   // { payload }
      hello: null,        // { session_id, thread_id, run, … }
      listening: false,   // mic capture active
      speaking: false,    // agent TTS playing
      playingId: null,    // id of the message whose audio is audible
      paused: false,      // that clip is user-paused
    }

    // The last per-thread `seq` we rendered. Sent as ?since_seq on every
    // reconnect so the daemon replays exactly the gap — this is what makes a
    // dropped socket (or a renderer reload) lossless instead of fatal.
    this.lastSeq = null
    this.streamingId = null
    this.smoother = null
    this.mic = null
    this.client = null
    this.bargeIn = false
    this.offPlayback = null
    // True while a mounted, *visible* panel is showing this conversation.
    // Drives ask routing: an ask renders inline only where the user can see it.
    this.visible = false

    // Stable identities — these end up in React dependency arrays.
    this.subscribe = this.subscribe.bind(this)
    this.getSnapshot = this.getSnapshot.bind(this)
    this._onMessage = this._onMessage.bind(this)
    this.actions = {
      send: this.send.bind(this),
      answerAsk: this.answerAsk.bind(this),
      interrupt: this.interrupt.bind(this),
      startVoice: this.startVoice.bind(this),
      stopVoice: this.stopVoice.bind(this),
      replay: this.replay.bind(this),
      togglePause: this.togglePause.bind(this),
      newSession: this.newSession.bind(this),
      getMicAnalyser: this.getMicAnalyser.bind(this),
    }

    this._hydrate()
    this._connect()
    // Nobody has retained it yet. If nobody ever does (a render that never
    // commits), the idle timer disposes it rather than leaving a socket open.
    this._armIdle()
  }

  // ---- subscription ---------------------------------------------------

  subscribe(fn) {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }

  getSnapshot() { return this.state }

  _set(patch) {
    this.state = { ...this.state, ...patch }
    for (const fn of this.listeners) { try { fn() } catch { /* ignore */ } }
  }

  _setMessages(fn) { this._set({ messages: fn(this.state.messages) }) }

  // ---- lifecycle ------------------------------------------------------

  retain() {
    this.refs += 1
    this.touchedAt = Date.now()
    if (this.idleTimer) { clearTimeout(this.idleTimer); this.idleTimer = null }
  }

  release() {
    this.refs = Math.max(0, this.refs - 1)
    this.touchedAt = Date.now()
    if (this.refs === 0) this._armIdle()
  }

  _armIdle() {
    if (this.idleTimer) clearTimeout(this.idleTimer)
    this.idleTimer = setTimeout(() => {
      this.idleTimer = null
      if (this.refs > 0) return
      // A turn is still running: hold the socket open. Re-check later — the
      // frames it produces are exactly what a returning view needs.
      if (this.state.busy) { this._armIdle(); return }
      this.dispose()
    }, IDLE_DISCONNECT_MS)
  }

  // Called by the panel rendering this conversation as it mounts/unmounts and
  // as its tab is shown/hidden.
  setVisible(visible) {
    const next = !!visible
    if (next === this.visible) return
    this.visible = next
    const tid = this.state.hello?.thread_id || this.resumeId
    if (tid) setThreadVisible(tid, next)
  }

  dispose() {
    if (this.disposed) return
    this.disposed = true
    if (this.visible) this.setVisible(false)
    if (this.idleTimer) { clearTimeout(this.idleTimer); this.idleTimer = null }
    this._stopSmoother()
    if (this.offPlayback) { this.offPlayback(); this.offPlayback = null }
    if (this.mic) { this.mic.stop(); this.mic = null }
    try { this.client?.disconnect() } catch { /* ignore */ }
    this.client = null
    if (sessions.get(this.key) === this) sessions.delete(this.key)
  }

  // ---- transport ------------------------------------------------------

  _connect() {
    const client = new ConverseClient(
      {
        onMessage: this._onMessage,
        onConnected: () => this._set({ connected: true }),
        onDisconnected: () => {
          // A dropped socket no longer ends anything: the run belongs to the
          // daemon. Surface whatever prose was buffered, keep `busy` exactly
          // as it is, and let ConverseClient reconnect — its handshake sends
          // ?since_seq and the server replays the gap.
          this._flushSmoother()
          this._set({ connected: false })
        },
        // The client gave up resuming a dead session (server rejected it
        // MAX_RESUME_FAILURES times in a row before any `hello`, e.g. a
        // freshly-created chat that got discarded after an early disconnect).
        // Surface exactly one clear message instead of the storm of raw
        // rejections that led here — the user picks "New chat" or a different
        // history entry to recover; we don't silently start one for them.
        onResumeExhausted: (message) => {
          console.warn('chat resume failed permanently:', message)
          this._flushSmoother()
          this._stopSmoother()
          this._finalizeStreaming()
          this._setMessages((cur) => [...cur, {
            id: nextId(), role: 'assistant', error: true, events: [],
            text: '⚠ this chat is no longer available — start a new one',
          }])
          this._set({ connected: false, busy: false, pendingAsk: null })
        },
      },
      { origin: this.origin, resumeId: this.resumeId, agent: this.agent, card: this.card },
    )
    client.setSinceSeq(this.lastSeq)
    this.client = client
    client.connect()
  }

  // Resume-history render: hydrate the past turns of a resumed thread so it
  // doesn't open empty. Voice turns carry an audio_url for ▶ replay; text chats
  // are prose only. Runs alongside the WS connect — the user hasn't sent
  // anything yet, so there's no ordering race with new turns.
  _hydrate() {
    if (!this.resumeId) return
    const forId = this.resumeId
    getSessionMessages(forId)
      .then((res) => {
        if (this.disposed || this.resumeId !== forId) return
        if (!res?.messages?.length || this.state.messages.length > 0) return
        this._set({
          messages: res.messages.map((m) => ({
            id: nextId(),
            role: m.role,
            // The verbatim transcript stores the server-composed selection
            // block ahead of what the user typed — strip it on hydration so
            // resumed threads match what the live bubble showed.
            text: m.role === 'user' ? stripSelection(m.text) : (m.text || ''),
            events: [],
            // Inline artifact cards made during the turn — the history endpoint
            // rebuilds them from the transcript's artifact_create tool results.
            artifacts: m.artifacts || [],
            // The server's audio_url is authoritative — on mixed text+voice
            // threads it carries the VOICE-store seq, which differs from this
            // row's transcript seq (rebuilding from m.seq would 404).
            audioUrl: m.audio_url ? `${getBase()}${m.audio_url}` : null,
          })),
        })
      })
      .catch(() => { /* history is best-effort — a fresh thread is fine */ })
  }

  // ---- streaming text -------------------------------------------------

  _appendToStreaming(patch) {
    const id = this.streamingId
    if (!id) return
    this._setMessages((cur) => cur.map((m) => (m.id === id ? patch(m) : m)))
  }

  _ensureStreaming() {
    if (this.streamingId) return
    const id = nextId()
    this.streamingId = id
    this._setMessages((cur) => [...cur, {
      id, role: 'assistant', text: '', events: [], images: [], artifacts: [], streaming: true,
    }])
  }

  // Mark the streaming message done and stop pointing at it. Captures the id
  // NOW and patches by that explicit id — must not go through
  // _appendToStreaming, whose read of this.streamingId happens after we've
  // nulled it, which would silently drop the streaming:false patch and leave
  // the typing dots spinning + hide the message actions forever.
  _finalizeStreaming() {
    const id = this.streamingId
    this.streamingId = null
    if (id) this._setMessages((cur) => cur.map((m) => (m.id === id ? { ...m, streaming: false } : m)))
  }

  // Reveal buffered prose immediately — call before rendering any non-prose
  // (tool/log) event so text stays in correct order, and at end of stream.
  _flushSmoother() { this.smoother?.flush() }

  // Tear down the smoother (drop pending text) at turn boundaries / teardown.
  _stopSmoother() { this.smoother?.stop(); this.smoother = null }

  _feedSmoother(text) {
    if (!this.smoother) {
      this.smoother = new TokenSmoother(
        (chunk) => this._appendToStreaming((m) => ({ ...m, text: m.text + chunk })),
      )
    }
    this.smoother.feed(text)
  }

  // ---- playback -------------------------------------------------------

  // Mark `id` as the audible message and watch the shared player so the toggle
  // flips back once it drains. One subscription, not a poll per session — see
  // audioPlayer.onChange.
  _watchPlayback(id) {
    this._set({ playingId: id })
    setSpeaker(this.describe())
    if (this.offPlayback) return
    this.offPlayback = audioPlayer.onChange(({ playing, paused }) => {
      if (!playing) {
        this.offPlayback?.()
        this.offPlayback = null
        this._set({ playingId: null, paused: false })
        if (getSpeaker()?.key === this.key) setSpeaker(null)
        return
      }
      if (paused !== this.state.paused) this._set({ paused })
    })
  }

  // Cut off whatever is playing and reset the toggle to ▶. audioPlayer.stop()
  // also clears a pending pause (a suspended context would mute future audio).
  _stopPlayback() {
    if (this.offPlayback) { this.offPlayback(); this.offPlayback = null }
    audioPlayer.stop()
    this._set({ playingId: null, paused: false })
    if (getSpeaker()?.key === this.key) setSpeaker(null)
  }

  // Where this conversation lives, for the titlebar transport's label + click.
  describe() {
    const chat = this.state.hello?.session_id || this.resumeId || null
    if (this.agent === 'tinker' && this.card) {
      return {
        key: this.key,
        label: 'TinkerAgent',
        nav: { panel: 'todos', params: { cardId: this.card, ...(chat ? { chat } : {}) } },
      }
    }
    if (this.origin === 'voice') {
      return { key: this.key, label: 'Voice', nav: { panel: 'voice', params: {} } }
    }
    return { key: this.key, label: 'Chat', nav: { panel: 'chat', params: {} } }
  }

  // ---- frames ---------------------------------------------------------

  _onMessage(msg) {
    // Every frame that belongs to the turn carries a monotonic per-thread seq.
    // Connection-scoped frames (pong, mic state) carry none and must not move
    // the resume cursor. Neither must `hello` — it reports where the *thread*
    // stands, not what we've rendered, and adopting it would make a socket that
    // drops part-way through the replay resume past the frames it never got.
    if (typeof msg.seq === 'number' && msg.type !== 'hello') {
      this.lastSeq = msg.seq
      this.client?.setSinceSeq(msg.seq)
    }

    switch (msg.type) {
      case 'hello': {
        this._set({ hello: msg })
        // Whether the server allows voice barge-in (talk-over). Default off:
        // while off we mute the mic and ignore interrupt-y events until the
        // reply finishes PLAYING, so background noise can't cut it off.
        this.bargeIn = !!msg.barge_in
        // Pin the live thread so a reconnect (socket drop / daemon restart)
        // resumes THIS conversation instead of starting a fresh one.
        if (msg.thread_id) {
          const prev = this.resumeId
          this.resumeId = msg.thread_id
          this.client?.setResumeId(msg.thread_id)
          // A visible panel registered under the old id (or none at all) —
          // move the visibility marker onto the thread we actually landed on.
          if (this.visible && prev !== msg.thread_id) {
            if (prev) setThreadVisible(prev, false)
            setThreadVisible(msg.thread_id, true)
          }
        }
        // The daemon tells us whether a turn is running on this thread — which
        // may well be one we never saw start (another view began it, or we were
        // reloaded mid-answer). Trust it over local state; the replayed frames
        // that follow refine it.
        const running = msg.run?.status === 'running'
        if (running) {
          this._set({ busy: true })
        } else if (this.state.busy) {
          this._flushSmoother()
          this._stopSmoother()
          this._finalizeStreaming()
          this._set({ busy: false, pendingAsk: null })
        }
        break
      }
      case 'turn_start': {
        // The user side of a turn, echoed by the daemon so a view that attached
        // mid-answer can render the bubble it never saw. Skip it when we're the
        // one who sent it (send() already added the bubble optimistically) or
        // when the voice path already echoed the transcript.
        this._set({ busy: true })
        const text = stripSelection(msg.text || '')
        if (!text) break
        // Already have it if we sent it ourselves, if the voice path echoed the
        // transcript, or if session hydration raced ahead of the replay. "Have
        // it" = the newest user bubble matches and nothing has answered it yet.
        const msgs = this.state.messages
        let lastUser = -1
        for (let i = msgs.length - 1; i >= 0; i--) {
          if (msgs[i].role === 'user') { lastUser = i; break }
        }
        const answered = msgs.slice(lastUser + 1).some((m) => m.role === 'assistant' && !m.streaming)
        if (lastUser >= 0 && msgs[lastUser].text === text && !answered) break
        this._setMessages((cur) => [...cur, { id: nextId(), role: 'user', text, events: [] }])
        break
      }
      case 'token':
        this._ensureStreaming()
        this._feedSmoother(msg.text || '')
        break
      case 'image': {
        // Inline artifact (chart/diagram/table/…) rendered by a vis_* tool this
        // turn. Attach it to the streaming assistant message so it renders inside
        // the bubble; create the message if the image somehow arrives first.
        this._flushSmoother()
        this._ensureStreaming()
        this._appendToStreaming((m) => ({
          ...m,
          images: [...(m.images || []), {
            visual_id: msg.visual_id, url: msg.url, kind: msg.kind, title: msg.title, mime: msg.mime,
          }],
        }))
        break
      }
      case 'artifact': {
        // Inline rich artifact (interactive HTML/JSX, doc, audio) made by
        // artifact_create this turn — attach it to the streaming assistant
        // message so it renders as a block card in the bubble, openable big.
        this._flushSmoother()
        this._ensureStreaming()
        this._appendToStreaming((m) => ({
          ...m,
          artifacts: [...(m.artifacts || []), {
            attachment_id: msg.attachment_id || msg.artifact_id,
            url: msg.url, kind: msg.kind, mime: msg.mime, title: msg.title,
          }],
        }))
        break
      }
      case 'tool_call':
        this._flushSmoother() // show buffered prose before the tool row
        this._appendToStreaming((m) => ({ ...m, events: [...m.events, { kind: 'tool_call', name: msg.name, args: msg.args }] }))
        break
      case 'tool_result':
        this._flushSmoother()
        this._appendToStreaming((m) => ({ ...m, events: [...m.events, { kind: 'tool_result', name: msg.name, preview: msg.preview }] }))
        break
      case 'log':
        // Surfaced as a transient event on the streaming message, or dropped —
        // a bare log ("preparing agent…", "(turn cancelled)") must not conjure
        // an empty assistant bubble of its own.
        this._flushSmoother()
        this._appendToStreaming((m) => ({ ...m, events: [...m.events, { kind: 'log', text: msg.text }] }))
        break
      case 'final':
        // Drain buffered prose, then snap to the canonical final text. A
        // correct final is always ≥ the streamed text (same chunks), so a
        // SHORTER final can only be a server-side truncation — keep the
        // accumulated text rather than snapping the bubble back to a stub.
        this._flushSmoother()
        if (msg.text) this._appendToStreaming((m) => (
          msg.text.length >= m.text.length ? { ...m, text: msg.text } : m
        ))
        break
      case 'ask':
        // The full ask record — the same shape the Inbox and the overlay get,
        // so all three render the one shared AskCard. Arriving on THIS thread's
        // channel is what makes it ours: an ask is only ever emitted to the
        // conversation that raised it, so an inline card can't leak into
        // somebody else's session.
        this._set({ pendingAsk: msg.ask || msg.payload || null })
        break
      case 'ask_resolved':
        // Answered somewhere — here, the Inbox, the overlay, or the CLI — or
        // the turn was cancelled. Either way the prompt is no longer live.
        if (!msg.ask_id || this.state.pendingAsk?.ask_id === msg.ask_id) {
          this._set({ pendingAsk: null })
        }
        break
      case 'speech_started':
        // The user is talking — cut off any agent audio still playing (barge-in).
        // But with barge-in off, a "speech onset" during playback is almost
        // always the agent's own audio echo / room noise: DON'T stop a reply
        // that's still playing, or it gets chopped mid-sentence. A real user
        // interrupt goes through the Stop button (interrupt()).
        if (this.bargeIn || !audioPlayer.isPlaying()) this._stopPlayback()
        break
      case 'transcript':
        // STT of the user's spoken utterance becomes the user turn.
        this._setMessages((cur) => [...cur, { id: nextId(), role: 'user', text: msg.text, events: [] }])
        this._set({ busy: true })
        break
      case 'audio_chunk':
        try {
          const pcm = base64ToInt16(msg.pcm)
          const sr = msg.sample_rate || 22050
          audioPlayer.enqueuePcm(pcm, sr)
          // Retain the chunk on the streaming assistant message so the Voice UI
          // can offer a ▶ replay of this turn's spoken reply.
          this._appendToStreaming((m) => ({ ...m, audioChunks: [...(m.audioChunks || []), pcm], audioSampleRate: sr }))
          // The live reply is now audible — light up this message's Stop toggle
          // and name this conversation as the one the titlebar transport drives.
          if (this.streamingId) this._watchPlayback(this.streamingId)
        } catch { /* ignore */ }
        break
      case 'speaking_start':
        this._set({ speaking: true })
        break
      case 'speaking_end':
        this._set({ speaking: false })
        break
      case 'turn_end':
        this._flushSmoother()
        this._stopSmoother()
        this._finalizeStreaming()
        this._set({ busy: false, speaking: false, pendingAsk: null })
        // Nothing is watching and nothing is running — start the idle countdown.
        if (this.refs === 0) this._armIdle()
        break
      case 'clarify':
        // Low-confidence ASR: the agent turn was skipped on purpose. Show a
        // gentle re-prompt instead of running on a garbled transcript. A
        // turn_end follows to release the busy/speaking state. Never let a stray
        // low-confidence "utterance" (usually background noise) stop a reply
        // that's still playing.
        if (this.bargeIn || !audioPlayer.isPlaying()) this._stopPlayback()
        this._setMessages((cur) => [...cur, {
          id: nextId(),
          role: 'assistant',
          text: msg.message || "I didn't quite catch that — could you say it again?",
          events: [],
          clarify: true,
        }])
        break
      case 'error':
        this._flushSmoother()
        this._stopSmoother()
        this._stopPlayback()
        this._setMessages((cur) => [...cur, { id: nextId(), role: 'assistant', text: `⚠ ${msg.message}`, events: [], error: true }])
        this._finalizeStreaming()
        this._set({ busy: false, speaking: false })
        break
      default:
        break
    }
  }

  // ---- actions --------------------------------------------------------

  // `context` (optional) carries board-selection references invisibly — the
  // local bubble and transcript render only the typed text. Returns whether
  // the frame actually left, so callers can consume one-shot context (chips)
  // only on success.
  send(text, { context } = {}) {
    const t = (text || '').trim()
    if (!t || this.state.busy) return false
    const ok = this.client?.sendText(t, context || null)
    if (!ok) {
      this._setMessages((cur) => [...cur, {
        id: nextId(), role: 'assistant', error: true, events: [],
        text: '⚠ not connected to the daemon — is it running? (restart it after updates)',
      }])
      return false
    }
    this._setMessages((cur) => [...cur, { id: nextId(), role: 'user', text: t, events: [] }])
    this._set({ busy: true })
    return true
  }

  // Answer the inline ask. Goes back over the socket carrying the ask_id, so
  // the daemon resolves the same durable record an Inbox or overlay answer
  // would — one code path, and every other surface clears in step.
  answerAsk(text) {
    this.client?.answerAsk(text, this.state.pendingAsk?.ask_id || null)
    this._set({ pendingAsk: null })
  }

  interrupt() {
    this.client?.interrupt()
    this._stopPlayback()
    this._flushSmoother()
    this._stopSmoother()
    // Fallback: if the server doesn't ack within a moment (e.g. socket wedged),
    // release the UI so the user isn't stuck on "working".
    setTimeout(() => {
      if (this.disposed) return
      this._finalizeStreaming()
      this._set({ busy: false })
    }, 1500)
  }

  // ---- voice (mic capture) --------------------------------------------

  async startVoice() {
    if (this.mic) return
    // ensureContext() must run from a user gesture to unlock playback.
    try { await audioPlayer.ensureContext() } catch { /* ignore */ }
    // Half-duplex mic: while the agent's reply is still PLAYING (client-side
    // playback lags server synthesis by seconds), don't stream mic frames to the
    // server — otherwise its own audio echo / room noise forms a "new utterance"
    // that interrupts the reply. Resumes the instant playback drains. When the
    // server enables barge-in, stream continuously so talk-over works.
    const mic = new MicCapture({
      onFrame: (int16) => {
        if (!this.bargeIn && audioPlayer.isPlaying()) return
        this.client?.sendAudio(int16)
      },
    })
    this.mic = mic
    try {
      await mic.start()
      this._set({ listening: true })
    } catch (e) {
      this.mic = null
      this._set({ listening: false })
      this._setMessages((cur) => [...cur, {
        id: nextId(), role: 'assistant', error: true, events: [],
        text: `⚠ microphone unavailable: ${e?.message || e}`,
      }])
    }
  }

  async stopVoice() {
    const mic = this.mic
    this.mic = null
    this._set({ listening: false })
    if (mic) {
      try { this.client?.endAudio() } catch { /* ignore */ }
      await mic.stop()
    }
  }

  getMicAnalyser() { return this.mic?.getAnalyser() || null }

  // Pause / resume the audible clip in place (live reply or replay): the audio
  // clock freezes, so playback resumes exactly where it stopped. No-op when
  // nothing is playing.
  async togglePause() {
    if (audioPlayer.isPaused()) {
      await audioPlayer.resume()
      this._set({ paused: false })
    } else if (audioPlayer.isPlaying()) {
      await audioPlayer.pause()
      this._set({ paused: true })
    }
  }

  // Play / stop a turn's spoken reply. Acts as a toggle: clicking the control on
  // the message that's currently audible cuts it off (so a long clip isn't a
  // hostage). In-session turns retain raw PCM chunks; a resumed turn instead
  // carries an audio_url to the persisted WAV.
  async replay(message) {
    if (this.state.playingId === message.id) { this._stopPlayback(); return }
    this._set({ paused: false }) // audioPlayer.stop() below clears the player-side pause
    try { await audioPlayer.ensureContext() } catch { /* ignore */ }
    const chunks = message?.audioChunks
    if (chunks && chunks.length > 0) {
      audioPlayer.stop()
      const total = chunks.reduce((n, c) => n + c.length, 0)
      const merged = new Int16Array(total)
      let off = 0
      for (const c of chunks) { merged.set(c, off); off += c.length }
      await audioPlayer.enqueuePcm(merged, message.audioSampleRate || 22050)
      this._watchPlayback(message.id)
      return
    }
    if (message?.audioUrl) {
      audioPlayer.stop()
      try { await audioPlayer.playUrl(message.audioUrl); this._watchPlayback(message.id) } catch { /* ignore */ }
    }
  }

  // Start a brand-new conversation in place (the per-view "New" button): stop
  // any audio/mic, drop the pinned thread, and reconnect fresh. The store entry
  // (and therefore every view of it) is reused — only the conversation rotates.
  newSession() {
    this._stopPlayback()
    this._flushSmoother()
    this._stopSmoother()
    if (this.mic) { this.mic.stop(); this.mic = null }
    try { this.client?.disconnect() } catch { /* ignore */ }
    this.client = null
    this.resumeId = null
    this.lastSeq = null
    this.streamingId = null
    this._set({
      messages: [], busy: false, pendingAsk: null, hello: null,
      listening: false, speaking: false, playingId: null, paused: false,
    })
    this._connect()
  }
}

// ---------------------------------------------------------------------------
// The map
// ---------------------------------------------------------------------------

const sessions = new Map()

export function acquireSession(key, opts) {
  let s = sessions.get(key)
  if (!s || s.disposed) {
    s = new ConversationSession(key, opts)
    sessions.set(key, s)
    evictIdle()
  }
  return s
}

function evictIdle() {
  if (sessions.size <= MAX_SESSIONS) return
  const idle = [...sessions.values()]
    .filter((s) => s.refs === 0 && !s.state.busy)
    .sort((a, b) => a.touchedAt - b.touchedAt)
  for (const s of idle) {
    if (sessions.size <= MAX_SESSIONS) break
    s.dispose()
  }
}

// Test/debug affordance — not used by the app.
export function _sessionsForTest() { return sessions }
