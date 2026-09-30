#!/system/bin/sh
# 前台常驻运行 MiniProxy（由外部会话保持；带自动重启）
export CLASSPATH=/data/local/tmp/miniproxy.dex
echo "[$(date)] MiniProxy supervisor start"
while true; do
  echo "[$(date)] launching MiniProxy on :17890"
  app_process /system/bin com.dbg.MiniProxy 17890
  echo "[$(date)] MiniProxy exited (rc=$?), restart in 3s"
  sleep 3
done
