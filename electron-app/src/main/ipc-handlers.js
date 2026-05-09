const { ipcMain, BrowserWindow } = require('electron')
const settings = require('./settings')
const daemon = require('./daemon')
const tray = require('./tray')

function register(win) {
  // Settings
  ipcMain.handle('settings:get', () => settings.readSettings())
  ipcMain.handle('settings:save', (_e, data) => {
    settings.writeSettings(data)
    if ('YUYUTSAVA_DAEMON_MANAGED' in data) {
      daemon.setManaged(data['YUYUTSAVA_DAEMON_MANAGED'] !== 'false')
    }
    return { ok: true }
  })

  // Daemon lifecycle
  ipcMain.handle('daemon:port', () => daemon.getPort())
  ipcMain.handle('daemon:status', () => ({
    running: daemon.isRunning(),
    managed: daemon.isManaged(),
    port: daemon.getPort(),
  }))
  ipcMain.handle('daemon:start', () => {
    daemon.start(process.cwd())
    return { ok: true }
  })
  ipcMain.handle('daemon:stop', async () => {
    await daemon.stop()
    return { ok: true }
  })
  ipcMain.handle('daemon:restart', async () => {
    await daemon.stop()
    daemon.start(process.cwd())
    return { ok: true }
  })

  // Window controls
  ipcMain.on('window:minimize', () => win.minimize())
  ipcMain.on('window:maximize', () => {
    if (win.isMaximized()) win.unmaximize()
    else win.maximize()
  })
  ipcMain.on('window:close', () => win.close())

  // Tray badge
  ipcMain.on('tray:badge', (_e, n) => tray.setBadge(n))
}

module.exports = { register }
