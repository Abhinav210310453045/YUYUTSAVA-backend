const { app, BrowserWindow, dialog, nativeImage, globalShortcut, ipcMain } = require('electron')
app.setName('YUYUTSAVA')

const path = require('path')
const daemon = require('./daemon')
const tray = require('./tray')
const ipcHandlers = require('./ipc-handlers')
const overlay = require('./overlay')
const settings = require('./settings')

// Global hotkey that summons the voice UI. Overridable via VOICE_HOTKEY in the
// daemon .env (Electron settings); falls back to a sensible default.
const DEFAULT_VOICE_HOTKEY = 'CommandOrControl+Shift+Y'

const isDev = process.env.NODE_ENV === 'development' || !app.isPackaged

let win = null

function createWindow() {
  win = new BrowserWindow({
    title: 'YUYUTSAVA',
    width: 1280,
    height: 800,
    minWidth: 900,
    minHeight: 600,
    frame: false,
    titleBarStyle: 'hidden',
    trafficLightPosition: { x: 16, y: 16 },
    vibrancy: 'under-window',
    visualEffectState: 'active',
    backgroundColor: '#0a0a0f',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  })

  if (isDev) {
    win.loadURL('http://localhost:5173')
    win.webContents.openDevTools({ mode: 'detach' })
  } else {
    win.loadFile(path.join(__dirname, '../../dist/renderer/index.html'))
  }

  // Recover from a blank screen: if the renderer process dies (crash/OOM) the
  // window is left white with nothing to re-bootstrap it. Reload it once it's
  // gone (not on a clean exit during quit) so the UI comes back on its own.
  win.webContents.on('render-process-gone', (_e, details) => {
    if (app.quitting || win.isDestroyed()) return
    if (details && details.reason === 'clean-exit') return
    try { win.reload() } catch {}
  })

  ipcHandlers.register(win)
  tray.init(win, daemon)

  // Forward daemon logs to renderer
  daemon.onLog(line => {
    if (win && !win.isDestroyed()) {
      win.webContents.send('daemon:log', line)
    }
  })
}

// Summon the voice UI. When the main window is focused and visible, route to
// the in-app Voice panel (richer surface); otherwise pop the mini overlay.
function activateVoice({ reason = 'hotkey', wakeWord = '' } = {}) {
  const mainFocused = win && !win.isDestroyed() && win.isFocused() && !win.isMinimized()
  if (mainFocused) {
    overlay.hide()
    // App handles both the nav to the Voice panel and auto-starting the mic.
    win.webContents.send('voice:activate', { reason, wakeWord })
  } else {
    overlay.show({ reason, wakeWord })
  }
}

function registerVoiceHotkey() {
  const cfg = (() => { try { return settings.readSettings() } catch { return {} } })()
  const accel = (cfg.VOICE_HOTKEY || DEFAULT_VOICE_HOTKEY).trim()
  globalShortcut.unregisterAll()
  try {
    const ok = globalShortcut.register(accel, () => activateVoice({ reason: 'hotkey' }))
    if (!ok) console.warn(`voice hotkey ${accel} could not be registered (in use?)`)
  } catch (e) {
    console.warn(`voice hotkey ${accel} invalid:`, e.message)
  }
}

