# -*- coding: utf-8 -*-
"""
环境自检（doctor）—— 本工具的核心。

设计依据（都是实际踩过的坑）：
  · su 弹窗时 adb shell 静默挂住 ⇒ 必须带超时，且要能区分"超时=弹窗"与"无 root"
  · 证书装错区（用户区而非系统区）⇒ 现象是"App 打不开"，极易误判成网络故障
  · adb reverse 可能被 ROM 阉割 ⇒ 这是本方案的核心动作，必须前置探测
  · tun0 抢路由 ⇒ 现象同样是"App 打不开"
  · 这些故障的共同特征：现象会伪装成别的问题，而不是干净报错

返回一个 Report 对象，包含各项结果与总体结论。
"""
import os
import time

from . import ui
from . import adbutil as A
from . import device as D
from . import config as C


class Item(object):
    def __init__(self, key, title, level="hard"):
        self.key = key
        self.title = title
        self.level = level        # hard=硬前置 / soft=自检 / info
        self.status = "skip"      # ok / fail / warn / skip
        self.detail = ""
        self.hint = ""

    def as_dict(self):
        return {"key": self.key, "title": self.title, "level": self.level,
                "status": self.status, "detail": self.detail, "hint": self.hint}


class Report(object):
    def __init__(self):
        self.items = []
        self.serial = None
        self.t0 = time.time()

    def add(self, item):
        self.items.append(item)
        return item

    @property
    def hard_fails(self):
        return [i for i in self.items if i.level == "hard" and i.status == "fail"]

    @property
    def soft_fails(self):
        return [i for i in self.items if i.level != "hard" and i.status in ("fail", "warn")]

    @property
    def healthy(self):
        return not self.hard_fails

    def elapsed(self):
        return time.time() - self.t0


# ── 单项检查 ────────────────────────────────────────────────────────
def _chk_adb(rep, cfg):
    it = rep.add(Item("adb", "adb 可用", "hard"))
    try:
        p = A.adb_path()
        rc, out, err = A.run(["version"], timeout=10)
        ver = (out or "").splitlines()[0].strip() if out else ""
        it.status = "ok"
        it.detail = "%s  |  %s" % (p, ver[:40])
    except A.AdbError as e:
        it.status = "fail"
        it.detail = str(e)
        it.hint = "安装 Android platform-tools，或把 adb 所在目录加入 PATH"
    return it


def _chk_device(rep, cfg):
    it = rep.add(Item("device", "设备在线（USB 调试已授权）", "hard"))
    try:
        ds = A.devices()
    except A.AdbError as e:
        it.status = "fail"
        it.detail = str(e)
        it.hint = "先修复 adb"
        return it

    if not ds:
        it.status = "fail"
        it.detail = "adb devices 为空"
        it.hint = "确认 USB 已连接、已开启【USB 调试】；必要时 adb kill-server 后重插"
        return it

    live = [d for d in ds if d["state"] == "device"]
    unauth = [d for d in ds if d["state"] == "unauthorized"]
    offline = [d for d in ds if d["state"] == "offline"]

    if live:
        try:
            rep.serial = A.pick_device(cfg)
            it.status = "ok"
            it.detail = "%s  （在线 %d 台）" % (rep.serial, len(live))
        except A.AdbError as e:
            it.status = "fail"
            it.detail = str(e)
            it.hint = "用 --serial <序列号> 指定，或写进 config.json 的 serial 字段"
    elif unauth:
        it.status = "fail"
        it.detail = "%s 状态 unauthorized" % unauth[0]["serial"]
        it.hint = "在手机屏幕上点【允许 USB 调试】，并勾选【一律允许】"
    elif offline:
        it.status = "fail"
        it.detail = "%s 状态 offline" % offline[0]["serial"]
        it.hint = "adb kill-server && adb devices；或重新插拔 USB"
    return it


