Documentation Index
Fetch the complete documentation index at: https://docs.bigmodel.cn/llms.txt Use this file to discover all available pages before exploring further.
视觉理解 MCP
视觉理解 MCP Server 是智谱为 GLM Coding Plan 用户开发的专属 Local MCP Server，基于模型上下文协议（Model Context Protocol），可为 Claude Code、Cline 等兼容 MCP 的客户端提供图像分析、视频理解等视觉能力。
如需体验 GLM-5.3-Flash 能力，请安装最新版本(>= 0.1.2) 的视觉理解MCP服务器。\   老用户可能会使用旧缓存版本，需删除 npx 缓存，或将 @z_ai/mcp-server 加上 @latest 标签强制安装最新版本，即 @z_ai/mcp-server@latest。

功能特性

}>     支持多种图像格式的智能分析和内容理解，让您的 AI Agent 拥有视觉

}>     支持本地视频与远端视频的视觉理解

}>     一键安装，快速集成到 Claude Code 等 MCP 兼容客户端


支持的工具
该服务器实现了模型上下文协议，可与任何兼容 MCP 的客户端一起使用，模型可根据用户 Prompt 自主调用最匹配的工具，实现在以下类型任务中更精准的效果。目前提供以下工具：
- ui_to_artifact - 将 UI 截图转换为代码、提示词、设计规范或自然语言描述，覆盖从前端落地到生成式设计提示的全流程
- extract_text_from_screenshot - 使用先进的 OCR 能力从截图中提取和识别文字。专门用于代码、终端输出、文档和通用文本的提取
- diagnose_error_screenshot - 解析错误弹窗、堆栈和日志截图，给出定位与修复建议
- understand_technical_diagram - 针对架构图、流程图、UML、ER 图等技术图纸生成结构化解读
- analyze_data_visualization - 阅读仪表盘、统计图表，提炼趋势、异常与业务要点
- ui_diff_check - 对比两张 UI 截图，识别视觉差异和实现偏差。专门用于 UI 质量保证和设计到实现的验证
- image_analysis - 通用图像理解能力，适配未被专项工具覆盖的视觉内容
- video_analysis - 支持 MP4/MOV/M4V(限制本地最大8M) 等格式的视频场景解析，抓取关键帧、事件与要点
  环境变量配置
  详细配置说明
  |环境变量|说明|默认值|可选值|
  |-|-|-|-|
  |Z_AI_API_KEY|智谱 API KEY|必需配置|您的API密钥|
  |Z_AI_MODE|服务平台选择|ZHIPU|ZHIPU 或 ZAI|

安装与使用
快速开始


- 个人版套餐的用户，通过 https://bigmodel.cn/coding-plan/personal/overview ，新建  API Key
- 团队版套餐的成员，通过 http://bigmodel.cn/coding-plan?z_plan=team ，获取  API Key（团队套餐 Key 与平台其他 API Key 不通用，使用团队额度请务必使用团队套餐 Key）

  前提条件：您需要安装 https://nodejs.org/en/download/  \     根据您使用的客户端 参考下方 选择相应的安装方式


支持的客户端


       在 Claude Code 中使用 GLM Coding Plan 时，模型服务端已内置 image_analysis 工具，具备图片理解能力，无需安装。如需使用/cn/coding-plan/mcp/vision-mcp-server#%E6%94%AF%E6%8C%81%E7%9A%84%E5%B7%A5%E5%85%B7 ，再按以下方式安装。

方式一：一键安装命令
注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
claude mcp add -s user zai-mcp-server --env Z_AI_API_KEY=YOUR_API_KEY -- npx -y "@z_ai/mcp-server"
若您忘记替换 API Key，重新执行安装命令前需要先卸载旧的此 MCP Server：
claude mcp list
claude mcp remove zai-mcp-server
若您在 Windows 系统的 PowerShell 中执行上述命令时遇到 -y 参数问题，请尝试使用 Windows 命令提示符 (CMD) 执行相同的命令。     若遇到告警 Windows requires 'cmd /c' wrapper to execute npx，可以忽略。
方式二：手动配置
编辑 Claude Code 的配置文件, 位于用户目录下 .claude.json 的 MCP 部分：\     注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
{
"mcpServers": {
"zai-mcp-server": {
"type": "stdio",
"command": "npx",
"args": [
"-y",
"@z_ai/mcp-server"
],
"env": {
"Z_AI_API_KEY": "YOUR_API_KEY",
"Z_AI_MODE": "ZHIPU"
}
}
}
}

     在 Cline 扩展设置中添加 MCP 服务器配置：
