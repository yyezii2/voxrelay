# 装不上 / 用不了 怎么办

按**症状**查。每条都给确认命令，先确认再动手，别猜。

---

## 0. 先跑一次自检

```bash
python smoke.py
```

它会另起一个临时实例（不动你正在跑的那个），把「服务起得来、音色在、能合成出声音」跑一遍，
逐项打印 PASS / FAIL，失败时把服务日志一起贴出来。

**这一步能定位 80% 的问题。** 下面的章节是用来解释 FAIL 的。

---

## 1. 双击 start.bat，黑窗口一闪就没了

**原因**：`python` 不在 PATH，或者脚本启动时抛异常，窗口来不及显示就关了。

**确认**：在这个目录里开一个 PowerShell，直接跑

```powershell
python server.py
```

- 提示「不是内部或外部命令」→ Python 没装好或没勾 Add to PATH
- 打印了一堆 Traceback → 看最后一行的错误信息
- 正常打印「VoxRelay 1.0.0 启动 http://...」→ 那就是 bat 里找解释器的问题，跑 `install.ps1` 重建 `.venv`

---

## 2. 浏览器打不开 / 拒绝连接

**确认**：服务窗口日志的第一行写着真实地址，比如

```
VoxRelay 1.0.0 启动  http://127.0.0.1:8787
```

**端口不一定是 8787**——它取自 `config.json` 的 `port`。先看日志，再照那个地址打开。

日志里没有这一行，说明服务根本没起来，回到第 1 条。

---

## 3. 启动报 `[WinError 10048]` / 端口被占

**确认**：谁占了这个端口

```cmd
netstat -ano | findstr :8787
tasklist /FI "PID eq 12345"
```

**解决**：改 `config.json` 的 `port` 换个值就行。

```json
"port": 9001
```

改完**不用动别的地方**：面板、MCP、启动脚本都从这一处读端口，下次启动自动跟随。
（旧版本需要同步改好几个文件，现在不需要了。）

---

## 4. 能打开页面，但点「合成并播放」没声音 / 报错

**确认**：

```bash
python smoke.py
```

看 `/v1/audio/speech 能出声` 这一项。

- 显示「跳过（没有可用引擎）」→ `edge-tts` 没装，而且系统 SAPI 也不可用
- 显示 `FAIL` 并带错误信息 → 按错误处理，常见的是网络问题

**分引擎判断**：

| 情况 | 现象 | 处理 |
|---|---|---|
| 断网 / 微软服务不可用 | edge-tts 报超时 | 会自动退到 SAPI 系统音色，日志里有「降级」 |
| `edge-tts` 没装 | 日志「edge-tts 未安装」 | `pip install edge-tts` |
| SAPI 也失败 | 两个引擎都不可用 | 检查 Windows 语音包是否安装 |

**都失败的话**，服务不会崩，只会返回错误 JSON —— 用 `/health` 里的 `engines` 看哪一档还活着。

---

## 5. 音色列表是空的，或者我自己的音色不出现

**目录结构必须是这个形状**（`voice.json` 可选）：

```
models/gsv/<你的音色名>/ref.wav + prompt.txt     零样本克隆
models/gsv/<你的音色名>/*.ckpt + *.pth           微调权重
models/rvc/<你的音色名>/*.pth + *.index          RVC 转换
```

**确认**：

```bash
curl -X POST http://127.0.0.1:8787/v1/rescan
```

返回里的 `total` 和 `discovered` 分别是总音色数和这次新发现的个数。

- `total` 有数但你的音色不在列表里 → 目录层级错了（多套了一层，或者文件名不对）
- `total` 是 0 → `config.json` 的 `voices` 被改坏了，或者 `models/` 被挪走了

面板上有「重新扫描」按钮，等价于上面这条命令。

---

## 6. 角色音色用不了 / GSV 起不来

**确认** GSV 到底有没有在跑：

```bash
curl http://127.0.0.1:9880/docs
```

- 返回 HTML（一串 Swagger 页面）→ GSV 在跑
- `拒绝连接` → GSV 没起来

