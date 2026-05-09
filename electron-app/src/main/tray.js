const { Tray, Menu, nativeImage, app } = require('electron')
const path = require('path')

let _tray = null
let _win = null
let _pendingCount = 0

function init(win, daemonModule) {
  _win = win

  const iconPath = path.join(__dirname, '../../assets/tray-icon.png')
  let icon
  try {
    icon = nativeImage.createFromPath(iconPath)
    if (icon.isEmpty()) throw new Error('empty')
    icon = icon.resize({ width: 16, height: 16 })
  } catch {
    // fallback: tiny 1x1 transparent icon so the tray still works without an asset
    icon = nativeImage.createEmpty()
  }

  _tray = new Tray(icon)
  _tray.setToolTip('YUYUTSAVA Terminal')
  _updateMenu()

  _tray.on('click', () => {
    if (_win.isVisible()) {
      _win.focus()
    } else {
      _win.show()
      _win.focus()
    }
  })

  // Intercept close → hide to tray instead
  _win.on('close', (event) => {
    if (app.quitting) return
    event.preventDefault()
    _win.hide()
  })
}

function _updateMenu() {
  if (!_tray) return
  const menu = Menu.buildFromTemplate([
    { label: 'Show Window', click: () => { _win.show(); _win.focus() } },
    { type: 'separator' },
    { label: 'Quit', click: () => app.quit() },  // before-quit in index.js handles dialog
  ])
  _tray.setContextMenu(menu)
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

module.exports = { init, setBadge }
