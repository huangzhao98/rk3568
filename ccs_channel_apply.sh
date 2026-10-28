#!/bin/sh
# ccs_channel_apply.sh — ccs_webcam 服务启动前（ExecStartPre）按
# /etc/ccs_channel 恢复采集通道。此时采集进程尚未打开任何 v4l2 句柄，
# unbind/bind 最安全（实测句柄跨 unbind 会触发 subdev_close 空指针 oops）。
# 已在目标通道则跳过，避免无谓的重探测。
CH=$(cat /etc/ccs_channel 2>/dev/null)
[ "$CH" = "vga" ] || CH=hdmi
if [ "$(cat /sys/class/gpio/gpio41/value 2>/dev/null)" = "1" ]; then CUR=vga; else CUR=hdmi; fi
[ "$CH" = "$CUR" ] && exit 0

sh /usr/sbin/ccs_channel.sh "$CH"

# ccs_channel.sh 已写默认 720p EDID；若本次开机后网页应用过别的 EDID
# （/tmp/ccs_last_edid.txt，重启即清），补写覆盖之
SUBDEV=$(media-ctl -d /dev/media0 -p 2>/dev/null | awk '/tc35874x/{f=1} f && /device node name/{print $NF; exit}')
if [ -n "$SUBDEV" ] && [ -f /tmp/ccs_last_edid.txt ]; then
    v4l2-ctl -d "$SUBDEV" --set-edid file=/tmp/ccs_last_edid.txt,format=hex >/dev/null 2>&1
fi
exit 0
