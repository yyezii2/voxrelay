# -*- coding: utf-8 -*-
"""VoxRelay —— 本地语音服务（OpenAI 兼容），零外部框架依赖。

启动:  python server.py
接口:
  GET  /health
  GET  /v1/audio/voices
  POST /v1/audio/speech      {model,input|text,voice,speed,response_format}
  GET  /say?text=..&voice=..
  GET  /                     浏览器试听页

  GET  /v1/mode              当前语音状态（含 speed / 三件套 / 模型目录）
  POST /v1/mode              切换开关、音色、引擎、音量、语速
  GET  /v1/stack             组件安装位置 + 运行状态
  POST /v1/stack             {component:gsv|rvc, path:"..."}  设置/清除安装位置
  GET  /v1/services          三个服务的状态
  POST /v1/service           {name, action:start|stop|restart}  单个组件启停
  POST /v1/rescan            重新扫描 models/ 发现新音色
  POST /v1/restart           重启本服务
"""
import io
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import traceback
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(ROOT, "vendor")
if os.path.isdir(VENDOR) and VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import voice_engines as VE  # noqa: E402
import voice_models as VM  # noqa: E402
import voice_services as VS  # noqa: E402

LOG_DIR = os.path.join(ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "server.log")
VERSION = "0.3.0"
START_TS = time.time()

# 实际监听的地址，main() 里填。重启时要用它拉新进程，
# 否则拿 --port 18788 起的实例重启后会退回 config 里的端口。
LISTEN = {"host": "127.0.0.1", "port": 8787}

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8787,
    "max_chars": 3000,
    "default_voice": "xiaoxiao",
    "cache": {"enabled": True, "max_mb": 300},
    "engines": {
        "edge": {"enabled": True},
        "sapi": {"enabled": True},
        "upstream": {"enabled": False, "base_url": "", "api_key": "", "model": "tts", "voice": "default"},
    },
    "rvc": {"enabled": False, "worker": "http://127.0.0.1:8790", "timeout": 180},
    "voices": {},
}


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 5 * 1024 * 1024:
            os.replace(LOG_FILE, LOG_FILE + ".1")
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    # 优先 config.json（用户自己改的），没有就回落到 config.example.json，
    # 保证新克隆下来直接能跑，不需要手动复制。
    user = {}
    for name in ("config.json", "config.example.json"):
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                user = json.load(fh)
            if name != "config.json":
                log("未找到 config.json，使用 %s 作为默认配置" % name)
            break
        except Exception as exc:
            log("!! %s 解析失败，已跳过: %s" % (name, exc))
    return deep_merge(DEFAULTS, user)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG = load_config()
# 记住 config.json 里写死的音色，重新扫描时以它为基础，只重算 models/ 发现的那部分。
BASE_VOICES = dict(CFG.get("voices") or {})
VM.discover(CFG, BASE_DIR, log)
VE.configure(CFG, logger=log)


# ---------------------------------------------------------------- 语音模式状态
# voice_mode.json 是 MCP 工具和侧边栏面板共用的唯一状态来源：
# 谁先改都行，另一边下次读到的就是新值。
STATE_FILE = (os.environ.get("VOXRELAY_STATE") or os.environ.get("VOICEHUB_STATE")
              or os.path.join(BASE_DIR, "voice_mode.json"))


def _voice_engine(vid):
    """把音色归到「gsv」还是「tts」——面板上那两个按钮就是按这个分的。"""
    ent = (CFG.get("voices") or {}).get(vid) or {}
    return "gsv" if ent.get("engine") == "gsv" else "tts"


def _read_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            s = json.load(fh)
    except Exception:  # noqa: BLE001
        s = {}
    if not isinstance(s, dict):
        s = {}
    s.setdefault("enabled", True)
    s.setdefault("voice", CFG.get("default_voice") or "")
    s.setdefault("engineVoices", {})
    s.setdefault("modes", {})
    s.setdefault("engine", _voice_engine(s["voice"]))
    s.setdefault("speed", 1.0)
    # 朗读来源：panel=面板自动朗读（turnTail 插槽），mcp=agent 用 speak 工具念。
    # 两边改的是同一份状态，谁先改都行，另一边下次读到就是新值。
    s.setdefault("speech_mode", "panel")
    return s


