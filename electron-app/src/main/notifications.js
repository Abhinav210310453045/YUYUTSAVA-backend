// Focus-aware OS notifications.
//
// The renderer decides whether to render in-window (toast) or hand off to the
// OS — it has `document.hasFocus()`. When unfocused it calls `notify:show` and
// we surface an OS banner, bump the dock badge, and bounce/flash the icon
// until the user comes back. Click → focus the window and tell the renderer
// to scroll to the proposal that triggered the notification.

const { Notification, app, BrowserWindow } = require('electron')
const tray = require('./tray')

let _win = null
let _activeIds = new Set()  // ids the renderer expects clicks for

function init(win) { _win = win }

function _platformBounce() {
  if (process.platform === 'darwin') {
    try { app.dock.bounce('informational') } catch {}
  } else if (process.platform === 'win32' && _win && !_win.isDestroyed()) {
    try { _win.flashFrame(true) } catch {}
  }
}

function show({ title, body, proposalId, urgency }) {
  if (!Notification.isSupported()) return { ok: false, reason: 'unsupported' }

  // Tray badge + bounce always happen when an OS banner is shown.
  tray.incrPending()
  _platformBounce()

  const note = new Notification({
    title: title || 'YUYUTSAVA',
    body: body || '',
    silent: false,
    // urgent => critical so notification persists until interacted with
    urgency: urgency === 3 ? 'critical' : 'normal',
  })

  if (proposalId) {
    _activeIds.add(proposalId)
    note.on('click', () => {
      _activeIds.delete(proposalId)
      if (_win && !_win.isDestroyed()) {
        if (!_win.isVisible()) _win.show()
        _win.focus()
        _win.webContents.send('notify:click', { proposalId })
      }
    })
    note.on('close', () => { _activeIds.delete(proposalId) })
  }

  note.show()
  return { ok: true }
}

module.exports = { init, show }
