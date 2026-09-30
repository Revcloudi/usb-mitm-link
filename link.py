#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
usb-mitm-link —— 手机抓包链路一键编排（纯 USB 两跳，不依赖局域网）

场景：
  测试机（已 ROOT，挂内网/VPN） ←USB→ 电脑（公网，跑 MITM/Yakit）

链路：
  上行  手机 App → 127.0.0.1:<pc_mitm_port>  --adb reverse-->  电脑:<pc_mitm_port>  (MITM)
  下行  电脑 MITM → 127.0.0.1:<phone_proxy_port>  --adb forward-->  手机:<phone_proxy_port>  (MiniProxy)
                                        └─ 裸 socket 出站 → VPN/内网目标

子命令：
  doctor   环境自检（只读，零副作用）★ 建议第一步
  up       建立链路
  status   查看链路状态
  heal     尝试自愈（幂等）
  down     拆除链路并还原手机代理设置
  watch    看门狗（前台常驻，掉线自动修复）
  logs     查看手机侧代理日志
  config   查看/修改配置

用法示例：
  python link.py doctor
  python link.py doctor --route-target 10.1.2.3
  python link.py up
  python link.py up --watch
  python link.py status
  python link.py down
"""
import io
import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import ui                          # noqa: E402

ui.setup_console()                          # ★ 必须在任何输出之前

from lib import config as C                 # noqa: E402
from lib import adbutil as A                # noqa: E402
from lib import device as D                 # noqa: E402
from lib import doctor as DOC               # noqa: E402
from lib import linkmgr as L                # noqa: E402

VERSION = "1.0.0"


def _cfg(args):
    cfg = C.load()
    if getattr(args, "serial", None):
        cfg["serial"] = args.serial
    if getattr(args, "phone_proxy_port", None):
        cfg["phone_proxy_port"] = args.phone_proxy_port
    if getattr(args, "pc_mitm_port", None):
        cfg["pc_mitm_port"] = args.pc_mitm_port
    if getattr(args, "route_target", None) is not None:
        cfg["route_target"] = args.route_target
    if getattr(args, "mitm", None):
        cfg["mitm_name"] = args.mitm
    if getattr(args, "ca_pem", None) is not None:
        cfg["ca_pem"] = args.ca_pem
    return cfg


# ── 子命令 ──────────────────────────────────────────────────────────
def cmd_doctor(args):
    cfg = _cfg(args)
    if args.save:
        cfg.save()
        ui.info("配置已保存到 config.json")
    rep = DOC.run(cfg)
    return 0 if rep.healthy else 2


def cmd_up(args):
    cfg = _cfg(args)
    if args.save:
        cfg.save()
    return L.up(cfg, skip_doctor=args.skip_doctor, with_watch=args.watch)


def cmd_down(args):
    cfg = _cfg(args)
    keep = True if args.keep_proxy else (False if args.clear_proxy else None)
    return L.down(cfg, keep_proxy=keep)


def cmd_status(args):
    cfg = _cfg(args)
    return L.status(cfg)


def cmd_heal(args):
    cfg = _cfg(args)
    fixed, broken = L.heal(cfg)
    return 0 if not broken else 1


def cmd_watch(args):
    cfg = _cfg(args)
    from lib import watchdog
    return watchdog.run(cfg, interval=args.interval,
                        auto_heal=(False if args.no_heal else None))


def cmd_logs(args):
    cfg = _cfg(args)
    serial = A.pick_device(cfg)
    if args.clear:
        D.proxy_log_clear(serial)
        ui.ok("已清空手机侧日志")
        return 0
    txt = D.proxy_log_tail(serial, args.lines)
    ui.head("手机侧代理日志（末尾 %d 行）" % args.lines)
    if not txt:
        ui.info("（空 —— 可能代理还没跑过）")
    else:
        for line in txt.splitlines():
            print("  " + line)
    return 0


def cmd_config(args):
    cfg = C.load()
    if args.set:
        for kv in args.set:
            if "=" not in kv:
                ui.die("格式应为 key=value：%s" % kv)
            k, _, v = kv.partition("=")
            k = k.strip()
            old = cfg.get(k)
            if isinstance(old, bool):
                v = v.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(old, int):
                try:
                    v = int(v)
                except ValueError:
                    ui.die("%s 需要整数" % k)
            cfg[k] = v
            ui.ok("已设置", "%s = %r" % (k, v))
        cfg.save()
        ui.info("已保存到 config.json")
        return 0

    ui.head("当前配置  (%s)" % C.CONF_FILE)
    for k in sorted(cfg.keys()):
        default = C.DEFAULTS.get(k)
        mark = "" if cfg.get(k) == default else ui.yel("  (已改)")
        ui.kv(k, "%r%s" % (cfg.get(k), mark), width=24)
    print()
    ui.info("改法： link.py config --set pc_mitm_port=9090")
    return 0


# ── 参数 ────────────────────────────────────────────────────────────
def build_parser():
    p = argparse.ArgumentParser(
        prog="link.py",
        description="usb-mitm-link —— 手机抓包链路一键编排（纯 USB 两跳）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法示例：")[-1],
    )
    p.add_argument("--version", action="version", version="usb-mitm-link %s" % VERSION)

    def common(sp):
        sp.add_argument("--serial", help="设备序列号（多设备时必须指定）")
        sp.add_argument("--phone-proxy-port", type=int, dest="phone_proxy_port",
                        help="手机侧 MiniProxy 端口（默认 17890）")
        sp.add_argument("--pc-mitm-port", type=int, dest="pc_mitm_port",
                        help="电脑侧 MITM 端口（默认 8888；Burp 常用 8080）")
        sp.add_argument("--mitm", help="MITM 名字，仅用于提示文案（如 burp / yakit / mitmproxy）")
        sp.add_argument("--ca-pem", dest="ca_pem",
                        help="CA 证书 PEM 路径；给了就核对这张证书是否已装进系统证书区")
        sp.add_argument("--save", action="store_true", help="把本次参数写入 config.json")

    sub = p.add_subparsers(dest="cmd")

    d = sub.add_parser("doctor", help="环境自检（只读）")
    common(d)
    d.add_argument("--route-target", dest="route_target", default=None,
                   help="路由/可达性自检的目标（内网 IP 或域名）")
    d.set_defaults(func=cmd_doctor)

    u = sub.add_parser("up", help="建立链路")
    common(u)
    u.add_argument("--skip-doctor", action="store_true", help="跳过前置自检（不推荐）")
    u.add_argument("--watch", action="store_true", help="建链后自动进看门狗（前台）")
    u.set_defaults(func=cmd_up)

    dn = sub.add_parser("down", help="拆除链路")
    common(dn)
    g = dn.add_mutually_exclusive_group()
    g.add_argument("--keep-proxy", action="store_true", help="保留手机系统代理设置")
    g.add_argument("--clear-proxy", action="store_true", help="强制清除（不还原原值）")
    dn.set_defaults(func=cmd_down)

    s = sub.add_parser("status", help="查看链路状态")
    common(s)
    s.set_defaults(func=cmd_status)

    h = sub.add_parser("heal", help="尝试自愈（幂等，可反复跑）")
    common(h)
    h.set_defaults(func=cmd_heal)

    w = sub.add_parser("watch", help="看门狗（前台常驻）")
    common(w)
    w.add_argument("--interval", type=int, help="巡检间隔秒（默认 15）")
    w.add_argument("--no-heal", action="store_true", help="只告警不自动修复")
    w.set_defaults(func=cmd_watch)

    lg = sub.add_parser("logs", help="查看手机侧代理日志")
    common(lg)
    lg.add_argument("-n", "--lines", type=int, default=40, help="显示末尾多少行")
    lg.add_argument("--clear", action="store_true", help="清空日志")
    lg.set_defaults(func=cmd_logs)

    cf = sub.add_parser("config", help="查看/修改配置")
    cf.add_argument("--set", nargs="+", metavar="key=value", help="设置配置项")
    cf.set_defaults(func=cmd_config)

    return p


def main():
    if len(sys.argv) == 1:
        build_parser().print_help()
        print()
        ui.info("建议第一步：  python link.py doctor")
        return 0
    p = build_parser()
    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print()
        ui.info("已中断")
        return 130
    except A.AdbError as e:
        ui.die("adb 错误: %s" % str(e))


if __name__ == "__main__":
    sys.exit(main())
