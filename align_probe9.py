#!/usr/bin/env python3
# align_probe9.py — TC358743 开流中复位的相位确定性实验（2026-10-26）
#
# 原理：存档 §8 实测"线路静默时开流、信号恢复后第一帧必完整（对齐）"。
# 本实验在 rkcif 保持开流的状态下，用 i2c 打 SYSCTL(0x0002) 的
# HDMIRST(0x0100, HDMI RX 复位) 或 CTXRST(0x0200, CSI TX 复位)，人为制造
# 静默→重锁。若每次复位后相位收敛到同一常数（表现为相邻两轮 delta=0），
# 则"复位即对齐"成立，开机可做到确定性相位、完全不依赖画面内容。
#
# 用法（先 systemctl stop ccs_webcam 释放 /dev/video0）：
#   python3 align_probe9.py [hdmirst|ctxrst] [轮数=8]
# 判定：第 2 轮起 delta 全为 0（±1）→ 确定性成立；delta 乱跳 → 不成立。
# 安全：全程 20Hz 清中断锁存防驱动轮询静音；流死了 systemctl start ccs_webcam 即恢复。
import os, sys, fcntl, mmap, select, struct, ctypes, threading, time

import numpy as np

VIDIOC_REQBUFS   = 0xC0145608
VIDIOC_QUERYBUF  = 0xC0585609
VIDIOC_QBUF      = 0xC058560F
VIDIOC_DQBUF     = 0xC0585611
VIDIOC_STREAMON  = 0x40045612
VIDIOC_STREAMOFF = 0x40045613
MPLANE, MEM_MMAP = 9, 1

SYSCTL = 0x0002
STARTCNTRL = 0x0204
CSI_START = 0x0518
MASKS = {'hdmirst': 0x0100, 'ctxrst': 0x0200, 'ctxkick': 0x0200}

mode = sys.argv[1] if len(sys.argv) > 1 else 'hdmirst'
MASK = MASKS[mode]
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 8
W, H = 1280, 720
NBYTES = W * H * 2


# ---------------- i2c（寄存器值小端，与驱动 i2c_rdreg/i2c_wrreg 一致） ----------------
class Tc:
    def __init__(self, bus=4, addr=0x0f):
        self.fd = os.open(f'/dev/i2c-{bus}', os.O_RDWR)
        self.addr = addr

    def _xfer(self, wbuf, rlen=0):
        msgs = bytearray()
        wd = ctypes.create_string_buffer(bytes(wbuf), len(wbuf))
        msgs += struct.pack('<HHH', self.addr, 0, len(wbuf)) + b'\0\0' + struct.pack('<Q', ctypes.addressof(wd))
        n = 1
        rd = None
        if rlen:
            rd = ctypes.create_string_buffer(rlen)
            msgs += struct.pack('<HHH', self.addr, 1, rlen) + b'\0\0' + struct.pack('<Q', ctypes.addressof(rd))
            n = 2
        m = ctypes.create_string_buffer(bytes(msgs), len(msgs))
        fcntl.ioctl(self.fd, 0x0707, struct.pack('<QI', ctypes.addressof(m), n))   # I2C_RDWR
        return rd.raw if rlen else None

    def rd16(self, reg):
        r = self._xfer([reg >> 8, reg & 0xff], 2)
        return r[0] | (r[1] << 8)

    def wr16(self, reg, val):
        self._xfer([reg >> 8, reg & 0xff, val & 0xff, (val >> 8) & 0xff])

    def wr32(self, reg, val):
        self._xfer([reg >> 8, reg & 0xff, val & 0xff, (val >> 8) & 0xff,
                    (val >> 16) & 0xff, (val >> 24) & 0xff])

    def wr8(self, reg, val):
        self._xfer([reg >> 8, reg & 0xff, val])


def clear_latches(tc, stop):
    while not stop.is_set():
        try:
            for reg in range(0x8502, 0x8510):
                tc.wr8(reg, 0xff)
        except OSError:
            pass
        time.sleep(0.05)


# ---------------- V4L2 MPLANE mmap 抓帧 ----------------
plane_ctypes = (ctypes.c_char * 64)()
plane_ptr = ctypes.addressof(plane_ctypes)


def v4l2_buffer(index):
    b = bytearray(88)
    struct.pack_into('<I', b, 0, index)
    struct.pack_into('<I', b, 4, MPLANE)
    struct.pack_into('<I', b, 60, MEM_MMAP)
    struct.pack_into('<Q', b, 64, plane_ptr)
    struct.pack_into('<I', b, 72, 1)
    return b


def open_cap():
    fd = os.open('/dev/video0', os.O_RDWR)
    req = bytearray(20)
    struct.pack_into('<IIII', req, 0, 4, MPLANE, MEM_MMAP, 0)
    fcntl.ioctl(fd, VIDIOC_REQBUFS, req, True)
    nbuf = struct.unpack_from('<I', req, 0)[0]
    assert nbuf >= 2, f'REQBUFS 只给了 {nbuf} 个'
    maps = []
    for i in range(nbuf):
        ctypes.memset(plane_ptr, 0, 64)
        fcntl.ioctl(fd, VIDIOC_QUERYBUF, v4l2_buffer(i), True)
        plen = struct.unpack_from('<I', bytes(plane_ctypes), 4)[0]
        poff = struct.unpack_from('<I', bytes(plane_ctypes), 8)[0]
        maps.append(mmap.mmap(fd, plen, offset=poff))
        fcntl.ioctl(fd, VIDIOC_QBUF, v4l2_buffer(i), True)
    fcntl.ioctl(fd, VIDIOC_STREAMON, struct.pack('<I', MPLANE))
    return fd, maps


