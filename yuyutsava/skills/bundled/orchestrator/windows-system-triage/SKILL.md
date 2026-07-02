---
name: windows-system-triage
description: Diagnose and repair common Windows problems — services, disk health, event-log errors, and system-file corruption. Use when the user asks to fix or check their Windows machine.
platforms: [windows]
---
# Windows system triage

Native shell is PowerShell (via `tr_execute`). Call `tr_sysinfo` first if unsure of the version.
Explain what you'll run and why BEFORE running anything that changes state; run repairs
with `tr_execute(elevated=True)` (triggers the UAC prompt).

## Diagnose (read-only, no elevation)
- Recent errors: `Get-WinEvent -LogName System -MaxEvents 50 | Where-Object LevelDisplayName -eq 'Error' | Format-Table TimeCreated, Id, ProviderName, Message -Auto`
- Service state: `Get-Service <name>` (e.g. `spooler`, `wuauserv`, `bits`)
- Disk space/health: `Get-Volume` ; `Get-PhysicalDisk | Select FriendlyName, HealthStatus, OperationalStatus`
- Failed drivers/devices: `Get-PnpDevice -Status Error`
- Top memory hogs: `Get-Process | Sort-Object WS -Descending | Select -First 10 Name, Id, @{n='MB';e={[int]($_.WS/1MB)}}`

## Repair (elevated — always confirm with the user first)
- Restart a stuck service: `Restart-Service <name>` (or `Stop-Service`/`Start-Service`)
- System file repair: `sfc /scannow`
- Windows image repair: `DISM /Online /Cleanup-Image /RestoreHealth`
- Disk scan (schedules at reboot if in use): `Repair-Volume -DriveLetter C -Scan`
- Reset Windows Update: stop `wuauserv`,`bits`, rename `C:\Windows\SoftwareDistribution`, restart them.

## Guardrails
- Never `format`, `diskpart clean`, `bcdedit`, `vssadmin delete shadows`, or delete under
  `C:\Windows` — these are blocked and are not "fixes".
- Prefer the least-destructive step; report what changed and how to verify it.
