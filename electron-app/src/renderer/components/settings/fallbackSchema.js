// Minimal fallback so the form is usable before the daemon's first boot
// (the schema endpoint is unreachable then). Mirrors the daemon's Core group.
export const FALLBACK_SCHEMA = {
  groups: [
    {
      name: 'LLM Provider',
      vars: [
        {
          key: 'LLM_PROVIDER',
          label: 'Provider',
          type: 'select',
          default: 'groq',
          options: ['groq', 'openrouter', 'anthropic', 'ollama'],
          reload_class: 'restart_resume',
        },
        {
          key: 'GROQ_API_KEY',
          label: 'Groq API key',
          type: 'password',
          secret: true,
          placeholder: 'gsk_...',
          reload_class: 'restart_resume',
          depends_key: 'LLM_PROVIDER',
          depends_value: 'groq',
        },
        {
          key: 'GROQ_MODEL',
          label: 'Groq model',
          type: 'text',
          placeholder: 'llama-3.3-70b-versatile',
          reload_class: 'restart_resume',
          depends_key: 'LLM_PROVIDER',
          depends_value: 'groq',
        },
      ],
    },
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
