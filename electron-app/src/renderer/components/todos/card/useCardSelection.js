import { useCallback, useMemo } from 'react'
import { useViewState } from '../../../nav/useViewState'
import { selectionChips, selectionContext, toggled, without } from './selection'

// Multi-select of notes/objectives → context chips on the tinker composer.
// The mode and the ticks are view state, kept per card across navigation.
export function useCardSelection(card, scope, setThinkOpen) {
  const [selectMode, setSelectMode] = useViewState('selectMode', false, scope)
  const [selected, setSelected] = useViewState('selected', () => new Set(), scope) // 'obj:…' | 'note:…'

  const toggleSelect = useCallback(
    (key) => {
      setSelected((s) => toggled(s, key))
      // Chips live on the tinker composer — surface it as soon as a selection
      // starts so the context is visible where it acts.
      setThinkOpen(true)
    },
    [setSelected, setThinkOpen],
  )

  const toggleSelectMode = useCallback(() => {
    setSelectMode((v) => !v)
    setSelected(new Set())
  }, [setSelectMode, setSelected])

  const contextChips = useMemo(() => selectionChips(card, selected), [card, selected])
  const buildSelectionContext = useCallback(
    () => selectionContext(card, selected),
    [card, selected],
  )
  const onRemoveChip = useCallback((key) => setSelected((s) => without(s, key)), [setSelected])
  // Clearing the chips, and consuming them: chips are one-shot — the block is
  // in the thread after a successful send, and keeping them would silently
  // re-attach stale context to later turns.
  const clearSelection = useCallback(() => setSelected(new Set()), [setSelected])

  return {
    selectMode,
    selected,
    toggleSelect,
    toggleSelectMode,
    contextChips,
    buildSelectionContext,
    onRemoveChip,
    onClearChips: clearSelection,
    onChipsConsumed: clearSelection,
  }
}
