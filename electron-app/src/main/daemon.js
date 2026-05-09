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

  _proc = spawn('uv', ['run', 'yuyutsava', 'daemon', '--no-browser', '--workspace', workspacePath || process.cwd()], {
    env,
    cwd: workspacePath || process.cwd(),
    stdio: ['ignore', 'pipe', 'pipe'],
  })

  _proc.stdout.on('data', d => _log(d.toString()))
  _proc.stderr.on('data', d => _log(d.toString()))
  _proc.on('exit', (code) => {
    _log(`[daemon] exited with code ${code}`)
    _proc = null
  })
}

async function stop() {
  if (!_proc) return
  _proc.kill('SIGTERM')
  await new Promise(resolve => {
    const t = setTimeout(() => {
      if (_proc) _proc.kill('SIGKILL')
      resolve()
    }, 3000)
    const check = setInterval(() => {
      if (!_proc || _proc.exitCode !== null) {
        clearTimeout(t)
        clearInterval(check)
        resolve()
      }
    }, 100)
  })
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
