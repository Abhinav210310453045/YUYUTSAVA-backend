/**
 * Launch Electron with the app rebranded as YUYUTSAVA in dev mode.
 *
 * macOS shows the dock tooltip from the .app bundle's CFBundleName +
 * the executable filename. To make the dock tooltip read "YUYUTSAVA"
 * instead of "Electron", we:
 *   1. Patch Info.plist (CFBundleName, CFBundleDisplayName, CFBundleExecutable,
 *      and a unique CFBundleIdentifier — the id is what actually busts the
 *      LaunchServices name cache)
 *   2. Provide a binary named "YUYUTSAVA" to spawn (so the process name matches)
 *
 * IMPORTANT — why this is non-destructive:
 *   `node_modules/electron/path.txt` names the canonical "Electron" binary, and
 *   `require('electron')` re-DOWNLOADS the whole runtime if that file is missing
 *   (see electron/index.js → downloadElectron). An earlier version of this
 *   script *renamed/deleted* that binary, so every other `npm run dev` found it
 *   gone, kicked off a slow re-download, and the window failed to open — the
 *   "UI starts only every other time" bug. We therefore:
 *     - read path.txt directly instead of calling require('electron') (so we
 *       never trigger the auto-download), and
 *     - COPY the binary to "YUYUTSAVA" and keep "Electron" intact (so path.txt
 *       always resolves). Two launchers in the bundle is harmless.
 *
 * The patches are idempotent — re-running this script is a no-op once applied.
 * A node_modules reinstall resets everything; the next run re-applies it.
 */
const { spawn, execFileSync } = require('child_process')
const fs = require('fs')
const path = require('path')

const APP_NAME = 'YUYUTSAVA'
// A unique bundle id is what actually fixes the dock tooltip: LaunchServices
// caches the display name keyed by CFBundleIdentifier, so while it stays
// "com.github.Electron" the dock keeps serving "Electron" no matter what
// CFBundleName says. A fresh id has no cached name → it reads CFBundleName.
const BUNDLE_ID = 'com.yuyutsava.terminal'

// Resolve the install WITHOUT require('electron') — that executes the package's
// index.js, which re-downloads the binary when path.txt's target is missing.
const electronModuleDir = path.resolve(__dirname, '..', 'node_modules', 'electron')
const pathTxt = path.join(electronModuleDir, 'path.txt')

// path.txt only exists once electron's postinstall has downloaded the runtime.
// It can be missing entirely (postinstall skipped via --ignore-scripts, an
// interrupted install, ELECTRON_SKIP_BINARY_DOWNLOAD), which used to crash this
// script with ENOENT before Vite even had a client. Run the package's own
// installer once to fetch the dist — that's what postinstall would have done.
if (!fs.existsSync(pathTxt) || !fs.existsSync(path.join(electronModuleDir, 'dist'))) {
  console.log('[launch-electron] Electron runtime missing — downloading it once...')
  execFileSync(process.execPath, [path.join(electronModuleDir, 'install.js')], {
    cwd: electronModuleDir,
    stdio: 'inherit',
  })
}

// canonicalExe = dist/electron.exe on Windows,
//                dist/Electron.app/Contents/MacOS/Electron on macOS
const canonicalExe = path.join(electronModuleDir, 'dist', fs.readFileSync(pathTxt, 'utf8').trim())
if (!fs.existsSync(canonicalExe)) {
  console.error(`[launch-electron] Electron binary not found at ${canonicalExe}`)
  console.error('[launch-electron] Try: rm -rf node_modules/electron && npm install electron')
  process.exit(1)
}

const macosDir = path.dirname(canonicalExe)
const contentsDir = path.dirname(macosDir)
const appBundleDir = path.dirname(contentsDir)
const plistPath = path.join(contentsDir, 'Info.plist')

let binaryPath = canonicalExe

if (process.platform === 'darwin' && fs.existsSync(plistPath)) {
  let changed = false

  // 1. Patch Info.plist (name keys + a unique bundle id)
  let plist = fs.readFileSync(plistPath, 'utf8')
  const before = plist
  plist = plist
    .replace(/(<key>CFBundleDisplayName<\/key>\s*<string>)[^<]*(<\/string>)/, `$1${APP_NAME}$2`)
    .replace(/(<key>CFBundleName<\/key>\s*<string>)[^<]*(<\/string>)/, `$1${APP_NAME}$2`)
    .replace(/(<key>CFBundleExecutable<\/key>\s*<string>)[^<]*(<\/string>)/, `$1${APP_NAME}$2`)
    .replace(/(<key>CFBundleIdentifier<\/key>\s*<string>)[^<]*(<\/string>)/, `$1${BUNDLE_ID}$2`)
  if (plist !== before) {
    fs.writeFileSync(plistPath, plist, 'utf8')
    changed = true
  }

  // 2. Ensure a binary named "YUYUTSAVA" exists to spawn (its filename becomes
  //    the process/dock name, matching CFBundleExecutable above). Crucially we
  //    COPY and never delete the canonical "Electron" binary, so path.txt keeps
  //    resolving and require('electron') never re-downloads.
  const brandedExe = path.join(macosDir, APP_NAME)
  if (canonicalExe !== brandedExe) {
    if (fs.existsSync(canonicalExe) && !fs.existsSync(brandedExe)) {
      fs.copyFileSync(canonicalExe, brandedExe)
      fs.chmodSync(brandedExe, 0o755)
      changed = true
    } else if (!fs.existsSync(canonicalExe) && fs.existsSync(brandedExe)) {
      // A prior version of this script deleted the canonical binary. Restore it
      // from the branded copy so path.txt resolves again (no re-download).
      fs.copyFileSync(brandedExe, canonicalExe)
      fs.chmodSync(canonicalExe, 0o755)
      changed = true
    }
    if (fs.existsSync(brandedExe)) binaryPath = brandedExe
  }

  // 3. Only when we actually changed the bundle: reset LaunchServices so macOS
  //    re-reads the metadata, then restart the Dock so it drops the cached
  //    "Electron" tooltip. Gated so we don't restart the user's Dock every run.
  if (changed) {
    try {
      execFileSync(
        '/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister',
        ['-f', appBundleDir],
        { stdio: 'ignore' }
      )
    } catch (_) {}
    try {
      execFileSync('killall', ['Dock'], { stdio: 'ignore' })
    } catch (_) {}
  }
}

// Strip ELECTRON_RUN_AS_NODE before launching the GUI process. When set (e.g.
// leaked into the session by another Electron app, or via `launchctl setenv`),
// it forces this binary to boot as a plain Node process — `require('electron')`
// then returns no API, so `app` is undefined and index.js dies on app.setName.
// That is the "daemon starts but the window never opens" failure; clearing it
// here makes the launch immune to a polluted environment.
const childEnv = { ...process.env, NODE_ENV: process.env.NODE_ENV || 'development' }
delete childEnv.ELECTRON_RUN_AS_NODE

const proc = spawn(binaryPath, ['.'], {
  stdio: 'inherit',
  env: childEnv,
})

proc.on('close', code => process.exit(code ?? 0))
