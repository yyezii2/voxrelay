# VoxRelay

本地语音服务。一个进程、一个端口、一个可选依赖，把 TTS 包成 **OpenAI 兼容接口**，
给任何支持 `/v1/audio/speech` 的客户端用。

```
POST http://127.0.0.1:8787/v1/audio/speech
{"input": "要念的话", "voice": "xiaoxiao", "speed": 1.0}
```

- 纯标准库实现，没有 Web 框架；MCP 和侧边栏面板都是可选的
- 一个仓库就是一个包：`package.json` 管 DSH 插件，`server.py` 管服务，共用同一份 `config.json`
- 引擎可叠：edge-tts（免费在线）→ Windows SAPI（离线兜底）→ 上游服务 → RVC 后处理；
  吃显存的 GSV / RVC / Higgs / CosyVoice 四选一互斥，同一时刻只跑一个
- 任何一个引擎挂掉都会被降级绕过，**保证一定有声音出来**
- 音色由你自己带，仓库不含任何模型（见 [models/README.md](models/README.md)）

## 快速开始

需要 Python 3.10+（Windows）。

```powershell
git clone https://github.com/nico2905120737-lab/voxrelay.git VoxRelay
cd VoxRelay
powershell -ExecutionPolicy Bypass -File install.ps1
start.bat
```

浏览器打开**服务窗口第一行打印的地址**（默认 <http://127.0.0.1:8787>）就是试听页。
端口取自 `config.json` 的 `port`，改了要按新地址打开。

装完直接有 11 个音色（10 个微软在线音色 + 1 个系统 SAPI），够用再往下看。

面板不是必需的。想在 DSH 侧边栏里开关语音，安装时多带一个开关，然后重启 DSH：

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -RegisterDsh
```

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
| POST | `/v1/long-tts` | 长文本合成（按句切段、逐段合成、拼成单个 WAV） |
| GET | `/long` | 长文合成页面 |
| GET | `/long-file?name=..` | 下载已合成的长文 WAV |
| POST | `/v1/voice` | 添加克隆音色（higgs/cosy 零样本，写回 config.json） |
| POST | `/v1/voice/delete` | 删除 `custom-` 前缀的音色 |
| GET | `/say?text=..&voice=..` | 浏览器直接听 |
| GET | `/` | 试听页 |

**状态与控制**（面板用的就是这几个）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/mode` | 开关、音色、音量、语速、当前引擎 + 四个引擎的端口 |
| POST | `/v1/mode` | 改上面任意一项，字段按需给 |
| GET | `/v1/stack` | 组件安装位置 + 运行状态 |
| POST | `/v1/stack` | `{component:"gsv"\|"rvc", path:"..."}`，`path` 留空恢复默认 |
| GET | `/v1/services` | 五个服务的 installed / running / port / reason |
| POST | `/v1/service` | `{name, action:"start"\|"stop"\|"restart"}` |
| POST | `/v1/engine` | `{name:"GSV"\|"RVC"\|"Higgs"\|"CosyVoice"}`，四选一切换（阻塞，GSV 约 40s） |
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

自动朗读请带 `X-VoiceHub-Auto: 1`（等价于请求体 `"auto": true`）：语音模式关闭时服务端
直接回 `204`，不合成、不出声。手动试听不要带，否则开关关着就听不到了。

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

握手（`initialize`）时会按「朗读来源」（`speech_mode`）下发不同的 `instructions`：

- **`panel`（默认）**：面板在回合结束时自己念，`不要主动调用 speak`——模型再调一次就念两遍。
- **`mcp`**：每轮回复后调用 `speak` 朗读要点，面板不自动念。

`speech_mode` 存在 `voice_mode.json` 里，可在面板「朗读来源」处随时切换，或用
`set_speech_mode` 工具切换（口令「让面板念 / 让 MCP 念」）。切完下一轮指令跟着变。

规则跟着服务走，不用在 DSH / WorkBuddy 各自的 agent 规则文件里再抄一遍。

## DSH 侧边栏面板（可选）

面板和 MCP 改的是同一份 `voice_mode.json`：AI 说「打开语音」，面板 4 秒内自己刷成开启；
在面板上关掉，AI 也不再念。两边不会打架。

