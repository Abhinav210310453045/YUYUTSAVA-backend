const { app, BrowserWindow, dialog, nativeImage } = require('electron')
app.setName('YUYUTSAVA')

const path = require('path')
const daemon = require('./daemon')
const tray = require('./tray')
const ipcHandlers = require('./ipc-handlers')

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

  ipcHandlers.register(win)
  tray.init(win, daemon)

  // Forward daemon logs to renderer
  daemon.onLog(line => {
    if (win && !win.isDestroyed()) {
      win.webContents.send('daemon:log', line)
    }
  })
}

async function onReady() {
  if (process.platform === 'darwin') {
    const dockIcon = nativeImage.createFromPath(path.join(__dirname, '../../assets/icon.png'))
    if (!dockIcon.isEmpty()) app.dock.setIcon(dockIcon)
  }

  createWindow()

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
    if (response === 0) daemon.start(process.cwd())
  }

  // Keep the tray menu's daemon status accurate, including daemons started or
  // stopped externally (CLI / a prior session). Only rebuilds when it changes.
  let lastRunning = null
  setInterval(async () => {
    const running = daemon.isRunning() || await daemon.ping(daemon.getPort())
    if (running !== lastRunning) {
      lastRunning = running
      tray.refreshMenu(running)
    }
  }, 4000)
}

app.whenReady().then(onReady)

app.on('window-all-closed', () => {
  // On macOS keep app alive in tray; on other platforms quit
  if (process.platform !== 'darwin') app.quit()
})

app.on('activate', () => {
  if (win) { win.show(); win.focus() }
})

// Intercept Cmd+Q / app.quit() calls — show daemon dialog first if needed
app.on('before-quit', async (event) => {
  if (app.quitting) return  // already confirmed, let it proceed
  if (!daemon.isRunning() || !daemon.isManaged()) {
    app.quitting = true
    return
  }
  // Prevent the quit, show dialog, then decide
  event.preventDefault()
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
  if (response === 2) return  // user cancelled
  app.quitting = true
  if (response === 0) await daemon.stop()
  app.quit()
})
