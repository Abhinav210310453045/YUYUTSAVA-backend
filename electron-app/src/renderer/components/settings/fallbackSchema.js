// Minimal fallback so the form is usable before the daemon's first boot
// (the schema endpoint is unreachable then). Mirrors the daemon's Core group.
// Models are not here: they are set up in Settings → Models (models.toml).
export const FALLBACK_SCHEMA = {
  groups: [
    {
      name: 'Daemon',
      vars: [
        {
          key: 'YUYUTSAVA_DAEMON_PORT',
          label: 'Port',
          type: 'number',
          default: '7654',
          placeholder: '7654',
          reload_class: 'restart_no_resume',
        },
      ],
    },
  ],
}
