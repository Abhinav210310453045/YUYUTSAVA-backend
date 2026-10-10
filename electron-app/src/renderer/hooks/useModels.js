import { useEffect, useSyncExternalStore } from 'react'
import { SSEClient } from '../api/sse'
import { getModels } from '../api/models'

// models.toml as the daemon sees it, kept current off the `models` stream item —
// so a change made in another window, in the CLI or by hand shows up here, and a
// form opened before it carries the stale revision the daemon will refuse.
//
// One store for the whole window, however many components read it: one
// /stream connection, open while anything is subscribed. A stream per reader
// would use up the browser's six connections to the daemon, after which every
// request waits for one of them to close.

let state = { status: null, error: null }
const listeners = new Set()
let client = null

function publish(next) {
  state = { ...state, ...next }
  for (const listener of listeners) listener()
}

export async function refreshModels() {
  try {
    const status = await getModels()
    publish({ status, error: null })
    return status
  } catch (error) {
    publish({ error })
    return null
  }
}

function subscribe(listener) {
  listeners.add(listener)
  if (!client) {
    client = new SSEClient({
      onModels: (msg) => msg?.models && publish({ status: msg.models }),
      onConnected: () => refreshModels(), // a reconnect may have missed a change
    })
    client.connect()
  }
  return () => {
    listeners.delete(listener)
    if (!listeners.size && client) {
      client.disconnect()
      client = null
    }
  }
}

export function useModels() {
  const { status, error } = useSyncExternalStore(subscribe, () => state)
  useEffect(() => {
    refreshModels()
  }, [])
  return { status, error, refresh: refreshModels }
}