def _chk_root(rep, cfg):
    it = rep.add(Item("root", "ROOT 可用，且 su 【免交互】", "hard"))
    if not rep.serial:
        it.status = "skip"
        it.detail = "无在线设备，跳过"
        return it

    state, detail = D.check_root(rep.serial, timeout=int(cfg["t_su"]))
    if state == "ok":
        it.status = "ok"
        it.detail = detail
    elif state == "prompt":
        it.status = "fail"
        it.detail = "su 在 %ss 内无响应（超时）" % cfg["t_su"]
        it.hint = ("手机上很可能弹出了 root 授权框 —— 点【允许】并务必勾选【永久记住】。"
                   "★ 这是最隐蔽的失败：adb shell 不报错，只会静默挂住")
    elif state == "noroot":
        it.status = "fail"
        it.detail = detail
        it.hint = "该机未 root，或 su 不在 PATH。本工具在挂 VPN 场景下【必须 root】"
    elif state == "denied":
        it.status = "fail"
        it.detail = detail
        it.hint = "Magisk 里把 shell 的授权改为【允许】"
    else:
        it.status = "warn"
        it.detail = detail
    return it


def _chk_app_process(rep, cfg):
    it = rep.add(Item("app_process", "app_process 可用（免装 APK 起代理）", "hard"))
    if not rep.serial:
        it.status = "skip"
        return it
    ok, detail = D.has_app_process(rep.serial)
    it.status = "ok" if ok else "fail"
    it.detail = detail or "未找到 app_process"
    if not ok:
        it.hint = "极罕见；可改用其他方式在手机侧起代理"
    return it


def _chk_tmp(rep, cfg):
    it = rep.add(Item("tmp", "/data/local/tmp 可写", "hard"))
    if not rep.serial:
        it.status = "skip"
        return it
    ok, detail = D.tmp_writable(rep.serial)
    it.status = "ok" if ok else "fail"
    it.detail = detail or "不可写"
    if not ok:
        it.hint = "尝试 adb shell su -c 'chmod 777 /data/local/tmp'"
    return it


def _chk_reverse(rep, cfg):
    """
    ★ 本方案的核心动作。部分 ROM 阉割了 adb reverse，必须实测。
    """
    it = rep.add(Item("reverse", "adb reverse 可用（本方案核心动作）", "hard"))
    if not rep.serial:
        it.status = "skip"
        return it

    probe = int(cfg["reverse_probe_port"])
    A.reverse_remove(probe, serial=rep.serial)
    rc, out, err = A.reverse_add(probe, probe, serial=rep.serial)
    listing = A.reverse_list(serial=rep.serial)
    A.reverse_remove(probe, serial=rep.serial)

    if "tcp:%d" % probe in (listing or "") and "tcp:%d" % probe in (listing or "").replace("tcp:%d" % probe, "", 1):
        it.status = "ok"
        it.detail = "支持（已实测建立并清理）"
    elif "tcp:%d" % probe in (listing or ""):
        it.status = "ok"
        it.detail = "支持"
    else:
        it.status = "fail"
        it.detail = ("建立失败 rc=%s %s" % (rc, (err or out or "").strip()[:80])).strip()
        it.hint = ("该 ROM 可能裁剪了 adb reverse。退化方案：上行改走局域网 —— "
                   "把手机系统代理指向【电脑的局域网 IP:端口】，并要求手机与电脑同网段")
    return it


