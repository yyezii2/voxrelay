# -*- coding: utf-8 -*-
"""服务进程管理：查状态、启停、重启、自愈。

被 server.py（面板接口）和 mcp_server.py（MCP 工具）共用，
保证面板上点「启动」和 MCP 里说「打开语音」走的是同一套逻辑。

GSV / RVC / Higgs / CosyVoice 都是可选组件：没装就报「未安装」，不影响主服务本身。
四个引擎都吃显存，同一时刻只允许一个在跑（见 switch）。
"""
import json
import os
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
IS_WIN = os.name == "nt"

# 和 mcp_server 共用同一把跨进程锁：面板、MCP、/v1/service 的启停互相串行，
# 避免「面板点停止」和「打开 mod1」同时进来抢端口。锁模块不可用时降级为不加锁。
try:
    from service_lock import service_lock
except Exception:  # noqa: BLE001
    from contextlib import contextmanager

    @contextmanager
    def service_lock(tag="", timeout=180):
        yield False

# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
# 让子进程脱离当前控制台，父进程退出（或终端关掉）不会把它带走。
DETACHED = (0x00000008 | 0x00000200) if IS_WIN else 0


def _env(name):
    """读环境变量：VOXRELAY_<name> 优先，回退 VOICEHUB_<name> 兼容老配置。"""
    return os.environ.get("VOXRELAY_" + name) or os.environ.get("VOICEHUB_" + name) or ""


def _first_existing(*cands):
    for cand in cands:
        p = cand if os.path.isabs(cand) else os.path.join(ROOT, cand)
        if os.path.exists(p):
            return p
    return sys.executable


# GSV / RVC 要跑 torch，一般单独一个 3.10 环境；跑本服务用轻量解释器即可。
PY310 = _env("PY310") or os.path.join(ROOT, "rvc", "py310", "python.exe")
PYRT = _env("PYRT") or _first_existing(
    os.path.join("runtime", "python.exe"),
    os.path.join(".venv", "Scripts", "python.exe"),
)

# Higgs / CosyVoice 跑在各自的 venv 里（torch 版本和 GSV 那套不一定一致，
# 而且它们的依赖比 GSV 轻得多，混用一个环境容易互相顶掉）。
PY_HIGGS = _env("HIGGS_PY") or os.path.join(ROOT, "higgs", "venv", "Scripts", "python.exe")
PY_COSY = _env("COSY_PY") or os.path.join(ROOT, "cosyvoice", "venv", "Scripts", "python.exe")


# ---------------------------------------------------------------- 端口从 config 来
# 端口只在一处定义（config.json），这里读出来还原成服务的监听参数。
# 以前三个端口在 server.py / mcp_server.py / 这个文件里各写一遍，
# 用户改 config 只改到一处，结果服务还往老端口起、引擎却去连新端口。

def _load_cfg():
    """和 server.py 同一套回落顺序，保证两边看到同一个端口。"""
    for name in ("config.json", "config.example.json"):
        p = os.path.join(ROOT, name)
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8-sig") as fh:
                d = json.load(fh)
            return d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            continue
    return {}


def _endpoint(url, fallback_host, fallback_port):
    """从 http://host:port/xxx 里取出 (host, port)，取不到就用默认值。"""
    try:
        u = urlparse(url or "")
        return u.hostname or fallback_host, int(u.port or fallback_port)
    except Exception:  # noqa: BLE001
        return fallback_host, fallback_port


_CFG = _load_cfg()
GSV_HOST, GSV_PORT = _endpoint(
    ((_CFG.get("engines") or {}).get("gsv") or {}).get("base_url"), "127.0.0.1", 9880)
RVC_HOST, RVC_PORT = _endpoint(
    (_CFG.get("rvc") or {}).get("worker"), "127.0.0.1", 8790)
HIGGS_HOST, HIGGS_PORT = _endpoint(
    ((_CFG.get("engines") or {}).get("higgs") or {}).get("base_url"), "127.0.0.1", 8791)
