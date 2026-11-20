# -*- coding: utf-8 -*-
r"""把本仓库注册成 DSH 插件（或者撤掉）。

DSH 的插件不是"装进程序目录"，而是写进一个 profile 清单：
  ~/.dsh/profiles/desktop/package.json
    dependencies:  "voxrelay": "link:D:/某处/voxrelay"
    dsh.profile.bundles: [ ..., "voxrelay" ]
再让 node_modules/voxrelay 指向仓库，宿主就能 require 到它。
包自己带 cordis.patch.yml，插进侧边栏面板。

用法：
  python dsh_link.py status                 # 看现在挂没挂
  python dsh_link.py register               # 挂上（会自动备份清单）
  python dsh_link.py unregister             # 撤掉
  python dsh_link.py register --profile D:\...\.dsh\profiles\desktop

只碰这三样：profile 的 package.json、node_modules 里的链接、旧版面板的残留项。
不动服务、不动 DSH 本体。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

NAME = "voxrelay"
LEGACY_NAMES = ("@local/voicehub-panel", "voicehub-panel")
HERE = os.path.dirname(os.path.abspath(__file__))
IS_WIN = os.name == "nt"


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- profile 定位

def find_profile(explicit=None):
    """按 explicit > 环境变量 > 默认 desktop > profiles 里唯一的那个 找 profile。"""
    cands = []
    if explicit:
        cands.append(explicit)
    if os.environ.get("DSH_PROFILE"):
        cands.append(os.environ["DSH_PROFILE"])
    home = os.path.expanduser("~")
    root = os.path.join(home, ".dsh", "profiles")
    cands.append(os.path.join(root, "desktop"))
    for p in cands:
        if os.path.isfile(os.path.join(p, "package.json")):
            return os.path.abspath(p)
    if os.path.isdir(root):
        subs = [os.path.join(root, d) for d in sorted(os.listdir(root))]
        subs = [d for d in subs if os.path.isfile(os.path.join(d, "package.json"))]
        if len(subs) == 1:
            return subs[0]
    raise SystemExit(
        "找不到 DSH profile。用 --profile 指定，例如：\n"
        r"  python dsh_link.py register --profile %USERPROFILE%\.dsh\profiles\desktop")


# ---------------------------------------------------------------- 链接识别

def is_link(path):
    """软链或目录联接（junction）都算链接；真实目录不算。"""
    if not os.path.exists(path) and not os.path.islink(path):
        return False
    if os.path.islink(path):
        return True
    if hasattr(os.path, "isjunction") and os.path.isjunction(path):
        return True
    try:
        # IO_REPARSE_TAG_MOUNT_POINT —— 老解释器没有 isjunction 时的兜底
        return getattr(os.lstat(path), "st_reparse_tag", 0) == 0xA000000C
    except OSError:
        return False


def link_target(path):
    try:
        return os.path.realpath(path)
    except OSError:
        return ""


def make_link(link_path, target):
    """先试真软链，失败再退回目录联接（junction 不需要管理员权限）。"""
    os.makedirs(os.path.dirname(link_path), exist_ok=True)
    try:
        os.symlink(target, link_path, target_is_directory=True)
        return "symlink"
    except OSError:
        pass
    if not IS_WIN:
        raise SystemExit("建软链失败：%s -> %s" % (link_path, target))
    cmdline = 'mklink /J "%s" "%s"' % (link_path, target)
    r = subprocess.run(cmdline, shell=True, capture_output=True)
    if r.returncode != 0:
        raise SystemExit("建目录联接失败：%s\n%s" % (
            cmdline, (r.stdout + r.stderr).decode("gbk", "replace")))
    return "junction"


def drop_link(path):
    if not is_link(path):
        return False
    try:
        os.unlink(path)          # 软链
    except OSError:
        os.rmdir(path)           # junction
    return True


# ---------------------------------------------------------------- 清单读写

def read_manifest(profile):
    path = os.path.join(profile, "package.json")
    with open(path, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise SystemExit("%s 不是 JSON 对象，先手工修好再来" % path)
    return data


def write_manifest(profile, data):
    path = os.path.join(profile, "package.json")
    backup = "%s.bak-voxrelay-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return backup


def posix_path(path):
    return os.path.abspath(path).replace("\\", "/")


def bundles_of(data):
    dsh = data.setdefault("dsh", {})
    if not isinstance(dsh, dict):
        raise SystemExit("profile 里的 dsh 字段不是对象，先手工修好再来")
    prof = dsh.setdefault("profile", {})
    if not isinstance(prof, dict):
        raise SystemExit("profile 里的 dsh.profile 不是对象，先手工修好再来")
    arr = prof.setdefault("bundles", [])
    if not isinstance(arr, list):
        raise SystemExit("dsh.profile.bundles 不是数组，先手工修好再来")
    return arr


def dep_spec():
    return "link:" + posix_path(HERE)


def legacy_links(profile):
    out = []
    for legacy in LEGACY_NAMES:
        if legacy.startswith("@"):
            p = os.path.join(profile, "node_modules", *legacy.split("/"))
            if os.path.isdir(p) or is_link(p):
                out.append(p)
    return out


# ---------------------------------------------------------------- 动作

def cmd_status(profile):
    data = read_manifest(profile)
    deps = data.get("dependencies") or {}
    bundles = bundles_of(data)
    link = os.path.join(profile, "node_modules", NAME)
    spec = deps.get(NAME)
    log("profile      %s" % profile)
    log("依赖         %s" % (spec or "（没有）"))
    log("bundles      %s" % ("有" if NAME in bundles else "（没有）"))
    log("node_modules %s" % (
        ("%s -> %s" % ("链接", link_target(link))) if is_link(link)
        else ("（没有）" if not os.path.exists(link) else "真实目录")))
    if spec or NAME in bundles:
        ok = bool(spec) and NAME in bundles and is_link(link) and \
            link_target(link).lower().rstrip("\\/") == os.path.abspath(HERE).lower().rstrip("\\/")
        log("结论         %s" % ("已挂上" if ok else "挂了一半，重新 register 一次"))
        return 0 if ok else 1
    log("结论         没挂")
    return 1


def cmd_register(profile):
    data = read_manifest(profile)
    deps = data.setdefault("dependencies", {})
    if not isinstance(deps, dict):
        raise SystemExit("profile 里的 dependencies 不是对象，先手工修好再来")
    bundles = bundles_of(data)

    for legacy in LEGACY_NAMES:
        if legacy in deps:
            log("清掉旧依赖   %s" % legacy)
            deps.pop(legacy, None)

    # 旧面板的插入项换成本包的，位置不变（bundle 层顺序会影响加载先后）
    replaced = False
    for i, item in enumerate(bundles):
        if item in LEGACY_NAMES:
            bundles[i] = NAME
            replaced = True
    if NAME not in bundles:
        bundles.append(NAME)

    deps[NAME] = dep_spec()
    backup = write_manifest(profile, data)
    log("备份清单     %s" % backup)
    log("写入依赖     %s = %s" % (NAME, deps[NAME]))
    log("写入 bundles %s%s" % ("（替换旧项）" if replaced else "（追加）", NAME))

    for legacy in legacy_links(profile):
        if drop_link(legacy):
            log("清掉旧链接   %s" % legacy)

    link = os.path.join(profile, "node_modules", NAME)
    if is_link(link):
        if link_target(link).lower().rstrip("\\/") == os.path.abspath(HERE).lower().rstrip("\\/"):
            log("链接已就绪   %s" % link)
        else:
            drop_link(link)
            log("重建链接     %s（原来指向别处）" % link)
    if not os.path.exists(link):
        kind = make_link(link, HERE)
        log("建立链接     %s -> %s（%s）" % (link, HERE, kind))

    log("")
    log("完成。重启 DSH 后侧边栏出现面板。")
    log("（DSH 只在启动时读 profile，不重启看不到变化。）")
    return 0


def cmd_unregister(profile):
    data = read_manifest(profile)
    deps = data.get("dependencies") or {}
    bundles = bundles_of(data)
    changed = False
    if NAME in deps:
        deps.pop(NAME, None)
        changed = True
    while NAME in bundles:
        bundles.remove(NAME)
        changed = True
    if changed:
        backup = write_manifest(profile, data)
        log("备份清单     %s" % backup)
        log("已从清单移除 %s" % NAME)
    else:
        log("清单里本来就没有 %s" % NAME)

    link = os.path.join(profile, "node_modules", NAME)
    if drop_link(link):
        log("已删链接     %s" % link)
    log("")
    log("完成。重启 DSH 后面板消失；服务本身不受影响。")
    log("仓库目录还在：%s" % HERE)
    return 0


def main():
    ap = argparse.ArgumentParser(description="把 VoxRelay 注册成 DSH 插件")
    ap.add_argument("action", choices=["status", "register", "unregister"])
    ap.add_argument("--profile", default=None, help=r"profile 目录，默认 %USERPROFILE%\.dsh\profiles\desktop")
    args = ap.parse_args()
    profile = find_profile(args.profile)
    if args.action == "status":
        return cmd_status(profile)
    if args.action == "register":
        return cmd_register(profile)
    return cmd_unregister(profile)


if __name__ == "__main__":
    # 不要强行把 stdout 改成 UTF-8：Windows 控制台/管道各自有编码，
    # 改了反而会让 PowerShell 按 GBK 解码时变成乱码。
    sys.exit(main())
