import React, { useEffect, useState } from 'react'
import { todoAttachmentUrl } from '../../../api/client'

// Phase-7 JSX/HTML sandbox block. Interactive artifacts render inside a
// sandboxed iframe with an opaque origin (sandbox="allow-scripts" ONLY — no
// same-origin, no top navigation, no popups, no forms) plus an injected CSP
// that denies ALL network. This upholds TextBlock's never-inject posture:
// artifact code never touches the app's DOM, and the app's node-free,
// context-isolated renderer never leaks into the frame.
//
// JSX is transpiled locally with @babel/standalone and runs against the
// app's own bundled React 18 UMD builds, all inlined into the srcDoc — the
// frame needs (and is allowed) zero remote fetches. Everything heavy is
// dynamically imported so it lands in a lazy chunk loaded only when a JSX
// artifact is actually on screen.

const SANDBOX_MIMES = ['text/html', 'text/jsx']

export const matches = (att) =>
  ['file', 'artifact'].includes(att.kind) && SANDBOX_MIMES.includes(att.mime || '')

// Everything the framed document may do: inline scripts/styles and data:
// assets only. No connect/img/media/frame sources — the artifact must be
// fully self-contained. (The srcdoc frame also inherits the app's CSP;
// policies combine restrictively, so this can only tighten it.)
const FRAME_CSP =
  "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; " +
  "img-src data:; font-src data:; media-src data:; form-action 'none'"
const CSP_META = `<meta http-equiv="Content-Security-Policy" content="${FRAME_CSP}">`

// `</script` anywhere in inlined source (even inside a JS string) would close
// the tag; `<\/` is byte-identical inside JS strings/regexes and unreachable
// elsewhere in valid compiled code.
const escScript = (s) => s.replace(/<\/script/gi, '<\\/script')

// Raw HTML artifacts run as authored — just force the CSP in first.
function buildHtmlDoc(src) {
  if (/<head[^>]*>/i.test(src)) return src.replace(/<head[^>]*>/i, (m) => m + CSP_META)
  if (/<html[^>]*>/i.test(src)) return src.replace(/<html[^>]*>/i, (m) => m + `<head>${CSP_META}</head>`)
  return `<!doctype html><html><head><meta charset="utf-8">${CSP_META}</head><body>${src}</body></html>`
}

// JSX contract: the file may `import` react / react-dom(/client), and either
// export a component (default or `App`), define `window.App`, or mount into
// #root itself. Compiled to CommonJS and run against a tiny require shim.
async function buildJsxDoc(src) {
  const [reactUmd, reactDomUmd, babelMod] = await Promise.all([
    import('../../../../../node_modules/react/umd/react.production.min.js?raw'),
    import('../../../../../node_modules/react-dom/umd/react-dom.production.min.js?raw'),
    import('@babel/standalone'),
  ])
  const Babel = babelMod.default ?? babelMod
  const compiled = Babel.transform(src, {
    filename: 'artifact.jsx',
    // classic runtime: JSX compiles to React.createElement against the
    // inlined UMD globals (automatic would require("react/jsx-runtime")).
    presets: [['env', { modules: 'commonjs' }], ['react', { runtime: 'classic' }]],
  }).code

  return `<!doctype html><html><head><meta charset="utf-8">${CSP_META}
<style>
  html, body { margin: 0; padding: 12px; background: #10101c; color: #e8e8f0;
               font-family: ui-monospace, Menlo, monospace; font-size: 13px; }
  #root { min-height: 40px; }
  pre.sandbox-error { color: #ff5577; white-space: pre-wrap; word-break: break-word; }
</style>
</head><body><div id="root"></div>
<script>${escScript(reactUmd.default)}</script>
<script>${escScript(reactDomUmd.default)}</script>
<script>
function __showErr(msg) {
  var r = document.getElementById('root');
  r.innerHTML = '';
  var p = document.createElement('pre');
  p.className = 'sandbox-error';
  p.textContent = String(msg);
  r.appendChild(p);
}
window.onerror = function (msg) { __showErr(msg); };
(function () {
  var module = { exports: {} };
  var exports = module.exports;
  var __mods = {
    'react': window.React,
    'react-dom': window.ReactDOM,
    'react-dom/client': window.ReactDOM,
  };
  // Belt-and-braces: we compile with the classic runtime, but shim the
  // automatic runtime too so hand-precompiled artifacts also load.
  var __jsxShim = function (type, props, key) {
    props = props || {};
    var children = props.children;
    var rest = {};
    for (var k in props) if (k !== 'children') rest[k] = props[k];
    if (key !== undefined) rest.key = key;
    if (children === undefined) return React.createElement(type, rest);
    return Array.isArray(children)
      ? React.createElement.apply(React, [type, rest].concat(children))
      : React.createElement(type, rest, children);
  };
  __mods['react/jsx-runtime'] = { jsx: __jsxShim, jsxs: __jsxShim, Fragment: React.Fragment };
  var require = function (name) {
    if (__mods[name]) return __mods[name];
    throw new Error('module not available in the sandbox: ' + name);
  };
  try {
${escScript(compiled)}
    // __-prefixed on purpose: the compiled code shares this block scope, so
    // plain names (App, root) would collide with the artifact's own.
    var __App = (module.exports && (module.exports.default || module.exports.App)) || window.App;
    var __root = document.getElementById('root');
    if (__App && !__root.childElementCount) {
      ReactDOM.createRoot(__root).render(React.createElement(__App));
    } else if (!__root.childElementCount) {
      __showErr('no component found: export default a component (or define window.App, or mount into #root yourself)');
    }
  } catch (e) { __showErr(e && e.stack || e); }
})();
</script></body></html>`
}

