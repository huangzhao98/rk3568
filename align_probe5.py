import os, fcntl, mmap, select, struct, ctypes, time, subprocess, threading

SUB = '/dev/v4l-subdev4'
VIDIOC_REQBUFS=0xC0145608; VIDIOC_QUERYBUF=0xC0585609; VIDIOC_QBUF=0xC058560F
VIDIOC_DQBUF=0xC0585611; VIDIOC_STREAMON=0x40045612; VIDIOC_STREAMOFF=0x40045613
MPLANE=9; MMAP=1
I2C_RDWR=0x0707

def q(cmd): return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout
def sig(): return 'Active width: 1280' in q(f'v4l2-ctl -d {SUB} --query-dv-timings 2>/dev/null')

# ---- i2c 直写 TC358743（16-bit 寄存器地址，W1C 清中断锁存）----
class I2C:
    def __init__(self, bus=4, addr=0x0F):
        self.fd = os.open(f'/dev/i2c-{bus}', os.O_RDWR)
        self.addr = addr
    def wr8(self, reg, val):
        data = ctypes.create_string_buffer(bytes([reg >> 8, reg & 0xff, val]), 3)
        # struct i2c_msg {__u16 addr; __u16 flags; __u16 len; pad; __u8 *buf;}
        msg = struct.pack('<HHH', self.addr, 0, 3) + b'\0\0' + struct.pack('<Q', ctypes.addressof(data))
        msgs_buf = ctypes.create_string_buffer(msg, len(msg))
        # struct i2c_rdwr_ioctl_data {struct i2c_msg *msgs; __u32 nmsgs;}
        ioctl_data = struct.pack('<QI', ctypes.addressof(msgs_buf), 1)
        fcntl.ioctl(self.fd, I2C_RDWR, ioctl_data)

clear_stop = threading.Event()
def clear_loop():
    i2c = I2C()
    while not clear_stop.is_set():
        try:
            for reg in range(0x8502, 0x8510):   # SYS_INT..KEY_INT 全部 W1C 清掉
                i2c.wr8(reg, 0xff)
        except OSError as e:
            print(f'i2c clear err: {e}', flush=True)
        time.sleep(0.05)

fd = os.open('/dev/video0', os.O_RDWR)
planes = (ctypes.c_char*64)(); pp = ctypes.addressof(planes)
def buf(i):
    b = bytearray(88)
    struct.pack_into('<I', b, 0, i); struct.pack_into('<I', b, 4, MPLANE)
    struct.pack_into('<I', b, 60, MMAP); struct.pack_into('<Q', b, 64, pp)
    struct.pack_into('<I', b, 72, 1); return b

req = bytearray(20); struct.pack_into('<IIII', req, 0, 4, MPLANE, MMAP, 0)
fcntl.ioctl(fd, VIDIOC_REQBUFS, req, True); n = struct.unpack_from('<I', req, 0)[0]
maps = []
for i in range(n):
    ctypes.memset(pp, 0, 64); fcntl.ioctl(fd, VIDIOC_QUERYBUF, buf(i), True)
    plen = struct.unpack_from('<I', bytes(planes), 4)[0]
    poff = struct.unpack_from('<I', bytes(planes), 8)[0]
    maps.append(mmap.mmap(fd, plen, offset=poff))

def stream_on():
    for i in range(n): fcntl.ioctl(fd, VIDIOC_QBUF, buf(i), True)
    fcntl.ioctl(fd, VIDIOC_STREAMON, struct.pack('<I', MPLANE))
def stream_off():
    fcntl.ioctl(fd, VIDIOC_STREAMOFF, struct.pack('<I', MPLANE))

def grab(path):
    r, _, _ = select.select([fd], [], [], 3)
    if not r: return False
    b = buf(0)
    try: fcntl.ioctl(fd, VIDIOC_DQBUF, b, True)
    except OSError: return False
    idx = struct.unpack_from('<I', b, 0)[0]
    used = struct.unpack_from('<I', bytes(planes), 0)[0]
    fcntl.ioctl(fd, VIDIOC_QBUF, buf(idx), True)
    open(path, 'wb').write(maps[idx][:used if used else len(maps[idx])])
    return True

d = bytes([0,255,255,255,255,255,255,0] + [0]*119)
d += bytes([(256 - sum(d) % 256) % 256])
open('/tmp/junk.hex','w').write(' '.join(f'{x:02x}' for x in d))

for rnd in range(3):
    t0 = time.time()
    clear_stop.clear()
    th = threading.Thread(target=clear_loop, daemon=True)
    th.start()                           # 全程屏蔽 SYNC/DE 变化锁存 → 驱动不静音
    os.system(f'v4l2-ctl -d {SUB} --set-edid file=/tmp/junk.hex,format=hex --fix-edid-checksums 2>/dev/null')
    while sig() and time.time()-t0 < 15: time.sleep(0.2)
    stream_on()                          # 静默期 armed：rkcif 等 FS
    os.system(f'v4l2-ctl -d {SUB} --set-edid file=/usr/sbin/edid_720p.hex.txt,format=hex 2>/dev/null')
    t1 = time.time()
    while not sig() and time.time()-t1 < 90: time.sleep(0.1)
    tb = time.time()-t1
    time.sleep(2.5)                      # Windows 重锁定，期间持续清锁存
    clear_stop.set(); th.join()
    okn, last = 0, None
    for k in range(6):
        ok = grab(f'/tmp/p5_{rnd}_{k}.raw')
        okn += ok
        if ok: last = f'/tmp/p5_{rnd}_{k}.raw'
    stream_off()
    print(f'[p5] round{rnd}: back={tb:.1f}s frames={okn}/6 last={last}', flush=True)