def grab(fd, maps, timeout=4.0):
    """排干队列后等一帧新帧，返回最新帧 bytes；超时/异常返回 None。"""
    while True:                                  # 排干
        r, _, _ = select.select([fd], [], [], 0)
        if not r:
            break
        b = v4l2_buffer(0)
        try:
            fcntl.ioctl(fd, VIDIOC_DQBUF, b, True)
        except OSError:
            break
        idx = struct.unpack_from('<I', b, 0)[0]
        fcntl.ioctl(fd, VIDIOC_QBUF, v4l2_buffer(idx), True)
    r, _, _ = select.select([fd], [], [], timeout)
    if not r:
        return None
    got = None
    while True:
        b = v4l2_buffer(0)
        try:
            fcntl.ioctl(fd, VIDIOC_DQBUF, b, True)
        except OSError:
            break
        idx = struct.unpack_from('<I', b, 0)[0]
        maps[idx].seek(0)
        d = maps[idx].read(NBYTES)
        if len(d) == NBYTES:
            got = d
        fcntl.ioctl(fd, VIDIOC_QBUF, v4l2_buffer(idx), True)
        r, _, _ = select.select([fd], [], [], 0)
        if not r:
            break
    return got


# ---------------- 相位测量（行亮度轮廓循环相关） ----------------
def profile(raw):
    a = np.frombuffer(raw, dtype=np.uint8).reshape(H, W * 2)
    return a[:, 1::16].astype(np.float32).mean(axis=1)


def roll_of(cur, ref):
    """k：cur 第 b 行 = ref 第 (b+k)%H 行（与 _measure_roll 同约定）。"""
    pc, pr = profile(cur), profile(ref)
    best_k, best_d, sum_d = 0, 1e18, 0.0
    for k in range(H):
        d = float(np.abs(np.roll(pr, -k) - pc).mean())
        sum_d += d
        if d < best_d:
            best_d, best_k = d, k
    return best_k, best_d, sum_d / H


def main():
    os.system("for ent in 'm00_b_tc35874x 4-000f:0' 'rockchip-csi2-dphy0:0' "
              "'rockchip-csi2-dphy0:1' 'rockchip-mipi-csi2:0' 'rockchip-mipi-csi2:4'; do "
              "media-ctl -d /dev/media0 --set-v4l2 '\"'$ent'\"[fmt:UYVY8_2X8/1280x720]' "
              "2>/dev/null; done")
    os.system('v4l2-ctl -d /dev/video0 --set-fmt-video=width=1280,height=720,'
              'pixelformat=UYVY 2>/dev/null')
    fmt = os.popen('v4l2-ctl -d /dev/video0 --get-fmt-video 2>/dev/null').read()
    assert '1280/720' in fmt and "'UYVY'" in fmt, f'节点格式不对:\n{fmt}'

    tc = Tc()
    stop = threading.Event()
    threading.Thread(target=clear_latches, args=(tc, stop), daemon=True).start()

    fd, maps = open_cap()
    for _ in range(5):                           # 丢弃开流头几帧（可能不完整）
        if grab(fd, maps) is None:
            break
    f0 = grab(fd, maps)
    assert f0, '开流后拿不到帧'
    print(f'[probe9] mode={mode} mask=0x{MASK:04x} rounds={ROUNDS}, 首帧 OK', flush=True)

    deltas = []
    try:
        for i in range(ROUNDS):
            v = tc.rd16(SYSCTL)
            tc.wr16(SYSCTL, v | MASK)
            time.sleep(0.05)
            tc.wr16(SYSCTL, v & ~MASK)
            if mode == 'ctxkick':
                # CTXRST 后 TX 死掉，需按驱动 set_csi 末尾的顺序踢一脚
                tc.wr32(STARTCNTRL, 1)
                tc.wr32(CSI_START, 1)
            f1 = grab(fd, maps)                  # 复位后首帧可能撕裂，丢
            f1 = grab(fd, maps) if f1 is not None else None
            if f1 is None:
                print(f'round {i + 1}: 复位后 4s 无帧，流死了！', flush=True)
                break
            k, d, avg = roll_of(f1, f0)
            deltas.append(k)
            print(f'round {i + 1}: delta={k:4d}  dist={d:6.2f} avg={avg:6.2f} '
                  f'{"<-- 可信" if d < 6 and d < 0.5 * avg else "(匹配弱)"}', flush=True)
            f0 = f1
    finally:
        stop.set()
        fcntl.ioctl(fd, VIDIOC_STREAMOFF, struct.pack('<I', MPLANE))
        os.close(fd)

    tail = deltas[1:]
    ok = len(tail) >= 3 and all(abs(x) <= 1 or abs(x - H) <= 1 for x in tail)
    print(f'[probe9] deltas={deltas}')
    print(f'[probe9] 结论: {"确定性成立（复位后相位恒定）" if ok else "不成立（相位仍随机）"}')


if __name__ == '__main__':
    main()
