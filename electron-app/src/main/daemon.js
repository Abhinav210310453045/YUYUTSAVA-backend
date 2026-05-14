const { spawn } = require('child_process')
const http = require('http')
const { readSettings } = require('./settings')

let _proc = null
let _logCallback = null
let _managed = true  // default: app manages the daemon

function getPort() {
  const s = readSettings()
  return parseInt(s['YUYUTSAVA_DAEMON_PORT'] || '7654', 10)
}

function isManaged() { return _managed }
function setManaged(v) { _managed = v }

function isRunning() {
  return _proc !== null && _proc.exitCode === null
}

function onLog(cb) { _logCallback = cb }

function _log(line) {
  if (_logCallback) _logCallback(line)
}

function start(workspacePath) {
  if (isRunning()) return

  const settings = readSettings()
  const env = { ...process.env, ...Object.fromEntries(Object.entries(settings)) }

  // detached:true puts the child in its own process group on POSIX so we can
  // signal `uv` AND its python grandchild together via `kill(-pid, ...)`.
  // Without this, SIGTERM reaches `uv` but not the python process it spawns.
  const isPosix = process.platform !== 'win32'
  _proc = spawn('uv', ['run', 'yuyutsava', 'daemon', '--no-ui', '--workspace', workspacePath || process.cwd()], {
    env,
    cwd: workspacePath || process.cwd(),
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: isPosix,
  })

  _proc.stdout.on('data', d => _log(d.toString()))
  _proc.stderr.on('data', d => _log(d.toString()))
  _proc.on('exit', (code) => {
    _log(`[daemon] exited with code ${code}`)
    _proc = null
  })
}

function _killGroup(proc, signal) {
  if (!proc || proc.exitCode !== null) return
  const isPosix = process.platform !== 'win32'
  try {
    if (isPosix) {
      // Negative PID → entire process group (uv + python grandchild).
      process.kill(-proc.pid, signal)
    } else {
      proc.kill(signal)
    }
  } catch (_) {
    // Process group might already be gone — fall back to direct child kill.
    try { proc.kill(signal) } catch (_) {}
  }
}

async function stop() {
  if (!_proc) return
  _killGroup(_proc, 'SIGTERM')
  await new Promise(resolve => {
    const t = setTimeout(() => {
      _killGroup(_proc, 'SIGKILL')
      resolve()
    }, 5000)
    const check = setInterval(() => {
      if (!_proc || _proc.exitCode !== null) {
        clearTimeout(t)
        clearInterval(check)
        resolve()
      }
    }, 100)
  })
  // Belt-and-suspenders: poll /health until it stops responding so callers
  // (e.g. restart) don't race the OS releasing the port.
  const port = getPort()
  const deadline = Date.now() + 3000
  while (Date.now() < deadline) {
    if (!(await ping(port))) break
    await new Promise(r => setTimeout(r, 150))
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

module.exports = { start, stop, isRunning, isManaged, setManaged, getPort, onLog, waitUntilReady, ping }
