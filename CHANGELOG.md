# 更新日志

本文件记录 VoxRelay 各版本的改动。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [0.3.0] - 2026-11-23

### 新增
- **引擎四选一**：GSV / RVC / Higgs / CosyVoice 四个吃显存的引擎，同一时刻只允许一个在跑。面板上点另一个，会先停掉当前的、等端口释放、再启目标（`POST /v1/engine`，`VS.switch()`）。
- 接入 **Higgs**（8791）和 **CosyVoice**（8789）两个新引擎的服务项，各自独立 venv，和 GSV 那套环境互不干扰。
- 面板「三件套」改为引擎四选一：显示当前引擎高亮、各引擎显存占用，按钮从「启动/停止」改为「切到这里」。
- 互斥守护：自愈每轮先检查组内是否多于一个在跑，多出来的停掉，保留 `intent=on` 的那个。这样旧版入口（没重启的 MCP、手写脚本）把全套拉起来，也会在 15 秒内被纠正。
- **合成链路接入 higgs/cosy**：`voice_engines.synthesize` 补上 `_higgs_synth` / `_cosy_synth` 两个引擎分支（含失败回退默认音色），`engine_status` 补 `higgs`/`cosy` 字段（端口监听判断），`list_voices` 返回 `prompt_text`/`instruct`。此前四选一只有进程级互斥，合成链路仍走旧引擎。
- **长文本合成**：服务端新增 `POST /v1/long-tts`（自动按句切段、逐段合成、拼成单个 16bit PCM WAV，段间停顿可调），配 `GET /long` 独立页面和 `GET /long-file?name=` 下载接口。
- **面板内「长文合成」页签**：插件新增 `LongPanel` 组件，侧边栏保持单一条目「语音」，打开后顶部用页签在「语音 / 长文合成」之间切换。
- **添加克隆音色**：`POST /v1/voice`（添加 higgs/cosy 零样本克隆音色，写回 config.json）+ `POST /v1/voice/delete`（只允许删 `custom-` 前缀的音色）。面板「添加音色」表单：填音色名 + 参考音频 + 参考台词（CosyVoice 还可填语气指令），不用再手改 config.json。
- **音色下拉按引擎分组**：用 `<optgroup>` 把 GSV / RVC / Higgs / CosyVoice / 系统 TTS 分开，一眼看出音色归属。
- **语气提示词展示**：选中 Higgs/CosyVoice 音色时，展示该音色的参考音台词（prompt_text）和语气指令（instruct），自动剥离 CosyVoice 的 `<|endofprompt|>` 系统前缀。
- **长文合成语气词辅助**：长文合成页新增「语气词辅助」区——选 CosyVoice 音色时可填语气指令（覆盖音色默认 instruct），并列出各模型语气能力（CosyVoice 支持语气指令 / Higgs 靠 temperature / GSV 靠参考音）。`/v1/long-tts` 新增 `instruct` 参数，透传到 `synthesize` 并并入缓存键。

### 修复
- **`/health` 要 4 秒才返回**：原来同步 HTTP 探测 GSV 的 `/docs` 和 RVC 的 `/health`，各 2 秒超时。改成端口探测（<1ms）。这个缺陷会让冒烟测试用 2 秒超时轮询时永远等不到就绪。
- **点「停止」停不掉**：`stop()` 不写意图，自愈 15 秒后又把服务拉回来。改为停止前先落盘 `intent=off`。
- **面板首屏慢**：`/v1/mode` 不再调 `engine_status()`（它对每个引擎做 HTTP 探测），改用端口状态 + `has_edge()/has_sapi()`。
- 端口探测超时 0.4 → 0.15 秒：面板每次刷新要探 5 个端口，0.4 秒 ×5 就是 2 秒的空白。
- 长文本拼接兼容 float32 WAV：Higgs 用 `torchaudio.save` 输出 IEEE float（`wave` 模块读不了），改为手写 RIFF 头解析 + float32→int16 转换，纯标准库实现（主 runtime 是 Python 3.13，无 numpy/audioop）。

### 变更
- `GET /v1/services` 从三个服务变五个；`/v1/mode` 新增 `engines.{higgs,cosy}`、`engine_group`、`running_engine`、`engine_ports`。
- `POST /v1/engine` 只接受互斥组内的引擎：主服务自身和未知名字一律拒绝。
- 中文音色名的 id 生成：纯中文名用 md5 前 8 位兜底（`custom-xxxxxxxx`），保证唯一稳定。

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
