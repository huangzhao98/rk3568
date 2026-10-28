#!/bin/sh
# 40_hid_gadget.sh — RK3568-CCS USB OTG 模拟键鼠/触摸（configfs HID gadget）
# 用法：开机由 ccs.service → ccs_init.sh 调用，或手动 sh 40_hid_gadget.sh start|stop
# 依赖内核：CONFIG_USB_CONFIGFS=y CONFIG_USB_CONFIGFS_F_HID=y（SDK defconfig 已有）
# 设备：/dev/hidg0 = 键盘（boot protocol），/dev/hidg1 = 绝对坐标鼠标，
#       /dev/hidg2 = 单点触摸 digitizer（tip|inrange, contactID, X16, Y16），
#       /dev/hidg3 = 笔 digitizer（tip|barrel|inrange, X16, Y16）
# 上位机（Win/Linux）免驱识别为 "CCS KVM Keyboard/Mouse/Touch"。

G=/sys/kernel/config/usb_gadget/ccs_kvm
UDC=$(ls /sys/class/udc | head -n1)

start() {
    [ -d "$G" ] && { echo "already started"; return 0; }
    modprobe libcomposite 2>/dev/null
    mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config

    mkdir -p $G
    cd $G || return 1

    # 设备描述符：随便编但保持固定，被控端电脑按它识别
    echo 0x1d6b > idVendor    # Linux Foundation
    echo 0x0104 > idProduct   # Multifunction Composite Gadget
    echo 0x0100 > bcdDevice
    echo 0x0200 > bcdUSB

    mkdir -p strings/0x409
    echo "RK3568CCS0004"      > strings/0x409/serialnumber
    echo "RK3568-CCS"         > strings/0x409/manufacturer
    echo "CCS KVM Keyboard/Mouse" > strings/0x409/product

    mkdir -p configs/c.1/strings/0x409
    echo "HID Keyboard+Mouse" > configs/c.1/strings/0x409/configuration
    echo 100 > configs/c.1/MaxPower

    # ---- 功能 1：键盘（boot keyboard, 8 字节报告）----
    mkdir -p functions/hid.usb0
    echo 1 > functions/hid.usb0/protocol      # keyboard
    echo 1 > functions/hid.usb0/subclass      # boot interface
    echo 8 > functions/hid.usb0/report_length
    # 标准 boot 键盘报告描述符（63B；base64 写入——dash 的 printf 不认 \xNN，
    # 曾把转义字面量原样写进 report_desc 导致 Win 代码 10 描述符验证失败）
    echo BQEJBqEBBQcZ4CnnFQAlAXUBlQiBApUBdQiBA5UFdQEFCBkBKQWRApUBdQORA5UGdQgVACVlBQcZACllgQDA \
        | base64 -d > functions/hid.usb0/report_desc

    # ---- 功能 2：绝对坐标鼠标（远程桌面必须绝对坐标）----
    mkdir -p functions/hid.usb1
    echo 2 > functions/hid.usb1/protocol      # mouse
    echo 0 > functions/hid.usb1/subclass
    echo 6 > functions/hid.usb1/report_length # btn1 + x2 + y2 + wheel1
    # 绝对坐标鼠标报告描述符（65B，0~32767 逻辑范围）
    # 坑：描述符长度绝不能是 64 的倍数——64B 时正好装满 EP0 整包，
    # f_hid 不补 ZLP，主机无法判断传输结束 → 超时 → Win 代码 10
    # "I/O 请求已取消"（滚轮 Logical Min 用 16 位形式 +1 字节规避）。
    echo BQEJAqEBCQGhAAUJGQEpAxUAJQGVA3UBgQKVAXUFgQMFAQkwCTEWAAAm/391EJUCgQIJOBaB/yV/dQiVAYEGwMA= \
        | base64 -d > functions/hid.usb1/report_desc

    # ---- 功能 3：单点触摸 digitizer（6 字节报告）----
    # [tip|inrange|pad×6, contactID, X lo/hi, Y lo/hi]，Win10+ 识别为触摸屏
    mkdir -p functions/hid.usb2
    echo 0 > functions/hid.usb2/protocol      # none
    echo 0 > functions/hid.usb2/subclass
    echo 6 > functions/hid.usb2/report_length
    echo BQ0JBKEBCSKhAglCFQAlAXUBlQGBAgkygQJ1BpUBgQMJUXUIlQElH4ECBQEJMAkxFgAAJv9/dRCVAoECwMA= \
        | base64 -d > functions/hid.usb2/report_desc

    # ---- 功能 4：笔 digitizer（5 字节报告）----
    # [tip|barrel|inrange|pad×5, X lo/hi, Y lo/hi]
    # 为什么需要它：Windows 把"绝对坐标鼠标"整体锁死在主屏（0~32767 全范围
    # 都落在主屏，永远够不到扩展副屏），而 digitizer 类（触摸/笔）按显示器
    # 关联走，实测落在副屏。笔比触摸多"悬停有光标"，网页鼠标模式走这里。
    mkdir -p functions/hid.usb3
    echo 0 > functions/hid.usb3/protocol      # none
    echo 0 > functions/hid.usb3/subclass
    echo 5 > functions/hid.usb3/report_length
    echo BQ0JAqEBCSChAAlCCUQJMhUAJQF1AZUDgQJ1BZUBgQMFAQkwCTEWAAAm/391EJUCgQLAwA== \
        | base64 -d > functions/hid.usb3/report_desc

    ln -s functions/hid.usb0 configs/c.1/
    ln -s functions/hid.usb1 configs/c.1/
    ln -s functions/hid.usb2 configs/c.1/
    ln -s functions/hid.usb3 configs/c.1/

    # 强制 PHY 侧 peripheral：本板 Type-C 无 ID 脚，inno-usb2phy 的 iddig
    # 输入悬空抖动会把端口误判成 host（extcon USB-HOST 0/1 乱跳、drd 反复
    # 切 xhci），写 otg_mode=peripheral 让 GRF 强制 ID 为高（B-device）。
    [ -w /sys/bus/platform/devices/fe8a0000.usb2-phy/otg_mode ] && \
        echo peripheral > /sys/bus/platform/devices/fe8a0000.usb2-phy/otg_mode

    echo "$UDC" > UDC
    echo "gadget up on $UDC: /dev/hidg0(kbd) /dev/hidg1(mouse) /dev/hidg2(touch) /dev/hidg3(pen)"
}

stop() {
    [ -d "$G" ] || return 0
    cd $G
    echo "" > UDC
    rm -f configs/c.1/hid.usb0 configs/c.1/hid.usb1 configs/c.1/hid.usb2 configs/c.1/hid.usb3
    rmdir functions/hid.usb0 functions/hid.usb1 functions/hid.usb2 functions/hid.usb3
    rmdir configs/c.1/strings/0x409 configs/c.1
    rmdir strings/0x409
    cd /
    rmdir $G
    echo "gadget removed"
}

case "$1" in
    start) start ;;
    stop)  stop ;;
    *) echo "usage: $0 start|stop" ;;
esac
