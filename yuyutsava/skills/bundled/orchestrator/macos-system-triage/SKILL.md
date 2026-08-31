---
name: macos-system-triage
description: Diagnose and repair common macOS problems — services (launchd), disk health, unified logs, and background daemons. Use when the user asks to fix or check their Mac.
platforms: [macos]
---
# macOS system triage

Native shell is bash (`tr_execute`). Explain state-changing steps before running; use
`tr_execute(elevated=True)` (admin auth prompt) only when a step needs root.

## Diagnose (read-only, no elevation)
- Recent errors: `log show --last 1h --predicate 'messageType == 16' --style compact | tail -n 50`
- Disk usage / health: `df -h` ; `diskutil info / | grep -i 'SMART\|Verified'`
- launchd service state: `launchctl print gui/$(id -u)/<label>` (user) or `launchctl list | grep <name>`
- Top memory/CPU: `ps aux | sort -nrk 4 | head -10`
- Free space by dir: `du -sh ~/Library/Caches/* 2>/dev/null | sort -rh | head`

## Repair (confirm with the user; elevated where noted)
- Restart a user agent: `launchctl kickstart -k gui/$(id -u)/<label>`
- Verify/repair the boot volume: `diskutil verifyVolume /` then `diskutil repairVolume /`
  (repair of the system volume may need elevation / recovery mode — explain this).
- Flush DNS (elevated): `dscacheutil -flushcache; killall -HUP mDNSResponder`
- Clear a user cache dir: remove a specific `~/Library/Caches/<app>` folder (never all of it blindly).

## Guardrails
- Never `rm -rf` a home/system root, never touch `/System`, `/usr/bin`, `/etc` — blocked.
- Prefer `launchctl kickstart` over killing processes; report what changed and how to verify.