async function onReady() {
  if (process.platform === 'darwin') {
    const dockIcon = nativeImage.createFromPath(path.join(__dirname, '../../assets/icon.png'))
    if (!dockIcon.isEmpty()) app.dock.setIcon(dockIcon)
  }

  createWindow()

  registerVoiceHotkey()
  // Renderer (SSE) forwards a daemon wake-word detection here; main decides
  // overlay-vs-panel. Also lets the overlay/panel ask main to re-summon.
  // Stage "open" pops the surface; stage "command" carries the same-breath
  // trailing command and is relayed to the already-open overlay as a seed turn
  // (no re-pop), so it doesn't replay the open earcon / restart the mic.
  ipcMain.on('voice:wake', (_e, payload) => {
    const p = payload || {}
    if (p.stage === 'command') {
      // Relay the same-breath command only to the overlay — it gates its mic on
      // this signal, so it needs the seed. The focused Voice panel starts its mic
      // immediately on activate and captures the command via its own live mic, so
      // seeding it would double-capture; we skip it there.
      const mainFocused = win && !win.isDestroyed() && win.isFocused() && !win.isMinimized()
      if (!mainFocused) overlay.sendCommand({ command: p.command || '' })
      return
    }
    activateVoice({ reason: 'wake', ...p })
  })
  ipcMain.on('overlay:close', () => overlay.hide())
  // Re-read the hotkey after a settings save (it may have changed).
  ipcMain.on('voice:rebindHotkey', () => registerVoiceHotkey())

  const port = daemon.getPort()
  const reachable = await daemon.ping(port)

  if (!reachable && daemon.isManaged()) {
    const { response } = await dialog.showMessageBox(win, {
      type: 'question',
      title: 'YUYUTSAVA Terminal',
      message: 'The YUYUTSAVA daemon is not running.',
      detail: `Start the daemon now? It will watch your workspace on port ${port}.`,
      buttons: ['Start Daemon', 'Connect Later'],
      defaultId: 0,
    })
    if (response === 0) await daemon.start(process.cwd())
  }

  // Keep the tray menu's daemon status accurate, including daemons started or
  // stopped externally (CLI / a prior session). isAlive() also consults the
  // discovery file, so a terminal-launched daemon shows as running. Only
  // rebuilds when the state changes; seed it immediately so the menu is correct
  // at startup rather than after the first tick.
  let lastRunning = null
  const syncTray = async () => {
    const running = await daemon.isAlive()
    if (running !== lastRunning) {
      // The daemon went away after having been up (e.g. closed from the
      // terminal, or `yuyutsava daemon --stop`). The UI is useless without it,
      // so quit the app instead of leaving a dead window/tray behind. Guard on
      // lastRunning === true so we never quit during the startup window when the
      // daemon simply hasn't come up yet (null → false). Also skip while a
      // restart is in flight — the daemon is intentionally down for a moment and
      // will be back, so quitting would tear the UI away from its own restart.
      if (lastRunning === true && running === false && !app.quitting && !daemon.isRestarting()) {
        app.quitting = true  // skip the before-quit daemon dialog; it's already dead
        app.quit()
        return
      }
      lastRunning = running
      tray.refreshMenu(running)
    }
  }
  await syncTray()
  setInterval(syncTray, 4000)
}

app.whenReady().then(onReady)

app.on('window-all-closed', () => {
  // On macOS keep app alive in tray; on other platforms quit
  if (process.platform !== 'darwin') app.quit()
})

app.on('will-quit', () => {
  globalShortcut.unregisterAll()
  overlay.destroy()
})

app.on('activate', () => {
  if (win) { win.show(); win.focus() }
})

// Intercept Cmd+Q / app.quit() calls — show daemon dialog first if needed
app.on('before-quit', async (event) => {
  if (app.quitting) return  // already confirmed, let it proceed
  // Never let a quit slip through while a restart is mid-flight. A restart
  // briefly stops the daemon, and any stray quit (e.g. a racing status poll)
  // would tear the window away from its own restart, leaving the daemon running
  // headless. Swallow it — the restart will bring the daemon back and the UI
  // stays put.
  if (daemon.isRestarting()) {
    event.preventDefault()
    return
  }
  // Hold the quit synchronously while we check daemon state + ask. (Any await
  // before preventDefault() comes too late and the app would quit anyway.)
  event.preventDefault()
  // Offer to stop any live daemon — stop() can now reach a terminal-launched
  // one too, not just a child we spawned.
  if (!(await daemon.isAlive())) {
    app.quitting = true
    app.quit()
    return
  }
  const w = win || BrowserWindow.getAllWindows()[0]
  const { response } = await dialog.showMessageBox(w || null, {
    type: 'question',
    title: 'YUYUTSAVA Terminal',
    message: 'What should happen to the YUYUTSAVA daemon?',
    detail: 'The daemon is currently running and watching your files.',
    buttons: ['Stop Daemon & Quit', 'Keep Daemon Running', 'Cancel'],
    defaultId: 0,
    cancelId: 2,
  })
  if (response === 2) return  // user cancelled — stay open
  app.quitting = true
  if (response === 0) await daemon.stop()
  app.quit()
})
