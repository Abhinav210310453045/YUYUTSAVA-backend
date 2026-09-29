import { ConverseClient } from '../api/converse'
import { getBase, getSessionMessages } from '../api/client'
import { TokenSmoother } from '../lib/tokenSmoother'
import { audioPlayer } from '../audio'
import { MicCapture } from '../audio/capture'
import { applyFrame } from './frames'
import {
  errorMessage,
  fromHistory,
  lastAssistantId,
  nextId,
  noticeMessage,
  patchMessage,
  streamingMessage,
  userMessage,
  withAsk,
  withoutAsk,
} from './messages'
import { getSpeaker, setSpeaker, setThreadVisible } from './presence'

// Conversations outlive the components that show them.
//
// A ChatPanel unmounts constantly — closing the think pane, switching TODO
// cards, navigating to Settings, remounting on a chat switch. Before this
// store, its `useConverse` hook owned the socket, so every one of those killed
// the connection and (server-side) the turn with it. Now the daemon owns the
// run (see yuyutsava/daemon/turn_registry.py) and this module owns the *view*
// of it: a ConversationSession per conversation — its socket, its state and
// the user's actions — held in the map in sessions.js and retained/released
// by whoever is currently rendering it. What each daemon frame does is in
// frames.js; the message list itself, as pure functions, in messages.js.
//
// A released-but-busy session stays connected — that is the entire point:
// the agent is still working and we want its frames when the view comes back.
// An idle one lets go of the socket after IDLE_DISCONNECT_MS.