COSY_HOST, COSY_PORT = _endpoint(
    ((_CFG.get("engines") or {}).get("cosy") or {}).get("base_url"), "127.0.0.1", 8789)

# 四个吃显存的引擎，同一时刻只允许一个在跑。
# 主服务是路由器，自己不占显存，不在组内。
GPU_GROUP = ("GSV", "RVC", "Higgs", "CosyVoice")
SELF_PORT = int(_CFG.get("port") or 8787)

SERVICES = [
    {
        "name": "GSV",
        "port": GSV_PORT,
        "cwd": os.path.join(ROOT, "gsv"),
        "cmd": [PY310, "run_api.py", "-a", GSV_HOST, "-p", str(GSV_PORT),
                "-c", "GPT_SoVITS/configs/tts_infer.yaml"],
        "entry_name": "run_api.py",
        "entry": os.path.join(ROOT, "gsv", "run_api.py"),
        "extra_path": os.path.join(ROOT, "bin"),
        "optional": True,
        "self": False,
    },
    {
        "name": "VoxRelay",
        "port": SELF_PORT,
        "cwd": ROOT,
        "cmd": [PYRT, "server.py"],
        "entry_name": "server.py",
        "entry": os.path.join(ROOT, "server.py"),
        "extra_path": None,
        "optional": False,
        "self": True,
    },
    {
        "name": "RVC",
        "port": RVC_PORT,
        "cwd": ROOT,
        "cmd": [PY310, os.path.join("rvc", "worker.py"),
                "--host", RVC_HOST, "--port", str(RVC_PORT),
                "--models", os.path.join(ROOT, "models", "rvc")],
        "entry_name": "worker.py",
        "entry": os.path.join(ROOT, "rvc", "worker.py"),
        "extra_path": None,
        "optional": True,
        "self": False,
    },
    {
        "name": "Higgs",
        "port": HIGGS_PORT,
        "cwd": os.path.join(ROOT, "higgs"),
        # worker 只认 --port（监听 127.0.0.1），和 GSV/RVC 的 --host 不同
        "cmd": [PY_HIGGS, "worker.py", "--port", str(HIGGS_PORT)],
        "entry_name": "worker.py",
        "entry": os.path.join(ROOT, "higgs", "worker.py"),
        "extra_path": None,
        "optional": True,
        "self": False,
    },
    {
        "name": "CosyVoice",
        "port": COSY_PORT,
        "cwd": os.path.join(ROOT, "cosyvoice"),
        "cmd": [PY_COSY, "worker.py", "--port", str(COSY_PORT)],
        "entry_name": "worker.py",
        "entry": os.path.join(ROOT, "cosyvoice", "worker.py"),
        "extra_path": None,
        "optional": True,
        "self": False,
        # stack.json 里的键。服务名是 CosyVoice，但 config 和面板都用 cosy，
        # 不显式指定的话 resolve() 会按名字推成 "cosyvoice"，两边永远对不上，
        # 结果就是「设置了安装位置但不生效」。
        "stack_key": "cosy",
    },
]


def stack_key(svc):
    """这个服务在 stack.json 里的键。"""
    return svc.get("stack_key") or svc["name"].lower()


# 允许写进 stack.json 的组件名，直接由服务清单推出来，加引擎不用再改 server。
STACK_COMPONENTS = tuple(stack_key(s) for s in SERVICES if not s.get("self"))


# ---------------------------------------------------------------- 安装位置
# stack.json 存用户自选的 GSV / RVC 目录：发布出去时没人会把 8G 的底模塞进仓库，
# 都是解压在自己盘上，这里指过去就行。

def _stack_path():
    return os.path.join(ROOT, "stack.json")


