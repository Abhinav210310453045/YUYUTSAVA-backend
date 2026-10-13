// Getting from "the app opened" to "a daemon is running", including every way
// that can fail on someone else's machine.
//
// Two of those failures used to be invisible. A backend environment that was
// never built is normal on a fresh install — the app ships source, not a .venv
// (electron-builder.config.js `extraResources`) — and a spawn that failed for
// want of uv reached only daemon.onLog, which no renderer component subscribes
// to. Both left a window that looked fine and did nothing. Here each one is
// either fixed (setup.ensure) or said out loud, as a problem and a fix.
const { dialog } = require('electron')
const daemon = require('./daemon')
const setup = require('./setup')

const TITLE = 'YUYUTSAVA Terminal'

function tell(win, problem, fix) {
  return dialog.showMessageBox(win, {
    type: 'error',
    title: TITLE,
    message: problem,
    detail: fix,
    buttons: ['OK'],
  })
}

async function offerToStart(win, port) {
  const { response } = await dialog.showMessageBox(win, {
    type: 'question',
    title: TITLE,
    message: 'The YUYUTSAVA daemon is not running.',
    detail: `Start the daemon now? It will watch your workspace on port ${port}.`,
    buttons: ['Start Daemon', 'Connect Later'],
    defaultId: 0,
  })
  if (response !== 0) return
  const started = await daemon.start(process.cwd())
  if (started.ok) return
  await tell(win, started.problem, [started.fix, started.detail].filter(Boolean).join('\n\n'))
}

/**
 * Build the backend environment if it isn't there, then offer to start the
 * daemon. Returns nothing: every outcome the user needs to know about has
 * already been put in front of them.
 */
async function bringUp(win, port) {
  // A no-op once the environment exists, so this costs a fresh install a few
  // minutes and every later launch nothing.
  const ready = await setup.ensure(win)
  if (!ready.ok) {
    // Say what is wrong and what to do, then carry on into the tray rather than
    // quitting: a quiet, broken window is the thing this replaces.
    await tell(win, ready.problem, ready.fix)
    return
  }
  await offerToStart(win, port)
}

module.exports = { bringUp }
