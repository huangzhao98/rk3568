import os, fcntl, mmap, select, struct, ctypes, time

# 探针 v6：信号稳定、帧在流时，i2c 硬静音芯片 → 解静音，检验 CSI 恢复是否帧对齐
VIDIOC_REQBUFS=0xC0145608; VIDIOC_QUERYBUF=0xC0585609; VIDIOC_QBUF=0xC058560F
VIDIOC_DQBUF=0xC0585611; VIDIOC_STREAMON=0x40045612; VIDIOC_STREAMOFF=0x40045613
MPLANE=9; MMAP=1; I2C_RDWR=0x0707

class I2C:
    def __init__(self, bus=4, addr=0x0F):
        self.fd = os.open(f'/dev/i2c-{bus}', os.O_RDWR)
        self.addr = addr
    def _xfer(self, data):
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        msg = struct.pack('<HHH', self.addr, 0, len(data)) + b'\0\0' + struct.pack('<Q', ctypes.addressof(buf))
        msgs = ctypes.create_string_buffer(msg, len(msg))
        fcntl.ioctl(self.fd, I2C_RDWR, struct.pack('<QI', ctypes.addressof(msgs), 1))
    def wr8(self, reg, val):  self._xfer([reg >> 8, reg & 0xff, val])
    def wr16(self, reg, val): self._xfer([reg >> 8, reg & 0xff, val & 0xff, val >> 8])
    def wr32(self, reg, val): self._xfer([reg >> 8, reg & 0xff, val & 0xff, (val>>8)&0xff, (val>>16)&0xff, (val>>24)&0xff])
    def rd16(self, reg):
        buf = ctypes.create_string_buffer(bytes([reg >> 8, reg & 0xff]), 2)
        msg_w = struct.pack('<HHH', self.addr, 0, 2) + b'\0\0' + struct.pack('<Q', ctypes.addressof(buf))
        rbuf = ctypes.create_string_buffer(2)
        msg_r = struct.pack('<HHH', self.addr, 1, 2) + b'\0\0' + struct.pack('<Q', ctypes.addressof(rbuf))
        msgs = ctypes.create_string_buffer(msg_w + msg_r, 32)
        fcntl.ioctl(self.fd, I2C_RDWR, struct.pack('<QI', ctypes.addressof(msgs), 2))
        return rbuf.raw[0] | (rbuf.raw[1] << 8)

def chip_mute(i2c):
    i2c.wr8(0x857F, 0xd0)     # VI_MUTE = AUTO_MUTE|VI_MUTE
    i2c.wr32(0x0238, 0)       # TXOPTIONCNTRL = 0
    i2c.wr16(0x0004, i2c.rd16(0x0004) & ~0x3)   # CONFCTL &= ~(VBUFEN|ABUFEN)

def chip_unmute(i2c):         # 复刻驱动 enable_stream(true)
    i2c.wr32(0x0238, 0)
    i2c.wr32(0x0238, 1)       # CONTCLKMODE
    i2c.wr8(0x857F, 0xc0)     # VI_MUTE = AUTO_MUTE
    i2c.wr16(0x0004, i2c.rd16(0x0004) | 0x3)

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
for i in range(n): fcntl.ioctl(fd, VIDIOC_QBUF, buf(i), True)
fcntl.ioctl(fd, VIDIOC_STREAMON, struct.pack('<I', MPLANE))

def grab(path, timeout=3):
    r, _, _ = select.select([fd], [], [], timeout)
    if not r: return False
    b = buf(0)
    try: fcntl.ioctl(fd, VIDIOC_DQBUF, b, True)
    except OSError: return False
    idx = struct.unpack_from('<I', b, 0)[0]
    used = struct.unpack_from('<I', bytes(planes), 0)[0]
    fcntl.ioctl(fd, VIDIOC_QBUF, buf(idx), True)
    open(path, 'wb').write(maps[idx][:used if used else len(maps[idx])])
    return True

i2c = I2C()
for rnd in range(3):
    ok0 = grab(f'/tmp/p6_{rnd}_before.raw')
    chip_mute(i2c)
    time.sleep(0.3)
    chip_unmute(i2c)
    okn, last = 0, None
    for k in range(8):                        # 跳过静音期的烂帧，取稳定后的
        ok = grab(f'/tmp/p6_{rnd}_{k}.raw')
        okn += ok
        if ok: last = f'/tmp/p6_{rnd}_{k}.raw'
    print(f'[p6] round{rnd}: before={ok0} after_frames={okn}/8 last={last}', flush=True)
    time.sleep(0.5)
