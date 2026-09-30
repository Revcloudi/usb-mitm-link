# usb-mitm-link

通过 USB 端口转发为手机抓包建立代理链路，不要求手机与电脑处于同一网段。

## 背景

手机接入 VPN 后，将系统代理指向电脑局域网地址的方式通常不可用：

- 手机 IP 随 VPN 变化，原局域网地址失效
- VPN 接管路由，无法回连电脑
- 电脑位于公网、手机位于内网时链路不成立

本工具上行与下行均通过 USB 转发，与网络拓扑无关。

## 链路

```
上行  手机 App → 127.0.0.1:8888   --adb reverse-->  电脑:8888   (MITM)
下行  电脑 MITM → 127.0.0.1:17890 --adb forward-->  手机:17890  (MiniProxy)
                                    └─ 出站 → VPN/内网目标
```

## 依赖

| 项 | 要求 |
|---|---|
| Python | 3.7 及以上，仅使用标准库 |
| adb | PATH 中可用，或位于常见安装路径 |
| 手机 | 已 ROOT，`su` 可免交互执行 |
| USB 调试 | 已开启，`adb devices` 显示为 `device` |
| MITM | 在电脑侧指定端口监听，上游代理指向 `127.0.0.1:17890` |

## 使用

```bash
cd D:\Desktop\work\tools\usb-mitm-link

python link.py doctor      # 环境自检，只读
python link.py up          # 建立链路
python verify.py           # 验证链路
python link.py down        # 拆除链路，还原手机设置
```

执行 `up` 后再启动目标 App。App 在启动时读取系统代理，运行期间不再读取。

## 命令

| 命令 | 说明 |
|---|---|
| `doctor` | 环境自检 |
| `up` | 建立链路 |
| `status` | 查看状态 |
| `heal` | 修复链路，可重复执行 |
| `down` | 拆除链路 |
| `watch` | 看门狗，掉线自动修复 |
| `logs` | 手机侧代理日志 |
| `config` | 查看或修改配置 |

常用参数：

```bash
python link.py doctor --route-target 10.1.2.3           # 增加目标可达性检查
python link.py doctor --route-target 10.1.2.3 --save    # 写入配置
python link.py up --pc-mitm-port 8080                   # 指定 MITM 端口
python link.py up --watch                               # 建链后进入看门狗
python link.py up --serial FA7BM1A06928                 # 指定设备
```

## 自检项

| # | 检查项 |
|---|---|
| 1 | adb 可用 |
| 2 | 设备在线且已授权 |
| 3 | ROOT 可用，`su` 免交互 |
| 4 | `app_process` 可用 |
| 5 | `/data/local/tmp` 可写 |
| 6 | `adb reverse` 可用 |
| 7 | PC 侧端口空闲 |
| 8 | MITM 证书位于系统证书区 |
| 9 | 手机网络可达，路由走 VPN 接口 |
| 10 | 无上次残留 |

第 8 项在配置 `ca_pem` 后核对指定证书，而非仅统计系统区证书数量。

## 对接 MITM

工具不限定 MITM 实现，仅要求：

1. MITM 在 `pc_mitm_port` 监听
2. MITM 的上游代理指向 `127.0.0.1:phone_proxy_port`

### Burp Suite

| 步骤 | 操作 |
|---|---|
| 监听端口 | Proxy → Options → Proxy Listeners，默认 `127.0.0.1:8080`；使用该端口时以 `--pc-mitm-port 8080` 启动 |
| 上游代理 | Settings → Network → Connections → Upstream Proxy Servers，新增规则：Destination host `*`，Proxy host `127.0.0.1`，Port `17890` |
| 导出证书 | Proxy → Options → Import/export CA certificate → Export，选择 DER 格式 |
| 转换格式 | `openssl x509 -inform DER -in cacert.der -out burp.pem` |
| 安装证书 | 导入手机系统证书区 `/system/etc/security/cacerts` |
| 登记路径 | `python link.py config --set ca_pem=D:\path\burp.pem` |

### mitmproxy

```bash
mitmproxy --listen-port 8888 --mode upstream:http://127.0.0.1:17890
```

更换 MITM 后需重新安装对应的 CA 证书。系统区存在旧证书时第 8 项仍会通过，配置 `ca_pem` 可发现新证书缺失。

## 配置

`config.json`：

| 键 | 默认值 | 说明 |
|---|---|---|
| `phone_proxy_port` | 17890 | 手机侧代理端口 |
| `pc_mitm_port` | 8888 | 电脑侧 MITM 端口 |
| `mitm_name` | Yakit | 仅用于提示文案 |
| `ca_pem` | 空 | CA 证书 PEM 路径，配置后核对具体证书 |
| `serial` | 空 | 设备序列号，留空时自动选择唯一在线设备 |
| `t_su` | 8 | su 探测超时，单位秒 |
| `watch_interval` | 15 | 看门狗巡检间隔，单位秒 |
| `auto_heal` | 开 | 看门狗是否自动修复 |
| `keep_proxy_on_down` | 关 | `down` 时是否保留手机代理设置 |
| `route_target` | 空 | 路由自检目标 |

```bash
python link.py config --set mitm_name=Burp pc_mitm_port=8080
python link.py config
```

## 覆盖范围

可捕获：使用系统代理的 HTTP 客户端，包括 OkHttp、WebView、HttpURLConnection。

不可捕获：

- QUIC / HTTP3，其 UDP 443 不经过代理
- 不使用系统代理的应用
- 存在 SSL Pinning 的应用，需另行处理
- 原生 socket 协议

## 排错

| 现象 | 排查方向 |
|---|---|
| 抓不到数据包 | `link.py status`，检查代理设置、MITM 是否监听、代理是否在 App 启动前设置 |
| App 无法连接 | `link.py doctor`，检查证书是否位于用户证书区 |
| 运行中断开 | `link.py heal` |
| 手机侧代理未启动 | `link.py logs` |
| 出站失败 | `link.py doctor --route-target <IP>` |
| `su` 无响应 | 检查手机上是否出现授权弹窗 |

## 目录

```
usb-mitm-link/
├── link.py            主入口
├── verify.py          端到端验证
├── config.json        配置，首次运行后生成
├── lib/               模块
├── assets/            手机侧代理 miniproxy.dex
└── out/               状态与日志
```
