#!/usr/bin/env python3
# align_probe12.py — 网页流相位精确测量（不占用 /dev/video0，可与 ccs_webcam 共存）
#
# 从 127.0.0.1:8080/stream 抓一帧 JPEG，算行亮度轮廓；
#   ref  模式：保存当前轮廓为基准 /root/phase_ref.npy
#   meas 模式：与基准做循环相关，输出相对卷动行数 k（0 = 与基准同相位）
# 判定：多次冷启动跑 meas，k 的分布即 boot 相位抖动。
import sys, io, urllib.request
import numpy as np
from PIL import Image

REF = '/root/phase_ref.npy'
H = 720


def grab_profile():
    req = urllib.request.urlopen('http://127.0.0.1:8080/stream', timeout=8)
    buf = b''
    while True:                                  # 读到第一个完整 JPEG
        chunk = req.read(65536)
        if not chunk:
            raise RuntimeError('流中断')
        buf += chunk
        i = buf.find(b'\xff\xd8\xff')
        if i >= 0:
            j = buf.find(b'\xff\xd9', i)
            if j > 0:
                break
    img = Image.open(io.BytesIO(buf[i:j + 2])).convert('L')
    a = np.asarray(img, dtype=np.float32)
    assert a.shape[0] == H, f'高度 {a.shape} 不是 720'
    return a[:, ::16].mean(axis=1)               # 行亮度轮廓（抽列加速）


def roll_of(cur, ref):
    best_k, best_d, sum_d = 0, 1e18, 0.0
    for k in range(H):
        d = float(np.abs(np.roll(ref, -k) - cur).mean())
        sum_d += d
        if d < best_d:
            best_d, best_k = d, k
    return best_k, best_d, sum_d / H


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'meas'
    p = grab_profile()
    if mode == 'ref':
        np.save(REF, p)
        print('[probe12] 基准已保存')
        return
    ref = np.load(REF)
    k, d, avg = roll_of(p, ref)
    tag = '可信' if d < 6 and d < 0.5 * avg else '匹配弱'
    print(f'[probe12] phase_offset={k:4d}  dist={d:6.2f} avg={avg:6.2f}  {tag}')


if __name__ == '__main__':
    main()
