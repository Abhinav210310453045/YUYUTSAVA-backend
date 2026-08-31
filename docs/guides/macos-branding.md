# macOS app branding (dock name + menu bar) — how to finish it at build time

## TL;DR

The macOS **dock tooltip** and the **menu-bar app name** only reliably read
"YUYUTSAVA" from a **packaged** build. During `npm run dev` they can still say
"Electron" — this is a documented Electron/macOS limitation, not a bug in our
code. Everything needed for the packaged build is already configured; two small
items remain to be done **when we package**. This doc is the checklist.

## Why dev shows "Electron"

`npm run dev` launches the generic prebuilt `node_modules/electron/dist/Electron.app`.
macOS LaunchServices caches the dock/display name keyed to that bundle, so the
name is sticky regardless of what we set at runtime. We already apply a
best-effort dev workaround in
[electron-app/scripts/launch-electron.js](../../electron-app/scripts/launch-electron.js):
it patches the bundle's `Info.plist` (`CFBundleName`, `CFBundleDisplayName`,
`CFBundleExecutable`, and `CFBundleIdentifier` → `com.yuyutsava.terminal`),
renames the binary, removes the stale `Electron` binary, then runs `lsregister`
+ `killall Dock` to flush the cache. This *can* work but is not guaranteed in
dev. **The packaging step is what makes the name stick.**

## What is already done (no action needed)

- `app.setName('YUYUTSAVA')` is called before `app.whenReady()` —
  [electron-app/src/main/index.js:2](../../electron-app/src/main/index.js#L2).
- Packager is fully configured —
  [electron-app/electron-builder.config.js](../../electron-app/electron-builder.config.js):
  - `appId: 'com.yuyutsava.terminal'`
  - `productName: 'YUYUTSAVA Terminal'`
  - `mac.icon: 'assets/icon.icns'`
- `package.json` has top-level `productName: "YUYUTSAVA"` and a `dist` script:
  `vite build && electron-builder`.

## What to do WHEN we package (the remaining steps)

### 1. Build the packaged app
```bash
cd electron-app
npm run dist        # → vite build && electron-builder, output in dist/app/
```
Launch the produced `dist/app/**/YUYUTSAVA Terminal.app`. The dock tooltip and
Finder name will read **"YUYUTSAVA Terminal"** (from `productName`). No further
plist hacking is needed for packaged builds — electron-builder writes the
Info.plist correctly.

> Decide the exact name: `productName` is currently `"YUYUTSAVA Terminal"` in
> electron-builder.config.js but `"YUYUTSAVA"` in package.json. Pick one and make
> them consistent so the dock/Finder/menu-bar all match.

### 2. Add a custom application menu (menu-bar label)
The bold app menu next to the Apple logo comes from the **application menu**, not
from `productName`. We have not added one yet (only a tray menu exists). When
packaging, add to the main process (e.g. in
[electron-app/src/main/index.js](../../electron-app/src/main/index.js), after the
window is created):

```js
const { Menu } = require('electron')

function buildAppMenu() {
  const isMac = process.platform === 'darwin'
  const template = [
    ...(isMac ? [{
      label: 'YUYUTSAVA',                 // first label = app menu title
      submenu: [
        { role: 'about' },
        { type: 'separator' },
        { role: 'hide' }, { role: 'hideOthers' }, { role: 'unhide' },
        { type: 'separator' },
        { role: 'quit' },
      ],
    }] : []),
    { role: 'editMenu' },
    { role: 'windowMenu' },
  ]
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}
```
Call `buildAppMenu()` from `onReady()`. (On macOS the first submenu's label is
shown as the app name in the menu bar; with a signed/packaged build it will
display "YUYUTSAVA".)

### 3. (Optional) Code signing / notarization
For distribution outside your own machine, add signing config to
electron-builder (`mac.identity`, notarization). Not required for a local demo.

## Cleanup once packaging is the norm

The dev-only Info.plist/binary patching in `launch-electron.js` exists purely to
make `npm run dev` look right. It can stay (harmless, idempotent, and only acts
on a changed bundle), but it is **not** the long-term branding mechanism —
electron-builder is. If it ever causes friction with electron upgrades
(it edits files inside `node_modules`), it's safe to delete; packaged builds are
unaffected.

## Quick reference — where things live

| Concern | File |
|---|---|
| Runtime app name | [electron-app/src/main/index.js](../../electron-app/src/main/index.js) (`app.setName`) |
| Packaged name / appId / icon | [electron-app/electron-builder.config.js](../../electron-app/electron-builder.config.js) |
| Dev-mode dock workaround | [electron-app/scripts/launch-electron.js](../../electron-app/scripts/launch-electron.js) |
| Build command | `npm run dist` (in `electron-app/`) |
