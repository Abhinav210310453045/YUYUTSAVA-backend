import React, { useEffect, useRef, useState } from 'react'
import { useConverse } from '../../hooks/useConverse'

function ToolEvents({ events }) {
  if (!events || events.length === 0) return null
  return (
    <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 2 }}>
      {events.map((e, i) => {
        if (e.kind === 'tool_call') {
          const args = e.args ? JSON.stringify(e.args).slice(0, 100) : ''
          return (
            <div key={i} style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-muted)' }}>
              · {e.name}{args ? ` ${args}` : ''}
            </div>
          )
        }
        if (e.kind === 'tool_result') {
          return (
            <div key={i} style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-dim)' }}>
              ↳ {e.name}: {(e.preview || '').slice(0, 80)}
            </div>
          )
        }
        return (
          <div key={i} style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--neon-amber)' }}>
            {e.text}
          </div>
        )
      })}
    </div>
  )
}

function Bubble({ m }) {
  const isUser = m.role === 'user'
  return (
    <div style={{ display: 'flex', justifyContent: isUser ? 'flex-end' : 'flex-start' }}>
      <div style={{
        maxWidth: '78%',
        background: isUser ? 'rgba(0,255,136,0.08)' : 'var(--bg-card)',
        border: `1px solid ${m.error ? 'rgba(255,51,102,0.4)' : isUser ? 'rgba(0,255,136,0.2)' : 'var(--border-card)'}`,
        borderRadius: 'var(--radius-card)',
        padding: '10px 14px',
        color: m.error ? 'var(--neon-red)' : 'var(--text-primary)',
        fontSize: 13,
        lineHeight: 1.6,
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
      }}>
        {m.text}{m.streaming ? <span style={{ color: 'var(--neon-green)' }}>▋</span> : null}
        {!isUser && <ToolEvents events={m.events} />}
      </div>
    </div>
  )
}

