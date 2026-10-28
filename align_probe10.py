#!/usr/bin/env python3
# align_probe10.py — TC358743 硬件相位确定性第三轮实验（2026-10-26）
#
# 背景：probe9 已证 HDMIRST/CTXRST 复位后相位仍随机。本轮试两个未碰过的途径：
#   1. HV_RST(0x85AF) 的 V_PI_RST(0x10)/H_PI_RST(0x20)：驱动仅作"同步出界自动
#      复位"使能位用（tc358743.c:765），从未试过开流中手动打。若写 1 能让 V 通路
#      在下一个输入 vsync 重建相位，则卷动相位收敛为常数 → 确定性硬件对齐。
#   2. 寄存器扫描：驱动头文件只覆盖用到的寄存器（大量 Not in REF_01），
#      0x8500~0x86FF / 0x0400~0x05FF 可能有未文档化的实时行计数器；找到后配合
#      帧边界时刻即可算出相位，任意画面零图像分析。
#
# 用法（先 systemctl stop ccs_webcam 释放 /dev/video0）：
#   python3 align_probe10.py scan                # 寄存器扫描找计数器
#   python3 align_probe10.py vrst [轮数=8]       # HV_RST V_PI_RST 相位复位实验
#   python3 align_probe10.py hrst [轮数=8]       # HV_RST H_PI_RST
#   python3 align_probe10.py hvrst [轮数=8]      # 两位一起
#   python3 align_probe10.py fiforst [轮数=8]    # FIFOCTL(0x0006) 复位实验
#   python3 align_probe10.py cscal 0x85XX [轮数=16]  # 候选计数器与帧边界相关
# 判定：vrst 第 2 轮起 delta 全 0(±1) → 确定性成立；scan 找出每轮都变的寄存器。
# 安全：全程 20Hz 清中断锁存防驱动轮询静音；流死了 systemctl start ccs_webcam 恢复。
import os, sys, fcntl, mmap, select, struct, ctypes, threading, time

import numpy as np

VIDIOC_REQBUFS   = 0xC0145608
VIDIOC_QUERYBUF  = 0xC0585609
VIDIOC_QBUF      = 0xC058560F
VIDIOC_DQBUF     = 0xC0585611
VIDIOC_STREAMON  = 0x40045612
VIDIOC_STREAMOFF = 0x40045613
MPLANE, MEM_MMAP = 9, 1

HV_RST   = 0x85AF
FIFOCTL  = 0x0006
VI_MUTE  = 0x857F   # bit4 MASK_VI_MUTE：视频通路静音（不动 RX/TX 复位）
FORCE_MUTE = 0x8600 # bit0 FORCE_DMUTE：数据静音
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

    def rd8(self, reg):
        return self._xfer([reg >> 8, reg & 0xff], 1)[0]

    def rd16(self, reg):
        r = self._xfer([reg >> 8, reg & 0xff], 2)
        return r[0] | (r[1] << 8)

    def wr16(self, reg, val):
        self._xfer([reg >> 8, reg & 0xff, val & 0xff, (val >> 8) & 0xff])

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


def setup_media():
    os.system("for ent in 'm00_b_tc35874x 4-000f:0' 'rockchip-csi2-dphy0:0' "
              "'rockchip-csi2-dphy0:1' 'rockchip-mipi-csi2:0' 'rockchip-mipi-csi2:4'; do "
              "media-ctl -d /dev/media0 --set-v4l2 '\"'$ent'\"[fmt:UYVY8_2X8/1280x720]' "
              "2>/dev/null; done")
    os.system('v4l2-ctl -d /dev/video0 --set-fmt-video=width=1280,height=720,'
              'pixelformat=UYVY 2>/dev/null')
    fmt = os.popen('v4l2-ctl -d /dev/video0 --get-fmt-video 2>/dev/null').read()
    assert '1280/720' in fmt and "'UYVY'" in fmt, f'节点格式不对:\n{fmt}'


# ---------------- scan：找自由运行计数器 ----------------
def mode_scan(tc):
    fd, maps = open_cap()
    for _ in range(3):
        grab(fd, maps)
    ranges = [(0x0400, 0x0600), (0x8500, 0x8700)]
    dumps = []
    for rnd in range(3):
        snap = {}
        for lo, hi in ranges:
            for reg in range(lo, hi, 2):
                try:
                    snap[reg] = tc.rd16(reg)
                except OSError:
                    pass
        dumps.append(snap)
        time.sleep(0.02)
    fcntl.ioctl(fd, VIDIOC_STREAMOFF, struct.pack('<I', MPLANE))
    os.close(fd)
    print('[scan] 每轮都变化的寄存器（候选计数器/实时状态）：')
    n_chg = 0
    for reg in sorted(dumps[0]):
        vs = [d.get(reg) for d in dumps]
        if None in vs:
            continue
        if len(set(vs)) > 1:
            n_chg += 1
            print(f'  0x{reg:04X}: {vs[0]:5d} -> {vs[1]:5d} -> {vs[2]:5d}')
    print(f'[scan] 共 {len(dumps[0])} 个寄存器，{n_chg} 个有变化')


