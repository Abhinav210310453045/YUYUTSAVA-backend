// First run: build the backend environment the daemon needs, on the user's
// machine, with the user watching.
//
// What ships in the installer is source — `yuyutsava/`, `pyproject.toml`,
// `uv.lock`, `scripts/` (see electron-builder.config.js `extraResources`). The
// `.venv` the daemon runs in has to be made here. Before this existed the app
// just ran `uv run` against an environment someone was assumed to have built in
// a terminal, and when that assumption was wrong the ENOENT went to a log
// channel nothing listens to: a window that looked fine and did nothing.
//
// uv is the only thing the user must already have. `uv run --no-project
// --python <pinned>` fetches the interpreter itself, so a machine with no
// Python at all still gets a working backend — and if uv is missing too, we
// offer to install it, showing the exact command first.
const { BrowserWindow, dialog } = require('electron')
const { spawn, spawnSync } = require('child_process')
const fs = require('fs')
const path = require('path')
const { backendRoot, spawnPath } = require('./daemon')

// scripts/install.py exits 3 for "a repair exists, ask the user yourself", and
// prints the command on a `heal:` line. Consent for anything that touches the
// user's machine is asked here, in a dialog, not in a terminal prompt nobody
// would see.
const NEEDS_CONSENT = 3
const HEAL_LINE = /^heal:\s*(.+)$/m

const UV_HOME = 'https://docs.astral.sh/uv/'
const UV_INSTALL =
  process.platform === 'win32'
    ? [
        'powershell',
        ['-ExecutionPolicy', 'ByPass', '-c', 'irm https://astral.sh/uv/install.ps1 | iex'],
      ]
    : ['/bin/sh', ['-c', 'curl -LsSf https://astral.sh/uv/install.sh | sh']]

const UV_FIX = `Install uv (${UV_HOME}), then start YUYUTSAVA again.`

function pinnedPython(root) {
  try {
    const found = fs
      .readFileSync(path.join(root, '.python-version'), 'utf8')
      .split(/\r?\n/)
      .map((line) => line.trim())
      .find((line) => line && !line.startsWith('#'))
    if (found) return found
  } catch {
    // Not shipped, or unreadable: fall through to the floor this app was built on.
  }
  return '3.11'
}

function hasUv(env) {
  try {
    return spawnSync('uv', ['--version'], { env, encoding: 'utf8' }).status === 0
  } catch {
    return false
  }
}

function venvPython(root) {
  return process.platform === 'win32'
    ? path.join(root, '.venv', 'Scripts', 'python.exe')
    : path.join(root, '.venv', 'bin', 'python')
}

// Enough to know whether `uv run` has something to run. Whether that
// environment matches the lock is the daemon's own start-up check.
function backendReady(root) {
  return fs.existsSync(venvPython(root))
}

// A window that exists before anything else does: plain HTML beside this file,
// not part of the renderer bundle, because setup has to work when the app's own
// environment is exactly what is missing. It is driven by executeJavaScript
// rather than IPC so it needs no preload and adds no channels.
function openWindow(parent) {
  const win = new BrowserWindow({
    parent,
    width: 720,
    height: 460,
    title: 'Setting up YUYUTSAVA',
    backgroundColor: '#0a0a0f',
    autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  })
  win.loadFile(path.join(__dirname, 'setup.html'))
  return win
}

function reporter(win) {
  let loaded = false
  const queued = []
  const run = (js) => {
    if (win.isDestroyed()) return
    win.webContents.executeJavaScript(js).catch(() => {
      // The window went away mid-install; the install itself carries on.
    })
  }
  win.webContents.once('did-finish-load', () => {
    loaded = true
    for (const js of queued.splice(0)) run(js)
  })
  const send = (js) => (loaded ? run(js) : queued.push(js))
  return {
    step: (text) => send(`window.setStep(${JSON.stringify(text)})`),
    line: (text) => send(`window.append(${JSON.stringify(text)})`),
  }
}

// Run a child, streaming every line to the setup window and keeping the whole
// of it for the `heal:` contract and for the error dialog.
function stream(command, args, options, report) {
  return new Promise((resolve) => {
    let child
    try {
      child = spawn(command, args, options)
    } catch (err) {
      resolve({ code: -1, output: String(err && err.message) })
      return
    }
    let output = ''
    const take = (data) => {
      const text = data.toString()
      output += text
      for (const line of text.split(/\r?\n/)) if (line.trim()) report.line(line)
    }
    child.stdout.on('data', take)
    child.stderr.on('data', take)
    child.on('error', (err) => resolve({ code: -1, output: `${output}\n${err.message}` }))
    child.on('close', (code) => resolve({ code, output }))
  })
}

