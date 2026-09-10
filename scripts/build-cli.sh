#!/usr/bin/env bash
# 打包 xihe CLI 为独立可执行文件（PyInstaller onedir 模式）。
# 产物：dist/cli/xihe   （Windows/Git-Bash 下为 dist/cli/xihe.exe）
#
# 用法：bash scripts/build-cli.sh
# 前置：网络可访问 PyPI；产物不依赖目标机预装 Python。
#
# 打包边界：
#   * 包含全部核心功能与内置 skills/agents/kbs 模板；
#   * 排除 paddleocr / paddlepaddle（可选离线 OCR，懒加载 + 模型文件数 GB，
#     需要时以 pip 单独安装即可，不影响基础包）；playwright 驱动随包打包，
#     浏览器二进制需在目标机执行 `playwright install chromium` 安装。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
VENV="build/cli-venv"

# 运行时核心依赖（与 requirements.txt 对应，去掉可选/测试/平台专属项；
# paddleocr/paddlepaddle 因体积与懒加载特性不进基础包）
CORE_DEPS=(
  "openai>=1.40.0"
  "aiohttp>=3.10.0"
  "httpx>=0.27.0"
  "pyyaml>=6.0"
  "mcp>=1.2.0,<2"
  "playwright>=1.40"
  "paramiko>=3.0"
  "prompt_toolkit>=3.0"
  "RestrictedPython>=8.0"
  "textual>=0.8"
  "pyautogui>=0.9.54"
  "psutil>=5.9"
  "pyinstaller"
)

echo "[1/4] 创建构建环境: $VENV"
"$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q "${CORE_DEPS[@]}"
"$VENV/bin/pip" install -q -e . --no-deps

echo "[2/4] PyInstaller 打包 xihe"
rm -rf dist/cli build/pyinstaller build/xihe.spec
"$VENV/bin/pyinstaller" --noconfirm --clean \
  --name xihe \
  --onedir \
  --distpath dist/cli \
  --workpath build/pyinstaller \
  --specpath build \
  --exclude-module paddleocr \
  --exclude-module paddlepaddle \
  --exclude-module paddlex \
  --exclude-module modelscope \
  --exclude-module pytest \
  --hidden-import RestrictedPython \
  --collect-all playwright \
  --collect-submodules tools \
  --add-data "$ROOT/src/core/kbs_templates:core/kbs_templates" \
  --add-data "$ROOT/src/agents:agents" \
  --add-data "$ROOT/src/skills:skills" \
  --add-data "$ROOT/src/core/kbs_protocol.md:core/kbs_protocol.md" \
  --add-data "$ROOT/src/tools/web_record_recorder.js:tools/web_record_recorder.js" \
  "$ROOT/src/app/__main__.py"

echo "[3/4] 冒烟验证"
BIN="$ROOT/dist/cli/xihe/xihe"
if [ ! -x "$BIN" ]; then
  echo "错误: 打包产物不存在: $BIN" >&2
  exit 1
fi
if ! "$BIN" --version >/dev/null 2>&1; then
  # --version 不存在时退化为 --help；两者都失败才报错
  if ! "$BIN" --help >/dev/null 2>&1; then
    echo "错误: 打包产物无法执行" >&2
    exit 1
  fi
fi

echo "[4/4] 完成: $ROOT/dist/cli/"
du -sh "$ROOT/dist/cli/xihe"
