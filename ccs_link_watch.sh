#!/bin/sh
# CCS 链路跳变监视：0.3s 采样 TC358743 SYS(0x8520)/HPD(0x8544)，只记跳变。
# 用法：setsid ./ccs_link_watch.sh >/dev/null 2>&1 < /dev/null &
# 判读：SYS bit0(0x01)=MS9332 输出侧 5V、bit1(0x02)=TMDS、bit2=PHY_PLL、
#       bit3=PHY_SCDT、bit7(0x80)=SYNC；满锁 0x9f/0x8f。
#       TMDS 掉而 5V/HPD 在 = MS9332 接收侧失锁（§28o/§28p 挂死家族）。
# 板上部署路径 /tmp/ccs_link_watch.sh（重启丢，源码在本仓库）。
last=""
: > /tmp/link_watch.log
while true; do
  s=$(i2ctransfer -f -y 4 w2@0x0f 0x85 0x20 r1 2>/dev/null)
  h=$(i2ctransfer -f -y 4 w2@0x0f 0x85 0x44 r1 2>/dev/null)
  cur="$s $h"
  if [ "$cur" != "$last" ]; then
    echo "$(date +%T.%N) SYS=$s HPD=$h" >> /tmp/link_watch.log
    last="$cur"
  fi
  sleep 0.3
done