**开关是服务端总闸，不是提示词约定。** `enabled=false` 时，所有带 `X-VoiceHub-Auto: 1`
（或 `auto:true`）的合成请求都会被直接回 `204`、不合成不出声——包括绕过 MCP 直连 HTTP 的
agent。手动试听和明确调用不带这个头，照常出声。

**自动朗读由前端触发。** 面板在 `conversation.chat.turnTail`（回合尾部）挂了一个朗读组件：
回复一结束，前端自己取正文、合成、播放，不再等模型记得调 `speak`。开关一动立刻生效；
打开历史会话不会重念旧消息；同一段文本在一个会话里只念一次。

**朗读来源可在面板切换**（`panel` 面板自动朗读 / `mcp` 由 AI 调 `speak`），
与 MCP 的 `set_speech_mode` 是同一份状态，谁先改都行、两边自动同步。

老版本 DSH 没有这个插槽时，注册失败会自动跳过，退化成「只靠 MCP 工具」——不会报错，
也不会让面板消失。

**侧边栏只有一个「语音」条目**，打开后面板顶部有「语音 / 长文合成」两个页签可切换。
长文合成走服务端 `/v1/long-tts`：自动按句切段、逐段合成、拼成单个 WAV 下载，
绕开 `/tts` 的 3000 字上限；段间停顿可调。

