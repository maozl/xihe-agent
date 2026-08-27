---
type: concept
title: xihe+desktop 打包发行策略（Windows / macOS / iOS）
slug: 0030_packaging-distribution-strategy
aliases:
  - 桌面端打包
  - xihe 安装包
  - xihe 发行
  - packaging
tags:
  - desktop
  - packaging
  - distribution
  - electron-builder
  - pyinstaller
status: active
created: 2026-08-13
updated: 2026-08-13
related_pages:
  - wiki/concepts/0024_desktop-serve-protocol.md
  - wiki/concepts/0029_desktop-dual-engine-architecture.md
  - wiki/insights/0026_desktop-agent-model-built-in-xihe.md
  - wiki/concepts/0023_multi-instance-config.md
  - wiki/entities/0001_xihe-agent.md
---

# xihe+desktop 打包发行策略（Windows / macOS / iOS）

> **状态：设计参考，尚未实现。** 本页把 2026-08-13 关于「把 xihe+desktop 打成可运行软件」的讨论沉淀成参考，供后续真正做打包时取用。所有技术约束（Electron spawn python 子进程、paddle 重原生、气隙无 Apple 联网、iOS 不支持 Electron/Python）来自现行架构与环境，属稳定事实；具体工具链选型（PyInstaller vs 嵌入式 python、签名方式）落地时再定。

## 摘要

桌面端已从独立仓迁入 `xihe-agent/desktop/`（electron-vite 构建，目前**只有 `build` 出 `out/`，尚未接 electron-builder、无安装包**）。要把 xihe+desktop 整体打成可运行软件，三平台可行性差异巨大：

| 平台 | 可行性 | 形态 | Python 大脑位置 | 分发 |
|------|--------|------|----------------|------|
| **Windows** | ✅ 可行 | Electron 安装包 + 本地冻结的 `xihe serve` 子进程 | bundle 进安装包（本地） | NSIS / portable + Authenticode |
| **macOS** | ✅ 可行（成本更高） | `.app`/`.dmg` + 本地冻结的 `xihe serve` | bundle 进 `.app`（本地） | dmg + 签名/公证（气隙受限） |
| **iOS** | ❌ 不能同款 | 瘦客户端 UI 连**远程** serve/gateway | 留服务端（不上设备） | 企业/MDM/TestFlight（App Store 没戏） |

**一句话**：打包本质上只是**决定 Python 大脑跑在哪**——本地 bundle（Windows/macOS 桌面）还是远程（iOS/移动瘦客户端）。desktop 现在经 WS/REST 跟 `xihe serve` 通信（[[0024]]）、而非进程内嵌引擎（[[0029]]），这条接缝正是让两种位置都成立的 enabler。

## 前提：当前运行时架构

- desktop `main` 经 `ServeSupervisor`（[[0029]]）spawn 一个 `xihe serve` 子进程，renderer 经 WS `/stream` + REST 驱动它。
- `xihe serve` 读单一 `config.yaml`（[[0023]] / 配置收敛）、开 `sessions.db`/`agent.log`、按需加载 Playwright（系统 Chrome）/ PaddleOCR（离线 `~/.paddlex`）/ MCP。
- `ServeSupervisor` 现在按 **PATH 找 `xihe`** 来 spawn——假定机器装了 Python + xihe。**打包后的机器可能两者都没有**，这是打包的核心待解点。
- 依赖约束（本部署）：气隙内网（包走内部 PyPI/npm 镜像）、默认模型 glm-5.2-zp 非多模态（视觉走 `vision_model`/`image_ocr`）、Playwright 用系统 Chrome/Edge（不靠 bundled Chromium）。

## Windows 打包（最直接，4 步）

### ① 冻结 `xihe serve` 成独立 exe
把 xihe（`cli.app:main`）冻结成自带解释器的 `xihe.exe`，`ServeSupervisor` 改 spawn app resources 目录下的那个。两条路线：
- **PyInstaller**（`--onedir`）→ `xihe.exe` + 依赖目录。常见，但 **PaddlePaddle/PaddleOCR 是大坑**（一堆 `.pyd`/`.dll` + `~/.paddlex` 模型），建议把 OCR 做成**可选模块**（核心包不含 paddle，单独 bundle 或首跑从内网镜像拉）。Playwright 继续走系统 Chrome，PyInstaller 加 playwright driver hook。
- **嵌入式 Python**（`python-embed` + 整包 site-packages 放进 `python/`）→ 对 paddle 这种重原生栈比 PyInstaller 稳，体积大但少踩 hidden-import 坑。OCR 重时倾向这条。

### ② electron-builder 出安装包
加 `electron-builder` devDep + 配置，`extraResources` 把冻结的 `xihe.exe` + paddlex 模型打进去，`win.target: nsis`/`portable`。
> 气隙注意：electron-builder 构建时从 GitHub 拉 `winCodeSign`/`nsis`，需内部镜像或预置。

### ③ 首次运行：数据目录 + 引导写 config（不带凭据出货）
安装包**绝不带 `api_key`**。首跑：建实例数据目录（如 `%APPDATA%\xihe\` + 它自己的 `config.yaml`，走桌面 `--config`/xiheConfig 机制，[[0023]]），写无凭据默认 config，用 **ConfigPanel 引导**收 `api_key`/`base_url`/model。`ServeSupervisor` 改 spawn `xihe.exe serve --config <appdata>/config.yaml …`，数据根跟 app 走。

### ④ 签名
Authenticode + 内部证书（signtool），免 SmartScreen 拦。

> LLM 可达性：打包后仍需联网到内部 glm 网关——云模型 agent 的固有性质，断网约等于不能用。

## macOS 增量（骨架同 Windows，6 个差异点）

1. **必须在 mac 上构建**（最大实际障碍）。electron-builder / PyInstaller 都**不能从 Windows 交叉构建 mac 产物**；当前环境是 Windows + 气隙，做 mac 版先得有台 mac。
2. **签名 + 公证（比 Windows 严）**。Gatekeeper 默认拦未签名/未公证 app。**公证（notarization）要上传到 Apple 服务器——气隙做不了**。两条路：ad-hoc 签名（`codesign --sign -`）+ 用户 `xattr -d com.apple.quarantine` 放行（内部分发起步）；或 MDM 预信任。完整公证需 Apple 开发者证书 + 能连 Apple 的机器。
3. **arm64 / x64 / universal**。Apple Silicon 是主流；冻结的 python 二进制要对应架构（PyInstaller 按 arch 出；universal 得 build 两份 `lipo` 合）。**⚠️ PaddlePaddle 在 mac arm64 的 wheel 支持待验证**——不行则 OCR 在 mac 版「不可用 / x64-only」。这是 mac 版独有坑（Windows 已跑通离线 paddle）。
4. **bundle 内所有二进制都得签名（deep signing）**。electron-builder 只签 Electron 部分；冻结的 `xihe` + 它的 `.dylib`/`.so`（含 paddle 原生库）必须用 `afterSign` hook 一起签，否则 Gatekeeper 下启动即崩。
5. **Hardened runtime + entitlements**。公证要求 hardened runtime（`--options runtime`）；若 python 要加载未签名库（paddle）就需 entitlements 开 `com.apple.security.cs.disable-library-validation`，削弱公证（又是 paddle 连锁麻烦）。
6. **数据目录走 mac 惯例 + Mac App Store 没戏**。数据根 `~/Library/Application Support/xihe/`；Mac App Store 的 sandbox 禁止 spawn 子进程 + 任意文件访问（与 xihe 本质冲突），只能 dmg / MDM。

**electron-builder mac 配置（示意）**：

```jsonc
"mac": {
  "target": [{ "target": "dmg", "arch": ["arm64"] }],   // 或 "universal"
  "hardenedRuntime": true,
  "entitlements": "build/entitlements.mac.plist",
  "notarize": false                                      // 气隙做不了；外网构建机再开
},
"extraResources": [
  { "from": "../dist/xihe-frozen/", "to": "xihe-runtime/" },
  { "from": "../assets/paddlex/",   "to": "paddlex/" }
],
"afterSign": "build/sign-python.js"                      // 签 extraResources 里的 .dylib/.so/xihe
```

`ServeSupervisor` 用 Electron 的 `process.resourcesPath` 解析：`xihe.app/Contents/Resources/xihe-runtime/xihe`。

## iOS：不能同款，只能瘦客户端

- **Electron 没有 iOS 端；Python 也无法在 iOS 上以子进程跑**（无 subprocess；paddle/playwright 无 iOS 原生构建）。xihe+desktop **没法作为一个 bundle 跑到 iPhone**。
- 唯一可行形态：**原生 SwiftUI / React Native（可复用部分 TSX+Zustand 逻辑）/ Capacitor-webview 做薄 UI，连一台远程 `xihe serve` / `xihe gateway`**，复用现有 WS 协议。iOS = 「又一个前端」，大脑留服务端——正是已有的 gateway 部署。
- **App Store 政策**：能跑 shell / 工具调用的 agent 过不了审核 → 企业分发 / TestFlight / MDM / 自托管；或阉割成纯对话版本过审。
- 若「Apple 平台」是泛指：**macOS 容易**（同 Windows 那套，出 `.app` + 公证），**iOS 才是墙**。

## 共同主干：serve 协议是打包的 enabler

- 因为 desktop 经 WS/REST 跟 serve 通信（非进程内嵌），「打包」= 决定 Python 大脑跑哪：本地 bundle（Win/macOS）或远程（iOS/移动）。**若当初把引擎嵌进 main 进程，iOS 连瘦客户端都难做。** 这是选 serve 架构（[[0024]]/[[0029]]）的额外回报。
- **Windows 与 macOS 是同一套代码、两条 target**，差别只在签名 / 架构 / bundle 细节。做 Windows 时把 `resolveXiheBin()`、`extraResources` 布局、`afterSign` 设计成 mac 可复用，第二次就省。

跨平台 spawn 路径解析（建议落点）：

```ts
function resolveXiheBin(): string {
  const dir = path.join(process.resourcesPath, 'xihe-runtime', 'xihe')
  return process.platform === 'win32' ? dir + '.exe' : dir
}
// spawn: resolveXiheBin() serve --config <userData>/config.yaml --host 127.0.0.1 --port <p>
```

## 风险 / 待验证

- **PaddleOCR 跨平台**：PyInstaller + 重原生（paddle）打包复杂；mac arm64 wheel 是否存在未验证。→ 倾向把 OCR 做成可选模块，核心包不含 paddle。
- **气隙构建资源**：electron-builder 要从 GitHub 拉 `nsis`/`winCodeSign`；mac 构建要 mac 机器 + Apple 联网做公证。需内部镜像 / 外网跳板。
- **LLM 可达性**：所有平台都依赖内部 glm 网关可达；iOS 瘦客户端还多一层「移动设备到网关」的网络 + 鉴权。
- **签名证书**：Windows Authenticode 内部证书、mac Developer ID / 内部 CA，需就位。
- **首跑体验**：ConfigPanel 引导写 config（与桌面 `--config` 改造、[[0023]] 多实例数据根）耦合，需一并对齐。

## 建议落地顺序

1. **先 Windows**（手上的 Windows 机器就能做，不依赖 mac、不依赖 Apple 联网，产出最大化）。
2. **再 macOS**（前置：mac 构建机 + 公证策略 + paddle mac arm64 验证）。
3. **iOS 另立项**（瘦客户端 → 远程 serve/gateway；产品形态独立，不在「打包桌面」范围内）。

## 相关页面

- [[0024_desktop-serve-protocol]] —— serve 的 WS/REST 接缝（打包灵活性的根源）。
- [[0029_desktop-dual-engine-architecture]] —— desktop 整体架构（spawn 模型、main 总编排）。
- [[0026_desktop-agent-model-built-in-xihe]] —— xihe 内置、main 托管 serve 生命周期。
- [[0023_multi-instance-config]] —— `--config` 数据根隔离（首跑实例数据目录复用此机制）。
- [[0001_xihe-agent]] —— 项目总览（运行模式、依赖约束）。
