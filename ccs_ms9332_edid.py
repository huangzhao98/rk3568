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
#       ccs_ms9332_edid.py --watch      写入后常驻看门狗：EDID 被冲回默认时自动
#                                       重写（实测：开机早期/个别拔插后会触发）。
#                                       每 10s 比对一次。
# 写入成功后自动做 TC358743 HPD 脉冲（模拟热插拔，经 MS9332 传导到上游口，
# 2026-10-30 实测电脑秒级重新枚举读到新 EDID）——**改 EDID 免物理拔线**。
# 仅在 TMDS 有信号时脉冲：无信号说明源没输出（开机 HPD 门控期/源休眠），源
# 下次启动会直接读到新 EDID，且不可打扰驱动 HPD 门控（存档 §23）。
# 退出码 0=写入并校验通过；1=失败(窗口已尽量关回)。
import ctypes
import fcntl
import os
import struct
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
    last = ''
    for _ in range(5):          # 偶发与 MS8005F 轮询撞车(NACK)，50ms 退避重试
        out = io('r %02x %02x %d' % (b1, b2, n))
        bs = out.split()
        if len(bs) == n:
            return bytes(int(x, 16) for x in bs)
        last = out
        time.sleep(0.05)
    raise RuntimeError('read %02x%02x: %s' % (b2, b1, last[:60]))


def wr(b1, b2, data):
    last = ''
    for _ in range(5):
        out = io('w %02x %02x %s' % (b1, b2, ' '.join('%02x' % x for x in data)))
        if not out:
            return
        last = out
        time.sleep(0.05)
    raise RuntimeError('write %02x%02x: %s' % (b2, b1, last[:60]))


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


def hpd_pulse():
    """EDID 变更后让电脑重读：TC358743 HPD 拉低 2s 再拉高，经 MS9332
    传导到上游口（2026-10-30 实测：电脑秒级重新枚举，免物理拔插）。
    仅在 TMDS 有信号时脉冲——无信号说明源没输出（开机 HPD 门控期/源
    休眠），源下次启动会直接读到新 EDID，且门控期不能碰 HPD（§23）。"""
    fd = None
    try:
        addr = None
        for d in os.listdir('/sys/bus/i2c/devices'):
            try:
                with open('/sys/bus/i2c/devices/%s/name' % d) as f:
                    if not f.read().strip().startswith('tc35874'):
                        continue
                bus, a = d.split('-')
                fd = os.open('/dev/i2c-%s' % bus, os.O_RDWR)
                addr = int(a, 16)
                break
            except (OSError, ValueError):
                continue
        if fd is None:
            print('HPD: 找不到 tc35874x，跳过脉冲', flush=True)
            return

        def rw(reg, val=None):
            if val is None:
                w = ctypes.create_string_buffer(bytes([reg >> 8, reg & 0xff]), 2)
                r = ctypes.create_string_buffer(1)
                msgs = ctypes.create_string_buffer(
                    struct.pack('<HHH', addr, 0, 2) + b'\0\0' + struct.pack('<Q', ctypes.addressof(w)) +
                    struct.pack('<HHH', addr, 1, 1) + b'\0\0' + struct.pack('<Q', ctypes.addressof(r)), 56)
                fcntl.ioctl(fd, 0x0707, struct.pack('<QI', ctypes.addressof(msgs), 2))
                return r.raw[0]
            buf = ctypes.create_string_buffer(bytes([reg >> 8, reg & 0xff, val]), 3)
            msg = struct.pack('<HHH', addr, 0, 3) + b'\0\0' + struct.pack('<Q', ctypes.addressof(buf))
            msgs = ctypes.create_string_buffer(msg, len(msg))
            fcntl.ioctl(fd, 0x0707, struct.pack('<QI', ctypes.addressof(msgs), 1))

        if not (rw(0x8520) & 0x02):
            # 源无输出：采集服务在跑说明驱动 HPD 门控已放行(§23)，此时无
            # 输出多半是电脑读 EDID 失败后撤掉了显示器——仍要脉冲唤回；
            # 服务没起=开机门控期，绝不可碰 HPD。
            active = subprocess.run(['systemctl', 'is-active', 'ccs_webcam'],
                                    capture_output=True, text=True).stdout.strip()
            if active != 'active':
                print('HPD: 源无输出且服务未运行(开机门控期)，跳过脉冲', flush=True)
                return
            print('HPD: 源无输出但服务在跑，脉冲尝试唤回电脑...', flush=True)
        else:
            print('HPD: 脉冲模拟热插拔，电脑将重新识别...', flush=True)
        rw(0x8544, 0x00)
        time.sleep(2)
        rw(0x8544, 0x01)
        print('HPD: 已脉冲，电脑数秒内重读 EDID', flush=True)
    except Exception as e:
        print('WARN: HPD 脉冲失败(不影响 EDID 内容，拔插一次等效): %s' % e,
              file=sys.stderr, flush=True)
    finally:
        if fd is not None:
            os.close(fd)


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