注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
{
"mcpServers": {
"zai-mcp-server": {
"type": "stdio",
"command": "npx",
"args": [
"-y",
"@z_ai/mcp-server"
],
"env": {
"Z_AI_API_KEY": "YOUR_API_KEY",
"Z_AI_MODE": "ZHIPU"
}
}
}
}

     在 OpenCode 设置中添加 MCP 服务器配置：
参考 https://opencode.ai/docs/mcp-servers
注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
{
"$schema": "https://opencode.ai/config.json",
"mcp": {
"zai-mcp-server": {
"type": "local",
"command": ["npx","-y","@z_ai/mcp-server"],
"environment": {
"Z_AI_API_KEY": "YOUR_API_KEY",
"Z_AI_MODE": "ZHIPU"
}
}
}
}

     在 Crush 设置中添加 MCP 服务器配置：
注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
{
"$schema": "https://charm.land/crush.json",
"mcp": {
"zai-mcp-server": {
"type": "stdio",
"command": "npx",
"args": [
"-y",
"@z_ai/mcp-server"
],
"env": {
"Z_AI_API_KEY": "YOUR_API_KEY",
"Z_AI_MODE": "ZHIPU"
}
}
}
}

     对于 Roo Code, Kilo Code 等其它支持 MCP 协议的客户端，参考以下通用配置：
注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
{
"mcpServers": {
"zai-mcp-server": {
"type": "stdio",
"command": "npx",
"args": [
"-y",
"@z_ai/mcp-server"
],
"env": {
"Z_AI_API_KEY": "YOUR_API_KEY",
"Z_AI_MODE": "ZHIPU"
}
}
}
}


使用示例
通过上一步将视觉 MCP 服务器安装到客户端后，您就可以在自己的 Coding 客户端通过对话的方式直接使用MCP了。\ 比如下面在 Claude Code 中，对话输入 hi describe this xx.png，MCP Server 会处理图片并返回描述结果。(前置条件是您的当前目录下有该图片)
除了 Claude Code 之外，直接在客户端粘贴图片无法调用此 MCP Server，客户端默认会将图片转码后直接调用模型接口。最佳实践是将图片放到本地目录，通过对话的方式指定图片名称或路径来调用 Mcp Server。例如: What does demo.png describe?

[图片]
[图片]
故障排除
在本地命令行直接执行下面的命令，验证其是否能安装到本地，用于排查是否是环境，权限等问题：

Z_AI_API_KEY=YOUR_API_KEY npx -y @z_ai/mcp-server
set Z_AI_API_KEY=YOUR_API_KEY && npx -y @z_ai/mcp-server
$env:Z_AI_API_KEY="YOUR_API_KEY"; npx -y @z_ai/mcp-server

- 若安装成功，则表示环境正确，问题可能在客户端配置上，请检查客户端的 MCP 配置。
- 若安装失败，请根据错误信息进行排查，建议将错误信息粘贴给大模型进行分析解决。
  其它常见问题：


问题： MCP 服务器连接失败
解决方案：
1. 检查本地是否存在 Node.js 18 或更新版本
2. node -v 和 npx -v 查看是否拥有执行环境
3. 确认环境变量 Z_AI_API_KEY 是否正确配置


问题： 收到 API Key 无效的错误
解决方案：
1. 确认 API Key 是否正确复制
2. 检查 API Key 是否已激活
3. 确认选择的平台 (Z_AI_MODE) 与 API Key 匹配
4. 检查 API Key 是否有足够的余额


问题： MCP 服务器连接超时
解决方案：
1. 检查网络连接
2. 确认防火墙设置
3. 尝试切换到不同的平台 (ZHIPU 或 ZAI)
4. 增加超时时间设置


