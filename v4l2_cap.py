#!/usr/bin/env python3
# 极简 V4L2 MPLANE mmap 抓帧工具（仅用标准库）
# 用法: python3 v4l2_cap.py /dev/video0 /tmp/frame.raw [帧数]
import os, sys, fcntl, mmap, select, struct, ctypes

VIDIOC_REQBUFS   = 0xC0145608
VIDIOC_QUERYBUF  = 0xC0585609
VIDIOC_QBUF      = 0xC058560F
VIDIOC_DQBUF     = 0xC0585611
VIDIOC_STREAMON  = 0x40045612
VIDIOC_STREAMOFF = 0x40045613
V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE = 9
V4L2_MEMORY_MMAP = 1

dev, out = sys.argv[1], sys.argv[2]
want = int(sys.argv[3]) if len(sys.argv) > 3 else 3

fd = os.open(dev, os.O_RDWR)

plane_ctypes = (ctypes.c_char * 64)()
plane_ptr = ctypes.addressof(plane_ctypes)

def v4l2_buffer(index):
    b = bytearray(88)              # struct v4l2_buffer (arm64)
    struct.pack_into('<I', b, 0, index)
    struct.pack_into('<I', b, 4, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE)
    struct.pack_into('<I', b, 60, V4L2_MEMORY_MMAP)
    struct.pack_into('<Q', b, 64, plane_ptr)
    struct.pack_into('<I', b, 72, 1)   # length = 1 plane
    return b

req = bytearray(20)
struct.pack_into('<IIII', req, 0, 4, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, V4L2_MEMORY_MMAP, 0)
fcntl.ioctl(fd, VIDIOC_REQBUFS, req, True)
nbuf = struct.unpack_from('<I', req, 0)[0]
assert nbuf >= 2, f"REQBUFS 只给了 {nbuf} 个"

maps = []
for i in range(nbuf):
    ctypes.memset(plane_ptr, 0, 64)
    fcntl.ioctl(fd, VIDIOC_QUERYBUF, v4l2_buffer(i), True)
    plen = struct.unpack_from('<I', bytes(plane_ctypes), 4)[0]
    poff = struct.unpack_from('<I', bytes(plane_ctypes), 8)[0]
    maps.append(mmap.mmap(fd, plen, offset=poff))
    fcntl.ioctl(fd, VIDIOC_QBUF, v4l2_buffer(i), True)

fcntl.ioctl(fd, VIDIOC_STREAMON, struct.pack('<I', V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE))

got = 0
with open(out, 'wb') as f:
    while got < want:
        r, _, _ = select.select([fd], [], [], 30)
        if not r:
            continue              # 无信号时干等，不超时退出
        b = v4l2_buffer(0)
        try:
            fcntl.ioctl(fd, VIDIOC_DQBUF, b, True)
        except OSError:
            continue
        idx = struct.unpack_from('<I', b, 0)[0]
        if got == want - 1:           # 只存最后一帧（前序帧可能不完整）
            maps[idx].seek(0)
            f.write(maps[idx].read())
        got += 1
        fcntl.ioctl(fd, VIDIOC_QBUF, v4l2_buffer(idx), True)

fcntl.ioctl(fd, VIDIOC_STREAMOFF, struct.pack('<I', V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE))
print(f"OK: {got} 帧, 最后一帧已写入 {out} ({os.path.getsize(out)} 字节)")