def _chk_cert(rep, cfg):
    """
    证书位置检查。系统区 vs 用户区差别很大：
      targetSdk >= 24 的 App 默认【不信任用户区 CA】

    若 config 里给了 ca_pem，进一步核对【就是这张证书】——
    换 MITM（Yakit ↔ Burp）时最容易踩的坑：旧证书还在系统区，
    于是"系统区有证书"检查通过，但新 MITM 的证书根本没装。
    """
    mitm = (cfg.get("mitm_name") or "MITM").strip() or "MITM"
    it = rep.add(Item("cert", "%s 证书在【系统证书区】" % mitm, "soft"))
    if not rep.serial:
        it.status = "skip"
        return it

    sys_certs = D.list_system_certs(rep.serial)
    usr_certs = D.list_user_certs(rep.serial)
    pem = (cfg.get("ca_pem") or "").strip()

    # ── 指定了具体证书：核对它本人在不在系统区 ──
    if pem:
        if not os.path.exists(pem):
            it.status = "warn"
            it.detail = "ca_pem 指向的文件不存在: %s" % pem
            it.hint = "检查路径，或 link.py config --set ca_pem=  清空后只看总数"
            return it
        h = D.cert_subject_hash(rep.serial, pem)
        if not h:
            it.status = "warn"
            it.detail = "算不出证书 hash（需要 openssl）"
            it.hint = "装 openssl，或先清空 ca_pem 只看系统区总数"
            return it
        target = "%s.0" % h
        if target in sys_certs:
            it.status = "ok"
            it.detail = "已装 %s（%s）" % (target, os.path.basename(pem))
        elif target in usr_certs:
            it.status = "fail"
            it.detail = "只装在【用户证书区】(%s)" % target
            it.hint = ("targetSdk>=24 的 App 不信任用户区 CA，现象是 App 打不开。"
                       "需把 %s 装进 /system/etc/security/cacerts/" % target)
        else:
            it.status = "fail"
            it.detail = "系统区和用户区都没有 %s" % target
            it.hint = ("系统区现有 %d 张，但不是这一张 —— "
                       "换了 MITM 没装新证书的典型情况" % len(sys_certs))
        return it

    # ── 没指定证书：只能看总数 ──
    if sys_certs:
        it.status = "ok"
        it.detail = "系统区 %d 张" % len(sys_certs)
        if usr_certs:
            it.detail += "；用户区另有 %d 张（对 targetSdk>=24 的 App 无效）" % len(usr_certs)
        it.hint = ("只说明系统区有证书，不保证是 %s 那张。"
                   "想精确核对：config --set ca_pem=<CA 的 PEM 路径>" % mitm)
    elif usr_certs:
        it.status = "fail"
        it.detail = "只在【用户证书区】找到 %d 张，系统区为空" % len(usr_certs)
        it.hint = ("targetSdk>=24 的 App 不信任用户区 CA，现象是 App 打不开。"
                   "需把证书装进系统区"
                   "（Magisk 模块 / 挂载到 /system/etc/security/cacerts）")
    else:
        it.status = "warn"
        it.detail = "两个区都没找到证书"
        it.hint = "确认 %s 的 CA 已安装；Android 14+ 还要看 /apex/com.android.conscrypt/cacerts" % mitm
    return it


def _chk_ports(rep, cfg):
    it = rep.add(Item("ports", "PC 侧端口空闲", "hard"))
    busy = []
    for name, port in (("phone_proxy_port", int(cfg["phone_proxy_port"])),
                       ("pc_mitm_port", int(cfg["pc_mitm_port"]))):
        free, detail = D.pc_port_free(port)
        if not free:
            busy.append("%s:%d" % (name, port))
    if busy:
        it.status = "warn"
        it.detail = "被占用: %s" % ", ".join(busy)
        it.hint = ("pc_mitm_port 被占用通常是 MITM 已在跑（正常）；"
                   "phone_proxy_port 被占用说明链路可能已建立，可先 status 看看")
    else:
        it.status = "ok"
        it.detail = "phone_proxy_port=%s  pc_mitm_port=%s 均空闲" % (
            cfg["phone_proxy_port"], cfg["pc_mitm_port"])
    return it


def _chk_network(rep, cfg):
    it = rep.add(Item("network", "手机网络可达 + 路由正确", "soft"))
    if not rep.serial:
        it.status = "skip"
        return it

    target = (cfg.get("route_target") or "").strip()
    dr = D.default_route(rep.serial)
    ifaces = D.vpn_ifaces(rep.serial)
    vpn = [i for i in ifaces if i.startswith(("tun", "ppp", "tap"))]

    parts = []
    if vpn:
        parts.append("VPN 接口: %s" % ", ".join(vpn))
    else:
        parts.append("未发现 VPN 接口")

    if target:
        dev, raw = D.route_get(rep.serial, target)
        if dev:
            parts.append("→ %s 走 %s" % (target, dev))
            if vpn and dev not in vpn:
                it.status = "warn"
                it.detail = "; ".join(parts)
                it.hint = ("目标是内网地址但路由没走 VPN 接口 —— 出站会失败。"
                           "★ 注意 root(uid 0) 与普通 App 的路由表可能不同，"
                           "本工具的手机侧代理以 root 运行")
                return it
            ok, detail = D.ping(rep.serial, target)
            if not ok:
                it.status = "warn"
                it.detail = "; ".join(parts) + "; ping 不通"
                it.hint = "确认 VPN 已连接且目标是白名单内地址（有些目标禁 ping，可用 tcp_probe 再判）"
                return it
            parts.append("ping 通")
            it.status = "ok"
        else:
            it.status = "warn"
            parts.append("route get %s 无结果" % target)
            it.hint = "换一个确定可达的目标，或清空 config.json 的 route_target 跳过此项"
    else:
        it.status = "skip"
        parts.append("未配置 route_target，跳过目标可达性")
        it.hint = "在 config.json 里设 route_target=<内网IP或域名> 可启用本项检查"

    # 默认路由异常也算信号
    if not dr:
        parts.append("无默认路由!")
        it.status = "fail"
        it.hint = "手机没网"

    it.detail = "; ".join(parts)
    return it