export default function SandboxBlock({ attachment, cardId }) {
  const [doc, setDoc] = useState(null)
  const [source, setSource] = useState(null)
  const [error, setError] = useState(null)
  const [showSource, setShowSource] = useState(false)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch(todoAttachmentUrl(cardId, attachment.attachment_id))
        if (!res.ok) throw new Error(`fetch → ${res.status}`)
        const text = await res.text()
        if (cancelled) return
        setSource(text)
        const built = attachment.mime === 'text/jsx'
          ? await buildJsxDoc(text)
          : buildHtmlDoc(text)
        if (!cancelled) setDoc(built)
      } catch (e) {
        if (!cancelled) setError(e.message)
      }
    })()
    return () => { cancelled = true }
  }, [cardId, attachment.attachment_id, attachment.mime])

  if (error) {
    return (
      <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--neon-red)' }}>
        {`> ${error}`}
      </div>
    )
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, minWidth: 0 }}>
      <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
        {['run', 'source'].map((mode) => (
          <button
            key={mode}
            onClick={() => setShowSource(mode === 'source')}
            style={{
              fontSize: 9, padding: '2px 8px', borderRadius: 8, cursor: 'pointer',
              fontFamily: 'var(--font-mono)', textTransform: 'uppercase', letterSpacing: '0.05em',
              background: (mode === 'source') === showSource ? 'rgba(120,160,255,0.12)' : 'transparent',
              color: (mode === 'source') === showSource ? '#9bb8ff' : 'var(--text-dim)',
              border: `1px solid ${(mode === 'source') === showSource ? 'rgba(120,160,255,0.25)' : 'var(--border-subtle)'}`,
            }}
          >
            {mode}
          </button>
        ))}
      </div>
      {showSource ? (
        <pre style={{
          margin: 0, padding: '8px 10px', maxHeight: 280, overflow: 'auto',
          background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
          borderRadius: 6, fontFamily: 'var(--font-mono)', fontSize: 11,
          color: 'var(--text-primary)', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
        }}>
          {source == null ? 'loading…' : source}
        </pre>
      ) : doc == null ? (
        <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-dim)' }}>
          loading sandbox…
        </div>
      ) : (
        <iframe
          sandbox="allow-scripts"
          referrerPolicy="no-referrer"
          srcDoc={doc}
          title={attachment.title || 'artifact sandbox'}
          style={{
            width: '100%', height: 280, border: '1px solid var(--border-subtle)',
            borderRadius: 6, background: '#10101c',
          }}
        />
      )}
    </div>
  )
}
