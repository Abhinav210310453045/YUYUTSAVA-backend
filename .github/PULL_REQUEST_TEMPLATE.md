<!--
PR title must be a Conventional Commit — it becomes the squashed commit message.
  feat(voice): gate wake-word detection behind VAD
  fix(storage): stop dropping task_id on usage rows
-->

## What and why

<!-- What was wrong or missing, and what this changes. Link any issue. -->

## How it was verified

<!--
Say what you actually ran. "Untested on Windows" is more useful than silence.
-->

- [ ] `python -m compileall yuyutsava`
- [ ] `uv build --wheel`
- [ ] `cd electron-app && npm run build` (if the renderer changed)
- [ ] Ran the CLI or daemon against the change
- [ ] Other:

## Checklist

- [ ] One logical change. Mechanical changes (reformat, codemod) are a separate PR.
- [ ] No secrets, absolute home-directory paths, or real conversation data.
- [ ] Docs updated if behaviour or configuration changed.
- [ ] If a dependency ceiling moved, `scripts/verify_framework_contract.py` passes.