相关资源
- https://modelcontextprotocol.io/
- https://docs.anthropic.com/en/docs/claude-code/mcp
- https://docs.bigmodel.cn/cn/coding-plan/overview#%E4%B8%93%E5%B1%9E-mcp
- /cn/guide/models/vlm/glm-4.6v

Documentation Index
Fetch the complete documentation index at: https://docs.bigmodel.cn/llms.txt Use this file to discover all available pages before exploring further.
视觉理解 MCP
视觉理解 MCP Server 是智谱为 GLM Coding Plan 用户开发的专属 Local MCP Server，基于模型上下文协议（Model Context Protocol），可为 Claude Code、Cline 等兼容 MCP 的客户端提供图像分析、视频理解等视觉能力。
如需体验 GLM-5.3-Flash 能力，请安装最新版本(>= 0.1.2) 的视觉理解MCP服务器。\   老用户可能会使用旧缓存版本，需删除 npx 缓存，或将 @z_ai/mcp-server 加上 @latest 标签强制安装最新版本，即 @z_ai/mcp-server@latest。

功能特性

}>     支持多种图像格式的智能分析和内容理解，让您的 AI Agent 拥有视觉

}>     支持本地视频与远端视频的视觉理解

}>     一键安装，快速集成到 Claude Code 等 MCP 兼容客户端


支持的工具
该服务器实现了模型上下文协议，可与任何兼容 MCP 的客户端一起使用，模型可根据用户 Prompt 自主调用最匹配的工具，实现在以下类型任务中更精准的效果。目前提供以下工具：
- ui_to_artifact - 将 UI 截图转换为代码、提示词、设计规范或自然语言描述，覆盖从前端落地到生成式设计提示的全流程
- extract_text_from_screenshot - 使用先进的 OCR 能力从截图中提取和识别文字。专门用于代码、终端输出、文档和通用文本的提取
- diagnose_error_screenshot - 解析错误弹窗、堆栈和日志截图，给出定位与修复建议
- understand_technical_diagram - 针对架构图、流程图、UML、ER 图等技术图纸生成结构化解读
- analyze_data_visualization - 阅读仪表盘、统计图表，提炼趋势、异常与业务要点
- ui_diff_check - 对比两张 UI 截图，识别视觉差异和实现偏差。专门用于 UI 质量保证和设计到实现的验证
- image_analysis - 通用图像理解能力，适配未被专项工具覆盖的视觉内容
- video_analysis - 支持 MP4/MOV/M4V(限制本地最大8M) 等格式的视频场景解析，抓取关键帧、事件与要点
  环境变量配置
  详细配置说明
  |环境变量|说明|默认值|可选值|
  |-|-|-|-|
  |Z_AI_API_KEY|智谱 API KEY|必需配置|您的API密钥|
  |Z_AI_MODE|服务平台选择|ZHIPU|ZHIPU 或 ZAI|

安装与使用
快速开始


- 个人版套餐的用户，通过 https://bigmodel.cn/coding-plan/personal/overview ，新建  API Key
- 团队版套餐的成员，通过 http://bigmodel.cn/coding-plan?z_plan=team ，获取  API Key（团队套餐 Key 与平台其他 API Key 不通用，使用团队额度请务必使用团队套餐 Key）

  前提条件：您需要安装 https://nodejs.org/en/download/  \     根据您使用的客户端 参考下方 选择相应的安装方式


支持的客户端


       在 Claude Code 中使用 GLM Coding Plan 时，模型服务端已内置 image_analysis 工具，具备图片理解能力，无需安装。如需使用/cn/coding-plan/mcp/vision-mcp-server#%E6%94%AF%E6%8C%81%E7%9A%84%E5%B7%A5%E5%85%B7 ，再按以下方式安装。

