// Remark plugin: rescue prose that CommonMark mistook for a code block.
//
// CommonMark turns any 4-space-indented run of lines into a code block. Models
// emit fenced blocks when they mean code, so an indentation-derived block in an
// assistant reply is usually just prose the model happened to indent — and it
// lands in the bubble as a framed snippet panel, which reads as distortion.
//
// So: fenced blocks are never touched (the fence is an explicit signal), and an
// indentation-derived block is unwrapped to a paragraph *only* when its body
// does not look like code.

// Structural markers that ordinary prose does not carry: statement punctuation,
// call/arrow/scope syntax, comment leaders, and common language keywords.
const CODE_MARKERS =
  /[{};]|=>|::|\(\)|^\s*(?:#|\/\/)\s|\b(?:def|class|function|const|let|var|import|from|return|for|while|elif|SELECT|INSERT|UPDATE|DELETE)\b/m

// Fraction of non-word, non-space characters above which a block reads as code
// even without a keyword hit (e.g. a JSON fragment or a shell pipeline).
const SYMBOL_DENSITY_THRESHOLD = 0.12

export function looksLikeCode(text) {
  const body = String(text ?? '')
  if (!body.trim()) return false
  if (CODE_MARKERS.test(body)) return true
  const symbols = body.replace(/[\w\s]/g, '').length
  return symbols / body.length > SYMBOL_DENSITY_THRESHOLD
}

// mdast records no fenced/indented flag and gives both kinds a column-1 start,
// so the only way to tell them apart is the source: a fence opens with ``` or
// ~~~ (after at most three spaces), an indented block with four or more. When
// the source is unavailable, assume fenced — that leaves the block untouched.
function isIndentDerived(node, source) {
  if (node.lang != null) return false
  const start = node.position?.start?.offset
  if (typeof start !== 'number' || typeof source !== 'string') return false
  return !/^ {0,3}(`{3,}|~{3,})/.test(source.slice(start, start + 8))
}

// Keep the visual line structure the indentation implied, minus the chrome.
function toParagraph(node) {
  const lines = String(node.value ?? '').split('\n')
  const children = []
  lines.forEach((line, i) => {
    if (i > 0) children.push({ type: 'break' })
    children.push({ type: 'text', value: line })
  })
  return { type: 'paragraph', children, position: node.position }
}

export default function remarkIndentedProse() {
  return (tree, file) => {
    const source = typeof file?.value === 'string' ? file.value : String(file ?? '')
    const walk = (node) => {
      if (!Array.isArray(node.children)) return
      node.children = node.children.map((child) => {
        if (child.type === 'code') {
          const unwrap = isIndentDerived(child, source) && !looksLikeCode(child.value)
          return unwrap ? toParagraph(child) : child
        }
        walk(child)
        return child
      })
    }
    walk(tree)
  }
}
