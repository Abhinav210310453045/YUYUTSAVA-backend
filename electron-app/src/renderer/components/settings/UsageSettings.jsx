import React, { useCallback, useEffect, useState } from 'react'
import { getUsageSessions, getUsageSummary } from '../../api/client'
import { fmtCost, fmtPct, fmtTokens } from '../chat/ContextAside'
import './UsageSettings.css'

// Settings → Usage: what every session has spent.
//
// Two calls per range: /usage/summary (totals + per-model + a per-day series,
// so the three views cannot disagree) and /usage/sessions (per-conversation,
// most recently active first). No charting dependency — the bars and the
// sparkline are a handful of divs and one inline <svg>.
//
// The honesty rule from the live panel carries over: a model with no price
// entry makes a total an undercount, so it is labelled rather than presented
// as a confident figure.

const RANGES = [
  { key: '24h', label: '24 h', seconds: 24 * 3600 },
  { key: '7d', label: '7 days', seconds: 7 * 24 * 3600 },
  { key: '30d', label: '30 days', seconds: 30 * 24 * 3600 },
  { key: 'all', label: 'All', seconds: null },
]

function Card({ label, value, hint }) {
  return (
    <div className="usage-card">
      <div className="usage-card-label">{label}</div>
      <div className="usage-card-value">{value}</div>
      {hint && <div className="usage-card-hint">{hint}</div>}
    </div>
  )
}

// Horizontal bars, widest first, scaled to the largest row.
function ModelBars({ rows }) {
  const max = Math.max(1, ...rows.map((r) => r.input_tokens + r.output_tokens))
  return (
    <div className="usage-models">
      {rows.map((r) => {
        const total = r.input_tokens + r.output_tokens
        return (
          <div key={r.key} className="usage-model">
            <span className="usage-model-name" title={r.key}>
              {r.key || '—'}
            </span>
            <div className="usage-model-track">
              <div
                className="usage-model-bar"
                style={{ width: `${Math.max(2, (total / max) * 100)}%` }}
              />
            </div>
            <span className="usage-model-count">
              {r.calls} · {fmtTokens(total)}
            </span>
          </div>
        )
      })}
    </div>
  )
}

// Per-day tokens as a bar sparkline. Hand-rolled SVG: one chart is not worth a
// charting dependency, and the shape of the data (a short ordered series) is
// exactly what a polyline-free bar row does well.
function DaySeries({ rows }) {
  if (rows.length < 2) return null
  const max = Math.max(1, ...rows.map((r) => r.input_tokens + r.output_tokens))
  const W = 100
  const barW = W / rows.length
  return (
    <div>
      <svg viewBox={`0 0 ${W} 28`} preserveAspectRatio="none" className="usage-days">
        {rows.map((r, i) => {
          const h = ((r.input_tokens + r.output_tokens) / max) * 26
          return (
            <rect
              key={r.key}
              x={i * barW + barW * 0.15}
              y={28 - Math.max(1, h)}
              width={barW * 0.7}
              height={Math.max(1, h)}
              fill="var(--neon-green)"
              opacity="0.75"
            >
              <title>{`${r.key}: ${fmtTokens(r.input_tokens + r.output_tokens)} tokens · ${r.calls} calls`}</title>
            </rect>
          )
        })}
      </svg>
      <div className="usage-days-axis">
        <span>{rows[0].key}</span>
        <span>{rows[rows.length - 1].key}</span>
      </div>
    </div>
  )
}

