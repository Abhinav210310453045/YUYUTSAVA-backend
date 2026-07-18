const { spawn } = require('child_process')
const { app } = require('electron')
const http = require('http')
const fs = require('fs')
const os = require('os')
const path = require('path')
const { readSettings } = require('./settings')

let _proc = null
let _logCallback = null
let _managed = true  // default: app manages the daemon
let _restarting = false  // true while restart() tears down + brings back up

// True during the stop→start window of a restart. The status poll in index.js
// checks this so the transient "daemon down" state doesn't get mistaken for a
// crash and quit the whole app.
function isRestarting() { return _restarting }

function getPort() {
  const s = readSettings()
  return parseInt(s['YUYUTSAVA_DAEMON_PORT'] || '7654', 10)
}

function isManaged() { return _managed }
function setManaged(v) { _managed = v }

function isRunning() {
  return _proc !== null && _proc.exitCode === null
}

// --- Daemon discovery (mirrors yuyutsava/daemon/singleton.py) -------------
// The Python daemon writes <state_dir>/daemon.json with {pid, web_url, ...} on
// startup and unlinks it on clean shutdown. state_dir() is YUYUTSAVA_HOME or
// ~/.yuyutsava (see yuyutsava/storage/paths.py). Reading this lets us control a
// daemon launched by the terminal — not just one we spawned ourselves.
function stateDir() {
  const raw = (process.env.YUYUTSAVA_HOME || '').trim()
  if (raw) {
    return raw.startsWith('~') ? path.join(os.homedir(), raw.slice(1)) : raw
  }
  return path.join(os.homedir(), '.yuyutsava')
}

function discoveryPath() { return path.join(stateDir(), 'daemon.json') }
function lockPath() { return path.join(stateDir(), 'daemon.lock') }

function readDiscovery() {
  try {
    return JSON.parse(fs.readFileSync(discoveryPath(), 'utf8'))
  } catch (_) {
    return null  // missing or malformed
  }
}

// Mirror of singleton.py:_is_pid_alive — ESRCH→dead, EPERM→alive.
function pidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false
  try {
    process.kill(pid, 0)
    return true
  } catch (e) {
    return e.code === 'EPERM'
  }
}

// PID of the actual daemon: prefer the discovery file (authoritative across
// launch modes), fall back to a process we own.
function getDaemonPid() {
  const disco = readDiscovery()
  if (disco && pidAlive(disco.pid)) return disco.pid
  if (_proc && _proc.exitCode === null) return _proc.pid
  return null
}

// Single source of truth for "is a daemon up", regardless of who launched it.
async function isAlive() {
  if (isRunning()) return true
  const disco = readDiscovery()
  if (disco && pidAlive(disco.pid)) return true
  return ping(getPort())
}

function _unlinkStaleFiles() {
  for (const p of [discoveryPath(), lockPath()]) {
    try { fs.unlinkSync(p) } catch (_) {}
  }
}

function onLog(cb) { _logCallback = cb }

function _log(line) {
  if (_logCallback) _logCallback(line)
}

// Directory that holds the Python backend's pyproject.toml — the cwd `uv run`
// needs to resolve the project. Two layouts:
//   • Dev checkout: this file is <root>/electron-app/src/main/daemon.js, so the
//     backend is three levels up.
//   • Packaged app: the backend is bundled to resources/backend (see
//     electron-builder.config.js `extraResources`); __dirname is inside the
//     asar, three levels up is NOT the backend, and process.cwd() (Start-Menu
//     launch) is often System32 — so neither of those can be trusted.
// This is the P0 fix for the packaged Windows/macOS app: without it `uv run`
// starts in the wrong directory and dies with "no pyproject.toml".
function backendRoot() {
  if (app && app.isPackaged) return path.join(process.resourcesPath, 'backend')
  return path.resolve(__dirname, '../../..')
}

// PATH that finds `uv` regardless of how Electron was launched (shell vs Dock/
// Explorer, which start with a minimal PATH). Covers uv's standalone
// (~/.local/bin), brew (/opt/homebrew/bin) and Windows (WinGet Links,
// %LOCALAPPDATA%\Programs) install locations.
function _spawnPath() {
  const home = os.homedir()
  const extra = process.platform === 'win32'
    ? [
        path.join(home, '.local', 'bin'), // uv standalone installer
        path.join(home, '.cargo', 'bin'),
        path.join(process.env.LOCALAPPDATA || path.join(home, 'AppData', 'Local'),
                  'Microsoft', 'WinGet', 'Links'),
        path.join(process.env.LOCALAPPDATA || path.join(home, 'AppData', 'Local'),
                  'Programs', 'uv'),
      ]
    : [
        '/opt/homebrew/bin',
        '/usr/local/bin',
        path.join(home, '.local/bin'),
        path.join(home, '.cargo/bin'),
      ]
  return [...extra, process.env.PATH].filter(Boolean).join(path.delimiter)
}