方式一：一键安装命令
注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
claude mcp add -s user zai-mcp-server --env Z_AI_API_KEY=YOUR_API_KEY -- npx -y "@z_ai/mcp-server"
若您忘记替换 API Key，重新执行安装命令前需要先卸载旧的此 MCP Server：
claude mcp list
claude mcp remove zai-mcp-server
若您在 Windows 系统的 PowerShell 中执行上述命令时遇到 -y 参数问题，请尝试使用 Windows 命令提示符 (CMD) 执行相同的命令。     若遇到告警 Windows requires 'cmd /c' wrapper to execute npx，可以忽略。
方式二：手动配置
编辑 Claude Code 的配置文件, 位于用户目录下 .claude.json 的 MCP 部分：\     注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
{
"mcpServers": {
"zai-mcp-server": {
"type": "stdio",
"command": "npx",
"args": [
"-y",
"@z_ai/mcp-server"
],
"env": {
"Z_AI_API_KEY": "YOUR_API_KEY",
"Z_AI_MODE": "ZHIPU"
}
}
}
}

     在 Cline 扩展设置中添加 MCP 服务器配置：
注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
{
"mcpServers": {
"zai-mcp-server": {
"type": "stdio",
"command": "npx",
"args": [
"-y",
"@z_ai/mcp-server"
],
"env": {
"Z_AI_API_KEY": "YOUR_API_KEY",
"Z_AI_MODE": "ZHIPU"
}
}
}
}

     在 OpenCode 设置中添加 MCP 服务器配置：
参考 https://opencode.ai/docs/mcp-servers
注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
{
"$schema": "https://opencode.ai/config.json",
"mcp": {
"zai-mcp-server": {
"type": "local",
"command": ["npx","-y","@z_ai/mcp-server"],
"environment": {
"Z_AI_API_KEY": "YOUR_API_KEY",
"Z_AI_MODE": "ZHIPU"
}
}
}
}

     在 Crush 设置中添加 MCP 服务器配置：
注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
{
"$schema": "https://charm.land/crush.json",
"mcp": {
"zai-mcp-server": {
"type": "stdio",
"command": "npx",
"args": [
"-y",
"@z_ai/mcp-server"
],
"env": {
"Z_AI_API_KEY": "YOUR_API_KEY",
"Z_AI_MODE": "ZHIPU"
}
}
}
}

     对于 Roo Code, Kilo Code 等其它支持 MCP 协议的客户端，参考以下通用配置：
注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
{
"mcpServers": {
"zai-mcp-server": {
"type": "stdio",
"command": "npx",
"args": [
"-y",
"@z_ai/mcp-server"
],
"env": {
"Z_AI_API_KEY": "YOUR_API_KEY",
"Z_AI_MODE": "ZHIPU"
}
}
}
}


使用示例
通过上一步将视觉 MCP 服务器安装到客户端后，您就可以在自己的 Coding 客户端通过对话的方式直接使用MCP了。\ 比如下面在 Claude Code 中，对话输入 hi describe this xx.png，MCP Server 会处理图片并返回描述结果。(前置条件是您的当前目录下有该图片)
除了 Claude Code 之外，直接在客户端粘贴图片无法调用此 MCP Server，客户端默认会将图片转码后直接调用模型接口。最佳实践是将图片放到本地目录，通过对话的方式指定图片名称或路径来调用 Mcp Server。例如: What does demo.png describe?

[图片]
[图片]
故障排除
在本地命令行直接执行下面的命令，验证其是否能安装到本地，用于排查是否是环境，权限等问题：

Z_AI_API_KEY=YOUR_API_KEY npx -y @z_ai/mcp-server
set Z_AI_API_KEY=YOUR_API_KEY && npx -y @z_ai/mcp-server
$env:Z_AI_API_KEY="YOUR_API_KEY"; npx -y @z_ai/mcp-server

- 若安装成功，则表示环境正确，问题可能在客户端配置上，请检查客户端的 MCP 配置。
- 若安装失败，请根据错误信息进行排查，建议将错误信息粘贴给大模型进行分析解决。
  其它常见问题：


问题： MCP 服务器连接失败
解决方案：
1. 检查本地是否存在 Node.js 18 或更新版本
2. node -v 和 npx -v 查看是否拥有执行环境
3. 确认环境变量 Z_AI_API_KEY 是否正确配置


问题： 收到 API Key 无效的错误
解决方案：
1. 确认 API Key 是否正确复制
2. 检查 API Key 是否已激活
3. 确认选择的平台 (Z_AI_MODE) 与 API Key 匹配
4. 检查 API Key 是否有足够的余额


