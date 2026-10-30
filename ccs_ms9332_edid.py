#!/usr/bin/env python3
# ccs_ms9332_edid.py — 向板上 MS9332(HDMI 1分2分配器) 的 EDID RAM 写入一份 EDID
#
# 背景：电脑(上游)读的 EDID 由 MS9332 提供，单片机 MS8005F 上电时写入默认值。
# RK3568 与 MS8005F、MS9332 三方共用一条 I2C 总线(GPIO1_A0/A1, bit-bang,
# 需 i2c_sniff.ko，内核 i2c3 节点必须保持 disabled)。
# MS9332 协议(嗅探实测): 从机 0x59；寄存器地址 16 位，线上顺序 [低字节, 高字节]；
# EDID RAM 在 0x0d00 起 256 字节；**写前必须开窗口**: 0x000c bit7=1,
# 写完必须关窗口(=0)——窗口开着时 HDMI 口 DDC 停止供 EDID，电脑会认不到屏。
# MS8005F 每 ~112ms 轮询总线，所有操作走模块的空闲窗检测，互不冲突。
#
# 用法: ccs_ms9332_edid.py [edid.bin]   (默认 /etc/ccs_edid.bin，128 或 256 字节)
#       ccs_ms9332_edid.py --watch      写入后常驻看门狗：EDID 被单片机冲回默认
#                                       值（实测：开机后首次插拔上游 HDMI 可能触发
#                                       MS8005F 重写）时自动重写。每 10s 比对一次。
# 退出码 0=写入并校验通过；1=失败(窗口已尽量关回)。
import fcntl
import os
import subprocess
import sys
import time

MOD = '/usr/lib/modules/i2c_sniff.ko'
IO = '/proc/i2c_sniff_io'
LOCK = '/tmp/.ccs_ms9332.lock'
CHUNK = 16          # 16 字节一笔约 2ms，稳进 MS8005F 轮询间隙(~98ms)
ROUNDS = 8          # 校验失败重写的最大轮数


def io(cmd):
    with open(IO, 'w') as f:
        f.write(cmd + '\n')
    with open(IO) as f:
        return f.read().strip()


def rd(b1, b2, n):
    out = io('r %02x %02x %d' % (b1, b2, n))
    bs = out.split()
    if len(bs) != n:
        raise RuntimeError('read %02x%02x: %s' % (b2, b1, out[:60]))
    return bytes(int(x, 16) for x in bs)


def wr(b1, b2, data):
    out = io('w %02x %02x %s' % (b1, b2, ' '.join('%02x' % x for x in data)))
    if out:
        raise RuntimeError('write %02x%02x: %s' % (b2, b1, out[:60]))


def window(on):
    wr(0x0c, 0x00, [0x80 if on else 0x00])


def name_off(data):
    """EDID 里监视器名描述符(0xFC)的数据区偏移；没有返回 None"""
    for off in range(54, min(len(data), 126), 18):
        if data[off:off + 4] == b'\x00\x00\x00\xfc':
            return off + 5
    return None


def apply(path):
    """开窗口→分块写→逐块校验(坏块重写)→关窗口。返回 0/1"""
    data = open(path, 'rb').read()
    if len(data) not in (128, 256):
        print('EDID 长度须为 128/256 字节，当前 %d' % len(data))
        return 1
    lk = open(LOCK, 'w')
    fcntl.flock(lk, fcntl.LOCK_EX)          # 与网页端并发的写入互斥
    try:
        window(True)
        for off in range(0, len(data), CHUNK):      # 先整体写一遍
            wr(off & 0xff, 0x0d, data[off:off + CHUNK])
        for rnd in range(ROUNDS):                    # 逐块校验，坏块重写
            bad = []
            for off in range(0, len(data), CHUNK):
                want = data[off:off + CHUNK]
                if rd(off & 0xff, 0x0d, len(want)) != want:
                    bad.append((off, want))
            if not bad:
                print('OK: %s 已写入 MS9332 并校验通过(第%d轮)'
                      % (path, rnd + 1), flush=True)
                return 0
            for off, want in bad:
                wr(off & 0xff, 0x0d, want)
        print('FAIL: %d 轮后仍有块不一致' % ROUNDS, flush=True)
        return 1
    finally:
        try:
            window(False)                            # 任何结局都把窗口关回
        except Exception as e:
            print('WARN: 关窗口失败: %s' % e, file=sys.stderr)
        lk.close()


def probe_matches(data):
    """只读抽查：头16字节 + 名字区，与文件一致返回 True"""
    if rd(0x00, 0x0d, 16) != data[:16]:
        return False
    noff = name_off(data)
    if noff is not None:
        nb = bytes(data[noff:noff + 13])
        if rd(noff & 0xff, 0x0d, 13) != nb:
            return False
    return True


def watch(path):
    """常驻看门狗：EDID RAM 被 MS8005F 冲回默认就重写。每 10s 抽查一次
    （开窗口~30ms，DDC 只在插入瞬间被读，抽查对其基本无感）。"""
    data = open(path, 'rb').read()
    while True:
        time.sleep(10)
        lk = open(LOCK, 'w')
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            window(True)
            same = probe_matches(data)
            window(False)
        except Exception as e:
            print('WARN: 看门狗抽查失败: %s' % e, file=sys.stderr, flush=True)
            try:
                window(False)
            except Exception:
                pass
            lk.close()
            continue
        lk.close()
        if not same:
            print('看门狗：EDID 被冲掉，重写', flush=True)
            apply(path)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    path = args[0] if args else '/etc/ccs_edid.bin'
    if not os.path.exists(IO):
        subprocess.run(['insmod', MOD], check=True)
        time.sleep(0.3)
    rc = apply(path)
    if rc == 0 and '--watch' in sys.argv:
        watch(path)
    return rc


if __name__ == '__main__':
    sys.exit(main())
