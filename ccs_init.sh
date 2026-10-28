#!/bin/sh
# /usr/sbin/ccs_init.sh — RK3568-CCS 底板外设初始化（Ubuntu/systemd 版）
# 由 /etc/systemd/system/ccs.service 调用，也可手动执行 start|stop
# IO 依据：buildroot_dev/02_IO与复用配置总表.md（0916A 版）

case "$1" in
start)
    echo "[ccs] init start"

    # 0. rootfs 在线扩容（烧录后首次启动才有效，之后是 no-op）：
    #    分区 6GB 但 img 只有 ~770MB，不扩容 apt 索引都装不下
    if [ -b /dev/mmcblk0p6 ] && [ "$(findmnt -n -o SOURCE /)" = "/dev/mmcblk0p6" ]; then
        resize2fs /dev/mmcblk0p6 >/dev/null 2>&1
    fi

    # 1. USB HID gadget（模拟键鼠，USB3_OTG0 device 模式）
    sh /usr/sbin/40_hid_gadget.sh start

    # 1.4 采集通道恢复（上次网页里切换的结果存 /etc/ccs_channel，默认 HDMI）
    #     必须先于 EDID 写入：EDID 要写到当前选中的那颗 TC358743
    CH=$(cat /etc/ccs_channel 2>/dev/null)
    if [ "$CH" = "vga" ]; then
        echo "[ccs] restore channel: vga"
        sh /usr/sbin/ccs_channel.sh vga
    fi

    # 1.5 TC358743 HDMI 采集桥：写入 720p-only EDID
    #     EDID 存于芯片 RAM，断电/重启即丢，必须每次开机重写；
    #     写 EDID 会触发 HPD 翻转，源端随即重读并按 EDID 限输出 720p60，
    #     以适配 EVB 模块 2-lane MIPI 带宽（CCS 实物 4-lane 验证后可换回 1080p EDID）。
    EDID=/usr/sbin/edid_720p.hex.txt
    for i in $(seq 1 30); do
        [ -e /dev/media0 ] && break; sleep 1
    done
    SUBDEV=$(timeout 5 media-ctl -d /dev/media0 -p 2>/dev/null | awk '/tc35874x/{f=1} f && /device node name/{print $NF; exit}')
    if [ -n "$SUBDEV" ] && [ -f "$EDID" ]; then
        # 注意：v4l2-ctl 对该 subdev 会先报一个无害的 QUERYCAP 错误并使退出码变 255，
        # 不能用退出码判断，必须回读 EDID 内容验证（"CCS"=0x43 0x43 0x53）；
        # 套 timeout 防芯片 I2C 卡死时拖死 init
        timeout 8 v4l2-ctl -d "$SUBDEV" --set-edid file=$EDID,format=hex >/dev/null 2>&1
        if timeout 8 v4l2-ctl -d "$SUBDEV" --get-edid 2>/dev/null | grep -qa "43 43 53"; then
            echo "[ccs] tc35874x 720p EDID -> $SUBDEV"
        else
            echo "[ccs] WARN: EDID 写入未生效"
        fi
    else
        echo "[ccs] WARN: tc35874x subdev 或 EDID 文件未找到，跳过 EDID 写入"
    fi

    # 2. MS9288C（VGA→HDMI 转换）：固件独立运行，SCL/SDA 脚未连接（原理图
    #    打×），无需也无法 I2C 配置。i2c3 经 JP3/JP4 跳线接 MS 总线
    #    （MS9332 环出分配器/MS8005F 单片机），仅工厂调试用。

    # 3. 视频采集通道选择（TMUX121 SEL = GPIO1_B1，线名 "5G_RESET"）
    #    0=HDMI 路（默认，R80 15k 下拉），1=VGA 路
    #    注意：SW2 拨码闭合时硬件强制 VGA 路，此处设置无效（也不应反向驱动）
    # gpioset $(gpiofind "5G_RESET")=0

    # 4. 工作灯已由 dts gpio-leds（ccs:working=GPIO0_C0 heartbeat）接管；
    #    四色 LED 用线名直控：
    #    gpioset $(gpiofind "LED_RED_STATE")=1    # GPIO2_C6 → LED14 红
    #    注：以下四路现按原理图改为外部信号检测输入（网页只读显示），
    #    映射：绿=GPIO2_B2 黄=GPIO2_B1 蓝=GPIO2_C5 红=GPIO2_C6（B1/B2 勿反）
    #    gpioset $(gpiofind "LED_GREEN_STATE")=1  # GPIO2_B2 → LED16 绿
    #    gpioset $(gpiofind "LED_YELLOW_STATE")=1 # GPIO2_B1 → LED15 黄
    #    gpioset $(gpiofind "LED_BLUE_STATE")=1   # GPIO2_C5 → LED17 蓝

    # 5. PICO（RP2040）说明：
    #    SW10 拨码闭合后 PICO 的 USB 切到板内 USB2_HOST3，
    #    枚举为 /dev/ttyACM0（内核需 CONFIG_USB_ACM=y）；
    #    SW10 断开时 PICO 连 USB4 Type-C（外部供电/通讯/刷机）。
    #    SW11=BOOTSEL：闭合后上电/复位进 USB bootloader，
    #    配合 SW10 闭合可用 picotool 从 RK3568 在线刷 PICO 固件。
    #    无需脚本动作，仅注释备查。

    # 6. 视频采集守护（采集→rkmpp 编码→推流），二期
    #    采集节点：/dev/video0（rkcif id0 = VC0；media 拓扑里 id0~id3 全 ENABLED，
    #    数据只落在 id0）。格式 UYVY 1280x720。抓帧验证：
    #    v4l2-ctl -d /dev/video0 --set-fmt-video=width=1280,height=720,pixelformat=UYVY
    #    v4l2-ctl -d /dev/video0 --stream-mmap --stream-count=100 --stream-to=/dev/null
    #    （注意：v4l2-ctl 1.22 的 --stream-to 在本驱动上写文件为空（0 字节），
    #     落盘抓帧用 /usr/sbin/v4l2_cap.py）
    # /usr/bin/ccs_capture &

    # 6.1 网页实时预览（调试用，MJPEG over HTTP，访问 http://<IP>:8080/）
    #     依赖 python3-numpy + python3-pil（apt 装，烧录后需重装或打入 rootfs）
    # nohup python3 /usr/sbin/ccs_webcam.py --port 8080 >/var/log/ccs_webcam.log 2>&1 &

    # 7. 键鼠/触摸注入：已由 ccs_webcam.py 实现（网页捕获浏览器键鼠事件
    #    → POST /hid → 写 /dev/hidg0/1/2 → 上位机）。hidg 设备由第 1 步
    #    40_hid_gadget.sh 创建；ccs_webcam.py 启动时也会自检补建。
    #    RP2040 透传方案废弃（网页直连 hidg 链路更短、免单片机固件）。

    echo "[ccs] init done"
    ;;
stop)
    sh /usr/sbin/40_hid_gadget.sh stop
    ;;
*)
    echo "usage: $0 start|stop"
    ;;
esac
exit 0