def _stack():
    try:
        with open(_stack_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def resolve(svc):
    """返回 (cwd, entry)。用户给这个组件指定过目录就用那个目录。"""
    loc = _stack().get(stack_key(svc))
    if loc and os.path.isdir(loc):
        return loc, os.path.join(loc, svc.get("entry_name") or "")
    return svc["cwd"], svc["entry"]


def command(svc):
    """实际执行的命令。

    用户把 GSV 整合包解压到别的盘时，解释器也要用那个包里自带的，
    否则仓库里那个 rvc/py310 解释器还是不存在，指了目录也起不来。
    """
    cmd = list(svc["cmd"])
    cwd, _ = resolve(svc)
    if cwd != svc["cwd"]:
        for cand in ("runtime/python.exe", "python.exe",
                     os.path.join("py310", "python.exe"), "venv/Scripts/python.exe"):
            p = os.path.join(cwd, *cand.replace("\\", "/").split("/"))
            if os.path.exists(p):
                cmd[0] = p
                break
    return cmd


def _short(path):
    try:
        return os.path.relpath(path, ROOT)
    except ValueError:
        # 跨盘符时 relpath 会抛异常，退回原样显示
        return path


def get(name):
    for svc in SERVICES:
        if svc["name"].lower() == str(name).lower():
            return svc
    raise KeyError("未知服务: %s" % name)


def set_self_port(port):
    """主服务端口以运行时实际监听为准。

    用户把 config.json 的 port 改成 9000 之后，面板不该还去盯 8787——
    那上面可能是别的程序，于是「自己」被显示成别人的状态。
    """
    for svc in SERVICES:
        if svc.get("self"):
            svc["port"] = int(port)


# ---------------------------------------------------------------- 状态与就绪

def port_open(port, timeout=0.15):
    """端口是否在监听。

    timeout 别调大：本地回环上未监听的端口要么立刻 RST，要么被防火墙静默丢弃，
    后者会一直阻塞到超时。面板每次刷新要探测 5 个端口，0.4 秒 ×5 就是 2 秒空白。
    0.15 秒足够本地回环往返（实测 <1ms）。
    """
    try:
        with socket.socket() as s:
            s.settimeout(timeout)
            return s.connect_ex(("127.0.0.1", int(port))) == 0
    except Exception:  # noqa: BLE001
        return False


def ready(svc):
    """能不能起来：(True, "") / (False, 缺什么)。

    可选组件的缺件原因统一报「未安装」——面板上一长串「缺 rvc/py310/python.exe」
    对只想用 edge 音色的人来说是噪音。
    """
    exe = command(svc)[0]
    if os.path.isabs(exe) and not os.path.exists(exe):
        return False, "未安装" if svc.get("optional") else "缺解释器 " + _short(exe)
    cwd, entry = resolve(svc)
    if entry and not os.path.exists(entry):
        return False, "未安装" if svc.get("optional") else "缺 " + _short(entry)
    if not os.path.isdir(cwd):
        return False, "未安装" if svc.get("optional") else "缺目录 " + _short(cwd)
    return True, ""


def status():
    out = {}
    for svc in SERVICES:
        ok, why = ready(svc)
        out[svc["name"]] = {
            "installed": ok,
            # 没装却端口有响应，那是别人的进程，不能算「我们的服务在跑」
            "running": bool(ok and port_open(svc["port"])),
            "port": svc["port"],
            "optional": bool(svc.get("optional")),
            "reason": "" if ok else why,
        }
    return out


# ---------------------------------------------------------------- 启停

def _start_impl(name, wait=90):
    svc = get(name)
    ok, why = ready(svc)
    if not ok:
        return {"ok": False, "name": svc["name"], "error": why}
    if port_open(svc["port"]):
        return {"ok": True, "name": svc["name"], "note": "已在运行"}

    env = dict(os.environ)
    if svc.get("extra_path"):
        env["PATH"] = svc["extra_path"] + os.pathsep + env.get("PATH", "")
    cwd, _ = resolve(svc)
    kwargs = {"creationflags": DETACHED} if IS_WIN else {"start_new_session": True}
    try:
        subprocess.Popen(
            command(svc), cwd=cwd, env=env,
            close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **kwargs
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "name": svc["name"], "error": str(exc)}

    deadline = time.time() + wait
    while time.time() < deadline:
        if port_open(svc["port"]):
            return {"ok": True, "name": svc["name"], "note": "已启动"}
        time.sleep(1)
    return {"ok": False, "name": svc["name"], "error": "启动超时"}


def start(name, wait=90):
    """对外的启动入口：先拿锁再执行。锁等超时会降级放行，不拦住操作。"""
    with service_lock("start:%s" % name):
        return _start_impl(name, wait)


def _pids_on(port):
    if not IS_WIN:
        return set()
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             timeout=15).stdout.decode("gbk", "replace")
    except Exception:  # noqa: BLE001
        return set()
    pids = set()
    needle = ":%d " % int(port)
    for line in out.splitlines():
        if needle in line and "LISTENING" in line:
            parts = line.split()
            if parts and parts[-1].isdigit():
                pids.add(parts[-1])
    return pids


def _stop_impl(name, wait=20):
    svc = get(name)
    if not IS_WIN:
        return {"ok": False, "name": svc["name"], "error": "当前系统不支持进程管理"}
    ok, why = ready(svc)
    if not ok:
        # 没装就绝不能按端口杀：那个端口上可能是别的程序，
        # 或者用户机器上另一份同款服务，一按停止就把人家的进程干掉。
        return {"ok": True, "name": svc["name"], "note": "未安装，无需停止"}
    pids = _pids_on(svc["port"])
    if not pids:
        return {"ok": True, "name": svc["name"], "note": "本来就没跑"}
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=20)
        except Exception:  # noqa: BLE001
            pass
    # 必须等端口真的关掉，否则紧接着 start() 会抢不到端口、启动失败，
    # 而 start() 又会把「旧进程还在监听」误判成启动成功。
    deadline = time.time() + wait
    while time.time() < deadline:
        if not port_open(svc["port"]):
            return {"ok": True, "name": svc["name"], "note": "已停止"}
        time.sleep(0.5)
    return {"ok": False, "name": svc["name"], "error": "端口 %d 未释放" % svc["port"]}


def stop(name, wait=20):
    """对外的停止入口：先拿锁再执行。

    停止前把意图写成 off，否则自愈会在 15 秒后把刚停的服务又拉回来
    （面板上点「停止」却停不掉，就是这个原因）。
    """
    svc = get(name)
    with service_lock("stop:%s" % svc["name"]):
        set_intent(svc["name"], False)
        return _stop_impl(svc["name"], wait)


def restart(name, wait=90):
    """重启只在最外层拿一次锁：内部走 _impl，否则会自己等自己（锁不可重入）。"""
    with service_lock("restart:%s" % name):
        _stop_impl(name)
        time.sleep(0.5)
        return _start_impl(name, wait=wait)


# ---------------------------------------------------------------- 四选一互斥
# 四个吃显存的引擎同一时刻只允许一个在跑。切换时先停其他、等端口释放，
# 再启目标——宁可切换慢几秒，也不让两个模型同时抢 16GB 显存。