问题： MCP 服务器连接超时
解决方案：
1. 检查网络连接
2. 确认防火墙设置
3. 尝试切换到不同的平台 (ZHIPU 或 ZAI)
4. 增加超时时间设置


相关资源
- https://modelcontextprotocol.io/
- https://docs.anthropic.com/en/docs/claude-code/mcp
- https://docs.bigmodel.cn/cn/coding-plan/overview#%E4%B8%93%E5%B1%9E-mcp
- /cn/guide/models/vlm/glm-4.6v

Documentation Index
Fetch the complete documentation index at: https://docs.bigmodel.cn/llms.txt Use this file to discover all available pages before exploring further.
网页读取 MCP
网页读取 MCP Server 是智谱为 GLM Coding Plan 用户开发的专属 Remote MCP Server，基于模型上下文协议（Model Context Protocol）接入网页内容抓取能力，可为 Claude Code、Cline 等兼容 MCP 的客户端提供网页内容提取、详细内容读取与结构化数据获取等能力。
功能特性

}>     支持抓取任意网页的完整内容，包括文本、链接等

}>     提取网页的结构化数据，包括标题、正文、元数据等

}>     基于 HTTP 协议的远程 MCP 服务，无需本地安装


支持的工具
该服务器实现了模型上下文协议，可与任何兼容 MCP 的客户端一起使用。目前提供以下工具：
- webReader - 抓取指定URL的网页内容，返回结果包括网页标题、正文内容、元数据、链接列表等。
  示例场景

  自动抓取并解析官方文档页面的标题、正文、示例与版本说明，提炼要点摘要，帮助快速对接与实现。

  解析项目官网或仓库页面（如 README、Release Notes、使用指南），提取核心信息与链接列表，辅助评估与集成。

  从博客、教程、指南页面提取步骤、命令与注意事项，将非结构化内容整理为可用的开发笔记与任务清单。

  问题修复，读取指定网页的公开信源已有的步骤，参考修复问题。

  将指定网页内容转换为结构化数据，并结合页面内链接进行增量同步，构建团队技术知识库。


安装与使用
快速开始


- 个人版套餐的用户，通过 https://bigmodel.cn/coding-plan/personal/overview ，新建  API Key
- 团队版套餐的成员，通过 http://bigmodel.cn/coding-plan?z_plan=team ，获取  API Key（团队套餐 Key 与平台其他 API Key 不通用，使用团队额度请务必使用团队套餐 Key）

  根据您使用的客户端 参考下方 选择相应的配置方式


支持的客户端


       在 Claude Code 中使用 GLM Coding Plan 时，模型服务端已内置网页读取 MCP，无需安装。\       如您希望在调用其他非智谱模型时仍应用此 MCP，再按以下方式安装。

一键安装命令     注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
claude mcp add -s user -t http web-reader https://open.bigmodel.cn/api/mcp/web_reader/mcp --header "Authorization: Bearer YOUR_API_KEY"
手动配置
编辑 Claude Code 的配置文件, 位于用户目录下 .claude.json 的 MCP 部分：
{
"mcpServers": {
"web-reader": {
"type": "http",
"url": "https://open.bigmodel.cn/api/mcp/web_reader/mcp",
"headers": {
"Authorization": "Bearer YOUR_API_KEY"
}
}
}
}

     在 Cline 扩展设置中添加 MCP 服务器配置：
注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
{
"mcpServers": {
"web-reader": {
"type": "streamableHttp",
"url": "https://open.bigmodel.cn/api/mcp/web_reader/mcp",
"headers": {
"Authorization": "Bearer YOUR_API_KEY"
}
}
}
}
若老版本 Cline 不支持 StreamableHttp 类型的 MCP 服务器，可以使用 SSE 类型的配置：
{
"mcpServers": {
"web-reader": {
"type": "sse",
"url": "https://open.bigmodel.cn/api/mcp/web_reader/sse?Authorization=YOUR_API_KEY"
}
}
}

     在 OpenCode 设置中添加 MCP 服务器配置：
参考 https://opencode.ai/docs/mcp-servers
注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
{
"$schema": "https://opencode.ai/config.json",
"mcp": {
"web-reader": {
"type": "remote",
"url": "https://open.bigmodel.cn/api/mcp/web_reader/mcp",
"headers": {
"Authorization": "Bearer YOUR_API_KEY"
}
}
}
}

     在 Crush 设置中添加 MCP 服务器配置：
