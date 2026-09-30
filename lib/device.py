# -*- coding: utf-8 -*-
"""
手机侧操作 —— root、代理进程、系统代理设置、证书、路由、连通性。

所有函数返回结构化结果，不直接打印（打印交给 doctor / linkmgr）。
"""
import os
import re
import time

from . import adbutil as A
from . import config as C


# ── root / 环境 ─────────────────────────────────────────────────────
def check_root(serial, timeout=8):
    """
    检测 su 可用性。★ 关键：必须带超时 ——
    Magisk 弹授权框时 adb shell 会静默挂住，报不出错。
    返回 (state, detail)
      state ∈ ok / denied / prompt / noroot / error
    """
    rc, out, err = A.shell("id", timeout=timeout, serial=serial, su=True)
    if rc == -9:
        return "prompt", "su 无响应（超时）—— 手机上很可能弹出了 root 授权框，请点【允许】并勾选【永久记住】"
    if rc != 0:
        return "noroot", "su 执行失败: %s" % ((err or out or "").strip()[:120])
    o = (out or "").strip()
    if "uid=0" in o:
        return "ok", o
    return "denied", "su 返回 %s —— 授权被拒绝" % o[:80]


def has_app_process(serial):
    rc, out, err = A.shell(
        "ls /system/bin/app_process* 2>/dev/null || which app_process 2>/dev/null",
        timeout=10, serial=serial)
    o = (out or "").strip()
    return (rc == 0 and bool(o)), (o.splitlines()[0] if o else "")


def tmp_writable(serial):
    rc, out, err = A.shell(
        'touch /data/local/tmp/.wtest 2>&1 && rm -f /data/local/tmp/.wtest && echo WRITABLE',
        timeout=10, serial=serial)
    return "WRITABLE" in (out or ""), (out or "").strip()[:80]


# ── 证书 ────────────────────────────────────────────────────────────
CACERT_DIRS = [
    ("/system/etc/security/cacerts", "系统证书区"),
    ("/apex/com.android.conscrypt/cacerts", "系统证书区(APEX, Android 14+)"),
]
USER_CACERT_DIRS = [
    ("/data/misc/user/0/cacerts-added", "用户证书区"),
]


def list_system_certs(serial, filt=None):
    """列出系统证书区的证书指纹（文件名即 subject hash）"""
    names = []
    for d, _ in CACERT_DIRS:
        rc, out, err = A.shell("ls %s 2>/dev/null" % d, timeout=10, serial=serial, su=True)
        for line in (out or "").splitlines():
            n = line.strip()
            if not n or n in (".", ".."):
                continue
            if n.endswith(".0"):
                names.append(n)
    if filt:
        f = filt.lower()
        names = [n for n in names if f in n.lower()]
    return sorted(set(names))


def list_user_certs(serial):
    names = []
    for d, _ in USER_CACERT_DIRS:
        rc, out, err = A.shell("ls %s 2>/dev/null" % d, timeout=10, serial=serial, su=True)
        for line in (out or "").splitlines():
            n = line.strip()
            if n.endswith(".0"):
                names.append(n)
    return sorted(set(names))


def cert_subject_hash(serial, pem_on_pc):
    """
    由 PC 上的 PEM 算 Android 用的 subject_hash_old（8 位十六进制）。
    仅在 PC 有 openssl 时可用；否则返回 None。
    注意：Android 用的通常是 hash_old（MD5 变体），openssl 需加 -subject_hash_old。
    """
    import subprocess
    import shutil
    ossl = shutil.which("openssl")
    if not ossl or not os.path.exists(pem_on_pc):
        return None
    try:
        p = subprocess.run([ossl, "x509", "-inform", "PEM", "-subject_hash_old", "-in", pem_on_pc],
                           capture_output=True, timeout=15)
        if p.returncode == 0:
            return p.stdout.decode("utf-8", "replace").splitlines()[0].strip()
    except Exception:
        pass
    return None


