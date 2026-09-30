# -*- coding: utf-8 -*-
"""
配置 —— 端口、路径、超时、可持久化的用户设置。

端口约定（可按需改）：
  PHONE_PROXY_PORT  手机侧 MiniProxy 监听端口（adb forward 的目标）
  PC_MITM_PORT      电脑侧 MITM（Yakit/mitmproxy）监听端口
  REVERSE_PROBE     探测 adb reverse 能力时临时占用的端口（用完即删）

链路两跳：
  上行(手机→电脑)  adb reverse  tcp:PC_MITM_PORT   手机:PC_MITM_PORT → 电脑:PC_MITM_PORT
  下行(电脑→手机)  adb forward  tcp:PHONE_PROXY_PORT 电脑:PHONE_PROXY_PORT → 手机:PHONE_PROXY_PORT
"""
import os
import json
import copy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
OUT = os.path.join(ROOT, "out")
CONF_FILE = os.path.join(ROOT, "config.json")

# 手机侧落地路径
PHONE_TMP = "/data/local/tmp"
PHONE_DEX = PHONE_TMP + "/usbmitm.dex"
PHONE_RUN = PHONE_TMP + "/usbmitm_run.sh"
PHONE_LOG = PHONE_TMP + "/usbmitm.log"

DEFAULTS = {
    # ── 端口 ──
    "phone_proxy_port": 17890,     # 手机侧 MiniProxy
    "pc_mitm_port": 8888,          # 电脑侧 MITM
    "reverse_probe_port": 17899,   # 探测 adb reverse 用

    # ── 设备 ──
    "serial": "",                  # 空 = 自动取唯一在线设备

    # ── MITM ──
    # 仅供提示文案使用，填 burp / mitmproxy / 任意名字都不影响功能
    "mitm_name": "Yakit",
    # CA 证书 PEM 路径（可选）。填了 doctor 会核对【这张证书】是否已装进手机的系统证书区；
    # 不填则只报系统区证书总数，无法判断装的是不是当前 MITM 那张。
    #   Yakit：从 Yakit 导出 CA
    #   Burp ：Proxy → Options → Import/export CA certificate → Export（DER）
    #          openssl x509 -inform DER -in cacert.der -out burp.pem
    "ca_pem": "",

    # ── 超时（秒）──
    "t_su": 8,                     # su 探测：超时即视为"弹窗未授权"
    "t_adb": 20,                   # 一般 adb 命令
    "t_push": 120,                 # 推送文件
    "t_net": 8,                    # 网络连通性探测

    # ── 行为 ──
    "watch_interval": 15,          # 看门狗巡检间隔（秒）
    "auto_heal": True,             # 看门狗是否自动修复
    "keep_proxy_on_down": False,   # down 时是否保留手机系统代理设置
    "route_target": "",            # 路由自检目标（IP 或域名，空=跳过网络类检查）
}


class Config(dict):
    def __init__(self, *a, **kw):
        super(Config, self).__init__(*a, **kw)
        self.update(copy.deepcopy(DEFAULTS))

    def load(self):
        if os.path.exists(CONF_FILE):
            try:
                with open(CONF_FILE, "r", encoding="utf-8") as f:
                    self.update(json.load(f))
            except Exception:
                pass
        return self

    def save(self):
        try:
            with open(CONF_FILE, "w", encoding="utf-8") as f:
                json.dump(self, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # 便捷访问
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)


def load():
    os.makedirs(OUT, exist_ok=True)
    return Config().load()
