# -*- coding: utf-8 -*-
"""音色自动发现：扫描 models/ 目录，把用户自带的模型并入音色表。

约定（目录名即音色 id）：

    models/gsv/<id>/
        ref.wav        必需，5~10 秒干声，决定音色
        prompt.txt     必需，ref.wav 里说的那句话（转录文本）
        *.ckpt         可选，GPT 权重；有才会切换（零样本克隆不需要）
        *.pth          可选，SoVITS 权重
        voice.json     可选，覆盖 name / desc / prompt_lang / text_lang / speed_factor

    models/rvc/<id>/
        *.pth          必需
        *.index        可选，检索特征库，有的话音色更像
        voice.json     可选，覆盖 name / desc / pitch / base_voice

显式写在 config.json 里的音色优先，同 id 不会被覆盖。
"""
import json
import os

GSV_DIR = "gsv"
RVC_DIR = "rvc"
DEFAULT_BASE_VOICE = "zh-CN-XiaoxiaoNeural"


def _read_manifest(folder):
    path = os.path.join(folder, "voice.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _first(folder, ext):
    """取目录里第一个指定后缀的文件，按名称排序保证结果稳定。"""
    try:
        names = sorted(n for n in os.listdir(folder) if n.lower().endswith(ext))
    except OSError:
        return None
    return os.path.join(folder, names[0]) if names else None


def _read_prompt(folder):
    path = os.path.join(folder, "prompt.txt")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def _scan_gsv(root, voices, log):
    base = os.path.join(root, "models", GSV_DIR)
    if not os.path.isdir(base):
        return 0
    found = 0
    for vid in sorted(os.listdir(base)):
        folder = os.path.join(base, vid)
        if not os.path.isdir(folder) or vid.startswith("."):
            continue
        ref = os.path.join(folder, "ref.wav")
        if not os.path.exists(ref):
            log("[models] 跳过 %s/gsv/%s：缺 ref.wav" % (GSV_DIR, vid))
            continue
        if vid in voices:
            continue
        man = _read_manifest(folder)
        entry = {
            "name": man.get("name") or vid,
            "engine": "gsv",
            "ref_audio": ref,
            "prompt_text": man.get("prompt_text") or _read_prompt(folder),
            "prompt_lang": man.get("prompt_lang") or "zh",
            "text_lang": man.get("text_lang") or "zh",
            "language": man.get("language") or "zh-CN",
            "desc": man.get("desc") or "本地模型",
        }
        gpt = _first(folder, ".ckpt")
        sovits = _first(folder, ".pth")
        if gpt:
            entry["gpt_weights"] = gpt
        if sovits:
            entry["sovits_weights"] = sovits
        if man.get("speed_factor"):
            entry["speed_factor"] = float(man["speed_factor"])
        voices[vid] = entry
        found += 1
        kind = "微调权重" if (gpt or sovits) else "零样本克隆"
        log("[models] + gsv/%s（%s）" % (vid, kind))
    return found


def _scan_rvc(root, voices, log):
    base = os.path.join(root, "models", RVC_DIR)
    if not os.path.isdir(base):
        return 0
    found = 0
    for vid in sorted(os.listdir(base)):
        folder = os.path.join(base, vid)
        if not os.path.isdir(folder) or vid.startswith("."):
            continue
        if not _first(folder, ".pth"):
            log("[models] 跳过 %s/rvc/%s：缺 .pth" % (RVC_DIR, vid))
            continue
        key = "rvc-" + vid
        if key in voices:
            continue
        man = _read_manifest(folder)
        voices[key] = {
            "name": man.get("name") or vid,
            "engine": "edge",
            "tts_voice": man.get("base_voice") or DEFAULT_BASE_VOICE,
            "rvc": vid,
            "pitch": man.get("pitch") or 0,
            "language": man.get("language") or "zh-CN",
            "desc": man.get("desc") or "RVC 音色转换",
        }
        found += 1
        log("[models] + rvc/%s" % vid)
    return found


def discover(cfg, root, log=print):
    """把 models/ 下的音色并入 cfg["voices"]，返回本次新增的数量。"""
    voices = cfg.setdefault("voices", {})
    try:
        gsv = _scan_gsv(root, voices, log)
        rvc = _scan_rvc(root, voices, log)
    except Exception as exc:  # 扫描失败不能拖垮服务启动
        log("[models] 扫描异常，已跳过: %s" % exc)
        return 0
    if gsv or rvc:
        log("[models] 自带音色 %d 个（gsv %d / rvc %d）" % (gsv + rvc, gsv, rvc))
    if rvc and not (cfg.get("rvc") or {}).get("enabled"):
        log("[models] 检测到 RVC 模型但 rvc.enabled=false，转换不会生效")
    return gsv + rvc
