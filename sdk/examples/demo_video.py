#!/usr/bin/env python3
"""demo_video.py — 打印采集状态 + 抓一帧存 JPEG。

用法: python3 demo_video.py [板子IP]（默认 192.168.1.222）

演示 Video 的两个只读方法：
- stat()     → GET /stat 状态文本（fps/状态/档位/编码器/通道）
- snapshot() → 从 /stream 取一帧 JPEG（板上没有 /frame.jpg 单帧端点）

注意：板上无输入信号（stat 显示 0.0 fps / align failed）时 /stream 不出帧，
snapshot() 会在读超时后抛 CCSApiError——这是预期行为，不是 SDK 故障。
"""
import sys

from ccs_sdk import Video

host = sys.argv[1] if len(sys.argv) > 1 else '192.168.1.222'
v = Video(host)

print('stat:', v.stat().strip())

jpg = v.snapshot()
with open('snapshot.jpg', 'wb') as f:
    f.write(jpg)
print(f'已存 snapshot.jpg（{len(jpg)} 字节）')
