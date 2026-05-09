---
name: pdf-to-archive
description: |
  Move a newly downloaded PDF into ~/Archive/<year>/<month>/.
  Use when fs.changed event has ext=pdf and kind=created.
compatibility: Requires ~/Archive directory (created if missing)
---

## What to do

1. Use `fetch_event` to get the full event payload (path, size, name).
2. Use `tr_read_file` to verify the file exists and check its size.
3. Determine the destination: ~/Archive/{year}/{month}/{filename}
   where year/month come from today's date.
4. Use `tr_write_file` to create destination subdirs if missing
   (write a .keep placeholder, then delete it — or use tr_execute_in_sandbox
   to run `mkdir -p`).
5. Use `tr_execute_in_sandbox` to move: `mv <src> <dst>`.
6. Return a one-line summary: "Moved {name} → ~/Archive/{year}/{month}/".

## Tools used

fetch_event, tr_read_file, tr_execute_in_sandbox

## Gotchas

- If a file with the same name exists at destination, append _2, _3, etc.
- Do not move files still being written (size=0 or .part extension).