# ---------------- 复位实验（vrst/hrst/hvrst/fiforst） ----------------
def mode_reset(tc, mode, rounds):
    fd, maps = open_cap()
    for _ in range(5):
        if grab(fd, maps) is None:
            break
    f0 = grab(fd, maps)
    assert f0, '开流后拿不到帧'
    print(f'[probe10] mode={mode} rounds={rounds}, 首帧 OK', flush=True)

    deltas = []
    try:
        for i in range(rounds):
            if mode == 'fiforst':
                v = tc.rd16(FIFOCTL)
                tc.wr16(FIFOCTL, 0x0001)
                time.sleep(0.05)
                tc.wr16(FIFOCTL, v)
            elif mode == 'vmute':
                v = tc.rd8(VI_MUTE)
                tc.wr8(VI_MUTE, v | 0x10)
                time.sleep(0.1)
                tc.wr8(VI_MUTE, v & ~0x10)
            elif mode == 'dmute':
                v = tc.rd8(FORCE_MUTE)
                tc.wr8(FORCE_MUTE, v | 0x01)
                time.sleep(0.1)
                tc.wr8(FORCE_MUTE, v & ~0x01)
            else:
                mask = {'vrst': 0x10, 'hrst': 0x20, 'hvrst': 0x30}[mode]
                v = tc.rd8(HV_RST)
                tc.wr8(HV_RST, v | mask)
                time.sleep(0.05)
                tc.wr8(HV_RST, v)
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
        fcntl.ioctl(fd, VIDIOC_STREAMOFF, struct.pack('<I', MPLANE))
        os.close(fd)

    tail = deltas[1:]
    ok = len(tail) >= 3 and all(abs(x) <= 1 or abs(x - H) <= 1 for x in tail)
    print(f'[probe10] deltas={deltas}')
    print(f'[probe10] 结论: {"确定性成立（复位后相位恒定）" if ok else "不成立（相位仍随机或复位无效）"}')


# ---------------- cscal：候选计数器与帧边界相关 ----------------
def mode_cscal(tc, reg, rounds):
    fd, maps = open_cap()
    grab(fd, maps)
    print(f'[cscal] reg=0x{reg:04X} rounds={rounds}：每轮排干队列→等新帧→DQBUF 返回'
          f'后立即读寄存器（DQBUF 返回≈帧刚完成，延迟<1ms）', flush=True)
    vals = []
    try:
        for i in range(rounds):
            while True:                          # 排干
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
            r, _, _ = select.select([fd], [], [], 4)
            if not r:
                print('无帧，退出'); break
            b = v4l2_buffer(0)
            fcntl.ioctl(fd, VIDIOC_DQBUF, b, True)   # 阻塞拿到刚完成的帧
            v = tc.rd16(reg)                         # 立即读计数器
            idx = struct.unpack_from('<I', b, 0)[0]
            fcntl.ioctl(fd, VIDIOC_QBUF, v4l2_buffer(idx), True)
            vals.append(v)
            print(f'round {i + 1}: cnt={v}', flush=True)
    finally:
        fcntl.ioctl(fd, VIDIOC_STREAMOFF, struct.pack('<I', MPLANE))
        os.close(fd)
    if len(vals) >= 4:
        spread = max(vals) - min(vals)
        print(f'[cscal] vals={vals}')
        print(f'[cscal] 极差={spread}：{"稳定（可用作相位测量，极差越小越好）" if spread <= 60 else "跳动大，该寄存器与帧边界无固定关系"}')


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'scan'
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 8
    setup_media()
    tc = Tc()
    stop = threading.Event()
    threading.Thread(target=clear_latches, args=(tc, stop), daemon=True).start()
    try:
        if mode == 'scan':
            mode_scan(tc)
        elif mode in ('vrst', 'hrst', 'hvrst', 'fiforst', 'vmute', 'dmute'):
            mode_reset(tc, mode, rounds)
        elif mode == 'cscal':
            reg = int(sys.argv[2], 16)
            rr = int(sys.argv[3]) if len(sys.argv) > 3 else 16
            mode_cscal(tc, reg, rr)
        else:
            sys.exit(f'未知模式 {mode}')
    finally:
        stop.set()


if __name__ == '__main__':
    main()
