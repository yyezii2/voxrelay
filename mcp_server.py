# -*- coding: utf-8 -*-
"""VoxRelay MCP 服务（stdio，零依赖）。

把 VoxRelay 的语音合成包成 MCP 工具，DSH 和 WorkBuddy 共用。
协议：JSON-RPC 2.0，按行分隔（MCP stdio 传输）。

工具：
  speak           合成并播放一段语音（受语音模式开关控制）
  set_voice_mode  开关语音模式 / 切换音色
  get_voice_mode  查看当前状态
  list_voices     列出可用音色
"""
import array
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import voice_services as VS  # noqa: E402

# 端口只在 config.json 定义一次。这里刻意不缓存成常量：
# MCP 是常驻进程，用户改完端口不该为了让它生效而重启整个客户端。
# 每次请求现读，代价只是一次几 KB 的 JSON 解析。
STATE = os.environ.get("VOICEHUB_STATE", os.path.join(ROOT, "voice_mode.json"))
PROTOCOL = "2024-11-05"
DEFAULT_VOICE = "xiaoxiao"

CREATE_NO_WINDOW = 0x08000000
# 脱离父进程：否则父进程（MCP 服务）一退出，拉起的服务会被一起干掉
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# 启动顺序：GSV 最慢先起，VoxRelay 在中，RVC 最后。
# optional=True 的服务缺文件就跳过，不影响主服务起来。
# 清单和端口都由 voice_services 统一提供，避免三处各写一遍慢慢漂移。
SERVICES = VS.SERVICES


def _load_config():
    """完整配置。每次现读——改了端口或音色，不用重启这个常驻进程。"""
    for path in (os.environ.get("VOICEHUB_CONFIG"),
                 os.path.join(ROOT, "config.json"),
                 os.path.join(ROOT, "config.example.json")):
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001
            continue
    return None


def _base():
    """服务地址：VOICEHUB_BASE 覆盖 > config.json 的 port > 8787。"""
    env = os.environ.get("VOICEHUB_BASE")
    if env:
        return env.rstrip("/")
    cfg = _load_config() or {}
    try:
        port = int(cfg.get("port") or 8787)
    except (TypeError, ValueError):
        port = 8787
    return "http://127.0.0.1:%d" % port

# 缺 GSV 时回给模型看的提示。写成「用户可以直接说这句话」的形式，
# 让 AI 顺着往下帮他装，而不是让用户自己去啃文档。
GSV_HINT = (
    "GSV（GPT-SoVITS）没装，角色音色用不了，edge/sapi 基础音色正常。"
    "想用角色音色的话：下 GPT-SoVITS 官方整合包解压，用 "
    "`api.py -a 127.0.0.1 -p 9880` 起服务，再把 config.json 里 "
    "engines.gsv.base_url 填成 http://127.0.0.1:9880。"
    "自己装要 Python 3.10 + 约 8G 的 torch 和底模，比较费时——"
    "需要 AI 帮忙装的话，直接跟它说一声就行。"
)

INSTRUCTIONS_PANEL = (
    "本机语音约定（由 voicehub 服务下发）：\n"
    "- 当前客户端有 VoxRelay 面板，它会在每次回复结束后自动朗读，"
    "**不要主动调用 speak**，否则同一段会念两遍。\n"
    "- 只有用户明确要求「念一下这段 / 读出来」时才调用 speak。\n"
    "- 开关与音色由面板和服务端统一管理。"
)

INSTRUCTIONS_MCP = (
    "本机语音约定（由 voicehub 服务下发，不需要在 agent 规则文件里重复）：\n"
    "- 每次回复完成后调用 speak 朗读正文要点，不超过 300 字；不念代码块、表格、路径、长数字。\n"
    "- 语音模式关闭时 speak 静默返回，所以可以放心每次都调用。\n"
    "- 固定口令：打开mod1 / 关闭mod1 / 别念了 / 换声音。\n"
    "- 开关与音色由服务端统一管理，面板和 MCP 改的是同一份 voice_mode.json。"
)


def _instructions_for(params):
    """有侧边栏面板的客户端（DSH）让面板自己念，别让模型再调一次。

    其他客户端（WorkBuddy 等）没有面板，只能靠模型每轮调 speak。
    判据是 MCP initialize 里的 clientInfo.name。
    """
    info = (params or {}).get("clientInfo") or {}
    name = str(info.get("name") or "").lower()
    if any(k in name for k in ("dsh", "deepseek", "harness")):
        return INSTRUCTIONS_PANEL
    return INSTRUCTIONS_MCP