**确认** VoxRelay 连的是不是同一个地址：`config.json`

```json
"engines": { "gsv": { "base_url": "http://127.0.0.1:9880" } }
```

两边的端口必须一致。**改端口要两边一起改**：GSV 用 `-p` 启动参数，VoxRelay 改 `base_url`。

如果 GSV 装在别的地方（不在 `gsv/` 目录），在面板里点「设置安装位置」指过去，
或者写 `stack.json`：

```json
{ "gsv": "D:\\GPT-SoVITS" }
```

指向的目录里要有 `run_api.py`，最好还有自带的 `runtime/python.exe`。

---

## 7. RVC 不生效

三个条件缺一不可：

1. `config.json` 里 `rvc.enabled` 是 `true`
2. worker 在跑：`curl http://127.0.0.1:8790/health`
3. 模型在 `models/rvc/<名字>/` 下

worker 需要 torch（`rvc/py310/python.exe`），没装的话面板会显示「未安装」——这是正常的，RVC 本来就是可选件。

---

## 8. AI 不念了 / MCP 连不上

**确认** MCP 能不能单独跑起来：

```bash
python mcp_server.py
```

正常表现是**没有输出、一直挂着**（它是 stdio 服务，等 JSON-RPC 输入）。有 Traceback 就是问题。

**确认** 它连的地址：

MCP 默认读 `config.json` 的 `port`。如果你手动设了环境变量 `VOICEHUB_BASE`，它会以那个为准，
两边不一致就会出现「面板正常、AI 不念」。

**确认** 服务真的在跑（第 2 条）。

MCP 是启动时加载的，**改完配置要重启一次客户端**才会生效。

---

## 9. 侧边栏面板连不上服务

面板会自动问宿主「服务在哪个端口」，正常情况下不需要管。

连不上时面板会弹出一个地址输入框：

1. 先看服务窗口日志里的地址
2. 填进去，点「连接」（只填一次，之后会记住）
3. 想交还给自动发现，点「自动检测」

常见原因：服务没启动、端口改过但服务没重启、`config.json` 和实际监听不一致。

---

## 10. 侧边栏里根本找不到面板

**先重启一次 DSH**。面板和 MCP 都是启动时加载的。

重启后还是没有，先让脚本告诉你挂没挂上：

```bash
python dsh_link.py status
```

输出里四行都要对：

```
profile      C:\Users\你\.dsh\profiles\desktop
依赖         link:D:/你的路径/voxrelay
bundles      有
node_modules 链接 -> D:\你的路径\voxrelay
```

哪一行不对就重挂一次（改之前会自动备份清单）：

```bash
python dsh_link.py register
```

还是不行，把插件宿主侧退回空实现，确认是不是插件本身的代码问题：

```bash
echo 'export function apply() {}' > index.js
```

重启后面板回来了，说明是宿主侧注册路由那段的问题（它只负责"自动发现端口"，
退掉之后面板依然能用，只是要手填地址）。别忘了把 `index.js` 从 git 恢复回来。

---

## 11. 面板出现两个，或者名字还是旧的

旧版面板叫 `@local/voicehub-panel`，新版叫 `voxrelay`。升级时如果两套都在 profile 里，
就会挂两个面板。

```bash
python dsh_link.py status   # 看现在挂的是哪个
python dsh_link.py register # 会清掉旧依赖、把旧插入项就地换成 voxrelay
```

然后重启 DSH。旧面板的目录（比如 `default-workspace\voicehub-panel`）可以删了，
但**要先确认 profile 里已经没有 `@local/voicehub-panel`**——删早了面板会整个消失。

---

## 还是不行

带上三样东西去看日志，基本能自己定位：

| 看什么 | 在哪 |
|---|---|
| 启动和合成日志 | 服务窗口，或 `logs/server.log` |
| 自检结果 | `python smoke.py` 的输出 |
| 引擎状态 | `curl http://127.0.0.1:<端口>/health` |

`/health` 的 `engines` 字段一眼就能看出哪一档引擎是活的：

```json
{"edge": true, "sapi": true, "upstream": false, "gsv": false, "rvc": false}
```
