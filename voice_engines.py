# -*- coding: utf-8 -*-
"""VoxRelay 引擎层：edge-tts / Windows SAPI / 上游 OpenAI 兼容服务 / RVC 后处理。

设计原则：任何一个引擎坏了都不能让服务挂掉，按 voices 配置逐级降级。
"""
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(ROOT, "vendor")
if os.path.isdir(VENDOR) and VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)

OUT_DIR = os.path.join(ROOT, "out")
CACHE_DIR = os.path.join(OUT_DIR, "cache")
for _d in (OUT_DIR, CACHE_DIR):
    os.makedirs(_d, exist_ok=True)

MIME = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "flac": "audio/flac",
    "opus": "audio/opus",
    "aac": "audio/aac",
}

_CFG = {}
_EDGE_OK = None
_SAPI_OK = None
_SEM = threading.Semaphore(3)
_log = print


def configure(cfg, logger=None):
    global _CFG, _log
    _CFG = cfg
    if logger:
        _log = logger


# ---------------------------------------------------------------- 基础工具


def _voice_entry(voice_id):
    voices = _CFG.get("voices", {})
    if voice_id in voices:
        return voice_id, voices[voice_id]
    for key, val in voices.items():
        if val.get("name") == voice_id:
            return key, val
    raise KeyError("未知音色: %s" % voice_id)


def has_edge():
    global _EDGE_OK
    if _EDGE_OK is None:
        try:
            import edge_tts  # noqa: F401
            _EDGE_OK = True
        except Exception as exc:  # pragma: no cover
            _EDGE_OK = False
            _log("[engine] edge-tts 不可用: %s" % exc)
    return _EDGE_OK


def sapi_voices():
    """列出系统 SAPI 音色（失败返回空列表，不抛错）。"""
    try:
        ps = (
            "Add-Type -AssemblyName System.Speech;"
            "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
            ".GetInstalledVoices()|%{$_.VoiceInfo.Name}"
        )
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True,
            timeout=20,
        )
        out = p.stdout.decode("utf-8", "replace")
        return [x.strip() for x in out.splitlines() if x.strip()]
    except Exception:
        return []


def has_sapi():
    global _SAPI_OK
    if _SAPI_OK is None:
        _SAPI_OK = os.name == "nt" and bool(sapi_voices())
    return _SAPI_OK


def _speed_to_rate(speed):
    pct = int(round((float(speed) - 1.0) * 100))
    pct = max(-50, min(100, pct))
    return "%+d%%" % pct


def _speed_to_sapi_rate(speed):
    r = int(round((float(speed) - 1.0) * 10))
    return max(-10, min(10, r))


# ---------------------------------------------------------------- 各引擎


def _edge_synth(text, voice, rate, path):
    import edge_tts

    async def _go():
        comm = edge_tts.Communicate(text, voice, rate=rate)
        await comm.save(path)

    with _SEM:
        asyncio.run(_go())


_PS_SAPI = r"""
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($env:VH_VOICE) { try { $s.SelectVoice($env:VH_VOICE) } catch {} }
$s.Rate = [int]$env:VH_RATE
$text = [Console]::In.ReadToEnd()
$s.SetOutputToWaveFile($env:VH_OUT)
$s.Speak($text)
$s.Dispose()
"""


def _sapi_synth(text, voice_name, rate, path):
    env = dict(os.environ)
    env["VH_OUT"] = path
    env["VH_VOICE"] = voice_name or ""
    env["VH_RATE"] = str(rate)
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _PS_SAPI],
        input=text.encode("utf-8"),
        env=env,
        capture_output=True,
        timeout=180,
    )
    if proc.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) < 128:
        err = proc.stderr.decode("utf-8", "replace")[:400]
        raise RuntimeError("SAPI 合成失败: %s" % (err or "无输出"))


