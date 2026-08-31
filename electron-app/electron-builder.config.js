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
  files: [
    'dist/renderer/**',
    'src/main/**',
    'assets/**',
    'package.json',
  ],
  extraResources: [
    {
      from: '../',
      to: 'backend',
      filter: ['yuyutsava/**', 'pyproject.toml', 'uv.lock'],
    },
  ],
}