# ── 系统代理 ────────────────────────────────────────────────────────
def get_proxy(serial):
    """返回 (host, port) 或 (None, None)。global 优先，其次 per-model。"""
    for ns in ("global",):
        rc, out, err = A.shell("settings get %s http_proxy" % ns, timeout=10, serial=serial)
        v = (out or "").strip()
        if v and v not in ("null", ":0", ""):
            if ":" in v:
                h, _, p = v.rpartition(":")
                try:
                    return h, int(p)
                except ValueError:
                    return v, None
            return v, None
    return None, None


def set_proxy(host, port, serial):
    cmd = "settings put global http_proxy %s:%d" % (host, port)
    rc, out, err = A.shell(cmd, timeout=10, serial=serial)
    if rc != 0 or "Exception" in (err or "") or "Exception" in (out or ""):
        # 回退：分开设 host / port
        A.shell("settings put global global_http_proxy_host %s" % host, timeout=10, serial=serial)
        A.shell("settings put global global_http_proxy_port %d" % port, timeout=10, serial=serial)
        A.shell("settings put global http_proxy %s:%d" % (host, port), timeout=10, serial=serial)
    return get_proxy(serial)


def clear_proxy(serial):
    A.shell("settings put global http_proxy :0", timeout=10, serial=serial)
    A.shell("settings delete global http_proxy", timeout=10, serial=serial)
    A.shell("settings delete global global_http_proxy_host", timeout=10, serial=serial)
    A.shell("settings delete global global_http_proxy_port", timeout=10, serial=serial)
    return get_proxy(serial)


# ── 代理进程（MiniProxy）────────────────────────────────────────────
def proxy_pids(serial):
    """列出手机侧代理相关进程 PID（守护 + 代理本体）"""
    # ★ 两个细节：
    #   1) 必须看 ARGS 而非 NAME —— app_process 的 NAME 是 "app_process"，
    #      只有 ARGS 里才有 "com.dbg.MiniProxy"（用 NAME 匹配会得到假阴性）
    #   2) 用 [x] 字符类打断自匹配，避免 grep 匹配到自己这条命令
    rc, out, err = A.shell_script(
        'ps -A -o PID,ARGS 2>/dev/null | grep -E "usbmitm[_]run|com[.]dbg[.]Mini[P]roxy" '
        '| grep -v "grep -E" | awk \'{print $1}\'',
        timeout=12, serial=serial, su=True)
    return [x.strip() for x in (out or "").split() if x.strip().isdigit()]


def stop_proxy(serial, port=None, verify=True):
    """
    停止手机侧代理。

    ★ 三个踩过的坑：
      1) 守护脚本是 while true 循环，只杀代理进程会被立刻拉起来
         ⇒ 必须先杀守护、再杀代理。
      2) `pkill -f usbmitm_run.sh` 会【自杀】—— 发起命令的 shell 命令行里
         本身就含该字符串，-f 匹配整条命令行 ⇒ 把自己也匹配上，
         现象是 adb shell 直接 "Terminated"、后续命令全不执行。
         ⇒ 用字符类 [_] 打断自匹配。
      3) su -c "..." 的引号嵌套会让外层 sh 抢先展开 $( ) 和 $var，
         导致脚本根本没跑（且不报错）。
         ⇒ 改用 A.shell_script()（base64 传输，不经过 shell 解析）。
    """
    # 1. 先杀守护循环（防止它把代理重新拉起来）
    A.shell_script(
        'for p in $(pgrep -f "usbmitm[_]run"); do kill -9 "$p" 2>/dev/null; done\n'
        'pkill -9 -f "usbmitm[_]run" 2>/dev/null\n'
        'echo STEP1_DONE',
        timeout=15, serial=serial, su=True)
    time.sleep(1)

    # 2. 再杀代理进程本体（app_process 形式）
    A.shell_script(
        'for p in $(pgrep -f "com[.]dbg[.]Mini[P]roxy"); do kill -9 "$p" 2>/dev/null; done\n'
        'pkill -9 -f "com[.]dbg[.]Mini[P]roxy" 2>/dev/null\n'
        'echo STEP2_DONE',
        timeout=15, serial=serial, su=True)
    time.sleep(1)

    # 3. 兜底：按监听端口的持有者 PID 再杀一次（避免 pgrep 不可靠）
    if port:
        A.shell_script(
            'p=$(netstat -tlnp 2>/dev/null | grep ":%d " | grep -i listen '
            '| sed -n "s#.*[^0-9]\\([0-9]\\{1,6\\}\\)/.*#\\1#p" | head -1)\n'
            '[ -n "$p" ] && kill -9 "$p" 2>/dev/null\n'
            'echo STEP3_DONE' % int(port),
            timeout=15, serial=serial, su=True)
        time.sleep(1)

    if not verify:
        return True
    if port:
        alive, _ = proxy_alive(serial, int(port))
        return not alive
    return not proxy_pids(serial)


