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
                    "locations", "model_dirs", "engines", "engine_group",
                    "running_engine"}
            missing = need - set(data)
            assert not missing, "缺字段: %s" % ", ".join(sorted(missing))
            assert data["stack"], "stack 是空的"
            assert set(data["engine_group"]) == {"GSV", "RVC", "Higgs", "CosyVoice"}, \
                "互斥组不对: %s" % data["engine_group"]
            # 安装位置要覆盖四个引擎，少一个那个引擎就只能用默认目录
            dirs = set(data.get("model_dirs") or {})
            assert {"gsv", "rvc", "higgs", "cosy"} <= dirs, \
                "model_dirs 缺: %s" % ", ".join(sorted({"gsv", "rvc", "higgs", "cosy"} - dirs))
            # 端口只能来自 config：四个引擎都得有值，且和 stack 里的端口一致
            ports = data.get("engine_ports") or {}
            assert set(ports) == {"GSV", "RVC", "Higgs", "CosyVoice"}, "engine_ports 不全: %s" % ports
            alias = {"GSV": "gsv", "RVC": "rvc", "Higgs": "higgs", "CosyVoice": "cosy"}
            for name, port in ports.items():
                assert isinstance(port, int) and port > 0, "%s 端口异常: %r" % (name, port)
                assert port == data["stack"][alias[name]]["port"], \
                    "%s 的 engine_ports 和 stack 对不上" % name
            return "engine=%s voice=%s ports=%s" % (
                data["engine"], data["voice"], ",".join(str(p) for p in ports.values()))
        check("/v1/mode 字段完整", mode)

        def stack():
            _, data = call(base, "/v1/stack")
            need = {"voicehub", "gsv", "rvc", "higgs", "cosy"}
            got = set(data.get("stack") or {})
            assert need <= got, "服务状态不全: 缺 %s" % ", ".join(sorted(need - got))
            return "voicehub 端口 %s" % data["stack"]["voicehub"]["port"]
        check("/v1/stack 五个组件齐全", stack)

        def services():
            _, data = call(base, "/v1/services")
            names = set((data.get("services") or {}).keys())
            assert {"VoxRelay", "GSV", "RVC", "Higgs", "CosyVoice"} <= names, \
                "缺服务: %s" % names
            return " / ".join(sorted(names))
        check("/v1/services 正常", services)

        def engine_guard():
            # 四选一只能选互斥组内的引擎；主服务和未知名字都必须被拒，
            # 否则面板传错一个参数就把线上引擎全停了。
            for bad in ("VoxRelay", "NoSuchEngine"):
                try:
                    call(base, "/v1/engine", {"name": bad})
                except urllib.error.HTTPError as exc:
                    assert exc.code in (400, 500), "%s 应该被拒，实际 %d" % (bad, exc.code)
                    continue
                raise AssertionError("%s 竟然被接受了" % bad)
            return "主服务 / 未知引擎均被拒"
        check("/v1/engine 拒绝越界", engine_guard)

        def stack_guard():
            # 安装位置只认服务清单里的组件，别的一律拒——否则会往 stack.json
            # 里写没用的键，看着「设置了」其实永远不生效。
            # 只测拒绝分支，避免在仓库目录里真的写出 stack.json。
            try:
                call(base, "/v1/stack", {"component": "calc.exe", "path": ""})
            except urllib.error.HTTPError as exc:
                assert exc.code == 400, "应该 400，实际 %d" % exc.code
                return "未知组件被拒"
            raise AssertionError("未知组件竟然被接受了")
        check("/v1/stack 拒绝未知组件", stack_guard)

        def voice_add_guard():
            # 添加音色只认 higgs/cosy，别的引擎一律拒；空 body 也应给参数错误。
            # 只测拒绝分支，避免在仓库目录里真的写出 config.json。
            try:
                call(base, "/v1/voice", {"name": "x", "engine": "gsv",
                                         "ref_audio": "/nonexist.wav",
                                         "prompt_text": "hi"})
            except urllib.error.HTTPError as exc:
                assert exc.code == 400, "应该 400，实际 %d" % exc.code
                return "非 higgs/cosy 引擎被拒"
            raise AssertionError("gsv 引擎竟然被接受了")
        check("/v1/voice 拒绝非克隆引擎", voice_add_guard)

        def voice_delete_guard():
            # 删除只认 custom- 前缀，内置音色一律拒。
            try:
                call(base, "/v1/voice/delete", {"id": "xiaoxiao"})
            except urllib.error.HTTPError as exc:
                assert exc.code == 400, "应该 400，实际 %d" % exc.code
                return "内置音色删除被拒"
            raise AssertionError("内置音色竟然可删")
        check("/v1/voice/delete 拒删内置音色", voice_delete_guard)

        def long_page():
            code, body, _ctype = call(base, "/long", raw=True)
            assert code == 200 and "长文本".encode("utf-8") in body, "/long 页面异常"
            return "长文页可访问"
        check("/long 页面可访问", long_page)

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
