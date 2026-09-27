// What each frame from the daemon does to a conversation — one handler per
// frame type, so changing how one kind renders means reading only that one.
// Handlers receive the session and use its message and playback operations;
// the list transformations themselves live in messages.js.

import { audioPlayer, base64ToInt16 } from '../audio'
import { alreadyShown, errorMessage, nextId, stripSelection, userMessage } from './messages'
import { setThreadVisible } from './presence'

const HANDLERS = {
  hello(s, msg) {
    // Adopt the handshake's usage snapshot, but never overwrite a live one
    // with null: a reconnect mid-turn would blank a panel that is correct.
    s._set(msg.usage ? { hello: msg, usage: msg.usage } : { hello: msg })
    // Whether the server allows voice barge-in (talk-over). Default off:
    // while off we mute the mic and ignore interrupt-y events until the
    // reply finishes PLAYING, so background noise can't cut it off.
    s.bargeIn = !!msg.barge_in
    // Pin the live thread so a reconnect (socket drop / daemon restart)
    // resumes THIS conversation instead of starting a fresh one.
    if (msg.thread_id) {
      const prev = s.resumeId
      s.resumeId = msg.thread_id
      s.client?.setResumeId(msg.thread_id)
      // A visible panel registered under the old id (or none at all) —
      // move the visibility marker onto the thread we actually landed on.
      if (s.visible && prev !== msg.thread_id) {
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
      s._set({ busy: true })
    } else if (s.state.busy) {
      s._flushSmoother()
      s._stopSmoother()
      s._finalizeStreaming()
      s._set({ busy: false, pendingAsks: [] })
    }
  },
  turn_start(s, msg) {
    // The user side of a turn, echoed by the daemon so a view that attached
    // mid-answer can render the bubble it never saw. Skip it when we're the
    // one who sent it (send() already added the bubble optimistically) or
    // when the voice path already echoed the transcript.
    s._set({ busy: true })
    const text = stripSelection(msg.text || '')
    if (!text) return
    if (alreadyShown(s.state.messages, text)) return
    s._setMessages((cur) => [...cur, userMessage(text)])
  },
  token(s, msg) {
    s._ensureStreaming()
    s._feedSmoother(msg.text || '')
  },
  image(s, msg) {
    // Inline artifact (chart/diagram/table/…) rendered by a vis_* tool this
    // turn. Attach it to the streaming assistant message so it renders inside
    // the bubble; create the message if the image somehow arrives first.
    s._flushSmoother()
    s._ensureStreaming()
    s._appendToStreaming((m) => ({
      ...m,
      images: [
        ...(m.images || []),
        {
          visual_id: msg.visual_id,
          url: msg.url,
          kind: msg.kind,
          title: msg.title,
          mime: msg.mime,
        },
      ],
    }))
  },
  artifact(s, msg) {
    // Inline rich artifact (interactive HTML/JSX, doc, audio) made by
    // artifact_create this turn — attach it to the streaming assistant
    // message so it renders as a block card in the bubble, openable big.
    s._flushSmoother()
    s._ensureStreaming()
    s._appendToStreaming((m) => ({
      ...m,
      artifacts: [
        ...(m.artifacts || []),
        {
          attachment_id: msg.attachment_id || msg.artifact_id,
          url: msg.url,
          kind: msg.kind,
          mime: msg.mime,
          title: msg.title,
        },
      ],
    }))
  },
  tool_call(s, msg) {
    // Flush the prose, attach the tool row, then CLOSE the bubble so the
    // next token opens a new one. Without the close, a whole turn of
    // prose -> tool -> prose -> tool collapsed into one blob with every
    // tool row underneath it, and the reasoning between steps was
    // unreadable. This is what the CLI renderer does (`self._md.flush()`
    // before printing the tool line) and why its transcript reads right.
    s._flushSmoother()
    s._appendToStreaming((m) => ({
      ...m,
      events: [...m.events, { kind: 'tool_call', name: msg.name, args: msg.args }],
    }))
    s._finalizeStreaming()
  },
  tool_result(s, msg) {
    // Attach to the block that issued the call, which tool_call just
    // closed — not to a new bubble that has no text of its own.
    s._flushSmoother()
    s._appendToLastAssistant((m) => ({
      ...m,
      events: [...m.events, { kind: 'tool_result', name: msg.name, preview: msg.preview }],
    }))
  },
  log(s, msg) {
    // Attach to the block in flight when there is one; otherwise show it
    // as its own row. It used to be DROPPED in that case — which is how
    // four consecutive "no assistant text" warnings reached the user as
    // silence on a wedged thread.
    s._flushSmoother()
    if (s.streamingId || s._lastAssistantId()) {
      s._appendToLastAssistant((m) => ({
        ...m,
        events: [...m.events, { kind: 'log', text: msg.text }],
      }))
    } else {
      s._pushNotice(msg.text, 'info')
    }
  },
  notice(s, msg) {
    // Never attached, never dropped: a notice is the channel for "the
    // model returned nothing", and it has to be visible on its own.
    s._flushSmoother()
    s._pushNotice(msg.text, msg.level || 'info')
  },
  final(s, msg) {
    // Drain buffered prose and stop. `final` carries only the LAST
    // assistant message (streaming.last_assistant_text), not the turn, so
    // the old "snap to the canonical text" step replaced every earlier
    // block with the closing one whenever the comparison happened to
    // favour it. The token stream is the transcript — the CLI ignores
    // `final.text` entirely for exactly this reason.
    //
    // The one case it still earns its keep: nothing streamed at all (a
    // replayed turn whose tokens fell off the ring), where there is no
    // accumulated text to lose.
    s._flushSmoother()
    if (msg.text && !s.streamingId && !s._lastAssistantId()) {
      s._ensureStreaming()
      s._appendToStreaming((m) => ({ ...m, text: msg.text }))
    }
  },
  ask(s, msg) {
    // The full ask record — the same shape the Inbox and the overlay get,
    // so all three render the one shared AskCard. Arriving on THIS thread's
    // channel is what makes it ours: an ask is only ever emitted to the
    // conversation that raised it, so an inline card can't leak into
    // somebody else's session.
    s._pushAsk(msg.ask || msg.payload || null)
  },
  ask_resolved(s, msg) {
    // Answered somewhere — here, the Inbox, the overlay, or the CLI — or
    // the turn was cancelled. Either way the prompt is no longer live.
    s._resolveAsk(msg.ask_id)
  },
  speech_started(s, _msg) {
    // The user is talking — cut off any agent audio still playing (barge-in).
    // But with barge-in off, a "speech onset" during playback is almost
    // always the agent's own audio echo / room noise: DON'T stop a reply
    // that's still playing, or it gets chopped mid-sentence. A real user
    // interrupt goes through the Stop button (interrupt()).
    if (s.bargeIn || !audioPlayer.isPlaying()) s._stopPlayback()
  },
  transcript(s, msg) {
    // STT of the user's spoken utterance becomes the user turn.
    s._setMessages((cur) => [...cur, userMessage(msg.text)])
    s._set({ busy: true })
  },
  audio_chunk(s, msg) {
    try {
      const pcm = base64ToInt16(msg.pcm)
      const sr = msg.sample_rate || 22050
      audioPlayer.enqueuePcm(pcm, sr)
      // Retain the chunk on the streaming assistant message so the Voice UI
      // can offer a ▶ replay of this turn's spoken reply.
      s._appendToStreaming((m) => ({
        ...m,
        audioChunks: [...(m.audioChunks || []), pcm],
        audioSampleRate: sr,
      }))
      // The live reply is now audible — light up this message's Stop toggle
      // and name this conversation as the one the titlebar transport drives.
      if (s.streamingId) s._watchPlayback(s.streamingId)
    } catch {
      /* ignore */
    }
  },
  // Context-meter reading, twice per model call. Ephemeral on the wire —
  // never replayed — so this is the only place it lands, and the last one
  // simply wins.
  usage(s, msg) {
    s._set({ usage: msg })
  },
  speaking_start(s, _msg) {
    s._set({ speaking: true })
  },
  speaking_end(s, _msg) {
    s._set({ speaking: false })
  },
  turn_end(s, _msg) {
    s._flushSmoother()
    s._stopSmoother()
    s._finalizeStreaming()
    s._set({ busy: false, speaking: false, pendingAsks: [] })
    // Anything typed during the turn goes now. Deferred a tick so `busy`
    // is observably false first — otherwise send() would re-queue it.
    if (s.state.queued) setTimeout(() => s._flushQueued(), 0)
    // Nothing is watching and nothing is running — start the idle countdown.
    if (s.refs === 0) s._armIdle()
  },
  clarify(s, msg) {
    // Low-confidence ASR: the agent turn was skipped on purpose. Show a
    // gentle re-prompt instead of running on a garbled transcript. A
    // turn_end follows to release the busy/speaking state. Never let a stray
    // low-confidence "utterance" (usually background noise) stop a reply
    // that's still playing.
    if (s.bargeIn || !audioPlayer.isPlaying()) s._stopPlayback()
    s._setMessages((cur) => [
      ...cur,
      {
        id: nextId(),
        role: 'assistant',
        text: msg.message || "I didn't quite catch that — could you say it again?",
        events: [],
        clarify: true,
      },
    ])
  },
  error(s, msg) {
    s._flushSmoother()
    s._stopSmoother()
    s._stopPlayback()
    s._setMessages((cur) => [...cur, errorMessage(`⚠ ${msg.message}`)])
    s._finalizeStreaming()
    s._set({ busy: false, speaking: false })
  },
}

// Apply one frame. Every frame that belongs to the turn carries a monotonic
// per-thread seq. Connection-scoped frames (pong, mic state) carry none and must
// not move the resume cursor. Neither must `hello` — it reports where the
// *thread* stands, not what we've rendered, and adopting it would make a socket
// that drops part-way through the replay resume past the frames it never got.
export function applyFrame(s, msg) {
  if (typeof msg.seq === 'number' && msg.type !== 'hello') {
    s.lastSeq = msg.seq
    s.client?.setSinceSeq(msg.seq)
  }
  HANDLERS[msg.type]?.(s, msg)
}

export const FRAME_TYPES = Object.freeze(Object.keys(HANDLERS))
