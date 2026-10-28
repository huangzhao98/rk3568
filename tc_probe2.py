#!/usr/bin/env python3
# 读 TC358743 CSI/PLL 状态
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
for name, reg, n in [('PLLCTL0', 0x0020, 2), ('PLLCTL1', 0x0022, 2),
                     ('CSI_STATUS', 0x0410, 1), ('VI_STATUS1', 0x8522, 1),
                     ('SYSCTL', 0x0002, 2), ('SYS_STATUS', 0x8520, 1),
                     ('LINEINITCNT', 0x0230, 4), ('HSYNC_CNT', 0x8230, 2)]:
    v = rd(fd, 0x0f, reg, n)
    print(f"{name} 0x{reg:04X} = {v.hex()}")
os.close(fd)