def _note_client(params):
    """记下谁在连，方便确认 clientInfo 名字有没有变。"""
    try:
        info = (params or {}).get("clientInfo") or {}
        folder = os.path.join(ROOT, "logs")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "mcp-clients.log"), "a", encoding="utf-8") as fh:
            fh.write("%s\t%s\t%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"),
                info.get("name") or "?", info.get("version") or "?"))
    except Exception:  # noqa: BLE001
        pass

TOOLS = [
    {
        "name": "speak",
        "description": (
            "用本机 VoxRelay 合成语音并播放。"
            "默认行为：每次回复完成后，调用本工具朗读这次的回复正文"
            "（较长时朗读要点即可，不超过 300 字）。"
            "语音模式关闭时本工具静默返回、不会出声，所以可以放心每次都调用。"
            "用户说“别念了/关掉语音”时改用 set_voice_mode 关闭。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要朗读的文本，建议不超过 300 字"},
                "voice": {"type": "string", "description": "音色 id，如 xiaoxiao；不填用当前设置的音色"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "set_voice_mode",
        "description": (
            "开关语音模式、切换音色/模式，并管理服务进程。"
            "用户说“打开语音”→ enabled=true，**会同时拉起语音服务（首次约 1 分钟）**；"
            "“关闭语音”→ enabled=false，**会停掉所有语音服务、释放显存**。"
            "**换声音**：“把语音换成 XXX”→ enabled=true, voice=\"XXX\"（会记住，下次打开就是它）；"
            "“换成 XXX”→ 只传 voice（临时换）。名字可以只写一部分，服务端会模糊匹配。"
            "mode 是预设的音色组名，不填就沿用当前设置。"
            "其他：“开启语音” → enabled=true；“别念了/静音” → enabled=false, stop_services=false。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "description": "true 开启自动朗读，false 关闭"},
                "mode": {"type": "string", "description": "模式名，如 语音；指定后自动套用该模式的音色"},
                "voice": {"type": "string", "description": "音色 id，直接指定时优先于 mode"},
                "stop_services": {
                    "type": "boolean",
                    "description": "关闭时是否停掉服务，默认 true；只想临时静音就传 false",
                },
                "volume": {
                    "type": "number",
                    "description": "朗读音量 0.1–1.0，默认 0.7",
                },
            },
        },
    },
    {
        "name": "get_voice_mode",
        "description": "查看语音模式当前是开还是关、用的哪个音色。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_voices",
        "description": "列出 VoxRelay 当前可用的所有音色 id 和名称。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _load_state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            s = json.load(fh)
    except Exception:  # noqa: BLE001
        s = {}
    s.setdefault("enabled", True)
    s.setdefault("voice", DEFAULT_VOICE)
    s.setdefault("volume", 0.7)
    s.setdefault("modes", {})
    return s


def _save_state(s):
    folder = os.path.dirname(STATE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as fh:
        # ensure_ascii=True：文件保持纯 ASCII，PowerShell 5.1 用 GBK 读也不会乱码
        json.dump(s, fh, ensure_ascii=True, indent=2)


def _post(path, payload, timeout=180):
    req = urllib.request.Request(
        _base() + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-VoiceHub-Auto": "1"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get("Content-Type") or "", dict(resp.headers)


def _write_volume(value):
    """音量写进 VoxRelay 的 volume.json，那边每次请求都读，改完立刻生效。"""
    path = os.environ.get("VOICEHUB_VOLUME", os.path.join(ROOT, "volume.json"))
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"volume": round(float(value), 3)}, fh)
    except Exception:  # noqa: BLE001
        pass


def _read_volume():
    path = os.environ.get("VOICEHUB_VOLUME", os.path.join(ROOT, "volume.json"))
    try:
        with open(path, encoding="utf-8") as fh:
            return float(json.load(fh).get("volume", 1.0))
    except Exception:  # noqa: BLE001
        return 1.0


_SENT_END = re.compile(r"(?<=[。！？；!?;…])|\n+")


def _split_sentences(text, min_len=14, max_len=180):
    """按句末标点切句。

    - 太短的片段并到前一句，避免「好的。」单独发一次请求；
    - 太长的在逗号处再切，避免单句合成太久。
    """
    parts = [p.strip() for p in _SENT_END.split(text) if p and p.strip()]
    merged, buf = [], ""
    for part in parts:
        buf += part
        if len(buf) >= min_len:
            merged.append(buf)
            buf = ""
    if buf:
        if merged and len(buf) < min_len:
            merged[-1] += buf
        else:
            merged.append(buf)

    out = []
    for sent in merged:
        while len(sent) > max_len:
            cut = sent.rfind("，", 0, max_len)
            if cut < max_len // 2:
                cut = max_len
            out.append(sent[:cut])
            sent = sent[cut:]
        if sent:
            out.append(sent)
    return out or [text]


def _synth(voice, sentence):
    """合成一句并落成临时文件，返回 (path, ext, engine)。"""
    audio, ctype, headers = _post("/v1/audio/speech", {"input": sentence, "voice": voice})
    ext = "wav" if "wav" in ctype else "mp3"
    fd, path = tempfile.mkstemp(prefix="voxrelay_", suffix="." + ext)
    with os.fdopen(fd, "wb") as fh:
        fh.write(audio)
    return path, ext, headers.get("X-VoxRelay-Engine") or "?"


def speak(text, voice=None):
    st = _load_state()
    if not st["enabled"]:
        return "语音模式已关闭，这段话没有朗读。需要开就说“打开语音”。"

    want = voice or st["voice"]
    vid = _resolve_voice(want)
    if not vid:
        return "音色「%s」不存在，这段没念。说“把语音换成 XXX”重新绑一个。" % want

    sentences = _split_sentences(text)

    if len(sentences) <= 1:
        path, ext, engine = _synth(vid, text)
        try:
            import winsound

            winsound.PlaySound(path, winsound.SND_FILENAME)
        except Exception as exc:  # noqa: BLE001
            return "已合成但播放失败：%s（文件 %s）" % (exc, path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return "已朗读 %d 字（音色 %s，引擎 %s）" % (len(text), vid, engine)

    # 句子级流水线：合成线程按顺序预取，主线程同步播放。
    # 播第 N 句时第 N+1 句已经在合成，首声延迟从「整段」降到「第一句」。
    import queue
    import threading
    import winsound

    q = queue.Queue(maxsize=2)
    errors = []

    def produce():
        try:
            for sent in sentences:
                try:
                    q.put(_synth(vid, sent))
                except Exception as exc:  # noqa: BLE001
                    errors.append("第 %d 句合成失败：%s" % (len(errors) + 1, exc))
                    return
        finally:
            q.put(None)

    threading.Thread(target=produce, daemon=True).start()

    played, engine = 0, "?"
    while True:
        item = q.get()
        if item is None:
            break
        path, ext, engine = item
        try:
            winsound.PlaySound(path, winsound.SND_FILENAME)
            played += 1
        except Exception as exc:  # noqa: BLE001
            errors.append("播放失败：%s" % exc)
            break
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    while True:
        try:
            leftover = q.get_nowait()
        except queue.Empty:
            break
        if leftover:
            try:
                os.remove(leftover[0])
            except OSError:
                pass

    if errors:
        return "念了 %d/%d 句后出错：%s" % (played, len(sentences), errors[0])
    return "已朗读 %d 字（%d 句，音色 %s，引擎 %s）" % (len(text), played, vid, engine)


def _port_open(port, timeout=1.0):
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


# 只有显式设置 VOICEHUB_TASK 才会去用计划任务托管服务；
# 公开版默认留空，完全不碰 schtasks。
TASK_NAME = os.environ.get("VOICEHUB_TASK", "")


def _task_exists():
    """先查任务在不在，不然每次调用都白等一次 schtasks 报错。"""
    if not TASK_NAME:
        return False
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME],
                           capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _run_task():
    """让计划任务拉起服务——由任务计划程序托管，不随调用方会话结束而死。"""
    if not _task_exists():
        return False
    try:
        r = subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _service_ready(svc):
    """(能不能起, 缺什么)。解释器、入口脚本、工作目录任一不存在就起不来。

    optional 的服务缺文件时报「未安装」——用户看得懂；
    必装服务才报技术细节，那是给排查用的。
    """
    # 交给 voice_services：它会考虑用户自选的安装目录（stack.json）和换过的端口，
    # 否则「装了但装在 D:\gsv」会被判成未安装、直接跳过。
    return VS.ready(svc)


def start_services(wait=180):
    """拉起服务并等端口就绪，返回 (已启动, 失败, 跳过)。

    缺失的 optional 服务跳过即可，不再中断整轮启动——
    以前 GSV 的解释器不存在时 Popen 抛异常直接 return，
    连排在后面的 VoxRelay 都没起来，用户看到的就是「一片都没起来」。
    """
    ready, skipped = [], []
    for svc in SERVICES:
        ok, why = _service_ready(svc)
        if ok:
            ready.append(svc)
        else:
            skipped.append((svc["name"], why))

    if ready and all(_port_open(s["port"]) for s in ready):
        return [], [], skipped

    started, failed = [], []
    if ready and _run_task():
        started.append("计划任务")
    else:
        for svc in ready:
            if _port_open(svc["port"]):
                continue
            env = dict(os.environ)
            if svc.get("extra_path"):
                env["PATH"] = svc["extra_path"] + os.pathsep + env.get("PATH", "")
            # 走 voice_services 的解析：用户给 GSV/RVC 指定过目录、或换过端口，
            # 这里拉起的才是同一份配置
            cwd, _ = VS.resolve(svc)
            try:
                subprocess.Popen(
                    VS.command(svc), cwd=cwd, env=env,
                    creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                    close_fds=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                started.append(svc["name"])
            except Exception as exc:  # noqa: BLE001
                failed.append("%s(%s)" % (svc["name"], exc))

    deadline = time.time() + wait
    while time.time() < deadline:
        if all(_port_open(s["port"]) for s in ready):
            return started, [], skipped
        time.sleep(2)
    failed += [s["name"] for s in ready if not _port_open(s["port"])]
    return started, failed, skipped


def _pids_on(port):
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             timeout=15).stdout.decode("gbk", "replace")
    except Exception:  # noqa: BLE001
        return set()
    pids = set()
    needle = ":%d " % port
    for line in out.splitlines():
        if needle in line and "LISTENING" in line:
            parts = line.split()
            if parts and parts[-1].isdigit():
                pids.add(parts[-1])
    return pids


def stop_services():
    killed = []
    for svc in SERVICES:
        for pid in _pids_on(svc["port"]):
            try:
                subprocess.run(["taskkill", "/F", "/PID", pid],
                               capture_output=True, timeout=20)
                killed.append(svc["name"])
            except Exception:  # noqa: BLE001
                pass
    return killed


def _load_voices():
    """音色表。config.json 优先，没有就用 config.example.json（和 server.py 一致）。

    读不到任何配置时返回 None，和「配置存在但音色为空」区分开。
    """
    cfg = _load_config()
    return None if cfg is None else (cfg.get("voices") or {})


def _resolve_voice(name):
    """把角色名/别名解析成音色 id。精确 id → 别名 → 名字包含匹配；都没中返回 None。

    2026-11-19：以前没匹配上会把原字符串原样返回，set_voice_mode 就直接存进状态，
    于是 语音 被绑成「长期素食」这种不存在的音色，之后每次朗读都 400，看着像服务炸了。
    """
    if not name:
        return None
    voices = _load_voices()
    if voices is None:
        return name  # 配置读不到就别拦，交给下游去报 400
    if name in voices:
        return name
    key = name.strip().lower()
    for vid, v in voices.items():
        if key == vid.lower():
            return vid
    # 显式别名：config.json 里每个音色可以加 "aliases": ["长期素食", "素世"]
    for vid, v in voices.items():
        for alias in (v.get("aliases") or []):
            if key == str(alias).strip().lower():
                return vid
    hits = [vid for vid, v in voices.items() if key in (v.get("name") or "").lower()]
    if len(hits) == 1:
        return hits[0]
    if hits:
        # 多个命中时优先 gsV 引擎、名字最短的
        hits.sort(key=lambda vid: (voices[vid].get("engine") != "gsv", len(voices[vid].get("name") or "")))
        return hits[0]
    return None


def _near_voices(name, limit=3):
    """没匹配上时给几个最接近的候选，让用户直接挑一个。"""
    voices = _load_voices()
    if voices is None:
        return []
    import difflib

    pool = {}
    for vid, v in voices.items():
        pool[v.get("name") or vid] = vid
        for alias in (v.get("aliases") or []):
            pool[str(alias)] = vid
    try:
        cand = difflib.get_close_matches(name.strip().lower(), list(pool), n=limit * 2, cutoff=0.3)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for label in cand:
        vid = pool[label]
        if vid not in [x[0] for x in out]:
            out.append((vid, label))
    return out[:limit]


def _voice_label(vid):
    voices = _load_voices() or {}
    return (voices.get(vid) or {}).get("name") or vid


def set_voice_mode(enabled=None, voice=None, mode=None, stop_services_flag=True,
                   volume=None):
    st = _load_state()
    if enabled is not None:
        st["enabled"] = bool(enabled)
    vid = None
    if voice:
        vid = _resolve_voice(voice)
        if not vid:
            near = _near_voices(voice)
            hint = "、".join("%s(%s)" % (label, v) for v, label in near)
            return "没有这个音色：%s。%s" % (
                voice, ("最接近的是 " + hint) if hint else "先 list_voices 看看有哪些。")
    if mode:
        m = st["modes"].get(mode)
        if not m:
            return "没有这个模式：%s（已有：%s）" % (mode, "、".join(st["modes"]))
        st["mode"] = mode
        if vid:
            # 指定了音色 → 绑定到这个模式，以后"打开 X"就是这个声音
            m["voice"] = vid
            m["name"] = _voice_label(vid)
            st["voice"] = vid
        else:
            st["voice"] = m.get("voice", st["voice"])
    elif vid:
        st["voice"] = vid
    if volume is not None:
        st["volume"] = max(0.05, min(1.0, float(volume)))
        _write_volume(st["volume"])
    _save_state(st)

    note = ""
    if enabled is True:
        started, failed, skipped = start_services()
        if failed:
            note = "；但 %s 没起来，语音可能失败" % "、".join(failed)
        elif started:
            note = "；已拉起 %s（约 20–80 秒，首句还要等模型加载）" % "、".join(started)
        else:
            note = "；服务本来就在跑"
        if skipped:
            if all(w == "未安装" for _, w in skipped):
                note += "。%s 未安装" % "、".join(n for n, _ in skipped)
            else:
                note += "。跳过 %s" % "、".join("%s(%s)" % (n, w) for n, w in skipped)
            if any(n == "GSV" for n, _ in skipped):
                note += "。" + GSV_HINT
    elif enabled is False:
        if stop_services_flag:
            killed = stop_services()
            note = "；已停止 %s，显存已释放" % "、".join(sorted(set(killed))) if killed else "；服务本来就没跑"
        else:
            note = "；仅静音，服务保持运行"

    return "语音模式：%s，音色：%s（%s），音量：%.2f%s%s" % (
        "开启" if st["enabled"] else "关闭", _voice_label(st["voice"]), st["voice"],
        _read_volume(), "，模式 %s" % st["mode"] if st.get("mode") else "", note)


def get_voice_mode():
    st = _load_state()
    return "语音模式：%s，音色：%s（%s），音量：%.2f%s" % (
        "开启" if st["enabled"] else "关闭", _voice_label(st["voice"]), st["voice"],
        _read_volume(), "，模式 %s" % st["mode"] if st.get("mode") else "")


def list_voices():
    with urllib.request.urlopen(_base() + "/v1/audio/voices", timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    rows = ["%s（%s，%s）" % (v.get("id"), v.get("name"), v.get("engine"))
            for v in data.get("voices", [])]
    return "\n".join(rows) or "没有可用音色"


def call_tool(name, args):
    if name == "speak":
        text = (args.get("text") or "").strip()
        if not text:
            return "text 不能为空", True
        return speak(text, args.get("voice")), False
    if name == "set_voice_mode":
        return set_voice_mode(args.get("enabled"), args.get("voice"), args.get("mode"),
                              args.get("stop_services", True), args.get("volume")), False
    if name == "get_voice_mode":
        return get_voice_mode(), False
    if name == "list_voices":
        return list_voices(), False
    return "未知工具：%s" % name, True


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        params = msg.get("params") or {}
        _note_client(params)
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "voicehub", "version": "0.1.0"},
            # 规则随握手下发：有面板的客户端让面板念，没有的让模型调 speak
            "instructions": _instructions_for(params),
        }}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            text, is_err = call_tool(params.get("name"), params.get("arguments") or {})
        except urllib.error.URLError as exc:
            text = "连不上 VoxRelay（%s），确认 %s 在跑" % (exc, _base())
            is_err = True
        except Exception as exc:  # noqa: BLE001
            text, is_err = "调用失败：%s" % exc, True
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": text}], "isError": is_err}}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": "未实现的方法：%s" % method}}
    return None


def main():
    # MCP 走 UTF-8；Windows 下 Python 默认按 GBK 写管道，中文会变乱码
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    out = sys.stdout
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            out.write(json.dumps(resp, ensure_ascii=False) + "\n")
            out.flush()


if __name__ == "__main__":
    main()