def proxy_alive(serial, port):
    """
    判断手机侧代理是否【在监听】。
    ★ 踩过的坑：不能只 grep 端口号 —— 会把 TIME_WAIT/ESTABLISHED 的连接也算进来
      （实测 netstat -tln 在该 toybox 上未正确过滤，必须显式要求 LISTEN）。
    判据：某行同时含 LISTEN 与 ":<port>"，且端口出现在本地地址列。
    """
    if not port:
        return False, ""
    cmd = ("(netstat -tln 2>/dev/null || netstat -ln 2>/dev/null || ss -tln 2>/dev/null) "
           "| grep -i listen | grep ':%d '" % port)
    rc, out, err = A.shell(cmd, timeout=10, serial=serial, su=True)
    o = (out or "").strip()
    if o:
        return True, o.splitlines()[0].strip()[:120]
    # 兜底：有些 ROM 的 netstat 无 LISTEN 字样，改用 ss -tlnp 的 State 列
    rc, out2, err2 = A.shell(
        "ss -tln 2>/dev/null | awk 'NR>1 && $1==\"LISTEN\" && $4 ~ /:%d$/ {print $0}'" % port,
        timeout=10, serial=serial, su=True)
    o2 = (out2 or "").strip()
    return bool(o2), (o2.splitlines()[0].strip()[:120] if o2 else "")


def proxy_listening_pids(serial, port):
    rc, out, err = A.shell(
        'netstat -tlnp 2>/dev/null | grep ":%d "' % port, timeout=10, serial=serial, su=True)
    return (out or "").strip()


def proxy_log_tail(serial, n=40):
    rc, out, err = A.shell("tail -n %d %s 2>/dev/null" % (n, C.PHONE_LOG),
                           timeout=10, serial=serial, su=True)
    return (out or "").rstrip()


def proxy_log_clear(serial):
    A.shell(": > %s 2>/dev/null || true" % C.PHONE_LOG, timeout=10, serial=serial, su=True)


# ── 路由 / 网络 ─────────────────────────────────────────────────────
def route_get(serial, target):
    """返回 (dev, raw)。dev 如 tun0 / wlan0 / rmnet_data0"""
    rc, out, err = A.shell("ip route get %s" % target, timeout=10, serial=serial, su=True)
    o = (out or "").strip()
    if not o:
        return None, ""
    m = re.search(r"\bdev\s+(\S+)", o)
    return (m.group(1) if m else None), o


def vpn_ifaces(serial):
    rc, out, err = A.shell("ip -o link show 2>/dev/null | awk -F': ' '{print $2}'",
                           timeout=10, serial=serial, su=True)
    return [x.strip() for x in (out or "").splitlines() if x.strip()]


def default_route(serial):
    rc, out, err = A.shell("ip route show default 2>/dev/null", timeout=10, serial=serial, su=True)
    return (out or "").strip()


def ping(serial, target, count=2, timeout=8):
    rc, out, err = A.shell("ping -c %d -W 2 %s" % (count, target), timeout=timeout,
                           serial=serial, su=True)
    o = out or ""
    m = re.search(r"(\d+)% packet loss", o)
    loss = int(m.group(1)) if m else 100
    return loss < 100, o.strip()[:200]