export default function ChatPanel({ resumeId = null }) {
  const {
    messages, connected, busy, pendingAsk, listening, speaking,
    send, answerAsk, interrupt, startVoice, stopVoice,
  } = useConverse({ origin: 'ui', resumeId })
  const [draft, setDraft] = useState('')
  const [askDraft, setAskDraft] = useState('')
  const scrollRef = useRef(null)

  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, pendingAsk])

  const onSubmit = () => { send(draft); setDraft('') }
  const onKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onSubmit() }
  }

  const askBody = pendingAsk?.payload?.body || pendingAsk?.payload?.question
    || pendingAsk?.payload?.reason || pendingAsk?.payload?.text || 'The agent is asking for input.'
  const askTitle = pendingAsk?.payload?.title
    || (pendingAsk?.payload?.type === 'task_runner_permission' ? 'Permission requested' : 'Question')

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      {/* header */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '14px 24px', borderBottom: '1px solid var(--border-subtle)',
      }}>
        <span style={{
          width: 8, height: 8, borderRadius: '50%',
          background: connected ? 'var(--neon-green)' : 'var(--neon-red)',
          boxShadow: connected ? '0 0 6px var(--neon-green)' : 'none',
        }} />
        <span style={{
          fontFamily: 'var(--font-mono)', fontSize: 11, letterSpacing: '0.1em',
          textTransform: 'uppercase', color: 'var(--text-primary)', fontWeight: 600,
        }}>Chat — orchestrator</span>
      </div>

      {/* messages */}
      <div ref={scrollRef} style={{
        flex: 1, overflowY: 'auto', padding: '20px 24px',
        display: 'flex', flexDirection: 'column', gap: 12,
      }}>
        {messages.length === 0 && (
          <div style={{
            flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center',
            justifyContent: 'center', gap: 8, color: 'var(--text-muted)',
            fontFamily: 'var(--font-mono)', fontSize: 12,
          }}>
            <div style={{ fontSize: 28, opacity: 0.3 }}>◈</div>
            <div>{'> talk to YUYUTSAVA — it can run tasks and delegate to subagents'}</div>
          </div>
        )}
        {messages.map((m) => <Bubble key={m.id} m={m} />)}

        {pendingAsk && (
          <div style={{
            border: '1px solid var(--neon-amber)', borderRadius: 'var(--radius-card)',
            padding: '12px 14px', background: 'rgba(255,176,0,0.06)',
          }}>
            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--neon-amber)', marginBottom: 6 }}>
              ▣ {askTitle}
            </div>
            <div style={{ fontSize: 13, color: 'var(--text-primary)', marginBottom: 8 }}>{askBody}</div>
            <div style={{ display: 'flex', gap: 8 }}>
              <input
                value={askDraft}
                onChange={(e) => setAskDraft(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && askDraft.trim()) { answerAsk(askDraft.trim()); setAskDraft('') } }}
                placeholder="type a reply, or use the buttons"
                style={{
                  flex: 1, background: 'var(--bg-deep)', color: 'var(--text-primary)',
                  border: '1px solid var(--border-card)', borderRadius: 6, padding: '6px 10px', fontSize: 12,
                }}
              />
              <button onClick={() => answerAsk('yes')} style={btnStyle(true)}>approve</button>
              <button onClick={() => answerAsk('no')} style={btnStyle(false)}>reject</button>
            </div>
          </div>
        )}
      </div>

      {/* composer */}
      <div style={{
        display: 'flex', gap: 8, padding: '12px 24px 18px',
        borderTop: '1px solid var(--border-subtle)',
      }}>
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          rows={1}
          placeholder={busy ? 'agent is working…' : 'message the orchestrator (Enter to send, Shift+Enter for newline)'}
          style={{
            flex: 1, resize: 'none', background: 'var(--bg-card)', color: 'var(--text-primary)',
            border: '1px solid var(--border-card)', borderRadius: 8, padding: '10px 12px',
            fontSize: 13, fontFamily: 'inherit', maxHeight: 120,
          }}
        />
        <button
          onClick={() => (listening ? stopVoice() : startVoice())}
          title={listening ? 'stop microphone' : 'talk to the agent'}
          style={micBtnStyle(listening)}
        >
          {listening ? '● mic' : '🎙 mic'}
        </button>
        {busy ? (
          <button onClick={interrupt} style={btnStyle(false)}>stop</button>
        ) : (
          <button onClick={onSubmit} disabled={!draft.trim()} style={btnStyle(true)}>send</button>
        )}
      </div>
      {(listening || speaking) && (
        <div style={{
          padding: '0 24px 10px', fontFamily: 'var(--font-mono)', fontSize: 10,
          color: speaking ? 'var(--neon-green)' : 'var(--neon-amber)',
        }}>
          {speaking ? '▸ agent speaking…' : '● listening — speak, then pause (or stop the mic)'}
        </div>
      )}
    </div>
  )
}

function btnStyle(primary) {
  return {
    fontFamily: 'var(--font-mono)', fontSize: 12, cursor: 'pointer',
    padding: '8px 14px', borderRadius: 8,
    background: primary ? 'rgba(0,255,136,0.1)' : 'rgba(255,51,102,0.08)',
    border: `1px solid ${primary ? 'rgba(0,255,136,0.3)' : 'rgba(255,51,102,0.3)'}`,
    color: primary ? 'var(--neon-green)' : 'var(--neon-red)',
  }
}

function micBtnStyle(active) {
  return {
    fontFamily: 'var(--font-mono)', fontSize: 12, cursor: 'pointer',
    padding: '8px 12px', borderRadius: 8,
    background: active ? 'rgba(120,160,255,0.18)' : 'rgba(120,160,255,0.06)',
    border: `1px solid rgba(120,160,255,${active ? 0.5 : 0.3})`,
    color: '#9bb8ff',
  }
}