def tmds_present():
    """读 TC358743 SYS_STATUS(0x8520) bit1=TMDS 在活动。读不到返回 None"""
    fd = None
    try:
        addr = None
        for d in os.listdir('/sys/bus/i2c/devices'):
            try:
                with open('/sys/bus/i2c/devices/%s/name' % d) as f:
                    if not f.read().strip().startswith('tc35874'):
                        continue
                bus, a = d.split('-')
                fd = os.open('/dev/i2c-%s' % bus, os.O_RDWR)
                addr = int(a, 16)
                break
            except (OSError, ValueError):
                continue
        if fd is None:
            return None
        w = ctypes.create_string_buffer(bytes([0x85, 0x20]), 2)
        r = ctypes.create_string_buffer(1)
        msgs = ctypes.create_string_buffer(
            struct.pack('<HHH', addr, 0, 2) + b'\0\0' + struct.pack('<Q', ctypes.addressof(w)) +
            struct.pack('<HHH', addr, 1, 1) + b'\0\0' + struct.pack('<Q', ctypes.addressof(r)), 56)
        fcntl.ioctl(fd, 0x0707, struct.pack('<QI', ctypes.addressof(msgs), 2))
        return bool(r.raw[0] & 0x02)
    except Exception:
        return None
    finally:
        if fd is not None:
            os.close(fd)


