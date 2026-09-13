---
name: chrome-profile-by-email
description: |
  Finding which Chrome profile directory belongs to a given Google account
  or email address. Use whenever the user says "in my <email> profile", "my
  work/personal profile", or asks to open something as a specific account.
platforms: [macos]
---

## Pattern

Chrome's profile map lives in one JSON file:
`~/Library/Application Support/Google/Chrome/Local State`

Read `profile.info_cache` — keys are the on-disk directory names
(`Default`, `Profile 1`, `Profile 2`…) and each value carries
`user_name` (the account email), `gaia_name` and `name` (the display
label). Match the email case-insensitively against `user_name`, then pass
the KEY as `--profile-directory`.

Do it with tr_run_python (a few lines of json + pathlib), not a shell
one-liner: the path has spaces and the file is large.

Then launch with the `macos-open-app-or-url` skill.

## Gotchas

- `--profile-directory` takes the DIRECTORY name (`Profile 1`), never the
  display name ("Work") and never the email.
- `Default` is a real profile directory and is usually the first account
  ever signed in; do not assume `Profile 1` is the default.
- A profile that was never signed in has an empty `user_name` — match on
  `name`/`gaia_name` only as a fallback, and say which one you picked.
- Reading `Local State` needs no elevation, but it IS outside the
  workspace, so use a python read rather than a shell `cat` to keep it one
  approval.
- Chrome does not have to be closed to read this file.
