import { useCallback, useEffect, useRef, useState } from 'react'
import { SSEClient } from '../api/sse'
import { getRuntimeSettings, patchRuntimeSettings } from '../api/client'

// The daemon-owned hot toggles: voice mode (wake word + spoken replies) and the
// dedicated-subagent deny-list. Fetched once, then kept live off the `settings`
// SSE item so a flip in ANY surface lands here — this window, the voice overlay
// (a separate renderer), mobile, or a `/voice off` typed into the CLI.
//
// Deliberately self-subscribing rather than reading useSSE's context: the
// overlay renderer mounts VoiceOverlay with none of the main app's SSE
// plumbing, and one idle EventSource is far cheaper than duplicating this
// state in two places and keeping them in sync.
//
// Defaults are ON so a daemon that's still starting (or an older build without
// /settings/runtime) behaves exactly as before the toggle existed.
const DEFAULTS = {
  voice: { wake_enabled: true, tts_enabled: true },
  subagents: { disabled: [] },
}

export function useRuntimeSettings() {
  const [settings, setSettings] = useState(DEFAULTS)
  const [loaded, setLoaded] = useState(false)
  // True while a PATCH is in flight. The wake toggle tears down and respawns
  // the daemon's mic subprocess, so the button needs a pending state rather
  // than pretending the change is instant.
  const [saving, setSaving] = useState(false)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    getRuntimeSettings()
      .then((s) => { if (alive.current && s) setSettings(s) })
      .catch(() => { /* daemon not up yet — defaults stand, SSE will correct us */ })
      .finally(() => { if (alive.current) setLoaded(true) })

    const client = new SSEClient({
      onSettings: (msg) => { if (alive.current && msg?.settings) setSettings(msg.settings) },
      // A reconnect means we may have missed a change while disconnected.
      onConnected: () => {
        getRuntimeSettings()
          .then((s) => { if (alive.current && s) setSettings(s) })
          .catch(() => {})
      },
    })
    client.connect()
    return () => { alive.current = false; client.disconnect() }
  }, [])

  // Optimistic: apply locally, then let the server's response (and the SSE
  // echo) confirm. On failure we roll back so the button never lies.
  const patch = useCallback(async (body) => {
    const previous = settings
    setSettings((cur) => ({
      voice: { ...cur.voice, ...(body.voice || {}) },
      subagents: { ...cur.subagents, ...(body.subagents?.disabled ? { disabled: body.subagents.disabled } : {}) },
    }))
    setSaving(true)
    try {
      const next = await patchRuntimeSettings(body)
      if (alive.current && next) setSettings(next)
      return next
    } catch (e) {
      if (alive.current) setSettings(previous)
      throw e
    } finally {
      if (alive.current) setSaving(false)
    }
  }, [settings])

  const voice = settings.voice || DEFAULTS.voice
  const setVoice = useCallback((v) => patch({ voice: v }), [patch])
  // Flip one subagent by name; the server owns the deny-list arithmetic.
  const setSubagentEnabled = useCallback(
    (name, enabled) => patch({ subagents: { name, enabled } }),
    [patch],
  )

  return {
    settings,
    loaded,
    saving,
    voice,
    // "Voice mode" as one user-facing idea: on when either half is on.
    voiceOn: !!(voice.wake_enabled || voice.tts_enabled),
    disabledSubagents: settings.subagents?.disabled || [],
    setVoice,
    setSubagentEnabled,
    patch,
  }
}