function runInstaller(root, env, heal, report) {
  // `--ui` only: the app needs the .venv it runs the daemon in, not a
  // `yuyutsava` command on the user's PATH.
  const args = [
    'run',
    '--no-project',
    '--python',
    pinnedPython(root),
    'python',
    path.join('scripts', 'install.py'),
    '--ui',
    `--heal=${heal}`,
  ]
  return stream('uv', args, { cwd: root, env }, report)
}

async function agreed(parent, message, detail, confirm) {
  const { response } = await dialog.showMessageBox(parent || null, {
    type: 'question',
    title: 'YUYUTSAVA',
    message,
    detail,
    buttons: [confirm, 'Cancel'],
    defaultId: 0,
    cancelId: 1,
  })
  return response === 0
}

async function installUv(parent, env, report) {
  const [command, args] = UV_INSTALL
  const ok = await agreed(
    parent,
    'YUYUTSAVA needs uv to set itself up.',
    `uv is the package manager that builds the backend environment (and fetches ` +
      `Python itself, so nothing else has to be installed first).\n\n` +
      `This will run:\n  ${args[args.length - 1]}\n\nIt installs uv for your user account only.`,
    'Install uv',
  )
  if (!ok) return false
  report.step('Installing uv...')
  const { code } = await stream(command, args, { env }, report)
  return code === 0 && hasUv({ ...env, PATH: spawnPath() })
}

async function healAndRetry(parent, root, env, report, output) {
  const found = HEAL_LINE.exec(output)
  if (!found) return null
  const ok = await agreed(
    parent,
    'Setup hit a problem with this machine, and can repair it.',
    `${lastProblem(output)}\n\nIt would run:\n  ${found[1]}\n\n` +
      `This only clears cached downloads; uv fetches them again.`,
    'Repair and retry',
  )
  if (!ok) return null
  report.step('Repairing, then retrying...')
  return runInstaller(root, env, 'auto', report)
}

// The installer says what went wrong in the user's terms, on a line starting
// with "!". Surface that rather than the last line of uv's own output. Failing
// that, the last line that was meant for a person: not the `$ command` echoes,
// and not the `heal:` line, which is addressed to this module rather than them.
const SPOKEN = /^!\s+/
function lastProblem(output) {
  const lines = output
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
  const spoken = lines.filter((line) => SPOKEN.test(line))
  if (spoken.length) return spoken[spoken.length - 1].replace(SPOKEN, '')
  const quiet = lines.filter((line) => !line.startsWith('$ ') && !HEAL_LINE.test(line))
  return quiet.length ? quiet[quiet.length - 1] : 'The setup step failed.'
}

/**
 * Make sure the backend environment exists. Returns `{ ok }` on success, or
 * `{ ok: false, problem, fix }` — the same shape the rest of the product uses
 * to say something is wrong and what to do about it.
 */
async function ensure(parent) {
  const root = backendRoot()
  const env = { ...process.env, PATH: spawnPath() }

  if (hasUv(env) && backendReady(root)) return { ok: true, skipped: true }

  if (!fs.existsSync(path.join(root, 'scripts', 'install.py'))) {
    return {
      ok: false,
      problem: 'This build cannot set itself up: the installer is missing.',
      fix: `Install from the checkout instead: python scripts/install.py. (${root})`,
    }
  }

  const win = openWindow(parent)
  const report = reporter(win)
  try {
    if (!hasUv(env)) {
      report.step('Checking for uv...')
      if (!(await installUv(parent, env, report))) {
        return { ok: false, problem: 'uv is not installed.', fix: UV_FIX }
      }
      env.PATH = spawnPath()
    }

    report.step('Building the backend environment. This takes a few minutes.')
    let result = await runInstaller(root, env, 'never', report)
    if (result.code === NEEDS_CONSENT) {
      result = (await healAndRetry(parent, root, env, report, result.output)) || result
    }
    if (result.code !== 0) {
      return { ok: false, problem: lastProblem(result.output), fix: nextStep(root) }
    }
    report.step('Done.')
    return { ok: true }
  } finally {
    if (!win.isDestroyed()) win.destroy()
  }
}

function nextStep(root) {
  return `Run this in a terminal to see the whole story: python scripts/install.py --doctor (in ${root})`
}

module.exports = { ensure, backendReady, pinnedPython, hasUv, lastProblem, venvPython }
