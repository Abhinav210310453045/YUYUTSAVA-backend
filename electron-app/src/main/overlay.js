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

// Sized for two jobs, not one. It stays open across a whole voice conversation
// and renders the reply in full, and it now also carries permission requests —
// which expand to a full command, every path, the reason and the risk. Both
// were cramped at 360x320; the message/detail areas still scroll internally,
// this just stops everything fighting for the same 40 pixels.
const WIDTH = 420
const HEIGHT = 440
const MARGIN = 24

let overlayWin = null
// What the window is currently up for: 'voice' | 'ask' | null. The overlay is a
// single shared window, so dismissing an ask must not tear down a live voice
// conversation that happens to be using it.
let shownFor = null

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
      // The overlay spends most of its life hidden, and it streams audio and
      // prose the moment it appears — a throttled timer budget would stall
      // both. Same reasoning as the main window (see main/index.js).
      backgroundThrottling: false,
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
  shownFor = 'voice'
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

// Pop the overlay for a pending ask, without stealing focus.
//
// This is the surface that reaches the user when they are not in YUYUTSAVA at
// all — the whole reason a permission prompt is a pop-up and not just an inline
// block. `showInactive()` is load-bearing: an ask arriving while you're typing
// somewhere else must never take the keyboard away from you.
//
// Skipped when the main window is focused: the owning view shows the ask inline
// and the Inbox lists it, so an overlay on top would be a third copy of
// something already in front of the user.
function showAsk({ title = '', agent = '', mainFocused = false } = {}) {
  if (mainFocused) return
  const w = create()
  const { x, y } = _position(WIDTH)
  w.setBounds({ x, y, width: WIDTH, height: HEIGHT })
  // macOS resets a window's collection behaviour on hide/show, so these have to
  // be re-applied every time or the overlay won't cross into another app's
  // full-screen Space — exactly where an ask most needs to reach the user.
  w.setAlwaysOnTop(true, 'screen-saver')
  w.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true })
  const wasVisible = w.isVisible()
  if (!wasVisible) w.showInactive()
  // Don't relabel a window that a live voice conversation is using — the ask
  // renders alongside it, and dismissing the ask later must leave voice alone.
  if (shownFor !== 'voice') shownFor = 'ask'
  // Bounce the dock / flash the taskbar: quiet, but noticeable when the user is
  // heads-down in another app. Only on the first pop, so a second ask arriving
  // while the window is already up doesn't rattle the dock again.
  if (!wasVisible) { try { require('./notifications').bounce() } catch {} }
  const send = () => w.webContents.send('overlay:ask', { title, agent })
  if (w.webContents.isLoading()) w.webContents.once('did-finish-load', send)
  else send()
}

// Dismiss an ask overlay. No-op when the window is up for a voice
// conversation: closing an ask card is not a reason to cut the user off
// mid-sentence.
function hideAsk() {
  if (shownFor === 'voice') return
  hide()
}

function hide() {
  shownFor = null
  if (overlayWin && !overlayWin.isDestroyed() && overlayWin.isVisible()) {
    overlayWin.hide()
  }
}

// Why the overlay is currently up: 'voice' | 'ask' | null. The renderer asks
// on mount, because the window is now shared: it must start the microphone
// when it was summoned for a voice turn, and stay silent when it was summoned
// to show a permission request.
function currentReason() { return shownFor }

function isVisible() {
  return !!(overlayWin && !overlayWin.isDestroyed() && overlayWin.isVisible())
}

function destroy() {
  if (overlayWin && !overlayWin.isDestroyed()) {
    overlayWin.destroy()
  }
  overlayWin = null
}

module.exports = { show, showAsk, hide, hideAsk, isVisible, destroy, create, sendCommand, currentReason }
