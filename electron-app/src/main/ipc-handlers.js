const { ipcMain, dialog } = require('electron')
const http = require('http')
const fs = require('fs')
const settings = require('./settings')
const daemon = require('./daemon')
const tray = require('./tray')
const notifications = require('./notifications')

function _daemonRequest(method, path, body) {
  return new Promise((resolve, reject) => {
    const port = daemon.getPort()
    const data = body ? Buffer.from(JSON.stringify(body)) : null
    const req = http.request({
      host: '127.0.0.1', port, path, method,
      headers: {
        'Content-Type': 'application/json',
        ...(data ? { 'Content-Length': data.length } : {}),
      },
    }, res => {
      let chunks = ''
      res.on('data', c => chunks += c.toString())
      res.on('end', () => {
        try {
          const parsed = chunks ? JSON.parse(chunks) : null
          if (res.statusCode >= 400) reject({ status: res.statusCode, body: parsed })
          else resolve(parsed)
        } catch (e) { reject(e) }
      })
    })
    req.on('error', reject)
    req.setTimeout(5000, () => { req.destroy(new Error('daemon request timeout')) })
    if (data) req.write(data)
    req.end()
  })
}

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
  ipcMain.handle('daemon:status', async () => {
    const port = daemon.getPort()
    // A daemon may be running externally (started in a prior session, or by
    // the user via CLI). Treat the daemon as running if we either own the
    // process OR the port responds to a health check.
    const reachable = await daemon.ping(port)
    return {
      running: daemon.isRunning() || reachable,
      managed: daemon.isManaged(),
      port,
      external: reachable && !daemon.isRunning(),
    }
  })
  ipcMain.handle('daemon:start', async () => {
    const port = daemon.getPort()
    if (await daemon.ping(port)) { tray.refreshMenu(true); return { ok: true, alreadyRunning: true } }
    await daemon.start(process.cwd())
    tray.refreshMenu(await daemon.isAlive())
    return { ok: true }
  })
  ipcMain.handle('daemon:stop', async () => {
    await daemon.stop()
    tray.refreshMenu(await daemon.isAlive())
    return { ok: true }
  })
  ipcMain.handle('daemon:restart', async () => {
    await daemon.stop()
    // daemon.stop() already polls /health until it stops responding, so the
    // port is free. Start a fresh process. In-flight tasks resume from their
    // last checkpoint on the fresh daemon (resume_interrupted_tasks).
    await daemon.start(process.cwd())
    tray.refreshMenu(await daemon.isAlive())
    return { ok: true }
  })

  // Daemon-side config (events_config.json, permissions.json) — read/write
  // via the daemon's HTTP API so changes go through validation and SIGHUP.
  ipcMain.handle('daemon:getConfig', async (_e, kind) => {
    return await _daemonRequest('GET', `/config/${kind}`)
  })
  ipcMain.handle('daemon:saveConfig', async (_e, { kind, body }) => {
    return await _daemonRequest('PATCH', `/config/${kind}`, body)
  })
  ipcMain.handle('daemon:addWatchedDir', async (_e, path) => {
    return await _daemonRequest('POST', '/config/events/roots', { path })
  })
  ipcMain.handle('daemon:removeWatchedDir', async (_e, path) => {
    return await _daemonRequest('DELETE', `/config/events/roots?path=${encodeURIComponent(path)}`)
  })
  ipcMain.handle('dialog:pickDirectory', async () => {
    const r = await dialog.showOpenDialog({ properties: ['openDirectory', 'createDirectory'] })
    if (r.canceled || !r.filePaths || !r.filePaths[0]) return null
    return r.filePaths[0]
  })
  ipcMain.handle('dialog:saveFile', async (_e, { name, data }) => {
    const r = await dialog.showSaveDialog(win, {
      defaultPath: name || 'download',
      properties: ['createDirectory', 'showOverwriteConfirmation'],
    })
    if (r.canceled || !r.filePath) return null
    // `data` arrives as a Uint8Array/ArrayBuffer over IPC (structured clone).
    await fs.promises.writeFile(r.filePath, Buffer.from(data))
    return r.filePath
  })

  // Window controls
  ipcMain.on('window:minimize', () => win.minimize())
  ipcMain.on('window:maximize', () => {
    if (win.isMaximized()) win.unmaximize()
    else win.maximize()
  })
  ipcMain.on('window:close', () => win.close())

  // Tray badge
  ipcMain.on('tray:badge', (_e, n) => {
    tray.setBadge(n)
    // Renderer's reducer is the source of truth for pendingCount; when it
    // ticks down to zero we cancel any lingering Windows flash.
    if (n === 0) tray.clearAttention()
  })

  // Focus-aware OS notifications (renderer decides when to call this).
  notifications.init(win)
  ipcMain.on('notify:show', (_e, opts) => notifications.show(opts || {}))
}

module.exports = { register }
