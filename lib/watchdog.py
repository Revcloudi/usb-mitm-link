# -*- coding: utf-8 -*-
"""
看门狗 —— 周期性巡检链路，掉线自动修复。

为什么必须有：
  实测中链路两次无故中断，每次都要人工排查。根因各不相同（VPN 抢路由 / adb forward 丢失），
  但现象都是"App 打不开"。有了看门狗，这类问题从"人工排查"变成"自动恢复 + 日志留痕"。
"""
import os
import io
import sys
import time

from . import ui
from . import config as C
from . import adbutil as A
from . import device as D
from . import linkmgr as L

LOG = os.path.join(C.OUT, "watchdog.log")


def _log(msg):
    try:
        with io.open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def check_once(cfg, serial):
    """
    巡检一次。返回 (healthy: bool, problems: [str])
    只做判定，不修复。
    """
    pp = int(cfg["phone_proxy_port"])
    mp = int(cfg["pc_mitm_port"])
    problems = []

    # 设备还在吗
    try:
        live = [d for d in A.devices() if d["state"] == "device"]
        if not any(d["serial"] == serial for d in live):
            return False, ["设备离线（USB 断开？）"]
    except Exception as e:
        return False, ["adb 异常: %s" % str(e)[:60]]

    # 手机侧代理
    alive, _ = D.proxy_alive(serial, pp)
    if not alive:
        problems.append("手机侧代理未监听 :%d" % pp)

    # forward
    if ("tcp:%d" % pp) not in A.forward_list(serial=serial):
        problems.append("adb forward 丢失")

    # reverse
    if ("tcp:%d" % mp) not in A.reverse_list(serial=serial):
        problems.append("adb reverse 丢失")

    # 系统代理
    ph, pport = D.get_proxy(serial)
    if not (ph == "127.0.0.1" and pport == mp):
        problems.append("手机系统代理异常(%s:%s)" % (ph, pport))

    return (len(problems) == 0), problems


def run(cfg=None, serial=None, interval=None, auto_heal=None):
    cfg = cfg or C.load()
    interval = int(interval or cfg["watch_interval"])
    heal_on = cfg["auto_heal"] if auto_heal is None else auto_heal

    if not serial:
        st = L.load_state()
        serial = st.get("serial")
    if not serial:
        try:
            serial = A.pick_device(cfg)
        except A.AdbError as e:
            ui.die("看门狗启动失败: %s" % str(e))

    ui.head("看门狗运行中")
    ui.kv("设备", serial)
    ui.kv("巡检间隔", "%ds" % interval)
    ui.kv("自动修复", "开" if heal_on else "关")
    ui.kv("日志", LOG)
    print()
    ui.info("Ctrl+C 退出")

    bad_streak = 0
    try:
        while True:
            ok, problems = check_once(cfg, serial)
            ts = time.strftime("%H:%M:%S")
            if ok:
                if bad_streak:
                    print("  %s %s 链路已恢复正常" % (ui.gry(ts), ui.grn("OK")))
                    _log("recovered")
                else:
                    sys.stdout.write("\r  %s %s 链路正常          " % (ts, ui.grn("OK")))
                    sys.stdout.flush()
                bad_streak = 0
            else:
                bad_streak += 1
                print()
                print("  %s %s 异常: %s" % (ts, ui.red("!!"), "; ".join(problems)))
                _log("problem: %s" % "; ".join(problems))
                if heal_on:
                    fixed, broken = L.heal(cfg, verbose=False)
                    if fixed:
                        print("  %s    已修复: %s" % (" " * len(ts), "; ".join(fixed)))
                        _log("healed: %s" % "; ".join(fixed))
                    if broken:
                        print("  %s    修复失败: %s" % (" " * len(ts), "; ".join(broken)))
                        _log("heal_failed: %s" % "; ".join(broken))
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        ui.info("看门狗已停止")
    return 0
