import React, { useEffect, useRef, useState } from 'react'
import { getSessionModels, modelChoices, setSessionModels } from '../../api/models'
import { useModels } from '../../hooks/useModels'

// The composer's model chip: which model this conversation runs on, and switching
// it — from the next message, never mid-turn. The choice is the conversation's
// own (kept on its session), so it stays when the chat is resumed here or in the
// CLI. After a switch, if the subagents still run on another model, one line
// offers to move them too (design D7).
export default function ModelChip({ sessionId }) {
  const { status } = useModels()
  const [mine, setMine] = useState(null) // GET /models/sessions/{id}
  const [open, setOpen] = useState(false)
  const [nudge, setNudge] = useState(null) // { model, behind }
  const [problem, setProblem] = useState('')
  const box = useRef(null)

  useEffect(() => {
    if (!sessionId || !status?.configured) return
    getSessionModels(sessionId)
      .then(setMine)
      .catch(() => setMine(null))
  }, [sessionId, status])

  useEffect(() => {
    if (!open) return undefined
    const close = (e) => box.current && !box.current.contains(e.target) && setOpen(false)
    window.addEventListener('mousedown', close)
    return () => window.removeEventListener('mousedown', close)
  }, [open])

  if (!sessionId || !status?.configured || !mine) return null

  async function choose(model) {
    setOpen(false)
    setProblem('')
    try {
      const next = await setSessionModels(sessionId, model, mine.subagents)
      setMine(next)
      const subagentModel =
        next.subagents['*'] || status.tasks.find((t) => t.name === 'subagent')?.reference
      const chatModel = next.effective
      setNudge(
        model && subagentModel && subagentModel !== chatModel
          ? { model: chatModel, behind: subagentModel }
          : null,
      )
    } catch (e) {
      setProblem(e.message)
    }
  }

  async function moveSubagents() {
    setMine(await setSessionModels(sessionId, mine.model, { '*': nudge.model }))
    setNudge(null)
  }

  return (
    <div ref={box} style={{ position: 'relative', display: 'flex', alignItems: 'center', gap: 8 }}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="tap-pop"
        title="This chat's model — changes apply from your next message"
        style={chip(!!mine.model)}
      >
        ◇ {mine.effective}
      </button>
      {nudge && (
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
          subagents still use {nudge.behind} —{' '}
          <a href="#" onClick={(e) => (e.preventDefault(), moveSubagents())} style={link}>
            all subagents
          </a>{' '}
          ·{' '}
          <a href="#" onClick={(e) => (e.preventDefault(), setNudge(null))} style={link}>
            not now
          </a>
        </span>
      )}
      {problem && <span style={{ fontSize: 11, color: 'var(--neon-red)' }}>{problem}</span>}
      {open && (
        <div style={menu}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', padding: '4px 10px' }}>
            this chat runs on
          </div>
          {modelChoices(status).map((ref) => (
            <div key={ref} onClick={() => choose(ref)} style={item(ref === mine.effective)}>
              {ref === mine.effective ? '● ' : '○ '}
              {ref}
              {ref === status.default && (
                <span style={{ color: 'var(--text-dim)' }}> · default</span>
              )}
            </div>
          ))}
          {mine.model && (
            <div onClick={() => choose('')} style={item(false)}>
              ↺ follow models.toml
            </div>
          )}
        </div>
      )}
    </div>
  )
}

const chip = (own) => ({
  padding: '4px 10px',
  borderRadius: 999,
  border: `1px solid ${own ? 'rgba(var(--accent-rgb), 0.5)' : 'var(--border-card)'}`,
  background: 'transparent',
  color: own ? 'var(--neon-green)' : 'var(--text-secondary)',
  fontFamily: 'var(--font-mono)',
  fontSize: 11,
  cursor: 'pointer',
  whiteSpace: 'nowrap',
  maxWidth: 260,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
})
const menu = {
  position: 'absolute',
  bottom: 'calc(100% + 6px)',
  left: 0,
  zIndex: 50,
  minWidth: 260,
  maxHeight: 320,
  overflowY: 'auto',
  padding: '4px 0',
  borderRadius: 'var(--radius-card)',
  border: '1px solid var(--border-card)',
  background: 'var(--bg-elevated)',
  boxShadow: '0 8px 24px rgba(0,0,0,0.35)',
}
const item = (current) => ({
  padding: '6px 10px',
  fontFamily: 'var(--font-mono)',
  fontSize: 12,
  cursor: 'pointer',
  color: current ? 'var(--neon-green)' : 'var(--text-primary)',
})
const link = { color: 'var(--neon-green)', textDecoration: 'none' }