def watch(path, last_hpd=0.0):
    """常驻看门狗：EDID RAM 被冲回默认就重写。
    2026-11-04 事故改版（信号驱动，无信号不再硬锤，§28l 追加实捕）：
    夜里电脑睡眠后 MS9332 子系统进入病态（RAM 写不进、读回不一致），
    旧看门狗整夜 20 轮重写+4 次 HPD 脉冲，把它视频通路彻底锤挂。
    2026-11-10 再改版（§28n 实捕）：静默期"完全不写"会死锁——EDID
    在凌晨被冲坏，电脑醒来读到坏 EDID（校验和不过）拒绝输出 → TMDS
    永远不来 → 恢复边沿永远不触发 → 整天无信号。新逻辑：
    - 无 TMDS = 静默期：不打 HPD（电脑睡着脉冲无用且诱发挂死），
      每 10 分钟只读抽查；发现被冲**单次重写**（睡着时重写最安全，
      没人在读），失败则 30 分钟内不再试（防 §28k 式锤击）
    - TMDS 从无到有边沿：校验一次，不一致才重写，重写成功补一个 HPD
      脉冲让电脑重读（它可能刚按坏/默认 EDID 起了输出或拒绝输出）
    - 有 TMDS 正常期：维持 10s 抽查，被冲就重写+HPD
    - HPD 脉冲全程限频 120s（含 main() 启动那次）：开机竞态实测
      13s 内双脉冲，密集脉冲是挂死诱因（§28e/§28m）
    连续失败指数退避保留（§28k）。每拍重读文件：网页端更新无需重启。"""
    fails = 0
    had_signal = None
    quiet_checked = 0.0
    quiet_fix_failed = 0.0
    while True:
        time.sleep(10 if fails == 0 else min(30 * fails, 300))
        sig = tmds_present()
        if not sig:
            if had_signal:
                print('看门狗：信号消失，转入静默期（不重写不脉冲）', flush=True)
            had_signal = False
            now = time.monotonic()
            if now - quiet_checked < 600:
                continue
            quiet_checked = now
            try:
                data = open(path, 'rb').read()
                lk = open(LOCK, 'w')
                fcntl.flock(lk, fcntl.LOCK_EX)
                try:
                    window(True)
                    same = probe_matches(data)
                    window(False)
                finally:
                    lk.close()
                if not same:
                    now2 = time.monotonic()
                    if now2 - quiet_fix_failed < 1800:
                        print('看门狗：静默期 EDID 不一致，上次重写失败未满30分钟，'
                              '暂不处理', flush=True)
                    else:
                        print('看门狗：静默期 EDID 被冲，单次重写（不打 HPD）',
                              flush=True)
                        if apply(path) == 0:
                            quiet_fix_failed = 0.0
                            print('看门狗：静默期重写成功，等信号恢复', flush=True)
                        else:
                            quiet_fix_failed = now2
                            print('看门狗：静默期重写失败，30分钟后再试', flush=True)
            except Exception as e:
                print('WARN: 静默期抽查失败: %s' % e, file=sys.stderr, flush=True)
                try:
                    window(False)
                except Exception:
                    pass
            continue
        edge = (had_signal is False)
        had_signal = True
        try:
            data = open(path, 'rb').read()
        except OSError:
            continue
        lk = open(LOCK, 'w')
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            window(True)
            same = probe_matches(data)
            if not same:
                time.sleep(0.15)               # 读值可能被撞车污染，复核再定
                same = probe_matches(data)
            window(False)
        except Exception as e:
            print('WARN: 看门狗抽查失败: %s' % e, file=sys.stderr, flush=True)
            try:
                window(False)
            except Exception:
                pass
            lk.close()
            fails += 1
            continue
        lk.close()
        if not same:
            print('看门狗：EDID 被冲掉，重写%s'
                  % ('（信号恢复边沿）' if edge else ''), flush=True)
            if apply(path) == 0:
                # HPD 脉冲限频 120s（§28n：开机竞态曾 13s 内双脉冲，
                # 密集脉冲是 MS9332 挂死诱因 §28e/§28m）；重写照做只限脉冲
                if time.monotonic() - last_hpd >= 120:
                    hpd_pulse()
                    last_hpd = time.monotonic()
                else:
                    print('看门狗：120s 内已脉冲过，本次只重写不脉冲', flush=True)
                fails = 0
            else:
                fails += 1
                if fails == 3:
                    print('看门狗：连续重写失败，转入退避（总线/芯片可能挂死）',
                          flush=True)
        else:
            if edge:
                print('看门狗：信号恢复，EDID 一致无需动作', flush=True)
            fails = 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    path = args[0] if args else '/etc/ccs_edid.bin'
    if not os.path.exists(IO):
        subprocess.run(['insmod', MOD], check=True)
        time.sleep(0.3)
    # 2026-11-04 改版：先只读抽查，一致就跳过写入+HPD——HPD 脉冲后的
    # 电脑重新枚举过程实捕会把 MS9332 视频通路打挂（§28m 追加），
    # 网页端点"应用"时文件刚改过仍会正常写入，不受影响
    need = True
    pulsed_at = 0.0
    try:
        data = open(path, 'rb').read()
        lk = open(LOCK, 'w')
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            window(True)
            need = not probe_matches(data)
            window(False)
        finally:
            lk.close()
    except Exception as e:
        print('WARN: 写入前抽查失败(按需要写入处理): %s' % e, file=sys.stderr,
              flush=True)
        try:
            window(False)
        except Exception:
            pass
    if need:
        rc = apply(path)
        if rc == 0:
            hpd_pulse()
            pulsed_at = time.monotonic()
    else:
        print('EDID 已一致，跳过写入和 HPD 脉冲', flush=True)
        rc = 0
    if rc == 0 and '--watch' in sys.argv:
        watch(path, pulsed_at)
    return rc


if __name__ == '__main__':
    sys.exit(main())