// Sessions with nothing looking at them and no turn running are disconnected
// and dropped after this long.
const IDLE_DISCONNECT_MS = 10 * 60 * 1000

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
      // Every question waiting on this conversation, oldest first. `pendingAsk`
      // is kept as a derived alias of the head so existing consumers (the
      // voice overlay, useConverse callers) need no change.
      pendingAsks: [],
      pendingAsk: null,
      // A message typed while a turn was running: held, then sent when the
      // turn ends. Never silently dropped, never allowed to kill a tool call.
      queued: null,
      hello: null, // { session_id, thread_id, run, … }
      listening: false, // mic capture active
      speaking: false, // agent TTS playing
      playingId: null, // id of the message whose audio is audible
      paused: false, // that clip is user-paused
      // Context-window + spend reading from the daemon's context meter. Null
      // until the first model call of the session reports one; `hello` carries
      // the last known value so a panel opened between turns is not blank.
      usage: null,
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
      sendNow: this.sendNow.bind(this),
      retry: this.retry.bind(this),
      discardQueued: this.discardQueued.bind(this),
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

  getSnapshot() {
    return this.state
  }

  _set(patch) {
    this.state = { ...this.state, ...patch }
    // `pendingAsk` is derived, always the head of the queue. Keeping it in
    // sync here means every existing reader (the voice overlay, useConverse
    // consumers) keeps working while the chat renders the whole queue.
    if ('pendingAsks' in patch) {
      this.state.pendingAsk = this.state.pendingAsks[0] || null
    }
    for (const fn of this.listeners) {
      try {
        fn()
      } catch {
        /* ignore */
      }
    }
  }

  _setMessages(fn) {
    this._set({ messages: fn(this.state.messages) })
  }

  // ---- lifecycle ------------------------------------------------------

  retain() {
    this.refs += 1
    this.touchedAt = Date.now()
    if (this.idleTimer) {
      clearTimeout(this.idleTimer)
      this.idleTimer = null
    }
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
      if (this.state.busy) {
        this._armIdle()
        return
      }
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
    if (this.idleTimer) {
      clearTimeout(this.idleTimer)
      this.idleTimer = null
    }
    this._stopSmoother()
    if (this.offPlayback) {
      this.offPlayback()
      this.offPlayback = null
    }
    if (this.mic) {
      this.mic.stop()
      this.mic = null
    }
    try {
      this.client?.disconnect()
    } catch {
      /* ignore */
    }
    this.client = null
    this.onDispose?.()
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
          this._setMessages((cur) => [
            ...cur,
            errorMessage('⚠ this chat is no longer available — start a new one'),
          ])
          this._set({ connected: false, busy: false, pendingAsks: [] })
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
        this._set({ messages: fromHistory(res, getBase()) })
      })
      .catch(() => {
        /* history is best-effort — a fresh thread is fine */
      })
  }

  // ---- streaming text -------------------------------------------------

  _appendToStreaming(patch) {
    const id = this.streamingId
    if (!id) return
    this._setMessages((cur) => patchMessage(cur, id, patch))
  }

  _ensureStreaming() {
    if (this.streamingId) return
    const id = nextId()
    this.streamingId = id
    this._setMessages((cur) => [...cur, streamingMessage(id)])
  }

  // Mark the streaming message done and stop pointing at it. Captures the id
  // NOW and patches by that explicit id — must not go through
  // _appendToStreaming, whose read of this.streamingId happens after we've
  // nulled it, which would silently drop the streaming:false patch and leave
  // the typing dots spinning + hide the message actions forever.
  _finalizeStreaming() {
    const id = this.streamingId
    this.streamingId = null
    if (id) this._setMessages((cur) => patchMessage(cur, id, (m) => ({ ...m, streaming: false })))
  }

  _lastAssistantId() {
    return lastAssistantId(this.state.messages)
  }

  _appendToLastAssistant(patch) {
    const id = this.streamingId || this._lastAssistantId()
    if (!id) return
    this._setMessages((cur) => patchMessage(cur, id, patch))
  }

  _pushNotice(text, level = 'info', extra = {}) {
    if (!text) return
    this._setMessages((cur) => [...cur, noticeMessage(text, level, extra)])
  }

  // ---- pending asks ---------------------------------------------------
  //
  // A queue, not a slot. It was a single field, so a second question silently
  // replaced the first — and the overwritten one went on blocking its agent
  // with no way to answer it except the Inbox.

  _pushAsk(ask) {
    if (!ask) return
    this._set({ pendingAsks: withAsk(this.state.pendingAsks, ask) })
  }

  _resolveAsk(askId) {
    this._set({ pendingAsks: withoutAsk(this.state.pendingAsks, askId) })
  }

  // Reveal buffered prose immediately — call before rendering any non-prose
  // (tool/log) event so text stays in correct order, and at end of stream.
  _flushSmoother() {
    this.smoother?.flush()
  }

  // Tear down the smoother (drop pending text) at turn boundaries / teardown.
  _stopSmoother() {
    this.smoother?.stop()
    this.smoother = null
  }

  _feedSmoother(text) {
    if (!this.smoother) {
      this.smoother = new TokenSmoother((chunk) =>
        this._appendToStreaming((m) => ({ ...m, text: m.text + chunk })),
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
    if (this.offPlayback) {
      this.offPlayback()
      this.offPlayback = null
    }
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
    applyFrame(this, msg)
  }

  // ---- actions --------------------------------------------------------

  // `context` (optional) carries board-selection references invisibly — the
  // local bubble and transcript render only the typed text. Returns whether
  // the frame actually left, so callers can consume one-shot context (chips)
  // only on success.
  // Send, or park it. Typing mid-turn used to be dropped on the floor here
  // (`if busy: return false`) — and the daemon's own path was worse: a message
  // that did land cancelled the running tool call, and the fabricated
  // "cancelled" result wedged the thread so nothing answered again.
  //
  // Now a mid-turn message is QUEUED and sent the moment the turn ends, so a
  // tool call is never killed by accident. `sendNow` is the explicit escape
  // hatch: interrupt, then send — safe because every turn repairs an
  // interrupted history before it starts.
  send(text, { context } = {}) {
    const t = (text || '').trim()
    if (!t) return false
    if (this.state.busy) {
      this._set({ queued: { text: t, context: context || null } })
      return true
    }
    const ok = this.client?.sendText(t, context || null)
    if (!ok) {
      this._setMessages((cur) => [
        ...cur,
        errorMessage('⚠ not connected to the daemon — is it running? (restart it after updates)'),
      ])
      return false
    }
    this._setMessages((cur) => [...cur, userMessage(t)])
    this._set({ busy: true, queued: null })
    return true
  }

  // Interrupt the running turn and send the queued (or given) message. The
  // turn_end handler flushes the queue, and run_turn repairs the interrupted
  // tool call before it sends — so "continue where it got interrupted" is a
  // decision the agent makes with accurate history rather than a guess.
  sendNow(text = null, { context } = {}) {
    const t = (text || this.state.queued?.text || '').trim()
    if (!t) return false
    this._set({ queued: { text: t, context: context || this.state.queued?.context || null } })
    if (this.state.busy) {
      this.interrupt()
      return true
    }
    return this._flushQueued()
  }

  // A notice's Retry: the model runs again on the conversation and nothing is
  // sent, so no user bubble is added. Refused mid-turn, like the daemon does.
  retry() {
    if (this.state.busy) return false
    const ok = this.client?.retry()
    if (ok) this._set({ busy: true })
    return !!ok
  }

  discardQueued() {
    this._set({ queued: null })
  }

  _flushQueued() {
    const q = this.state.queued
    if (!q) return false
    this._set({ queued: null })
    return this.send(q.text, { context: q.context })
  }

  // Answer the inline ask. Goes back over the socket carrying the ask_id, so
  // the daemon resolves the same durable record an Inbox or overlay answer
  // would — one code path, and every other surface clears in step.
  answerAsk(text, askId = null) {
    // The id is explicit so a queue of questions can be answered in any
    // order. Falls back to the head, which is what a single-card caller
    // means. Answering without an id while several were pending used to
    // resolve whichever had survived the overwrite — and leave the others
    // blocking their agents forever.
    const id = askId || this.state.pendingAsk?.ask_id || null
    this.client?.answerAsk(text, id)
    this._resolveAsk(id)
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
    try {
      await audioPlayer.ensureContext()
    } catch {
      /* ignore */
    }
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
      this._setMessages((cur) => [
        ...cur,
        errorMessage(`⚠ microphone unavailable: ${e?.message || e}`),
      ])
    }
  }

  async stopVoice() {
    const mic = this.mic
    this.mic = null
    this._set({ listening: false })
    if (mic) {
      try {
        this.client?.endAudio()
      } catch {
        /* ignore */
      }
      await mic.stop()
    }
  }

  getMicAnalyser() {
    return this.mic?.getAnalyser() || null
  }

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
    if (this.state.playingId === message.id) {
      this._stopPlayback()
      return
    }
    this._set({ paused: false }) // audioPlayer.stop() below clears the player-side pause
    try {
      await audioPlayer.ensureContext()
    } catch {
      /* ignore */
    }
    const chunks = message?.audioChunks
    if (chunks && chunks.length > 0) {
      audioPlayer.stop()
      const total = chunks.reduce((n, c) => n + c.length, 0)
      const merged = new Int16Array(total)
      let off = 0
      for (const c of chunks) {
        merged.set(c, off)
        off += c.length
      }
      await audioPlayer.enqueuePcm(merged, message.audioSampleRate || 22050)
      this._watchPlayback(message.id)
      return
    }
    if (message?.audioUrl) {
      audioPlayer.stop()
      try {
        await audioPlayer.playUrl(message.audioUrl)
        this._watchPlayback(message.id)
      } catch {
        /* ignore */
      }
    }
  }

  // Start a brand-new conversation in place (the per-view "New" button): stop
  // any audio/mic, drop the pinned thread, and reconnect fresh. The store entry
  // (and therefore every view of it) is reused — only the conversation rotates.
  newSession() {
    this._stopPlayback()
    this._flushSmoother()
    this._stopSmoother()
    if (this.mic) {
      this.mic.stop()
      this.mic = null
    }
    try {
      this.client?.disconnect()
    } catch {
      /* ignore */
    }
    this.client = null
    this.resumeId = null
    this.lastSeq = null
    this.streamingId = null
    this._set({
      messages: [],
      busy: false,
      // A message queued for the old conversation is not the new one's.
      queued: null,
      pendingAsks: [],
      hello: null,
      listening: false,
      speaking: false,
      playingId: null,
      paused: false,
    })
    this._connect()
  }
}
