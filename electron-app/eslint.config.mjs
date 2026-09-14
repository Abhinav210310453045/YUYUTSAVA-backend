// Lint rules for the Electron app. The standard they enforce, and why:
// docs/code-review/v0.1.0/01-standards.md §14.
import js from '@eslint/js'
import react from 'eslint-plugin-react'
import reactHooks from 'eslint-plugin-react-hooks'
import globals from 'globals'

// Rules shared by every JavaScript file in the app.
const shared = {
  ...js.configs.recommended.rules,
  // A leading underscore marks a binding as deliberately unused, e.g. `catch (_)`.
  'no-unused-vars': [
    'error',
    { argsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_', varsIgnorePattern: '^_' },
  ],
  // Empty blocks are silent failures. Ratcheted as warnings (see `npm run lint`)
  // until each is given a reason or real handling (R-UI-01, W7).
  'no-empty': 'warn',
}

export default [
  {
    ignores: ['dist/**', 'release/**', 'node_modules/**', 'conversation_history/**', 'large_tool_results/**'],
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

  // Vite config: an ES module evaluated by Node.
  {
    files: ['vite.config.js'],
    languageOptions: { ecmaVersion: 'latest', sourceType: 'module', globals: globals.node },
    rules: shared,
  },
]