def _upstream_synth(text, speed, path_stem):
    cfg = _CFG.get("engines", {}).get("upstream", {})
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        raise RuntimeError("upstream.base_url 未配置")
    url = base + "/audio/speech"
    payload = {
        "model": cfg.get("model") or "tts",
        "input": text,
        "voice": cfg.get("voice") or "default",
        "speed": float(speed),
        "response_format": cfg.get("format") or "wav",
    }
    headers = {"Content-Type": "application/json"}
    key = cfg.get("api_key") or ""
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = resp.read()
        ctype = (resp.headers.get("Content-Type") or "").lower()
    ext = "wav"
    for k, m in MIME.items():
        if m in ctype:
            ext = k
            break
    path = path_stem + "." + ext
    with open(path, "wb") as fh:
        fh.write(data)
    return path, ext


_GSV_SET = {"gpt": None, "sovits": None}
# 并发合成时多个线程会同时走到下面：检查时都是空，于是同一权重被切换 N 次。
# 检查 + HTTP 切换 + 写缓存必须是一段临界区，后来的请求等锁并直接复用结果。
_GSV_WEIGHT_LOCK = threading.Lock()


def _gsv_base():
    cfg = _CFG.get("engines", {}).get("gsv", {})
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        raise RuntimeError("engines.gsv.base_url 未配置")
    return cfg, base


