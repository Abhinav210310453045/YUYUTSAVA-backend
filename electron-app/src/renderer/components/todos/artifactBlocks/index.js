// Pluggable artifact-block registry — the frontend twin of
// yuyutsava/todoboard/artifacts.py. Each module exports a `matches(att)`
// predicate plus a component taking { attachment, cardId }; resolution walks
// the registry in order and unknown kinds fall back to the download tile.
// Adding a block (the Phase-7 JSX sandbox and audio blocks below) = one new
// module + one entry here — zero edits to TodoCardView or the other blocks.
import DiagramBlock, { matches as diagramMatches } from './DiagramBlock'
import ImageBlock, { matches as imageMatches } from './ImageBlock'
import SandboxBlock, { matches as sandboxMatches } from './SandboxBlock'
import TextBlock, { matches as textMatches } from './TextBlock'
import LinkBlock, { matches as linkMatches } from './LinkBlock'
import AudioBlock, { matches as audioMatches } from './AudioBlock'
import DownloadTile from './DownloadTile'

const REGISTRY = [
  { matches: diagramMatches, Component: DiagramBlock },
  { matches: imageMatches, Component: ImageBlock },
  // Before TextBlock: html/jsx artifacts render live in the sandbox instead
  // of as source preview — the ordered registry IS the override mechanism.
  { matches: sandboxMatches, Component: SandboxBlock },
  { matches: textMatches, Component: TextBlock },
  { matches: linkMatches, Component: LinkBlock },
  { matches: audioMatches, Component: AudioBlock },
]

export function resolveBlock(attachment) {
  const entry = REGISTRY.find((e) => e.matches(attachment))
  return entry ? entry.Component : DownloadTile
}

export { DownloadTile }
