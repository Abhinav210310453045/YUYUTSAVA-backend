---
name: macos-app-local-data
description: |
  Finding and safely reading a macOS app's own local data — SQLite stores,
  plists, JSON state — for Messages, Mail, Notes, Safari, Chrome, Slack,
  Spotify and friends. Use when the user asks for content that lives inside
  an installed app rather than on the web.
platforms: [macos]
---

## Pattern

1. **Locate.** Apps keep data in one of three places:
   - `~/Library/Group Containers/<group.id>/` (sandboxed, shared between
     the app and its extensions — WhatsApp, Messages helpers)
   - `~/Library/Containers/<bundle.id>/Data/Library/Application Support/`
   - `~/Library/Application Support/<App>/` (unsandboxed — Chrome, Slack)
   Find it with a bounded search, never a full-disk walk:
   `find ~/Library -maxdepth 4 -iname '*<app>*' -maxdepth 4` or a glob in
   tr_run_python. Stop at the first hit.
2. **Copy before you read.** `shutil.copy2` the store into the SANDBOX
   along with any `-wal`/`-shm`/`.lock` siblings, then open the copy. The
   app has it open; reading the live file risks stale pages, and any write
   risks the user's data.
3. **Inspect, don't guess.** `SELECT name FROM sqlite_master WHERE
   type='table'`, then `PRAGMA table_info(<table>)`. Core Data schemas
   prefix everything with `Z` (`ZWAMESSAGE`, `Z_PK`) and store dates as
   seconds since 2001-01-01 (`unix = value + 978307200`). Plists:
   `plistlib.load` handles binary plists directly.
4. Query, convert dates, and present in the reply.

## Gotchas

- Some directories need Full Disk Access for the terminal (Mail, Messages,
  Safari history). A `PermissionError`/`unable to open database file` means
  that, not a wrong path: tell the user to grant it in System Settings →
  Privacy & Security → Full Disk Access, rather than retrying.
- Never `sudo` your way in, and never open the live DB read-write.
- A WAL-mode database read without its `-wal` file silently omits the most
  recent messages — the newest rows are exactly what was asked for.
- This is the user's private data. Keep it in the reply; do not write it to
  outputs/ or an artifact unless the user asked for a file.
- If a dedicated skill exists for the app (e.g. `whatsapp-mac-chat-db`),
  use that instead — it already has the schema.