async function start(workspacePath) {
  if (isRunning()) return
  // Idempotent: never spawn a duplicate. A daemon launched from the terminal
  // (or a prior session) already holds the singleton lock — a second spawn
  // would just die on it. Detect it via the discovery file / health probe.
  const disco = readDiscovery()
  if ((disco && pidAlive(disco.pid)) || await ping(getPort())) {
    _log('[daemon] already running; not spawning a duplicate\n')
    return
  }
  // No live daemon: clear any lock/discovery left by an ungraceful prior exit
  // so it can't block the new daemon's singleton lock or fool the guard above.
  _unlinkStaleFiles()

  // `uv run` MUST start in the backend dir (has pyproject.toml). The agent
  // workspace (--workspace) can be a caller-chosen dir in a dev checkout, but a
  // packaged launch passes process.cwd() (System32 on Windows), which is not a
  // usable workspace — fall back to the backend root there.
  const codeRoot = backendRoot()
  const packaged = !!(app && app.isPackaged)
  const workspace = (!packaged && workspacePath) ? workspacePath : codeRoot
  const settings = readSettings()
  const env = { ...process.env, ...Object.fromEntries(Object.entries(settings)) }
  env.PATH = _spawnPath()

  // detached:true puts the child in its own process group on POSIX so we can
  // signal `uv` AND its python grandchild together via `kill(-pid, ...)`.
  // Without this, SIGTERM reaches `uv` but not the python process it spawns.
  // (Windows has no process groups; _killGroup uses taskkill /T instead.)
  const isPosix = process.platform !== 'win32'
  _proc = spawn('uv', ['run', 'yuyutsava', 'daemon', '--no-ui', '--workspace', workspace], {
    env,
    cwd: codeRoot,
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: isPosix,
  })

  _proc.stdout.on('data', d => _log(d.toString()))
  _proc.stderr.on('data', d => _log(d.toString()))
  // Without this handler a failed spawn (e.g. `uv` not on PATH → ENOENT) is
  // swallowed and "Start Daemon" silently does nothing.
  _proc.on('error', (err) => {
    _log(`[daemon] failed to start: ${err.message}\n`)
    _proc = null
  })
  _proc.on('exit', (code) => {
    _log(`[daemon] exited with code ${code}`)
    _proc = null
  })
}

// Kill a PID and every descendant. POSIX: SIGTERM/SIGKILL the whole process
// group. Windows: `proc.kill()` reaps only the `uv` wrapper and orphans the
// python grandchild, so use `taskkill /T` (tree) `/F` (force) instead — Node
// maps any signal to a hard TerminateProcess on Windows anyway, so there is no
// graceful signal to preserve here.
function _killTree(pid, signal) {
  if (!Number.isInteger(pid) || pid <= 0) return
  if (process.platform === 'win32') {
    try { spawn('taskkill', ['/pid', String(pid), '/T', '/F'], { stdio: 'ignore' }) } catch (_) {}
    return
  }
  try { process.kill(-pid, signal) }        // negative pid → process group
  catch (_) { try { process.kill(pid, signal) } catch (_) {} }
}

function _killGroup(proc, signal) {
  if (!proc || proc.exitCode !== null) return
  _killTree(proc.pid, signal)
}

async function stop() {
  // Target the real daemon, whoever launched it. The discovery file carries the
  // python PID (which owns the SIGTERM handler); falling back to _proc covers
  // the brief window before discovery is written.
  const pid = getDaemonPid()
  if (pid === null) { _proc = null; return }

  if (_proc && _proc.exitCode === null) {
    // We own it: kill the whole tree so the `uv` wrapper AND its python
    // grandchild are reaped (on Windows the wrapper alone would orphan python).
    _killGroup(_proc, 'SIGTERM')
  } else {
    // Externally launched (e.g. terminal). The discovery PID IS the python
    // daemon, so signal it directly rather than its group (a POSIX group kill
    // would include this Electron process and take the UI down). On POSIX the
    // python SIGTERM handler tears down + unlinks the lock/discovery files; on
    // Windows Node maps the signal to a hard TerminateProcess (no graceful
    // teardown — SQLite's per-transaction durability keeps state intact, and
    // the stale-file sweep below / next start() clears the lock).
    try { process.kill(pid, 'SIGTERM') } catch (_) {}
  }

  // Wait for the daemon to actually go away: /health stops responding AND the
  // discovery file is gone (mirrors `yuyutsava daemon --stop`). SIGKILL fallback.
  const port = getPort()
  const deadline = Date.now() + 5000
  let stopped = false
  while (Date.now() < deadline) {
    const gone = !(await ping(port)) && !pidAlive(pid)
    if (gone || (_proc && _proc.exitCode !== null)) { stopped = true; break }
    await new Promise(r => setTimeout(r, 150))
  }

  if (!stopped) {
    if (_proc && _proc.exitCode === null) _killGroup(_proc, 'SIGKILL')
    else _killTree(pid, 'SIGKILL')
    // Best-effort cleanup of files the dying process couldn't unlink.
    _unlinkStaleFiles()
  } else if (process.platform === 'win32') {
    // A Windows stop is always a hard kill, so python never ran its own unlink;
    // clear the lock/discovery so the next start() isn't blocked (POSIX cleans
    // these up in the daemon's SIGTERM handler).
    _unlinkStaleFiles()
  }
  _proc = null
}

// Stop the current daemon and bring a fresh one up on the same port, then wait
// until it is healthy so the UI reconnects to it. Flags itself as restarting so
// index.js's status poll won't auto-quit during the brief down window.
async function restart(workspacePath) {
  _restarting = true
  try {
    await stop()
    await start(workspacePath)
    // Generous timeout: a fresh `uv run` cold-starts the heavy langgraph stack,
    // which can take well over the default 10s before /health responds.
    return await waitUntilReady(getPort(), 45000)
  } finally {
    _restarting = false
  }
}

function ping(port) {
  return new Promise(resolve => {
    const req = http.get(`http://127.0.0.1:${port}/health`, res => {
      resolve(res.statusCode === 200)
    })
    req.on('error', () => resolve(false))
    req.setTimeout(1000, () => { req.destroy(); resolve(false) })
  })
}

async function waitUntilReady(port, maxMs = 10000) {
  const start = Date.now()
  while (Date.now() - start < maxMs) {
    if (await ping(port)) return true
    await new Promise(r => setTimeout(r, 400))
  }
  return false
}

module.exports = { start, stop, restart, isRestarting, isRunning, isAlive, isManaged, setManaged, getPort, onLog, waitUntilReady, ping }
