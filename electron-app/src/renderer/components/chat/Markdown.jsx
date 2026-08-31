import React, { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkIndentedProse from '../../lib/remarkIndentedProse'

// Themed Markdown renderer for assistant bubbles. Custom renderers keep the
// terminal/neon identity (mono code blocks, neon links, tinted tables) and
// consume theme tokens so it flips with the light/dark toggle. Safe for
// streaming: react-markdown re-parses the accumulated text each token.
//
// The framed block chrome hangs off `pre`, not `code`. react-markdown dropped
// the `inline` prop in v9, so `code` can no longer tell a `` `span` `` from a
// fenced block on its own — but in hast a block is always `<pre><code>` while
// inline code has no `pre` parent. Overriding `pre` is the only stable
// discriminator; do not move this back onto `code`.

// A `pre` carries exactly one `code` child holding the block's text and, when
// the fence named a language, a `language-*` class.
function codeChildOf(node) {
  return node?.children?.find((c) => c.type === 'element' && c.tagName === 'code')
}

function hastText(el) {
  if (!el) return ''
  if (el.type === 'text') return String(el.value ?? '')
  return (el.children || []).map(hastText).join('')
}

// Defensive fallback for when `node` is absent (e.g. a future react-markdown
// stops passing it): recover the text from the rendered React children.
function reactText(children) {
  return React.Children.toArray(children)
    .map((c) => (React.isValidElement(c) ? reactText(c.props.children) : String(c ?? '')))
    .join('')
}

function InlineCode({ children }) {
  return (
    <code style={{
      fontFamily: 'var(--font-mono)', fontSize: '0.88em',
      background: 'var(--glass-bg)', border: '1px solid var(--border-card)',
      borderRadius: 5, padding: '1px 5px', color: 'var(--text-code)',
    }}>{children}</code>
  )
}

function CodeBlock({ node, children }) {
  const codeEl = codeChildOf(node)
  // Render the extracted text directly rather than `children` — the nested
  // `code` element would otherwise route through InlineCode and draw a pill
  // inside the panel.
  const text = (codeEl ? hastText(codeEl) : reactText(children)).replace(/\n$/, '')
  const [copied, setCopied] = useState(false)
  const classes = codeEl?.properties?.className || []
  const langClass = (Array.isArray(classes) ? classes : String(classes).split(' '))
    .find((c) => /^language-.+/.test(c))
  const lang = langClass ? langClass.slice('language-'.length) : ''
  const onCopy = async () => {
    try { await navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1400) } catch { /* ignore */ }
  }
  return (
    <div style={{
      position: 'relative', margin: '8px 0', borderRadius: 10, overflow: 'hidden',
      border: '1px solid var(--border-card)', background: 'var(--bg-deep)',
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '4px 10px', borderBottom: '1px solid var(--border-subtle)',
        fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-dim)',
      }}>
        <span>{lang || 'code'}</span>
        <button onClick={onCopy} className="tap-pop" style={{
          background: 'transparent', border: 'none', cursor: 'pointer',
          color: copied ? 'var(--neon-green)' : 'var(--text-muted)',
          fontFamily: 'var(--font-mono)', fontSize: 10,
        }}>{copied ? 'copied' : 'copy'}</button>
      </div>
      <pre style={{ margin: 0, padding: '10px 12px', overflowX: 'auto' }}>
        <code style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.55 }}>
          {text}
        </code>
      </pre>
    </div>
  )
}

const COMPONENTS = {
  pre: CodeBlock,
  code: InlineCode,
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noreferrer"
       style={{ color: 'var(--neon-cyan)', textDecoration: 'underline', textUnderlineOffset: 2 }}>
      {children}
    </a>
  ),
  ul: ({ children }) => <ul style={{ margin: '6px 0', paddingLeft: 20 }}>{children}</ul>,
  ol: ({ children }) => <ol style={{ margin: '6px 0', paddingLeft: 20 }}>{children}</ol>,
  li: ({ children }) => <li style={{ margin: '2px 0' }}>{children}</li>,
  p: ({ children }) => <p style={{ margin: '4px 0' }}>{children}</p>,
  h1: ({ children }) => <h3 style={{ margin: '8px 0 4px', fontSize: 16 }}>{children}</h3>,
  h2: ({ children }) => <h4 style={{ margin: '8px 0 4px', fontSize: 14 }}>{children}</h4>,
  h3: ({ children }) => <h5 style={{ margin: '6px 0 4px', fontSize: 13 }}>{children}</h5>,
  blockquote: ({ children }) => (
    <blockquote style={{
      margin: '6px 0', padding: '2px 12px', borderLeft: '3px solid var(--border-neon)',
      color: 'var(--text-muted)', background: 'var(--glass-bg)', borderRadius: 4,
    }}>{children}</blockquote>
  ),
  table: ({ children }) => (
    <div style={{ overflowX: 'auto', margin: '8px 0' }}>
      <table style={{ borderCollapse: 'collapse', fontSize: 12, width: '100%' }}>{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th style={{ border: '1px solid var(--border-card)', padding: '4px 8px', textAlign: 'left', color: 'var(--neon-green)' }}>{children}</th>
  ),
  td: ({ children }) => (
    <td style={{ border: '1px solid var(--border-card)', padding: '4px 8px', color: 'var(--text-secondary)' }}>{children}</td>
  ),
}

export default function Markdown({ children }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm, remarkIndentedProse]} components={COMPONENTS}>
      {children || ''}
    </ReactMarkdown>
  )
}
