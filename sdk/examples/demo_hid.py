#!/usr/bin/env python3
"""demo_hid.py — 鼠标点一下屏幕中心 + 按一下 Enter。

用法: python3 demo_hid.py [板子IP]（默认 192.168.1.222）

前提: 板子 Type-C 已通过 USB 连到被控上位机，HID gadget 已枚举
（板上 /dev/hidg0~3 存在，上位机识别出 "CCS KVM Keyboard/Mouse"）；
否则板上写 hidg 失败回 503，抛 CCSApiError。

实现说明：鼠标走 hidg3 笔 digitizer（与网页"鼠标模式"一致）而不是
hidg1 绝对鼠标——Windows 把绝对坐标鼠标锁死在主屏，digitizer 才能
落到扩展副屏。坐标是 0.0~1.0 归一化（0.5, 0.5 = 屏幕中心）。
"""
import sys

from ccs_sdk import Hid

host = sys.argv[1] if len(sys.argv) > 1 else '192.168.1.222'
h = Hid(host)

h.click(0.5, 0.5)
print('已在屏幕中心点击左键')

# 再演示一个键盘按键：回车
h.key('Enter')
print('已按 Enter')
