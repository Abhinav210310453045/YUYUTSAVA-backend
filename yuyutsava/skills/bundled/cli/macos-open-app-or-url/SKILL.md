---
name: macos-open-app-or-url
description: |
  Opening a macOS app, or a URL in a specific Chrome/browser profile, and
  actually landing on the page. Use for "open YouTube in my work profile",
  "play this video", "open WhatsApp/Kaggle/any app", or any request to put
  something on the user's screen.
platforms: [macos]
---

## Pattern

1. **App, no URL** → `open -a "WhatsApp"`. Done.
2. **URL in a specific Chrome profile** → launch the binary directly, NOT
   `open`:
   `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --profile-directory="Profile 1" "https://…"`
   Resolve the profile directory first — see the `chrome-profile-by-email`
   skill.
3. **Act on the page, not just show it** → build the URL that *is* the
   action. Search: `https://www.youtube.com/results?search_query=<q>`.
   Play a specific video: `https://www.youtube.com/watch?v=<id>` (a channel
   or /videos page only *shows* results — it plays nothing). Log in:
   go to the site's login URL directly.
4. Report what you opened, with the URL, so the user can confirm it is the
   right thing.

## Gotchas

- `open -a "Google Chrome" --args --profile-directory=…` silently ignores
  the args when Chrome is ALREADY RUNNING — it just focuses the existing
  window. This is the single most common failure: the command "succeeds"
  (exit 0) and nothing happens. Launch the binary path instead.
- "Opening in existing browser session." on stdout means it worked.
- These are EXTERNAL-zone commands and will prompt for approval. Send one
  command that does the whole job rather than a chain of approvals.
- When the user says play / click / log in, they mean it. Navigating to a
  page that merely contains the thing is not the task.