def tcp_probe(serial, host, port, timeout=8):
    """
    用手机侧做 TCP 连通性探测（不依赖 nc，兼容性更好）。
    返回 (ok, detail)
    """
    script = (
        'python -c "import socket,sys;s=socket.socket();s.settimeout(4);'
        's.connect((\'%s\',%d));print(\'OPEN\');s.close()" 2>/dev/null '
        '|| echo NO_PYTHON' % (host, port)
    )
    rc, out, err = A.shell(script, timeout=timeout, serial=serial, su=True)
    o = (out or "").strip()
    if "OPEN" in o:
        return True, "TCP 可达"
    if "NO_PYTHON" in o or rc != 0:
        # 回退到 /system/bin/toybox nc
        rc2, out2, err2 = A.shell(
            'echo | toybox nc -w 4 %s %d >/dev/null 2>&1 && echo OPEN || echo CLOSED' % (host, port),
            timeout=timeout, serial=serial, su=True)
        o2 = (out2 or "").strip()
        return ("OPEN" in o2), (o2 or ("rc=%s" % rc2))
    return False, o[:160]


# ── 部署 ────────────────────────────────────────────────────────────
def deploy(serial, dex_local, script_local, progress=None):
    """
    推送 dex + 启动脚本到手机。返回 (ok, messages[])
    """
    msgs = []
    okall = True

    ok, out = A.push(dex_local, C.PHONE_DEX, timeout=120, serial=serial)
    msgs.append(("推送 %s" % os.path.basename(dex_local), ok, out.strip()[:100]))
    okall &= ok

    # 启动脚本内容在 PC 侧生成（端口可变），不走 assets 原文件
    return okall, msgs


def write_run_script(serial, port):
    """在手机侧生成守护启动脚本（带自动重启）"""
    body = (
        "#!/system/bin/sh\n"
        "# usb-mitm-link 手机侧守护（自动重启）\n"
        "export CLASSPATH=%s\n"
        "LOG=%s\n"
        'echo "[$(date)] supervisor start port=%d" >> $LOG\n'
        "while true; do\n"
        '  echo "[$(date)] launching on :%d" >> $LOG\n'
        "  app_process /system/bin com.dbg.MiniProxy %d >> $LOG 2>&1\n"
        '  echo "[$(date)] exited rc=$? , restart in 2s" >> $LOG\n'
        "  sleep 2\n"
        "done\n"
    ) % (C.PHONE_DEX, C.PHONE_LOG, port, port, port)

    # 用 base64 传输，避免引号/换行在 adb shell 里被吞
    import base64
    b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")
    rc, out, err = A.shell(
        "echo %s | base64 -d > %s && chmod 755 %s && echo WROTE" % (b64, C.PHONE_RUN, C.PHONE_RUN),
        timeout=20, serial=serial, su=True)
    return ("WROTE" in (out or "")), (out or err or "").strip()[:120]


def start_proxy(serial, port):
    """后台拉起守护脚本（nohup + setsid 脱离 adb 会话）"""
    cmd = ("nohup setsid sh %s >/dev/null 2>&1 & echo STARTED" % C.PHONE_RUN)
    rc, out, err = A.shell(cmd, timeout=15, serial=serial, su=True)
    return ("STARTED" in (out or "")), (out or err or "").strip()[:120]


# 注意：stop_proxy 定义在上方（proxy_pids 之后），此处不要再定义 ——
# Python 同名函数【后定义者生效】，重复定义会让上面的修复被静默覆盖。
# （本项目就踩过：新版停在 158 行，旧版在 349 行把它盖掉了，导致调试一直在追旧实现）


# ── 端口占用（PC 侧）────────────────────────────────────────────────
def pc_port_free(port, host="127.0.0.1"):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.5)
    try:
        s.connect((host, port))
        s.close()
        return False, "已被占用"
    except Exception:
        return True, "空闲"