安装（只是把包登记进 DSH，不会重装服务）：

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -RegisterDsh
```

它只动三样东西，改之前都会备份 profile 清单：

1. 在 `~/.dsh/profiles/desktop/package.json` 的 `dependencies` 里加一行
   `"voxrelay": "link:<本仓库绝对路径>"`，并在 `dsh.profile.bundles` 里加 `voxrelay`
2. 把 `node_modules/voxrelay` 指向本仓库（软链，失败则退回目录联接）
3. 清掉旧版 `@local/voicehub-panel` 的残留依赖和插入项

**然后重启 DSH** —— 宿主只在启动时读 profile。侧边栏出现面板就是成功。
撤掉用 `install.ps1 -UnregisterDsh`，服务本身不受影响。

不想用脚本就手动改：照上面三条来，`bundles` 里加一行 `voxrelay` 即可。
改完可以先用 `python dsh_link.py status` 看挂没挂对：

```
profile      C:\Users\你\.dsh\profiles\desktop
依赖         link:D:/你的路径/voxrelay
bundles      有
node_modules 链接 -> D:\你的路径\voxrelay
结论         已挂上
```

**面板怎么找到服务**：宿主半边（`index.js`）注册了同源接口 `/voicehub/endpoint`，
优先读服务启动时写的 `endpoint.json`（带 pid 校验，服务被强杀留下的死文件不会被采纳），
没有才退回 `config.json` 的 `port`。所以改端口只需重启服务，面板下次轮询自己跟上，
不用改面板配置、也不用重启 DSH。

**服务没起时，面板能自己拉起来**。除了 `/voicehub/endpoint`，宿主还挂了三条控制路由：

| 路由 | 作用 |
|---|---|
| `GET /voicehub/status` | 服务状态 + 语音模式，**不依赖 8787** |
| `POST /voicehub/service` | `{name:"GSV"\|"VoiceHub"\|"RVC"\|"Higgs"\|"CosyVoice", action:"start"\|"stop"\|"restart"}` |
| `POST /voicehub/mode` | `{enabled:true\|false}`，总开关 |

连不上 8787 时面板不会只剩一个"填地址"的空壳，而是显示「服务未运行」：列出各服务状态，
给「启动语音面板」和每个组件的单独启动按钮。

**引擎是四选一的**：GSV / RVC / Higgs / CosyVoice 都吃显存，同一时刻只允许一个在跑。
面板上点「切到这里」会走 `voice_services.switch()`——先停掉其余三个、等端口释放，再启目标。
对引擎点「启动」走的也是 `switch()`，不是简单的 `start()`，所以不会出现两个模型同时抢显存。

因此「打开 mod1」也**只拉起一个引擎**：优先上次用的那个（`services.json` 里 `intent=on`），
其余的在返回里标成「四选一：本次不启动」。已经有引擎在跑时一个都不起——不然一次请求就能把
四个模型全装上，21GB 显存必炸。要换引擎请走面板或 `POST /v1/engine`。

这些路由**不重写启停逻辑**——`start/stop` 转手调 `voice_services`，总开关转手调
`mcp_server.set_voice_mode`，和「打开 mod1 / 关闭 mod1」、计划任务走的是同一套幂等逻辑、
同一份状态（`services.json` 意图 + 端口实际）。组件名和动作都有白名单，宿主不接受任意命令。

启停请求一律「立即返回、后台执行」：`start` 内部最长等 90 秒，同步等会把面板请求挂住，
所以前端靠原有的 4 秒轮询看服务自己起来。

**「关闭全部服务」**（面板底部「维护」区，要点两下确认）会停掉所有引擎进程、关闭语音模式、释放显存。
服务是 `DETACHED` 起的，DSH 关掉之后它们仍在后台跑 —— 要清干净就用这个按钮，不用去命令行。
它走的是 `/voicehub/mode {enabled:false}` → `mcp_server.set_voice_mode` → `stop_services`，
不自己实现停进程的逻辑。「服务未运行」页里也有一个「全部停止」，用来清掉 VoiceHub 掉了、
但 GSV/RVC 还在吃显存的残局。

面板自己的自检（不需要启动 DSH）：

```bash
node panel-selftest.mjs    # 宿主半边：路由、端口发现
node client-selftest.mjs   # 前端：三个插槽是否都注册上（含回合尾部朗读）
```

`panel-selftest.mjs` 检查包名和客户端 id 是否一致、`apply()` 能否注册路由、端点算出来的端口
是否与 `config.json` 对得上——这三条任一不满足，面板就会静默不出现。
`client-selftest.mjs` 用一个假 ctx 走一遍真实加载路径，确认 `main`、`sidebar.panellist`、
`conversation.chat.turnTail` 都注册成功；少一个，对应的功能就静默缺失。

## 目录

```
server.py            HTTP 服务 + 试听页
voice_engines.py     引擎层（edge / sapi / upstream / rvc）
voice_models.py      models/ 自动发现
voice_services.py    五个服务 / 四个引擎的启停、自愈、端口解析
service_lock.py      跨进程启停锁（面板 / MCP / HTTP 串行化，避免抢端口）
mcp_server.py        MCP 封装
smoke.py             冒烟测试（另起临时实例自检）
index.js             面板宿主半边：/voicehub/endpoint，告诉前端服务在哪
client.js            面板前端 + 回合结束自动朗读（手写 React.createElement）
cordis.patch.yml     DSH 插入项：把面板挂进侧边栏
package.json         一个包两个身份：npm 包清单 + DSH 插件声明
dsh_link.py          把本包登记进 DSH profile / 撤销
panel-selftest.mjs   面板自检（假 ctx 走一遍真实加载路径）
client-selftest.mjs  前端自检（确认 main / 侧边栏 / 回合尾部三个插槽都注册上）
TROUBLESHOOT.md      按症状查的排查手册
config.example.json  默认配置（缺 config.json 时自动用它）
start.bat stop.bat   Windows 启停脚本
models/              你自己的音色模型放这里
```

## 已知限制

- **Windows 优先**。SAPI 引擎和 `start.bat` 是 Windows 的，其他平台只剩 edge-tts 一档。
- 缓存按 `sha1(音色|语速|文本)` 存 `out/cache/`：超过 `cache.max_mb`（默认 300MB）或
  超过 `cache.max_age_days`（默认 7 天）自动清。启动时清一次，运行中每小时补一次，
  不需要手动删。
- edge-tts 是微软的在线服务，需要联网；断网时自动退到 SAPI。
- 前端自动朗读依赖 DSH 的 `conversation.chat.turnTail` 插槽；没有这个插槽的客户端
  只能靠模型主动调 `speak`。两种路径都受同一个开关管。
- 自动朗读只念 assistant 正文：去掉代码块、表格、URL，超过 300 字按句截断；
  同一段文本在一个会话里只念一次，打开历史会话不重念。

## 许可

代码 MIT。仓库不含音色模型和训练权重；GPT-SoVITS、RVC、edge-tts 各自遵循其原有许可证。
用谁的音色、发给谁，法律责任在你自己这边。
