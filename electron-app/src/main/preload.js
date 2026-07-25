const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('electronAPI', {
  // Per-main-process run id — lets the renderer tell an in-run reload apart
  // from a fresh app launch when deciding whether to restore its last view.
  getAppRunId: () => ipcRenderer.invoke('app:runId'),

  // Settings
  getSettings: () => ipcRenderer.invoke('settings:get'),
  saveSettings: (data) => ipcRenderer.invoke('settings:save', data),

  // Daemon lifecycle
  getDaemonPort: () => ipcRenderer.invoke('daemon:port'),
  getDaemonStatus: () => ipcRenderer.invoke('daemon:status'),
  startDaemon: () => ipcRenderer.invoke('daemon:start'),
  stopDaemon: () => ipcRenderer.invoke('daemon:stop'),
  restartDaemon: () => ipcRenderer.invoke('daemon:restart'),

  // Daemon-side config (events_config.json, permissions.json)
  getDaemonConfig: (kind) => ipcRenderer.invoke('daemon:getConfig', kind),
  saveDaemonConfig: (kind, body) => ipcRenderer.invoke('daemon:saveConfig', { kind, body }),
  addWatchedDir: (path) => ipcRenderer.invoke('daemon:addWatchedDir', path),
  removeWatchedDir: (path) => ipcRenderer.invoke('daemon:removeWatchedDir', path),
  pickDirectory: () => ipcRenderer.invoke('dialog:pickDirectory'),
  // Save-as: pop a native dialog seeded with `name`, then write `data` (a
  // Uint8Array/ArrayBuffer of the file bytes) to the chosen path. Returns the
  // saved path, or null if the user cancelled. Used by the artifact Download
  // button so the user picks where their copy lands.
  saveFile: (name, data) => ipcRenderer.invoke('dialog:saveFile', { name, data }),

  // Window controls (frameless)
  minimizeWindow: () => ipcRenderer.send('window:minimize'),
  maximizeWindow: () => ipcRenderer.send('window:maximize'),
  closeWindow: () => ipcRenderer.send('window:close'),

  // Tray badge
  setProposalCount: (n) => ipcRenderer.send('tray:badge', n),

  // Focus-aware notifications
  //   showNotification: called by renderer when window is unfocused and a new
  //     proposal/ask arrives. The main process owns the OS banner + dock bounce.
  //   onNotificationClick: subscribe to banner clicks. The cleanup function
  //     removes the listener so React's effect can re-run safely.
  showNotification: (opts) => ipcRenderer.send('notify:show', opts),
  onNotificationClick: (cb) => {
    const handler = (_event, payload) => cb(payload)
    ipcRenderer.on('notify:click', handler)
    return () => ipcRenderer.removeListener('notify:click', handler)
  },

  // Tray menu navigation: main process asks the renderer to switch view
  // (e.g. tray → "Settings"). Cleanup fn removes the listener.
  onNavigate: (cb) => {
    const handler = (_event, target) => cb(target)
    ipcRenderer.on('tray:navigate', handler)
    return () => ipcRenderer.removeListener('tray:navigate', handler)
  },

  // ── Voice overlay / hotkey ──────────────────────────────────────────
  // Renderer (SSE) tells main a wake word fired → main pops overlay or routes
  // to the Voice panel.
  notifyVoiceWake: (payload) => ipcRenderer.send('voice:wake', payload || {}),
  // Overlay renderer asks main to hide it (auto-dismiss / user gesture).
  closeOverlay: () => ipcRenderer.send('overlay:close'),
  // Ask main to re-register the global hotkey (after a settings change).
  rebindVoiceHotkey: () => ipcRenderer.send('voice:rebindHotkey'),
  // Voice mode (daemon-owned) mirrored into the main process, which owns the
  // global hotkey and the overlay but has no daemon client of its own. Pushed
  // by the renderer on mount and on every change; with voice mode off, neither
  // the hotkey nor a stray wake can summon the mic.
  setVoiceModeEnabled: (enabled) => ipcRenderer.send('voice:modeEnabled', !!enabled),
  // Overlay: a pending ask needs the user. Main decides whether to actually
  // pop the window (it knows if the main window is focused, in which case the
  // inline card + Inbox already cover it) and shows it WITHOUT taking focus.
  showAskOverlay: (payload) => ipcRenderer.send('overlay:show-ask', payload || {}),
  hideAskOverlay: () => ipcRenderer.send('overlay:hide-ask'),
  // 'voice' | 'ask' | null — what the overlay window was summoned for.
  getOverlayReason: () => ipcRenderer.invoke('overlay:reason'),
  // Overlay: main signals "you are visible now, start listening".
  onOverlayActivate: (cb) => {
    const handler = (_event, payload) => cb(payload)
    ipcRenderer.on('overlay:activate', handler)
    return () => ipcRenderer.removeListener('overlay:activate', handler)
  },
  // Main window: a hotkey/wake while focused routes here (open + start voice).
  onVoiceActivate: (cb) => {
    const handler = (_event, payload) => cb(payload)
    ipcRenderer.on('voice:activate', handler)
    return () => ipcRenderer.removeListener('voice:activate', handler)
  },
  // Overlay: main relays the same-breath trailing command (stage "command") so
  // the overlay seeds it as the first turn, then starts its mic. An empty
  // command means "no same-breath speech — just start listening".
  onOverlayCommand: (cb) => {
    const handler = (_event, payload) => cb(payload)
    ipcRenderer.on('overlay:command', handler)
    return () => ipcRenderer.removeListener('overlay:command', handler)
  },

  // Daemon log stream
  onDaemonLog: (cb) => {
    ipcRenderer.on('daemon:log', (_event, line) => cb(line))
    return () => ipcRenderer.removeAllListeners('daemon:log')
  },
})