function SessionTable({ rows }) {
  return (
    <div className="usage-table-scroll">
      <table className="usage-table">
        <thead>
          <tr>
            <th>session</th>
            <th>origin</th>
            <th className="usage-num">calls</th>
            <th className="usage-num">in</th>
            <th className="usage-num">cached</th>
            <th className="usage-num">out</th>
            <th className="usage-num">cost</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.thread_id || 'unattributed'}>
              <td
                className="usage-session"
                title={`${r.title || r.thread_id}\n${(r.models || []).join(', ')}`}
              >
                {r.title || r.thread_id || 'unattributed'}
              </td>
              <td className="usage-muted">{r.origin || '—'}</td>
              <td className="usage-num">{r.calls}</td>
              <td className="usage-num">{fmtTokens(r.input_tokens)}</td>
              <td className="usage-num usage-muted">
                {r.cache_read_tokens
                  ? `${fmtPct(r.cache_read_tokens / Math.max(1, r.input_tokens))}`
                  : '—'}
              </td>
              <td className="usage-num">{fmtTokens(r.output_tokens)}</td>
              <td
                className="usage-num"
                style={{ color: r.priced ? 'var(--text-secondary)' : 'var(--text-dim)' }}
              >
                {fmtCost(r.est_cost_usd, r.priced)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function UsageSettings() {
  const [range, setRange] = useState('7d')
  const [data, setData] = useState(null)
  const [error, setError] = useState(false)
  const [loading, setLoading] = useState(true)

  const load = useCallback(() => {
    const spec = RANGES.find((r) => r.key === range) || RANGES[1]
    // The API takes epoch SECONDS.
    const since = spec.seconds ? Math.floor(Date.now() / 1000 - spec.seconds) : null
    let alive = true
    setLoading(true)
    Promise.all([getUsageSummary(since), getUsageSessions(since, 100)])
      .then(([summary, sessions]) => {
        if (!alive) return
        setData({ summary, sessions: sessions?.rows || [] })
        setError(false)
      })
      .catch(() => {
        if (alive) setError(true)
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [range])

  useEffect(() => load(), [load])

  if (error) {
    return <div className="usage-note">Could not read usage — is the daemon running?</div>
  }
  if (!data) {
    return <div className="usage-note">Loading…</div>
  }

  const { summary, sessions } = data
  const totals = summary?.totals || {}
  const unpriced = summary?.unpriced_models || []
  const cacheShare = totals.input_tokens ? totals.cache_read_tokens / totals.input_tokens : 0

  return (
    <div className="usage">
      {/* range picker + refresh */}
      <div className="usage-ranges">
        {RANGES.map((r) => (
          <button
            key={r.key}
            onClick={() => setRange(r.key)}
            className="usage-range"
            style={{
              background: range === r.key ? 'rgba(var(--accent-rgb), 0.12)' : 'transparent',
              border: `1px solid ${
                range === r.key ? 'rgba(var(--accent-rgb), 0.3)' : 'var(--border-subtle)'
              }`,
              color: range === r.key ? 'var(--neon-green)' : 'var(--text-muted)',
            }}
          >
            {r.label}
          </button>
        ))}
        <button onClick={load} title="Refresh" className="hover-bulge usage-refresh">
          {loading ? '…' : '↻'}
        </button>
      </div>

      {!totals.calls ? (
        <div className="usage-note">No model calls recorded in this range.</div>
      ) : (
        <>
          <div className="usage-cards">
            <Card label="calls" value={totals.calls} />
            <Card
              label="input"
              value={fmtTokens(totals.input_tokens)}
              hint={totals.cache_read_tokens ? `${fmtPct(cacheShare)} from cache` : undefined}
            />
            <Card label="output" value={fmtTokens(totals.output_tokens)} />
            <Card
              label="cost"
              value={
                unpriced.length && !totals.est_cost_usd
                  ? 'unpriced'
                  : fmtCost(totals.est_cost_usd, true)
              }
              hint={unpriced.length ? 'incomplete — see below' : undefined}
            />
          </div>

          {unpriced.length > 0 && (
            <div className="usage-unpriced">
              No price entry for {unpriced.join(', ')}, so their spend counts as $0 and the total
              above is an undercount. Add them to <code>~/.yuyutsava/model_prices.json</code> as{' '}
              <code>{'{"<model>": [$/1M in, $/1M out]}'}</code>. Prefixes match, so one entry covers
              a family.
            </div>
          )}

          {summary?.by_day?.length > 1 && (
            <div>
              <div className="usage-heading">tokens per day</div>
              <DaySeries rows={summary.by_day} />
            </div>
          )}

          {summary?.by_model?.length > 0 && (
            <div>
              <div className="usage-heading usage-heading--spaced">by model</div>
              <ModelBars rows={summary.by_model} />
            </div>
          )}

          {sessions.length > 0 && (
            <div>
              <div className="usage-heading">by session ({sessions.length})</div>
              <SessionTable rows={sessions} />
            </div>
          )}

          <div className="usage-footnote">
            Cached input is charged at the full input rate here, so a well-cached
            conversation&apos;s cost is an upper bound. Deleting a session removes its usage rows
            with it.
          </div>
        </>
      )}
    </div>
  )
}
