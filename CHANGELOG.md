# 更新日志

本文件记录 VoxRelay 各版本的改动。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [0.2.0] - 2026-11-21

### 新增
- 朗读来源可切换：面板朗读 / MCP 朗读自主选择。面板模式逐字读完所有内容，MCP 模式更智能——长文本自动读核心要点，不念代码块、表格、路径。
- 回合结束自动朗读，带缓存 TTL 去重。
- 按客户端下发朗读规则：DSH 与 WorkBuddy 客户端自动识别，指令按身份动态调整。
- 打包为单包：服务 + DSH 面板合并为一个仓库，安装脚本自动在 DSH 里登记面板。

### 修复
- 正文提取：只朗读最终文本，不再把推理过程（思考）念出来。
- panel 模式下 WorkBuddy 无声：静默逻辑改为按客户端作用域判断，只有 DSH 面板客户端才静默。
- 面板无法滚动：内容超出时可上下滚动。
- 临时音频残留：MCP 启动时清理强杀残留的临时文件，防止 Temp 目录膨胀。

### 稳定性
- 引入跨进程启停锁：面板 / MCP / `/v1/service` 同时启停时串行化，避免抢端口。

## [0.1.0] - 2026-11-20

### 新增
- 本地语音服务（OpenAI 兼容接口 `POST /v1/audio/speech`），零外部框架依赖。
- 多引擎支持：edge-tts（免费在线）→ Windows SAPI（离线兜底）→ 上游服务 → RVC 后处理，任一引擎挂掉自动降级绕过。
- DSH 侧边栏面板：查状态、启停服务、试听。
- MCP 工具：`speak` / `set_voice_mode` / `get_voice_mode` / `list_voices` / `start_services` / `stop_services`。

### 技术说明
- 五个 Python 文件标准库实现，无 Web 框架。
- 一个仓库一个包：`package.json` 管 DSH 插件，`server.py` 管服务，共用同一份 `config.json`。
- 仓库不含任何音色模型和训练权重。
