import React from 'react'

// The bottom of a chat: the queued-message banner, the composer (draft, mic,
// send/stop) and the voice status line.
export default function ChatComposer({
  draft,
  setDraft,
  onKeyDown,
  onSubmit,
  busy,
  placeholder,
  queued,
  sendNow,
  discardQueued,
  showVoice,
  listening,
  speaking,
  startVoice,
  stopVoice,
  interrupt,
}) {
  return (
    <>
      {/* A message typed mid-turn is held, not dropped and not allowed to kill
        the running tool call. Both choices are offered explicitly, because
        silently doing either is what made the agent look broken. */}
      {queued && (
        <div
          style={{
            margin: '0 24px 8px',
            padding: '8px 12px',
            position: 'relative',
            zIndex: 1,
            borderRadius: 'var(--radius-card)',
            flexShrink: 0,
            border: '1px solid var(--border-amber)',
            background: 'rgba(255,255,255,0.03)',
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            fontSize: 12,
          }}
        >
          <span style={{ color: 'var(--neon-amber)', fontFamily: 'var(--font-mono)' }}>⏸</span>
          <span
            style={{
              flex: 1,
              color: 'var(--text-secondary)',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            queued — sends when this turn ends: “{queued.text}”
          </span>
          <button
            onClick={() => sendNow()}
            title="Interrupt the turn and send now"
            style={{
              padding: '3px 10px',
              borderRadius: 'var(--radius-btn)',
              cursor: 'pointer',
              fontFamily: 'var(--font-mono)',
              fontSize: 10,
              background: 'rgba(var(--accent-rgb), 0.12)',
              border: '1px solid rgba(var(--accent-rgb), 0.3)',
              color: 'var(--neon-green)',
            }}
          >
            send now
          </button>
          <button
            onClick={discardQueued}
            title="Discard the queued message"
            style={{
              padding: '3px 8px',
              borderRadius: 'var(--radius-btn)',
              cursor: 'pointer',
              background: 'transparent',
              border: '1px solid var(--border-subtle)',
              color: 'var(--text-muted)',
              fontSize: 11,
            }}
          >
            ✕
          </button>
        </div>
      )}

      {/* composer */}
      <div
        style={{
          display: 'flex',
          gap: 8,
          padding: '12px 24px 18px',
          position: 'relative',
          zIndex: 1,
          borderTop: '1px solid var(--border-subtle)',
        }}
      >
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          rows={1}
          placeholder={busy ? 'agent is working — your message will be queued' : placeholder}
          style={{
            flex: 1,
            resize: 'none',
            background: 'var(--glass-bg)',
            color: 'var(--text-primary)',
            border: '1px solid var(--glass-border)',
            borderRadius: 22,
            padding: '11px 16px',
            fontSize: 13,
            fontFamily: 'var(--font-ui)',
            maxHeight: 120,
            backdropFilter: 'blur(var(--glass-blur))',
            outline: 'none',
          }}
        />
        {showVoice && (
          <button
            onClick={() => (listening ? stopVoice() : startVoice())}
            title={listening ? 'stop microphone' : 'talk to the agent'}
            className="tap-pop"
            style={micBtnStyle(listening)}
          >
            {listening ? '● mic' : '🎙 mic'}
          </button>
        )}
        {busy ? (
          <button onClick={interrupt} className="tap-pop" style={btnStyle(false)}>
            stop
          </button>
        ) : (
          <button
            onClick={onSubmit}
            disabled={!draft.trim()}
            className="grad-animated tap-pop"
            style={sendStyle(!!draft.trim())}
          >
            send
          </button>
        )}
      </div>
      {(listening || speaking) && (
        <div
          style={{
            padding: '0 24px 10px',
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
            position: 'relative',
            zIndex: 1,
            color: speaking ? 'var(--neon-green)' : 'var(--neon-amber)',
          }}
        >
          {speaking ? '▸ agent speaking…' : '● listening — speak, then pause (or stop the mic)'}
        </div>
      )}
    </>
  )
}

function btnStyle(primary) {
  return {
    fontFamily: 'var(--font-mono)',
    fontSize: 12,
    cursor: 'pointer',
    padding: '8px 14px',
    borderRadius: 10,
    background: primary ? 'rgba(var(--accent-rgb),0.1)' : 'rgba(255,51,102,0.08)',
    border: `1px solid ${primary ? 'rgba(var(--accent-rgb),0.3)' : 'rgba(255,51,102,0.3)'}`,
    color: primary ? 'var(--neon-green)' : 'var(--neon-red)',
  }
}

function sendStyle(enabled) {
  return {
    fontFamily: 'var(--font-mono)',
    fontSize: 12,
    fontWeight: 700,
    cursor: enabled ? 'pointer' : 'default',
    padding: '8px 18px',
    borderRadius: 22,
    border: 'none',
    background: enabled ? 'var(--grad-accent)' : 'var(--glass-bg)',
    color: enabled ? '#04120a' : 'var(--text-dim)',
    opacity: enabled ? 1 : 0.6,
    boxShadow: enabled ? '0 2px 14px rgba(var(--accent-rgb),0.3)' : 'none',
  }
}

function micBtnStyle(active) {
  return {
    fontFamily: 'var(--font-mono)',
    fontSize: 12,
    cursor: 'pointer',
    padding: '8px 12px',
    borderRadius: 22,
    background: active ? 'rgba(120,160,255,0.18)' : 'rgba(120,160,255,0.06)',
    border: `1px solid rgba(120,160,255,${active ? 0.5 : 0.3})`,
    color: 'var(--text-info)',
  }
}
