import {
  todoAttachmentUrl, artifactUrl, todoAttachmentBundleUrl, artifactBundleUrl,
} from '../../../api/client'

// Resolve the bytes URL for an attachment/artifact record, source-agnostic so
// the same block components render both TODO-card attachments (served under
// /todos/{card}/attachments/{id}) and general chat/voice artifacts (served
// under /artifacts/{id}, carried on the record's `url`). Card context always
// passes a cardId; chat context passes none and the record carries `url`.
export function blockSrc(attachment, cardId, { download = false } = {}) {
  // created_ts rides as a cache-buster: singleton blocks (journey) regenerate
  // fresh bytes under the same attachment id/URL.
  if (cardId) {
    return todoAttachmentUrl(cardId, attachment.attachment_id, {
      download, v: attachment.created_ts,
    })
  }
  return artifactUrl(attachment.url)
}

// The record's file as it is named ON DISK. Not the title: the title is a free
// label, and an upload that collided was renamed ("x-1.html") — only the path's
// basename addresses the actual file. Split on both separators; the daemon may
// be serving from Windows.
export const primaryName = (attachment) =>
  (attachment.path || '').split(/[\\/]/).pop() || ''

// Bytes URL for one file of a MULTI-file artifact, resolved inside the record's
// own directory — the twin of blockSrc, source-agnostic the same way. Framing the
// primary file at its own basename is what makes the document's relative refs
// (`./support.js`) resolve back into the bundle instead of dangling.
export function bundleSrc(attachment, cardId, relPath) {
  if (cardId) return todoAttachmentBundleUrl(cardId, attachment.attachment_id, relPath)
  return artifactBundleUrl(attachment.attachment_id, relPath)
}
