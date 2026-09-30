# -*- coding: utf-8 -*-
"""
端到端验证 —— 真正发包走一遍两跳，确认链路可用。

验证点：
  ① 下行  电脑 → 127.0.0.1:17890 → (adb forward) → 手机 MiniProxy
           判据：MiniProxy 返回代理应答（对 HTTP 请求回 502/200 之类，而不是连接被拒）
  ② 上行  手机 → 127.0.0.1:8888 → (adb reverse) → 电脑 MITM
           判据：手机侧 TCP connect 成功
  ③ 全链路 手机侧发 HTTP 到 MiniProxy → 手机 MiniProxy 出站 → VPN → 目标
           判据：能取回内容（需要目标可达）
"""
import os
import sys
import socket
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import ui
ui.setup_console()
from lib import adbutil as A
from lib import device as D
from lib import config as C

cfg = C.load()
serial = A.pick_device(cfg)
pp = int(cfg["phone_proxy_port"])
mp = int(cfg["pc_mitm_port"])
target = (cfg.get("route_target") or "").strip()

ui.head("端到端验证")
ui.kv("设备", serial)
ui.kv("下行端口", pp)
ui.kv("上行端口", mp)

# ── ① 下行：PC → forward → 手机 MiniProxy ──
ui.sub("① 下行  电脑 → adb forward → 手机 MiniProxy")
try:
    s = socket.create_connection(("127.0.0.1", pp), timeout=5)
    s.sendall(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
    s.settimeout(12)
    data = s.recv(200)
    s.close()
    first = data.split(b"\r\n")[0].decode("latin1", "replace")
    if data.startswith(b"HTTP/"):
        ui.ok("MiniProxy 应答正常", first[:60])
        ui.hint("（返回 502 也正常 —— 说明代理在工作，只是它自己出站没成功）")
    else:
        ui.warn("收到非 HTTP 应答", repr(data[:60]))
except Exception as e:
    ui.fail("连接失败", "%s" % str(e)[:100])
    ui.hint("检查：手机侧代理是否在跑（link.py status）")

# ── ② 上行：手机 → reverse → 电脑 MITM ──
ui.sub("② 上行  手机 → adb reverse → 电脑 MITM")
rc, out, err = A.shell(
    "echo | toybox nc -w 4 127.0.0.1 %d >/dev/null 2>&1 && echo OPEN || echo CLOSED" % mp,
    timeout=12, serial=serial, su=True)
o = (out or "").strip()
if "OPEN" in o:
    ui.ok("手机侧可连通电脑 :%d" % mp, "adb reverse 生效")
else:
    ui.fail("手机侧连不通电脑 :%d" % mp, o[:80])
    ui.hint("检查：adb reverse --list 是否有 tcp:%d" % mp)

# ── ③ 全链路：手机侧经 MiniProxy 出站 ──
ui.sub("③ 全链路  手机 → MiniProxy → 出站 → 目标")
if not target:
    ui.skip("未配置 route_target，跳过", "在 config.json 设 route_target=<内网IP或域名> 可启用")
else:
    rc, out, err = A.shell(
        "echo -e 'GET http://%s/ HTTP/1.0\\r\\nHost: %s\\r\\n\\r\\n' | "
        "toybox nc -w 6 127.0.0.1 %d 2>/dev/null | head -c 120" % (target, target, pp),
        timeout=20, serial=serial, su=True)
    o = (out or "").strip()
    if o:
        ui.ok("全链路有应答", o.replace("\n", " | ")[:110])
    else:
        ui.warn("全链路无应答", "可能目标不可达、或目标非 HTTP")
    # 顺带报一下路由
    dev, raw = D.route_get(serial, target)
    if dev:
        ui.kv("路由 → %s" % target, dev)

ui.head("结论")
ui.info("①③ 通过 = 抓包链路可用；② 通过 = 手机侧流量能回到电脑 MITM")
print()
