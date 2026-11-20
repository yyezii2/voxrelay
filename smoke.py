# -*- coding: utf-8 -*-
"""冒烟测试：起一个临时实例，把关键路径跑一遍。

    python smoke.py

只用标准库，不碰正在运行的服务（换端口 + 独立状态文件）。
退出码 0 = 全过，1 = 有失败；任何一项失败都会打印原因。
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
FAILS = []


def free_port(start=18787):
    """从 start 往上找第一个没人监听的端口，避免撞上默认的 8787。"""
    for port in range(start, start + 40):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到空闲端口")


def call(base, path, body=None, timeout=60, raw=False):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = resp.read()
        if raw:
            return resp.status, payload, resp.headers.get("Content-Type", "")
        return resp.status, json.loads(payload.decode("utf-8"))


def check(name, fn):
    try:
        detail = fn()
        print("  PASS  %s%s" % (name, ("  " + detail) if detail else ""))
    except Exception as exc:  # noqa: BLE001
        print("  FAIL  %s\n        %s" % (name, exc))
        FAILS.append(name)


def wait_ready(base, proc, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("服务启动时就退出了（退出码 %s），看上面的日志" % proc.returncode)
        try:
            call(base, "/health", timeout=2)
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    raise RuntimeError("等了 %d 秒服务还没起来" % timeout)


def main():
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    state = os.path.join(tempfile.gettempdir(), "voxrelay-smoke-state.json")
    if os.path.exists(state):
        os.remove(state)

    env = dict(os.environ)
    env["VOXRELAY_STATE"] = state

    print("起临时实例 http://127.0.0.1:%d" % port)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server.py"), "--port", str(port)],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        wait_ready(base, proc)
        print("服务已就绪，开始检查\n")

        check("/health 正常", lambda: (
            "%d 个音色" % call(base, "/health")[1]["voices"]))

        def voices():
            _, data = call(base, "/v1/audio/voices")
            items = data.get("voices") or []
            assert items, "音色列表是空的，检查 models/ 和 config.json"
            ready = [v for v in items if v.get("ready") is not False]
            assert ready, "所有音色都不可用，检查 edge-tts / SAPI"
            return "%d 个可用 / %d 个总计" % (len(ready), len(items))
        check("/v1/audio/voices 有可用音色", voices)

        def mode():
            _, data = call(base, "/v1/mode")
            need = {"enabled", "voice", "engine", "speed", "volume", "stack",
                    "locations", "model_dirs"}
            missing = need - set(data)
            assert not missing, "缺字段: %s" % ", ".join(sorted(missing))
            assert data["stack"], "stack 是空的"
            return "engine=%s voice=%s" % (data["engine"], data["voice"])
        check("/v1/mode 字段完整", mode)

        def stack():
            _, data = call(base, "/v1/stack")
            assert set(data.get("stack") or {}) == {"voicehub", "gsv", "rvc"}, \
                "三件套不全: %s" % data.get("stack")
            return "voicehub 端口 %s" % data["stack"]["voicehub"]["port"]
        check("/v1/stack 三件套齐全", stack)

        def services():
            _, data = call(base, "/v1/services")
            names = set((data.get("services") or {}).keys())
            assert {"VoxRelay", "GSV", "RVC"} <= names, "缺服务: %s" % names
            return " / ".join(sorted(names))
        check("/v1/services 正常", services)

        def rescan():
            _, data = call(base, "/v1/rescan", {}, timeout=120)
            assert data.get("total", 0) > 0, "重扫之后一个音色都没有"
            return "共 %d 个" % data["total"]
        check("/v1/rescan 正常", rescan)

        def speech():
            _, health = call(base, "/health")
            eng = health.get("engines") or {}
            if not (eng.get("edge") or eng.get("sapi")):
                return "跳过（没有可用引擎）"
            _, data = call(base, "/v1/audio/voices")
            voice = next((v["id"] for v in data["voices"] if v.get("ready") is not False), None)
            assert voice, "找不到可用音色"
            _, audio, ctype = call(base, "/v1/audio/speech",
                                   {"input": "冒烟测试", "voice": voice}, raw=True)
            assert len(audio) > 500, "音频只有 %d 字节，太小了" % len(audio)
            assert not ctype.startswith("application/json"), "返回的是 JSON，不是音频: %s" % ctype
            return "%s, %d 字节, %s" % (voice, len(audio), ctype.split(";")[0])
        check("/v1/audio/speech 能出声", speech)

        def reject_empty():
            try:
                call(base, "/v1/audio/speech", {"input": ""})
            except urllib.error.HTTPError as exc:
                assert exc.code == 400, "空文本应该返回 400，实际 %d" % exc.code
                return "正确拒绝"
            raise AssertionError("空文本居然被接受了")
        check("空文本被拒绝", reject_empty)

    finally:
        print()
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        if os.path.exists(state):
            try:
                os.remove(state)
            except OSError:
                pass
        # terminate 在 Windows 上是强制终止，服务来不及自己清理 endpoint.json。
        # 注意 os.remove 必须放在 with 外面：文件还开着的时候 Windows 删不掉。
        ep = os.path.join(ROOT, "endpoint.json")
        try:
            same = False
            with open(ep, encoding="utf-8") as fh:
                same = json.load(fh).get("port") == port
            if same:
                os.remove(ep)
        except Exception:  # noqa: BLE001
            pass

    # 服务日志只在失败时有用
    if FAILS and out:
        print("---- 服务日志 ----")
        print(out[-3000:])

    if FAILS:
        print("失败 %d 项：%s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    sys.exit(main())
