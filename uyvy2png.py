#!/usr/bin/env python3
# UYVY raw -> PNG（仅标准库）。用法: uyvy2png.py in.raw out.png [宽 高]
import sys, zlib, struct

raw_path, png_path = sys.argv[1], sys.argv[2]
W = int(sys.argv[3]) if len(sys.argv) > 3 else 1280
H = int(sys.argv[4]) if len(sys.argv) > 4 else 720

data = open(raw_path, 'rb').read()
assert len(data) >= W * H * 2, f"文件太小: {len(data)}"

def clamp(v):
    return 0 if v < 0 else (255 if v > 255 else v)

rows = bytearray()
pos = 0
for y in range(H):
    rows.append(0)  # filter: None
    line = bytearray(W * 3)
    for x in range(0, W, 2):
        u = data[pos]; y0 = data[pos+1]; v = data[pos+2]; y1 = data[pos+3]
        pos += 4
        c0, c1, d, e = y0 - 16, y1 - 16, u - 128, v - 128
        for k, c in ((x, c0), (x+1, c1)):
            cc = 298 * c + 128
            line[k*3]   = clamp((cc + 409 * e) >> 8)
            line[k*3+1] = clamp((cc - 100 * d - 208 * e) >> 8)
            line[k*3+2] = clamp((cc + 516 * d) >> 8)
    rows += line

def chunk(tag, payload):
    c = struct.pack('>I', len(payload)) + tag + payload
    return c + struct.pack('>I', zlib.crc32(tag + payload) & 0xffffffff)

png  = b'\x89PNG\r\n\x1a\n'
png += chunk(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 2, 0, 0, 0))
png += chunk(b'IDAT', zlib.compress(bytes(rows), 6))
png += chunk(b'IEND', b'')
open(png_path, 'wb').write(png)
print(f"OK: {png_path} ({W}x{H})")