注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
{
"$schema": "https://charm.land/crush.json",
"mcp": {
"web-reader": {
"type": "http",
"url": "https://open.bigmodel.cn/api/mcp/web_reader/mcp",
"headers": {
"Authorization": "Bearer YOUR_API_KEY"
}
}
}
}

     暂时 Goose 不支持，详见 https://github.com/block/goose/issues/6576 
在 Goose 设置中添加 MCP 服务器配置：
点击 Extensions -> Add custom extension
配置 Extension Name 为 web-reader，Type 选择 HTTP，Endpoint 填写如下 URL：
https://open.bigmodel.cn/api/mcp/web_reader/mcp
配置 Request Headers 添加 Authorization : YOUR_API_KEY
最后点击底部 Add Extension 即可，注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key

     对于 Roo Code, Kilo Code 等其它支持 MCP 协议的客户端，参考以下通用配置：
注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
{
"mcpServers": {
"web-reader": {
"type": "streamable-http",
"url": "https://open.bigmodel.cn/api/mcp/web_reader/mcp",
"headers": {
"Authorization": "Bearer YOUR_API_KEY"
}
}
}
}


故障排除


问题： 收到访问令牌无效的错误
解决方案：
1. 确认访问令牌是否正确复制
2. 检查访问令牌是否已激活
3. 确认访问令牌是否有足够的余额
4. 检查 Authorization header 格式是否正确


问题： MCP 服务器连接超时
解决方案：
1. 检查网络连接
2. 确认防火墙设置
3. 验证服务器 URL 是否正确
4. 增加超时时间设置


问题： 网页内容抓取返回空结果或错误
解决方案：
1. 确认目标 URL 是否可访问
2. 检查网页是否存在反爬虫机制
3. 尝试使用不同的网页 URL
4. 确认网络连接正常
5. 联系技术支持获取帮助


相关资源
- https://modelcontextprotocol.io/
- https://docs.anthropic.com/en/docs/claude-code/mcp
- https://docs.bigmodel.cn/cn/coding-plan/overview#%E4%B8%93%E5%B1%9E-mcp
- /cn/coding-plan/overview

Documentation Index
Fetch the complete documentation index at: https://docs.bigmodel.cn/llms.txt Use this file to discover all available pages before exploring further.
开源仓库 MCP
开源仓库 MCP Server（ZRead MCP）是智谱为 GLM Coding Plan 用户开发的专属 Remote MCP Server，基于模型上下文协议（Model Context Protocol）和 https://zread.ai  能力，可为 Claude Code、Cline 等兼容 MCP 的客户端提供开源仓库知识文档、代码结构与文件内容访问能力。
功能特性

}>     GitHub 代码仓库检索文档、代码与注释

}>     获取 GitHub 仓库的目录结构和文件列表，快速掌握项目布局

}>     读取 GitHub 仓库中指定文件的完整代码内容，深入分析实现细节


支持的工具
该服务器实现了模型上下文协议，可与任何兼容 MCP 的客户端一起使用。目前提供以下工具：
- search_doc - 搜索 GitHub 仓库的对应的知识文档，快速了解仓库知识，新闻，最近的 issue pr 和贡献者等。
- get_repo_structure - 获取 GitHub 仓库的目录结构和文件列表，了解项目模块拆分和目录组织方式。
- read_file - 读取 GitHub 仓库中指定文件的完整代码内容，深入文件代码的实现细节。
  示例场景

  通过搜索文档和获取仓库结构，快速了解开源库的核心概念、安装步骤和代码组织方式，加速学习曲线。

  在遇到问题时，搜索仓库的 Issue 和 Commit 历史，查找是否有类似问题的解决方案或修复记录。

  直接读取核心文件的代码内容，分析实现逻辑，辅助进行二次开发或 Debug。

  在引入新的依赖库之前，通过查看其仓库结构和文档，评估其活跃度、代码质量和维护情况。


安装与使用
快速开始


