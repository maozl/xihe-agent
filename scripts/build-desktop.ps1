# 构建 xihe 桌面安装包（Windows）。
# 流程：① 运行 build-cli.ps1 打包 CLI → ② 内嵌到 desktop\resources\bin →
#       ③ electron-builder 出 NSIS 安装包 + portable 免安装版。
# 产物：desktop\release\xihe-agent Setup *.exe（安装版）、xihe-agent *.exe（便携版）。
#
# 用法：在 PowerShell 中  .\scripts\build-desktop.ps1
# 前置：Node.js 18+、Python 3.10+（均在 PATH）；网络可访问 npm / PyPI。
#       跳过 CLI：$env:XIHE_SKIP_CLI=1; .\scripts\build-desktop.ps1
param()

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

# ① CLI 打包并内嵌
if ($env:XIHE_SKIP_CLI -eq "1") {
  Write-Host "[1/3] 跳过 CLI 构建（XIHE_SKIP_CLI=1）"
} else {
  Write-Host "[1/3] 构建 CLI 并内嵌到桌面包"
  & (Join-Path $PSScriptRoot "build-cli.ps1")
  $BinDir = "desktop\resources\bin"
  New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
  Remove-Item -Recurse -Force (Join-Path $BinDir "xihe") -ErrorAction SilentlyContinue
  Copy-Item -Recurse "dist\cli\xihe" (Join-Path $BinDir "xihe")
}

# ② 构建桌面 bundle
Write-Host "[2/3] electron-vite 构建 renderer/main/preload"
Set-Location (Join-Path $Root "desktop")
if (-not (Test-Path "node_modules")) {
  npm install
  if ($LASTEXITCODE -ne 0) { throw "npm install 失败" }
}
npm run build
if ($LASTEXITCODE -ne 0) { throw "electron-vite build 失败" }

# ③ electron-builder 出包
Write-Host "[3/3] electron-builder 打包 (win)"
npx electron-builder --win
if ($LASTEXITCODE -ne 0) { throw "electron-builder 打包失败" }

Write-Host ""
Write-Host "完成。安装包在: $Root\desktop\release\"
Get-ChildItem "$Root\desktop\release" -File | Select-Object Name, @{N="Size(MB)"; E={[math]::Round($_.Length/1MB,1)}}
