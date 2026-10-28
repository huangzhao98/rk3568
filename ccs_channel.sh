#!/bin/sh
# ccs_channel.sh hdmi|vga — 采集通道切换（TMUX121 选择器）
#
# SEL = GPIO1_B1 = gpio41：0=HDMI 路（默认，R80 下拉），1=VGA 路。
# MIPI 4-lane 与 I2C4（U20）都经 TMUX121 随 SEL 切换，两路 TC358743
# 不会同时上总线，设备树始终只有 4-000f 一个节点。
#
# 复位脚分工：HDMI 路 = GPIO3_B6 = gpio110（设备树 reset-gpios，驱动持有）；
#             VGA  路 = GPIO3_B5 = gpio109（无人认领，须 sysfs 持有）。
# 两颗芯片复位节点均有 18k 下拉，松手（高阻）即回复位态：
#   - 切 HDMI：打完复位必须 unexport 110 还回给驱动，否则 bind 时
#     驱动 gpiod_get 失败 probe 不起来；
#   - 切 VGA ：必须保持 109 export 且 out hi，否则 18k 下拉把芯片
#     拉回复位态，probe 找不到芯片。
#
# 采集服务调用前须先停流（流式中 unbind 会让 rkcif 内核崩溃）。
# 选择结果写 /etc/ccs_channel，开机由 ccs_init.sh 恢复。
#
# 注意：SW2 拨码闭合时硬件强制 VGA 路，本脚本设置无效。

CH="$1"
[ "$CH" = "hdmi" ] || [ "$CH" = "vga" ] || { echo "usage: $0 hdmi|vga" >&2; exit 1; }

SEL=41
B=/sys/bus/i2c/drivers/tc35874x
D=4-000f
G=/sys/class/gpio
EDID=/usr/sbin/edid_720p.hex.txt

if [ "$CH" = vga ]; then SELV=1; RST=109; else SELV=0; RST=110; fi

echo $D > $B/unbind 2>/dev/null
sleep 0.2

# SEL 保持 export：unexport 回高阻会被 R80 下拉拉回 HDMI
echo $SEL > $G/export 2>/dev/null
echo out > $G/gpio$SEL/direction
echo $SELV > $G/gpio$SEL/value
sleep 0.1

# 复位目标芯片
echo $RST > $G/export 2>/dev/null
echo out > $G/gpio$RST/direction
echo 0 > $G/gpio$RST/value
sleep 0.3
echo 1 > $G/gpio$RST/value
sleep 0.2
[ "$CH" = hdmi ] && echo $RST > $G/unexport 2>/dev/null
sleep 0.3

echo $D > $B/bind 2>/dev/null

# 等驱动 probe 完成（超时即失败，芯片没在总线上应答）
i=0
while [ $i -lt 10 ]; do
    [ -e $B/$D ] && break
    sleep 0.5
    i=$((i + 1))
done
if [ ! -e $B/$D ]; then
    echo "ccs_channel: $D probe 失败（$CH 路芯片无应答）" >&2
    exit 1
fi

# unbind 后 media 链路失效，必须重接，否则 "No link between dphy and sensor"
# （v4l2-ctl/media-ctl 一律套 timeout：芯片 I2C 卡死时防把调用方拖死）
i=0
while [ $i -lt 8 ]; do
    timeout 5 media-ctl -d /dev/media0 -l "'m00_b_tc35874x 4-000f':0 -> 'rockchip-csi2-dphy0':0 [1]" 2>/dev/null && break
    sleep 1
    i=$((i + 1))
done

# 新选中芯片的 EDID RAM 需要写入（先写出厂默认；网页层有"上次 EDID"会再覆盖）
SUBDEV=$(timeout 5 media-ctl -d /dev/media0 -p 2>/dev/null | awk '/tc35874x/{f=1} f && /device node name/{print $NF; exit}')
if [ -n "$SUBDEV" ] && [ -f "$EDID" ]; then
    timeout 5 v4l2-ctl -d "$SUBDEV" --set-edid file=$EDID,format=hex >/dev/null 2>&1
    timeout 5 v4l2-ctl -d "$SUBDEV" --get-edid 2>/dev/null | grep -qa "43 43 53" \
        && echo "ccs_channel: EDID -> $SUBDEV" || echo "ccs_channel: WARN EDID 写入未生效" >&2
fi

echo "$CH" > /etc/ccs_channel 2>/dev/null
echo "ccs_channel: $CH ok"
