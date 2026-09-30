# -*- coding: utf-8 -*-
"""
链路编排 —— up / down / status。

链路两跳（全程 USB，不依赖局域网）：
  上行  手机 App → 127.0.0.1:pc_mitm_port
              └─ adb reverse ─→ 电脑:pc_mitm_port （MITM 在此）
  下行  电脑 MITM → 127.0.0.1:phone_proxy_port
              └─ adb forward ─→ 手机:phone_proxy_port （MiniProxy）
                                    └─ 裸 socket 出站 → VPN/内网目标

为什么两跳都走 USB：
  · 手机挂 VPN 时，回电脑的路由常被 tun0 抢走（实测踩过）
  · 电脑在公网、手机在内网时，局域网那一跳根本不成立
  · 两跳都走环回+USB ⇒ 与网络拓扑完全解耦
"""
import io
import os
import json
import time

from . import ui
from . import adbutil as A
from . import device as D
from . import config as C
from . import doctor as DOC

STATE_FILE = os.path.join(C.OUT, "state.json")


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with io.open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(st):
    try:
        with io.open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def clear_state():
    try:
        os.remove(STATE_FILE)
    except Exception:
        pass


# ── up ──────────────────────────────────────────────────────────────
def up(cfg=None, skip_doctor=False, with_watch=False):
    cfg = cfg or C.load()
    pp = int(cfg["phone_proxy_port"])
    mp = int(cfg["pc_mitm_port"])

    ui.head("建立链路")

    # ── 0. 前置自检 ──
    if not skip_doctor:
        ui.info("先跑环境自检（--skip-doctor 可跳过）")
        rep = DOC.run(cfg, quiet=True)
        if not rep.healthy:
            ui.fail("硬前置未通过，已中止。跑 `link.py doctor` 看详情")
            for i in rep.hard_fails:
                ui.hint("%s —— %s" % (i.title, i.detail))
            return 1
        serial = rep.serial
        ui.ok("自检通过", "设备 %s" % serial)
        for i in rep.soft_fails:
            ui.warn("%s" % i.title, i.detail)
    else:
        serial = A.pick_device(cfg)

    st = {"serial": serial, "phone_proxy_port": pp, "pc_mitm_port": mp,
          "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
          "started": [], "prev_proxy": None}
    steps = []

    # ── 1. 记录原代理设置（以便 down 时还原）──
    ph, pport = D.get_proxy(serial)
    st["prev_proxy"] = ("%s:%s" % (ph, pport)) if ph else None
    if ph:
        ui.info("手机原有系统代理: %s:%s（down 时会还原）" % (ph, pport))
    else:
        ui.info("手机原无系统代理设置")

    # ── 2. 推送 + 部署手机侧代理 ──
    ui.sub("部署手机侧代理")
    dex = os.path.join(C.ASSETS, "miniproxy.dex")
    if not os.path.exists(dex):
        ui.die("找不到 %s —— assets 目录不完整" % dex)

    ok, out = A.push(dex, C.PHONE_DEX, timeout=int(cfg["t_push"]), serial=serial)
    ui.ok("推送 dex", C.PHONE_DEX) if ok else ui.fail("推送 dex 失败", out[:120])
    if not ok:
        return 1
    steps.append("push_dex")

    ok, msg = D.write_run_script(serial, pp)
    ui.ok("写入守护脚本", C.PHONE_RUN) if ok else ui.fail("写脚本失败", msg)
    if not ok:
        return 1
    steps.append("write_script")

    D.proxy_log_clear(serial)
    ok, msg = D.start_proxy(serial, pp)
    if ok:
        ui.ok("拉起 MiniProxy", "端口 %d" % pp)
        steps.append("start_proxy")
    else:
        ui.fail("拉起失败", msg)

    # 等端口起来
    alive = False
    for _ in range(12):
        time.sleep(1)
        alive, det = D.proxy_alive(serial, pp)
        if alive:
            break
    if alive:
        ui.ok("手机侧代理已监听", ":%d" % pp)
    else:
        ui.fail("手机侧代理未监听 :%d" % pp)
        ui.hint("看日志：link.py logs")
        tail = D.proxy_log_tail(serial, 15)
        if tail:
            ui.hint("日志尾部: " + tail.replace("\n", " | ")[:300])
        return 1

    # ── 3. adb forward（电脑→手机，下行）──
    ui.sub("建立 USB 两跳")
    A.forward_remove(pp, serial=serial)
    rc, out, err = A.forward_add(pp, pp, serial=serial)
    fl = A.forward_list(serial=serial)
    if "tcp:%d" % pp in fl:
        ui.ok("adb forward", "tcp:%d → 手机 tcp:%d（下行，电脑→手机）" % (pp, pp))
        steps.append("forward")
    else:
        ui.fail("adb forward 失败", (err or out or "").strip()[:120])
        return 1

    # ── 4. adb reverse（手机→电脑，上行）──
    A.reverse_remove(mp, serial=serial)
    rc, out, err = A.reverse_add(mp, mp, serial=serial)
    rl = A.reverse_list(serial=serial)
    if "tcp:%d" % mp in rl:
        ui.ok("adb reverse", "手机 tcp:%d → 电脑 tcp:%d（上行，手机→电脑）" % (mp, mp))
        steps.append("reverse")
    else:
        ui.fail("adb reverse 失败", (err or out or "").strip()[:120])
        ui.hint("该 ROM 可能裁剪了 adb reverse。退化：把手机代理指向【电脑局域网 IP:%d】" % mp)
        return 1

    # ── 5. 设置手机系统代理 ──
    ui.sub("设置手机系统代理")
    got = D.set_proxy("127.0.0.1", mp, serial)
    if got and got[0] == "127.0.0.1" and got[1] == mp:
        ui.ok("系统代理已设为", "127.0.0.1:%d  （经 adb reverse 到电脑）" % mp)
        steps.append("set_proxy")
    else:
        ui.fail("设置失败", "当前读回 %s" % (got,))
        ui.hint("可手动：adb shell settings put global http_proxy 127.0.0.1:%d" % mp)
        return 1
    ui.info("先设代理再启动 App。App 启动时读一次系统代理，之后不重读。")

    # ── 6. 端到端验证 ──
    ui.sub("端到端验证")
    mitm = (cfg.get("mitm_name") or "MITM").strip() or "MITM"
    if D.pc_port_free(mp)[0]:
        ui.warn("PC 侧 :%d 当前无监听" % mp, "手机发来的请求会失败")
        ui.hint("%s：监听端口设为 %d；上游/下游代理指向 127.0.0.1:%d" % (mitm, mp, pp))
    else:
        ui.ok("PC 侧有监听", ":%d" % mp)
        ui.hint("确认这个监听就是 %s，且它的上游代理指向 127.0.0.1:%d" % (mitm, pp))

    st["started"] = steps
    st["state"] = "up"
    save_state(st)

    ui.head("链路已建立")
    ui.kv("设备", serial)
    ui.kv("上行（手机→电脑）", "手机 127.0.0.1:%d  --adb reverse-->  电脑 :%d" % (mp, mp))
    ui.kv("下行（电脑→手机）", "电脑 127.0.0.1:%d  --adb forward-->  手机 :%d" % (pp, pp))
    ui.kv("手机系统代理", "127.0.0.1:%d" % mp)
    ui.kv("MITM", "%s @ 电脑 :%d" % (mitm, mp))
    ui.kv("出口", "手机侧 MiniProxy 以 root 出站 → VPN/内网")
    print()
    ui.info("收工：link.py down")
    if with_watch:
        ui.info("看门狗启动（前台，Ctrl+C 退出）")
        from . import watchdog
        watchdog.run(cfg, serial)
    return 0


