# -*- coding: utf-8 -*-
"""
adb 封装 —— 设备发现、命令执行、文件推送、错误归类。

要点：
  · su 命令【必须带超时】—— Magisk 弹授权框时 adb shell 会静默挂住，不报错
  · 所有命令返回 (rc, out, err)，不抛异常（除致命）
  · 自动定位 adb（PATH 找不到时扫常见安装位置）
"""
import os
import re
import shutil
import subprocess

from . import config as C


class AdbError(Exception):
    pass


# ── adb 定位 ────────────────────────────────────────────────────────
_ADB = None

_CANDIDATES = [
    r"C:\Program Files (x86)\platform-tools\adb.exe",
    r"C:\Program Files\platform-tools\adb.exe",
    r"C:\platform-tools\adb.exe",
    os.path.expanduser(r"~\platform-tools\adb.exe"),
    os.path.expanduser(r"~\AppData\Local\Android\Sdk\platform-tools\adb.exe"),
    "/usr/local/bin/adb",
    "/usr/bin/adb",
]


def find_adb():
    global _ADB
    if _ADB:
        return _ADB
    p = shutil.which("adb")
    if p:
        _ADB = p
        return _ADB
    for c in _CANDIDATES:
        if os.path.exists(c):
            _ADB = c
            return _ADB
    raise AdbError("找不到 adb —— 请安装 platform-tools 或把 adb 加进 PATH")


def adb_path():
    return find_adb()


# ── 执行 ────────────────────────────────────────────────────────────
def run(args, timeout=20, binary=False, serial=None):
    """
    跑一条 adb 命令。args 为参数列表（不含 'adb'）。
    返回 (rc, stdout, stderr)。rc = -9 表示超时。
    """
    cmd = [find_adb()]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)

    # Windows 下隐藏黑框
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000

    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, **kw)
        out = p.stdout if binary else p.stdout.decode("utf-8", "replace")
        err = p.stderr if binary else p.stderr.decode("utf-8", "replace")
        return p.returncode, out, err
    except subprocess.TimeoutExpired:
        return -9, (b"" if binary else ""), "TIMEOUT"
    except FileNotFoundError:
        raise AdbError("adb 可执行文件不存在: %s" % find_adb())
    except Exception as e:
        return -1, (b"" if binary else ""), str(e)


def shell(cmd, timeout=20, serial=None, su=False, raw=False):
    """
    执行 shell 命令。
      su=True  → adb shell su -c "<cmd>"
      raw=True → 不包引号（cmd 里已有完整命令）
    返回 (rc, stdout, stderr)
    """
    if su:
        # 用双引号包裹，内部双引号转义 —— 兼顾含空格/特殊字符的命令
        esc = cmd.replace('\\', '\\\\').replace('"', '\\"')
        args = ["shell", 'su -c "%s"' % esc]
    else:
        args = ["shell", cmd]
    return run(args, timeout=timeout, serial=serial)


def shell_script(script, timeout=20, serial=None, su=False):
    """
    在设备上执行一段【复杂脚本】。

    ★ 为什么不能直接 shell(su=True)：
      该封装会把命令包成 `su -c "<cmd>"` 并转义内部双引号，
      于是外层 sh 会【抢先展开】命令里的 $( ) 与 $var，
      su 收到的已经是残缺命令 —— 复杂脚本在这种引号嵌套下必然出错。
      （实测：pkill 脚本因此完全没执行，但也不报错）

    做法：本地 base64 → 设备上 base64 -d → 交给 sh 执行。
    这样脚本内容不经过任何一层 shell 解析，彻底避开引号地狱。
    """
    import base64
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    wrapper = "echo %s | base64 -d | sh" % b64
    if su:
        return shell(wrapper, timeout=timeout, serial=serial, su=True)
    return shell(wrapper, timeout=timeout, serial=serial, su=False)


