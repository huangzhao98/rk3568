#!/usr/bin/env python3
# 读 TC358743 关键寄存器：信号检测/静音/输出使能状态（布局抄 ccs_webcam.TcI2c）
import ctypes, fcntl, os, struct

def rd(fd, addr, reg, n):
    w = ctypes.create_string_buffer(bytes([reg >> 8, reg & 0xff]), 2)
    r = ctypes.create_string_buffer(n)
    m1 = struct.pack('<HHH', addr, 0, 2) + b'\0\0' + struct.pack('<Q', ctypes.addressof(w))
    m2 = struct.pack('<HHH', addr, 1, n) + b'\0\0' + struct.pack('<Q', ctypes.addressof(r))
    msgs = ctypes.create_string_buffer(m1 + m2, 32)
    fcntl.ioctl(fd, 0x0707, struct.pack('<QI', ctypes.addressof(msgs), 2))
    return r.raw

fd = os.open('/dev/i2c-4', os.O_RDWR)
for name, reg, n in [('SYS_STATUS', 0x8520, 1), ('VI_MUTE', 0x857F, 1),
                     ('CONFCTL', 0x0004, 2), ('TXOPTIONCNTRL', 0x0238, 4),
                     ('SYS_INT', 0x8502, 1), ('DE_CNT_H', 0x8232, 2)]:
    v = rd(fd, 0x0f, reg, n)
    print(f"{name} 0x{reg:04X} = {v.hex()}")
os.close(fd)
