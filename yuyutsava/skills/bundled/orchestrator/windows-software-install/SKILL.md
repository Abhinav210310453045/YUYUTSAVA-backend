---
name: windows-software-install
description: Install, update, or remove software on Windows via winget or an MSI/EXE installer. Use when the user asks to install or update an app on their Windows machine.
platforms: [windows]
---
# Windows software install

Native shell is PowerShell (`tr_execute`). Prefer a package manager over a manual download.
Check `tr_sysinfo` `package_managers` — if `winget` is absent, fall back to an MSI.

## winget (preferred)
- Search: `winget search "<name>"`
- Install (no prompts): `winget install --id <Publisher.App> -e --accept-package-agreements --accept-source-agreements`
- Upgrade one / all: `winget upgrade --id <Publisher.App>` · `winget upgrade --all`
- Uninstall: `winget uninstall --id <Publisher.App>`
winget installs to the machine → these need `tr_execute(elevated=True)` for machine scope
(or add `--scope user` to avoid elevation when the app supports it).

## MSI / EXE (no winget, or vendor-only)
- Download with `tr_fetch_url` to the workspace first (verifies it's a real file).
- Silent MSI install (elevated): `msiexec /i "<path\to.msi>" /qn /norestart`
- Silent MSI uninstall (elevated): `msiexec /x "<path\to.msi>" /qn`
- EXE installers vary — common silent flags: `/S`, `/silent`, `/quiet`. Confirm the flag
  from the vendor before assuming.

## Guardrails
- Always show the exact package id / installer path and that it's elevated before running.
- Never pipe a downloaded script straight into PowerShell (`iwr ... | iex`) — download with
  `tr_fetch_url`, let the user see it, then run.
