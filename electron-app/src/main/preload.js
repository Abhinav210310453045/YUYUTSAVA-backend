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

  // Window controls (frameless)
  minimizeWindow: () => ipcRenderer.send('window:minimize'),
  maximizeWindow: () => ipcRenderer.send('window:maximize'),
  closeWindow: () => ipcRenderer.send('window:close'),

  // Tray badge
  setProposalCount: (n) => ipcRenderer.send('tray:badge', n),

  // Daemon log stream
  onDaemonLog: (cb) => {
    ipcRenderer.on('daemon:log', (_event, line) => cb(line))
    return () => ipcRenderer.removeAllListeners('daemon:log')
  },
})
