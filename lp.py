#!/usr/bin/env python3
# 触摸流式长按：60Hz 持续发 tip-down 报告 1.5s（模拟真实触摸控制器）
import time
x, y = int(32767*0.5), int(32767*0.5)
f = open('/dev/hidg2', 'wb', buffering=0)
def rpt(bits):
    f.write(bytes([bits, 0, x & 255, x >> 8, y & 255, y >> 8]))
rpt(3)                      # tip + in-range
t0 = time.time()
while time.time() - t0 < 1.6:
    rpt(3)
    time.sleep(0.016)
for _ in range(3):
    rpt(2)                  # 抬起（保持 in-range）
    time.sleep(0.016)
f.write(bytes([0, 0, 0, 0, 0, 0]))
print('long-press done')
