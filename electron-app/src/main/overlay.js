// Mini voice overlay — a frameless, transparent, always-on-top window that
// slides up from the bottom-right when the global hotkey is pressed or a wake
// word fires while the main window is unfocused/minimized. It hosts a thin
// second renderer (overlay.html) over the same /ws/converse conversation.
//
// The window stays alive (created lazily, then hidden) so show/hide is instant
// and the earcons/animation play without a cold load each time.

const { BrowserWindow, screen } = require('electron')
const path = require('path')

const isDev = process.env.NODE_ENV === 'development' || !require('electron').app.isPackaged

const WIDTH = 360
// Taller than a one-shot toast: the overlay now stays open across a conversation
// and renders the current reply in full (the message area scrolls internally for
// long answers), so it needs room for the streamed text.
const HEIGHT = 320
const MARGIN = 24

let overlayWin = null

function _position(winw) {
  // Bottom-right of the display under the cursor (multi-monitor friendly).
  const point = screen.getCursorScreenPoint()
  const display = screen.getDisplayNearestPoint(point)
  const { x, y, width, height } = display.workArea
  return {
    x: Math.round(x + width - WIDTH - MARGIN),
    y: Math.round(y + height - HEIGHT - MARGIN),
  }
}

function create() {
  if (overlayWin && !overlayWin.isDestroyed()) return overlayWin

  overlayWin = new BrowserWindow({
    width: WIDTH,
    height: HEIGHT,
    show: false,
    frame: false,
    transparent: true,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    hasShadow: false,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  })

  // Float above full-screen apps too.
  overlayWin.setAlwaysOnTop(true, 'screen-saver')
  overlayWin.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true })

  if (isDev) {
    overlayWin.loadURL('http://localhost:5173/overlay.html')
  } else {
    overlayWin.loadFile(path.join(__dirname, '../../dist/renderer/overlay.html'))
  }

  // Closing (e.g. user gesture) just hides; we keep the instance warm.
  overlayWin.on('close', (e) => {
    if (!require('electron').app.quitting) {
      e.preventDefault()
      hide()
    }
  })

  return overlayWin
}

// Show the overlay and tell its renderer to start listening. `reason` is
// 'hotkey' | 'wake'; `wakeWord` is set for wake-triggered opens.
function show({ reason = 'hotkey', wakeWord = '' } = {}) {
  const w = create()
  const { x, y } = _position(WIDTH)
  w.setBounds({ x, y, width: WIDTH, height: HEIGHT })
  // Re-assert float-over-everything on every show. macOS resets a window's
  // collection behaviour when it's hidden/shown, so a one-time setup in create()
  // is not enough to keep the overlay visible over *another app's* native
  // full-screen Space — it must be re-applied each time, after positioning and
  // before showing. 'screen-saver' is the highest standard level; the
  // visibleOnFullScreen flag is what lets it cross into a full-screen Space.
  w.setAlwaysOnTop(true, 'screen-saver')
  w.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true })
  w.showInactive() // don't steal focus from the user's current app
  const send = () => w.webContents.send('overlay:activate', { reason, wakeWord })
  if (w.webContents.isLoading()) {
    w.webContents.once('did-finish-load', send)
  } else {
    send()
  }
}

// Relay a same-breath trailing command to the open overlay so it seeds the first
// turn (instead of re-popping). No-op if the overlay isn't around; the renderer
// also has a fallback timer so a dropped command can't wedge its mic.
function sendCommand({ command = '' } = {}) {
  if (!overlayWin || overlayWin.isDestroyed()) return
  const send = () => overlayWin.webContents.send('overlay:command', { command })
  if (overlayWin.webContents.isLoading()) {
    overlayWin.webContents.once('did-finish-load', send)
  } else {
    send()
  }
}

function hide() {
  if (overlayWin && !overlayWin.isDestroyed() && overlayWin.isVisible()) {
    overlayWin.hide()
  }
}

function isVisible() {
  return !!(overlayWin && !overlayWin.isDestroyed() && overlayWin.isVisible())
}

function destroy() {
  if (overlayWin && !overlayWin.isDestroyed()) {
    overlayWin.destroy()
  }
  overlayWin = null
}

module.exports = { show, hide, isVisible, destroy, create, sendCommand }
