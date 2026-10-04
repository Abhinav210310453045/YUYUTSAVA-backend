import { useCallback, useEffect, useRef, useState } from 'react'
import { SSEClient } from '../api/sse'
import { getModels } from '../api/models'

// models.toml as the daemon sees it, kept current off the `models` stream item —
// so a change made in another window, in the CLI or by hand shows up here, and a
// form opened before it carries the stale revision the daemon will refuse.
export function useModels() {
  const [status, setStatus] = useState(null)
  const [error, setError] = useState(null)
  const alive = useRef(true)

  const refresh = useCallback(async () => {
    try {
      const s = await getModels()
      if (alive.current) {
        setStatus(s)
        setError(null)
      }
      return s
    } catch (e) {
      if (alive.current) setError(e)
      return null
    }
  }, [])

  useEffect(() => {
    alive.current = true
    refresh()
    const client = new SSEClient({
      onModels: (msg) => {
        if (alive.current && msg?.models) setStatus(msg.models)
      },
      onConnected: () => refresh(), // a reconnect may have missed a change
    })
    client.connect()
    return () => {
      alive.current = false
      client.disconnect()
    }
  }, [refresh])

  return { status, error, refresh, setStatus }
}
