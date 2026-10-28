import os, fcntl, mmap, select, struct, ctypes, time, subprocess

SUB = '/dev/v4l-subdev4'
VIDIOC_REQBUFS=0xC0145608; VIDIOC_QUERYBUF=0xC0585609; VIDIOC_QBUF=0xC058560F
VIDIOC_DQBUF=0xC0585611; VIDIOC_STREAMON=0x40045612; VIDIOC_STREAMOFF=0x40045613
MPLANE=9; MMAP=1

def q(cmd): return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout
def sig(): return 'Active width: 1280' in q(f'v4l2-ctl -d {SUB} --query-dv-timings 2>/dev/null')

fd = os.open('/dev/video0', os.O_RDWR)
planes = (ctypes.c_char*64)(); pp = ctypes.addressof(planes)
def buf(i):
    b = bytearray(88)
    struct.pack_into('<I', b, 0, i); struct.pack_into('<I', b, 4, MPLANE)
    struct.pack_into('<I', b, 60, MMAP); struct.pack_into('<Q', b, 64, pp)
    struct.pack_into('<I', b, 72, 1); return b

# 只分配一次缓冲区
req = bytearray(20); struct.pack_into('<IIII', req, 0, 4, MPLANE, MMAP, 0)
fcntl.ioctl(fd, VIDIOC_REQBUFS, req, True); n = struct.unpack_from('<I', req, 0)[0]
maps = []
for i in range(n):
    ctypes.memset(pp, 0, 64); fcntl.ioctl(fd, VIDIOC_QUERYBUF, buf(i), True)
    plen = struct.unpack_from('<I', bytes(planes), 4)[0]
    poff = struct.unpack_from('<I', bytes(planes), 8)[0]
    maps.append(mmap.mmap(fd, plen, offset=poff))

def stream_on():  # STREAMOFF 后所有 buf 已出队，需重新 QBUF
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
    os.system(f'v4l2-ctl -d {SUB} --set-edid file=/tmp/junk.hex,format=hex --fix-edid-checksums 2>/dev/null')
    while sig() and time.time()-t0 < 15: time.sleep(0.2)
    stream_on()
    os.system(f'v4l2-ctl -d {SUB} --set-edid file=/usr/sbin/edid_720p.hex.txt,format=hex 2>/dev/null')
    t1 = time.time()
    while not sig() and time.time()-t1 < 90: time.sleep(0.1)
    tb = time.time()-t1
    time.sleep(2.5)                      # 等驱动轮询把 format_change 落定
    stream_off()
    stream_on()                          # 信号稳定后重开流，解除芯片静音
    okn, last = 0, None
    for k in range(6):
        ok = grab(f'/tmp/p3_{rnd}_{k}.raw')
        okn += ok
        if ok: last = f'/tmp/p3_{rnd}_{k}.raw'
    stream_off()
    print(f'[p3] round{rnd}: back={tb:.1f}s frames={okn}/6 last={last}', flush=True)
