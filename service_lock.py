# -*- coding: utf-8 -*-
"""跨进程的服务操作锁。

面板、MCP（set_voice_mode）、/v1/service、以及手动跑的脚本，几个入口都会启停服务。
两个入口同时动就会抢端口 —— 典型场景：面板上点「停止」，嘴上同时说了「打开 mod1」。
这把锁把启停串起来，保证同一时刻只有一个入口在操作进程。

实现：排他创建锁文件（O_CREAT|O_EXCL），跨进程有效，纯标准库。
锁文件里写持有者 pid，用于判断「是崩溃残留还是真有人在操作」：
  - pid 已死 → 立刻清掉重试
  - 文件超过 STALE_SEC 没动 → 也当残留（pid 可能被复用）
  - 等满 timeout 仍拿不到 → **降级放行**：锁本身有问题时，宁可退回原来的行为，
    也不能把语音系统卡死。返回值告诉调用方这轮有没有真的互斥。

不要给 svc_launcher 加锁：它由计划任务拉起，而计划任务通常是持锁的 mcp_server
触发的 —— 子进程再去抢同一把锁就会干等。
"""
import os
import time
from contextlib import contextmanager

ROOT = os.path.dirname(os.path.abspath(__file__))
LOCK_PATH = os.path.join(ROOT, "services.lock")
STALE_SEC = 300


def _pid_alive(pid):
    """进程还活着吗。判断不了就当活着（宁可多等，也别误删别人的锁）。"""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return True
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001
        return True


def _holder():
    try:
        with open(LOCK_PATH, "r", encoding="ascii", errors="replace") as fh:
            parts = fh.read().split()
        return int(parts[0]) if parts else 0
    except Exception:  # noqa: BLE001
        return 0


def _stale():
    try:
        return time.time() - os.path.getmtime(LOCK_PATH) > STALE_SEC
    except OSError:
        return False


def _clear():
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


@contextmanager
def service_lock(tag="", timeout=180):
    """with service_lock("start:GSV") as locked: ...

    locked=False 表示等超时后降级放行 —— 这轮没有互斥，但操作照常进行。
    """
    fd = None
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            try:
                os.write(fd, ("%d %s %s" % (
                    os.getpid(), str(tag)[:40], time.strftime("%H:%M:%S"))).encode("ascii", "replace"))
            except OSError:
                pass
            break
        except FileExistsError:
            holder = _holder()
            if (holder and not _pid_alive(holder)) or _stale():
                _clear()
                continue
            if time.time() >= deadline:
                break  # 降级：不阻塞用户，只是这轮没有互斥
            time.sleep(0.2)
        except OSError:
            break  # 锁文件所在目录不可写之类：直接放行
    try:
        yield fd is not None
    finally:
        # 只删自己建的锁；降级放行时 fd 是 None，绝不能去删别人的
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            _clear()
