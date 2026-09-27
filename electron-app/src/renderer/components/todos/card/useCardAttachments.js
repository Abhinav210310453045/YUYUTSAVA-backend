import { useCallback, useState } from 'react'
import { generateTodoArtifact, uploadTodoAttachment } from '../../../api/client'

// A card's attachment writes — uploads, deletes, and the Journey document —
// each folded back into the card through onCardChange. `onJourney` runs with
// the journey attachment once it is on the card.
export function useCardAttachments(cardId, onCardChange, onJourney) {
  const [uploading, setUploading] = useState(0) // in-flight upload count
  const [journeyBusy, setJourneyBusy] = useState(false)

  // Shared by every drop target on the board and the drawer: upload each file
  // (with its objective/note association when dropped onto one), appending the
  // returned row; a per-file failure reports and moves on to the next.
  const onFiles = useCallback(
    async (files, assoc = {}) => {
      for (const file of Array.from(files || [])) {
        setUploading((n) => n + 1)
        try {
          const att = await uploadTodoAttachment(cardId, file, assoc)
          onCardChange((c) => (c ? { ...c, attachments: [...c.attachments, att] } : c))
        } catch (e) {
          alert(`Upload of ${file.name} failed: ${e.message}`)
        } finally {
          setUploading((n) => n - 1)
        }
      }
    },
    [cardId, onCardChange],
  )

  const onAttachmentDeleted = useCallback(
    (attachmentId) => {
      onCardChange((c) =>
        c
          ? {
              ...c,
              attachments: c.attachments.filter((a) => a.attachment_id !== attachmentId),
            }
          : c,
      )
    },
    [onCardChange],
  )

  // "Journey of the plan": compile the think flow into an HTML artifact; it
  // lands as a tile in the drawer. Journey is a singleton block — regeneration
  // returns the SAME attachment id with fresh content, so upsert the tile
  // instead of appending a twin.
  const generateJourney = async () => {
    if (journeyBusy) return
    setJourneyBusy(true)
    try {
      const att = await generateTodoArtifact(cardId, 'journey')
      onCardChange((c) => {
        if (!c) return c
        const exists = c.attachments.some((a) => a.attachment_id === att.attachment_id)
        return {
          ...c,
          attachments: exists
            ? c.attachments.map((a) => (a.attachment_id === att.attachment_id ? att : a))
            : [...c.attachments, att],
        }
      })
      onJourney(att)
    } catch (e) {
      alert(`Journey failed: ${e.message}`)
    } finally {
      setJourneyBusy(false)
    }
  }

  return { uploading, onFiles, onAttachmentDeleted, journeyBusy, generateJourney }
}
