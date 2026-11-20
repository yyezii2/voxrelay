# VoxRelay

本地语音服务。一个进程、一个端口、一个可选依赖，把 TTS 包成 **OpenAI 兼容接口**，
给任何支持 `/v1/audio/speech` 的客户端用。

```
POST http://127.0.0.1:8787/v1/audio/speech
{"input": "要念的话", "voice": "xiaoxiao", "speed": 1.0}
```

- 五个 Python 文件，标准库实现，没有 Web 框架；MCP 和侧边栏面板都是可选的
- 引擎可叠：edge-tts（免费在线）→ Windows SAPI（离线兜底）→ 上游服务 → RVC 后处理
- 任何一个引擎挂掉都会被降级绕过，**保证一定有声音出来**
- 音色由你自己带，仓库不含任何模型（见 [models/README.md](models/README.md)）

## 快速开始

需要 Python 3.10+（Windows）。

```powershell
git clone <this-repo> VoxRelay
cd VoxRelay
powershell -ExecutionPolicy Bypass -File install.ps1
start.bat
```

浏览器打开**服务窗口第一行打印的地址**（默认 <http://127.0.0.1:8787>）就是试听页。
端口取自 `config.json` 的 `port`，改了要按新地址打开。

装完直接有 11 个音色（10 个微软在线音色 + 1 个系统 SAPI），够用再往下看。

出问题了先跑自检，它会逐项告诉你是哪一环坏了：

```bash
python smoke.py
```

详细的症状排查见 [TROUBLESHOOT.md](TROUBLESHOOT.md)。

## 接口

**OpenAI 兼容 / 合成**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 引擎状态、音色数、运行时长 |
| GET | `/v1/audio/voices` | 音色列表，含每个音色的 `ready` 状态 |
| POST | `/v1/audio/speech` | OpenAI 兼容合成，返回音频流 |
| GET | `/say?text=..&voice=..` | 浏览器直接听 |
| GET | `/` | 试听页 |

**状态与控制**（面板用的就是这几个）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/mode` | 开关、音色、引擎、音量、语速、三件套端口 |
| POST | `/v1/mode` | 改上面任意一项，字段按需给 |
| GET | `/v1/stack` | 组件安装位置 + 运行状态 |
| POST | `/v1/stack` | `{component:"gsv"\|"rvc", path:"..."}`，`path` 留空恢复默认 |
| GET | `/v1/services` | 三个服务的 installed / running / port / reason |
| POST | `/v1/service` | `{name, action:"start"\|"stop"\|"restart"}` |
| POST | `/v1/rescan` | 重扫 `models/`，并把新发现的音色并进来 |
| POST | `/v1/restart` | 重启本服务（保持原端口） |

所有端口只在 `config.json` 定义一次，上面这些接口返回的也是同一份值。

```bash
curl -X POST http://127.0.0.1:8787/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input":"你好","voice":"xiaoxiao"}' -o out.mp3
```

请求体字段：`input`（或 `text`）、`voice`、`speed`、`response_format`、`model`。
超过 `max_chars` 会被拒绝，避免一次塞一本书进来。

## 带自己的音色

模型不进仓库。把目录丢进 `models/` 就行，服务启动自动扫描：

```
models/gsv/myvoice/      ref.wav + prompt.txt        → 零样本克隆，不用训练
models/gsv/trained/      + *.ckpt + *.pth            → 微调权重
models/rvc/mymodel/      *.pth + *.index             → RVC 音色转换
```

想临时改音色显示名、语言、底嗓，在同目录放一个 `voice.json`。
完整约定见 [models/README.md](models/README.md)。

也可以用配置文件的老办法：在 `config.json` 的 `voices` 里手写一条，显式配置优先于扫描结果。

## 接 GPT-SoVITS / RVC

**GPT-SoVITS**：自己装一份（[官方仓库](https://github.com/RVC-Boss/GPT-SoVITS)，MIT），
用 `api.py` 起 HTTP 服务，然后把它填进 `config.json`：

```json
"gsv": { "base_url": "http://127.0.0.1:9880" }
```

VoxRelay 不打包 GSV —— 它带 3G 底模和一套 CUDA 环境，捆进来只会让仓库变成 20G。

GSV 装在哪个盘都行，让 VoxRelay 知道路径即可：面板里点「设置安装位置」，
或者写 `stack.json`：`{"gsv": "D:\\GPT-SoVITS"}`。指向的目录里要有 `run_api.py`。

**RVC**：`rvc.enabled = true`，再起一个 worker 指向 `rvc.worker`。
worker 需要 torch，属于可选组件。

## MCP 接入

`mcp_server.py` 把服务包成 MCP 工具（`speak` / `set_voice_mode` / `list_voices`），
stdio 传输、零依赖，DSH 和 WorkBuddy 都能直接挂：

```json
{ "command": "python", "args": ["mcp_server.py"] }
```

端口自动从 `config.json` 读，不用在 MCP 配置里写地址。
只有当服务不在本机、或者你想故意指到别处时，才需要设 `VOICEHUB_BASE`。

## 目录

```
server.py            HTTP 服务 + 试听页
voice_engines.py     引擎层（edge / sapi / upstream / rvc）
voice_models.py      models/ 自动发现
voice_services.py    三个服务的启停、自愈、端口解析
mcp_server.py        MCP 封装
smoke.py             冒烟测试（另起临时实例自检）
TROUBLESHOOT.md      按症状查的排查手册
config.example.json  默认配置（缺 config.json 时自动用它）
start.bat stop.bat   Windows 启停脚本
models/              你自己的音色模型放这里
```

## 已知限制

- **Windows 优先**。SAPI 引擎和 `start.bat` 是 Windows 的，其他平台只剩 edge-tts 一档。
- 缓存按 `sha1(音色|语速|文本)` 存 `out/cache/`，超 `cache.max_mb` 自动清。
- edge-tts 是微软的在线服务，需要联网；断网时自动退到 SAPI。

## 许可

代码 MIT。仓库不含音色模型和训练权重；GPT-SoVITS、RVC、edge-tts 各自遵循其原有许可证。
用谁的音色、发给谁，法律责任在你自己这边。
