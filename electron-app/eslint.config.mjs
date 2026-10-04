// Lint rules for the Electron app. The standard they enforce, and why:
// docs/code-review/v0.1.0/01-standards.md §14.
import js from '@eslint/js'
import react from 'eslint-plugin-react'
import reactHooks from 'eslint-plugin-react-hooks'
import globals from 'globals'
import { readFileSync } from 'node:fs'

// Files over the size limit today, exempted one by one. The list only shrinks:
// scripts/check-max-lines-allowlist.js fails when a listed file no longer needs
// its exemption (R-UI-03).
const maxLines = JSON.parse(readFileSync(new URL('./max-lines-allowlist.json', import.meta.url)))

// Rules shared by every JavaScript file in the app.
const shared = {
  ...js.configs.recommended.rules,
  // A leading underscore marks a binding as deliberately unused, e.g. `catch (_)`.
  'no-unused-vars': [
    'error',
    { argsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_', varsIgnorePattern: '^_' },
  ],
  // An empty block is a silent failure: handle it, or say in a comment why
  // silence is right (R-ERR).
  'no-empty': 'error',
  // A file does one thing: over the limit, split it by responsibility.
  'max-lines': ['error', { max: maxLines.max }],
}

export default [
  {
    ignores: [
      'dist/**',
      'release/**',
      'node_modules/**',
      'conversation_history/**',
      'large_tool_results/**',
    ],
  },

  // Renderer: ES modules, JSX, running in the browser context.
  {
    files: ['src/renderer/**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: globals.browser,
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    plugins: { react, 'react-hooks': reactHooks },
    settings: { react: { version: 'detect' } },
    rules: {
      ...shared,
      ...react.configs.recommended.rules,
      // The React 17+ JSX transform needs no React import in scope.
      'react/react-in-jsx-scope': 'off',
      // The app does not use PropTypes; behaviour is pinned by tests instead.
      'react/prop-types': 'off',
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
    },
  },

  // Main process, preload, launch script and build config: CommonJS under Node.
  {
    files: ['src/main/**/*.js', 'scripts/**/*.js', 'electron-builder.config.js'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'commonjs',
      globals: { ...globals.node, ...globals.browser },
    },
    rules: shared,
  },

  // Main-process tests run under vitest, which is ES modules only; the code they
  // test stays CommonJS (they load it with createRequire).
  {
    files: ['src/main/**/*.test.js'],
    languageOptions: { ecmaVersion: 'latest', sourceType: 'module', globals: globals.node },
  },

  // Tests may run long; the size limit is for the code under test.
  { files: ['**/*.test.{js,jsx}'], rules: { 'max-lines': 'off' } },
  { files: maxLines.files, rules: { 'max-lines': 'off' } },

  // Vite config: an ES module evaluated by Node.
  {
    files: ['vite.config.js'],
    languageOptions: { ecmaVersion: 'latest', sourceType: 'module', globals: globals.node },
    rules: shared,
  },
]
