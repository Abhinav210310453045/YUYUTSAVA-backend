import { useCallback, useState } from 'react'
import { assignTodoNote, deleteTodoObjective, patchTodoObjective } from '../../../api/client'

// An objective card's writes — patch its fields, delete it, pull a note onto
// it — each folded back into the card through onCardChange. `busy` covers the
// patch and the delete.
export function useObjectiveActions(cardId, objective, onCardChange) {
  const [busy, setBusy] = useState(false)

  const patch = useCallback(
    async (fields) => {
      setBusy(true)
      try {
        const updated = await patchTodoObjective(cardId, objective.objective_id, fields)
        onCardChange((c) => ({
          ...c,
          objectives: c.objectives.map((o) =>
            o.objective_id === updated.objective_id ? updated : o,
          ),
        }))
      } catch (e) {
        alert(`Update failed: ${e.message}`)
      } finally {
        setBusy(false)
      }
    },
    [cardId, objective.objective_id, onCardChange],
  )

  const onDelete = async () => {
    if (!confirm('Delete this objective? Its notes stay on the card as general notes.')) return
    setBusy(true)
    try {
      await deleteTodoObjective(cardId, objective.objective_id)
      onCardChange((c) => ({
        ...c,
        objectives: c.objectives.filter((o) => o.objective_id !== objective.objective_id),
        notes: c.notes.map((n) =>
          n.objective_id === objective.objective_id ? { ...n, objective_id: null } : n,
        ),
      }))
    } catch (e) {
      alert(`Delete failed: ${e.message}`)
      setBusy(false)
    }
  }

  const assignHere = async (noteId) => {
    try {
      const updated = await assignTodoNote(cardId, noteId, objective.objective_id, objective.phase)
      onCardChange((c) => ({
        ...c,
        notes: c.notes.map((n) => (n.note_id === updated.note_id ? updated : n)),
      }))
    } catch (e) {
      alert(`Assign failed: ${e.message}`)
    }
  }

  return { busy, patch, onDelete, assignHere }
}