def _chk_residue(rep, cfg):
    """检查是否有上次残留（forward/reverse/代理进程）"""
    it = rep.add(Item("residue", "无上次残留的链路", "info"))
    if not rep.serial:
        it.status = "skip"
        return it

    fl = A.forward_list(serial=rep.serial)
    rl = A.reverse_list(serial=rep.serial)
    pp = int(cfg["phone_proxy_port"])
    rp = int(cfg["pc_mitm_port"])

    res = []
    if "tcp:%d" % pp in fl:
        res.append("forward:%d" % pp)
    if "tcp:%d" % rp in rl:
        res.append("reverse:%d" % rp)
    alive, _ = D.proxy_alive(rep.serial, pp)
    if alive:
        res.append("手机侧代理在跑")

    if res:
        it.status = "warn"
        it.detail = "发现残留: %s" % ", ".join(res)
        it.hint = "可先执行 down 清理，或直接 up（会覆盖）"
    else:
        it.status = "ok"
        it.detail = "干净"
    return it


CHECKS = [
    _chk_adb,
    _chk_device,
    _chk_root,
    _chk_app_process,
    _chk_tmp,
    _chk_reverse,
    _chk_cert,
    _chk_ports,
    _chk_network,
    _chk_residue,
]


# ── 执行 + 打印 ─────────────────────────────────────────────────────
def run(cfg=None, quiet=False):
    cfg = cfg or C.load()
    rep = Report()

    if not quiet:
        ui.head("环境自检 (doctor)")

    for fn in CHECKS:
        try:
            fn(rep, cfg)
        except Exception as e:
            it = rep.add(Item(fn.__name__, fn.__name__, "info"))
            it.status = "fail"
            it.detail = "检查抛异常: %s" % str(e)[:120]

    if not quiet:
        _print_report(rep, cfg)

    _write_report(rep, cfg)
    return rep


def _print_report(rep, cfg):
    hard = [i for i in rep.items if i.level == "hard"]
    soft = [i for i in rep.items if i.level != "hard"]

    if hard:
        ui.sub("硬前置")
        for i in rep.items:
            if i.level != "hard":
                continue
            _print_item(i)

    if soft:
        ui.sub("自检项")
        for i in rep.items:
            if i.level == "hard":
                continue
            _print_item(i)

    ui.head("结论")
    if rep.healthy:
        ui.done("硬前置全部通过，可以 up")
        if rep.soft_fails:
            ui.warn("有 %d 项自检未通过" % len(rep.soft_fails))
    else:
        ui.die("硬前置有 %d 项未通过，链路建不起来。按上面 → 提示逐条修"
               % len(rep.hard_fails))
    print(ui.gry("  耗时 %.1fs  报告已存 out/doctor.json" % rep.elapsed()))


def _print_item(i):
    line = "%-44s" % i.title
    if i.status == "ok":
        ui.ok(line, i.detail)
    elif i.status == "fail":
        ui.fail(line, i.detail)
    elif i.status == "warn":
        ui.warn(line, i.detail)
    else:
        ui.skip(line, i.detail)
    if i.hint and i.status in ("fail", "warn"):
        ui.hint(i.hint)


def _write_report(rep, cfg):
    import json
    import os
    try:
        p = os.path.join(C.OUT, "doctor.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"serial": rep.serial,
                       "healthy": rep.healthy,
                       "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "items": [i.as_dict() for i in rep.items]},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass
