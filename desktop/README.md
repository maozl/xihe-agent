# xihe desktop

xihe的桌面控制面（control plane）。桌面 main 进程**内置托管 `xihe serve` 子进程**（启动 / 健康检查 / 崩溃重启 / 退出清理）——用户无需手敲 `xihe serve`、也见不到 "serve" 字样；渲染进程经 REST + WS 与该子进程对话。**xihe是唯一的内置 agent**，其它引擎（如 claude）作为xihe的 `external_agent` 工具经由xihe访问，不作为并列进程。

> 现为 [xihe-agent](../) 仓库的 `desktop/` 子目录：Python agent 在仓库根，二者靠 `xihe serve` 协议通信，不共享代码。

## 现状

已接入真实 `xihe serve`（不再依赖 mock）：

- **内置xihe agent**：main spawn 并监管 `xihe` 进程，sidebar 显示其连接状态（运行中 / 启动中 / 未运行）；`/health` 就绪后切换为 serve-backed，未就绪时回落到本地预览（mock 流式回复）。
- **对话面板**：经 WS 流式输出真实回合（文本 / 思考 / 工具调用 trace），支持中断（interrupt）与插话（steer）；会话历史持久化在xihe侧的 `sessions.db`。
- **工作空间**：可复用的工作目录，按会话绑定成 cwd（桌面侧持久化，serve 本身不感知工作空间）。
- **管理面板**：只读展示 MCP / skills / 定时任务（进程级，拉取自 serve）；外加**配置编辑器**——行级补丁写回 `~/.xihe-agent/config.yaml`（保留注释），改完点「重启xihe」生效。

## 运行

```bash
# 首次安装：.npmrc 已配内部镜像 + ELECTRON_MIRROR，离线可装
npm install
npm run dev      # 启动开发模式，弹出窗口
```

类型检查 / 打 bundle：`npm run build`（electron-vite 产出 main / preload / renderer 三 bundle）。打成可安装包（electron-builder）的配置待补。

## 架构要点

- **内置xihe**：xihe不是"经协议连接的远端服务"，而是 main 托管的子进程；agent 是**类型**概念，目前只内置一个xihe槽位。其它引擎走xihe的内部能力（claude = `external_agent` 工具），暂不作为并列 connector。
- **能力驱动 UI**：serve 上报 capability descriptor，UI 按 flag 分支（shell / browser / mcp / interrupt / escalation…），**绝不 `if(engine==)`**。
- **配置单源**：所有配置在 `~/.xihe-agent/config.yaml`（无 `.env` / 无环境变量覆盖）。桌面只行级补丁精选键（模型与连接 / 行为与安全 / 能力开关），其余（平台凭据、MCP、web 搜索 key 等）请直接编辑该文件。
- **数据归属**：对话历史 / agent 定义 / skills / MCP 都归xihe拥有（真理在 serve 侧），桌面只存引用 + 工作空间绑定（annotate, don't duplicate）。

## 路线

- claude 作为 `external_agent` 在桌面侧的可发现 / 可配置 UI（当前仅作为xihe的工具，桌面无独立入口）。
- codebuddy / 其它引擎类型。
- 定时任务的触发与审批（scheduler + escalation）在桌面侧的 UI。
