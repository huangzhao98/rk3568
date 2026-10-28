import os, fcntl, mmap, select, struct, ctypes, time, subprocess, threading

SUB = '/dev/v4l-subdev4'
VIDIOC_REQBUFS=0xC0145608; VIDIOC_QUERYBUF=0xC0585609; VIDIOC_QBUF=0xC058560F
VIDIOC_DQBUF=0xC0585611; VIDIOC_STREAMON=0x40045612; VIDIOC_STREAMOFF=0x40045613
MPLANE=9; MMAP=1; I2C_RDWR=0x0707

def q(cmd): return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout
def sig(): return 'Active width: 1280' in q(f'v4l2-ctl -d {SUB} --query-dv-timings 2>/dev/null')

class I2C:
    def __init__(self, bus=4, addr=0x0F):
        self.fd = os.open(f'/dev/i2c-{bus}', os.O_RDWR)
        self.addr = addr
    def wr8(self, reg, val):
        buf = ctypes.create_string_buffer(bytes([reg >> 8, reg & 0xff, val]), 3)
        msg = struct.pack('<HHH', self.addr, 0, 3) + b'\0\0' + struct.pack('<Q', ctypes.addressof(buf))
        msgs = ctypes.create_string_buffer(msg, len(msg))
        fcntl.ioctl(self.fd, I2C_RDWR, struct.pack('<QI', ctypes.addressof(msgs), 1))

clear_stop = threading.Event()
def clear_loop():
    i2c = I2C()
    while not clear_stop.is_set():
        try:
            for reg in range(0x8502, 0x8510):
                i2c.wr8(reg, 0xff)
        except OSError as e:
            print(f'i2c err: {e}', flush=True)
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

def grab(timeout=3):
    """返回 (bytes, monotonic_ts) 或 None"""
    r, _, _ = select.select([fd], [], [], timeout)
    if not r: return None
    b = buf(0)
    try: fcntl.ioctl(fd, VIDIOC_DQBUF, b, True)
    except OSError: return None
    idx = struct.unpack_from('<I', b, 0)[0]
    used = struct.unpack_from('<I', bytes(planes), 0)[0]
    fcntl.ioctl(fd, VIDIOC_QBUF, buf(idx), True)
    return (bytes(maps[idx][:used if used else len(maps[idx])]), time.monotonic())

def lumasig(d):
    """每行亮度均值指纹（64 行抽样）"""
    W, H = 1280, 720
    return [sum(d[y*W*2+1:(y+1)*W*2:2]) // W for y in range(0, H, 12)]

d = bytes([0,255,255,255,255,255,255,0] + [0]*119)
d += bytes([(256 - sum(d) % 256) % 256])
open('/tmp/junk.hex','w').write(' '.join(f'{x:02x}' for x in d))

clear_stop.clear()
th = threading.Thread(target=clear_loop, daemon=True)
th.start()

os.system(f'v4l2-ctl -d {SUB} --set-edid file=/tmp/junk.hex,format=hex --fix-edid-checksums 2>/dev/null')
t0 = time.time()
while sig() and time.time()-t0 < 15: time.sleep(0.2)
stream_on()
os.system(f'v4l2-ctl -d {SUB} --set-edid file=/usr/sbin/edid_720p.hex.txt,format=hex 2>/dev/null')

# 连续抓帧，找黑→内容的过渡帧
frames = []          # (idx, data, ts)
transition = -1
for i in range(2400):  # 上限 ~60s
    g = grab(timeout=4)
    if g is None:
        print(f'frame{i}: TIMEOUT', flush=True)
        continue
    data, ts = g
    # 内容检测：抽样行均值的最大值（黑帧=16，任务栏行 >>30）
    rowmax = max(sum(data[y*2560+1:(y+1)*2560:2]) // 1280 for y in range(0, 720, 11))
    frames.append((i, data, ts))
    if transition < 0 and rowmax > 30:
        transition = i
        print(f'transition at frame {i}, rowmax={rowmax}, t={ts - frames[0][2]:.2f}s', flush=True)
    if transition >= 0 and i >= transition + 4:
        break
    if transition < 0 and len(frames) > 3:
        frames.pop(0)   # 只保留过渡前的黑帧上下文

clear_stop.set(); th.join()
stream_off()
print(f'transition={transition}, saved {len(frames)} frames', flush=True)
for i, data, ts in frames:
    open(f'/tmp/p7_{i}.raw', 'wb').write(data)
    print(f'  saved f{i} ts={ts:.4f}', flush=True)
