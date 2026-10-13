module.exports = {
  appId: 'com.yuyutsava.terminal',
  productName: 'YUYUTSAVA Terminal',
  directories: {
    output: 'dist/app',
    buildResources: 'assets',
  },
  mac: {
    category: 'public.app-category.developer-tools',
    target: [{ target: 'dmg', arch: ['arm64', 'x64'] }],
    icon: 'assets/icon.icns',
    darkModeSupport: true,
  },
  win: {
    target: [
      { target: 'nsis', arch: ['x64', 'arm64'] },
      { target: 'portable', arch: ['x64'] },
    ],
    icon: 'assets/icon.ico',
  },
  nsis: {
    oneClick: false,
    perMachine: false,
    allowToChangeInstallationDirectory: true,
  },
  files: ['dist/renderer/**', 'src/main/**', 'assets/**', 'package.json'],
  extraResources: [
    {
      from: '../',
      to: 'backend',
      // Everything `scripts/install.py` needs to build the backend environment
      // on the user's machine at first run — the app ships source, not a venv.
      //   scripts/ + .python-version  the installer, and the interpreter it pins
      //   README.md, LICENSE, NOTICE  `uv sync` installs the project itself, and
      //                               hatchling reads `readme` and `license-files`
      //                               from pyproject.toml; without them it fails.
      filter: [
        'yuyutsava/**',
        'scripts/**',
        'pyproject.toml',
        'uv.lock',
        '.python-version',
        'README.md',
        'LICENSE',
        'NOTICE',
      ],
    },
  ],
}
