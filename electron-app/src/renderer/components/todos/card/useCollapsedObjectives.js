import { useCallback, useEffect, useState } from 'react'

// Per-card objective collapse state — survives navigation and reloads so an
// expanded objective comes back exactly as it was left.
const collapseKey = (cardId) => `yy.todo.collapse.${cardId}`

// Persisted per card, pruned to live ids once the card loads.
export function useCollapsedObjectives(cardId, card) {
  const [collapsedObjectives, setCollapsedObjectives] = useState(() => {
    try {
      return new Set(JSON.parse(localStorage.getItem(collapseKey(cardId)) || '[]'))
    } catch {
      return new Set()
    }
  })

  // Prune collapse state to objectives that still exist once the card loads.
  useEffect(() => {
    if (!card) return
    setCollapsedObjectives((s) => {
      const live = new Set(card.objectives.map((o) => o.objective_id))
      const next = new Set([...s].filter((id) => live.has(id)))
      if (next.size === s.size) return s
      try {
        localStorage.setItem(collapseKey(cardId), JSON.stringify([...next]))
      } catch {
        /* quota */
      }
      return next
    })
  }, [card, cardId])

  const onToggleCollapse = useCallback(
    (objectiveId) => {
      setCollapsedObjectives((s) => {
        const next = new Set(s)
        if (next.has(objectiveId)) next.delete(objectiveId)
        else next.add(objectiveId)
        try {
          localStorage.setItem(collapseKey(cardId), JSON.stringify([...next]))
        } catch {
          /* quota */
        }
        return next
      })
    },
    [cardId],
  )

  return [collapsedObjectives, onToggleCollapse]
}
