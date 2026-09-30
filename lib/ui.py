# -*- coding: utf-8 -*-
"""
终端输出工具 —— 状态标记、颜色、表格。

设计原则：
  · 只用标准库
  · Windows 下自动开启 ANSI（否则颜色是乱码）
  · 不依赖 colorama
"""
import os
import sys
import ctypes

# ── 控制台初始化 ────────────────────────────────────────────────────
_INITED = None


def setup_console():
    """
    Windows 中文乱码修复：
      · 把控制台代码页切成 65001(UTF-8)
      · 把 Python 的 stdout/stderr 也切成 UTF-8
    两者必须同时做 —— 只改一边仍然乱码，因为"Python 发 UTF-8 字节、
    控制台按 GBK 解释"（或反之）照样对不上。
    """
    global _INITED
    if _INITED is not None:
        return _INITED
    _INITED = True

    if os.name == "nt":
        try:
            k = ctypes.windll.kernel32
            k.SetConsoleOutputCP(65001)
            k.SetConsoleCP(65001)
        except Exception:
            pass

    for stream in ("stdout", "stderr"):
        s = getattr(sys, stream, None)
        if s is None:
            continue
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    _enable_ansi()
    return True


# ── 颜色 ────────────────────────────────────────────────────────────
_ENABLED = None


def _enable_ansi():
    """Windows 10+ 需要显式开启 VT 处理，否则转义序列会打成乱码。"""
    global _ENABLED
    if _ENABLED is not None:
        return _ENABLED
    if os.name != "nt":
        _ENABLED = True
        return _ENABLED
    try:
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            _ENABLED = False
            return _ENABLED
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        k.SetConsoleMode(h, mode.value | 0x0004)
        _ENABLED = True
    except Exception:
        _ENABLED = False
    return _ENABLED


class C:
    R = "\033[0m"
    B = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GRN = "\033[32m"
    YEL = "\033[33m"
    BLU = "\033[34m"
    MAG = "\033[35m"
    CYN = "\033[36m"
    GRY = "\033[90m"


def _c(code, s):
    return code + s + C.R if _enable_ansi() else s


def red(s):   return _c(C.RED, s)
def grn(s):   return _c(C.GRN, s)
def yel(s):   return _c(C.YEL, s)
def blu(s):   return _c(C.BLU, s)
def cyn(s):   return _c(C.CYN, s)
def mag(s):   return _c(C.MAG, s)
def gry(s):   return _c(C.GRY, s)
def bold(s):  return _c(C.B, s)
def dim(s):   return _c(C.DIM, s)


# ── 状态标记 ────────────────────────────────────────────────────────
OK = "[ OK ]"
FAIL = "[FAIL]"
WARN = "[WARN]"
SKIP = "[SKIP]"
INFO = "[INFO]"


def ok(msg, extra=""):
    print("  %s %s%s" % (grn(OK), msg, ("  " + gry(extra)) if extra else ""))


def fail(msg, extra=""):
    print("  %s %s%s" % (red(FAIL), msg, ("  " + gry(extra)) if extra else ""))


def warn(msg, extra=""):
    print("  %s %s%s" % (yel(WARN), msg, ("  " + gry(extra)) if extra else ""))


def skip(msg, extra=""):
    print("  %s %s%s" % (gry(SKIP), msg, ("  " + gry(extra)) if extra else ""))


def info(msg):
    print("  %s %s" % (blu(INFO), msg))


def hint(msg):
    """修复建议 —— 缩进 + 暗色"""
    print("         %s %s" % (gry("→"), gry(msg)))


def head(title, width=78):
    print()
    print(bold(cyn("─" * width)))
    print(bold(cyn("  " + title)))
    print(bold(cyn("─" * width)))


def sub(title):
    print()
    print(bold("  " + title))


def kv(k, v, width=26):
    print("      %-*s %s" % (width, k, v))


def done(msg=""):
    print()
    print(grn(bold("  ✔ " + msg)) if msg else "")


def die(msg, code=1):
    print()
    print(red(bold("  ✘ " + msg)))
    sys.exit(code)


def step(i, total, msg):
    print("  %s %s" % (gry("[%d/%d]" % (i, total)), msg))