# ── down ────────────────────────────────────────────────────────────
def down(cfg=None, keep_proxy=None):
    cfg = cfg or C.load()
    pp = int(cfg["phone_proxy_port"])
    mp = int(cfg["pc_mitm_port"])
    st = load_state()

    ui.head("拆除链路")
    try:
        serial = st.get("serial") or A.pick_device(cfg)
    except A.AdbError as e:
        ui.fail("取设备失败", str(e))
        serial = st.get("serial")

    if not serial:
        ui.fail("无法确定设备，仅尝试清理本地状态")
        clear_state()
        return 1

    # ── 1. 系统代理 ──
    keep = cfg["keep_proxy_on_down"] if keep_proxy is None else keep_proxy
    if keep:
        ui.skip("保留手机系统代理设置（keep_proxy_on_down=true）")
    else:
        prev = st.get("prev_proxy")
        # ★ 陷阱：若"上次 up 之前的值"恰好就是本工具设的那个值
        #   （连续 up/down 时会发生：上一次 down 没清干净、或 keep 过），
        #   再"还原"就等于什么都没做，代理会一直留着。
        #   ⇒ 只有原值【不等于】本工具目标值时才还原，否则一律清除。
        our_value = "127.0.0.1:%d" % mp
        if prev and ":" in prev and prev != our_value:
            h, _, p = prev.rpartition(":")
            try:
                D.set_proxy(h, int(p), serial)
                ui.ok("已还原手机系统代理", prev)
            except Exception:
                D.clear_proxy(serial)
                ui.ok("已清除手机系统代理")
        else:
            if prev == our_value:
                ui.info("原值即本工具所设（%s），按清除处理" % our_value)
            D.clear_proxy(serial)
            got = D.get_proxy(serial)
            if got and got[0]:
                ui.warn("手机系统代理未清干净", "当前仍为 %s:%s" % got)
            else:
                ui.ok("已清除手机系统代理")

    # ── 2. forward / reverse ──
    A.reverse_remove(mp, serial=serial)
    ui.ok("已移除 adb reverse", "tcp:%d" % mp)
    # ★ 不删别人的 forward —— 用 --remove 精确删自己那条
    A.forward_remove(pp, serial=serial)
    ui.ok("已移除 adb forward", "tcp:%d" % pp)

    # ── 3. 手机侧代理进程 ──
    ok_stop = D.stop_proxy(serial, pp)
    if ok_stop:
        ui.ok("已停止手机侧代理进程")
    else:
        still, det = D.proxy_alive(serial, pp)
        ui.warn("手机侧代理仍在监听 :%d" % pp, det[:80])
        ui.hint("守护脚本会自动重启，已尝试先杀守护再杀进程；可手动：link.py logs 看状态")

    # ── 4. 本地状态 ──
    clear_state()
    ui.done("链路已拆除")
    return 0