- 个人版套餐的用户，通过 https://bigmodel.cn/coding-plan/personal/overview ，新建  API Key
- 团队版套餐的成员，通过 http://bigmodel.cn/coding-plan?z_plan=team ，获取  API Key（团队套餐 Key 与平台其他 API Key 不通用，使用团队额度请务必使用团队套餐 Key）

  根据您使用的客户端 参考下方 选择相应的配置方式


支持的客户端


一键安装命令
注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
claude mcp add -s user -t http zread https://open.bigmodel.cn/api/mcp/zread/mcp --header "Authorization: Bearer YOUR_API_KEY"
手动配置
编辑 Claude Code 的配置文件, 位于用户目录下 .claude.json 的 MCP 部分：
{
"mcpServers": {
"zread": {
"type": "http",
"url": "https://open.bigmodel.cn/api/mcp/zread/mcp",
"headers": {
"Authorization": "Bearer YOUR_API_KEY"
}
}
}
}

     在 Cline 扩展设置中添加 MCP 服务器配置：
注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
{
"mcpServers": {
"zread": {
"type": "streamableHttp",
"url": "https://open.bigmodel.cn/api/mcp/zread/mcp",
"headers": {
"Authorization": "Bearer YOUR_API_KEY"
}
}
}
}
若老版本 Cline 不支持 StreamableHttp 类型的 MCP 服务器，可以使用 SSE 类型的配置：
{
"mcpServers": {
"zread": {
"type": "sse",
"url": "https://open.bigmodel.cn/api/mcp/zread/sse?Authorization=YOUR_API_KEY"
}
}
}

     在 OpenCode 设置中添加 MCP 服务器配置：
参考 https://opencode.ai/docs/mcp-servers
注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
{
"$schema": "https://opencode.ai/config.json",
"mcp": {
"zread": {
"type": "remote",
"url": "https://open.bigmodel.cn/api/mcp/zread/mcp",
"headers": {
"Authorization": "Bearer YOUR_API_KEY"
}
}
}
}

     在 Crush 设置中添加 MCP 服务器配置：
注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
{
"$schema": "https://charm.land/crush.json",
"mcp": {
"zread": {
"type": "http",
"url": "https://open.bigmodel.cn/api/mcp/zread/mcp",
"headers": {
"Authorization": "Bearer YOUR_API_KEY"
}
}
}
}

     暂时 Goose 不支持，详见 https://github.com/block/goose/issues/6576 
在 Goose 设置中添加 MCP 服务器配置：
点击 Extensions -> Add custom extension
配置 Extension Name 为 zread，Type 选择 HTTP，Endpoint 填写如下 URL：
https://open.bigmodel.cn/api/mcp/zread/mcp
配置 Request Headers 添加 Authorization : YOUR_API_KEY
最后点击底部 Add Extension 即可，注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key

     对于 Roo Code, Kilo Code 等其它支持 MCP 协议的客户端，参考以下通用配置：
注意替换里面的 YOUR_API_KEY 为您上一步获取到的 API Key
{
"mcpServers": {
"zread": {
"type": "streamable-http",
"url": "https://open.bigmodel.cn/api/mcp/zread/mcp",
"headers": {
"Authorization": "Bearer YOUR_API_KEY"
}
}
}
}


故障排除


问题： 收到访问令牌无效的错误
解决方案：
1. 确认访问令牌是否正确复制
2. 检查访问令牌是否已激活
3. 确认访问令牌是否有足够的余额
4. 检查 Authorization header 格式是否正确


问题： MCP 服务器连接超时
解决方案：
1. 检查网络连接
2. 确认防火墙设置
3. 验证服务器 URL 是否正确
4. 增加超时时间设置


问题： 无法搜索或读取指定仓库内容
解决方案：
1. 确认仓库是否存在且为开源（公开）仓库
2. 检查仓库名称拼写是否正确 (owner/repo)
3. 访问 zread.ai 搜索此开源仓库是否被收纳支持


相关资源
- https://modelcontextprotocol.io/
- https://docs.anthropic.com/en/docs/claude-code/mcp
- https://docs.bigmodel.cn/cn/coding-plan/overview#%E4%B8%93%E5%B1%9E-mcp
- /cn/coding-plan/overview