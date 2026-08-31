---
name: file-download
description: |
  Handle a newly downloaded file in ~/Downloads. Route to the appropriate
  subagent based on file type. Use when fs.changed event has kind=created
  and the path is inside ~/Downloads.
compatibility: Requires ~/Downloads directory to exist
---

## Pattern

When a file appears in ~/Downloads:
1. Check the file extension to determine type (pdf, zip, img, doc, etc.).
2. Dispatch to file-organizer with instruction to move/archive the file.
3. For zip archives, consider whether to ask the user before extracting.

## Subagent

Use `file-organizer` for all file-download events.

## Instruction template

"Organize the newly downloaded file at {path}. Move it to the appropriate
archive location based on its type and date."

## Gotchas

- Partial downloads have extensions like .crdownload or .part — these are
  filtered by FsSource but double-check with tr_read_file before acting.
- Very large files (>500 MB) should prompt the user before moving.
