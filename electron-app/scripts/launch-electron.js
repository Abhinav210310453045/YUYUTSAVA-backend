/**
 * Launch Electron with the app rebranded as YUYUTSAVA in dev mode.
 *
 * macOS shows the dock tooltip from the .app bundle's CFBundleName +
 * the executable filename. To make the dock tooltip read "YUYUTSAVA"
 * instead of "Electron", we:
 *   1. Patch Info.plist (CFBundleName, CFBundleDisplayName, CFBundleExecutable)
 *   2. Rename the binary file from "Electron" to "YUYUTSAVA"
 *   3. Reset the macOS LaunchServices cache so the new name is picked up
 *
 * The patches are idempotent — re-running this script is a no-op if
 * already applied. Reinstalling node_modules resets everything, but the
 * next `npm run dev` re-applies the patches automatically.
 */
const { spawn, execFileSync } = require('child_process')
const fs = require('fs')
const path = require('path')
const electronPath = require('electron')

const APP_NAME = 'YUYUTSAVA'

// electronPath = .../Electron.app/Contents/MacOS/Electron
const macosDir = path.dirname(electronPath)
const contentsDir = path.dirname(macosDir)
const appBundleDir = path.dirname(contentsDir)
const plistPath = path.join(contentsDir, 'Info.plist')

let binaryPath = electronPath

if (process.platform === 'darwin' && fs.existsSync(plistPath)) {
  // 1. Patch Info.plist
  let plist = fs.readFileSync(plistPath, 'utf8')
  const before = plist
  plist = plist
    .replace(/(<key>CFBundleDisplayName<\/key>\s*<string>)[^<]*(<\/string>)/, `$1${APP_NAME}$2`)
    .replace(/(<key>CFBundleName<\/key>\s*<string>)[^<]*(<\/string>)/, `$1${APP_NAME}$2`)
    .replace(/(<key>CFBundleExecutable<\/key>\s*<string>)[^<]*(<\/string>)/, `$1${APP_NAME}$2`)
  if (plist !== before) {
    fs.writeFileSync(plistPath, plist, 'utf8')
  }

  // 2. Rename the binary so the process name matches CFBundleExecutable
  const renamedBinary = path.join(macosDir, APP_NAME)
  if (!fs.existsSync(renamedBinary) && fs.existsSync(electronPath)) {
    fs.renameSync(electronPath, renamedBinary)
  }
  if (fs.existsSync(renamedBinary)) {
    binaryPath = renamedBinary
  }

  // 3. Reset LaunchServices so macOS re-reads the bundle metadata
  try {
    execFileSync(
      '/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister',
      ['-f', appBundleDir],
      { stdio: 'ignore' }
    )
  } catch (_) {}
}

const proc = spawn(binaryPath, ['.'], {
  stdio: 'inherit',
  env: { ...process.env, NODE_ENV: process.env.NODE_ENV || 'development' },
})

proc.on('close', code => process.exit(code ?? 0))
