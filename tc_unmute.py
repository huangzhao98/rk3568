#!/usr/bin/env python3
# 强制解静音 TC358743：VI_MUTE=0xc0(AUTO) + CONFCTL|=0x3(VBUFEN|ABUFEN)
import ctypes, fcntl, os, struct, sys

def wr(fd, addr, reg, vals):
    data = ctypes.create_string_buffer(bytes([reg >> 8, reg & 0xff] + vals), 2 + len(vals))
    msg = struct.pack('<HHH', addr, 0, 2 + len(vals)) + b'\0\0' + struct.pack('<Q', ctypes.addressof(data))
    msgs = ctypes.create_string_buffer(msg, len(msg))
    fcntl.ioctl(fd, 0x0707, struct.pack('<QI', ctypes.addressof(msgs), 1))

def rd(fd, addr, reg, n):
    w = ctypes.create_string_buffer(bytes([reg >> 8, reg & 0xff]), 2)
    r = ctypes.create_string_buffer(n)
    m1 = struct.pack('<HHH', addr, 0, 2) + b'\0\0' + struct.pack('<Q', ctypes.addressof(w))
    m2 = struct.pack('<HHH', addr, 1, n) + b'\0\0' + struct.pack('<Q', ctypes.addressof(r))
    msgs = ctypes.create_string_buffer(m1 + m2, 32)
    fcntl.ioctl(fd, 0x0707, struct.pack('<QI', ctypes.addressof(msgs), 2))
    return r.raw

fd = os.open('/dev/i2c-4', os.O_RDWR)
cur = rd(fd, 0x0f, 0x0004, 2)
conf = cur[0] | 0x03                     # CONFCTL 低字节 |= VBUFEN|ABUFEN
wr(fd, 0x0f, 0x857F, [0xc0])             # VI_MUTE = AUTO（解除强制静音）
wr(fd, 0x0f, 0x0004, [conf, cur[1]])
print('VI_MUTE =', rd(fd, 0x0f, 0x857F, 1).hex())
print('CONFCTL =', rd(fd, 0x0f, 0x0004, 2).hex())
os.close(fd)
