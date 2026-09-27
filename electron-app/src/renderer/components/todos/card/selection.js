import { PHASE_ACCENT } from '../shared'

// A card's multi-select of objectives and notes, as pure functions. Keys are
// `obj:<objective_id>` and `note:<note_id>`.

// Bounded so the reference block the tinker receives stays reviewable.
export const MAX_SELECTION = 20

// The selection with `key` flipped. Past the cap, additional checks are
// ignored and the same set comes back.
export function toggled(selected, key) {
  const next = new Set(selected)
  if (next.has(key)) next.delete(key)
  else if (next.size < MAX_SELECTION) next.add(key)
  else return selected
  return next
}

export function without(selected, key) {
  const next = new Set(selected)
  next.delete(key)
  return next
}

// One composer pill per selected objective/note; null when nothing is selected.
export function selectionChips(card, selected) {
  if (!card || selected.size === 0) return null
  const chips = []
  for (const o of card.objectives) {
    if (selected.has(`obj:${o.objective_id}`)) {
      chips.push({
        key: `obj:${o.objective_id}`,
        label: `◆ ${o.title}`,
        title: `objective “${o.title}” [${o.phase}]`,
        accent: PHASE_ACCENT[o.phase] || PHASE_ACCENT.abandoned,
      })
    }
  }
  for (const n of card.notes) {
    if (selected.has(`note:${n.note_id}`)) {
      const excerpt = n.body.replace(/\n/g, ' ')
      chips.push({
        key: `note:${n.note_id}`,
        label: `✎ ${excerpt.length > 30 ? `${excerpt.slice(0, 30)}…` : excerpt}`,
        title: `note by ${n.author}`,
      })
    }
  }
  return chips
}

// The invisible reference block that rides the next tinker message — the
// tinker prompt knows these stable-id lines as its scope for the turn.
export function selectionContext(card, selected) {
  if (!card || selected.size === 0) return ''
  const lines = []
  for (const o of card.objectives) {
    if (selected.has(`obj:${o.objective_id}`)) {
      lines.push(`[objective ${o.objective_id} "${o.title}" phase=${o.phase}]`)
    }
  }
  for (const n of card.notes) {
    if (selected.has(`note:${n.note_id}`)) {
      const excerpt = n.body.length > 120 ? `${n.body.slice(0, 120)}…` : n.body
      lines.push(`[note ${n.note_id} by ${n.author}] "${excerpt.replace(/\n/g, ' ')}"`)
    }
  }
  return lines.join('\n')
}
