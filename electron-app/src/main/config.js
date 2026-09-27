/**
 * Addresses the main process uses, defined once (R-UI-07).
 *
 * The renderer does not import this: it asks for the daemon port through the
 * preload bridge (`electronAPI.getDaemonPort`), so a port set in the user's
 * settings reaches every window. `DEFAULT_DAEMON_PORT` must equal
 * `DEFAULT_DAEMON_PORT` in yuyutsava/config/network.py — a test holds them
 * together (test/config/test_default_daemon_port.py).
 */

const DAEMON_HOST = '127.0.0.1'
const DEFAULT_DAEMON_PORT = 7654

// The Vite dev server the unpacked app loads (see vite.config.js).
const DEV_SERVER_URL = 'http://localhost:5173'

function daemonUrl(port, path = '') {
  return `http://${DAEMON_HOST}:${port}${path}`
}

module.exports = { DAEMON_HOST, DEFAULT_DAEMON_PORT, DEV_SERVER_URL, daemonUrl }
