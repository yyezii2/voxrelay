# -*- coding: utf-8 -*-
"""VoxRelay 引擎层：edge-tts / Windows SAPI / 上游 OpenAI 兼容服务 / RVC 后处理。

设计原则：任何一个引擎坏了都不能让服务挂掉，按 voices 配置逐级降级。
"""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

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
    """切换到指定的训练权重；同一条路径只切一次。"""
    if not path or _GSV_SET[kind] == path:
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


def engine_status():
    rvc_cfg = _CFG.get("rvc", {})
    rvc_on = bool(rvc_cfg.get("enabled"))
    rvc_ok = False
    if rvc_on:
        try:
            url = (rvc_cfg.get("worker") or "").rstrip("/") + "/health"
            with urllib.request.urlopen(url, timeout=2) as resp:
                rvc_ok = resp.status == 200
        except Exception:
            rvc_ok = False
    up = _CFG.get("engines", {}).get("upstream", {})
    gsv_cfg = _CFG.get("engines", {}).get("gsv", {})
    gsv_ok = False
    if gsv_cfg.get("base_url"):
        try:
            with urllib.request.urlopen(gsv_cfg["base_url"].rstrip("/") + "/docs", timeout=2) as resp:
                gsv_ok = resp.status == 200
        except urllib.error.HTTPError:
            gsv_ok = True
        except Exception:
            gsv_ok = False
    return {
        "edge": has_edge(),
        "sapi": has_sapi(),
        "upstream": bool(up.get("enabled") and up.get("base_url")),
        "gsv": gsv_ok,
        "rvc": rvc_ok,
        "rvc_enabled": rvc_on,
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
        out.append(
            {
                "id": vid,
                "name": v.get("name") or vid,
                "desc": v.get("desc") or "",
                "engine": eng,
                "language": v.get("language") or "zh-CN",
                "rvc": v.get("rvc") or None,
                "ready": ready,
            }
        )
    return out


def _cache_key(voice_id, text, speed, rvc_model, pitch):
    raw = "|".join([voice_id, str(speed), str(rvc_model), str(pitch), text])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def cleanup_cache():
    cfg = _CFG.get("cache", {})
    if not cfg.get("enabled", True):
        return
    limit = int(cfg.get("max_mb", 300)) * 1024 * 1024
    files = []
    total = 0
    for name in os.listdir(CACHE_DIR):
        p = os.path.join(CACHE_DIR, name)
        if os.path.isfile(p):
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            files.append((os.path.getmtime(p), p, sz))
            total += sz
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


def synthesize(voice_id, text, speed=1.0, rvc_model=None, pitch=0, use_cache=True):
    """返回 {path, ext, mime, engine, rvc, cached, note}"""
    key, entry = _voice_entry(voice_id)
    text = (text or "").strip()
    if not text:
        raise ValueError("文本为空")

    rvc_model = rvc_model if rvc_model is not None else entry.get("rvc")
    pitch = int(pitch if pitch is not None else entry.get("pitch") or 0)
    rvc_cfg = _CFG.get("rvc", {})
    want_rvc = bool(rvc_model) and bool(rvc_cfg.get("enabled"))

    ck = _cache_key(key, text, speed, rvc_model if want_rvc else None, pitch)
    if use_cache and _CFG.get("cache", {}).get("enabled", True):
        for ext in ("mp3", "wav", "ogg", "flac"):
            cand = os.path.join(CACHE_DIR, "%s.%s" % (ck, ext))
            if os.path.exists(cand) and os.path.getsize(cand) > 128:
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
    engine = entry.get("engine", "edge")
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

    return {
        "path": path,
        "ext": ext,
        "mime": MIME.get(ext, "application/octet-stream"),
        "engine": engine,
        "rvc": rvc_model if want_rvc else None,
        "cached": False,
        "note": "; ".join(note),
    }