# ── status ──────────────────────────────────────────────────────────
def status(cfg=None):
    cfg = cfg or C.load()
    pp = int(cfg["phone_proxy_port"])
    mp = int(cfg["pc_mitm_port"])
    st = load_state()

    ui.head("链路状态")
    try:
        serial = st.get("serial") or A.pick_device(cfg)
    except A.AdbError as e:
        ui.fail("取设备失败", str(e))
        return 1
    ui.kv("设备", serial)
    if st.get("ts"):
        ui.kv("建立于", st["ts"])

    # 手机侧代理
    alive, det = D.proxy_alive(serial, pp)
    (ui.ok if alive else ui.fail)("手机侧代理 :%d" % pp, det[:80] if alive else "未监听")

    # forward
    fl = A.forward_list(serial=serial)
    has_f = ("tcp:%d" % pp) in fl
    (ui.ok if has_f else ui.fail)("adb forward tcp:%d" % pp, "下行 电脑→手机")

    # reverse
    rl = A.reverse_list(serial=serial)
    has_r = ("tcp:%d" % mp) in rl
    (ui.ok if has_r else ui.fail)("adb reverse tcp:%d" % mp, "上行 手机→电脑")

    # 手机系统代理
    ph, pport = D.get_proxy(serial)
    if ph:
        good = (ph == "127.0.0.1" and pport == mp)
        (ui.ok if good else ui.warn)("手机系统代理", "%s:%s" % (ph, pport))
    else:
        ui.warn("手机系统代理", "未设置")

    # PC 侧 MITM
    free, det2 = D.pc_port_free(mp)
    if free:
        ui.fail("PC 侧 MITM :%d" % mp, "无监听 —— 请启动 MITM")
    else:
        ui.ok("PC 侧 MITM :%d" % mp, "在监听")

    # 路由
    target = (cfg.get("route_target") or "").strip()
    if target:
        dev, raw = D.route_get(serial, target)
        ui.kv("路由 → %s" % target, "%s" % (dev or "无结果"))

    print()
    okall = alive and has_f and has_r and bool(ph) and not free
    if okall:
        ui.done("链路完整")
    else:
        ui.warn("链路不完整 —— 跑 `link.py up` 重建，或 `link.py heal` 尝试自愈")
    return 0 if okall else 1


# ── heal ────────────────────────────────────────────────────────────
def heal(cfg=None, verbose=True):
    """
    自愈：把断掉的组件补回来。幂等，可反复跑。
    返回 (fixed[], still_broken[])
    """
    cfg = cfg or C.load()
    pp = int(cfg["phone_proxy_port"])
    mp = int(cfg["pc_mitm_port"])
    st = load_state()
    fixed, broken = [], []

    try:
        serial = st.get("serial") or A.pick_device(cfg)
    except A.AdbError as e:
        return [], ["设备不可用: %s" % e]

    # 1. 手机侧代理
    alive, _ = D.proxy_alive(serial, pp)
    if not alive:
        D.start_proxy(serial, pp)
        for _ in range(8):
            time.sleep(1)
            alive, _ = D.proxy_alive(serial, pp)
            if alive:
                break
        (fixed if alive else broken).append("手机侧代理" + ("已重启" if alive else "重启失败"))

    # 2. forward
    fl = A.forward_list(serial=serial)
    if ("tcp:%d" % pp) not in fl:
        A.forward_remove(pp, serial=serial)
        A.forward_add(pp, pp, serial=serial)
        fl = A.forward_list(serial=serial)
        ok = ("tcp:%d" % pp) in fl
        (fixed if ok else broken).append("adb forward" + ("已重建" if ok else "重建失败"))

    # 3. reverse
    rl = A.reverse_list(serial=serial)
    if ("tcp:%d" % mp) not in rl:
        A.reverse_remove(mp, serial=serial)
        A.reverse_add(mp, mp, serial=serial)
        rl = A.reverse_list(serial=serial)
        ok = ("tcp:%d" % mp) in rl
        (fixed if ok else broken).append("adb reverse" + ("已重建" if ok else "重建失败"))

    # 4. 系统代理
    ph, pport = D.get_proxy(serial)
    if not (ph == "127.0.0.1" and pport == mp):
        got = D.set_proxy("127.0.0.1", mp, serial)
        ok = got and got[0] == "127.0.0.1"
        (fixed if ok else broken).append("手机系统代理" + ("已重设" if ok else "重设失败"))

    if verbose:
        ui.head("自愈结果")
        if fixed:
            for x in fixed:
                ui.ok(x)
        if broken:
            for x in broken:
                ui.fail(x)
        if not fixed and not broken:
            ui.done("链路本来就是完整的，无需修复")
    return fixed, broken