def sh_out(cmd, timeout=20, serial=None, su=False):
    """只要 stdout 的便捷方法（去掉首尾空白）"""
    rc, out, err = shell(cmd, timeout=timeout, serial=serial, su=su)
    return (out or "").strip()


# ── 设备 ────────────────────────────────────────────────────────────
def devices():
    """
    返回 [{'serial':..., 'state':...}]，state ∈ device/unauthorized/offline
    """
    rc, out, err = run(["devices"], timeout=15)
    res = []
    for line in (out or "").splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            res.append({"serial": parts[0], "state": parts[1]})
    return res


def pick_device(cfg=None):
    """
    选设备：配置里指定了就用指定的；否则要求【恰好一台】在线。
    返回 serial，失败抛 AdbError（带可操作的原因）。
    """
    cfg = cfg or C.load()
    want = (cfg.get("serial") or "").strip()
    ds = devices()

    if want:
        for d in ds:
            if d["serial"] == want:
                if d["state"] != "device":
                    raise AdbError("指定设备 %s 状态为 %s（需为 device）" % (want, d["state"]))
                return want
        raise AdbError("未找到指定设备 %s；当前在线: %s"
                       % (want, ", ".join(d["serial"] for d in ds) or "无"))

    live = [d for d in ds if d["state"] == "device"]
    bad = [d for d in ds if d["state"] != "device"]

    if len(live) == 1:
        return live[0]["serial"]
    if not live:
        msg = "没有可用设备"
        if bad:
            msg += "；检测到 %s（%s）—— " % (
                ", ".join(d["serial"] for d in bad), bad[0]["state"])
            if bad[0]["state"] == "unauthorized":
                msg += "请在手机上确认 USB 调试授权"
            elif bad[0]["state"] == "offline":
                msg += "试试 adb kill-server 后重新插拔"
        else:
            msg += "；请确认 USB 已连接且已开启 USB 调试"
        raise AdbError(msg)
    raise AdbError("检测到多台在线设备（%s）—— 请用 --serial 指定，或写进 config.json"
                   % ", ".join(d["serial"] for d in live))


# ── 文件推送 ────────────────────────────────────────────────────────
def push(local, remote, timeout=120, serial=None):
    rc, out, err = run(["push", local, remote], timeout=timeout, serial=serial)
    return rc == 0, (out or "") + (err or "")


def forward_add(local_port, remote_port, serial=None):
    return run(["forward", "tcp:%d" % local_port, "tcp:%d" % remote_port],
               timeout=15, serial=serial)


def forward_remove(local_port, serial=None):
    return run(["forward", "--remove", "tcp:%d" % local_port], timeout=15, serial=serial)


def forward_list(serial=None):
    rc, out, err = run(["forward", "--list"], timeout=15, serial=serial)
    return out or ""


def reverse_add(phone_port, pc_port, serial=None):
    return run(["reverse", "tcp:%d" % phone_port, "tcp:%d" % pc_port],
               timeout=15, serial=serial)


def reverse_remove(phone_port, serial=None):
    return run(["reverse", "--remove", "tcp:%d" % phone_port], timeout=15, serial=serial)


def reverse_list(serial=None):
    rc, out, err = run(["reverse", "--list"], timeout=15, serial=serial)
    return out or ""


def reverse_all_remove(serial=None):
    return run(["reverse", "--remove-all"], timeout=15, serial=serial)


def forward_all_remove(serial=None):
    """注意：这会连带删掉别人建的 forward，慎用"""
    return run(["forward", "--remove-all"], timeout=15, serial=serial)


# ── 解析工具 ────────────────────────────────────────────────────────
def parse_forward_listing(text):
    """把 adb forward --list 输出解析成 {serial: [(local, remote), ...]}"""
    res = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            res.setdefault(parts[0], []).append((parts[1], parts[2]))
    return res


def has_mapping(text, serial, local, remote):
    for s, pairs in parse_forward_listing(text).items():
        if s == serial and (local, remote) in pairs:
            return True
    return False
