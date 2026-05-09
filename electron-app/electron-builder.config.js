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