def gpu_free_gb():
    """剩余显存（GB）。读不到返回 None，不阻断切换。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6,
            creationflags=0x08000000 if IS_WIN else 0,  # CREATE_NO_WINDOW
        )
        line = (out.stdout or "").strip().splitlines()[0]
        return round(int(line.strip()) / 1024.0, 1)
    except Exception:  # noqa: BLE001
        return None


def running_engines():
    """互斥组里当前在跑的服务名（正常情况最多一个）。"""
    return [s["name"] for s in SERVICES
            if s["name"] in GPU_GROUP and port_open(s["port"])]


def switch(name, wait=120):
    """四选一：启动目标引擎，并把同组其余服务全部停掉。

    返回 {ok, name, stopped, stop_failed, free_gb, free_gb_before_start, note|error}。
    注意互斥是硬性的：即使目标启动失败，其他引擎也已经停了，
    调用方拿到 ok=False 时要自己决定切回哪个。
    """
    target = get(name)
    if target.get("self"):
        return {"ok": False, "name": target["name"], "error": "主服务自身不参与四选一"}
    if target["name"] not in GPU_GROUP:
        return {"ok": False, "name": target["name"], "error": "不在互斥组内"}

    with service_lock("switch:%s" % target["name"]):
        # 意图必须最先落盘。启动一个引擎要 7~40 秒，在这段时间里自愈线程
        # （每 15 秒一轮）如果读到旧意图，就会把刚停掉的引擎拉回来——
        # 表现为「切过去之后过了一会儿，原来那个又自己起来了」。
        for gname in GPU_GROUP:
            set_intent(gname, gname == target["name"])

        stopped = []
        failed = []
        for other in SERVICES:
            if other["name"] in GPU_GROUP and other["name"] != target["name"]:
                if port_open(other["port"]):
                    res = _stop_impl(other["name"])
                    (stopped if res.get("ok") else failed).append(other["name"])

        # 等整组都空下来（_stop_impl 各自等过端口，这里兜底一次总确认）
        deadline = time.time() + 20
        while time.time() < deadline and running_engines():
            time.sleep(0.5)

        free = gpu_free_gb()
        res = _start_impl(target["name"], wait=wait)
        # 兜底清扫：切换期间可能有外部动作（计划任务、面板手点、上一轮自愈）
        # 又把别的引擎拉起，这里再停一次，保证返回时组内只剩目标一个。
        for other in SERVICES:
            if (other["name"] in GPU_GROUP and other["name"] != target["name"]
                    and port_open(other["port"])):
                sweep = _stop_impl(other["name"])
                if sweep.get("ok") and other["name"] not in stopped:
                    stopped.append(other["name"])
        res["stopped"] = stopped
        res["stop_failed"] = failed
        res["free_gb"] = gpu_free_gb()
        res["free_gb_before_start"] = free
        return res


# ---------------------------------------------------------------- 自愈意图
# services.json 记录「用户是否希望它跑着」：面板上点停止 → off，点启动 → on。
# 自愈只拉回「本来标记为 on、之前确实在跑、现在掉了」的服务，
# 不会把用户主动关掉的东西又偷偷打开。

def _intent_path():
    return os.path.join(ROOT, "services.json")


def read_intent():
    try:
        with open(_intent_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def write_intent(d):
    try:
        with open(_intent_path(), "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


def set_intent(name, on):
    d = read_intent()
    d[get(name)["name"]] = "on" if on else "off"
    write_intent(d)
    return d


def heal_once(state):
    """跑一轮自愈检查。state 是跨轮共享的 dict，记录上次是否在线、上次尝试时间。"""
    intent = read_intent()
    healed = []
    now = time.time()

    # 互斥守护：组内只能有一个在跑，多出来的一律停掉，保留 intent=on 的那个。
    # 这样即使还有旧版入口（没重启的 MCP、手写的 bat）把全套拉起来，
    # 也会在 15 秒内被纠正，不用等所有调用方都升级。
    live = running_engines()
    if len(live) > 1:
        keep = next((n for n in live if intent.get(n) == "on"), live[0])
        for other in live:
            if other == keep:
                continue
            res = _stop_impl(other)
            healed.append({"name": other, "ok": res.get("ok"),
                           "note": "互斥：为 %s 让出显存" % keep})

    for svc in SERVICES:
        name = svc["name"]
        if svc.get("self"):
            continue
        ok, _ = ready(svc)
        if not ok:
            continue
        if port_open(svc["port"]):
            state[name] = {"was_up": True}
            continue
        last = state.setdefault(name, {"was_up": False, "last_try": 0.0})
        if intent.get(name, "on") != "on":
            continue
        # 互斥组里已经有别的引擎在跑 → 不自愈，否则两个会同时占显存，
        # 16GB 的卡上 Higgs（9.3GB）和 GSV（5.8GB）一撞就是 OOM。
        if name in GPU_GROUP and [n for n in running_engines() if n != name]:
            continue
        if not last.get("was_up"):
            continue
        # 60 秒内只重试一次，避免进程一崩就疯狂拉起、把日志刷爆
        if now - float(last.get("last_try") or 0) < 60:
            continue
        last["last_try"] = now
        last["was_up"] = False
        res = start(name, wait=45)
        healed.append({"name": name, "ok": res.get("ok"),
                       "note": res.get("note") or res.get("error")})
    return healed
