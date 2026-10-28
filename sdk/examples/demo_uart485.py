#!/usr/bin/env python3
"""demo_uart485.py — RS485 发 "Hello" 并打印 3 秒内收到的数据。

用法: python3 demo_uart485.py [板子IP]（默认 192.168.1.222）

硬件背景：RS485 = /dev/ttyS4（UART4，JP5A 出），方向由 Q10(SS8050)
硬件自动切换，脚本不需要（也无法）控制收发方向。

演示 since 偏移的用法：板上接收缓冲是环形 64KB，since 是逻辑偏移——
先发再拉时把起点设为当前 next，跳过存量缓冲，只收发送之后的新数据。
如需收发循环，也可以用 UartConsole（后台线程 + 回调），见
ccs_sdk/uart.py 的类 docstring。
"""
import sys
import time

from ccs_sdk import Uart

host = sys.argv[1] if len(sys.argv) > 1 else '192.168.1.222'
u = Uart(host)

print(u.send(Uart.RS485, 'Hello'))          # 默认追加 \r\n（crlf=True）

since = 0
_, since = u.recv(Uart.RS485, since)        # 跳过存量缓冲，只收新数据
t0 = time.time()
while time.time() - t0 < 3:
    data, since = u.recv(Uart.RS485, since)
    if data:
        print(f'收到: {data!r}')
    time.sleep(0.3)
print('3 秒结束')
