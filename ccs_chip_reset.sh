#!/bin/sh
# TC358743 硬复位：芯片 I2C 挂死（驱动轮询全部超时）时由 ccs_webcam 看门狗调用。
# 复位脚被驱动占用，须先 unbind 驱动释放引脚，打完复位再 rebind 重探测。
# 调用方负责随后重走对齐（need_realign）。
#
# 复位脚按当前通道选择（SEL=gpio41，1=VGA）：
#   HDMI 路 = gpio110（设备树 reset-gpios，bind 前须 unexport 还回驱动）
#   VGA  路 = gpio109（须保持 export out hi，18k 下拉松手即复位）
B=/sys/bus/i2c/drivers/tc35874x
D=4-000f
G=/sys/class/gpio

if [ "$(cat $G/gpio41/value 2>/dev/null)" = "1" ]; then RST=109; else RST=110; fi

echo $D > $B/unbind 2>/dev/null
sleep 0.2
echo $RST > $G/export 2>/dev/null
echo out > $G/gpio$RST/direction 2>/dev/null
echo 0 > $G/gpio$RST/value 2>/dev/null
sleep 0.3
echo 1 > $G/gpio$RST/value 2>/dev/null
sleep 0.2
[ $RST -eq 110 ] && echo $RST > $G/unexport 2>/dev/null
sleep 0.3
echo $D > $B/bind 2>/dev/null
# unbind/rebind 后 media 链路掉回默认（dphy<-sensor 不再 ENABLED），
# 不重接的话 rkcif stream on 报 "No link between dphy and sensor" 收不到帧。
# 实体注册需要时间，重试几次
i=0
while [ $i -lt 8 ]; do
    media-ctl -d /dev/media0 -l "'m00_b_tc35874x 4-000f':0 -> 'rockchip-csi2-dphy0':0 [1]" 2>/dev/null && break
    sleep 1
    i=$((i + 1))
done