def _write_state(s):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(s, fh, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        log("写 voice_mode.json 失败: %s" % exc)


# ---------------------------------------------------------------- 三件套状态
# stack.json 记录用户自选的 GSV / RVC 安装目录；面板据此显示「装了没」。
STACK_FILE = os.path.join(BASE_DIR, "stack.json")

# endpoint.json 记录本进程实际监听的地址。面板的宿主侧优先读它，
# 这样用 --port 覆盖启动时，面板也不会跑去连 config.json 里的旧端口。
ENDPOINT_FILE = os.path.join(BASE_DIR, "endpoint.json")


def _write_endpoint(host, port):
    try:
        with open(ENDPOINT_FILE, "w", encoding="utf-8") as fh:
            json.dump({"host": host, "port": int(port), "pid": os.getpid()}, fh)
    except Exception as exc:  # noqa: BLE001
        log("写 endpoint.json 失败: %s" % exc)


def _clear_endpoint():
    try:
        os.remove(ENDPOINT_FILE)
    except Exception:  # noqa: BLE001
        pass


def _read_stack():
    try:
        with open(STACK_FILE, encoding="utf-8") as fh:
            s = json.load(fh)
        return s if isinstance(s, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_stack(s):
    try:
        with open(STACK_FILE, "w", encoding="utf-8") as fh:
            json.dump(s, fh, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        log("写 stack.json 失败: %s" % exc)


def _apply_stack(body):
    """设置某个组件的安装位置。path 留空 = 清掉自选目录、恢复默认。

    组件名从服务清单来（gsv / rvc / higgs / cosy），加引擎不用改这里。
    """
    comp = (body.get("component") or "").strip().lower()
    if comp not in VS.STACK_COMPONENTS:
        raise ValueError("component 必须是 %s" % " / ".join(VS.STACK_COMPONENTS))
    path = (body.get("path") or "").strip().strip('"')
    s = _read_stack()
    if not path:
        s.pop(comp, None)
    elif not os.path.isdir(path):
        raise ValueError("目录不存在: %s" % path)
    else:
        s[comp] = path
    _write_stack(s)
    return {"locations": _read_stack(), "stack": _stack_status()}


def _apply_engine(body):
    """面板四选一：切到指定引擎，同组其余服务自动停掉。

    阻塞式，GSV 约 40 秒、Higgs/CosyVoice 约 7 秒。
    互斥是硬性的，目标起不来时其他引擎也已经停了，所以失败要把话说清楚。
    """
    name = (body.get("name") or body.get("engine") or "").strip()
    if not name:
        raise ValueError("缺少 name（可选：%s）" % " / ".join(VS.GPU_GROUP))
    res = VS.switch(name)
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "%s 切换失败" % name)
    return {"result": res, "stack": _stack_status()}


def _apply_service(body):
    """面板上单个组件的启动 / 停止 / 重启。本服务自身走 /v1/restart。"""
    name = (body.get("name") or "").strip()
    action = (body.get("action") or "").strip().lower()
    svc = VS.get(name)
    if svc.get("self"):
        raise ValueError("%s 自身请用「重启服务」" % svc["name"])
    if action not in ("start", "stop", "restart"):
        raise ValueError("action 必须是 start / stop / restart")
    if action == "start":
        VS.set_intent(name, True)
        res = VS.start(name)
    elif action == "stop":
        VS.set_intent(name, False)
        res = VS.stop(name)
    else:
        VS.set_intent(name, True)
        res = VS.restart(name)
    return {"result": res, "stack": _stack_status()}


def _heal_loop():
    """每 15 秒检查一次：本来在跑、现在掉了的服务自动拉回来。"""
    state = {}
    while True:
        time.sleep(15)
        try:
            for h in VS.heal_once(state):
                log("[自愈] %s %s" % (h.get("name"), h.get("note") or h.get("error")))
        except Exception as exc:  # noqa: BLE001
            log("[自愈] 异常: %s" % exc)


def _rescan():
    """重新扫描 models/，把发现到的音色并回音色表（config.json 写死的不动）。"""
    CFG["voices"] = dict(BASE_VOICES)
    n = VM.discover(CFG, BASE_DIR, log)
    return {
        "total": len(CFG.get("voices", {})),
        "discovered": n,
        "voices": VE.list_voices(),
    }


# ---------------------------------------------------------------- 音色增删
# 面板里「添加音色」用：不用手改 config.json，提交字段即生成一个 higgs/cosy
# 零样本克隆音色，写回 config.json 并同步到运行时的 CFG / BASE_VOICES。
# 只允许 higgs / cosy 两种引擎（GSV/RVC 要走 models/ 扫描，不在这里建）。

_CUSTOM_PREFIX = "custom-"


def _slug(vid):
    """把音色 id 规整成安全键：只留小写字母/数字/下划线/连字符。

    中文名会整段被剥掉，所以纯中文名用其 md5 前 8 位兜底，保证唯一且稳定。
    """
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (vid or "").strip().lower()).strip("-")
    if not s:
        import hashlib as _hl
        s = _hl.md5((vid or "").encode("utf-8")).hexdigest()[:8]
    return s


def _write_cfg_voices(voices_dict):
    """把 voices 写回 config.json（保留其它字段与结构），失败抛 ValueError。"""
    path = os.path.join(ROOT, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                cfg = json.load(fh)
        except Exception as exc:
            raise ValueError("config.json 读不出来: %s" % exc)
    else:
        cfg = deep_merge(DEFAULTS, CFG)
    cfg["voices"] = voices_dict
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        raise ValueError("写 config.json 失败: %s" % exc)


def _add_voice(body):
    name = (body.get("name") or "").strip()
    engine = (body.get("engine") or "").strip().lower()
    ref_audio = (body.get("ref_audio") or "").strip()
    prompt_text = (body.get("prompt_text") or "").strip()
    instruct = (body.get("instruct") or "").strip()
    desc = (body.get("desc") or "").strip()

    if not name:
        raise ValueError("音色名不能为空")
    if engine not in ("higgs", "cosy"):
        raise ValueError("engine 只能是 higgs 或 cosy（GSV/RVC 走 models/ 扫描）")
    if not ref_audio:
        raise ValueError("参考音频路径 ref_audio 不能为空")
    if not os.path.exists(ref_audio):
        raise ValueError("参考音频不存在: %s" % ref_audio)
    if not prompt_text:
        raise ValueError("参考音台词 prompt_text 不能为空")

    # 生成音色 id：用户给了就用（规整后），否则用 custom-<name 的 slug>
    if body.get("id"):
        vid = _slug(body["id"])
    else:
        vid = _CUSTOM_PREFIX + _slug(name)
    voices = dict(CFG.get("voices") or {})
    if vid in voices:
        raise ValueError("音色 id 已存在: %s" % vid)

    entry = {
        "name": name,
        "engine": engine,
        "ref_audio": ref_audio,
        "prompt_text": prompt_text,
        "language": body.get("language") or "zh-CN",
    }
    if engine == "cosy":
        entry["cosy_mode"] = body.get("cosy_mode") or "instruct2"
        if instruct:
            entry["instruct"] = instruct
    if desc:
        entry["desc"] = desc

    voices[vid] = entry
    _write_cfg_voices(voices)
    # 同步运行时：CFG（合成用）+ BASE_VOICES（rescan 时保留）
    CFG["voices"] = voices
    BASE_VOICES.clear()
    BASE_VOICES.update(voices)
    log("已添加音色 %s (%s)" % (vid, engine))
    return {"ok": True, "id": vid, "voices": VE.list_voices()}


def _delete_voice(body):
    vid = (body.get("id") or "").strip()
    if not vid:
        raise ValueError("缺少 id")
    voices = dict(CFG.get("voices") or {})
    if vid not in voices:
        raise KeyError("没有这个音色: %s" % vid)
    # 只允许删自定义音色，防止误删内置音色
    if not vid.startswith(_CUSTOM_PREFIX):
        raise ValueError("只允许删除自定义音色（id 以 %s 开头）" % _CUSTOM_PREFIX)
    del voices[vid]
    _write_cfg_voices(voices)
    CFG["voices"] = voices
    BASE_VOICES.clear()
    BASE_VOICES.update(voices)
    log("已删除音色 %s" % vid)
    return {"ok": True, "voices": VE.list_voices()}


# ---------------------------------------------------------------- 长文本合成
# 单独一条路径：绕过 /tts 的 max_chars（默认 3000）限制，把长文按标点切段、
# 逐段走 VE.synthesize（含缓存 / 引擎选择 / 失败回退），再用 wave 拼成单个 wav。
# 这样长文能一次出整条音频，而不必让调用方自己分段、自己拼。

LONG_OUT = os.path.join(ROOT, "out", "long")
os.makedirs(LONG_OUT, exist_ok=True)


def _split_long(text, max_chars=40):
    """按句切、长句再按逗号切，保证每段不超过 max_chars。"""
    text = (text or "").strip()
    if not text:
        return []
    sents = [s for s in re.split(r"(?<=[。！？!?；;\n])", text) if s.strip()]
    chunks = []
    for s in sents:
        if len(s) <= max_chars:
            chunks.append(s)
            continue
        for p in re.split(r"(?<=[，,、：:])", s):
            p = p.strip()
            while len(p) > max_chars:
                chunks.append(p[:max_chars])
                p = p[max_chars:]
            if p:
                chunks.append(p)
    return [c for c in chunks if c.strip()]


def _wav_info(data):
    """解析 wav 头部，返回 (audio_format, nchannels, sampwidth, framerate, data_offset, data_size)。

    只处理标准 PCM(1) 和 IEEE float(3)。Python 的 wave 模块不支持 float32
    （Higgs 用 torchaudio.save 默认就是 float32），所以这里手写解析。
    """
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("不是合法 wav")
    audio_format = nchannels = sampwidth = framerate = 0
    data_offset = 0
    data_size = 0
    i = 12
    n = len(data)
    while i + 8 <= n:
        cid = data[i:i + 4]
        sz = struct.unpack("<I", data[i + 4:i + 8])[0]
        body = data[i + 8:i + 8 + sz]
        if cid == b"fmt ":
            audio_format, nchannels, framerate, _br, _ba, bits = \
                struct.unpack("<HHIIHH", body[:16])
            sampwidth = bits // 8
        elif cid == b"data":
            data_offset = i + 8
            data_size = sz
            break
        i += 8 + sz + (sz & 1)  # chunk 按 2 字节对齐
    if not audio_format or not data_offset:
        raise ValueError("wav 缺 fmt/data chunk")
    return audio_format, nchannels, sampwidth, framerate, data_offset, data_size


def _wav_samples(path):
    """把 wav 解码成 int16 采样（float32 和 pcm 都转成 16bit PCM）。"""
    data = open(path, "rb").read()
    fmt, nch, sw, sr, off, size = _wav_info(data)
    raw = data[off:off + size]
    if fmt == 3 and sw == 4:  # float32
        arr = struct.unpack("<%df" % (len(raw) // 4), raw)
        samples = [max(-1.0, min(1.0, x)) for x in arr]
        return nch, sw, sr, samples, True
    if fmt == 1 and sw == 2:  # 16bit PCM
        return nch, sw, sr, None, False  # 直接透传原始字节更快
    if fmt == 1 and sw == 1:  # 8bit PCM（少见）
        raise ValueError("暂不支持 8bit PCM")
    if fmt == 1 and sw == 4:  # 32bit int PCM
        raise ValueError("暂不支持 32bit int PCM")
    raise ValueError("不支持的 wav 格式 fmt=%d width=%d" % (fmt, sw))


def _concat_wavs(paths, out_path):
    """把多个 wav 拼成一个 16bit PCM wav。

    输入可能是 float32（Higgs）或 16bit PCM（CosyVoice/GSV），统一转成
    16bit PCM 输出，保证 wave 模块、浏览器 <audio> 都能直接放。
    """
    out_nch = out_sw = out_sr = None
    out_frames = []
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        nch, sw, sr, samples, is_float = _wav_samples(p)
        if out_nch is None:
            out_nch, out_sw, out_sr = nch, 2, sr
        elif (nch, sr) != (out_nch, out_sr):
            raise RuntimeError("音频参数不一致，无法拼接")
        if is_float:
            raw = b"".join(struct.pack("<h", int(round(x * 32767))) for x in samples)
        else:
            raw = open(p, "rb").read()
            _fmt, _nch, _sw, _sr, off, size = _wav_info(raw)
            raw = raw[off:off + size]
        out_frames.append(raw)
    if out_nch is None:
        raise RuntimeError("没有可拼接的音频")

    byte_rate = out_sr * out_nch * out_sw
    block_align = out_nch * out_sw
    total = sum(len(f) for f in out_frames)
    header = struct.pack("<4sI4s", b"RIFF", 36 + total, b"WAVE")
    header += struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, out_nch, out_sr,
                          byte_rate, block_align, out_sw * 8)
    header += struct.pack("<4sI", b"data", total)
    with open(out_path, "wb") as fh:
        fh.write(header)
        for fr in out_frames:
            fh.write(fr)


def _long_tts(body):
    """POST /v1/long-tts：分段合成 + 拼接，返回 {path, seconds, chunks, engine}。

    body: {input|text, voice, speed, gap_ms, instruct}
    每段之间加 gap_ms 静音（默认 200ms），让长文听起来有断句。
    instruct 非空时覆盖 cosy 音色的语气指令（长文合成页的语气词辅助）。
    """
    text = (body.get("input") or body.get("text") or "").strip()
    if not text:
        raise ValueError("缺少 input / text 字段")
    voice = body.get("voice") or body.get("voice_id") or CFG.get("default_voice")
    instruct = (body.get("instruct") or "").strip() or None
    try:
        speed = float(body.get("speed") or _read_state().get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    speed = max(0.5, min(2.0, speed))
    try:
        gap_ms = int(body.get("gap_ms") or 200)
    except (TypeError, ValueError):
        gap_ms = 200
    gap_ms = max(0, min(2000, gap_ms))

    chunks = _split_long(text)
    if not chunks:
        raise ValueError("没有可合成的内容")

    # 逐段合成（VE.synthesize 内部有缓存，重复段落会命中）
    seg_paths = []
    engines = set()
    for i, seg in enumerate(chunks):
        res = VE.synthesize(voice, seg, speed=speed, instruct=instruct)
        seg_paths.append(res["path"])
        engines.add(res.get("engine") or "?")

    # 段间静音
    with_audio = []
    if gap_ms > 0:
        first = seg_paths[0]
        _fmt, nch, sw, sr, off, size = _wav_info(open(first, "rb").read())
        gap_samples = int(sr * gap_ms / 1000) * nch
        gap_path = os.path.join(LONG_OUT, "_gap_%d.wav" % gap_ms)
        # 直接写一个 16bit PCM 静音 wav（手写，避免 wave 模块的格式限制）
        with open(gap_path, "wb") as fh:
            fh.write(struct.pack("<4sI4s", b"RIFF", 36 + gap_samples * 2, b"WAVE"))
            fh.write(struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, nch, sr,
                                 sr * nch * 2, nch * 2, 16))
            fh.write(struct.pack("<4sI", b"data", gap_samples * 2))
            fh.write(b"\x00" * (gap_samples * 2))
        for i, p in enumerate(seg_paths):
            with_audio.append(p)
            if i < len(seg_paths) - 1:
                with_audio.append(gap_path)
    else:
        with_audio = list(seg_paths)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_name = "long_%s_%s.wav" % (stamp, voice.replace("/", "_"))
    out_path = os.path.join(LONG_OUT, out_name)
    _concat_wavs(with_audio, out_path)

    _fmt, nch, sw, sr, off, size = _wav_info(open(out_path, "rb").read())
    seconds = round(size / (sr * nch * sw), 1)

    return {
        "path": out_path,
        "filename": out_name,
        "seconds": seconds,
        "chunks": len(chunks),
        "engines": sorted(engines),
    }


def _panel_debug(body):
    """面板前端把「朗读组件走到哪一步」上报到这里，排查用。

    只收键名、长度、状态码这类结构化信息，不收集聊天正文。
    """
    try:
        line = json.dumps(body, ensure_ascii=False)[:2000]
        path = os.path.join(BASE_DIR, "logs", "panel-debug.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
            if len(lines) > 200:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.writelines(lines[-200:])
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _task_name():
    """本机若用计划任务托管这几个服务，重启时优先让它来拉。"""
    return os.environ.get("VOXRELAY_TASK") or os.environ.get("VOICEHUB_TASK") or "VoiceHub-Services"


def _restart():
    """响应后 1 秒拉起新进程再退出自己，实现面板里的一键重启。"""
    def _do():
        ok = False
        task = _task_name()
        try:
            q = subprocess.run(["schtasks", "/Query", "/TN", task],
                               capture_output=True, timeout=20)
            if q.returncode == 0:
                # 计划任务能脱离会话存活，比直接 spawn 稳
                r = subprocess.run(["schtasks", "/Run", "/TN", task],
                                   capture_output=True, timeout=30)
                ok = r.returncode == 0
        except Exception as exc:  # noqa: BLE001
            log("计划任务拉起失败: %s" % exc)
        if not ok:
            try:
                flags = (0x00000008 | 0x00000200) if os.name == "nt" else 0
                subprocess.Popen(
                    [sys.executable, os.path.join(BASE_DIR, "server.py"),
                     "--host", LISTEN["host"], "--port", str(LISTEN["port"])],
                    cwd=BASE_DIR,
                    creationflags=flags,
                    close_fds=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc2:  # noqa: BLE001
                log("重启拉起新进程失败: %s" % exc2)
        os._exit(0)

    threading.Timer(1.0, _do).start()
    return {"restarting": True, "note": "1 秒后重启"}


def _stack_status():
    """三件套状态：以端口是否监听为准，和启停按钮用同一套判断。

    以前用 HTTP /health 判断，端口刚开、服务还没响应时会瞬间显示「未运行」，
    和刚点完「启动」的反馈自相矛盾。
    """
    st = VS.status()

    def pick(*names):
        # 主服务在旧面板里叫 voicehub，仓库里叫 VoxRelay，两个都认
        for n in names:
            if n in st:
                return st[n]
        return {}

    return {
        "voicehub": pick("VoxRelay", "VoiceHub"),
        "gsv": pick("GSV"),
        "rvc": pick("RVC"),
        "higgs": pick("Higgs"),
        "cosy": pick("CosyVoice"),
    }


def _mode_payload():
    s = _read_state()
    # 这里别调 VE.engine_status()：它会逐个 HTTP 探测引擎，每个超时 2 秒，
    # 面板首屏要等 4 秒以上。面板只需要「edge/sapi 能不能用」和「哪个服务在跑」。
    svc = VS.status()
    vid = s.get("voice") or ""
    ent = (CFG.get("voices") or {}).get(vid) or {}
    return {
        "enabled": bool(s.get("enabled")),
        "voice": vid,
        "voice_name": ent.get("name") or vid,
        "engine": s.get("engine"),
        "engine_voices": s.get("engineVoices") or {},
        "mode": s.get("mode"),
        "speech_mode": s.get("speech_mode") or "panel",
        "volume": _current_volume(),
        "speed": s.get("speed") or 1.0,
        "stack": _stack_status(),
        "locations": _read_stack(),
        "model_dirs": {
            "gsv": os.path.join(BASE_DIR, "models", "gsv"),
            "rvc": os.path.join(BASE_DIR, "models", "rvc"),
            # Higgs / CosyVoice 没有音色目录，这里给的是它们的安装目录，
            # 面板「设置安装位置」的占位符要用。
            "higgs": os.path.join(BASE_DIR, "higgs"),
            "cosy": os.path.join(BASE_DIR, "cosyvoice"),
        },
        "engines": {
            "gsv": bool(svc.get("GSV", {}).get("running")),
            "higgs": bool(svc.get("Higgs", {}).get("running")),
            "cosy": bool(svc.get("CosyVoice", {}).get("running")),
            "rvc": bool(svc.get("RVC", {}).get("running")),
            "tts": bool(VE.has_edge() or VE.has_sapi()),
        },
        # 四选一：前端据此高亮「当前是哪个引擎」，不用自己再猜。
        "engine_group": list(VS.GPU_GROUP),
        "running_engine": (VS.running_engines() or [None])[0],
        "engine_ports": {name: svc.get(name, {}).get("port") for name in VS.GPU_GROUP},
    }


def _apply_mode(body):
    s = _read_state()
    voices = CFG.get("voices") or {}
    if "enabled" in body:
        s["enabled"] = bool(body["enabled"])
    if body.get("volume") is not None:
        try:
            vol = max(0.05, min(1.0, float(body["volume"])))
            with open(os.path.join(BASE_DIR, "volume.json"), "w", encoding="utf-8") as fh:
                json.dump({"volume": round(vol, 3)}, fh)
            s["volume"] = vol
        except (TypeError, ValueError):
            raise ValueError("volume 必须是数字")
    if body.get("speed") is not None:
        try:
            s["speed"] = max(0.5, min(2.0, float(body["speed"])))
        except (TypeError, ValueError):
            raise ValueError("speed 必须是数字")
    if body.get("speech_mode") in ("panel", "mcp"):
        s["speech_mode"] = body["speech_mode"]
    vid = body.get("voice")
    if vid:
        if vid not in voices:
            raise KeyError("没有这个音色: %s" % vid)
        s["voice"] = vid
        s["engine"] = _voice_engine(vid)
        s["engineVoices"][s["engine"]] = vid
    eng = body.get("engine")
    if eng in ("gsv", "tts"):
        s["engine"] = eng
        remembered = (s.get("engineVoices") or {}).get(eng)
        if remembered in voices:
            s["voice"] = remembered
        elif _voice_engine(s["voice"]) == eng:
            # 当前音色本来就属于这个引擎 → 别动，顺手记住
            s["engineVoices"][eng] = s["voice"]
        else:
            # 这个引擎还没选过音色 → 拿第一个同类的顶上
            for k in voices:
                if _voice_engine(k) == eng:
                    s["voice"] = k
                    break
            s["engineVoices"][eng] = s["voice"]
    _write_state(s)
    return _mode_payload()


PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>VoxRelay 试听</title>
<style>
body{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:0;background:#f6f7f8;color:#1f2328}
.wrap{max-width:720px;margin:40px auto;background:#fff;border:1px solid #e3e5e8;border-radius:12px;padding:28px}
h1{font-size:18px;margin:0 0 4px}
p.sub{color:#6b7280;font-size:13px;margin:0 0 20px}
label{display:block;font-size:13px;color:#374151;margin:14px 0 6px}
select,textarea{width:100%;box-sizing:border-box;padding:9px 10px;border:1px solid #d8dbe0;border-radius:8px;font-size:14px;font-family:inherit}
textarea{height:90px;resize:vertical}
.row{display:flex;gap:10px;align-items:center;margin-top:16px}
button{padding:9px 18px;border:0;border-radius:8px;background:#1f6feb;color:#fff;font-size:14px;cursor:pointer}
button:disabled{opacity:.55;cursor:default}
audio{width:100%;margin-top:16px}
.st{font-size:12px;color:#6b7280;margin-top:8px;min-height:16px}
</style></head><body><div class="wrap">
<h1>VoxRelay</h1><p class="sub">本地语音服务，OpenAI 兼容接口 · POST /v1/audio/speech</p>
<label>音色</label><select id="v"></select>
<label>文本</label><textarea id="t">你好，我是本地的语音服务，现在开始为你朗读。</textarea>
<div class="row"><button id="b">合成并播放</button><span class="st" id="s"></span></div>
<audio id="a" controls></audio>
</div><script>
fetch('/v1/audio/voices').then(r=>r.json()).then(d=>{
  const sel=document.getElementById('v');
  d.voices.forEach(v=>{const o=document.createElement('option');o.value=v.id;
    o.textContent=v.name+' · '+v.engine+(v.rvc?' · RVC':'')+(v.ready?'':' (不可用)');sel.appendChild(o);});
});
document.getElementById('b').onclick=async()=>{
  const b=document.getElementById('b'),s=document.getElementById('s');
  b.disabled=true;s.textContent='合成中…';
  try{
    const r=await fetch('/v1/audio/speech',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({input:document.getElementById('t').value,voice:document.getElementById('v').value})});
    if(!r.ok){throw new Error(await r.text())}
    const bl=await r.blob();
    document.getElementById('a').src=URL.createObjectURL(bl);
    document.getElementById('a').play();
    s.textContent='完成 · '+(bl.size/1024).toFixed(1)+' KB · '+r.headers.get('X-VoxRelay-Engine');
  }catch(e){s.textContent='失败: '+e.message}
  b.disabled=false;
};
</script></body></html>"""


LONG_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VoxRelay · 长文本合成</title>
<style>
body{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:0;background:#f6f7f8;color:#1f2328}
.wrap{max-width:860px;width:calc(100% - 32px);margin:32px auto;background:#fff;border:1px solid #e3e5e8;border-radius:12px;padding:28px;box-sizing:border-box}
h1{font-size:18px;margin:0 0 4px}
p.sub{color:#6b7280;font-size:13px;margin:0 0 20px}
label{display:block;font-size:13px;color:#374151;margin:16px 0 6px}
select,textarea{width:100%;box-sizing:border-box;padding:10px;border:1px solid #d8dbe0;border-radius:8px;font-size:14px;font-family:inherit}
textarea{height:240px;resize:vertical;line-height:1.7}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:16px}
button{padding:10px 20px;border:0;border-radius:8px;background:#1f6feb;color:#fff;font-size:14px;cursor:pointer}
button:disabled{opacity:.55;cursor:default}
button.ghost{background:#fff;color:#374151;border:1px solid #d8dbe0}
audio{width:100%;margin-top:18px}
.st{font-size:12px;color:#6b7280;margin-top:10px;min-height:16px;white-space:pre-wrap}
.hint{font-size:12px;color:#9ca3af;margin-top:4px}
.prog{height:6px;background:#eef0f2;border-radius:3px;overflow:hidden;margin-top:10px;display:none}
.prog i{display:block;height:100%;background:#1f6feb;width:0;transition:width .3s}
</style></head><body><div class="wrap">
<h1>长文本合成</h1>
<p class="sub">整段文字一次生成一条音频，自动按句切段、拼接成单个 WAV 下载</p>
<label>音色</label><select id="v"></select>
<div class="hint">长文建议用 Higgs / CosyVoice（自带分段与自然停顿）；GSV 走回退时逐段合成较慢。</div>
<label>文本</label><textarea id="t" placeholder="把要朗读的整段文字粘贴到这里……"></textarea>
<label>语速 <span id="spd">1.0</span>×</label>
<input type="range" id="speed" min="0.5" max="2.0" step="0.05" value="1.0"
  style="width:100%;margin-top:4px">
<label>段间停顿 <span id="gapv">200</span> ms</label>
<input type="range" id="gap" min="0" max="800" step="20" value="200"
  style="width:100%;margin-top:4px">
<div class="row">
  <button id="b">开始合成</button>
  <button class="ghost" id="dl" disabled>下载 WAV</button>
</div>
<div class="prog" id="prog"><i id="bar"></i></div>
<div class="st" id="s"></div>
<audio id="a" controls style="display:none"></audio>
</div><script>
const $=id=>document.getElementById(id);
let lastPath=null,lastName=null;
function vSpeed(){ $('spd').textContent=(+$('speed').value).toFixed(2); }
function vGap(){ $('gapv').textContent=$('gap').value; }
$('speed').oninput=vSpeed; $('gap').oninput=vGap;
async function renderVoices(){
  const d=await (await fetch('/v1/audio/voices')).json();
  const sel=$('v'),keep=sel.value;
  sel.innerHTML='';
  d.voices.forEach(v=>{const o=document.createElement('option');o.value=v.id;
    o.textContent=v.name+' · '+v.engine+(v.ready?'':' (不可用)');sel.appendChild(o);});
  if(keep) sel.value=keep;
  // 优先当前引擎的音色；若它不可用，退回第一个可用的
  try{const m=await (await fetch('/v1/mode')).json();
    if(m.voice&&[...sel.options].some(o=>o.value===m.voice)) sel.value=m.voice;
  }catch(e){}
  const cur=d.voices.find(v=>v.id===sel.value);
  if(cur&&cur.ready===false){
    const ok=d.voices.find(v=>v.ready!==false);
    if(ok) sel.value=ok.id;
  }
}
$('b').onclick=async()=>{
  const text=$('t').value.trim();
  if(!text){ $('s').textContent='请先输入要合成的文字'; return; }
  const voice=$('v').value;
  $('b').disabled=true; $('dl').disabled=true;
  $('prog').style.display='block'; $('bar').style.width='0%';
  $('s').textContent='正在切到 '+(voice)+' 对应引擎并合成…（Higgs 首次加载约 10 秒）';
  try{
    const r=await fetch('/v1/long-tts',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({input:text,voice:voice,speed:+$('speed').value,gap_ms:+$('gap').value})});
    const d=await r.json();
    if(!r.ok){ throw new Error((d.error&&d.error.message)||('合成失败 '+r.status)); }
    $('bar').style.width='100%';
    lastPath=d.path; lastName=d.filename;
    const secs=d.seconds+' 秒';
    $('s').textContent='完成：'+d.filename+' · '+secs+' · '+d.chunks+' 段 · 引擎 '+d.engines.join('/');
    const a=$('a'); a.src='/long-file?name='+encodeURIComponent(d.filename); a.style.display='block';
    $('dl').disabled=false;
  }catch(e){
    $('s').textContent='出错：'+e.message;
  }finally{
    $('b').disabled=false;
  }
};
$('dl').onclick=()=>{ if(lastName) location.href='/long-file?name='+encodeURIComponent(lastName); };
renderVoices();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "VoxRelay/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---------------------------------------------------------- 输出helper
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        # X-VoiceHub-Auto 必须放行：面板是浏览器，发合成请求时带这个自定义头，
        # preflight 没允许它整条请求就会被浏览器拦掉，表现为 TypeError: Failed to fetch，
        # 服务端连日志都收不到。
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type,Authorization,X-Requested-With,X-VoiceHub-Auto")
        self.send_header("Access-Control-Expose-Headers", "X-VoxRelay-Engine,X-VoxRelay-Note,X-VoxRelay-RVC,X-VoxRelay-Cache")

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, obj, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", extra)

    def _err(self, code, msg, etype="invalid_request_error"):
        self._json(code, {"error": {"message": msg, "type": etype}})

    def _body_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        ctype = (self.headers.get("Content-Type") or "").lower()
        if not raw:
            return {}
        if "application/json" in ctype:
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                raise ValueError("请求体不是合法 JSON")
        if "form-urlencoded" in ctype:
            return {k: v[0] for k, v in parse_qs(raw.decode("utf-8")).items()}
        text = raw.decode("utf-8", "replace")
        try:
            return json.loads(text)
        except Exception:
            return {"input": text}

    # ---------------------------------------------------------- 路由
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if path in ("/long", "/long-tts-page"):
                return self._send(200, LONG_PAGE, "text/html; charset=utf-8")
            if path in ("/health", "/healthz"):
                st = VE.engine_status()
                return self._json(200, {
                    "ok": True,
                    "version": VERSION,
                    "uptime_sec": int(time.time() - START_TS),
                    "engines": st,
                    "voices": len(CFG.get("voices", {})),
                    "default_voice": CFG.get("default_voice"),
                })
            if path in ("/v1/audio/voices", "/voices"):
                return self._json(200, {"object": "list", "voices": VE.list_voices()})
            if path in ("/v1/mode", "/mode"):
                return self._json(200, _mode_payload())
            if path in ("/v1/stack", "/stack"):
                return self._json(200, {"locations": _read_stack(), "stack": _stack_status()})
            if path in ("/v1/services", "/services"):
                return self._json(200, {"services": VS.status()})
            if path in ("/v1/models", "/models"):
                return self._json(200, {
                    "object": "list",
                    "data": [{"id": "voxrelay", "object": "model", "owned_by": "local"}],
                })
            if path in ("/say", "/speak"):
                return self._speech(q, is_get=True)
            if path == "/long-file":
                name = q.get("name") or ""
                if not name or "/" in name or "\\" in name or ".." in name:
                    return self._err(400, "非法文件名")
                fp = os.path.join(LONG_OUT, name)
                if not os.path.exists(fp):
                    return self._err(404, "文件不存在")
                with open(fp, "rb") as fh:
                    data = fh.read()
                return self._send(200, data, "audio/wav",
                                  {"Content-Disposition": 'attachment; filename="%s"' % name})
            return self._err(404, "未知路径: %s" % path)
        except Exception as exc:
            log("GET %s 出错: %s\n%s" % (path, exc, traceback.format_exc()))
            return self._err(500, str(exc), "server_error")

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if path in ("/v1/audio/speech", "/tts", "/say"):
                params = self._body_json()
                params.update({k: v for k, v in q.items() if k not in params})
                return self._speech(params)
            if path in ("/v1/mode", "/mode"):
                return self._json(200, _apply_mode(self._body_json()))
            if path in ("/v1/stack", "/stack"):
                return self._json(200, _apply_stack(self._body_json()))
            if path in ("/v1/rescan", "/rescan"):
                return self._json(200, _rescan())
            if path in ("/v1/voice", "/voice"):
                return self._json(200, _add_voice(self._body_json()))
            if path in ("/v1/voice/delete", "/voice/delete"):
                return self._json(200, _delete_voice(self._body_json()))
            if path in ("/v1/restart", "/restart"):
                return self._json(200, _restart())
            if path in ("/v1/service", "/service"):
                return self._json(200, _apply_service(self._body_json()))
            if path in ("/v1/engine", "/engine"):
                return self._json(200, _apply_engine(self._body_json()))
            if path in ("/v1/debug", "/debug"):
                return self._json(200, _panel_debug(self._body_json()))
            if path in ("/v1/long-tts", "/long-tts"):
                try:
                    res = _long_tts(self._body_json())
                    return self._json(200, res)
                except ValueError as exc:
                    return self._err(400, str(exc))
                except Exception as exc:
                    log("long-tts 出错: %s\n%s" % (exc, traceback.format_exc()))
                    return self._err(500, str(exc), "server_error")
            return self._err(404, "未知路径: %s" % path)
        except ValueError as exc:
            return self._err(400, str(exc))
        except KeyError as exc:
            return self._err(400, str(exc).strip("'\""))
        except Exception as exc:
            log("POST %s 出错: %s\n%s" % (path, exc, traceback.format_exc()))
            return self._err(500, str(exc), "server_error")

    # ---------------------------------------------------------- 核心
    def _speech(self, params, is_get=False):
        t0 = time.time()
        # 自动朗读总闸：语音模式关闭时，凡标了 auto 的请求一律不出声。
        # 以前只在 MCP 的 speak 里判断，绕过 MCP 直接打 HTTP 的 agent 照样会念。
        auto = (self.headers.get("X-VoiceHub-Auto") == "1"
                or str(params.get("auto") or "").lower() in ("1", "true", "yes"))
        if auto and not _read_state().get("enabled", True):
            return self._send(204, b"", "audio/wav", {"X-VoiceHub-Muted": "1"})
        text = (params.get("input") or params.get("text") or "").strip()
        if not text:
            return self._err(400, "缺少 input / text 字段")
        max_chars = int(CFG.get("max_chars") or 3000)
        if len(text) > max_chars:
            return self._err(400, "文本过长（%d > %d 字），请分段请求" % (len(text), max_chars))

        voice = params.get("voice") or params.get("voice_id") or CFG.get("default_voice")
        try:
            speed = float(params.get("speed") or _read_state().get("speed") or 1.0)
        except (TypeError, ValueError):
            speed = 1.0
        speed = max(0.5, min(2.0, speed))
        rvc_model = params.get("rvc")
        pitch = params.get("pitch")

        res = VE.synthesize(
            voice,
            text,
            speed=speed,
            rvc_model=rvc_model,
            pitch=int(pitch) if pitch not in (None, "") else 0,
        )
        with open(res["path"], "rb") as fh:
            data = fh.read()
        data = _apply_volume(data, res["ext"])

        want = (params.get("response_format") or params.get("format") or "").lower()
        note = res.get("note") or ""
        if want and want != res["ext"]:
            note = (note + "; " if note else "") + "实际格式为 %s（未做转码）" % res["ext"]

        log("speech voice=%s engine=%s rvc=%s chars=%d ms=%d cache=%s %s" % (
            voice, res["engine"], res.get("rvc") or "-", len(text),
            int((time.time() - t0) * 1000), res.get("cached"), note))

        extra = {
            "X-VoxRelay-Engine": str(res["engine"]),
            "X-VoxRelay-RVC": str(res.get("rvc") or ""),
            "X-VoxRelay-Cache": "hit" if res.get("cached") else "miss",
            "Content-Disposition": 'inline; filename="speech.%s"' % res["ext"],
        }
        if note:
            extra["X-VoxRelay-Note"] = note.encode("utf-8").decode("latin-1", "ignore")
        self._send(200, data, res["mime"], extra)


def _current_volume():
    """音量单独放一个文件，改了立刻生效（不用重启）。"""
    path = os.path.join(BASE_DIR, "volume.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return max(0.05, min(1.0, float(json.load(fh).get("volume", 1.0))))
    except Exception:  # noqa: BLE001
        return float(CFG.get("output_volume") or 1.0)


def _apply_volume(data, ext):
    """按比例衰减 wav 音量。Python 3.13 已移除 audioop，这里手动处理采样。"""
    vol = _current_volume()
    if vol >= 0.99 or ext != "wav":
        return data
    try:
        import array
        import io
        import wave

        with wave.open(io.BytesIO(data), "rb") as w:
            params = w.getparams()
            frames = w.readframes(w.getnframes())
        if params.sampwidth != 2:
            return data
        samples = array.array("h")
        samples.frombytes(frames)
        scaled = array.array("h", (
            -32768 if int(v * vol) < -32768 else
            32767 if int(v * vol) > 32767 else int(v * vol)
            for v in samples
        ))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setparams(params)
            w.writeframes(scaled.tobytes())
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return data


def main():
    argv = sys.argv[1:]
    host = CFG.get("host") or "127.0.0.1"
    port = int(CFG.get("port") or 8787)
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
        if a == "--host" and i + 1 < len(argv):
            host = argv[i + 1]

    VS.set_self_port(port)
    LISTEN["host"], LISTEN["port"] = host, port

    cfg_port = int(CFG.get("port") or 8787)
    if port != cfg_port:
        log("注意: 实际监听 %d，而 config.json 里写的是 %d。" % (port, cfg_port))
        log("      面板/MCP 会读 endpoint.json 拿到 %d，能自动对上；" % port)
        log("      但要让配置本身一致，建议直接改 config.json 的 port。")

    VE.cleanup_cache()
    st = VE.engine_status()
    voices = CFG.get("voices", {})
    log("=" * 62)
    log("VoxRelay %s 启动  http://%s:%d" % (VERSION, host, port))
    log("音色 %d 个 | edge=%s sapi=%s upstream=%s rvc=%s" % (
        len(voices), st["edge"], st["sapi"], st["upstream"], st["rvc"]))
    if not st["edge"]:
        log("提示: edge-tts 未安装，将全部退回 SAPI 系统音色")
    log("=" * 62)

    threading.Thread(target=_heal_loop, daemon=True).start()

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    # 端口已真正 bind 成功，这时记下来才有意义
    _write_endpoint(host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("收到 Ctrl+C，正在关闭…")
    finally:
        httpd.server_close()
        _clear_endpoint()
        log("已停止")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    main()
