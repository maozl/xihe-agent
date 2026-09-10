#!/usr/bin/env bash
# 将 git 追踪的源码（含 .project-kbs，排除 .idea/缓存/构建产物）打包为加密 zip
# 用法: bash package-src.sh [输出路径]    密码可用 PASSWORD=xxx 覆盖
set -euo pipefail

PASSWORD="${PASSWORD:-311311}"
OUTPUT="${1:-/e/xihe-agent-src.zip}"
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

SEVEN_ZIP="/c/Program Files/7-Zip/7z.exe"
[ -f "$SEVEN_ZIP" ] || SEVEN_ZIP="$(command -v 7z)"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/xihe-agent"

( cd "$REPO_ROOT" && git ls-files -z -- \
  src tests desktop docs .project-kbs \
  README.md README.zh-CN.md pyproject.toml requirements.txt \
  LICENSE config.example.yaml CLAUDE.md AGENTS.md .gitignore \
  | while IFS= read -r -d '' f; do [ -f "$f" ] && printf '%s\0' "$f"; done \
  | tar --null -T - -cf - ) | tar -xf - -C "$tmp/xihe-agent"

rm -f "$OUTPUT"
"$SEVEN_ZIP" a -tzip -p"$PASSWORD" "$OUTPUT" "$tmp/xihe-agent" > /dev/null
"$SEVEN_ZIP" t -p"$PASSWORD" "$OUTPUT" > /dev/null

echo "完成: $OUTPUT (密码: $PASSWORD)"