def _gsv_get(base, path, timeout=120):
    req = urllib.request.Request(base + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get("Content-Type") or ""


def _gsv_set_weights(kind, path):
    """切换到指定的训练权重；同一条路径只切一次（并发请求也只切一次）。"""
    if not path:
        return
    with _GSV_WEIGHT_LOCK:
        if _GSV_SET[kind] == path:
            return
        from urllib.parse import quote

        cfg, base = _gsv_base()
        endpoint = "/set_gpt_weights" if kind == "gpt" else "/set_sovits_weights"
        _gsv_get(base, "%s?weights_path=%s" % (endpoint, quote(path)))
        _GSV_SET[kind] = path
        _log("[gsv] 已切换 %s 权重 -> %s" % (kind, os.path.basename(path)))


def _gsv_synth(text, entry, speed, path_stem):
    cfg, base = _gsv_base()
    ref = entry.get("ref_audio") or cfg.get("ref_audio") or ""
    if not ref or not os.path.exists(ref):
        raise RuntimeError("参考音频不存在: %s" % ref)
    _gsv_set_weights("gpt", entry.get("gpt_weights"))
    _gsv_set_weights("sovits", entry.get("sovits_weights"))
    payload = {
        "text": text,
        "text_lang": entry.get("text_lang") or cfg.get("text_lang") or "zh",
        "ref_audio_path": ref,
        "prompt_text": entry.get("prompt_text") or cfg.get("prompt_text") or "",
        "prompt_lang": entry.get("prompt_lang") or cfg.get("prompt_lang") or "zh",
        "text_split_method": cfg.get("text_split_method") or "cut5",
        "speed_factor": float(speed) * float(cfg.get("speed_factor") or 1.0),
        "sample_steps": int(cfg.get("sample_steps") or 32),
        "media_type": cfg.get("media_type") or "wav",
        "streaming_mode": False,
        "batch_size": 1,
        "parallel_infer": True,
        "use_cuda_graph": True,
    }
    if cfg.get("cfg_rate") is not None:
        payload["cfg_rate"] = float(cfg["cfg_rate"])
    req = urllib.request.Request(
        base + "/tts",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=int(cfg.get("timeout") or 180)) as resp:
        data = resp.read()
        ctype = (resp.headers.get("Content-Type") or "").lower()
    if not data or len(data) < 512:
        raise RuntimeError("GSV 返回空音频")
    ext = "mp3" if "mpeg" in ctype else "wav"
    path = path_stem + "." + ext
    with open(path, "wb") as fh:
        fh.write(data)
    return path, ext


def _cosy_synth(text, entry, speed, path_stem, instruct_override=None):
    """调用 CosyVoice 3.0 本地服务（零样本克隆 / instruct 情感控制）。

    instruct_override 传入时优先用它（长文合成页的语气指令覆盖音色默认值），
    否则回落到音色条目 / engines.cosy 里的 instruct。
    """
    cfg = _CFG.get("engines", {}).get("cosy", {})
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        raise RuntimeError("engines.cosy.base_url 未配置")
    ref = entry.get("ref_audio") or cfg.get("ref_audio") or ""
    if not ref or not os.path.exists(ref):
        raise RuntimeError("CosyVoice 参考音频不存在: %s" % ref)
    mode = entry.get("cosy_mode") or cfg.get("cosy_mode") or "zero_shot"
    instruct = instruct_override if instruct_override is not None \
        else (entry.get("instruct") or cfg.get("instruct") or "")
    payload = {
        "text": text,
        "mode": mode,
        "prompt_text": entry.get("prompt_text") or cfg.get("prompt_text") or "",
        "prompt_wav": ref,
        "instruct": instruct,
        "speed": float(speed or 1.0),
        "stream": False,
    }
    req = urllib.request.Request(
        base + "/synthesize",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=int(cfg.get("timeout") or 600)) as resp:
        resp_json = json.loads(resp.read().decode("utf-8"))
    if resp_json.get("error"):
        raise RuntimeError("CosyVoice 合成失败: %s" % resp_json["error"])
    src = resp_json.get("path")
    if not src or not os.path.exists(src):
        raise RuntimeError("CosyVoice 未返回有效音频路径")
    path = path_stem + ".wav"
    with open(src, "rb") as fin, open(path, "wb") as fout:
        fout.write(fin.read())
    # worker 每次把音频落在 out/ 下的新临时文件；复制完就删，
    # 否则每念一句都留一个几百 KB 孤儿，几天就堆出几个 GB。
    try:
        os.remove(src)
    except OSError:
        pass
    return path, "wav"


def _higgs_synth(text, entry, speed, path_stem):
    """调用本地 Higgs worker（Qwen3-4B + higgs codec），零样本克隆。

    实测结论：温度 0.5 / top_p 0.9 的重复一致性最好；长文本在 worker 侧按句切段，
    段间 220ms 静音。speed 暂不支持（模型本身没有语速参数），需要变速请在调用方处理。
    """
    cfg = _CFG.get("engines", {}).get("higgs", {})
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        raise RuntimeError("engines.higgs.base_url 未配置")
    ref = entry.get("ref_audio") or cfg.get("ref_audio") or ""
    if not ref or not os.path.exists(ref):
        raise RuntimeError("Higgs 参考音频不存在: %s" % ref)
    payload = {
        "text": text,
        "prompt_wav": ref,
        "prompt_text": entry.get("prompt_text") or cfg.get("prompt_text") or "",
        "temperature": float(entry.get("temperature") or cfg.get("temperature") or 0.5),
        "top_p": float(entry.get("top_p") or cfg.get("top_p") or 0.9),
        "chunk": bool(entry.get("chunk", cfg.get("chunk", True))),
        "max_chars": int(entry.get("max_chars") or cfg.get("max_chars") or 40),
        "gap_ms": int(entry.get("gap_ms") or cfg.get("gap_ms") or 220),
        "speed": float(entry.get("speed_factor") or cfg.get("speed_factor") or speed or 1.0),
    }
    req = urllib.request.Request(
        base + "/synthesize",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=int(cfg.get("timeout") or 600)) as resp:
        resp_json = json.loads(resp.read().decode("utf-8"))
    if resp_json.get("error"):
        raise RuntimeError("Higgs 合成失败: %s" % resp_json["error"])
    src = resp_json.get("path")
    if not src or not os.path.exists(src):
        raise RuntimeError("Higgs 未返回有效音频路径")
    path = path_stem + ".wav"
    with open(src, "rb") as fin, open(path, "wb") as fout:
        fout.write(fin.read())
    # worker 落盘的临时文件复制完就删，否则每句留一个孤儿（同 _cosy_synth）
    try:
        os.remove(src)
    except OSError:
        pass
    return path, "wav"


def rvc_convert(src_path, model, pitch):
    cfg = _CFG.get("rvc", {})
    worker = (cfg.get("worker") or "").rstrip("/")
    if not worker:
        raise RuntimeError("rvc.worker 未配置")
    from urllib.parse import quote

    url = "%s/convert?model=%s&pitch=%s" % (worker, quote(str(model)), int(pitch))
    with open(src_path, "rb") as fh:
        body = fh.read()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/octet-stream"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=int(cfg.get("timeout") or 180)) as resp:
        data = resp.read()
    if not data or len(data) < 128:
        raise RuntimeError("RVC worker 返回空音频")
    dst = os.path.splitext(src_path)[0] + ".rvc.wav"
    with open(dst, "wb") as fh:
        fh.write(data)
    return dst


# ---------------------------------------------------------------- 对外接口


def _port_up(url, timeout=0.15):
    """按端口判断服务是否在跑。

    以前这里用 urllib 打 GSV 的 /docs 和 RVC 的 /health，各自 2 秒超时：
    服务没起的时候，/health 要等满 2~4 秒才返回，而它本身是探活接口，
    慢到这个程度就失去了意义（冒烟测试用 2 秒超时轮询，永远等不到）。
    端口在监听就说明进程活着，本地回环实测 <1ms。
    """
    try:
        u = urlparse(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.socket() as s:
            s.settimeout(timeout)
            return s.connect_ex((host, int(port))) == 0
    except Exception:  # noqa: BLE001
        return False


def engine_status():
    """各引擎「当前可用吗」。

    GPU 四引擎（gsv / rvc / higgs / cosy）用端口监听判断（零延迟、不卡），
    否则每探一次要等一个 timeout。端口是否监听由 voice_services 统一维护，
    和面板 / /v1/mode 的判断完全一致。
    """
    rvc_cfg = _CFG.get("rvc", {})
    up = _CFG.get("engines", {}).get("upstream", {})

    # 端口监听判断（不发起 HTTP，绝不超时）
    try:
        import voice_services as _VS
        svc = _VS.status()
        gsv_ok = bool(svc.get("GSV", {}).get("running"))
        cosy_ok = bool(svc.get("CosyVoice", {}).get("running"))
        higgs_ok = bool(svc.get("Higgs", {}).get("running"))
        rvc_ok = bool(svc.get("RVC", {}).get("running"))
    except Exception:  # noqa: BLE001
        gsv_ok = cosy_ok = higgs_ok = rvc_ok = False

    return {
        "edge": has_edge(),
        "sapi": has_sapi(),
        "upstream": bool(up.get("enabled") and up.get("base_url")),
        "gsv": gsv_ok,
        "higgs": higgs_ok,
        "cosy": cosy_ok,
        "rvc": rvc_ok,
        "rvc_enabled": bool(rvc_cfg.get("enabled")),
    }


def list_voices():
    st = engine_status()
    out = []
    for vid, v in _CFG.get("voices", {}).items():
        eng = v.get("engine", "edge")
        ready = True
        if eng == "edge":
            ready = st["edge"]
        elif eng == "sapi":
            ready = st["sapi"]
        elif eng == "upstream":
            ready = st["upstream"]
        elif eng == "gsv":
            ready = st["gsv"]
        elif eng == "higgs":
            ready = st["higgs"]
        elif eng == "cosy":
            ready = st["cosy"]
        out.append(
            {
                "id": vid,
                "name": v.get("name") or vid,
                "desc": v.get("desc") or "",
                "engine": eng,
                "language": v.get("language") or "zh-CN",
                "rvc": v.get("rvc") or None,
                "ready": ready,
                # 语气参考：参考音台词 + 语气指令，面板「语气提示词」用它做展示
                "prompt_text": v.get("prompt_text") or "",
                "instruct": v.get("instruct") or "",
            }
        )
    return out


def _engine_variant(engine, entry):
    """把「同一音色、但会改变音频」的引擎参数并进缓存键。

    CosyVoice 的 mode / instruct / 参考音频直接决定输出，只按
    voice_id + text + speed 做键时，用户改了情感指令还会命中旧音频，
    现象就是「instruct 完全不起作用」——其实是被缓存吃了。
    """
    if engine != "cosy":
        return ""
    cfg = _CFG.get("engines", {}).get("cosy", {})
    return "|".join(str(entry.get(k) or cfg.get(k) or "")
                    for k in ("cosy_mode", "instruct", "ref_audio", "prompt_text"))


def _cache_key(voice_id, text, speed, rvc_model, pitch, extra=""):
    raw = "|".join([voice_id, str(speed), str(rvc_model), str(pitch), extra, text])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def cleanup_cache():
    """按 TTL + 大小上限清理缓存。

    - TTL：`cache.max_age_days`，默认 7 天；0 表示不按时间清
    - 大小：`cache.max_mb`，默认 300MB，超了删到 60%
    """
    cfg = _CFG.get("cache", {})
    if not cfg.get("enabled", True):
        return
    if not os.path.isdir(CACHE_DIR):
        return
    limit = int(cfg.get("max_mb", 300)) * 1024 * 1024
    try:
        max_age = float(cfg.get("max_age_days", 7))
    except (TypeError, ValueError):
        max_age = 7.0
    deadline = time.time() - max_age * 86400 if max_age > 0 else 0

    files = []
    total = 0
    for name in os.listdir(CACHE_DIR):
        p = os.path.join(CACHE_DIR, name)
        if not os.path.isfile(p):
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        if deadline and st.st_mtime < deadline:
            try:
                os.remove(p)
                continue
            except OSError:
                pass
        files.append((st.st_mtime, p, st.st_size))
        total += st.st_size

    if total <= limit:
        return
    files.sort()
    for _, p, sz in files:
        if total <= limit * 0.6:
            break
        try:
            os.remove(p)
            total -= sz
        except OSError:
            pass


_LAST_CLEANUP = 0.0


def _maybe_cleanup(interval=3600):
    """运行中也清，不再只靠启动那一次；每小时最多跑一次。"""
    global _LAST_CLEANUP
    now = time.time()
    if now - _LAST_CLEANUP < interval:
        return
    _LAST_CLEANUP = now
    try:
        cleanup_cache()
    except Exception:  # noqa: BLE001
        pass


def synthesize(voice_id, text, speed=1.0, rvc_model=None, pitch=0, use_cache=True,
               instruct=None):
    """返回 {path, ext, mime, engine, rvc, cached, note}

    instruct 非 None 时覆盖 cosy 音色的语气指令（长文合成页用），
    并并入缓存键，否则改了指令还会命中旧缓存。
    """
    key, entry = _voice_entry(voice_id)
    text = (text or "").strip()
    if not text:
        raise ValueError("文本为空")

    rvc_model = rvc_model if rvc_model is not None else entry.get("rvc")
    pitch = int(pitch if pitch is not None else entry.get("pitch") or 0)
    rvc_cfg = _CFG.get("rvc", {})
    want_rvc = bool(rvc_model) and bool(rvc_cfg.get("enabled"))
    engine = entry.get("engine", "edge")

    variant = _engine_variant(engine, entry)
    if instruct is not None:
        variant += "|instruct=" + str(instruct)
    ck = _cache_key(key, text, speed, rvc_model if want_rvc else None, pitch, variant)
    if use_cache and _CFG.get("cache", {}).get("enabled", True):
        for ext in ("mp3", "wav", "ogg", "flac"):
            cand = os.path.join(CACHE_DIR, "%s.%s" % (ck, ext))
            if os.path.exists(cand) and os.path.getsize(cand) > 128:
                _maybe_cleanup()
                return {
                    "path": cand,
                    "ext": ext,
                    "mime": MIME.get(ext, "application/octet-stream"),
                    "engine": entry.get("engine", "edge"),
                    "rvc": rvc_model if want_rvc else None,
                    "cached": True,
                    "note": "",
                }

    stem = os.path.join(CACHE_DIR, ck)
    note = []

    if engine == "sapi":
        path = stem + ".wav"
        _sapi_synth(text, entry.get("tts_voice") or "", _speed_to_sapi_rate(speed), path)
        ext = "wav"
    elif engine == "upstream":
        path, ext = _upstream_synth(text, speed, stem)
    elif engine == "gsv":
        try:
            path, ext = _gsv_synth(text, entry, speed, stem)
        except Exception as exc:
            note.append("GSV 失败，已回退默认音色: %s" % exc)
            _log("[gsv] 合成失败: %s" % exc)
            path = stem + ".mp3"
            _edge_synth(text, "zh-CN-XiaoxiaoNeural",
                        _speed_to_rate(speed), path)
            ext = "mp3"
            engine = "edge"
    elif engine == "higgs":
        try:
            path, ext = _higgs_synth(text, entry, speed, stem)
            if float(speed or 1.0) > 1.4:
                note.append("Higgs 变速 %.2f 倍偏高，可能有金属味" % float(speed))
        except Exception as exc:
            note.append("Higgs 失败，已回退默认音色: %s" % exc)
            _log("[higgs] 合成失败: %s" % exc)
            path = stem + ".mp3"
            _edge_synth(text, "zh-CN-XiaoxiaoNeural",
                        _speed_to_rate(speed), path)
            ext = "mp3"
            engine = "edge"
    elif engine == "cosy":
        try:
            path, ext = _cosy_synth(text, entry, speed, stem, instruct_override=instruct)
        except Exception as exc:
            note.append("CosyVoice 失败，已回退默认音色: %s" % exc)
            _log("[cosy] 合成失败: %s" % exc)
            path = stem + ".mp3"
            _edge_synth(text, "zh-CN-XiaoxiaoNeural",
                        _speed_to_rate(speed), path)
            ext = "mp3"
            engine = "edge"
    else:
        if not has_edge():
            # edge 不可用时退回 SAPI，保证一定有声音
            path = stem + ".wav"
            _sapi_synth(text, "", _speed_to_sapi_rate(speed), path)
            ext = "wav"
            note.append("edge 不可用，已回退 SAPI")
            engine = "sapi"
        else:
            path = stem + ".mp3"
            _edge_synth(text, entry.get("tts_voice") or "zh-CN-XiaoxiaoNeural",
                        _speed_to_rate(speed), path)
            ext = "mp3"

    if want_rvc:
        try:
            converted = rvc_convert(path, rvc_model, pitch)
            path, ext = converted, "wav"
        except Exception as exc:
            note.append("RVC 失败已跳过: %s" % exc)
            _log("[rvc] 转换失败 %s: %s" % (rvc_model, exc))

    if note:
        # 降级/跳过 RVC 的结果不写进缓存，否则以后一直命中降级音频，
        # 上游恢复了也不会自动切回去。
        alt = os.path.join(OUT_DIR, "uncached_%s.%s" % (ck, ext))
        try:
            os.replace(path, alt)
            path = alt
        except OSError:
            pass

    _maybe_cleanup()
    return {
        "path": path,
        "ext": ext,
        "mime": MIME.get(ext, "application/octet-stream"),
        "engine": engine,
        "rvc": rvc_model if want_rvc else None,
        "cached": False,
        "note": "; ".join(note),
    }
