#!/usr/bin/env python3
"""demo_gpio.py — 打印四路外部输入电平（LED_x_STATE，只读）。

用法: python3 demo_gpio.py [板子IP]（默认 192.168.1.222）

硬件背景：四路外部 5V 信号经 TXS0104E 电平转换降到 1.8V 进 SoC
（GPIO2_B1=73 黄 / GPIO2_B2=74 绿 / GPIO2_C5=85 蓝 / GPIO2_C6=86 红），
同时直接驱动 LED。这些脚务必保持高阻输入，板上 API 也只读——
本示例只调 GET /gpio_vals，不碰 /gpio_set。
"""
import sys

from ccs_sdk import Gpio

host = sys.argv[1] if len(sys.argv) > 1 else '192.168.1.222'
g = Gpio(host)

for name, level in g.read_inputs().items():
    print(f'{name}: {"高" if level else "低"}')
