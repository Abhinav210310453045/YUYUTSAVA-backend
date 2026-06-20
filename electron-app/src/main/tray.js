const { Tray, Menu, Notification, nativeImage, app } = require('electron')
const path = require('path')

let _tray = null
let _win = null
let _daemon = null
let _pendingCount = 0
let _daemonRunning = false
let _hintShown = false  // one "still in the menu bar" notice per app run

function init(win, daemonModule) {
  _win = win
  _daemon = daemonModule

  const iconPath = path.join(__dirname, '../../assets/tray-icon.png')
  let icon
  try {
    icon = nativeImage.createFromPath(iconPath)
    if (icon.isEmpty()) throw new Error('empty')
    icon = icon.resize({ width: 16, height: 16 })
    // On macOS a template image adapts to light/dark menu bars automatically.
    if (process.platform === 'darwin') icon.setTemplateImage(true)
  } catch {
    // fallback: tiny transparent icon so the tray still works without an asset
    icon = nativeImage.createEmpty()
  }

  _tray = new Tray(icon)
  _tray.setToolTip('YUYUTSAVA Terminal')
  _daemonRunning = _daemon ? _daemon.isRunning() : false
  refreshMenu()

  _tray.on('click', () => _showWindow())

  // Intercept close → hide to tray instead of quitting Electron, so the UI
  // can be reopened from the tray while the daemon keeps running.
  _win.on('close', (event) => {
    if (app.quitting) return
    event.preventDefault()
    _win.hide()
    _maybeShowHideHint()
  })
}

function _showWindow(navigateTo) {
  if (!_win || _win.isDestroyed()) return
  _win.show()
  _win.focus()
  if (navigateTo) _win.webContents.send('tray:navigate', navigateTo)
}

function _maybeShowHideHint() {
  if (_hintShown) return
  _hintShown = true
  if (!Notification.isSupported()) return
  try {
    new Notification({
      title: 'YUYUTSAVA is still running',
      body: 'The app lives in the menu bar and the daemon keeps working. '
          + 'Click the tray icon to reopen this window.',
      silent: true,
    }).show()
  } catch { /* notifications optional */ }
}

// Daemon controls invoked from the tray. They mirror the IPC handlers but are
// driven straight off the daemon module so the menu works without the window.
function _startDaemon() {
  if (!_daemon) return
  _daemon.start(process.cwd())
  setTimeout(() => refreshMenu(true), 800)
}

async function _stopDaemon() {
  if (!_daemon) return
  await _daemon.stop()
  refreshMenu(false)
}

async function _restartDaemon() {
  if (!_daemon) return
  await _daemon.stop()
  _daemon.start(process.cwd())
  setTimeout(() => refreshMenu(true), 800)
}

// Rebuild the context menu. ``running`` overrides the cached state (e.g. from
// index.js's status poll, which also sees externally-started daemons); when
// omitted we fall back to whether we own a live child process.
function refreshMenu(running) {
  if (!_tray) return
  if (typeof running === 'boolean') _daemonRunning = running
  else if (_daemon) _daemonRunning = _daemon.isRunning()

  const template = [
    { label: 'Open YUYUTSAVA', click: () => _showWindow('dashboard') },
    { label: 'Settings', click: () => _showWindow('settings') },
    { type: 'separator' },
    { label: _daemonRunning ? '● Daemon running' : '○ Daemon stopped', enabled: false },
  ]
  if (_daemonRunning) {
    template.push({ label: 'Restart Daemon', click: () => { _restartDaemon() } })
    template.push({ label: 'Stop Daemon', click: () => { _stopDaemon() } })
  } else {
    template.push({ label: 'Start Daemon', click: () => { _startDaemon() } })
  }
  template.push({ type: 'separator' })
  // before-quit in index.js handles the "what about the daemon?" dialog.
  template.push({ label: 'Quit YUYUTSAVA', click: () => app.quit() })

  _tray.setContextMenu(Menu.buildFromTemplate(template))
}

function setBadge(n) {
  _pendingCount = n
  if (process.platform === 'darwin') {
    app.dock.setBadge(n > 0 ? String(n) : '')
  }
  if (_tray) {
    _tray.setToolTip(`YUYUTSAVA Terminal${n > 0 ? ` — ${n} pending` : ''}`)
  }
}

// Increment the badge when an OS notification fires (window unfocused).
// The renderer's setBadge() will eventually overwrite with the canonical
// pendingCount once focus returns; this only bumps for visibility.
function incrPending() {
  setBadge(_pendingCount + 1)
}

// Called when the window regains focus — stop the dock-bounce-equivalent
// (no-op on macOS; on Windows we cancel flashFrame).
function clearAttention() {
  if (process.platform === 'win32' && _win && !_win.isDestroyed()) {
    try { _win.flashFrame(false) } catch {}
  }
}

module.exports = { init, refreshMenu, setBadge, incrPending, clearAttention }
