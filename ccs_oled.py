#!/usr/bin/env python3
# ccs_oled.py — JP6 座 SSD1306 OLED（128x64，I2C1 0x3C）状态屏
# 每秒刷新：输入通道（HDMI/VGA）、信号分辨率、采集帧率、网络状态、IP。
# 纯 ioctl I2C_RDWR + PIL 位图字体，无额外依赖。
import os, re, time, fcntl, struct, ctypes, socket
import urllib.request

import numpy as np
from PIL import Image, ImageDraw, ImageFont

I2C_DEV = '/dev/i2c-1'
OLED_ADDR = 0x3C
W, H = 128, 64

INIT = bytes([
    0xAE,             # display off
    0xD5, 0x80,       # clock div
    0xA8, 0x3F,       # multiplex 64
    0xD3, 0x00,       # offset 0
    0x40,             # start line 0
    0x8D, 0x14,       # charge pump on
    0x20, 0x00,       # horizontal addressing
    0xA1,             # segment remap（左右镜像，接线方向）
    0xC8,             # com scan dec（上下镜像）
    0xDA, 0x12,       # com pins
    0x81, 0xCF,       # contrast
    0xD9, 0xF1,       # precharge
    0xDB, 0x40,       # vcomh
    0xA4,             # display from RAM
    0xA6,             # normal（非反白）
    0xAF,             # display on
])


class Oled:
    def __init__(self):
        self.fd = os.open(I2C_DEV, os.O_RDWR)

    def _xfer(self, buf):
        data = ctypes.create_string_buffer(bytes(buf), len(buf))
        # struct i2c_msg {__u16 addr; __u16 flags; __u16 len; pad; __u8 *buf;}
        msg = struct.pack('<HHH', OLED_ADDR, 0, len(buf)) + b'\0\0' \
            + struct.pack('<Q', ctypes.addressof(data))
        msgs = ctypes.create_string_buffer(msg, len(msg))
        fcntl.ioctl(self.fd, 0x0707,
                    struct.pack('<QI', ctypes.addressof(msgs), 1))  # I2C_RDWR

    def cmd(self, *cmds):
        self._xfer(bytes([0x00]) + bytes(cmds))

    def init(self):
        self._xfer(bytes([0x00]) + INIT)

    def show(self, pix):
        """pix: (64,128) 0/1 numpy。SSD1306 页格式：每字节是一列 8 行，LSB 在上"""
        fb = np.packbits(pix.reshape(8, 8, W), axis=1,
                         bitorder='little').reshape(-1).tobytes()
        self.cmd(0x21, 0, 127, 0x22, 0, 7)      # 列/页地址窗口全屏
        self._xfer(bytes([0x40]) + fb)


def chan():
    """当前采集通道：SEL gpio41（0=HDMI 1=VGA），文件不在则读持久化配置"""
    try:
        v = open('/sys/class/gpio/gpio41/value').read().strip()
        return 'VGA' if v == '1' else 'HDMI'
    except OSError:
        try:
            return open('/etc/ccs_channel').read().strip().upper()
        except OSError:
            return '?'


def net():
    """(eth0 状态, IPv4)。ioctl 取地址，不 fork 子进程"""
    try:
        st = open('/sys/class/net/eth0/operstate').read().strip().upper()
    except OSError:
        st = '?'
    ip = '-'
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ip = socket.inet_ntoa(fcntl.ioctl(
            s.fileno(), 0x8915,                     # SIOCGIFADDR
            struct.pack('256s', b'eth0'))[20:24])
        s.close()
    except OSError:
        pass
    return st, ip


def stat():
    """采集服务状态：'30.0 fps | streaming | 清晰1280x720(源1920x1080) ...'"""
    try:
        t = urllib.request.urlopen('http://127.0.0.1:8080/stat',
                                   timeout=0.8).read().decode()
        m = re.search(r'源(\d+x\d+)', t)
        return m.group(1) if m else ''
    except Exception:
        return ''


def main():
    oled = Oled()
    oled.init()
    font = ImageFont.load_default()
    img = Image.new('1', (W, H), 0)
    draw = ImageDraw.Draw(img)
    while True:
        t0 = time.time()
        try:
            st, ip = net()
            src = stat()
            draw.rectangle((0, 0, W, H), fill=0)
            draw.text((0, 0),  f'IN : {chan()}', font=font, fill=1)
            draw.text((0, 16), f'SIG: {src or "-"}', font=font, fill=1)
            draw.text((0, 32), f'NET: eth0 {st}', font=font, fill=1)
            draw.text((0, 48), f'IP : {ip}', font=font, fill=1)
            oled.show(np.asarray(img, dtype=np.uint8))
        except Exception as e:
            print(f'[ccs_oled] {e!r}', flush=True)
            time.sleep(2)          # 总线异常时降速重试
            continue
        dt = time.time() - t0
        time.sleep(max(0.2, 1.0 - dt))


if __name__ == '__main__':
    main()
