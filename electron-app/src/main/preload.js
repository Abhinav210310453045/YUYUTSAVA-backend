const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('electronAPI', {
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

  // Daemon log stream
  onDaemonLog: (cb) => {
    ipcRenderer.on('daemon:log', (_event, line) => cb(line))
    return () => ipcRenderer.removeAllListeners('daemon:log')
  },
})
