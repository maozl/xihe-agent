# 打包 xihe CLI 为 Windows 独立可执行文件（PyInstaller onedir 模式）。
# 产物：dist\cli\xihe\xihe.exe（onedir 目录）
#
# 用法：在 PowerShell 中  .\scripts\build-cli.ps1
# 前置：已安装 Python 3.10+（在 PATH 中）；网络可访问 PyPI。
#
# 打包边界与 build-cli.sh 一致：排除 paddleocr/paddlepaddle（可选 OCR），
# playwright 驱动随包，浏览器二进制需 `playwright install chromium` 安装。
param()

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$Venv = "build\cli-venv"
$CoreDeps = @(
  "openai>=1.40.0",
  "aiohttp>=3.10.0",
  "httpx>=0.27.0",
  "pyyaml>=6.0",
  "mcp>=1.2.0,<2",
  "playwright>=1.40",
  "paramiko>=3.0",
  "prompt_toolkit>=3.0",
  "RestrictedPython>=8.0",
  "textual>=0.8",
  "pyautogui>=0.9.54",
  "psutil>=5.9",
  "pywin32>=306",
  "pywinauto>=0.6.8",
  "pyinstaller"
)

Write-Host "[1/4] 创建构建环境: $Venv"
python -m venv $Venv
& "$Venv\Scripts\python.exe" -m pip install -q --upgrade pip
& "$Venv\Scripts\python.exe" -m pip install -q @CoreDeps
& "$Venv\Scripts\python.exe" -m pip install -q -e . --no-deps

Write-Host "[2/4] PyInstaller 打包 xihe"
Remove-Item -Recurse -Force dist\cli, build\pyinstaller, build\xihe.spec -ErrorAction SilentlyContinue
& "$Venv\Scripts\pyinstaller.exe" --noconfirm --clean `
  --name xihe `
  --onedir `
  --distpath dist\cli `
  --workpath build\pyinstaller `
  --specpath build `
  --exclude-module paddleocr `
  --exclude-module paddlepaddle `
  --exclude-module paddlex `
  --exclude-module modelscope `
  --exclude-module pytest `
  --hidden-import RestrictedPython `
  --collect-all playwright `
  --collect-submodules tools `
  --add-data "$Root\src\core\kbs_templates;core\kbs_templates" `
  --add-data "$Root\src\agents;agents" `
  --add-data "$Root\src\skills;skills" `
  --add-data "$Root\src\core\kbs_protocol.md;core\kbs_protocol.md" `
  --add-data "$Root\src\tools\web_record_recorder.js;tools\web_record_recorder.js" `
  "$Root\src\app\__main__.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败 (exit=$LASTEXITCODE)" }

Write-Host "[3/4] 冒烟验证"
$Bin = Join-Path $Root "dist\cli\xihe\xihe.exe"
if (-not (Test-Path $Bin)) { throw "打包产物不存在: $Bin" }
& $Bin --help *> $null
if ($LASTEXITCODE -ne 0) { throw "打包产物无法执行" }

Write-Host "[4/4] 完成: $Root\dist\cli\xihe\xihe.exe"
