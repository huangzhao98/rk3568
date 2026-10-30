#!/usr/bin/env python3
# ccs_webcam.py — CCS 板 HDMI 采集网页实时预览 + 管理面板（MJPEG over HTTP）
#
# 用法: python3 ccs_webcam.py [--port 8080]
# 访问: http://<板子IP>:8080/
#
# 关键机制（2026-09-18 实测结论）：
# 1. rkcif 的 DMA 相位锁定在"开流后收到的第一个数据"，不等 FS 短包。
#    有信号时开流 → 从帧中间开始写 → 画面垂直卷动且运行中永不恢复。
#    对策：开流前先写垃圾 EDID 让源端停输出（线路静默），开流后恢复
#    正常 EDID，源端重新输出的第一帧必是完整帧 → 对齐。
# 2. 运行中源端断信号再恢复（休眠/拔插）会破坏对齐 → watchdog 检测到
#    信号恢复后自动重走对齐序列。注意：对齐序列本身就会引起信号掉/恢复，
#    期间必须置 in_align 屏蔽 watchdog，否则它把对齐造成的信号恢复误判为
#    外部事件再次触发重对齐 → 无限循环，fps 恒 0（2026-09-18 实测踩坑）。
# 3. numpy 色彩运算必须 int32：int16 下 (235-16)*255=55845 溢出回绕成
#    负数，亮部直接变黑（YCbCr 捷径版同样适用）。
# 4. 低延迟：每轮排干队列只取最新帧。清晰/流畅两档可网页切换：
#    JPEG 本身就是 YCbCr——直接组装 Y/Cb/Cr 平面交给 PIL 编码，
#    跳过 YUV→RGB 矩阵；流畅档再额外隔行隔列降采样到 640x360。
# 5. 管理面板：EDID 编辑（对齐序列会保存/恢复当前 EDID，不会冲掉用户设置）、
#    静态 IP（systemd-networkd，90s 未确认自动回滚防失联）、
#    GPIO 状态/白名单翻转（sysfs，仅无占用者的脚）。
# 6. 采集管线可网页切换：原生 V4L2 ioctl（默认）/ GStreamer
#    （v4l2src→appsink 出 UYVY），/etc/ccs_pipeline 持久化；
#    消卷/对齐/亮度/锐化/编码链两种管线共用，gst 缺失仅禁用该选项。

import os, re, sys, fcntl, json, mmap, select, socket, struct, ctypes, termios, threading, time, argparse, subprocess, secrets, queue
import signal, faulthandler
import multiprocessing
import urllib.parse
import numpy as np
from PIL import Image
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# vepu JPEG 硬编 shim（ccs_dev/mppjpeg_shim.c → /usr/lib/libmppjpeg_shim.so）
# 加载失败或连续编码出错都回退 PIL 软编，功能不受影响
try:
    _mppj = ctypes.CDLL('/usr/lib/libmppjpeg_shim.so')
    _mppj.mppjpeg_open.restype = ctypes.c_int
    _mppj.mppjpeg_encode.restype = ctypes.c_int
    _mppj.mppjpeg_encode.argtypes = [ctypes.c_char_p, ctypes.c_int,
                                     ctypes.c_char_p, ctypes.c_int]
except OSError:
    _mppj = None

# libjpeg-turbo 4:2:2 软编 shim（ccs_dev/ljpeg422_shim.c →
# /usr/lib/libljpeg422_shim.so）：vepu 硬编输出恒为 4:2:0（mpp HAL 写死），
# 纵向色度砍半，1px 高的彩色细线与相邻背景行平均后饱和度掉光变灰；
# 4:2:2 纵向色度全分辨率，细线颜色保住。内部双槽流水（一帧延迟），
# 同步接口语义与 mpp shim 相同，存在时优先于硬编。
try:
    _lj = ctypes.CDLL('/usr/lib/libljpeg422_shim.so')
    _lj.ljpeg_open.restype = ctypes.c_int
    _lj.ljpeg_encode.restype = ctypes.c_int
    _lj.ljpeg_encode.argtypes = [ctypes.c_char_p, ctypes.c_int,
                                 ctypes.c_char_p, ctypes.c_int]
    _lj.ljpeg_set_filter.argtypes = [ctypes.c_int, ctypes.c_int]
    if _lj.ljpeg_open(64, 64) != 0:      # 探测运行环境（libjpeg ABI 等）
        _lj = None
    else:
        _lj.ljpeg_close()
except (OSError, AttributeError):
    _lj = None

# RGA 硬件缩放（>720p 的源降到 720p 再编码用）；老 shim 无此符号则回退 PIL 缩放
_HAS_RGA = False
if _mppj is not None:
    try:
        _mppj.uyvy_scale.restype = ctypes.c_int
        _mppj.uyvy_scale.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        _HAS_RGA = True
    except AttributeError:
        pass

# GStreamer 采集后端（网页可切换的可选管线）：库缺失仅禁用该选项，
# 原生 ioctl 路径完全不受影响
try:
    import gi
    gi.require_version('Gst', '1.0')
    gi.require_version('GstApp', '1.0')
    from gi.repository import Gst, GstApp   # GstApp 不 import 则 AppSink 无 try_pull_sample
    Gst.init(None)
    _GST_OK = True
except (ImportError, ValueError):
    _GST_OK = False


def _cp(b):
    """消卷乒乓缓冲是 bytearray，而 ctypes 声明了 c_char_p argtype 的函数
    只接受 bytes/ctypes 缓冲——bytearray 要显式转成 c_char_p 实例（零拷贝）。"""
    if isinstance(b, bytearray):
        return ctypes.c_char_p(ctypes.addressof(ctypes.c_char.from_buffer(b)))
    return b

# 双实例流水编码（mppjpeg_submit/get/ninst）：submit 异步投递后取另一实例里
# 已编完的上一帧，CPU 拷贝/滤波与 vepu 引擎时间重叠（急速模式）。
# 老 shim 无此符号则回退单实例同步 mppjpeg_encode
_HAS_PIPE = False
if _mppj is not None:
    try:
        _mppj.mppjpeg_submit.restype = ctypes.c_int
        _mppj.mppjpeg_submit.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        _mppj.mppjpeg_get.restype = ctypes.c_int
        _mppj.mppjpeg_get.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        _mppj.mppjpeg_ninst.restype = ctypes.c_int
        _HAS_PIPE = True
    except AttributeError:
        pass

# 亮度/锐化补偿（mppjpeg_set_filter，C 编码拷贝趟里完成）；老 shim 无此符号
# 时回退 python 侧 numpy 补偿（慢，~80ms/帧，仅应急）
_HAS_FILTER = False
if _mppj is not None:
    try:
        _mppj.mppjpeg_set_filter.argtypes = [ctypes.c_int, ctypes.c_int]
        _HAS_FILTER = True
    except AttributeError:
        pass

# limited(16~235/240) → full(0~255) 查找表：与 int32 算术同式（uint8 索引无溢出问题），
# 比逐元素 int32 运算快约 5 倍
_LUT_Y = np.clip((np.arange(256, dtype=np.int32) - 16) * 255 // 219, 0, 255).astype(np.uint8)
_LUT_UV = np.clip((np.arange(256, dtype=np.int32) - 128) * 255 // 224 + 128, 0, 255).astype(np.uint8)

VIDIOC_REQBUFS, VIDIOC_QUERYBUF = 0xC0145608, 0xC0585609
VIDIOC_QBUF, VIDIOC_DQBUF = 0xC058560F, 0xC0585611
VIDIOC_STREAMON, VIDIOC_STREAMOFF = 0x40045612, 0x40045613
MPLANE, MEM_MMAP = 9, 1

EDID_GOOD = '/usr/sbin/edid_720p.hex.txt'
EDID_LAST = '/tmp/ccs_last_edid.txt'    # 最近一次成功应用的 EDID，切通道时带到新芯片
NET_DIR = '/etc/systemd/network'
NET_CONF = NET_DIR + '/10-ccs-%s.network'      # 按网卡一文件，优先于 20-wired(DHCP)
NET_ROLLBACK_SEC = 90

# 允许网页翻转的 GPIO（无占用者才生效）；电源/复位脚一律只读
GPIO_SETTABLE = {'WORKING_LEDEN_H'}

# 外部信号检测输入（LED_x_STATE：外部 5V 信号经 TXS0104E 降到 1.8V 进
# SoC，同时直接驱动 LED）——只读！务必保持高阻输入，输出会与外部驱动对打。
# 编号：GPIO2_B1=73 黄 / GPIO2_B2=74 绿 / GPIO2_C5=85 蓝 / GPIO2_C6=86 红
GPIO_INPUTS = {73: 'LED_YELLOW_STATE', 74: 'LED_GREEN_STATE',
               85: 'LED_BLUE_STATE', 86: 'LED_RED_STATE'}


def sh(cmd):
    # 30s 超时：芯片挂死时 unbind/media-ctl 可能卡壳，不能让看门狗线程
    # 永远死在子进程里（2026-10-27 热拔插后冻结的连锁失效教训）
    try:
        subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)
    except subprocess.TimeoutExpired:
        print(f"[sh] timeout: {cmd}", flush=True)


def sho(cmd):
    # errors='replace'：子进程输出混入非 UTF-8 字节时不让 HTTP 线程崩掉
    return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                          errors='replace').stdout


def find_subdev():
    return sho("media-ctl -d /dev/media0 -p 2>/dev/null | awk '/tc35874x/{f=1} f && /device node name/{print $NF; exit}'").strip()


def query_dv(subdev):
    """返回 dict：w/h/pixclk/fps，无信号时全 0"""
    info = {'w': 0, 'h': 0, 'pixclk': 0, 'fps': 0.0}
    if not subdev:
        return info
    out = sho(f"v4l2-ctl -d {subdev} --query-dv-timings 2>/dev/null")
    m = re.search(r'Active width:\s*(\d+)', out)
    if m:
        info['w'] = int(m.group(1))
    m = re.search(r'Active height:\s*(\d+)', out)
    if m:
        info['h'] = int(m.group(1))
    m = re.search(r'Pixelclock:\s*(\d+) Hz \(([\d.]+) frames', out)
    if m:
        info['pixclk'] = int(m.group(1))
        info['fps'] = float(m.group(2))
    return info


def has_signal(subdev):
    return query_dv(subdev)['w'] > 0


def _relock_dirty():
    """驱动标记：最近一次失锁期间 TMDS 是否一直在跑（脏重锁，相位摇骰）。
    读不到（旧内核）按脏处理（安全侧）。"""
    try:
        return open('/sys/module/tc35874x/parameters/relock_dirty') \
            .read().strip() == '1'
    except OSError:
        return True


# ---------------- EDID ----------------

# CEA-861 标准时序参数（pclk 单位 10kHz；hs?/vs? = 同步偏移/脉宽）
EDID_TIMINGS = {
    '720p60':  dict(pclk=7425,  ha=1280, hb=370, hso=110, hsp=40, va=720,  vb=30, vso=5, vp=5),
    '1080p60': dict(pclk=14850, ha=1920, hb=280, hso=88,  hsp=44, va=1080, vb=45, vso=4, vp=5),
    '1080p30': dict(pclk=7425,  ha=1920, hb=280, hso=88,  hsp=44, va=1080, vb=45, vso=4, vp=5),
}

# 色度坐标：沿用出厂 720p EDID 的实测可用值
EDID_CHROM = bytes([0xcb, 0x46, 0xa3, 0x54, 0x4c, 0x99, 0x26, 0x0f, 0x50, 0x54])


def _dtd(t, hmm, vmm):
    """18 字节详细时序描述符（数字分离同步，+hsync +vsync，与出厂 EDID 一致）"""
    b = bytearray(18)
    struct.pack_into('<H', b, 0, t['pclk'])
    b[2], b[3] = t['ha'] & 0xff, t['hb'] & 0xff
    b[4] = ((t['ha'] >> 8) << 4) | (t['hb'] >> 8)
    b[5], b[6] = t['va'] & 0xff, t['vb'] & 0xff
    b[7] = ((t['va'] >> 8) << 4) | (t['vb'] >> 8)
    b[8], b[9] = t['hso'] & 0xff, t['hsp'] & 0xff
    b[10] = ((t['vso'] & 0xf) << 4) | (t['vp'] & 0xf)
    b[11] = ((t['hso'] >> 8) << 6) | ((t['hsp'] >> 8) << 4) | ((t['vso'] >> 4) << 2) | (t['vp'] >> 4)
    b[12], b[13] = hmm & 0xff, vmm & 0xff
    b[14] = ((hmm >> 8) << 4) | (vmm >> 8)
    b[17] = 0x1e
    return b


def build_edid(name, timing, mfr, product, serial, year, week, hcm, vcm):
    """明文字段 → 128 字节 EDID（含校验和）。返回 bytes 或抛 ValueError"""
    if timing not in EDID_TIMINGS:
        raise ValueError('未知分辨率预设')
    name = name.strip()[:13]
    if not name or not all(32 <= ord(c) < 127 for c in name):
        raise ValueError('监视器名称须为 1~13 个 ASCII 字符')
    mfr = mfr.strip().upper()
    if not re.fullmatch(r'[A-Z]{3}', mfr):
        raise ValueError('厂商 ID 须为 3 个英文字母')
    if not (0 <= product <= 0xFFFF and 0 <= serial <= 0xFFFFFFFF
            and 1991 <= year <= 2245 and 0 <= week <= 53
            and 1 <= hcm <= 255 and 1 <= vcm <= 255):
        raise ValueError('产品码/序列号/日期/尺寸超出范围')
    ed = bytearray(128)
    ed[:8] = bytes([0, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0])
    mid = 0
    for i, c in enumerate(mfr):
        mid |= (ord(c) - 64) << (10 - 5 * i)
    ed[8], ed[9] = mid >> 8, mid & 0xff
    struct.pack_into('<H', ed, 10, product)
    struct.pack_into('<I', ed, 12, serial)
    ed[16], ed[17] = week, year - 1990
    ed[18], ed[19] = 1, 4                     # EDID 1.4
    ed[20] = 0x80                             # 数字输入
    ed[21], ed[22] = hcm, vcm
    ed[23] = 120                              # gamma 2.2
    ed[24] = 0x06                             # 特性位，沿出厂值
    ed[25:35] = EDID_CHROM
    for i in range(38, 54):                   # 标准时序：无
        ed[i] = 0x01
    ed[54:72] = _dtd(EDID_TIMINGS[timing], hcm * 10, vcm * 10)
    ed[72:77] = bytes([0, 0, 0, 0xFC, 0])     # 监视器名描述符
    nb = name.encode('ascii')
    ed[77:77 + len(nb)] = nb
    if len(nb) < 13:
        ed[77 + len(nb)] = 0x0a
        for i in range(78 + len(nb), 90):
            ed[i] = 0x20
    ed[90:95] = bytes([0, 0, 0, 0x10, 0])     # 哑描述符 ×2
    ed[108:113] = bytes([0, 0, 0, 0x10, 0])
    ed[127] = (-sum(ed[:127])) & 0xff
    return bytes(ed)


def edid_decode(hextext):
    """解析当前 EDID 摘要：厂商/名称/首个详细时序。返回 dict（可能缺键）"""
    info = {}
    try:
        b = bytes(int(x, 16) for x in hextext.split())
    except ValueError:
        return info
    if len(b) < 128:
        return info
    mid = (b[8] << 8) | b[9]
    info['mfr'] = ''.join(chr(64 + ((mid >> s) & 0x1f)) for s in (10, 5, 0))
    info['name'] = edid_monitor_name(hextext)
    if b[54] or b[55]:                        # 描述符 1 是详细时序
        pclk = (b[54] | (b[55] << 8)) * 10000
        ha = b[56] | ((b[58] >> 4) << 8)
        va = b[59] | ((b[61] >> 4) << 8)
        info['timing'] = f'{ha}x{va}, 像素时钟 {pclk / 1e6:.2f}MHz'
    return info


def edid_to_hextext(data):
    return '\n'.join(' '.join(f'{x:02x}' for x in data[i:i+16])
                     for i in range(0, len(data), 16)) + '\n'


def get_edid_hex(subdev):
    """读芯片当前 EDID，返回 v4l2-ctl hex 文本（可直接喂回 --set-edid）；失败返回 ''"""
    if not subdev:
        return ''
    out = sho(f"v4l2-ctl -d {subdev} --get-edid 2>/dev/null")
    lines = [l.strip() for l in out.splitlines()
             if re.fullmatch(r'([0-9a-fA-F]{2} ){15}[0-9a-fA-F]{2}', l.strip())]
    return '\n'.join(lines) + '\n' if lines else ''


def edid_monitor_name(hextext):
    try:
        b = bytes(int(x, 16) for x in hextext.split())
    except ValueError:
        return ''
    for off in range(54, 126, 18):
        d = b[off:off + 18]
        if len(d) == 18 and d[0] == 0 and d[1] == 0 and d[2] == 0 and d[3] == 0xFC:
            return d[5:18].split(b'\x0a')[0].decode('ascii', 'replace').strip()
    return ''


def set_edid_hex(subdev, hextext, fix=True):
    """写 EDID 到芯片并读回验证，返回 (ok, msg)"""
    try:
        data = bytes(int(x, 16) for x in hextext.split())
    except ValueError:
        return False, 'hex 格式错误'
    if len(data) not in (128, 256):
        return False, f'EDID 长度须为 128 或 256 字节，当前 {len(data)}'
    path = '/tmp/edid_web.hex'
    with open(path, 'w') as f:
        for i in range(0, len(data), 16):
            f.write(' '.join(f'{x:02x}' for x in data[i:i+16]) + '\n')
    opt = ' --fix-edid-checksums' if fix else ''
    sh(f"v4l2-ctl -d {subdev} --set-edid file={path},format=hex{opt}")
    back = get_edid_hex(subdev)
    if not back:
        return False, '写入后读回失败'
    want = ' '.join(f'{x:02x}' for x in data)
    got = ' '.join(back.split())
    if fix:
        # 修了校验和，逐块比较时跳过每块最后一字节
        ok = all(want.split()[i] == got.split()[i]
                 for i in range(len(data)) if i % 128 != 127)
    else:
        ok = want == got
    if ok:
        try:
            with open(EDID_LAST, 'w') as f:      # 切通道时新选中的芯片要补写这份
                f.write(edid_to_hextext(data))
        except OSError:
            pass
    return (True, 'EDID 已生效') if ok else (False, '读回内容不一致（校验和错误？可勾选自动修复）')


def ms9332_apply(data):
    """把同一份 EDID 同步进 MS9332 分配器（电脑/上游实际读的是它），并落盘
    /etc/ccs_edid.bin 供开机服务自动重写（单片机每次上电会恢复默认 EDID）。
    返回追加到页面消息的提示语。"""
    try:
        with open('/etc/ccs_edid.bin', 'wb') as f:
            f.write(data)
        r = subprocess.run(['/usr/sbin/ccs_ms9332_edid.py', '/etc/ccs_edid.bin'],
                           capture_output=True, text=True, errors='replace',
                           timeout=120)
        if r.returncode == 0:
            return '；电脑侧(分配器)已同步并模拟热插拔，电脑数秒内自动重新识别（无需拔线）'
        return '；电脑侧(分配器)写入失败: ' + (r.stdout + r.stderr).strip()[-120:]
    except Exception as e:
        return '；电脑侧(分配器)写入异常: %s' % e


# ---------------- 网络（systemd-networkd） ----------------

def net_ifaces():
    """解析 ip -brief addr → [(name, [addr...], state)]"""
    out = sho("ip -brief addr 2>/dev/null")
    res = []
    for l in out.splitlines():
        p = l.split()
        if len(p) >= 2 and p[0] not in ('lo',):
            res.append((p[0], p[1], ' '.join(p[2:])))
    return res


def net_ccs_conf(iface):
    path = NET_CONF % iface
    return open(path).read() if os.path.exists(path) else None


def net_apply(iface, mode, addr, gw, dns):
    """写配置并重启 networkd，返回 (ok, msg)。mode: dhcp|static"""
    path = NET_CONF % iface
    if mode == 'dhcp':
        if os.path.exists(path):
            os.remove(path)
    else:
        if not re.fullmatch(r'\d{1,3}(\.\d{1,3}){3}/\d{1,2}', addr or ''):
            return False, '地址格式应为 192.168.1.50/24'
        lines = ['[Match]', f'Name={iface}', '', '[Network]', f'Address={addr}']
        if gw:
            if not re.fullmatch(r'\d{1,3}(\.\d{1,3}){3}', gw):
                return False, '网关格式错误'
            lines.append(f'Gateway={gw}')
        for d in (dns or '').replace(',', ' ').split():
            if re.fullmatch(r'\d{1,3}(\.\d{1,3}){3}', d):
                lines.append(f'DNS={d}')
        with open(path, 'w') as f:
            f.write('\n'.join(lines) + '\n')
    sh('systemctl restart systemd-networkd')
    return True, '已下发'


class NetRollback:
    """应用网络配置后 NET_ROLLBACK_SEC 秒内未确认 → 自动恢复旧配置（防失联）"""
    def __init__(self):
        self.lock = threading.Lock()
        self.timer = None
        self.token = None
        self.deadline = 0

    def arm(self, iface, old_conf):
        with self.lock:
            self.cancel()
            self.token = secrets.token_hex(4)
            self.deadline = time.time() + NET_ROLLBACK_SEC

            def rb():
                path = NET_CONF % iface
                if old_conf is None:
                    if os.path.exists(path):
                        os.remove(path)
                else:
                    with open(path, 'w') as f:
                        f.write(old_conf)
                sh('systemctl restart systemd-networkd')
                print(f"[ccs_webcam] net rollback: {iface} 配置已恢复", flush=True)

            self.timer = threading.Timer(NET_ROLLBACK_SEC, rb)
            self.timer.daemon = True
            self.timer.start()
            return self.token

    def confirm(self, token):
        with self.lock:
            if self.timer and token == self.token:
                self.cancel()
                return True
            return False

    def cancel(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None
            self.token = None

    def remain(self):
        return max(0, int(self.deadline - time.time())) if self.timer else 0


netrb = NetRollback()


# ---------------- GPIO ----------------

def gpio_hold_inputs():
    """把 GPIO_INPUTS 常驻 export 为输入，网页才能读到实时电平"""
    base = '/sys/class/gpio'
    for num in GPIO_INPUTS:
        node = f'{base}/gpio{num}'
        try:
            if not os.path.exists(node):
                with open(f'{base}/export', 'w') as f:
                    f.write(str(num))
                time.sleep(0.05)
            with open(f'{node}/direction', 'w') as f:
                f.write('in')
        except OSError:
            pass


def gpio_vals():
    """{num: 'hi'/'lo'} + 按键状态，供网页轮询实时刷新"""
    out = {}
    for num in GPIO_INPUTS:
        try:
            out[num] = open(f'/sys/class/gpio/gpio{num}/value').read().strip()
        except OSError:
            pass
    out['keys'] = dict(KEY_STATE)
    return out


# ---------------- 按键（SARADC_VIN0 电阻梯 → adc-keys 驱动）----------------
KEY_NAMES = {148: 'SW6', 149: 'SW7', 202: 'SW8', 203: 'SW9'}  # KEY_PROG1~4
KEY_STATE = {n: False for n in KEY_NAMES.values()}            # True=按下


def _adc_keys_event():
    for ev in sorted(os.listdir('/dev/input')):
        if not ev.startswith('event'):
            continue
        try:
            if open(f'/sys/class/input/{ev}/device/name').read().strip() \
                    == 'adc-keys':
                return f'/dev/input/{ev}'
        except OSError:
            pass
    return None


def key_monitor():
    """后台读 adc-keys 事件维护按键状态（struct input_event = 24B）"""
    while True:
        ev = _adc_keys_event()
        if not ev:
            time.sleep(5)
            continue
        try:
            with open(ev, 'rb', buffering=0) as f:
                while True:
                    d = f.read(24)
                    if len(d) < 24:
                        break
                    _, _, t, c, v = struct.unpack('qqHHi', d)
                    if t == 1 and c in KEY_NAMES:   # EV_KEY
                        KEY_STATE[KEY_NAMES[c]] = (v == 1)
        except OSError:
            time.sleep(2)



    """解析 /sys/kernel/debug/gpio → [{num,name,dir,val,consumer}]，只收有名脚"""
def gpio_named():
    """解析 /sys/kernel/debug/gpio → [{num,name,dir,val,consumer}]，只收有名脚"""
    out = sho('cat /sys/kernel/debug/gpio 2>/dev/null')
    res = []
    for m in re.finditer(r'gpio-(\d+)\s+\(([^)|]+?)\s*(?:\|([^)]+?))?\s*\)\s*(in|out)?\s*(hi|lo)?', out):
        num, name, consumer, d, v = m.groups()
        name = name.strip()
        if not name:
            continue
        res.append({'num': int(num), 'name': name,
                    'consumer': (consumer or '').strip(),
                    'dir': d or '', 'val': v or ''})
    return res


def gpio_set(num, val):
    """sysfs 翻转，仅对无占用者的白名单脚调用。返回 (ok, msg)"""
    base = '/sys/class/gpio'
    node = f'{base}/gpio{num}'
    if not os.path.exists(node):
        try:
            with open(f'{base}/export', 'w') as f:
                f.write(str(num))
        except OSError as e:
            return False, f'export 失败: {e}'
        time.sleep(0.1)
    try:
        with open(f'{node}/direction', 'w') as f:
            f.write('out')
        with open(f'{node}/value', 'w') as f:
            f.write('1' if val else '0')
    except OSError as e:
        return False, str(e)
    return True, f'gpio{num} = {"hi" if val else "lo"}'


# ---------------- 采集通道（TMUX121：HDMI / VGA） ----------------

def chan_current():
    """SEL=gpio41 1=VGA；未 export（默认 R80 下拉）= HDMI"""
    try:
        return 'vga' if open('/sys/class/gpio/gpio41/value').read().strip() == '1' else 'hdmi'
    except OSError:
        return 'hdmi'


def chan_switch(cap, target):
    """通道切换 = 写 /etc/ccs_channel + 延迟 1s 重启服务。
    不做进程内热切换：实测句柄跨 unbind/bind 会触发内核 subdev_close
    空指针 oops 且 rkcif 卡死零帧（2026-09-21）。重启后 ExecStartPre
    （ccs_channel_apply.sh）在无任何 fd 的干净状态下完成切换。"""
    if target not in ('hdmi', 'vga'):
        return False, '未知通道'
    if target == chan_current():
        return True, f'已在 {target.upper()} 通道'
    try:
        with open('/etc/ccs_channel', 'w') as f:
            f.write(target)
    except OSError as e:
        return False, f'写配置失败: {e}'
    # systemd-run 开独立瞬时定时器，1s 后重启本服务（直接 systemctl restart
    # 会被 stop 时的 cgroup 清理连坐杀掉）
    sh('systemd-run --on-active=1 --unit=ccs_chan_restart systemctl restart ccs_webcam')
    return True, f'正在切换到 {target.upper()}，服务重启约 15 秒'


# ---------------- HID 注入（USB gadget → 上位机键鼠/触摸） ----------------

HID_DEV = {'k': '/dev/hidg0', 'm': '/dev/hidg1', 't': '/dev/hidg2', 'p': '/dev/hidg3'}


class Hid:
    """/dev/hidg* 写入管理：懒打开、出错关闭下次重开（上位机断连/重枚举自适应）。
    必须加锁：keep-alive 后多个 HTTP 线程并发写，无锁时两个线程会同时对同一
    fd double-close——fd 号被 subprocess 管道复用后再被误关，报 OSError EBADF
    把整个 /hid 打挂（2026-09-21 实发）。"""
    def __init__(self):
        self.fds = {}
        self.lock = threading.Lock()

    def write(self, which, data):
        with self.lock:
            fd = self.fds.get(which)
            try:
                if fd is None:
                    fd = os.open(HID_DEV[which], os.O_RDWR)
                    self.fds[which] = fd
                os.write(fd, data)
                return True
            except OSError:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    self.fds.pop(which, None)
                return False


hid = Hid()


# ---------------- 采集 ----------------

class Capture:
    def __init__(self, dev, w, h, quality=65, be='native'):
        self.w, self.h, self.q = w, h, quality
        self.ew, self.eh = _enc_size(w, h)   # 编码几何：>720p 的源降到 720p 再编
        self.dev = dev
        self.fd = None
        self.be_name = be        # 当前采集后端：'native' ioctl / 'gst' GStreamer
        self.want_be = be        # 网页请求的目标后端，loop 检测差异后热切换
        self._gst = None         # GstBE 实例，gst 模式时非 None
        self.frame = None          # 最新 JPEG bytes
        self.raw = None            # 最新原始帧（/frame.raw 调试用）
        self.cond = threading.Condition()
        self.mail = None           # 消卷后待编码原帧（单槽信箱，只留最新）
        self.mail_cond = threading.Condition()
        self.fps = 0.0
        self.state = "init"
        self.sig_ok = True         # 输入信号在位（看门狗 1.5s 轮询发布；网页
                                   # "无视频输入信号"提示用）
        self.phase_suspect = False  # 流式期间信号掉过又恢复（看门狗置位）：
                                   # 中途重锁相位是摇骰（2026-10-29 实测源侧
                                   # 抖动 6~8s 后重锁，一次 0 一次非 0），
                                   # 钉值 0 的快速通道一律禁用，走完整对齐
        self.half = False          # False=全分辨率清晰 / True=半分辨率流畅
        self.bright = 100          # 亮度增益 %（100=原始；VGA 模拟采样偏暗的补偿）
        self._blut = None          # 亮度 LUT，100 时为 None（零开销直通）
        self.sharp = 0             # 锐化强度 0~100（0=关闭；VGA 采样模糊的补偿）
        self.need_realign = threading.Event()
        self.realign_forced = False  # /realign 按钮/管线切换置位：绕过快速通道走完整对齐
        self._track_frame = None   # 已投递给 track 子进程的帧（回写 ref 用）
        self.in_align = threading.Event()   # 对齐期间屏蔽 watchdog（信号变化是自己造成的）
        self.roll = 0              # 硬件卷动相位：buffer 第 b 行 = 源画面第 (b+roll)%h 行
        self.roll_manual = False   # 用户 nudge 过：track 只记录不覆盖（重对齐解除）
        self.roll_epoch = 0        # roll 代际：在飞 track 结果过期即弃（防改回竞态）
        self.align_conf = False    # 最近一次对齐是否置信（不置信则慢速自动重试）
        self.align_via = 'none'    # 最近对齐判定途径 split/taskbar/band/ref/seam/none
        self.ref = None            # 已对齐参考帧（置信时更新，供轮廓相关测相位）
        self.last_frame_ts = time.time()   # 看门狗判芯片挂死用
        # JPEG 硬编：mppjpeg_open 成功则用 vepu（720p ~11ms/帧；1080p 实测
        # >1s/帧不可用，所以 >720p 的源一律降到 720p 再进编码器），失败回退 PIL
        self.enc = 'pil'
        self._hwfail = 0
        self._hwbuf = None
        self._pipe = False           # 双实例流水（急速）：submit/get 分离
        self._pi = 0                 # 下一次 submit 用的实例
        self._inflight = 0           # 各实例是否有未取回的帧（bit0/bit1）
        self.enc_lock = threading.Lock()   # 编码器使用与重开（切几何）互斥
        self._t_sc = 0.0             # CCS_TIMING 计时累加器
        self._t_hw = 0.0
        self._scbuf = None             # RGA 缩放输出缓冲（按编码几何分配）
        if _lj and _lj.ljpeg_open(self.ew, self.eh) == 0:
            self.enc = 'sw422'     # 4:2:2 软编：细线保色（优先）
        elif _mppj and _mppj.mppjpeg_open(self.ew, self.eh) == 0:
            self.enc = 'hw'
            # 双实例流水实测无收益：vepu 单引擎串行处理，submit/get 拆开
            # 只是换了等待位置（53fps 实测持平同步路径），且多一帧延迟——停用
            self._pipe = False
        self._open()

    def _buf(self, index):
        b = bytearray(88)
        struct.pack_into('<I', b, 0, index)
        struct.pack_into('<I', b, 4, MPLANE)
        struct.pack_into('<I', b, 60, MEM_MMAP)
        struct.pack_into('<Q', b, 64, self._pp)
        struct.pack_into('<I', b, 72, 1)
        return b

    def _open(self):
        if self.be_name == 'gst':
            self._gst = GstBE(self)
            self._gst.open()
            return
        self.fd = os.open(self.dev, os.O_RDWR)
        self._plane = (ctypes.c_char * 64)()
        self._pp = ctypes.addressof(self._plane)
        try:
            self._alloc()
        except OSError as e:
            # 无信号（或驱动复位）时节点格式是 0x0，REQBUFS 按 0 尺寸申请
            # 必失败（内核 vb2-dma-sg 已防护返回 ENOMEM，不再 oops）。
            # 先按目标分辨率配好链路格式再申请；仍不行抛给 main 等信号重试
            print(f"[ccs_webcam] first alloc failed ({e}), config fmt then retry",
                  flush=True)
            for ent in ('m00_b_tc35874x 4-000f:0', 'rockchip-csi2-dphy0:0',
                        'rockchip-csi2-dphy0:1', 'rockchip-mipi-csi2:0',
                        'rockchip-mipi-csi2:4'):
                sh(f"media-ctl -d /dev/media0 --set-v4l2 '\"{ent}\"[fmt:UYVY8_2X8/{self.w}x{self.h}]'")
            sh(f"v4l2-ctl -d {self.dev} --set-fmt-video=width={self.w},height={self.h},pixelformat=UYVY")
            self._alloc()

    def _alloc(self):
        """按当前节点格式申请 buffer。大小在 REQBUFS 时按格式定死，
        之后 S_FMT 改格式不会重分配，必须 _free 后重新 _alloc。"""
        if self.fd is None:
            raise OSError("capture not open")
        req = bytearray(20)
        struct.pack_into('<IIII', req, 0, 4, MPLANE, MEM_MMAP, 0)
        fcntl.ioctl(self.fd, VIDIOC_REQBUFS, req, True)
        self.nbuf = struct.unpack_from('<I', req, 0)[0]
        assert self.nbuf >= 2
        self.maps = []
        self.buf_bytes = 0
        for i in range(self.nbuf):
            ctypes.memset(self._pp, 0, 64)
            fcntl.ioctl(self.fd, VIDIOC_QUERYBUF, self._buf(i), True)
            plen = struct.unpack_from('<I', bytes(self._plane), 4)[0]
            poff = struct.unpack_from('<I', bytes(self._plane), 8)[0]
            self.maps.append(mmap.mmap(self.fd, plen, offset=poff))
            self.buf_bytes = plen

    def _free(self):
        if self.fd is None:
            return
        for m in getattr(self, 'maps', []):
            try:
                m.close()
            except (OSError, ValueError):
                pass
        self.maps = []
        req = bytearray(20)
        struct.pack_into('<IIII', req, 0, 0, MPLANE, MEM_MMAP, 0)
        fcntl.ioctl(self.fd, VIDIOC_REQBUFS, req, True)

    def realloc(self):
        """格式变化后重申请 buffer（调用前必须已 stream_off）"""
        self._free()
        self._alloc()

    def stream_on(self):
        if self.be_name == 'gst':
            self._gst.stream_on()
            return
        if self.fd is None:
            return
        for i in range(self.nbuf):
            fcntl.ioctl(self.fd, VIDIOC_QBUF, self._buf(i), True)
        fcntl.ioctl(self.fd, VIDIOC_STREAMON, struct.pack('<I', MPLANE))

    def stream_off(self):
        if self.be_name == 'gst':
            self._gst.stream_off()
            return
        if self.fd is None:
            return
        try:
            fcntl.ioctl(self.fd, VIDIOC_STREAMOFF, struct.pack('<I', MPLANE))
        except OSError:
            pass

    def close(self):
        """释放采集后端（gst 管线或设备 fd+mmap），幂等。
        原生必须 _free 掉 mmap：buffer 映射还在的话 rkcif 认为设备仍被
        占用，gst 接管时 v4l2src open 报 Device busy（2026-09-30 实测）"""
        self.stream_off()
        if self.be_name == 'gst':
            if self._gst is not None:
                self._gst.free()
                self._gst = None
        else:
            self._free()
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None

    def grab_latest(self, timeout=2.0):
        """排干队列，返回最新一帧的 bytes；无帧返回 None"""
        if self.be_name == 'gst':
            return self._gst.grab_latest(timeout)
        if self.fd is None:
            return None
        idxs = []
        while True:
            r, _, _ = select.select([self.fd], [], [], 0 if idxs else timeout)
            if not r:
                break
            b = self._buf(0)
            try:
                fcntl.ioctl(self.fd, VIDIOC_DQBUF, b, True)
            except OSError:
                break
            idxs.append(struct.unpack_from('<I', b, 0)[0])
        if not idxs:
            return None
        for i in idxs[:-1]:
            fcntl.ioctl(self.fd, VIDIOC_QBUF, self._buf(i), True)
        last = idxs[-1]
        self.maps[last].seek(0)
        need = self.w * self.h * 2
        raw = self.maps[last].read(need)
        fcntl.ioctl(self.fd, VIDIOC_QBUF, self._buf(last), True)
        # mmap 比帧小（驱动格式掉了、buffer 是旧尺寸）时 read 被截断，
        # 短帧交出去会编出绿屏——返回 None 让上层走零帧重对齐
        return raw if len(raw) == need else None

    def _sync_filter(self):
        """把亮度/锐化参数下发给 C shim（硬编/422软编路径在 C 拷贝趟里
        完成补偿，零额外耗时）；老 shim 无此符号则 python 侧 numpy 兜底（慢）。"""
        if _lj and self.enc == 'sw422':
            _lj.ljpeg_set_filter(self.bright, self.sharp)
        elif _HAS_FILTER and self.enc == 'hw':
            _mppj.mppjpeg_set_filter(self.bright, self.sharp)

    def set_bright(self, val):
        """亮度增益（%，100=原始）。MS9288C 的 SCL/SDA 原理图未接，调不了
        芯片增益，只能在采集端对 Y 分量做数字补偿（色度不动，色饱和度不变）。"""
        self.bright = max(20, min(200, int(val)))
        if self.bright == 100:
            self._blut = None
        else:
            self._blut = np.clip(np.arange(256, dtype=np.float32)
                                 * (self.bright / 100.0), 0, 255).astype(np.uint8)
        self._sync_filter()

    def _apply_bright(self, raw):
        """UYVY 只调 Y（奇数字节）。LUT 查表，720p 帧 ~2ms。
        仅 PIL 编码路径用；硬编路径由 C shim 在拷贝趟里做。"""
        if self._blut is None:
            return raw
        a = np.frombuffer(raw, dtype=np.uint8).copy()
        a[1::2] = self._blut[a[1::2]]
        return a.tobytes()

    def set_sharp(self, val):
        """锐化强度（0~100，0=关闭）。补偿 VGA 模拟采样（ADC 相位对不齐
        像素时钟）造成的边缘模糊，只作用在 Y 分量上。"""
        self.sharp = max(0, min(100, int(val)))
        self._sync_filter()

    def _apply_sharp(self, raw):
        """unsharp mask：y + k*(y - 十字模糊)，k=sharp/50（100→k=2）。
        边缘 1 像素环不处理。720p 帧 ~75ms——仅 PIL 回退路径用，
        硬编路径由 C shim 做（~3ms）。"""
        if not self.sharp:
            return raw
        k = self.sharp / 50.0
        a = np.frombuffer(raw, dtype=np.uint8).copy()
        w, h = self.ew, self.eh
        y = a.reshape(h, w // 2, 4)[:, :, 1::2].reshape(h, w)
        c = y[1:-1, 1:-1].astype(np.int16)
        blur = (y[1:-1, :-2].astype(np.int16) + y[1:-1, 2:].astype(np.int16)
                + y[:-2, 1:-1].astype(np.int16) + y[2:, 1:-1].astype(np.int16)) // 4
        y[1:-1, 1:-1] = np.clip(c + (c - blur) * k, 0, 255).astype(np.uint8)
        return a.tobytes()

    def _scale_uyvy(self, raw, dw=None, dh=None):
        """采集帧等比降到目标几何（默认清晰档编码几何，流畅档传减半值）。
        优先 RGA 硬件缩放（~3ms）；无 RGA（老 shim）回退 PIL 平面缩放
        （~190ms，仅应急）。"""
        ew, eh = dw or self.ew, dh or self.eh
        if _HAS_RGA:
            if self._scbuf is None or len(self._scbuf) != ew * eh * 2:
                self._scbuf = ctypes.create_string_buffer(ew * eh * 2)
            if _mppj.uyvy_scale(_cp(raw), self.w, self.h,
                                self._scbuf, ew, eh) == 0:
                return self._scbuf.raw
        a = np.frombuffer(raw, dtype=np.uint8).reshape(self.h, self.w // 2, 4)
        y = a[:, :, 1::2].reshape(self.h, self.w)
        u, v = a[:, :, 0], a[:, :, 2]
        ya = np.asarray(Image.fromarray(y).resize((ew, eh), Image.BILINEAR))
        ua = np.asarray(Image.fromarray(u).resize((ew // 2, eh), Image.BILINEAR))
        va = np.asarray(Image.fromarray(v).resize((ew // 2, eh), Image.BILINEAR))
        out = np.empty((eh, ew * 2), dtype=np.uint8)
        out[:, 0::4] = ua
        out[:, 1::4] = ya[:, 0::2]
        out[:, 2::4] = va
        out[:, 3::4] = ya[:, 1::2]
        return out.tobytes()

    def _reopen_enc(self):
        """源分辨率 follow 后按新编码几何重开编码器实例。与编码调用互斥
        （enc_lock）。注意：几何永远取 (ew,eh)——640x360 减半几何的
        close/open 实测会 wedge 内核，流畅档不走硬编无需切几何。"""
        with self.enc_lock:
            if self.enc == 'sw422' and _lj:
                try:
                    _lj.ljpeg_close()
                except Exception:
                    pass
                self.enc = 'pil'
            elif _mppj:
                try:
                    _mppj.mppjpeg_close()
                except Exception:
                    pass
                self.enc = 'pil'
                self._pipe = False        # 流水实测无收益（单引擎串行），恒停用
            if self.enc == 'pil':         # 按优先级重建：sw422 → hw → pil
                if _lj and _lj.ljpeg_open(self.ew, self.eh) == 0:
                    self.enc = 'sw422'
                elif _mppj and _mppj.mppjpeg_open(self.ew, self.eh) == 0:
                    self.enc = 'hw'
            self._sync_filter()
            self._hwfail = 0
            self._hwbuf = None
            self._pi = 0
            self._inflight = 0

    def uyvy_to_jpeg(self, raw):
        """UYVY → JPEG。清晰档走 vepu 硬编（720p ~20ms，limited→full 拉伸和
        亮度/锐化滤波在 shim 拷贝趟里做，零 numpy）；流畅档走 PIL 640x360
        软编（~20ms——640x360 硬编几何切换实测连续失败并 wedge 内核 2 次，
        禁用！）。硬编连续失败回退 PIL。numpy 锐化 75ms/帧，流畅档跳过
        （软编低分辨率下收益小）。采集分辨率高于 720p 先 RGA 缩放。"""
        import io
        _t = time.time()
        hw = self.enc in ('hw', 'sw422') and not self.half  # 流畅永不走 shim（见上）
        if (self.w, self.h) != (self.ew, self.eh):
            raw = self._scale_uyvy(raw)
        self._t_sc = getattr(self, '_t_sc', 0.0) + time.time() - _t
        _t = time.time()
        w, h = self.ew, self.eh
        # 亮度/锐化补偿：shim 路径由 C 在编码拷贝趟里做（~3ms），
        # python 侧不能先动数据（否则双重补偿）；PIL 路径 / 老 shim 才在
        # python 侧 numpy 做
        hw_filter = hw and (self.enc == 'sw422' or _HAS_FILTER)
        if not hw_filter:
            raw = self._apply_bright(raw)
            if not self.half:
                raw = self._apply_sharp(raw)
        if hw:
            with self.enc_lock:
                if self._hwbuf is None or len(self._hwbuf) != len(raw):
                    self._hwbuf = ctypes.create_string_buffer(len(raw))
                if self.enc == 'sw422':
                    n = _lj.ljpeg_encode(_cp(raw), len(raw),
                                         self._hwbuf, len(self._hwbuf))
                else:
                    n = _mppj.mppjpeg_encode(_cp(raw), len(raw),
                                             self._hwbuf, len(self._hwbuf))
            self._t_hw = getattr(self, '_t_hw', 0.0) + time.time() - _t
            if n > 0:
                self._hwfail = 0
                return self._hwbuf.raw[:n]
            self._hwfail += 1
            if self._hwfail >= 3:                    # 连续失败永久回退 PIL 软编
                self.enc = 'pil'
            if hw_filter:                            # 本帧 shim 失败转 PIL，补做补偿
                raw = self._apply_bright(raw)
                raw = self._apply_sharp(raw)
        a = np.frombuffer(raw, dtype=np.uint8, count=w * h * 2)
        a = a.reshape(h, w // 2, 4)
        if self.half:                                # 流畅：隔行隔列 → 640x360（PIL）
            sub = a[::2]
            y = sub[:, :, 1::2].reshape(h // 2, w)[:, ::2]
            u, v = sub[:, :, 0], sub[:, :, 2]        # 半宽后 U/V 1:1，免 repeat
        else:                                        # 清晰：全分辨率 1280x720（PIL 回退）
            y = a[:, :, 1::2].reshape(h, w)
            u, v = a[:, :, 0], a[:, :, 2]            # U/V 保持半宽，拉伸完再 repeat
        y = _LUT_Y[y]                                # LUT 替代 int32 算术，同式防溢出
        u, v = _LUT_UV[u], _LUT_UV[v]
        if not self.half:
            u = np.repeat(u, 2, axis=1)
            v = np.repeat(v, 2, axis=1)
        ycbcr = np.dstack([y, u, v])
        img = Image.frombytes('YCbCr', (ycbcr.shape[1], ycbcr.shape[0]), ycbcr.tobytes())
        out = io.BytesIO()
        img.save(out, 'JPEG', quality=self.q, subsampling=1)
        # PIL 直存 YCbCr 不过 RGB；subsampling=1 = 4:2:2，纵向色度全分辨率
        # （默认 4:2:0 会把 1px 高彩色细线洗成灰色，同 vepu 硬编的毛病）
        return out.getvalue()


# ---------------- GStreamer 采集后端（网页可切换的可选管线） ----------------

class _H264Client:
    """一个 /h264 客户端的接收队列。wait_idr：刚接入或溢出后必须等下一个
    IDR 再发——H264 丢 P 帧即花屏，宁可断到下一 GOP 也不发残流。"""
    def __init__(self):
        self.q = queue.Queue(maxsize=120)      # ~4s @30fps
        self.wait_idr = True


class _H264Tap:
    """H264 支路扇出：拉取线程把每个 access unit 分发给各客户端有界队列。
    队列满（客户端慢）则清空并标记等下一 IDR。SPS/PPS（与 IDR 同 AU）缓存
    用于新客户端接入即补头。产出裸 Annex-B 流，VLC/mpv/ffplay 直接播放。"""
    def __init__(self):
        self.clients = []
        self.lock = threading.Lock()
        self.sps_pps = b''

    def add(self, c):
        with self.lock:
            self.clients.append(c)

    def remove(self, c):
        with self.lock:
            if c in self.clients:
                self.clients.remove(c)

    def feed(self, data):
        if b'\x00\x00\x00\x01\x67' in data or b'\x00\x00\x01\x67' in data:
            self.sps_pps = data
        with self.lock:
            cl = list(self.clients)
        for c in cl:
            try:
                c.q.put_nowait(data)
            except queue.Full:
                while not c.q.empty():
                    try:
                        c.q.get_nowait()
                    except queue.Empty:
                        break
                c.wait_idr = True


class GstBE:
    """GStreamer 采集后端：v4l2src(NV16) → tee → ① videoconvert →
    caps(UYVY/WxH) → appsink(pull)；② videoconvert → NV12 → mpph264enc →
    appsink(/h264 裸流)。
    与原生 ioctl 后端同接口（open/stream_on/stream_off/free/grab_latest），
    对齐序列、消卷、亮度/锐化、编码链全部复用不变。appsink pull 模式 +
    drop=true max-buffers=1 等效原生"排干队列取最新帧"；无需 GLib 主循环
    线程，bus 错误在 grab 时顺带回收。v4l2src 在进 PLAYING 时才做 S_FMT
    ——换分辨率必须整管线重建（build），不能只改 caps。
    为什么采 NV16 再软转 UYVY：rkcif 驱动经 v4l2src 直接出 UYVY 时画面
    左右对半复制+隔行梳齿（原生 ioctl 路径同格式正常，半平面格式正常，
    疑驱动 UYVY out_fmt 的 cplanes/bpp 配置与 gst buffer 协商路径不合），
    改采半平面格式由 videoconvert 重排为 packed UYVY，720p 下 A55 耗时
    毫秒级，下游零改动。为什么 NV16 而非 NV12：NV12 是 4:2:0，纵向色度
    砍半——1px 高的彩色细线与相邻背景行平均后饱和度掉光变灰（2026-10-29
    客户实测）；NV16 是 4:2:2 与芯片输出同源，纵向色度全分辨率，配
    libjpeg 4:2:2 软编保住细线颜色。H264 支路 mpph264enc 只吃 NV12，
    经 videoconvert 转（H264 本就 4:2:0，无损失）；插件缺失时自动退化
    为无 H264 支路的单支路管线。"""
    def __init__(self, cap):
        self.cap = cap
        self.pipe = None
        self.sink = None
        self.h264sink = None
        self.bus = None
        self.tap = _H264Tap()
        self._pull_stop = None
        self._pull_thread = None

    def open(self):
        self.build(self.cap.w, self.cap.h)

    def build(self, w, h):
        self.free()
        try:
            self.pipe = Gst.parse_launch(
                f'v4l2src device={self.cap.dev} ! '
                f'video/x-raw,format=NV16,width={w},height={h} ! tee name=t '
                't. ! queue max-size-buffers=2 leaky=downstream ! videoconvert ! '
                f'video/x-raw,format=UYVY,width={w},height={h} ! '
                'appsink name=sink drop=true max-buffers=1 sync=false emit-signals=false '
                't. ! queue max-size-buffers=4 leaky=downstream ! '
                'videoconvert ! video/x-raw,format=NV12 ! '
                'mpph264enc bps=2000000 gop=30 ! '
                'appsink name=h264sink drop=true max-buffers=4 sync=false emit-signals=false')
            self.h264sink = self.pipe.get_by_name('h264sink')
        except Exception as e:
            print(f"[gst] H264 支路不可用（{e}），退化为纯采集管线", flush=True)
            self.pipe = Gst.parse_launch(
                f'v4l2src device={self.cap.dev} ! '
                f'video/x-raw,format=NV16,width={w},height={h} ! '
                'videoconvert ! '
                f'video/x-raw,format=UYVY,width={w},height={h} ! '
                'appsink name=sink drop=true max-buffers=1 sync=false emit-signals=false')
            self.h264sink = None
        self.sink = self.pipe.get_by_name('sink')
        self.bus = self.pipe.get_bus()

    def stream_on(self):
        if self.pipe is not None:
            self.pipe.set_state(Gst.State.PLAYING)
            if self.h264sink is not None and self._pull_thread is None:
                self._pull_stop = threading.Event()
                self._pull_thread = threading.Thread(
                    target=self._h264_puller,
                    args=(self.h264sink, self._pull_stop), daemon=True)
                self._pull_thread.start()

    def stream_off(self):
        # PAUSED 而不是 NULL：保持设备持有，语义等同原生 STREAMOFF
        if self.pipe is not None:
            self.pipe.set_state(Gst.State.PAUSED)

    def free(self):
        if self._pull_stop is not None:
            self._pull_stop.set()
        if self._pull_thread is not None:
            self._pull_thread.join(timeout=1.5)
        self._pull_stop = self._pull_thread = None
        if self.pipe is not None:
            self.pipe.set_state(Gst.State.NULL)
        self.pipe = self.sink = self.h264sink = self.bus = None

    def _h264_puller(self, sink, stop):
        while not stop.is_set():
            try:
                s = sink.try_pull_sample(int(0.5 * Gst.SECOND))
            except Exception:
                break
            if s is None:
                continue
            b = s.get_buffer()
            self.tap.feed(b.extract_dup(0, b.get_size()))

    def grab_latest(self, timeout=2.0):
        if self.pipe is None:
            return None
        self._poll_bus()
        s = self.sink.try_pull_sample(int(timeout * Gst.SECOND))
        self._poll_bus()
        if s is None:
            return None
        b = s.get_buffer()
        raw = b.extract_dup(0, b.get_size())
        # 尺寸不符（协商掉了格式）视同坏帧：短帧交出去会编出绿屏
        need = self.cap.w * self.cap.h * 2
        return raw if len(raw) == need else None

    def _poll_bus(self):
        while self.bus is not None:
            m = self.bus.pop()
            if m is None:
                break
            if m.type == Gst.MessageType.ERROR:
                err, _ = m.parse_error()
                print(f"[gst] {err.message}", flush=True)
                # 2026-10-30 实捕：gst 管线可能静默卡死（无 ERROR 也有一条
                # 路——appsink 永远 None），有 ERROR 更是铁证。打时间戳
                # 交给看门狗重建管线，比"无帧 15s"更快更准
                self.cap.gst_dead = time.time()


def _adopt_detected_timings():
    """漏边兜底：让驱动立即采纳芯片已检测到的时序。format_change 靠 SYNC/DE
    中断边沿触发，芯片在驱动 probe 前已锁定（源一直稳定输出）时永远等不到
    边沿，配置时序卡在默认 640x480，链路格式被钳死，720p 协商永远失败
    （2026-10-28 实测：开机前源已输出/开机后插回同分辨率信号，gst 永久
    Internal data stream error）。时序一致时 s_dv_timings 秒回无副作用；
    内核 1Hz 轮询兜底之外，这里在配置失败时主动触发一次立即自愈。"""
    sd = find_subdev()
    if sd:
        sh(f"v4l2-ctl -d {sd} --set-dv-bt-timings query 2>/dev/null")


def _config_stream(cap, w, h):
    """配格式再开流。5.10 vb2_is_busy() 看的是已申请 buffer 数
    （不是是否在流）：只要 REQBUFS 过，S_FMT 就被 rkcif 以 queue busy
    拒绝——必须先 _free 释放 buffer 再配格式，配完 _alloc 重申请。
    S_FMT 可能静默失败（sh 不看退出码），必须回读确认——否则驱动
    停在 640x480/RG10 默认格式（format_change 会把节点格式重置回默认），
    帧照流但内容是垃圾（绿屏/空帧）。回读确认失败时先补一次漏边兜底
    （采纳检测时序）再重试一轮。"""
    if cap.be_name == 'gst':
        # gst 路径：链路基元格式仍要 media-ctl 配好（同原生）；S_FMT 由
        # v4l2src 进 PLAYING 时按 caps 过滤器完成，故整管线重建再开流，
        # 回读确认同样保留（协商静默失败时帧尺寸校验也会兜底）。
        # 不走 stream_off：对 NULL 态管线 set PAUSED 会让 v4l2src 白白
        # 提前打开设备；free(NULL) 的 close→重建→PLAYING 已覆盖
        # "关流解静音→重开流"的对齐语义
        for attempt in range(2):
            for ent in ('m00_b_tc35874x 4-000f:0', 'rockchip-csi2-dphy0:0', 'rockchip-csi2-dphy0:1',
                        'rockchip-mipi-csi2:0', 'rockchip-mipi-csi2:4'):
                sh(f"media-ctl -d /dev/media0 --set-v4l2 '\"{ent}\"[fmt:UYVY8_2X8/{w}x{h}]'")
            cap._gst.build(w, h)
            cap.stream_on()
            time.sleep(0.5)          # S_FMT 发生在开流瞬间，回读前等一拍
            fmt = sho("v4l2-ctl -d /dev/video0 --get-fmt-video 2>/dev/null")
            # v4l2src 节点格式是 NV16（UYVY 由 videoconvert 在管线内软转）
            if f"{w}/{h}" in fmt and "'NV16'" in fmt:
                return True
            if attempt == 0:
                print("[config] fmt confirm failed, adopt detected timings "
                      "and retry", flush=True)
                _adopt_detected_timings()
        return False
    for attempt in range(2):
        cap.stream_off()
        cap._free()
        for ent in ('m00_b_tc35874x 4-000f:0', 'rockchip-csi2-dphy0:0', 'rockchip-csi2-dphy0:1',
                    'rockchip-mipi-csi2:0', 'rockchip-mipi-csi2:4'):
            sh(f"media-ctl -d /dev/media0 --set-v4l2 '\"{ent}\"[fmt:UYVY8_2X8/{w}x{h}]'")
        sh(f"v4l2-ctl -d /dev/video0 --set-fmt-video=width={w},height={h},pixelformat=UYVY")
        fmt = sho("v4l2-ctl -d /dev/video0 --get-fmt-video 2>/dev/null")
        cap.realloc()                    # 无论成败都按当前节点格式重申请 buffer
        if f"{w}/{h}" in fmt and "'UYVY'" in fmt:
            cap.stream_on()
            return True
        if attempt == 0:
            print("[config] fmt confirm failed, adopt detected timings "
                  "and retry", flush=True)
            _adopt_detected_timings()
    return False



def _enc_size(w, h):
    """编码几何：vepu 硬编 720p ~11ms/帧而 1080p >1s/帧，超过 720p 的源
    等比降到 720p 框内（取偶）再编码。<=720p 原样。"""
    r = min(1.0, 1280 / w, 720 / h)
    return max(2, int(w * r) // 2 * 2), max(2, int(h * r) // 2 * 2)


def _follow_source(cap, subdev):
    """源分辨率跟随：源输出什么就采什么。主机会强制输出 EDID 之外的分辨率
    （如 EDID 720p 但显卡自定义 1080p），若还按 720p 采只会得到上半屏。
    分辨率变化时按新编码几何重建硬编；参考帧跨分辨率不可相关，直接作废
    由任务栏自举重新对齐。"""
    if not subdev:
        return
    dv = query_dv(subdev)
    w2, h2 = dv['w'], dv['h']
    if w2 < 640 or h2 < 480 or (w2 == cap.w and h2 == cap.h):
        return
    print(f"[follow] source {cap.w}x{cap.h} -> {w2}x{h2}", flush=True)
    cap.w, cap.h = w2, h2
    cap.ew, cap.eh = _enc_size(w2, h2)
    cap._reopen_enc()                # 按新编码几何重开（永远是 ew,eh 全几何）
    cap._scbuf = None
    cap.ref = None
    cap.roll = 0
    cap.roll_manual = False
    cap.roll_epoch += 1



def edid_ok(hextext):
    """EDID 有效性：长度够、非全零负载、校验和正确。全零负载=对齐用的
    垃圾 EDID（若进入对齐时芯片里已是垃圾，绝不能把它当"原 EDID"恢复）"""
    try:
        d = bytes(int(x, 16) for x in hextext.split())
    except ValueError:
        return False
    return len(d) >= 128 and any(d[8:127]) and sum(d[:128]) % 256 == 0


# ---------------- i2c 直控 TC358743（对齐期间清中断锁存） ----------------

class TcI2c:
    """userspace i2c 写 TC358743 寄存器（16-bit 地址）。对齐期间以 20Hz 清
    SYS_INT..KEY_INT 中断锁存：驱动 1s 轮询感知不到 SYNC/DE 变化就不会调
    format_change → 不会在信号恢复瞬态把芯片静音（静音后没有任何东西解除，
    帧永远回不来——这是之前对齐闪烁循环的根源）。"""

    def __init__(self, subdev):
        # /sys/bus/i2c/devices/N-00XX/name 找 tc35874x（subdev 的 device
        # 软链在 4.19 内核指向 rkcif_mipi_lvds 而非 i2c 节点，不能信）
        for d in os.listdir('/sys/bus/i2c/devices'):
            try:
                with open(f'/sys/bus/i2c/devices/{d}/name') as f:
                    if not f.read().strip().startswith('tc35874'):
                        continue
                bus, addr = d.split('-')
                self.fd = os.open(f'/dev/i2c-{bus}', os.O_RDWR)
                self.addr = int(addr, 16)
                return
            except (OSError, ValueError):
                continue
        raise OSError('tc35874x i2c device not found')

    def wr8(self, reg, val):
        data = ctypes.create_string_buffer(bytes([reg >> 8, reg & 0xff, val]), 3)
        # struct i2c_msg {__u16 addr; __u16 flags; __u16 len; pad; __u8 *buf;}
        msg = struct.pack('<HHH', self.addr, 0, 3) + b'\0\0' + struct.pack('<Q', ctypes.addressof(data))
        msgs = ctypes.create_string_buffer(msg, len(msg))
        fcntl.ioctl(self.fd, 0x0707, struct.pack('<QI', ctypes.addressof(msgs), 1))  # I2C_RDWR

    def rd8(self, reg):
        w = ctypes.create_string_buffer(bytes([reg >> 8, reg & 0xff]), 2)
        r = ctypes.create_string_buffer(1)
        msgs = ctypes.create_string_buffer(
            struct.pack('<HHH', self.addr, 0, 2) + b'\0\0' + struct.pack('<Q', ctypes.addressof(w)) +
            struct.pack('<HHH', self.addr, 1, 1) + b'\0\0' + struct.pack('<Q', ctypes.addressof(r)), 56)
        fcntl.ioctl(self.fd, 0x0707, struct.pack('<QI', ctypes.addressof(msgs), 2))  # I2C_RDWR
        return r.raw[0]


def _hpd_clean_relock(tc):
    """HPD 重插造"干净静默→重锁"（2026-10-26 实测）：TX 帧结构在干净锁定下
    重建时相位恒为常数 C0（7/7 收敛）；静默不足（1.2s）曾失败一次，故静默
    3s 且要求源真的停输出（TMDS 消失）、重锁后连续 2s 稳定才算成功。
    之后相位=C0，配合持久化 roll（/var/lib/ccs_roll）无需任何画面参照物。
    返回值：1=干净重锁成功相位钉 0；0=源不理会 HPD（没停输出，重试无
    意义）；2=重锁发生了但驱动仍标 dirty（源停得太晚/抖动，可重试）。
    （2026-10-29：原先只看 has_signal 级别的 dropped，Windows 装死时
    dropped=False 也照常宣布成功、把随机相位钉 0 落盘——实测卷动挂屏。
    现在 dropped=False 直接判失败，且成功后必须驱动确认 dirty=0）"""
    HPD_CTL, SYS_STATUS = 0x8544, 0x8520
    try:
        tc.wr8(HPD_CTL, 0x00)                  # HPD 拉低 = 显示器拔出
        t0 = time.time()
        dropped = False
        while time.time() - t0 < 10:           # 等源真的停输出（Windows
            if not (tc.rd8(SYS_STATUS) & 0x02):  # 响应 HPD 可能要 5s+）
                dropped = True
                break
            time.sleep(0.2)
        time.sleep(3)                          # 充分静默
        tc.wr8(HPD_CTL, 0x01)                  # HPD 拉高 = 重新插入
        if not dropped:
            print("[align] hpd relock: 源不理会 HPD（输出未停），放弃",
                  flush=True)
            return 0
        t0, good = time.time(), 0
        # 源重出图的头几秒常伴随显示管线抖动（Windows 重初始化），2s 稳定
        # 判据可能在抖动中段误判（实测 boot 572 行卷动）：要求锁已连续
        # 稳定 4s 且距 HPD 拉高至少 8s，确认抖动彻底过去
        while time.time() - t0 < 25:
            if tc.rd8(SYS_STATUS) & 0x82 == 0x82:
                good += 1
                if good >= 8 and time.time() - t0 >= 8:
                    # 驱动最终裁决：重锁期间 TMDS 真停过（dirty=0）相位才
                    # 钉 0；装死/晚停的源会让芯片贴着飞行画面重锁，相位
                    # 随机（2026-10-29 实测 boot 590 行卷动）
                    if _relock_dirty():
                        print("[align] hpd relock 后驱动仍标 dirty，"
                              "相位随机不可信（可重试）", flush=True)
                        return 2
                    print("[align] hpd clean relock ok (dropped=True)",
                          flush=True)
                    return 1
            else:
                good = 0
            time.sleep(0.5)
    except OSError:
        pass
    print("[align] hpd clean relock failed/ignored", flush=True)
    return 0


def _clear_latches_loop(tc, stop):
    while not stop.is_set():
        try:
            for reg in range(0x8502, 0x8510):   # SYS_INT..KEY_INT 全部 W1C 清掉
                tc.wr8(reg, 0xff)
        except OSError:
            pass
        time.sleep(0.05)


# ---------------- 卷动相位测量（软件消卷） ----------------

REF_PATH = '/var/lib/ccs_ref.raw'    # 已对齐参考帧持久化：任务栏自动隐藏/
                                     # 全屏场景重启后无锚点，靠它轮廓相关自举
ROLL_PATH = '/var/lib/ccs_roll'      # 已确认相位持久化（w h k）：驱动补丁后
                                     # 相位恒定，测量失败时回退到它而不是 0

def _save_roll(cap):
    try:
        open(ROLL_PATH, 'w').write(f'{cap.w} {cap.h} {cap.roll}')
    except OSError:
        pass

def _load_roll(cap):
    try:
        w, h, k = open(ROLL_PATH).read().split()
        if int(w) == cap.w and int(h) == cap.h:
            cap.roll = int(k) % cap.h
    except (OSError, ValueError):
        pass

def _pinned_roll(w, h):
    """读持久化相位（分辨率匹配才有效）。只约束任务栏/横带/接缝等弱检测
    （2026-10-27 09:32 实测：无任务栏的全屏设置页 via=taskbar 假阳性测出
    338，真值 707，置信落盘把画面卷坏）；split/ref 是强证据不受约束——
    延迟解静音的相位钉定非 100%（同日 10:13 相位 135≠钉值 707，ref 测对）。"""
    try:
        w2, h2, k2 = open(ROLL_PATH).read().split()
        if (int(w2), int(h2)) == (w, h):
            return int(k2) % h
    except (OSError, ValueError):
        pass
    return None

def _load_ref(cap):
    try:
        d = open(REF_PATH, 'rb').read()
        if len(d) == cap.w * cap.h * 2:
            cap.ref = d
    except OSError:
        pass

def _save_ref(cap):
    try:
        open(REF_PATH, 'wb').write(cap.ref)
    except OSError:
        pass

def _row_profile(raw, w, h, step=1):
    """逐行亮度均值（UYVY 亮度在奇数字节）。step>1 时隔 step 列采样，
    1080p 全列相关一次 ~800ms 会卡住采集循环，step=8 降到 ~100ms，
    行轮廓形状对相位相关足够。"""
    a = np.frombuffer(raw, dtype=np.uint8).reshape(h, w * 2)
    return a[:, 1::2 * step].mean(axis=1)


def _measure_roll(cur, ref, w, h):
    """cur 相对 ref（已对齐）的垂直卷动行数 k：buffer 第 b 行 = 源画面第 (b+k)%h 行。
    行亮度轮廓循环相关，粗搜步长 2 再 ±2 细化。返回 (k, 是否置信)。
    平坦画面（黑桌面+一条任务栏）里错位匹配的误差绝对值也很小，光看阈值
    会误判——要求最佳匹配显著好于全体候选的平均水平。"""
    pc, pr = _row_profile(cur, w, h, 8), _row_profile(ref, w, h, 8)
    best_k, best_d, sum_d, cnt = 0, 1e9, 0.0, 0
    for k in range(0, h, 2):
        d = float(np.abs(np.roll(pr, -k) - pc).mean())
        sum_d += d; cnt += 1
        if d < best_d:
            best_d, best_k = d, k
    for k in range(best_k - 2, best_k + 3):
        d = float(np.abs(np.roll(pr, -(k % h)) - pc).mean())
        if d < best_d:
            best_d, best_k = d, k % h
    avg_d = sum_d / cnt
    conf = best_d < 6 and best_d < 0.5 * avg_d
    return best_k, conf


def _split_fix(cur, w, h):
    """任务栏被帧边界切成上下两截的签名检测：帧顶连续若干行与帧底连续一段
    都是任务栏纹理（std 高且行均亮度明显亮过桌面），中间是大段桌面。
    命中时正确相位 = h - 顶截行数（顶截恰好接到贴底截之后）。绝对锚点，
    不依赖参考帧、不会连锁中毒；未命中返回 None。彩色任务栏导致顶沿法/
    占比法全失效时（实测：cov 0.46 过不了 0.7 闸门，cwf hot 行太少），
    这是唯一能自愈的途径。"""
    a = np.frombuffer(cur, dtype=np.uint8).reshape(h, w * 2)[:, 1::2].astype(np.float32)
    a8 = a[:, ::8]
    p, s_ = a8.mean(axis=1), a8.std(axis=1)
    desk = float(p[h // 2: h // 2 + 100].mean())     # 桌面基准亮度

    def tb(y):
        return s_[y] > 30 and p[y] > desk + 15

    top = 0                                          # 顶截行数
    while top < h // 8 and tb(top):
        top += 1
    if top < 2:                                      # 小至 2 行的顶截也要修
        return None
    bot = 0                                          # 底截行数
    while bot < h // 6 and tb(h - 1 - bot):
        bot += 1
    if bot < 20:                                     # 底截应含大半个任务栏
        return None
    if tb(top + 5) or tb(h - bot - 5):               # 两截之间必须是桌面
        return None
    return (h - top) % h


def _seam_pos(f, w, h):
    """单帧接缝候选（buffer 行号）：行亮度轮廓的最强跳变即卷绕缝（源画面
    底行与顶行的拼接处）。强度闸门（≥15 且 ≥4×中位数）之后两路：
    - 唯一性强（最强 ≥1.8× 次强，±3 行邻域除外）→ 直接采信；内容里存在
      多个可比强度水平边时宁可不判，误判会把好画面卷错并污染参考帧；
    - 否则双峰仲裁：恰好两个可比强边夹出一条 15~100·s 行的带时，取带的
      下沿为接缝（把带转到帧底）——覆盖"任务栏/Dock 被卷到帧中间"这种
      两沿等强的典型场景（2026-10-26 实测 Win11 深色任务栏被卷到帧中部，
      顶沿 173 底沿 184，唯一性闸门必拒，五级链全灭）。真正无结构的歧义
      仍返回 None。
    纯色/黑屏或顶底行本就同色（黑壁纸+暗任务栏）时检不出接缝，但那种画面
    错位也看不见，返回 None 不处理。"""
    s = h / 720.0
    p = _row_profile(f, w, h, 8)
    d = np.abs(np.diff(np.concatenate([p[-1:], p])))  # d[y]=|p[y]-p[y-1]|，d[0]=回绕边
    ys = int(np.argmax(d))
    if d[ys] < max(15.0, 4.0 * float(np.median(d))):
        return None                         # 没有显著接缝（可能已对齐或太平坦）
    idx = np.arange(h)
    circ = np.minimum((idx - ys) % h, (ys - idx) % h)
    d2 = np.where(circ > 3, d, -1.0)
    y2 = int(np.argmax(d2))
    if d[ys] >= 1.8 * d2[y2]:
        return ys                           # 唯一强边
    y1, y2b = min(ys, y2), max(ys, y2)
    if y2b - y1 <= h // 2:
        seam, band = y2b, y2b - y1          # 带=(y1,y2b]，下沿 y2b
    else:
        seam, band = y1, h - (y2b - y1)     # 带=(y2b,y1]跨回绕，下沿 y1
    if not (int(15 * s) <= band <= int(100 * s)):
        return None                         # 两峰间距不像任务栏/Dock，不判
    return seam


def _seam_roll(frames, w, h):
    """通用自举：不依赖任务栏等任何具体内容。单帧候选见 _seam_pos，此处再加
    时间稳定性闸门：连续 3 帧候选位置一致（±1 行）——内容边缘随画面变化
    漂移，接缝只随采样时钟亚行级慢漂。返回相位 k 或 None。"""
    if len(frames) < 3:
        return None
    pos = []
    for f in frames[-3:]:
        sp = _seam_pos(f, w, h)
        if sp is None:
            return None
        pos.append(sp)
    if max(pos) - min(pos) > 1:
        return None
    return (h - pos[-1]) % h


def _floating_band_roll(cur, w, h):
    """悬浮横带自举（兜底）：纯色背景上的唯一高对比横带。典型场景：黑壁纸 +
    彩色任务栏——顶沿法过不了主色覆盖率 0.7 闸门（实测彩色任务栏 cov 0.46），
    占比法看行内偏离、对均匀色带失敏（带内行 bg≈自身，hot 行只剩图标那几行，
    凑不够带高）。本法改用行均值偏离全局背景找带。门槛：带完整落在帧内部
    （贴顶/贴底归分截法管）、带长 20~80 行（按 h/720 缩放）、带外行必须平坦
    （有窗口等其他内容就不判，避免把标题栏之类误转到帧底）。
    命中返回把带底转到帧底的相位 k；多带/无带/背景不平坦返回 None。"""
    s = h / 720.0
    a8 = np.frombuffer(cur, dtype=np.uint8).reshape(h, w * 2)[:, 1::16].astype(np.float32)
    p, sd = a8.mean(axis=1), a8.std(axis=1)
    bg = float(np.median(p))
    band_row = (np.abs(p - bg) > 10) | (sd > 20)
    runs, top = [], None
    for y in range(h):
        if band_row[y] and top is None:
            top = y
        elif not band_row[y] and top is not None:
            runs.append((top, y - 1)); top = None
    if top is not None:
        runs.append((top, h - 1))
    lo, hi = int(20 * s), int(80 * s)
    cands = [(t, b) for t, b in runs
             if lo <= b - t + 1 <= hi and t > 2 and b < h - 3]
    if len(cands) != 1:
        return None
    t, b = cands[0]
    outside = np.ones(h, dtype=bool)
    outside[max(0, t - 3):b + 4] = False
    if float(np.abs(p[outside] - bg).max()) > 5 or float(sd[outside].max()) > 5:
        return None                         # 带外不平坦：不是纯色背景+横带
    return (h - 1 - b) % h


def _taskbar_roll(cur, w, h):
    """无参考帧时的自举：扫描全部候选相位 k（步长 2，±1 细化），找
    Windows 任务栏的顶沿——任务栏贴底（底 36 行有图标/托盘纹理但
    整行主色一致），其顶沿是全帧该位置上最强的跳变（任务栏与上方
    内容颜色必然不同）。明暗主题通吃，对大片窗口/纯色桌面免疫
    （窗口行主色覆盖率跨桌面时 <0.7 出局，纯色区纹理 std 太小出局）。
    找不到（全屏应用盖住任务栏等）返回 None。
    像素常量以 720p 为基准随 h 线性缩放，1080p 源同样适用。"""
    s = h / 720.0
    band = max(24, int(36 * s))             # 任务栏贴底带高
    # 顶沿搜索窗口下界不能再往下放（试过 h-20·s：任务栏底沿/接缝会落进窗口
    # 变成更强候选，唯一性检查反而全拒）。已对齐帧上本法可能答 ±2 行级的
    # 微偏（真顶沿恰好压在窗口外），不可见且由 track 接缝复检兜底收敛
    e_hi, e_lo = h - int(48 * s), h - int(29 * s)   # 顶沿搜索窗口
    a = np.frombuffer(cur, dtype=np.uint8).reshape(h, w * 2)[:, 1::2].astype(np.float32)
    a8 = a[:, ::8]                          # 隔 8 列采样：1080p 逐行直方图
    p = a8.mean(axis=1)                     # 全列太贵，形状不变精度够用
    s_ = a8.std(axis=1)
    cov_row = np.empty(h)                       # 每行主色覆盖率，与相位无关只需算一次
    for y in range(h):
        hist, _ = np.histogram(a8[y], bins=16, range=(0, 256))
        cov_row[y] = hist.max() / a8.shape[1]
    cands = []
    for k in range(0, h, 2):
        if float(np.roll(s_, k)[h - band:].mean()) < 12.0:
            continue
        if float(np.roll(cov_row, k)[h - band:].mean()) < 0.7:
            continue
        pr = np.roll(p, k)
        edge = max(abs(float(pr[t]) - float(pr[t - 1])) for t in range(e_hi, e_lo))
        # 任务栏必须贴底且底边与带内同色。带内图标/托盘造成的亮度起伏不能否决
        # 一个强顶沿（Win11 深色任务栏实测带内 |Δp|=33 而顶沿 173，2026-10-26
        # 固定阈值 20 误拒导致五级链全灭），阈值随顶沿强度放宽
        if abs(float(pr[h - 4]) - float(pr[h - 20])) > max(20.0, 0.25 * edge):
            continue                    # 任务栏必须贴底且底边与带内同色
        cands.append((edge, k))
    cands.sort(reverse=True)
    if not cands:
        return None
    best_edge, best_k = cands[0]
    # 次强必须排除 best_k ±3 行邻域——同一条物理顶沿在相邻候选相位上会得到
    # 几乎相同的强度（实测 k=548 与 k=546 同为 173.2），不剔除会把唯一真
    # 任务栏误判成"不唯一"而拒判
    second = next((e for e, kk in cands[1:]
                   if min((kk - best_k) % h, (best_k - kk) % h) > 3), 0.0)
    if best_edge < 15.0 or best_edge < 1.5 * second:
        return None
    fine_k, fine_e = best_k, best_edge
    for k in ((best_k - 1) % h, (best_k + 1) % h):
        pr = np.roll(p, k)
        e = max(abs(float(pr[t]) - float(pr[t - 1])) for t in range(e_hi, e_lo))
        if e > fine_e:
            fine_e, fine_k = e, k
    return fine_k


def _taskbar_roll_cwf(cur, w, h):
    """顶沿法的兜底：按"内容横向占比"直接找任务栏带。把每行切成 40 个
    32px 格，偏离行底色的格占比 >0.5 视为任务栏行——搜索框+居中图标+
    右侧托盘横跨全行(~0.75)，桌面图标只在左侧(<0.2)、窗口文本格偏离小，
    都不入选。彩色任务栏（浅搜索框把主色覆盖率打到 0.47）顶沿法检不出、
    本法可以。命中唯一一条 30~100 行的带才算成功，返回把带底行转到
    帧底的相位 k；找不到返回 None。
    格数与带宽阈值以 720p 为基准随分辨率缩放。"""
    sc = h / 720.0
    a = np.frombuffer(cur, dtype=np.uint8).reshape(h, w * 2)[:, 1::2].astype(np.float32)
    ncell = 40
    cw = w // ncell
    cells = a[:, :ncell * cw].reshape(h, ncell, cw).mean(axis=2)
    bg = np.median(cells, axis=1, keepdims=True)
    hot = (np.abs(cells - bg) > 25).mean(axis=1) > 0.5
    runs, top = [], None
    for y in range(h):
        if hot[y] and top is None:
            top = y
        elif not hot[y] and top is not None:
            runs.append((top, y - 1)); top = None
    if top is not None:
        runs.append((top, h - 1))
    if len(runs) >= 2 and runs[0][0] == 0 and runs[-1][1] == h - 1:
        runs = [(runs[-1][0], runs[0][1] + h)] + runs[1:-1]
    cands = [(t, b) for t, b in runs if 30 * sc <= b - t + 1 <= 100 * sc]
    if len(cands) != 1:
        return None
    return (h - 1 - cands[0][1]) % h


def _measure_phase(raw, recent, ref, w, h, bootstrap=False):
    """单次相位测量链，返回 (k, conf, via)。
    分截签名(绝对锚点)永远最先。bootstrap（无参考帧且无持久化相位，如量产
    首烧）只信 split——任务栏/横带/接缝在客户的任意设备画面上都会稳定假
    阳性（2026-10-27 实测：无任务栏设置页 taskbar 报 338、内容横边过接缝
    全部闸门报 442，无钉值可守卫直接把方正画面卷错），bootstrap 测不出
    就交给 align 的干净重锁锚定相位 0。常态顺序：任务栏 → 横带 → 参考帧
    相关 → 接缝(仅无参考帧时)。任务栏/横带/接缝弱检测受持久化相位守卫约
    束（偏离 >3 行拒绝并回退），split/ref 强证据不受限。"""
    k, conf, m = 0, False, 'none'
    sk = _split_fix(raw, w, h)
    if sk is not None:
        k, conf, m = sk, True, 'split'
    if not conf and bootstrap:
        # bootstrap（首烧/无钉值）只信 split 绝对锚点：seam/taskbar/band 在
        # 客户的任意设备画面上都会稳定假阳性（2026-10-27 14:09 实测：设置页
        # 内容横边过全部闸门，k=442 卷错且无钉值可守卫），测不出就走下面的
        # 干净重锁锚定相位 0——那才是 bootstrap 的确定性出路
        pass
    if not conf and not bootstrap:
        # 任务栏结构检测自带几何自校验（带高/贴底/顶沿强度门槛）
        tk = _taskbar_roll(raw, w, h)
        if tk is None:
            tk = _taskbar_roll_cwf(raw, w, h)
        if tk is not None:
            k, conf, m = tk, True, 'taskbar'
    if not conf and not bootstrap:
        fb = _floating_band_roll(raw, w, h)
        if fb is not None:
            k, conf, m = fb, True, 'band'
    if not conf and not bootstrap and ref is not None:
        k, conf = _measure_roll(raw, ref, w, h)
        if conf:
            m = 'ref'
    if not conf and not bootstrap and ref is None:
        # 接缝检测在常态链里永远最后、且只在无参考帧时启用：真接缝比内容
        # 边缘弱时（黑壁纸+暗任务栏，接缝 d 仅 0.3）会把任务栏顶沿误当
        # 接缝——已两次实测（EDID 切换后/重刷系统后参考帧缺失时被抢跑）
        smk = _seam_roll(recent, w, h)
        if smk is not None:
            k, conf, m = smk, True, 'seam'
    if conf:
        # 防中毒校验：消卷结果仍有分截签名说明 k 是错的，拒绝置信
        out0 = raw[-k * w * 2:] + raw[:-k * w * 2] if k else raw
        if _split_fix(out0, w, h) is not None:
            print(f"[align] reject k={k} via={m}: still split", flush=True)
            k, conf, m = 0, False, 'none'
    if conf and m in ('taskbar', 'band', 'seam'):
        # 弱检测守卫：有持久化相位时偏离 >3 行一律拒绝回退。split/ref 不受
        # 限（相位逐开机随机：707/135/234/82 实测，强证据有权推翻持久化值）。
        # 逃生通道：nudge 手动微调不受守卫限制
        kp = _pinned_roll(w, h)
        if kp is not None and min((k - kp) % h, (kp - k) % h) > 3:
            print(f"[align] reject k={k} via={m}: deviates pinned {kp}",
                  flush=True)
            k, conf, m = kp, False, 'pinned'
    return k, conf, m


def align_and_start(cap, subdev, w, h, max_retry=3):
    """对齐序列（2026-10-27 二次修订）：等内容帧稳定 → 测卷动相位 k（能测
    就测）→ 测不出时自动做一次 HPD 干净重锁：源从帧边界重启输出后 TX 帧
    结构相位恒为 0，是确定性锚点而非摇骰（重锁前相位随机不影响结果），
    重锁后复测仍测不出（画面方正检测器静默）即采信 k=0 并落盘；重锁失败
    （源不理会 HPD）才回退持久化的已知相位，绝不盲回零。cap.roll=k 后采
    集循环按 k 软件消卷。全程 i2c 清芯片中断锁存，防止驱动在配置窗口动
    芯片。k 的测量四级链：分截签名（绝对锚点）→ 任务栏结构 → 浮动横带
    → 参考帧轮廓相关（全场景通用，仅在上次对齐置信时保存，错误相位不会
    自我锁死）→ 接缝检测（不依赖具体内容，接任意 HDMI 源都能自举，弱证
    据：结果不落盘不进参考帧）。成功返回消卷后的首帧 bytes，失败返回
    None。"""
    cap.in_align.set()
    stop = threading.Event()
    try:
        try:
            tc = TcI2c(subdev) if subdev else None
        except (OSError, AttributeError, ValueError):
            tc = None
        if tc:
            threading.Thread(target=_clear_latches_loop, args=(tc, stop), daemon=True).start()
        hpd_tries = 0                    # HPD 重锁次数（脏重锁可重试，限 3 次）
        relocked = False                 # 本轮对齐是否已成功干净重锁（相位锚 0）
        t_deadline = time.time() + 150   # 全局截止：in_align 期间看门狗的挂死
                                         # 检测被屏蔽，绝不能让对齐无限拖——挂死
                                         # 场景要对齐快速退出，放看门狗进来硬复位
                                         # （2026-10-27 热拔插冻结只能重启的教训）。
                                         # 预算：测量 40s + 重锁 ~20s + 重锁后复测 75s
        for attempt in range(max_retry):
            if time.time() > t_deadline:
                print("[align] deadline 150s, abort", flush=True)
                break
            cap.state = f"aligning#{attempt + 1}"
            clean_start = False          # 门控放行后源才出图：相位被驱动钉 0
            if subdev and not has_signal(subdev):
                # 驱动 HPD 门控：冷启动按住源端（HPD 低），应用层开始配置
                # 采集（subdev set_fmt/s_stream）才放行——源从帧边界重启出图，
                # 配合驱动延迟解静音相位恒 0，无需测量直接快速采信锚点。
                # 这里先戳一下 subdev 配格式触发放行再等信号；等不到 =
                # 真没插线或旧内核，放弃本轮走原逻辑（看门狗兜底）
                # 实测：media-ctl --set-v4l2 在未配格式的链路上前置校验
                # 失败（EINVAL），根本触达不到驱动；v4l2-ctl --set-subdev-fmt
                # 直接 ioctl subdev 节点，驱动 set_fmt 完整执行、门控放行。
                # （rc=255 是 v4l2-ctl 写后回读验证失败——get_fmt 报的是
                # 驱动 state->timings 默认 640x480，无害，驱动侧已生效）
                sh(f"v4l2-ctl -d {subdev} --set-subdev-fmt "
                   f"pad=0,width={w},height={h},code=0x2006")
                t_gate = time.time()
                sig_came = False
                while time.time() - t_gate < 12 and time.time() < t_deadline:
                    if has_signal(subdev):
                        sig_came = True
                        break
                    time.sleep(0.5)
                if not sig_came:
                    break
                _adopt_detected_timings()   # 门控期间无时序可采，放行后补采
                clean_start = True
                print("[align] 门控放行后信号到来：源从帧头出图，快速采信",
                      flush=True)
            elif _pinned_roll(cap.w, cap.h) == 0 and not cap.phase_suspect \
                    and not _relock_dirty():
                # 服务重启（门控早已放行、信号一直在）：驱动 1Hz tick 边界解
                # 静音保证相位不变，钉值 0 直接可信，走门控同一快速采信——
                # 实测热重启走 6s 测量窗在无特征画面上必落空，再搭 12s 重锁
                # （20:52 实测全程 ~20s）；相位本来就没变，纯属浪费。仅钉值
                # =0 可快进：非 0 说明上次源不理会 HPD，相位无 0 保证。
                # relock_dirty 兜底：服务停止期间发生的脏重锁看门狗看不到，
                # 驱动标记跨服务重启仍在（2026-10-29）
                clean_start = True
                print("[align] 热启动+钉值0：相位不变，快速采信", flush=True)
            elif cap.phase_suspect:
                # 看门狗报脏重锁：中途重锁的数据缺口让 rkcif DMA 相位错位
                # （2026-10-29 实测：强制静音造 1s 缺口卷 281 行）。不必
                # HPD 重锁（~13s 还打扰源）——驱动的延迟解静音保证 TX 帧
                # 结构对齐输入帧头，本 attempt 首帧后重启一次管线，rkcif
                # 按帧头重锁相位恒 0（实测 4/4 corr=1.000），~5s 归正
                clean_start = True
                print("[align] 脏重锁后管线重启锚定相位 0", flush=True)
            _follow_source(cap, subdev)      # 源切了分辨率就跟过去（如强制 1080p）
            w, h = cap.w, cap.h
            if cap.ref is None:
                _load_ref(cap)               # 跟随后再按新几何加载持久化参考帧
            # 解静音从帧中间恢复输出，相位随机但之后恒定，软件消卷处理
            if not _config_stream(cap, w, h):
                continue
            # 等内容帧回来并连续稳定 10 帧后开始逐帧测量，要求**连续两帧
            # 测出相同相位（±3 行）**才接受——开机/重锁瞬态内容在变化
            # （Windows 扩展屏初始化很慢），单次测量会自信地错
            # （2026-10-27 量产首烧实测：split 在瞬态帧测出 672，真值
            # 713，用户只能手动 nudge）。重锁前窗口 6s（双帧确认只需 ~2
            # 个测量帧，检测器几秒内不命中就是画面没特征/钉值已失效，
            # 等再久也测不出，趁早重锁——2026-10-28 实测冷启动钉值失效
            # 时白等 15s 才重锁，全程 39s，用户嫌慢）；重锁后窗口 20s
            # 且稳定 5 帧即提前采信锚点（重锁后画面方正检测器必然静默，
            # 等满窗口是纯浪费）。一直 0 帧说明芯片还是被静音了（清锁存
            # 与驱动轮询赛跑输了），重试
            raw, stable, none_streak, t0 = None, 0, 0, time.time()
            recent = []                  # 最近 3 帧：内容测量链（最后兜底）用
            last_km = None               # 上一帧测量 (k, via)
            k, conf, m = 0, False, 'none'
            fs_locked = False            # 本轮是否已在 TX 流式中重启过管线
            bootstrap = cap.ref is None and _pinned_roll(w, h) is None
            if not clean_start and not relocked and hpd_tries < 3 and tc:
                # 零图像识别（2026-10-29 用户明确要求）：客户画面不可预测，
                # 内容检测全链（split/taskbar/band/ref/seam）在静态设备 UI
                # 上会稳定地自信报错值——实测 split 两帧一致报 677、ref
                # 轮廓相关报 710（真值均 0），track 后台复测用 split 把
                # 方正画面卷坏两次还落了盘。"两帧一致"闸门救不了：静态画面
                # 的假阳性本来就是稳定的。相位锚点只用确定性手段：门控/
                # 热启动钉值（上方快进）或直接 HPD 干净重锁钉 0；重锁失败
                # （源不理会 HPD）才落回内容测量链做最后兜底。
                # 返回 2（重锁发生了但驱动仍标脏）可重试，最多 3 次
                hpd_tries += 1
                print("[align] 跳过内容测量，直接干净重锁锚定相位 0", flush=True)
                r = _hpd_clean_relock(tc)
                relocked = (r == 1)
                if relocked:
                    cap.ref = None
                    for p in (ROLL_PATH, REF_PATH):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                elif r == 2:
                    continue                # 脏重锁：直接重试，不等测量窗
                else:
                    hpd_tries = 3           # 源不理会 HPD：重试无意义
                continue
            win = 20
            stable_gate = 3 if (clean_start or relocked) else 10
            while time.time() - t0 < win:
                f = cap.grab_latest(timeout=4)
                if f is None:
                    none_streak += 1
                    if none_streak >= 5:             # ~20s 无一帧 → 放弃本轮
                        break
                    continue
                none_streak = 0
                cap.last_frame_ts = time.time()
                if _row_profile(f, w, h).max() > 30:
                    stable += 1
                    raw = f
                    recent = (recent + [f])[-3:]
                else:
                    stable = 0
                    last_km = None
                    continue
                if stable < stable_gate:
                    continue
                if clean_start or relocked:
                    # 确定性锚点路径：相位钉 0 的依据不是"源从帧头出图"的
                    # 假设，而是 rkcif 在 TX 流式中重启管线时按帧头重新
                    # 锁定 DMA 相位（2026-10-29 实证：开机首流在 TX 解静音
                    # 前布防，锁到帧中间，卷 78/171 行；强制静音造 1s 数据
                    # 缺口卷 281 行；TX 流式中重启管线后相位恒 0，4/4
                    # corr=1.000）。首帧到来即证明 TX 已流式，此时再重启
                    # 一次管线钉 0，不做任何内容检测
                    if not fs_locked:
                        print("[align] TX 已流式，重启管线按帧头重锁相位",
                              flush=True)
                        if not _config_stream(cap, w, h):
                            break
                        fs_locked = True
                        raw, stable, t0 = None, 0, time.time()
                        continue
                    if stable >= 5:
                        k, conf, m = 0, True, \
                            'gated' if clean_start else 'relock'
                        break
                    continue
                # 最后兜底：HPD 重锁失败（源不理会）后才走内容测量链
                k2, conf2, m2 = _measure_phase(raw, recent, cap.ref, w, h,
                                               bootstrap)
                if not conf2:
                    last_km = None
                    continue
                if last_km is not None and min((k2 - last_km[0]) % h,
                                               (last_km[0] - k2) % h) <= 3:
                    k, conf, m = k2, True, m2      # 两帧一致，接受
                    break
                if last_km is not None:
                    print(f"[align] transient? {last_km[0]}->{k2} via={m2}, "
                          f"keep waiting", flush=True)
                last_km = (k2, m2)
            if not conf and not relocked and hpd_tries < 3 and tc:
                # 测不出（无参照物均匀画面/一帧没来）→ 自动干净重锁：源响应
                # HPD 会从帧边界重启输出，TX 帧结构重建后相位恒为 0——确定性
                # 复位而非摇骰（2026-10-27 实测：开机相位随机 135/600，重锁
                # 后均归 0；历史 7/7），无论重锁前相位多少都归零，自动路径
                # 同样安全。源不理会 HPD（不停输出）时返回 0，相位不变
                # 无副作用，保留钉值做回退；返回 2（脏重锁）可重试，连同
                # 第一处调用点合计最多 3 次，不会反复打扰源
                hpd_tries += 1
                print("[align] 测不出相位 → 干净重锁锚定相位 0", flush=True)
                r = _hpd_clean_relock(tc)
                relocked = (r == 1)
                if relocked:
                    # 相位已归零重建：旧参考帧/持久化相位全部失效，连文件
                    # 一起清除防误导（下轮 _load_ref 会读回磁盘旧参考帧，
                    # 守卫也会拿旧钉值否决重锁后的正确测量）
                    cap.ref = None
                    for p in (ROLL_PATH, REF_PATH):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                elif r == 2:
                    continue                # 脏重锁：直接重试，不等测量窗
                else:
                    hpd_tries = 3           # 源不理会 HPD：重试无意义
                continue
            if raw is None:
                continue
            if not conf and relocked:
                # 重锁即锚点：相位=0。重锁后画面方正时检测器静默
                # （via=none）是正常现象而非失败，置信落盘 roll=0
                k, conf, m = 0, True, 'relock'
            elif not conf and m != 'pinned':
                # 走到这里说明干净重锁已试过且失败（源不理会 HPD）：测量失败
                # （无任务栏等参照物）时保持已持久化的已知相位，不再回零——
                # 回零在相位恰为非零时等于每次开机必卷动（2026-10-26 实测
                # k=19）。回退基准读持久化文件而非内存 cap.roll：内存值可能
                # 被弱证据路径改过，文件只被置信对齐/nudge 更新
                # （2026-10-27 实测：seam 把内存值卷坏后，手动重对齐测不出，
                # 又回退到被污染的内存值，按钮看起来"无效"）
                kp = _pinned_roll(w, h)
                k = kp if kp is not None else cap.roll
            cap.roll = k
            cap.align_conf, cap.align_via = conf, m
            cap.roll_manual = False        # 重对齐结果取代手动微调
            cap.roll_epoch += 1            # 在飞 track 结果全部过期
            out = raw[-k * w * 2:] + raw[:-k * w * 2] if k else raw
            if conf and m != 'seam':
                cap.ref = bytes(out)                 # 参考帧只在置信时更新
                _save_roll(cap)                      # 相位持久化：重启直接带
                _save_ref(cap)
            # 接缝结果（弱证据）既不落盘也不更新参考帧：误判一旦进入参考帧，
            # 后续 ref 相关测量会置信地锁到错误相位，手动按钮都救不回
            # （2026-10-27 实测，见采集循环 track 分支注释）
            open('/tmp/align_last.raw', 'wb').write(raw)
            print(f"[align] attempt{attempt + 1}: roll={k} conf={conf} via={m} "
                  f"ref={'y' if cap.ref else 'n'}", flush=True)
            cap.state = "streaming" if conf else "对齐未置信"
            if conf:
                cap.phase_suspect = False    # 相位已重新确认，解除快进禁令
            cap.align_done_ts = time.time()    # track 冷却起点（瞬态帧不可信）
            # 成功后清锁存再续 5s：恢复瞬态攒下的中断锁存会在此后 1~2 个
            # 轮询周期被驱动读到，触发 format_change 静音+重开流改变相位
            threading.Timer(5, stop.set).start()
            stop = None
            return out
        cap.state = "align failed"
        cap.align_conf, cap.align_via = False, 'none'
        return None
    finally:
        if stop is not None:
            stop.set()
        cap.in_align.clear()


def _switch_backend(cap):
    """采集管线热切换（原生 ioctl ↔ GStreamer）。/dev/video0 独占：先 close
    旧后端再 _open 新后端，开完置 need_realign 重对齐（重开流相位随机）。
    新后端连试 3 次打不开则回退旧后端；gst 不可用直接忽略请求。"""
    name = cap.want_be
    if name == cap.be_name:
        return
    if name == 'gst' and not _GST_OK:
        cap.want_be = cap.be_name
        return
    print(f"[pipeline] {cap.be_name} -> {name}", flush=True)
    cap.in_align.set()
    try:
        cap.close()
        ok = False
        for tgt in (name, cap.be_name):    # 新后端打不开就回退旧后端
            cap.be_name = tgt
            for attempt in range(3):
                try:
                    cap._open()
                    ok = True
                    break
                except Exception as e:
                    print(f"[pipeline] {tgt} open#{attempt + 1} 失败: {e!r}",
                          flush=True)
                    time.sleep(2)
            if ok:
                break
        if not ok:
            raise OSError("无可用采集后端")
    finally:
        cap.want_be = cap.be_name
        cap.in_align.clear()
    cap.realign_forced = True      # 管线重开后相位真随机，必须完整对齐
    cap.need_realign.set()


def align_supervisor(cap):
    """对齐卡死兜底（2026-10-30 两次实测"永远对齐中"）：align 内部的 150s
    截止只在 attempt 边界检查，线程若死在 attempt 内部（疑似 gst C 层
    try_pull_sample 阻塞，全线程 syscall 均正常等待、无 D 态）永不触发，
    而 in_align 又屏蔽了挂死看门狗 → 无人兜底。本线程独立盯 state：
    aligning/init 持续 >170s（>内部 150s 截止，正常对齐不可能到这）直接
    自杀，systemd Restart=always 拉起全新进程走干净恢复（~15s）。
    判定用"上次脱离对齐或对齐置信的时刻"而非 state 字符串变化——
    attempt 计数（aligning#1→#2）会改字符串，但 gst 管线静默卡死时
    attempt 永远测不到帧、对齐永不置信，空转多久都不算恢复（第三次
    实捕 2026-10-30 晚：state 在 aligning#1/#2 间翻转而卡死依旧）。
    调试：kill -USR1 <pid> 可经 faulthandler 喷全线程栈到 journal。"""
    last_ok = time.time()
    while True:
        time.sleep(10)
        st = cap.state or ''
        if not (st.startswith('aligning') or st == 'init') or \
                getattr(cap, 'align_conf', False):
            last_ok = time.time()          # 不在对齐，或对齐已置信 = 健康
            continue
        if time.time() - last_ok > 170:
            print(f"[supervisor] 对齐卡死 {time.time() - last_ok:.0f}s "
                  f"(state={st})，自杀由 systemd 拉起恢复", flush=True)
            os._exit(70)


def watchdog(cap):
    """监测信号：掉过再恢复（休眠/拔插）会破坏 DMA 相位，需重走对齐序列。
    对齐期间（in_align）的信号波动是序列自己造成的，只更新基线不触发。
    另外监测芯片挂死：TC358743 一旦 I2C 不应答，驱动每秒轮询全部超时刷屏，
    帧停止、整机被拖慢（实测连锁导致 USB 代码43）。特征：>10s 无帧且
    （dv 查询本身耗时>2s——真无信号时查询毫秒级返回）。命中后硬复位芯片
    并重对齐；硬复位后 30s 帧仍不回来则自杀由 systemd 拉起（实测复位后
    gst 管线可能永久 Internal data stream error，重启服务即恢复）。"""
    subdev = find_subdev()
    was_ok = has_signal(subdev)
    ok_since = time.time() if was_ok else 0.0   # 信号连续在的起点
    last_reset = 0.0
    last_rebuild = 0.0                           # 管线上次重建时刻（pipe_dead 用）
    while True:
        time.sleep(1.5)
        t_q = time.time()
        ok = has_signal(subdev)
        qd = time.time() - t_q
        now = time.time()
        if ok and not was_ok:
            ok_since = now
        elif not ok:
            ok_since = 0.0
        cap.sig_ok = ok                            # 发布给网页状态提示（对齐
                                                   # 期也更新：插回线后对齐中的
                                                   # ~90s 内不能让陈旧 False 把
                                                   # 页面停在"无视频输入信号"）
        if cap.in_align.is_set():
            was_ok = ok                            # 跟随基线，不触发
            cap.last_frame_ts = now                # 对齐期不做挂死判定
        else:
            if ok and not was_ok:
                # 信号掉过又回来 = 芯片中途重锁。相位是否可疑看驱动
                # relock_dirty：失锁期间 TMDS 一直在跑（源没停输出）= 脏
                # 重锁，源贴着飞行中的画面恢复，相位摇骰（2026-10-29 实测
                # 一次 0 一次非 0），钉值不可信必须完整对齐；TMDS 掉过
                # （拔线/源停输出）= 干净重锁，源从帧边界重新出图相位钉
                # 0，可继续快进。读不到参数（旧内核）按可疑处理（安全侧）
                dirty = _relock_dirty()
                cap.phase_suspect = dirty
                print(f"[watchdog] signal recovered, relock "
                      f"{'DIRTY, phase suspect' if dirty else 'clean, phase pinned'}",
                      flush=True)
                cap.need_realign.set()             # 信号恢复 → 重对齐
            was_ok = ok
            idle = now - cap.last_frame_ts
            # 挂死复位两级门槛：
            #  - qd>2s（dv 查询本身超时）= i2c 挂死特征，10s 即复位芯片；
            #  - 信号在但无帧：必须按"信号回来后"计时！idle 从最后一帧
            #    算起会把整段拔出时长计入，而驱动的延迟解静音在信号恢复
            #    后要 4s 稳定期才出帧——2026-10-27 实测：HPD 重插后看门狗
            #    在驱动即将解静音时误触发硬复位杀掉 work_unmute，gst 管线
            #    随后进入永久 Internal data stream error 抖动，只能重启
            #    服务。信号连续在 15s 仍无帧才是真死了
            # 2026-10-30 实捕修正：两种故障的病灶不同，必须分开处理——
            #  "信号在但无帧" 多为 gst 管线静默卡死（芯片锁定、rkcif 中断
            #    照跑、appsink 永远 None、总线无 ERROR），复位健康芯片毫无
            #    作用还制造扰动（当晚连复位 8 次）。先重建采集管线；
            #    90s 内重建过仍无帧 → 重启服务（实测重启即恢复）
            chip_wedged = idle > 10 and qd > 2.0
            pipe_dead = (ok_since and now - ok_since > 15 and idle > 10) or \
                        (getattr(cap, 'gst_dead', 0.0) > last_rebuild)
            if pipe_dead and not chip_wedged:
                if last_rebuild and now - last_rebuild < 90:
                    print("[watchdog] pipeline rebuild didn't help -> "
                          "self-restart via systemd", flush=True)
                    os._exit(1)
                last_rebuild = now
                print(f"[watchdog] signal OK but no frames idle={idle:.0f}s "
                      f"-> rebuild pipeline", flush=True)
                try:
                    if cap.be_name == 'gst':
                        cap._gst.build(cap.w, cap.h)   # free+重建， caps 不变
                        cap.stream_on()                # NULL→PLAYING
                    else:
                        cap.stream_off()
                        cap.stream_on()
                except Exception as e:
                    print(f"[watchdog] pipeline rebuild: {e!r}", flush=True)
                cap.last_frame_ts = time.time()
                cap.need_realign.set()
                continue
            if chip_wedged and now - last_reset > 60:
                last_reset = now                   # 冷却 60s：复位后重探测/重对齐
                print(f"[watchdog] chip wedged? idle={idle:.0f}s qd={qd:.1f}s "  # 期间别再触发
                      f"-> hard reset", flush=True)
                # ★ 首选驱动内自救复位（sysfs，不 unbind）：media 链路/rkcif
                # 流/用户态 fd 全部保持，复位后帧自然回来。旧 unbind/bind 路
                # 会让 rkcif pipeline 缓存的 subdev 指针变野，下次 streamoff
                # 解引用野指针 OOPS 在持 stream_lock 上下文 → 锁泄漏 → 之后
                # open() 永久 D 态卡死只能整机重启（2026-10-30 实捕）。
                # 仅旧内核（无 sysfs 节点）或非 tc35874x 通道时回退脚本
                if subdev and os.path.exists('/sys/bus/i2c/devices/4-000f/self_reset'):
                    sh('echo 1 > /sys/bus/i2c/devices/4-000f/self_reset')
                else:
                    # ★ 必须先停流再 unbind：实测流式采集中直接 unbind
                    # tc35874x 驱动会让 rkcif/dphy 野指针内核崩溃。
                    # 芯片挂死时 stream_off 的 i2c 写逐个超时，最坏几秒
                    try:
                        cap.stream_off()
                    except Exception as e:
                        print(f"[watchdog] stream_off before reset: {e!r}",
                              flush=True)
                    sh('sh /usr/sbin/ccs_chip_reset.sh')
                cap.last_frame_ts = time.time()
                subdev = find_subdev()             # 复位重探测后节点可能变
                cap.need_realign.set()
                continue
            # 硬复位后 30s 帧仍回不来：复位路径本身可能把 gst 管线弄死
            # （2026-10-27 实测），自杀由 systemd(Restart=always) 拉起全新
            # 实例——实测服务重启即可恢复
            if last_reset and now - last_reset > 30 and idle > 25:
                print("[watchdog] no frames 30s after hard reset -> "
                      "self-restart via systemd", flush=True)
                os._exit(1)
            # 驱动轮询杀流后 rkcif 节点格式会掉回 640x480/RG10 默认：
            # 信号在、帧也照流（绿屏），信号跳变检测发现不了，必须查节点格式。
            # 无信号时不查——否则初始 640x480 格式会让对齐空转浪费重试。
            # gst 后端节点格式是 NV16（管线内 videoconvert 软转 UYVY），
            # 按后端分别校验，否则 gst 模式下这里每拍都误触发重对齐
            fmt = sho(f"v4l2-ctl -d {cap.dev} --get-fmt-video 2>/dev/null")
            want_pf = 'NV16' if cap.be_name == 'gst' else 'UYVY'
            if ok and (f"{cap.w}/{cap.h}" not in fmt or f"'{want_pf}'" not in fmt):
                cap.need_realign.set()


def usb_heal():
    """OTG gadget 枚举卡死自愈。VBUS 在（PC 线连着，extcon USB=1）但 gadget
    长时间未 configured/suspended（实测 Windows 代码43：dwc3 ep0 使能失败后
    状态卡死，简单重挂 UDC 报 failed to enable ep0out），需完整重绑定 dwc3
    控制器才能恢复。"""
    bad = 0
    while True:
        time.sleep(5)
        try:
            vbus = 'USB=1' in open('/sys/class/extcon/usb2-phy/state').read()
            st = open('/sys/class/udc/fcc00000.dwc3/state').read().strip()
        except OSError:
            continue
        bad = bad + 1 if (vbus and st not in ('configured', 'suspended')) else 0
        if bad >= 3:                     # 连续 ~15s 才动手，枚举瞬态不碰
            bad = 0
            print(f"[usb] heal: state={st} with vbus -> rebind dwc3", flush=True)
            sh('G=/sys/kernel/config/usb_gadget/ccs_kvm; '
               'echo "" > $G/UDC 2>/dev/null; sleep 1; '
               'echo fcc00000.dwc3 > /sys/bus/platform/drivers/dwc3/unbind 2>/dev/null; sleep 1; '
               'echo fcc00000.dwc3 > /sys/bus/platform/drivers/dwc3/bind 2>/dev/null; sleep 2; '
               'echo fcc00000.dwc3 > $G/UDC 2>/dev/null')


# ---------------- 串口控制台（RS232=ttyS3 / RS485=ttyS4） ----------------

UART_PORTS = {'232': '/dev/ttyS3', '485': '/dev/ttyS4'}   # 见 IO 总表：232=UART3、485=UART4
UART_BAUDS = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]


class UartPort:
    """一路串口的常驻打开 + 后台接收线程。RS485 方向由 Q10(SS8050) 硬件
    自动切换，无需 rs485 rts-gpio（IO 总表第 24 条）。接收缓冲环形 64KB，
    rx_total 是逻辑偏移，网页按 since 增量拉取。"""

    def __init__(self, dev, baud=115200):
        self.dev = dev
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.rx_total = 0
        self.err = ''
        self.fd = None
        self.baud = baud
        self._open(baud)
        threading.Thread(target=self._rx_loop, daemon=True).start()

    def _open(self, baud):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = os.open(self.dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.set_baud(baud)

    def set_baud(self, baud):
        if baud not in UART_BAUDS:
            raise ValueError('unsupported baud')
        a = termios.tcgetattr(self.fd)
        a[0] = 0                                        # iflag：raw
        a[1] = 0                                        # oflag
        a[2] = ((a[2] & ~(termios.PARENB | termios.CSTOPB | termios.CSIZE))
                | termios.CS8 | termios.CREAD | termios.CLOCAL)  # 8N1
        a[3] = 0                                        # lflag
        a[4] = a[5] = getattr(termios, f'B{baud}')      # ispeed/ospeed
        a[6][termios.VMIN] = 0
        a[6][termios.VTIME] = 1
        termios.tcsetattr(self.fd, termios.TCSANOW, a)
        self.baud = baud

    def _rx_loop(self):
        while True:
            try:
                r, _, _ = select.select([self.fd], [], [], 1.0)
                if r:
                    data = os.read(self.fd, 4096)
                    if data:
                        with self.lock:
                            self.buf += data
                            self.rx_total += len(data)
                            if len(self.buf) > 65536:
                                del self.buf[:-32768]
            except (OSError, ValueError) as e:
                self.err = repr(e)
                time.sleep(2)

    def recv_since(self, since):
        """返回 (原始字节, 新偏移)。since 落后于缓冲起点时给全量缓冲。"""
        with self.lock:
            start = self.rx_total - len(self.buf)
            if since < start:
                since = start
            return bytes(self.buf[since - start:]), self.rx_total

    def send(self, data):
        while data:
            n = os.write(self.fd, data)
            data = data[n:]


_UARTS = {}


def uart_get(port):
    """按端口名拿常驻 UartPort（首次惰性打开）"""
    u = _UARTS.get(port)
    if u is None:
        if port not in UART_PORTS:
            raise KeyError(port)
        u = _UARTS[port] = UartPort(UART_PORTS[port])
    return u


# ---------------- 网页 ----------------

CSS = """body{margin:0;background:#111;color:#eee;font-family:sans-serif}
h2{font-weight:normal;margin:10px;font-size:20px}
h3{font-weight:normal;margin:8px;font-size:16px;border-bottom:1px solid #333;padding-bottom:4px}
#st{color:#8f8;font:14px monospace}
img{width:98vw;max-width:1280px;border:1px solid #444}
a{color:#6af} .sec{max-width:1280px;margin:0 auto;text-align:left;padding:0 10px}
table{border-collapse:collapse;font:13px monospace;margin:6px 0}
td,th{border:1px solid #333;padding:3px 10px}
input,select,textarea{background:#222;color:#eee;border:1px solid #555;padding:4px;font:13px monospace}
textarea{width:98%;height:150px}
button{background:#246;color:#fff;border:0;padding:5px 14px;margin:2px;cursor:pointer}
.msg{background:#253;border:1px solid #4a6;padding:6px;margin:6px 0}
.err{background:#523;border:1px solid #a66;padding:6px;margin:6px 0}
.warn{color:#fc6;font-size:13px}
details{margin:6px 0}summary{cursor:pointer;font-size:15px;padding:4px}
#scrwrap{position:relative;display:inline-block}
#scrwrap img{display:block}
#scrwrap video{display:block;background:#000}
#vsig{position:absolute;left:0;top:0;right:0;bottom:0;display:none;align-items:center;justify-content:center;
 background:rgba(0,0,0,.85);color:#fc6;font-size:28px;letter-spacing:4px;z-index:20}
#lcur{position:absolute;left:0;top:0;width:16px;height:16px;pointer-events:none;display:none;z-index:10;filter:drop-shadow(0 0 1px #000);
 background:url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><path d="M1 0 L15 8 L9 9.5 L12 15 L9.8 16 L7 10.5 L2.5 13.5 Z" fill="white" stroke="black"/></svg>') no-repeat}"""


def esc(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


# 页面 JS：普通字符串常量（不进 f-string，大括号不用转义）
# JMuxer 2.0.4（MIT，https://github.com/samirkumardas/jmuxer）——浏览器端把
# 裸 H264 Annex-B 实时封装成 fMP4 喂 MSE，/jmuxer.js 路由提供，网页内播 H264 用
JMUXER_JS = r'''
!function(e,t){"object"==typeof exports&&"undefined"!=typeof module?module.exports=t(require("stream")):"function"==typeof define&&define.amd?define(["stream"],t):(e="undefined"!=typeof globalThis?globalThis:e||self).JMuxer=t(e.stream)}(this,(function(e){"use strict";function t(e){return t="function"==typeof Symbol&&"symbol"==typeof Symbol.iterator?function(e){return typeof e}:function(e){return e&&"function"==typeof Symbol&&e.constructor===Symbol&&e!==Symbol.prototype?"symbol":typeof e},t(e)}function n(e,t){if(!(e instanceof t))throw new TypeError("Cannot call a class as a function")}function r(e,t){for(var n=0;n<t.length;n++){var r=t[n];r.enumerable=r.enumerable||!1,r.configurable=!0,"value"in r&&(r.writable=!0),Object.defineProperty(e,r.key,r)}}function i(e,t,n){return t&&r(e.prototype,t),n&&r(e,n),e}function a(e,t,n){return t in e?Object.defineProperty(e,t,{value:n,enumerable:!0,configurable:!0,writable:!0}):e[t]=n,e}function s(e,t){if("function"!=typeof t&&null!==t)throw new TypeError("Super expression must either be null or a function");e.prototype=Object.create(t&&t.prototype,{constructor:{value:e,writable:!0,configurable:!0}}),t&&u(e,t)}function o(e){return o=Object.setPrototypeOf?Object.getPrototypeOf:function(e){return e.__proto__||Object.getPrototypeOf(e)},o(e)}function u(e,t){return u=Object.setPrototypeOf||function(e,t){return e.__proto__=t,e},u(e,t)}function c(e){if(void 0===e)throw new ReferenceError("this hasn't been initialised - super() hasn't been called");return e}function f(e,t){if(t&&("object"==typeof t||"function"==typeof t))return t;if(void 0!==t)throw new TypeError("Derived constructors may only return object or undefined");return c(e)}function l(e){var t=function(){if("undefined"==typeof Reflect||!Reflect.construct)return!1;if(Reflect.construct.sham)return!1;if("function"==typeof Proxy)return!0;try{return Boolean.prototype.valueOf.call(Reflect.construct(Boolean,[],(function(){}))),!0}catch(e){return!1}}();return function(){var n,r=o(e);if(t){var i=o(this).constructor;n=Reflect.construct(r,arguments,i)}else n=r.apply(this,arguments);return f(this,n)}}function h(e,t){return function(e){if(Array.isArray(e))return e}(e)||function(e,t){var n=null==e?null:"undefined"!=typeof Symbol&&e[Symbol.iterator]||e["@@iterator"];if(null==n)return;var r,i,a=[],s=!0,o=!1;try{for(n=n.call(e);!(s=(r=n.next()).done)&&(a.push(r.value),!t||a.length!==t);s=!0);}catch(e){o=!0,i=e}finally{try{s||null==n.return||n.return()}finally{if(o)throw i}}return a}(e,t)||d(e,t)||function(){throw new TypeError("Invalid attempt to destructure non-iterable instance.\nIn order to be iterable, non-array objects must have a [Symbol.iterator]() method.")}()}function d(e,t){if(e){if("string"==typeof e)return y(e,t);var n=Object.prototype.toString.call(e).slice(8,-1);return"Object"===n&&e.constructor&&(n=e.constructor.name),"Map"===n||"Set"===n?Array.from(e):"Arguments"===n||/^(?:Ui|I)nt(?:8|16|32)(?:Clamped)?Array$/.test(n)?y(e,t):void 0}}function y(e,t){(null==t||t>e.length)&&(t=e.length);for(var n=0,r=new Array(t);n<t;n++)r[n]=e[n];return r}function p(e,t){var n="undefined"!=typeof Symbol&&e[Symbol.iterator]||e["@@iterator"];if(!n){if(Array.isArray(e)||(n=d(e))||t&&e&&"number"==typeof e.length){n&&(e=n);var r=0,i=function(){};return{s:i,n:function(){return r>=e.length?{done:!0}:{done:!1,value:e[r++]}},e:function(e){throw e},f:i}}throw new TypeError("Invalid attempt to iterate non-iterable instance.\nIn order to be iterable, non-array objects must have a [Symbol.iterator]() method.")}var a,s=!0,o=!1;return{s:function(){n=n.call(e)},n:function(){var e=n.next();return s=e.done,e},e:function(e){o=!0,a=e},f:function(){try{s||null==n.return||n.return()}finally{if(o)throw a}}}}var v,m;function k(e){if(v){for(var t=arguments.length,n=new Array(t>1?t-1:0),r=1;r<t;r++)n[r-1]=arguments[r];v.apply(void 0,[e].concat(n))}}function g(e){if(m){for(var t=arguments.length,n=new Array(t>1?t-1:0),r=1;r<t;r++)n[r-1]=arguments[r];m.apply(void 0,[e].concat(n))}}var b=function(){function e(t){n(this,e),this.payload=t,this.nri=(96&this.payload[0])>>5,this.ntype=31&this.payload[0],this.isvcl=1==this.ntype||5==this.ntype,this.stype="",this.isfmb=!1}return i(e,[{key:"toString",value:function(){return"".concat(e.type(this),": NRI: ").concat(this.getNri())}},{key:"getNri",value:function(){return this.nri}},{key:"type",value:function(){return this.ntype}},{key:"isKeyframe",value:function(){return this.ntype===e.IDR}},{key:"getPayload",value:function(){return this.payload}},{key:"getPayloadSize",value:function(){return this.payload.byteLength}},{key:"getSize",value:function(){return 4+this.getPayloadSize()}},{key:"getData",value:function(){var e=new Uint8Array(this.getSize());return new DataView(e.buffer).setUint32(0,this.getSize()-4),e.set(this.getPayload(),4),e}}],[{key:"NDR",get:function(){return 1}},{key:"IDR",get:function(){return 5}},{key:"SEI",get:function(){return 6}},{key:"SPS",get:function(){return 7}},{key:"PPS",get:function(){return 8}},{key:"AUD",get:function(){return 9}},{key:"TYPES",get:function(){var t;return a(t={},e.IDR,"IDR"),a(t,e.SEI,"SEI"),a(t,e.SPS,"SPS"),a(t,e.PPS,"PPS"),a(t,e.NDR,"NDR"),a(t,e.AUD,"AUD"),t}},{key:"type",value:function(t){return t.ntype in e.TYPES?e.TYPES[t.ntype]:"UNKNOWN"}}]),e}();function S(e,t){var n=new Uint8Array((0|e.byteLength)+(0|t.byteLength));return n.set(e,0),n.set(t,0|e.byteLength),n}var w,x=function(){function e(t){n(this,e),this.data=t,this.index=0,this.bitLength=8*t.byteLength}return i(e,[{key:"setData",value:function(e){this.data=e,this.index=0,this.bitLength=8*e.byteLength}},{key:"bitsAvailable",get:function(){return this.bitLength-this.index}},{key:"skipBits",value:function(e){if(this.bitsAvailable<e)return!1;this.index+=e}},{key:"readBits",value:function(e){var t=!(arguments.length>1&&void 0!==arguments[1])||arguments[1],n=this.getBits(e,this.index,t);return n}},{key:"getBits",value:function(e,t){var n=!(arguments.length>2&&void 0!==arguments[2])||arguments[2];if(this.bitsAvailable<e)return 0;var r=t%8,i=this.data[t/8|0]&255>>>r,a=8-r;if(a>=e)return n&&(this.index+=e),i>>a-e;n&&(this.index+=a);var s=e-a;return i<<s|this.getBits(s,t+a,n)}},{key:"skipLZ",value:function(){var e;for(e=0;e<this.bitLength-this.index;++e)if(0!==this.getBits(1,this.index+e,!1))return this.index+=e,e;return e}},{key:"skipUEG",value:function(){this.skipBits(1+this.skipLZ())}},{key:"skipEG",value:function(){this.skipBits(1+this.skipLZ())}},{key:"readUEG",value:function(){var e=this.skipLZ();return this.readBits(e+1)-1}},{key:"readEG",value:function(){var e=this.readUEG();return 1&e?1+e>>>1:-1*(e>>>1)}},{key:"readBoolean",value:function(){return 1===this.readBits(1)}},{key:"readUByte",value:function(){var e=arguments.length>0&&void 0!==arguments[0]?arguments[0]:1;return this.readBits(8*e)}},{key:"readUShort",value:function(){return this.readBits(16)}},{key:"readUInt",value:function(){return this.readBits(32)}}]),e}(),A=function(){function e(t){n(this,e),this.remuxer=t,this.track=t.mp4track}return i(e,[{key:"parseSPS",value:function(t){var n=e.readSPS(new Uint8Array(t));this.track.fps=n.fps,this.track.width=n.width,this.track.height=n.height,this.track.sps=[new Uint8Array(t)],this.track.codec="avc1.";for(var r=new DataView(t.buffer,t.byteOffset+1,4),i=0;i<3;++i){var a=r.getUint8(i).toString(16);a.length<2&&(a="0"+a),this.track.codec+=a}}},{key:"parsePPS",value:function(e){this.track.pps=[new Uint8Array(e)]}},{key:"parseNAL",value:function(e){if(!e)return!1;var t=!1;switch(e.type()){case b.IDR:case b.NDR:t=!0;break;case b.PPS:this.track.pps||(this.parsePPS(e.getPayload()),!this.remuxer.readyToDecode&&this.track.pps&&this.track.sps&&(this.remuxer.readyToDecode=!0)),t=!0;break;case b.SPS:this.track.sps||(this.parseSPS(e.getPayload()),!this.remuxer.readyToDecode&&this.track.pps&&this.track.sps&&(this.remuxer.readyToDecode=!0)),t=!0;break;case b.AUD:k("AUD - ignoing");break;case b.SEI:k("SEI - ignoing")}return t}}],[{key:"extractNALu",value:function(e){for(var t,n,r=0,i=e.byteLength,a=0,s=[],o=0;r<i;)switch(t=e[r++],a){case 0:0===t&&(a=1);break;case 1:a=0===t?2:0;break;case 2:case 3:0===t?a=3:1===t&&r<i?(o!=r-a-1&&s.push(e.subarray(o,r-a-1)),o=r,a=0):a=0}return o<i&&(n=e.subarray(o,i)),[s,n]}},{key:"skipScalingList",value:function(e,t){for(var n=8,r=8,i=0;i<t;i++)0!==r&&(r=(n+e.readEG()+256)%256),n=0===r?n:r}},{key:"readSPS",value:function(t){var n,r,i,a,s,o,u=new x(t),c=0,f=0,l=0,h=0,d=1,y=0;u.readUByte();for(var p=[],v=t.byteLength,m=1;m<v;m++)m+2<v&&3===u.readBits(24,!1)?(p.push(u.readBits(8)),p.push(u.readBits(8)),m+=2,u.readBits(8)):p.push(u.readBits(8));if(u.setData(new Uint8Array(p)),n=u.readUByte(),u.readBits(5),u.skipBits(3),u.readUByte(),u.skipUEG(),100===n||110===n||122===n||244===n||44===n||83===n||86===n||118===n||128===n){var k=u.readUEG();if(3===k&&u.skipBits(1),u.skipUEG(),u.skipUEG(),u.skipBits(1),u.readBoolean()){o=3!==k?8:12;for(var g=0;g<o;++g)u.readBoolean()&&(g<6?e.skipScalingList(u,16):e.skipScalingList(u,64))}}u.skipUEG();var b=u.readUEG();if(0===b)u.readUEG();else if(1===b){u.skipBits(1),u.skipEG(),u.skipEG(),r=u.readUEG();for(var S=0;S<r;++S)u.skipEG()}if(u.skipUEG(),u.skipBits(1),i=u.readUEG(),a=u.readUEG(),0===(s=u.readBits(1))&&u.skipBits(1),u.skipBits(1),u.readBoolean()&&(c=u.readUEG(),f=u.readUEG(),l=u.readUEG(),h=u.readUEG()),u.readBoolean()){if(u.readBoolean()){var w;switch(u.readUByte()){case 1:w=[1,1];break;case 2:w=[12,11];break;case 3:w=[10,11];break;case 4:w=[16,11];break;case 5:w=[40,33];break;case 6:w=[24,11];break;case 7:w=[20,11];break;case 8:w=[32,11];break;case 9:w=[80,33];break;case 10:w=[18,11];break;case 11:w=[15,11];break;case 12:w=[64,33];break;case 13:w=[160,99];break;case 14:w=[4,3];break;case 15:w=[3,2];break;case 16:w=[2,1];break;case 255:w=[u.readUByte()<<8|u.readUByte(),u.readUByte()<<8|u.readUByte()]}w&&w[0]>0&&w[1]>0&&(d=w[0]/w[1])}if(u.readBoolean()&&u.skipBits(1),u.readBoolean()&&(u.skipBits(4),u.readBoolean()&&u.skipBits(24)),u.readBoolean()&&(u.skipUEG(),u.skipUEG()),u.readBoolean()){var A=u.readUInt(),U=u.readUInt();u.readBoolean()&&(y=U/(2*A))}}return{fps:y>0?y:void 0,width:Math.ceil((16*(i+1)-2*c-2*f)*d),height:(2-s)*(a+1)*16-(s?2:4)*(l+h)}}},{key:"parseHeader",value:function(e){var t=new x(e.getPayload());t.readUByte(),e.isfmb=0===t.readUEG(),e.stype=t.readUEG()}}]),e}(),U=function(){function e(t){n(this,e),this.remuxer=t,this.track=t.mp4track}return i(e,[{key:"setAACConfig",value:function(){var t,n,r,i=new Uint8Array(2),a=e.getAACHeaderData;a&&(t=1+((192&a[2])>>>6),n=(60&a[2])>>>2,r=(1&a[2])<<2,r|=(192&a[3])>>>6,i[0]=t<<3,i[0]|=(14&n)>>1,i[1]|=(1&n)<<7,i[1]|=r<<3,this.track.codec="mp4a.40."+t,this.track.channelCount=r,this.track.config=i,this.remuxer.readyToDecode=!0)}}],[{key:"samplingRateMap",get:function(){return[96e3,88200,64e3,48e3,44100,32e3,24e3,22050,16e3,12e3,11025,8e3,7350]}},{key:"getAACHeaderData",get:function(){return w}},{key:"getHeaderLength",value:function(e){return 1&e[1]?7:9}},{key:"getFrameLength",value:function(e){return(3&e[3])<<11|e[4]<<3|(224&e[5])>>>5}},{key:"isAACPattern",value:function(e){return 255===e[0]&&240==(240&e[1])&&0==(6&e[1])}},{key:"extractAAC",value:function(t){var n,r,i=0,a=t.byteLength,s=[];if(!e.isAACPattern(t))return g("Invalid ADTS audio format"),s;for(n=e.getHeaderLength(t),w||(w=t.subarray(0,n));i<a;)r=e.getFrameLength(t),s.push(t.subarray(n,r)),t=t.slice(r),i+=r;return s}}]),e}(),B=function(){function e(t){n(this,e),this.listener={},this.type=""|t}return i(e,[{key:"on",value:function(e,t){return this.listener[e]||(this.listener[e]=[]),this.listener[e].push(t),!0}},{key:"off",value:function(e,t){if(this.listener[e]){var n=this.listener[e].indexOf(t);return n>-1&&this.listener[e].splice(n,1),!0}return!1}},{key:"offAll",value:function(){this.listener={}}},{key:"dispatch",value:function(e,t){return!!this.listener[e]&&(this.listener[e].map((function(e){e.apply(null,[t])})),!0)}}]),e}(),D=function(){function e(){n(this,e)}return i(e,null,[{key:"init",value:function(){var t;for(t in e.types={avc1:[],avcC:[],btrt:[],dinf:[],dref:[],esds:[],ftyp:[],hdlr:[],mdat:[],mdhd:[],mdia:[],mfhd:[],minf:[],moof:[],moov:[],mp4a:[],mvex:[],mvhd:[],sdtp:[],stbl:[],stco:[],stsc:[],stsd:[],stsz:[],stts:[],tfdt:[],tfhd:[],traf:[],trak:[],trun:[],trex:[],tkhd:[],vmhd:[],smhd:[]},e.types)e.types.hasOwnProperty(t)&&(e.types[t]=[t.charCodeAt(0),t.charCodeAt(1),t.charCodeAt(2),t.charCodeAt(3)]);var n=new Uint8Array([0,0,0,0,0,0,0,0,118,105,100,101,0,0,0,0,0,0,0,0,0,0,0,0,86,105,100,101,111,72,97,110,100,108,101,114,0]),r=new Uint8Array([0,0,0,0,0,0,0,0,115,111,117,110,0,0,0,0,0,0,0,0,0,0,0,0,83,111,117,110,100,72,97,110,100,108,101,114,0]);e.HDLR_TYPES={video:n,audio:r};var i=new Uint8Array([0,0,0,0,0,0,0,1,0,0,0,12,117,114,108,32,0,0,0,1]),a=new Uint8Array([0,0,0,0,0,0,0,0]);e.STTS=e.STSC=e.STCO=a,e.STSZ=new Uint8Array([0,0,0,0,0,0,0,0,0,0,0,0]),e.VMHD=new Uint8Array([0,0,0,1,0,0,0,0,0,0,0,0]),e.SMHD=new Uint8Array([0,0,0,0,0,0,0,0]),e.STSD=new Uint8Array([0,0,0,0,0,0,0,1]);var s=new Uint8Array([105,115,111,109]),o=new Uint8Array([97,118,99,49]),u=new Uint8Array([0,0,0,1]);e.FTYP=e.box(e.types.ftyp,s,u,s,o),e.DINF=e.box(e.types.dinf,e.box(e.types.dref,i))}},{key:"box",value:function(e){for(var t=arguments.length,n=new Array(t>1?t-1:0),r=1;r<t;r++)n[r-1]=arguments[r];for(var i,a=8,s=n.length,o=s;s--;)a+=n[s].byteLength;for((i=new Uint8Array(a))[0]=a>>24&255,i[1]=a>>16&255,i[2]=a>>8&255,i[3]=255&a,i.set(e,4),s=0,a=8;s<o;++s)i.set(n[s],a),a+=n[s].byteLength;return i}},{key:"hdlr",value:function(t){return e.box(e.types.hdlr,e.HDLR_TYPES[t])}},{key:"mdat",value:function(t){return e.box(e.types.mdat,t)}},{key:"mdhd",value:function(t,n){return e.box(e.types.mdhd,new Uint8Array([0,0,0,0,0,0,0,2,0,0,0,3,t>>24&255,t>>16&255,t>>8&255,255&t,n>>24,n>>16&255,n>>8&255,255&n,85,196,0,0]))}},{key:"mdia",value:function(t){return e.box(e.types.mdia,e.mdhd(t.timescale,t.duration),e.hdlr(t.type),e.minf(t))}},{key:"mfhd",value:function(t){return e.box(e.types.mfhd,new Uint8Array([0,0,0,0,t>>24,t>>16&255,t>>8&255,255&t]))}},{key:"minf",value:function(t){return"audio"===t.type?e.box(e.types.minf,e.box(e.types.smhd,e.SMHD),e.DINF,e.stbl(t)):e.box(e.types.minf,e.box(e.types.vmhd,e.VMHD),e.DINF,e.stbl(t))}},{key:"moof",value:function(t,n,r){return e.box(e.types.moof,e.mfhd(t),e.traf(r,n))}},{key:"moov",value:function(t,n,r){for(var i=t.length,a=[];i--;)a[i]=e.trak(t[i]);return e.box.apply(null,[e.types.moov,e.mvhd(r,n)].concat(a).concat(e.mvex(t)))}},{key:"mvex",value:function(t){for(var n=t.length,r=[];n--;)r[n]=e.trex(t[n]);return e.box.apply(null,[e.types.mvex].concat(r))}},{key:"mvhd",value:function(t,n){var r=new Uint8Array([0,0,0,0,0,0,0,1,0,0,0,2,t>>24&255,t>>16&255,t>>8&255,255&t,n>>24&255,n>>16&255,n>>8&255,255&n,0,1,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,64,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,255,255,255,255]);return e.box(e.types.mvhd,r)}},{key:"sdtp",value:function(t){var n,r,i=t.samples||[],a=new Uint8Array(4+i.length);for(r=0;r<i.length;r++)n=i[r].flags,a[r+4]=n.dependsOn<<4|n.isDependedOn<<2|n.hasRedundancy;return e.box(e.types.sdtp,a)}},{key:"stbl",value:function(t){return e.box(e.types.stbl,e.stsd(t),e.box(e.types.stts,e.STTS),e.box(e.types.stsc,e.STSC),e.box(e.types.stsz,e.STSZ),e.box(e.types.stco,e.STCO))}},{key:"avc1",value:function(t){var n,r,i,a=[],s=[];for(n=0;n<t.sps.length;n++)i=(r=t.sps[n]).byteLength,a.push(i>>>8&255),a.push(255&i),a=a.concat(Array.prototype.slice.call(r));for(n=0;n<t.pps.length;n++)i=(r=t.pps[n]).byteLength,s.push(i>>>8&255),s.push(255&i),s=s.concat(Array.prototype.slice.call(r));var o=e.box(e.types.avcC,new Uint8Array([1,a[3],a[4],a[5],255,224|t.sps.length].concat(a).concat([t.pps.length]).concat(s))),u=t.width,c=t.height;return e.box(e.types.avc1,new Uint8Array([0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,u>>8&255,255&u,c>>8&255,255&c,0,72,0,0,0,72,0,0,0,0,0,0,0,1,18,98,105,110,101,108,112,114,111,46,114,117,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,24,17,17]),o,e.box(e.types.btrt,new Uint8Array([0,28,156,128,0,45,198,192,0,45,198,192])))}},{key:"esds",value:function(e){var t=e.config.byteLength,n=new Uint8Array(26+t+3);return n.set([0,0,0,0,3,23+t,0,1,0,4,15+t,64,21,0,0,0,0,0,0,0,0,0,0,0,5,t]),n.set(e.config,26),n.set([6,1,2],26+t),n}},{key:"mp4a",value:function(t){var n=t.audiosamplerate;return e.box(e.types.mp4a,new Uint8Array([0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,0,0,t.channelCount,0,16,0,0,0,0,n>>8&255,255&n,0,0]),e.box(e.types.esds,e.esds(t)))}},{key:"stsd",value:function(t){return"audio"===t.type?e.box(e.types.stsd,e.STSD,e.mp4a(t)):e.box(e.types.stsd,e.STSD,e.avc1(t))}},{key:"tkhd",value:function(t){var n=t.id,r=t.duration,i=t.width,a=t.height,s=t.volume;return e.box(e.types.tkhd,new Uint8Array([0,0,0,7,0,0,0,0,0,0,0,0,n>>24&255,n>>16&255,n>>8&255,255&n,0,0,0,0,r>>24,r>>16&255,r>>8&255,255&r,0,0,0,0,0,0,0,0,0,0,0,0,s>>0&255,s%1*10>>0&255,0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,64,0,0,0,i>>8&255,255&i,0,0,a>>8&255,255&a,0,0]))}},{key:"traf",value:function(t,n){var r=e.sdtp(t),i=t.id;return e.box(e.types.traf,e.box(e.types.tfhd,new Uint8Array([0,0,0,0,i>>24,i>>16&255,i>>8&255,255&i])),e.box(e.types.tfdt,new Uint8Array([0,0,0,0,n>>24,n>>16&255,n>>8&255,255&n])),e.trun(t,r.length+16+16+8+16+8+8),r)}},{key:"trak",value:function(t){return t.duration=t.duration||4294967295,e.box(e.types.trak,e.tkhd(t),e.mdia(t))}},{key:"trex",value:function(t){var n=t.id;return e.box(e.types.trex,new Uint8Array([0,0,0,0,n>>24,n>>16&255,n>>8&255,255&n,0,0,0,1,0,0,0,0,0,0,0,0,0,1,0,1]))}},{key:"trun",value:function(t,n){var r,i,a,s,o,u,c=t.samples||[],f=c.length,l=12+16*f,h=new Uint8Array(l);for(n+=8+l,h.set([0,0,15,1,f>>>24&255,f>>>16&255,f>>>8&255,255&f,n>>>24&255,n>>>16&255,n>>>8&255,255&n],0),r=0;r<f;r++)a=(i=c[r]).duration,s=i.size,o=i.flags,u=i.cts,h.set([a>>>24&255,a>>>16&255,a>>>8&255,255&a,s>>>24&255,s>>>16&255,s>>>8&255,255&s,o.isLeading<<2|o.dependsOn,o.isDependedOn<<6|o.hasRedundancy<<4|o.paddingValue<<1|o.isNonSync,61440&o.degradPrio,15&o.degradPrio,u>>>24&255,u>>>16&255,u>>>8&255,255&u],12+16*r);return e.box(e.types.trun,h)}},{key:"initSegment",value:function(t,n,r){e.types||e.init();var i,a=e.moov(t,n,r);return(i=new Uint8Array(e.FTYP.byteLength+a.byteLength)).set(e.FTYP),i.set(a,e.FTYP.byteLength),i}}]),e}(),C=1,E=function(){function e(){n(this,e)}return i(e,[{key:"flush",value:function(){this.mp4track.len=0,this.mp4track.samples=[]}},{key:"isReady",value:function(){return!(!this.readyToDecode||!this.samples.length)||null}}],[{key:"getTrackID",value:function(){return C++}}]),e}(),T=function(e){s(r,e);var t=l(r);function r(e){var i;return n(this,r),(i=t.call(this)).readyToDecode=!1,i.nextDts=0,i.dts=0,i.mp4track={id:E.getTrackID(),type:"audio",channelCount:0,len:0,fragmented:!0,timescale:e,duration:e,samples:[],config:"",codec:""},i.samples=[],i.aac=new U(c(i)),i}return i(r,[{key:"resetTrack",value:function(){this.readyToDecode=!1,this.mp4track.codec="",this.mp4track.channelCount="",this.mp4track.config="",this.mp4track.timescale=this.timescale,this.nextDts=0,this.dts=0}},{key:"remux",value:function(e){if(e.length>0)for(var t=0;t<e.length;t++){var n=e[t],r=n.units,i=r.byteLength;this.samples.push({units:r,size:i,duration:n.duration}),this.mp4track.len+=i,this.readyToDecode||this.aac.setAACConfig()}}},{key:"getPayload",value:function(){if(!this.isReady())return null;var e,t,n=new Uint8Array(this.mp4track.len),r=0,i=this.mp4track.samples;for(this.dts=this.nextDts;this.samples.length;){var a=this.samples.shift();a.units,(t=a.duration)<=0?(k("remuxer: invalid sample duration at DTS: ".concat(this.nextDts," :").concat(t)),this.mp4track.len-=a.size):(this.nextDts+=t,e={size:a.size,duration:t,cts:0,flags:{isLeading:0,isDependedOn:0,hasRedundancy:0,degradPrio:0,dependsOn:1}},n.set(a.units,r),r+=a.size,i.push(e))}return i.length?new Uint8Array(n.buffer,0,this.mp4track.len):null}}]),r}(E),P=function(e){s(r,e);var t=l(r);function r(e){var i;return n(this,r),(i=t.call(this)).readyToDecode=!1,i.nextDts=0,i.dts=0,i.mp4track={id:E.getTrackID(),type:"video",len:0,fragmented:!0,sps:"",pps:"",fps:30,width:0,height:0,timescale:e,duration:e,samples:[]},i.samples=[],i.h264=new A(c(i)),i}return i(r,[{key:"resetTrack",value:function(){this.readyToDecode=!1,this.mp4track.sps="",this.mp4track.pps="",this.nextDts=0,this.dts=0}},{key:"remux",value:function(e){var t,n=p(e);try{for(n.s();!(t=n.n()).done;){var r,i=t.value,a=[],s=0,o=p(i.units);try{for(o.s();!(r=o.n()).done;){var u=r.value;this.h264.parseNAL(u)&&(a.push(u),s+=u.getSize())}}catch(e){o.e(e)}finally{o.f()}a.length>0&&this.readyToDecode&&(this.mp4track.len+=s,this.samples.push({units:a,size:s,keyFrame:i.keyFrame,duration:i.duration}))}}catch(e){n.e(e)}finally{n.f()}}},{key:"getPayload",value:function(){if(!this.isReady())return null;var e,t,n=new Uint8Array(this.mp4track.len),r=0,i=this.mp4track.samples;for(this.dts=this.nextDts;this.samples.length;){var a=this.samples.shift(),s=a.units;if((t=a.duration)<=0)k("remuxer: invalid sample duration at DTS: ".concat(this.nextDts," :").concat(t)),this.mp4track.len-=a.size;else{this.nextDts+=t,e={size:a.size,duration:t,cts:0,flags:{isLeading:0,isDependedOn:0,hasRedundancy:0,degradPrio:0,isNonSync:a.keyFrame?0:1,dependsOn:a.keyFrame?2:1}};var o,u=p(s);try{for(u.s();!(o=u.n()).done;){var c=o.value;n.set(c.getData(),r),r+=c.getSize()}}catch(e){u.e(e)}finally{u.f()}i.push(e)}}return i.length?new Uint8Array(n.buffer,0,this.mp4track.len):null}}]),r}(E),L=function(e){s(r,e);var t=l(r);function r(e){var i;return n(this,r),(i=t.call(this,"remuxer")).initialized=!1,i.trackTypes=[],i.tracks={},i.seq=1,i.env=e,i.timescale=1e3,i.mediaDuration=0,i}return i(r,[{key:"addTrack",value:function(e){"video"!==e&&"both"!==e||(this.tracks.video=new P(this.timescale),this.trackTypes.push("video")),"audio"!==e&&"both"!==e||(this.tracks.audio=new T(this.timescale),this.trackTypes.push("audio"))}},{key:"reset",value:function(){var e,t=p(this.trackTypes);try{for(t.s();!(e=t.n()).done;){var n=e.value;this.tracks[n].resetTrack()}}catch(e){t.e(e)}finally{t.f()}this.initialized=!1}},{key:"destroy",value:function(){this.tracks={},this.offAll()}},{key:"flush",value:function(){if(this.initialized){var e,t=p(this.trackTypes);try{for(t.s();!(e=t.n()).done;){var n=e.value,r=this.tracks[n],i=r.getPayload();if(i&&i.byteLength){var a={type:n,payload:S(D.moof(this.seq,r.dts,r.mp4track),D.mdat(i)),dts:r.dts};"video"===n&&(a.fps=r.mp4track.fps),this.dispatch("buffer",a);var s=(o=r.dts/this.timescale,u=void 0,c=void 0,f=void 0,l=void 0,l="",u=Math.floor(o),(c=parseInt(u/3600,10)%24)>0&&(l+=(c<10?"0"+c:c)+":"),l+=((f=parseInt(u/60,10)%60)<10?"0"+f:f)+":"+((u=u<0?0:u%60)<10?"0"+u:u));k("put segment (".concat(n,"): dts: ").concat(r.dts," frames: ").concat(r.mp4track.samples.length," second: ").concat(s)),r.flush(),this.seq++}}}catch(e){t.e(e)}finally{t.f()}}else this.isReady()&&(this.dispatch("ready"),this.initSegment(),this.initialized=!0,this.flush());var o,u,c,f,l}},{key:"initSegment",value:function(){var e,t=[],n=p(this.trackTypes);try{for(n.s();!(e=n.n()).done;){var r=e.value,i=this.tracks[r];if("browser"==this.env){var a={type:r,payload:D.initSegment([i.mp4track],this.mediaDuration,this.timescale)};this.dispatch("buffer",a)}else t.push(i.mp4track)}}catch(e){n.e(e)}finally{n.f()}if("node"==this.env){var s={type:"all",payload:D.initSegment(t,this.mediaDuration,this.timescale)};this.dispatch("buffer",s)}k("Initial segment generated.")}},{key:"isReady",value:function(){var e,t=p(this.trackTypes);try{for(t.s();!(e=t.n()).done;){var n=e.value;if(!this.tracks[n].readyToDecode||!this.tracks[n].samples.length)return!1}}catch(e){t.e(e)}finally{t.f()}return!0}},{key:"remux",value:function(e){var t,n=p(this.trackTypes);try{for(n.s();!(t=n.n()).done;){var r=t.value,i=e[r];"audio"===r&&this.tracks.video&&!this.tracks.video.readyToDecode||i.length>0&&this.tracks[r].remux(i)}}catch(e){n.e(e)}finally{n.f()}this.flush()}}]),r}(B),R=function(e){s(r,e);var t=l(r);function r(e,i){var a;return n(this,r),(a=t.call(this,"buffer")).type=i,a.queue=new Uint8Array,a.cleaning=!1,a.pendingCleaning=0,a.cleanOffset=30,a.cleanRanges=[],a.sourceBuffer=e,a.sourceBuffer.addEventListener("updateend",(function(){a.pendingCleaning>0&&(a.initCleanup(a.pendingCleaning),a.pendingCleaning=0),a.cleaning=!1,a.cleanRanges.length&&a.doCleanup()})),a.sourceBuffer.addEventListener("error",(function(){a.dispatch("error",{type:a.type,name:"buffer",error:"buffer error"})})),a}return i(r,[{key:"destroy",value:function(){this.queue=null,this.sourceBuffer=null,this.offAll()}},{key:"doCleanup",value:function(){if(this.cleanRanges.length){var e=this.cleanRanges.shift();k("".concat(this.type," remove range [").concat(e[0]," - ").concat(e[1],")")),this.cleaning=!0,this.sourceBuffer.remove(e[0],e[1])}else this.cleaning=!1}},{key:"initCleanup",value:function(e){try{if(this.sourceBuffer.updating)return void(this.pendingCleaning=e);if(this.sourceBuffer.buffered&&this.sourceBuffer.buffered.length&&!this.cleaning){for(var t=0;t<this.sourceBuffer.buffered.length;++t){var n=this.sourceBuffer.buffered.start(t),r=this.sourceBuffer.buffered.end(t);e-n>this.cleanOffset&&n<(r=e-this.cleanOffset)&&this.cleanRanges.push([n,r])}this.doCleanup()}}catch(e){g("Error occured while cleaning ".concat(this.type," buffer - ").concat(e.name,": ").concat(e.message))}}},{key:"doAppend",value:function(){if(this.queue.length&&this.sourceBuffer&&!this.sourceBuffer.updating)try{this.sourceBuffer.appendBuffer(this.queue),this.queue=new Uint8Array}catch(t){var e="unexpectedError";"QuotaExceededError"===t.name?(k("".concat(this.type," buffer quota full")),e="QuotaExceeded"):(g("Error occured while appending ".concat(this.type," buffer - ").concat(t.name,": ").concat(t.message)),e="InvalidStateError"),this.dispatch("error",{type:this.type,name:e,error:"buffer error"})}}},{key:"feed",value:function(e){this.queue=S(this.queue,e)}}]),r}(B);return function(r){s(o,r);var a=l(o);function o(e){var r;n(this,o),(r=a.call(this,"jmuxer")).isReset=!1;return r.options=Object.assign({},{node:"",mode:"both",flushingTime:500,maxDelay:500,clearBuffer:!0,fps:30,readFpsFromTrack:!1,debug:!1,onReady:function(){},onError:function(){}},e),r.env="object"===("undefined"==typeof process?"undefined":t(process))&&"undefined"==typeof window?"node":"browser",r.options.debug&&(v=console.log,m=console.error),r.options.fps||(r.options.fps=30),r.frameDuration=1e3/r.options.fps|0,r.remuxController=new L(r.env),r.remuxController.addTrack(r.options.mode),r.initData(),r.remuxController.on("buffer",r.onBuffer.bind(c(r))),"browser"==r.env&&(r.remuxController.on("ready",r.createBuffer.bind(c(r))),r.initBrowser()),r}return i(o,[{key:"initData",value:function(){this.lastCleaningTime=Date.now(),this.kfPosition=[],this.kfCounter=0,this.pendingUnits={},this.remainingData=new Uint8Array,this.startInterval()}},{key:"initBrowser",value:function(){"string"==typeof this.options.node&&""==this.options.node&&g("no video element were found to render, provide a valid video element"),this.node="string"==typeof this.options.node?document.getElementById(this.options.node):this.options.node,this.mseReady=!1,this.setupMSE()}},{key:"createStream",value:function(){var t=this.feed.bind(this),n=this.destroy.bind(this);return this.stream=new e.Duplex({writableObjectMode:!0,read:function(e){},write:function(e,n,r){t(e),r()},final:function(e){n(),e()}}),this.stream}},{key:"setupMSE",value:function(){if(window.MediaSource=window.MediaSource||window.WebKitMediaSource,!window.MediaSource)throw"Oops! Browser does not support media source extension.";this.isMSESupported=!!window.MediaSource,this.mediaSource=new MediaSource,this.url=URL.createObjectURL(this.mediaSource),this.node.src=this.url,this.mseEnded=!1,this.mediaSource.addEventListener("sourceopen",this.onMSEOpen.bind(this)),this.mediaSource.addEventListener("sourceclose",this.onMSEClose.bind(this)),this.mediaSource.addEventListener("webkitsourceopen",this.onMSEOpen.bind(this)),this.mediaSource.addEventListener("webkitsourceclose",this.onMSEClose.bind(this))}},{key:"endMSE",value:function(){if(!this.mseEnded)try{this.mseEnded=!0,this.mediaSource.endOfStream()}catch(e){g("mediasource is not available to end")}}},{key:"feed",value:function(e){var t,n,r,i=!1,a={video:[],audio:[]};if(e&&this.remuxController){if(r=e.duration?parseInt(e.duration):0,e.video){e.video=S(this.remainingData,e.video);var s=h(A.extractNALu(e.video),2);if(t=s[0],n=s[1],this.remainingData=n||new Uint8Array,!(t.length>0))return void g("Failed to extract any NAL units from video data:",n);a.video=this.getVideoFrames(t,r),i=!0}if(e.audio){if(!((t=U.extractAAC(e.audio)).length>0))return void g("Failed to extract audio data from:",e.audio);a.audio=this.getAudioFrames(t,r),i=!0}i?this.remuxController.remux(a):g("Input object must have video and/or audio property. Make sure it is a valid typed array")}}},{key:"getVideoFrames",value:function(e,t){var n,r=this,i=[],a=[],s=0,o=!1,u=!1;this.pendingUnits.units&&(i=this.pendingUnits.units,u=this.pendingUnits.vcl,o=this.pendingUnits.keyFrame,this.pendingUnits={});var c,f=p(e);try{for(f.s();!(c=f.n()).done;){var l=c.value,h=new b(l);h.type()!==b.IDR&&h.type()!==b.NDR||A.parseHeader(h),i.length&&u&&(h.isfmb||!h.isvcl)&&(a.push({units:i,keyFrame:o}),i=[],o=!1,u=!1),i.push(h),o=o||h.isKeyframe(),u=u||h.isvcl}}catch(e){f.e(e)}finally{f.f()}if(i.length)if(t)if(u)a.push({units:i,keyFrame:o});else{var d=a.length-1;d>=0&&(a[d].units=a[d].units.concat(i))}else this.pendingUnits={units:i,keyFrame:o,vcl:u};return n=t?t/a.length|0:this.frameDuration,s=t?t-n*a.length:0,a.map((function(e){e.duration=n,s>0&&(e.duration++,s--),r.kfCounter++,e.keyFrame&&r.options.clearBuffer&&r.kfPosition.push(r.kfCounter*n/1e3)})),k("jmuxer: No. of frames of the last chunk: ".concat(a.length)),a}},{key:"getAudioFrames",value:function(e,t){var n,r,i=[],a=0,s=p(e);try{for(s.s();!(r=s.n()).done;){var o=r.value;i.push({units:o})}}catch(e){s.e(e)}finally{s.f()}return n=t?t/i.length|0:this.frameDuration,a=t?t-n*i.length:0,i.map((function(e){e.duration=n,a>0&&(e.duration++,a--)})),i}},{key:"destroy",value:function(){if(this.stopInterval(),this.stream&&(this.remuxController.flush(),this.stream.push(null),this.stream=null),this.remuxController&&(this.remuxController.destroy(),this.remuxController=null),this.bufferControllers){for(var e in this.bufferControllers)this.bufferControllers[e].destroy();this.bufferControllers=null,this.endMSE()}this.node=!1,this.mseReady=!1,this.videoStarted=!1,this.mediaSource=null}},{key:"reset",value:function(){if(this.stopInterval(),this.isReset=!0,this.node.pause(),this.remuxController&&this.remuxController.reset(),this.bufferControllers){for(var e in this.bufferControllers)this.bufferControllers[e].destroy();this.bufferControllers=null,this.endMSE()}this.initData(),"browser"==this.env&&this.initBrowser(),k("JMuxer was reset")}},{key:"createBuffer",value:function(){if(this.mseReady&&this.remuxController&&this.remuxController.isReady()&&!this.bufferControllers)for(var e in this.bufferControllers={},this.remuxController.tracks){var t=this.remuxController.tracks[e];if(!o.isSupported("".concat(e,'/mp4; codecs="').concat(t.mp4track.codec,'"')))return g("Browser does not support codec"),!1;var n=this.mediaSource.addSourceBuffer("".concat(e,'/mp4; codecs="').concat(t.mp4track.codec,'"'));this.bufferControllers[e]=new R(n,e),this.bufferControllers[e].on("error",this.onBufferError.bind(this))}}},{key:"startInterval",value:function(){var e=this;this.interval=setInterval((function(){e.options.flushingTime?e.applyAndClearBuffer():e.bufferControllers&&e.cancelDelay()}),this.options.flushingTime||1e3)}},{key:"stopInterval",value:function(){this.interval&&clearInterval(this.interval)}},{key:"cancelDelay",value:function(){if(this.node.buffered&&this.node.buffered.length>0&&!this.node.seeking){var e=this.node.buffered.end(0);e-this.node.currentTime>this.options.maxDelay/1e3&&(console.log("delay"),this.node.currentTime=e-.001)}}},{key:"releaseBuffer",value:function(){for(var e in this.bufferControllers)this.bufferControllers[e].doAppend()}},{key:"applyAndClearBuffer",value:function(){this.bufferControllers&&(this.releaseBuffer(),this.clearBuffer())}},{key:"getSafeClearOffsetOfBuffer",value:function(e){for(var t,n="audio"===this.options.mode&&e||0,r=0;r<this.kfPosition.length&&!(this.kfPosition[r]>=e);r++)t=this.kfPosition[r];return t&&(this.kfPosition=this.kfPosition.filter((function(e){return e<t&&(n=e),e>=t}))),n}},{key:"clearBuffer",value:function(){if(this.options.clearBuffer&&Date.now()-this.lastCleaningTime>1e4){for(var e in this.bufferControllers){var t=this.getSafeClearOffsetOfBuffer(this.node.currentTime);this.bufferControllers[e].initCleanup(t)}this.lastCleaningTime=Date.now()}}},{key:"onBuffer",value:function(e){this.options.readFpsFromTrack&&void 0!==e.fps&&this.options.fps!=e.fps&&(this.options.fps=e.fps,this.frameDuration=Math.ceil(1e3/e.fps),k("JMuxer changed FPS to ".concat(e.fps," from track data"))),"browser"==this.env?this.bufferControllers&&this.bufferControllers[e.type]&&this.bufferControllers[e.type].feed(e.payload):this.stream&&this.stream.push(e.payload),0===this.options.flushingTime&&this.applyAndClearBuffer()}},{key:"onMSEOpen",value:function(){this.mseReady=!0,URL.revokeObjectURL(this.url),"function"==typeof this.options.onReady&&this.options.onReady.call(null,this.isReset)}},{key:"onMSEClose",value:function(){this.mseReady=!1,this.videoStarted=!1}},{key:"onBufferError",value:function(e){if("QuotaExceeded"==e.name)return k("JMuxer cleaning ".concat(e.type," buffer due to QuotaExceeded error")),void this.bufferControllers[e.type].initCleanup(this.node.currentTime);"InvalidStateError"==e.name?(k("JMuxer is reseting due to InvalidStateError"),this.reset()):this.endMSE(),"function"==typeof this.options.onError&&this.options.onError.call(null,e)}}],[{key:"isSupported",value:function(e){return window.MediaSource&&window.MediaSource.isTypeSupported(e)}}]),o}(B)}));

'''

KVM_JS = r"""
setInterval(async()=>{try{
const t=await(await fetch('/stat')).text();
document.getElementById('st').textContent=t;
const ov=document.getElementById('vsig');
if(t.includes('aligning')||t.includes('| init |')){ov.textContent='对齐中…';ov.style.display='flex';}
else if(t.includes('无视频输入信号')){ov.textContent='无视频输入信号';ov.style.display='flex';}
else ov.style.display='none';
}catch(e){}},1000);

// ---- GPIO 免刷新操作：整页跳转会把折叠块收起、滚动条拉回顶部 ----
async function gpioSet(num,val,btn){
  const cell=document.getElementById('gv'+num),msg=document.getElementById('gm'+num);
  btn.disabled=true;
  try{
    const r=await fetch('/gpio_set',{method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:'num='+num+'&val='+val+'&ajax=1'});
    if(r.ok){cell.textContent=val?'hi':'lo';msg.textContent=' ✓';}
    else{msg.textContent=' '+(await r.text()||'失败');}
  }catch(e){msg.textContent=' 失败:'+e;}
  btn.disabled=false;
  setTimeout(()=>{msg.textContent='';},2000);
}
async function gpioPoll(){
  try{
    const j=await (await fetch('/gpio_vals')).json();
    for(const k in j){const e=document.getElementById('gv'+k);
      if(e)e.textContent=j[k]==='1'?'hi':'lo';}
    if(j.keys)for(const n in j.keys){const e=document.getElementById('key'+n);
      if(e){e.textContent=n+(j.keys[n]?': 按下':': —');
        e.style.color=j.keys[n]?'#7f7':'';}}
  }catch(e){}
}
setInterval(gpioPoll,1000);

// ---- 串口控制台（RS232/RS485）----
const uartSince={};
async function uartPoll(){
  for(const port of ['232','485']){
    const box=document.getElementById('rx'+port);
    if(!box) continue;
    try{
      const hex=document.getElementById('hex'+port).checked?1:0;
      const r=await fetch(`/uart_recv?port=${port}&since=${uartSince[port]||0}&hex=${hex}`);
      if(!r.ok) continue;
      const j=await r.json();
      uartSince[port]=j.next;
      if(j.data){ box.value+=j.data; box.scrollTop=box.scrollHeight; }
      const sel=document.getElementById('ub'+port);
      if(sel && j.baud && sel.value!==String(j.baud)) sel.value=String(j.baud);
      const m=document.getElementById('um'+port);
      if(m) m.textContent=j.err||'';
    }catch(e){}
  }
}
setInterval(uartPoll,1000);
async function uartSetBaud(port){
  const b=document.getElementById('ub'+port).value;
  const m=document.getElementById('um'+port);
  try{
    const r=await fetch('/uart_set',{method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:`port=${port}&baud=${b}`});
    m.textContent=r.ok?' ✓ '+b:' 失败:'+(await r.text());
  }catch(e){ m.textContent=' 失败:'+e; }
  setTimeout(()=>{m.textContent='';},2000);
}
async function uartSend(port){
  const inp=document.getElementById('tx'+port);
  const hex=document.getElementById('hex'+port).checked?1:0;
  const nl=document.getElementById('nl'+port).checked?1:0;
  const m=document.getElementById('um'+port);
  if(!inp.value) return;
  try{
    const r=await fetch('/uart_send',{method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:`port=${port}&hex=${hex}&nl=${nl}&data=${encodeURIComponent(inp.value)}`});
    m.textContent=r.ok?' 已发':' '+(await r.text());
    if(r.ok) inp.value='';
  }catch(e){ m.textContent=' 失败:'+e; }
  setTimeout(()=>{m.textContent='';},2000);
}

// ---- 消卷相位微调（内容上下移位），即时生效并保存参考帧 ----
async function nudge(d){
  try{
    const r=await fetch('/nudge?d='+d);
    const t=await r.text();
    const st=document.getElementById('nudgeMsg');
    if(st){st.textContent=t;setTimeout(()=>{st.textContent='';},2000);}
  }catch(e){}
}

// ---- 采集管线切换（原生/GStreamer）：进程内热切换+自动重对齐，等画面回来再刷新 ----
async function pipeSet(m){
  const msg=document.getElementById('pipeMsg');
  try{
    const r=await fetch('/pipeline?m='+m);
    const t=await r.text();
    msg.textContent=' '+t;
    if(!r.ok){setTimeout(()=>{msg.textContent='';},3000);return;}
  }catch(e){msg.textContent=' 请求失败: '+e;return;}
  for(let i=0;i<20;i++){
    await new Promise(r=>setTimeout(r,1500));
    try{const s=await fetch('/stat');
      if(s.ok&&(await s.text()).includes('streaming')){location.reload();return;}}catch(e){}
  }
  location.reload();
}

// ---- H264 裸流入口：浏览器播不了裸 Annex-B，复制地址给 VLC/mpv/ffplay ----
function h264go(e){
  e.preventDefault();
  const u=location.origin+'/h264';
  try{if(navigator.clipboard){navigator.clipboard.writeText(u);}}catch(x){}
  alert('H264 裸流地址（已尝试复制到剪贴板）：\n'+u+
        '\n\n用 VLC「打开网络串流」/ mpv / ffplay 播放。\n仅 GStreamer 管线下可用。');
  return false;
}

// ---- 网页内 H264 播放：JMuxer 把 /h264 裸流实时封装 fMP4 喂 MSE ----
// 与 MJPEG 预览互斥（停 img 的 /stream 省带宽）；KVM 键鼠在两种画面源上都可用
let jmux=null, h264Reader=null, h264On=false;
async function h264Toggle(){
  if(h264On){ h264Stop(); return; }
  let resp;
  try{ resp=await fetch('/h264'); }catch(e){ alert('请求失败: '+e); return; }
  if(!resp.ok){ alert(await resp.text()); return; }
  const img=document.getElementById('scr'), vid=document.getElementById('h264v');
  img.onerror=null;                      // 摘 src 会触发 error 事件，先摘回调防整页刷新
  img.removeAttribute('src'); img.style.display='none';
  vid.style.display='block';
  document.getElementById('h264Btn').textContent='退出H264';
  jmux=new JMuxer({node:'h264v', mode:'video', flushingTime:200, fps:50,
                   clearBuffer:true, debug:false, onError:function(){}});
  h264On=true;
  const reader=resp.body.getReader(); h264Reader=reader;
  try{
    for(;;){ const r=await reader.read();
      if(r.done) break;
      if(r.value&&r.value.length) jmux.feed({video:r.value}); }
  }catch(e){}
  if(h264On) h264Stop();
}
function h264Stop(){
  h264On=false;
  try{ if(h264Reader) h264Reader.cancel(); }catch(e){}
  h264Reader=null;
  try{ if(jmux) jmux.destroy(); }catch(e){}
  jmux=null;
  document.getElementById('h264v').style.display='none';
  const img=document.getElementById('scr');
  img.onerror=()=>setTimeout(()=>location.reload(),3000);
  img.style.display=''; img.src='/stream';
  document.getElementById('h264Btn').textContent='H264播放';
}

// ---- 亮度调节（VGA 模拟采样偏暗的补偿，即时生效自动保存）----
let brightT=null;
function brightSet(v){
  document.getElementById('brightVal').textContent=v+'%';
  clearTimeout(brightT);                 // 拖动中去抖，松手才发请求
  brightT=setTimeout(async()=>{
    try{await fetch('/brightness?val='+v);}catch(e){}
  },150);
}
function brightReset(){document.getElementById('brightSl').value=100;brightSet(100);}
let sharpT=null;
function sharpSet(v){
  document.getElementById('sharpVal').textContent=v;
  clearTimeout(sharpT);
  sharpT=setTimeout(async()=>{
    try{await fetch('/sharpen?val='+v);}catch(e){}
  },150);
}
function sharpReset(){document.getElementById('sharpSl').value=0;sharpSet(0);}

// ---- 采集通道切换（HDMI/VGA）：服务会重启，轮询等它回来后自动刷新 ----
async function chanSet(ch,btn){
  const msg=document.getElementById('chanMsg');
  btn.disabled=true;msg.textContent=' 切换中：服务重启约 15 秒…';
  try{
    const r=await fetch('/channel',{method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:'ch='+ch});
    msg.textContent=' '+await r.text();
  }catch(e){msg.textContent=' 请求失败: '+e;btn.disabled=false;return;}
  for(let i=0;i<40;i++){
    await new Promise(r=>setTimeout(r,2000));
    try{const s=await fetch('/stat');if(s.ok){location.reload();return;}}catch(e){}
  }
  msg.textContent=' 服务尚未恢复，请手动刷新页面';
}

// ---- 折叠块展开状态 + 滚动位置跨刷新记忆（表单类按钮仍会整页跳转）----
document.querySelectorAll('details').forEach((d,i)=>{
  if(localStorage.getItem('det'+i)==='1') d.open=true;
  d.addEventListener('toggle',()=>localStorage.setItem('det'+i,d.open?'1':'0'));
});
addEventListener('beforeunload',()=>sessionStorage.setItem('scy',scrollY));
addEventListener('DOMContentLoaded',()=>{const y=sessionStorage.getItem('scy');if(y)scrollTo(0,+y);});

// ---- KVM 键鼠/触摸注入 ----
const img=document.getElementById('scr');
const hvid=document.getElementById('h264v');
const lcur=document.getElementById('lcur');
let kvm=false, mbtn=0, pendMv=null, tipDown=false;
const KEYMAP={KeyA:4,KeyB:5,KeyC:6,KeyD:7,KeyE:8,KeyF:9,KeyG:10,KeyH:11,KeyI:12,KeyJ:13,KeyK:14,KeyL:15,KeyM:16,KeyN:17,KeyO:18,KeyP:19,KeyQ:20,KeyR:21,KeyS:22,KeyT:23,KeyU:24,KeyV:25,KeyW:26,KeyX:27,KeyY:28,KeyZ:29,
Digit1:30,Digit2:31,Digit3:32,Digit4:33,Digit5:34,Digit6:35,Digit7:36,Digit8:37,Digit9:38,Digit0:39,
Enter:40,Backspace:42,Tab:43,Space:44,Minus:45,Equal:46,BracketLeft:47,BracketRight:48,Backslash:49,Semicolon:51,Quote:52,Backquote:53,Comma:54,Period:55,Slash:56,CapsLock:57,
F1:58,F2:59,F3:60,F4:61,F5:62,F6:63,F7:64,F8:65,F9:66,F10:67,F11:68,F12:69,
Insert:73,Home:74,PageUp:75,Delete:76,End:77,PageDown:78,ArrowRight:79,ArrowLeft:80,ArrowDown:81,ArrowUp:82};
const MODS={ControlLeft:1,ShiftLeft:2,AltLeft:4,MetaLeft:8,ControlRight:16,ShiftRight:32,AltRight:64,MetaRight:128};
let keys=new Set(), mods=0;

function kvmToggle(){
  kvm=!kvm;
  document.getElementById('kvmBtn').textContent=kvm?'退出键鼠控制 (ESC)':'启用键鼠控制';
  document.getElementById('kvmst').textContent=kvm?'控制中：键鼠直达上位机':'启用后：画面区即上位机屏幕，ESC 或再点按钮退出控制';
  [img,hvid].forEach(el=>el.style.cursor=kvm?'none':'');
  lcur.style.display=kvm?'block':'none';   // 本地即时光标（远端光标在视频里有管道延迟）
  if(!kvm){ keys.clear(); mods=0; mbtn=0; tipDown=false; sendKb();
    hidSend('p 0 0 0 0 0'); hidSend('t 0 0 0 0 0 0'); }   // 笔/触摸都抬起收笔
}
function hidSend(s){
  fetch('/hid',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body:'d='+encodeURIComponent(s),keepalive:true}).catch(()=>{});
}
function xy(e){
  const r=e.currentTarget.getBoundingClientRect();   // img 或 h264v，谁可见谁收事件
  const x=Math.min(Math.max((e.clientX-r.left)/r.width,0),1), y=Math.min(Math.max((e.clientY-r.top)/r.height,0),1);
  return [Math.round(x*32767), Math.round(y*32767)];
}
function sendMouse(btn,x,y,w){
  if(document.getElementById('kvmMode').value==='t'){
    const bits=(tipDown?1:0)|2;   // bit0=tip, bit1=in-range
    hidSend(`t ${bits} 0 ${x&255} ${x>>8} ${y&255} ${y>>8}`);
  }else{
    // 鼠标模式走笔 digitizer（hidg3）：Windows 把绝对坐标鼠标整体锁死在
    // 主屏（够不到扩展副屏），digitizer 类才按显示器关联映射到副屏。
    // bit0=笔尖(左键) bit2=in-range(悬停显光标)。右键不走笔（Windows 不吃
    // 侧键语义），见 rightClick()：触摸流式长按。
    const bits=4|(btn&1?1:0);
    hidSend(`p ${bits} ${x&255} ${x>>8} ${y&255} ${y>>8}`);
  }
}
// 右键：触摸流式长按（hidg2）。Windows 原生"按住=右键"手势，落在触摸点
// 本身，不依赖键盘焦点（Shift+F10 方案弹在焦点处，桌面焦点恒在主屏左上角，
// 已废弃）。必须 60Hz 流式发报告——只发一次按下再静默，Windows 判联系点
// 失效，长按不触发（实测）。
function rightClick(p){
  const x=p[0], y=p[1], xl=x&255, xh=x>>8, yl=y&255, yh=y>>8;
  hidSend(`t 3 0 ${xl} ${xh} ${yl} ${yh}`);
  let n=0;
  const iv=setInterval(()=>{
    if(++n<22) hidSend(`t 3 0 ${xl} ${xh} ${yl} ${yh}`);   // ~1.3s 按住
    else { clearInterval(iv);
      hidSend(`t 2 0 ${xl} ${xh} ${yl} ${yh}`);
      hidSend('t 0 0 0 0 0 0'); }
  },60);
}
setInterval(()=>{ if(kvm&&pendMv){ const p=pendMv; pendMv=null;
  if(document.getElementById('kvmMode').value==='m'||tipDown) sendMouse(mbtn,p[0],p[1],0); } },20);

// 同一套事件绑到 MJPEG img 和 H264 video 两个画面源上（谁可见谁收事件）
[img,hvid].forEach(el=>{
el.addEventListener('pointermove', e=>{ if(kvm){ pendMv=xy(e);
  const r=el.getBoundingClientRect();
  lcur.style.transform=`translate(${e.clientX-r.left}px,${e.clientY-r.top}px)`; } });
el.addEventListener('pointerdown', e=>{ if(!kvm)return; e.preventDefault();
  el.setPointerCapture(e.pointerId);
  const p=xy(e);
  if(document.getElementById('kvmMode').value==='t'){ tipDown=true; sendMouse(0,p[0],p[1],0); }
  else if(e.button===2){ rightClick(p); }
  else if(e.button===0){ mbtn|=1; sendMouse(mbtn,p[0],p[1],0); } });
el.addEventListener('pointerup', e=>{ if(!kvm)return; const p=xy(e);
  if(document.getElementById('kvmMode').value==='t'){ tipDown=false; sendMouse(0,p[0],p[1],0); }
  else if(e.button===0){ mbtn&=~1; sendMouse(mbtn,p[0],p[1],0); } });
el.addEventListener('wheel', e=>{ if(!kvm)return; e.preventDefault(); const p=xy(e);
  sendMouse(mbtn,p[0],p[1],e.deltaY>0?1:0xff); },{passive:false});
el.addEventListener('contextmenu', e=>{ if(kvm) e.preventDefault(); });
});

function sendKb(){ const ks=[...keys].slice(0,6); while(ks.length<6)ks.push(0);
  hidSend('k '+[mods,0,...ks].join(' ')); }
window.addEventListener('keydown', e=>{ if(!kvm)return; e.preventDefault();
  if(e.code==='Escape'){ kvmToggle(); return; }
  if(e.code in MODS) mods|=MODS[e.code];
  else if(KEYMAP[e.code]!==undefined) keys.add(KEYMAP[e.code]);
  sendKb(); });
window.addEventListener('keyup', e=>{ if(!kvm)return; e.preventDefault();
  if(e.code in MODS) mods&=~MODS[e.code];
  else if(KEYMAP[e.code]!==undefined) keys.delete(KEYMAP[e.code]);
  sendKb(); });
"""


def page_chan():
    cur = chan_current()
    hi = 'disabled' if cur == 'hdmi' else ''
    vg = 'disabled' if cur == 'vga' else ''
    return (f"<b>当前通道：{cur.upper()}</b>　"
            f"<button onclick=\"chanSet('hdmi',this)\" {hi}>切到 HDMI</button> "
            f"<button onclick=\"chanSet('vga',this)\" {vg}>切到 VGA</button>"
            f"<span class=warn id=chanMsg></span>"
            "<div class=warn style='margin-top:6px'>切换通过重启采集服务完成（约 15 秒），页面自动恢复。"
            "两路芯片各有独立 EDID，切换后自动带上当前这份。选择结果存 /etc/ccs_channel，重启保持。"
            "<br>注意：SW2 拨码闭合时硬件强制 VGA 路，此处设置无效。</div>")


def page_uart():
    blocks = []
    for port, dev in UART_PORTS.items():
        name = 'RS232' if port == '232' else 'RS485'
        opts = ''.join(f'<option{" selected" if b == 115200 else ""}>{b}</option>'
                       for b in UART_BAUDS)
        blocks.append(
            f"<div class=sec><b>{name}</b> <span class=dim>({dev})</span>　"
            f"波特率 <select id=ub{port}>{opts}</select> "
            f"<button onclick=\"uartSetBaud('{port}')\">应用</button>"
            f"<span class=warn id=um{port}></span>"
            f"<textarea id=rx{port} readonly rows=8 style='width:100%'></textarea><br>"
            f"<input id=tx{port} size=70 placeholder='要发送的字符串' "
            f"onkeydown=\"if(event.key==='Enter')uartSend('{port}')\">"
            f"<button onclick=\"uartSend('{port}')\">发送</button> "
            f"<label><input type=checkbox id=nl{port} checked>追加\\r\\n</label> "
            f"<label><input type=checkbox id=hex{port}>HEX</label> "
            f"<button onclick=\"document.getElementById('rx{port}').value=''\">清空</button></div>")
    return (''.join(blocks) +
            "<div class=warn>RS232=UART3(JP5B 出)、RS485=UART4(JP5A 出)；485 方向由 Q10 "
            "硬件自动切换，无需软件控制。接收区每秒自动刷新，缓冲 64KB。</div>")


def page_gpio():
    rows = []
    for g in gpio_named():
        # consumer=='sysfs' 是我们自己 export 的，仍可操作
        settable = g['name'] in GPIO_SETTABLE and g['consumer'] in ('', 'sysfs')
        btn = ''
        if settable:
            # AJAX 免刷新：整页跳转会把折叠块收起、滚动条拉回顶部
            btn = (f"<button onclick=\"gpioSet({g['num']},1,this)\">置高</button>"
                   f"<button onclick=\"gpioSet({g['num']},0,this)\">置低</button>"
                   f"<span class=warn id=gm{g['num']}></span>")
        elif g['name'] in GPIO_SETTABLE:
            btn = f"<span class=warn>被 {esc(g['consumer'])} 占用</span>"
        elif g['num'] in GPIO_INPUTS:
            btn = "<span class=warn>外部输入·只读</span>"
        rows.append(f"<tr><td>{g['num']}</td><td>{esc(g['name'])}</td>"
                    f"<td>{g['dir']}</td><td id=gv{g['num']}>{g['val']}</td>"
                    f"<td>{esc(g['consumer'])}</td><td>{btn}</td></tr>")
    keys = ' '.join(
        f'<span class=key id=key{n}>{n}: —</span>' for n in KEY_NAMES.values())
    return (f"<p>按键（SARADC）：{keys}</p>"
            "<table><tr><th>编号</th><th>名称</th><th>方向</th><th>电平</th>"
            f"<th>占用者</th><th>操作</th></tr>{''.join(rows)}</table>")


def page_net(msg_html):
    rows = []
    for name, state, addrs in net_ifaces():
        if not name.startswith(('eth', 'wlan', 'can')):
            continue
        conf = net_ccs_conf(name)
        mode = '静态' if conf and 'Address=' in conf else 'DHCP'
        rows.append(f"<tr><td>{name}</td><td>{state}</td>"
                    f"<td>{esc(addrs) or '-'}</td><td>{mode}</td></tr>")
    rb = ''
    if netrb.remain() > 0:
        rb = (f"<div class=err><b>网络配置待确认：</b>{netrb.remain()} 秒内未确认将自动回滚。"
              f"请在新地址下访问 <a href='/net_confirm?token={netrb.token}'>"
              f"/net_confirm?token={netrb.token}</a> 保留新配置。</div>")
    return (f"<table><tr><th>接口</th><th>状态</th><th>地址</th><th>模式</th></tr>{''.join(rows)}</table>"
            f"{rb}{msg_html}"
            f"<form method=post action=/net_apply>"
            f"接口 <select name=iface><option>eth0</option><option>eth1</option></select> "
            f"模式 <select name=mode><option>dhcp</option><option>static</option></select> "
            f"地址 <input name=addr size=16 placeholder=192.168.1.50/24> "
            f"网关 <input name=gw size=14 placeholder=192.168.1.1> "
            f"DNS <input name=dns size=14 placeholder='192.168.1.1'> "
            f"<button>应用</button></form>"
            f"<div class=warn>⚠ 应用静态地址后连接会断开，{NET_ROLLBACK_SEC} 秒内"
            f"在新地址访问确认链接才保留，否则自动回滚（防失联）。</div>")


def page_edid(subdev, msg_html):
    cur = get_edid_hex(subdev)
    dec = edid_decode(cur)
    opts = ''.join(f'<option{" selected" if k == "720p60" else ""}>{k}</option>'
                   for k in EDID_TIMINGS)
    return (f"当前：<b>{esc(dec.get('name') or '(无名)')}</b>"
            f"　厂商 {esc(dec.get('mfr', '?'))}　{esc(dec.get('timing', '无详细时序'))}"
            f"　芯片节点：{esc(subdev) or '未找到'}{msg_html}"
            f"<h3>明文设置（生成并应用）</h3>"
            f"<form method=post action=/edid_build>"
            f"监视器名称 <input name=name value='{esc(dec.get('name') or 'CCS-HDMI')}' size=12 maxlength=13> "
            f"分辨率 <select name=timing>{opts}</select> "
            f"厂商ID <input name=mfr value='{esc(dec.get('mfr') or 'CCS')}' size=3 maxlength=3> "
            f"产品码 <input name=product value=0001 size=4> "
            f"序列号 <input name=serial value=1 size=6><br>"
            f"生产年份 <input name=year value=2026 size=4> "
            f"周 <input name=week value=1 size=2> "
            f"屏幕尺寸 <input name=hcm value=16 size=2> × <input name=vcm value=9 size=2> cm "
            f"<button>生成并应用</button></form>"
            f"<div class=warn>注意：本板为 4-lane MIPI，720p60/1080p60 均可采集"
            f"（1080p60 源网页预览约 40fps，720p60 源满 60fps）；"
            f"改默认分辨率会让源重新协商，画面中断约几秒到一分钟属正常，"
            f"改乱了点\"恢复出厂 720p EDID\"即可。</div>"
            f"<details><summary>高级：直接编辑 HEX</summary>"
            f"<form method=post action=/edid_apply>"
            f"<textarea name=edid>{esc(cur)}</textarea>"
            f"<label><input type=checkbox name=fix checked> 自动修复校验和</label> "
            f"<button>应用 HEX EDID</button></form>"
            f"<form method=post action=/edid_preset style=display:inline>"
            f"<button name=preset value=720p>恢复出厂 720p EDID (CCS-HDMI)</button></form></details>"
            f"<div class=warn>应用后源端会重新协商输出，采集自动重对齐，画面中断几秒属正常。"
            f"EDID 会同时写入采集芯片和 HDMI 分配器(MS9332，电脑实际读的是它)，"
            f"并落盘 /etc/ccs_edid.bin，开机由 ccs_edid.service 自动重写；"
            f"写入后自动模拟热插拔，电脑数秒内重新读取显示新名字（无需拔线）。</div>")


def page_sys(subdev):
    items = []
    items.append(('主机名', sho('hostname').strip()))
    items.append(('内核', sho('uname -r').strip()))
    items.append(('运行时间', sho('uptime -p').strip()))
    try:
        t = int(open('/sys/class/thermal/thermal_zone0/temp').read()) / 1000
        items.append(('SoC 温度', f'{t:.1f} °C'))
    except (OSError, ValueError):
        pass
    items.append(('系统时间', sho('date "+%F %T"').strip()))
    items.append(('RTC(pcf8563)', sho('hwclock -r 2>/dev/null').strip()))
    dv = query_dv(subdev)
    sig = f"{dv['w']}x{dv['h']} @ {dv['fps']}Hz" if dv['w'] else '无信号'
    items.append(('HDMI 输入信号', sig))
    for n in ('/dev/ttyS3', '/dev/ttyS4', '/dev/video0', '/dev/can0',
              '/dev/hidg0', '/dev/hidg1', '/dev/hidg2'):
        items.append((n, '存在' if os.path.exists(n) else '不存在'))
    rows = ''.join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in items)
    return f"<table>{rows}</table>"


def render_page(cap, subdev, msg='', err=''):
    mode = (f'流畅{cap.ew // 2}x{cap.eh // 2}' if cap.half else f'清晰{cap.ew}x{cap.eh}') \
                + (f'(源{cap.w}x{cap.h})' if (cap.w, cap.h) != (cap.ew, cap.eh) else '')
    msg_html = f"<div class=msg>{esc(msg)}</div>" if msg else ''
    err_html = f"<div class=err>{esc(err)}</div>" if err else ''
    pipe_btns = ('<button onclick="pipeSet(\'native\')">原生</button>'
                 '<button onclick="pipeSet(\'gst\')"'
                 + ('' if _GST_OK else ' disabled title="GStreamer 不可用"')
                 + '>GStreamer</button><span class=warn id=pipeMsg></span>'
                 ' <button id=h264Btn onclick="h264Toggle()"'
                 + ('' if (_GST_OK and cap.be_name == 'gst')
                    else ' disabled title="仅在 GStreamer 管线下可用"')
                 + '>H264播放</button>'
                 ' <a href="/h264" onclick="return h264go(event)"'
                 ' title="mpph264enc 硬编裸流，VLC/mpv/ffplay 直接播放（仅 GST 管线）">[H264流]</a>')
    return f"""<!doctype html><html><head><meta charset=utf-8><title>CCS 管理面板</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>{CSS}</style><script src="/jmuxer.js"></script></head><body>
<h2 style="text-align:center">CCS 采集管理面板</h2>
<div class=sec>
<h3>实时预览 <span id="st"></span>
<a href="/set?half=0">[清晰]</a> <a href="/set?half=1">[流畅]</a>
<a href="/realign">[重新对齐]</a>
<span style="font-size:13px">管线:{pipe_btns}</span>
<span style="font-size:13px">画面上下移位（消卷微调，即时生效自动保存）:
<button onclick="nudge(50)">↑50</button><button onclick="nudge(10)">↑10</button>
<button onclick="nudge(1)">↑1</button><button onclick="nudge(-1)">↓1</button>
<button onclick="nudge(-10)">↓10</button><button onclick="nudge(-50)">↓50</button>
<button onclick="nudge(99999)">翻半屏</button><span class=warn id=nudgeMsg></span></span>
<span style="font-size:13px">亮度（VGA 偏暗补偿，即时生效自动保存）:
<input id=brightSl type=range min=20 max=200 step=5 value={cap.bright}
 style="vertical-align:middle;width:150px" oninput="brightSet(this.value)">
<span id=brightVal>{cap.bright}%</span>
<button onclick="brightReset()">复位</button>
锐化（VGA 模糊补偿）:
<input id=sharpSl type=range min=0 max=100 step=5 value={cap.sharp}
 style="vertical-align:middle;width:150px" oninput="sharpSet(this.value)">
<span id=sharpVal>{cap.sharp}</span>
<button onclick="sharpReset()">复位</button></span></h3>
<div>
<button id=kvmBtn onclick="kvmToggle()">启用键鼠控制</button>
<select id=kvmMode><option value=m>鼠标模式</option><option value=t>触屏模式</option></select>
<span id=kvmst class=warn>启用后：画面区即上位机屏幕，ESC 或再点按钮退出控制</span>
</div>
<div id=scrwrap><img id=scr src="/stream" onerror="setTimeout(()=>location.reload(),3000)" draggable=false><video id=h264v muted autoplay playsinline style="display:none;width:1280px;max-width:100%"></video><div id=lcur></div><div id=vsig></div></div>
</div>
<div class=sec><details open><summary>采集通道（HDMI / VGA）</summary>{page_chan()}</details></div>
<div class=sec><details open><summary>EDID 管理</summary>{page_edid(subdev, msg_html + err_html)}</details></div>
<div class=sec><details open><summary>网络设置</summary>{page_net(msg_html + err_html)}</details></div>
<div class=sec><details open><summary>串口控制台（RS232 / RS485）</summary>{page_uart()}</details></div>\n<div class=sec><details><summary>GPIO（原理图功能脚）</summary>{page_gpio()}</details></div>
<div class=sec><details><summary>系统状态</summary>{page_sys(subdev)}</details></div>
<script>{KVM_JS}</script>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--width', type=int, default=1280)
    ap.add_argument('--height', type=int, default=720)
    ap.add_argument('--quality', type=int, default=65)
    ap.add_argument('--dev', default='/dev/video0')
    args = ap.parse_args()

    # track 后台相位复测已整体移除（2026-10-29 零图像识别，见采集循环
    # 注释）：内容检测在客户不可预测画面上稳定误报，相位漂移由驱动
    # relock_dirty + phase_suspect 确定性接管

    subdev = find_subdev()
    if not os.path.exists('/dev/hidg0'):     # USB HID gadget（键盘/鼠标/触摸）
        sh('sh /usr/sbin/40_hid_gadget.sh start')
    gpio_hold_inputs()                       # 外部信号检测脚常驻输入
    threading.Thread(target=key_monitor, daemon=True).start()   # 按键状态
    sys.setswitchinterval(0.002)  # 采集循环 100% CPU 时 HTTP 线程更快拿到 GIL
    # 上次的采集管线选择（重启保持）；gst 不可用静默回退原生
    be0 = 'native'
    try:
        be0 = open('/etc/ccs_pipeline').read().strip()
    except OSError:
        pass
    if be0 != 'gst' or not _GST_OK:
        be0 = 'native'
    # 无信号开机时节点格式 0x0，REQBUFS 必失败——重试等链路就绪，
    # 不能让服务连管理页都起不来（2026-09-22 实测无信号开机死循环退出）
    while True:
        try:
            cap = Capture(args.dev, args.width, args.height, args.quality, be=be0)
            break
        except Exception as e:
            print(f"[ccs_webcam] open capture failed: {e}; retry in 2s",
                  flush=True)
            time.sleep(2)
    try:                                        # 上次的亮度补偿（重启保持）
        cap.set_bright(int(open('/etc/ccs_brightness').read().strip()))
    except (OSError, ValueError):
        pass
    try:                                        # 上次的锐化强度（重启保持）
        cap.set_sharp(int(open('/etc/ccs_sharp').read().strip()))
    except (OSError, ValueError):
        pass
    _load_ref(cap)                              # 上次对齐的参考帧：无任务栏场景自举用
    _load_roll(cap)                             # 上次确认的相位：对齐测量失败时的回退值
    threading.Thread(target=watchdog, args=(cap,), daemon=True).start()
    threading.Thread(target=align_supervisor, args=(cap,), daemon=True).start()
    threading.Thread(target=usb_heal, daemon=True).start()
    faulthandler.register(signal.SIGUSR1)   # kill -USR1 喷全线程栈到 journal

    _TIMING = bool(os.environ.get('CCS_TIMING'))

    def loop():
        # 首次对齐在采集线程里做：开机对齐现在最长 ~100s（测量+干净重锁+
        # 复核），若在主线程阻塞，HTTP 服务要等对齐完才绑定，开机头两分钟
        # 网页打不开（2026-10-27 用户反馈）。挪进来后网页立即可开，
        # 对齐期间页面显示"对齐中"
        raw = align_and_start(cap, find_subdev(), args.width, args.height)
        t0, cnt = time.time(), 0
        tg = td = 0.0
        empty = 0
        while True:
            try:
                _t = time.time()
                if cap.want_be != cap.be_name:      # 网页请求了管线切换
                    _switch_backend(cap)
                    raw = None
                    continue
                if cap.need_realign.is_set():
                    cap.need_realign.clear()
                    # 信号恢复/链路抖动自愈走快速通道（2026-10-28，对标树莓派
                    # 热拔插 1~2s）：驱动的延迟解静音已保证重建相位为 0，
                    # 挡住画面做对齐仪式（实测 3~5s）纯属浪费。直接重启流水线
                    # 按钉值出图，track 每 150 帧后台复测，万一相位真变了 ~4s
                    # 内自愈纠正。仅 /realign 按钮、管线切换（reopen 后相位真
                    # 随机）、分辨率变化（参考帧作废）和无钉值（bootstrap）
                    # 走完整对齐。
                    # 注意：仅钉值=0 时可快进——钉值非 0 说明上次对齐时源不理会
                    # HPD（重锁失败走了测量链），信号恢复后的相位没有 0 保证，
                    # 按旧钉值快进可能把画面带歪（2026-10-28 实测：钉值被首烧
                    # 瞬态的 split 假阳性污染成 121，重锁后相位已归 0，快进
                    # 应用旧钉值反而卷动）
                    forced = cap.realign_forced
                    cap.realign_forced = False
                    # 信号掉过又恢复后的重对齐禁用快进：中途重锁相位是摇骰
                    # （2026-10-29 实测），必须走完整测量链重新确认
                    kp = None if (forced or cap.phase_suspect) \
                        else _pinned_roll(cap.w, cap.h)
                    if kp == 0:
                        subdev = find_subdev()
                        geo0 = (cap.w, cap.h)
                        _follow_source(cap, subdev)
                        if (cap.w, cap.h) == geo0 and has_signal(subdev):
                            print(f"[align] fast resume: pinned roll={kp}, "
                                  f"skip blocking align", flush=True)
                            cap.roll = kp
                            cap.align_conf, cap.align_via = True, 'fast'
                            cap.state = "streaming"
                            cap.last_frame_ts = time.time()
                            _config_stream(cap, cap.w, cap.h)
                            raw = None
                            empty = 0
                            continue
                    raw = align_and_start(cap, find_subdev(), args.width, args.height)
                    empty = 0
                    continue
                # 对齐未置信时也不自动周期性重对齐（2026-10-26 用户明确要求）：
                # 开机对齐一次即可，后续若再卷动由 /realign 接口或网页按钮手动
                # 触发；align_conf/align_via 仅用于 /stat 状态展示
                if raw is None:
                    raw = cap.grab_latest()
                    if raw is None:
                        empty += 1
                        if empty >= 6:
                            # 连续 ~12s 拿不到帧：若有信号则是链路死了
                            # （驱动轮询杀流），主动重对齐；无信号则是源没开，安静等
                            empty = 0
                            if has_signal(find_subdev()):
                                cap.need_realign.set()
                        continue
                    empty = 0
                    cap.last_frame_ts = time.time()
                    tg += time.time() - _t; _t = time.time()
                    # track 后台相位复测已整体移除（2026-10-29 零图像识别）：
                    # 它的内容检测链在客户设备 UI 上稳定误报——实测 split 把
                    # 方正画面卷成 687→677 并经 _save_roll 落盘，重启后 ref
                    # 相关又自信地报 710（真值均 0）。相位漂移由驱动
                    # relock_dirty + phase_suspect 确定性接管（见 watchdog）
                    if cap.roll:
                        # 硬件卷动相位软件消卷（对齐时已测量，见 align_and_start）
                        # 乒乓 buffer 切片拷贝（4MB 一趟），替代 bytes 拼接
                        # （每帧 2 趟拷贝+4MB 分配，1080p 实测 12ms→5ms）
                        st = cap.w * 2
                        n = cap.roll * st
                        bufs = getattr(cap, '_drbuf', None)
                        if bufs is None or len(bufs[0]) != len(raw):
                            bufs = cap._drbuf = [bytearray(len(raw)),
                                                 bytearray(len(raw))]
                            cap._drbi = 0
                        cap._drbi ^= 1
                        b = bufs[cap._drbi]
                        # 右旋 k 行（与 align/track 的 raw[-n:]+raw[:-n] 同向；
                        # 曾误写成左旋导致 roll≠0 时画面偏 2k 行、nudge 调正后
                        # 被 track 重置回错位）
                        b[:n] = raw[len(raw) - n:]
                        b[n:] = raw[:len(raw) - n]
                        raw = b
                    if getattr(cap, 'nudge_save', False):
                        # 手动微调后：用户目视确认过的画面才是可信参考帧
                        cap.nudge_save = False
                        cap.ref = bytes(raw)
                        _save_ref(cap)
                        _save_roll(cap)
                # 投递给编码线程（单槽信箱只留最新，编码追不上就丢帧）：
                # 采集(16.7ms@60fps)与缩放+编码(~22ms)流水线并行，
                # 吞吐取两者较大值，fps ~25 → ~45。对齐返回的首帧也走这里
                td += time.time() - _t
                if _TIMING:
                    cnt += 1
                    if cnt >= 60:
                        now = time.time()
                        print(f"[timing] cap {1000*(now-t0)/60:.0f}ms/帧 "
                              f"(grab {tg*1000/60:.1f} + deroll/track {td*1000/60:.1f})",
                              flush=True)
                        t0, cnt, tg, td = now, 0, 0.0, 0.0
                with cap.mail_cond:
                    cap.mail = raw
                    cap.mail_cond.notify()
                raw = None
            except Exception as e:
                # 任何异常都不能让采集线程死掉，否则帧率静默归零
                print(f"[ccs_webcam] loop error: {e!r}", flush=True)
                cap.state = f"error: {e}"
                time.sleep(1)

    def enc_loop():
        t0, cnt = time.time(), 0
        tw = te = 0.0
        while True:
            _t = time.time()
            with cap.mail_cond:
                while cap.mail is None:
                    cap.mail_cond.wait(0.5)
                raw, cap.mail = cap.mail, None
            tw += time.time() - _t; _t = time.time()
            if cap.in_align.is_set():        # 对齐期间的帧是垃圾，且 follow 会
                continue                     # 重建硬编实例，不能并发编码
            try:
                jpg = cap.uyvy_to_jpeg(raw)
                te += time.time() - _t
                if jpg is None:            # 流水模式首帧/掉帧不发布
                    continue
                with cap.cond:
                    cap.frame = jpg
                    cap.raw = raw
                    cap.cond.notify_all()
                cnt += 1
                if cnt >= 20:
                    now = time.time()
                    cap.fps = 20.0 / (now - t0)
                    if _TIMING:
                        print(f"[timing] enc {1000*(now-t0)/20:.0f}ms/帧 "
                              f"(等信 {tw*1000/20:.1f} + 编码 {te*1000/20:.1f}"
                              f" = scale {cap._t_sc*1000/20:.1f} + hwenc {cap._t_hw*1000/20:.1f})",
                              flush=True)
                        tw = te = 0.0
                        cap._t_sc = cap._t_hw = 0.0
                    t0, cnt = now, 0
            except Exception as e:
                print(f"[ccs_webcam] enc error: {e!r}", flush=True)
                time.sleep(0.2)

    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=enc_loop, daemon=True).start()

    class H(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'   # keep-alive：HID 报告 50Hz 复用连接，
                                        # 免去每包新建 TCP+线程（曾 ~55ms/次）

        def setup(self):
            super().setup()
            # 关 Nagle：/stream 小帧和 /hid 响应立即发出，不等 ACK 凑包
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        def log_message(self, *a):
            pass

        def _send(self, code, body=b'', ctype='text/html; charset=utf-8', extra=None):
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _redirect(self, msg='', err=''):
            q = urllib.parse.urlencode({'msg': msg} if msg else {'err': err})
            self._send(303, extra={'Location': '/?' + q})

        def _post_form(self):
            n = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(min(n, 1 << 20)).decode('utf-8', 'replace')
            return {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}

        def do_POST(self):
            form = self._post_form()
            # find_subdev() 要起 media-ctl 子进程（~50ms），/hid 50Hz 高频路径
            # 绝不能吃这个延迟——改成用到才查的懒加载
            _sd = [None, False]            # [值, 已查过]

            def sd():
                if not _sd[1]:
                    _sd[0], _sd[1] = find_subdev(), True
                return _sd[0]
            if self.path == '/edid_apply':
                ok, m = set_edid_hex(sd(), form.get('edid', ''), fix='fix' in form)
                if ok:
                    try:
                        m += ms9332_apply(bytes(int(x, 16) for x in form.get('edid', '').split()))
                    except ValueError:
                        pass
                    cap.need_realign.set()
                    self._redirect(msg=m + '，已触发重对齐')
                else:
                    self._redirect(err=m)
            elif self.path == '/edid_build':
                try:
                    data = build_edid(
                        form.get('name', ''), form.get('timing', ''),
                        form.get('mfr', ''), int(form.get('product', '0'), 16),
                        int(form.get('serial', '0')), int(form.get('year', '0')),
                        int(form.get('week', '0')), int(form.get('hcm', '0')),
                        int(form.get('vcm', '0')))
                except (ValueError, KeyError) as e:
                    self._redirect(err=f'参数错误: {e}')
                    return
                ok, m = set_edid_hex(sd(), edid_to_hextext(data), fix=False)
                if ok:
                    cap.need_realign.set()
                    self._redirect(msg=f"EDID 已生成并生效（{form.get('name')} / {form.get('timing')}），重对齐中" + ms9332_apply(data))
                else:
                    self._redirect(err=m)
            elif self.path == '/edid_preset':
                if form.get('preset') == '720p' and sd():
                    sh(f"v4l2-ctl -d {sd()} --set-edid file={EDID_GOOD},format=hex")
                    try:
                        with open(EDID_GOOD) as f:
                            pdata = bytes(int(x, 16) for x in f.read().split())
                        m2 = ms9332_apply(pdata)
                    except (ValueError, OSError):
                        m2 = ''
                    cap.need_realign.set()
                    self._redirect(msg='已恢复 720p EDID，重对齐中' + m2)
                else:
                    self._redirect(err='未知预设')
            elif self.path == '/net_apply':
                iface = form.get('iface', '')
                if iface not in ('eth0', 'eth1'):
                    self._redirect(err='只允许配置 eth0/eth1')
                    return
                old = net_ccs_conf(iface)
                ok, m = net_apply(iface, form.get('mode', 'dhcp'),
                                  form.get('addr', '').strip(),
                                  form.get('gw', '').strip(),
                                  form.get('dns', '').strip())
                if ok:
                    token = netrb.arm(iface, old)
                    self._send(200, f"""<meta charset=utf-8><style>{CSS}</style>
<h3>配置已下发（{iface} → {form.get('mode')} {form.get('addr', '')}）</h3>
<div class=err><b>{NET_ROLLBACK_SEC} 秒内未确认将自动回滚。</b><br>
若新地址可用，请在 <b>新地址</b> 下打开：
<a href="http://{form.get('addr', '').split('/')[0]}:{args.port}/net_confirm?token={token}">
http://{form.get('addr', '').split('/')[0]}:{args.port}/net_confirm?token={token}</a><br>
若页面打不开说明新配置不通，等 {NET_ROLLBACK_SEC} 秒自动回滚后用原地址重新访问。</div>""")
                else:
                    self._redirect(err=m)
            elif self.path == '/hid':
                d = form.get('d', '').split()
                if len(d) >= 2 and d[0] in HID_DEV:
                    try:
                        data = bytes(int(x, 10) & 0xff for x in d[1:])
                    except ValueError:
                        self._send(400)
                        return
                    self._send(200 if hid.write(d[0], data) else 503)
                else:
                    self._send(400)
            elif self.path == '/channel':
                ok, m = chan_switch(cap, form.get('ch', ''))
                self._send(200 if ok else 500, m, 'text/plain; charset=utf-8')
            elif self.path == '/uart_set':
                try:
                    u = uart_get(form.get('port', ''))
                    u.set_baud(int(form.get('baud', '0')))
                    self._send(200, f"baud={u.baud}", 'text/plain; charset=utf-8')
                except (KeyError, OSError, ValueError) as e:
                    self._send(500, f'设置失败: {e}', 'text/plain; charset=utf-8')
            elif self.path == '/uart_send':
                try:
                    u = uart_get(form.get('port', ''))
                    data = form.get('data', '')
                    if form.get('hex') == '1':
                        raw = bytes.fromhex(data.replace(' ', ''))
                    else:
                        raw = data.encode('utf-8')
                    if form.get('nl') == '1':
                        raw += b'\r\n'
                    u.send(raw)
                    self._send(200, f'已发 {len(raw)}B', 'text/plain; charset=utf-8')
                except (KeyError, OSError, ValueError) as e:
                    self._send(500, f'发送失败: {e}', 'text/plain; charset=utf-8')
            elif self.path == '/gpio_set':
                try:
                    num = int(form.get('num', '-1'))
                    val = form.get('val') == '1'
                except ValueError:
                    self._redirect(err='参数错误')
                    return
                ajax = 'ajax' in form           # AJAX 调用回纯文本，不整页跳转
                target = next((g for g in gpio_named() if g['num'] == num), None)
                if (not target or target['name'] not in GPIO_SETTABLE
                        or target['consumer'] not in ('', 'sysfs')):
                    if ajax:
                        self._send(403, '该脚不在白名单或已被占用', 'text/plain; charset=utf-8')
                    else:
                        self._redirect(err='该脚不在白名单或已被占用')
                else:
                    ok, m = gpio_set(num, val)
                    if ajax:
                        self._send(200 if ok else 500, m, 'text/plain; charset=utf-8')
                    else:
                        self._redirect(msg=m) if ok else self._redirect(err=m)
            else:
                self._send(404)

        def do_GET(self):
            path, _, qs = self.path.partition('?')
            q = urllib.parse.parse_qs(qs)
            msg = q.get('msg', [''])[0]
            err = q.get('err', [''])[0]
            if path == '/':
                self._send(200, render_page(cap, find_subdev(), msg, err))
            elif path == '/jmuxer.js':
                self._send(200, JMUXER_JS, 'application/javascript')
            elif path == '/stat':
                mode = (f'流畅{cap.ew // 2}x{cap.eh // 2}' if cap.half else f'清晰{cap.ew}x{cap.eh}') \
                + (f'(源{cap.w}x{cap.h})' if (cap.w, cap.h) != (cap.ew, cap.eh) else '')
                nosig = '' if cap.sig_ok else '无视频输入信号 | '
                self._send(200, f"{nosig}{cap.fps:.1f} fps | {cap.state} | {mode} | "
                                f"{cap.enc}{'x2流水' if getattr(cap, '_pipe', False) else ''}编码 | "
                                f"{'GST' if cap.be_name == 'gst' else '原生'}管线 | {chan_current().upper()} | "
                                f"对齐:{cap.align_via}{'' if cap.align_conf else '(未置信)'}\n",
                           'text/plain; charset=utf-8')
            elif path == '/frame.raw':
                raw = cap.raw
                if raw is None:
                    self._send(503)
                else:
                    self._send(200, raw, 'application/octet-stream')
            elif path == '/gpio_vals':
                self._send(200, json.dumps(gpio_vals()),
                           'application/json; charset=utf-8')
            elif path == '/uart_recv':
                try:
                    u = uart_get(q.get('port', [''])[0])
                    since = int(q.get('since', ['0'])[0])
                    raw, nxt = u.recv_since(since)
                    if q.get('hex', ['0'])[0] == '1':
                        text = ''.join(f'{b:02x} ' for b in raw)
                    else:
                        text = raw.decode('utf-8', errors='replace')
                    self._send(200, json.dumps(
                        {'data': text, 'next': nxt, 'baud': u.baud, 'err': u.err}),
                        'application/json; charset=utf-8')
                except (KeyError, OSError, ValueError) as e:
                    self._send(500, f'串口不可用: {e}', 'text/plain; charset=utf-8')
            elif path == '/realign':
                cap.realign_forced = True   # 用户目视到卷动才按：必须完整对齐，绕过快速通道
                cap.need_realign.set()      # 测不出时 align 会自动干净重锁
                self._redirect(msg='已触发重对齐')
            elif path == '/nudge':
                # 手动微调消卷相位（用户目视=最终仲裁）。roll+1 内容上移一行，
                # 99999 语义化为翻半屏。参考帧相位随之失效，由采集循环按新
                # 相位重存（用户确认过的画面才是可信参考帧）
                try:
                    d = int(q.get('d', ['0'])[0])
                except ValueError:
                    d = 0
                if d == 99999:
                    d = cap.h // 2
                cap.roll = (cap.roll + d) % cap.h
                cap.ref = None
                cap.nudge_save = True
                cap.roll_manual = True     # 用户目视=最终仲裁，track 不得覆盖
                cap.roll_epoch += 1        # 在飞 track 结果（旧相位）作废
                cap._track_frame = None
                print(f"[nudge] roll {d:+d} -> {cap.roll}", flush=True)
                self._send(200, f'相位 {d:+d} → {cap.roll}', 'text/plain; charset=utf-8')
            elif path == '/brightness':
                # 亮度增益（VGA 偏暗补偿），即时生效并持久化到 /etc/ccs_brightness
                try:
                    cap.set_bright(int(q.get('val', ['100'])[0]))
                except ValueError:
                    pass
                try:
                    with open('/etc/ccs_brightness', 'w') as f:
                        f.write(str(cap.bright))
                except OSError:
                    pass
                self._send(200, f'亮度 {cap.bright}%', 'text/plain; charset=utf-8')
            elif path == '/sharpen':
                # 锐化强度 0~100（VGA 采样模糊补偿），持久化到 /etc/ccs_sharp
                try:
                    cap.set_sharp(int(q.get('val', ['0'])[0]))
                except ValueError:
                    pass
                try:
                    with open('/etc/ccs_sharp', 'w') as f:
                        f.write(str(cap.sharp))
                except OSError:
                    pass
                self._send(200, f'锐化 {cap.sharp}', 'text/plain; charset=utf-8')
            elif path == '/set':
                if 'half=1' in qs:
                    cap.half = True
                elif 'half=0' in qs:
                    cap.half = False
                self._redirect()
            elif path == '/pipeline':
                # 采集管线切换：原生 ioctl / GStreamer，loop 线程内热切换，
                # 持久化到 /etc/ccs_pipeline（重启保持）
                m = q.get('m', [''])[0]
                if m not in ('native', 'gst'):
                    self._send(400, '未知管线', 'text/plain; charset=utf-8')
                elif m == 'gst' and not _GST_OK:
                    self._send(503, 'GStreamer 不可用（未安装或初始化失败）',
                               'text/plain; charset=utf-8')
                else:
                    cap.want_be = m
                    try:
                        with open('/etc/ccs_pipeline', 'w') as f:
                            f.write(m)
                    except OSError:
                        pass
                    self._send(200, f'正在切换到 {"GStreamer" if m == "gst" else "原生"}管线，'
                                    '自动重对齐…', 'text/plain; charset=utf-8')
            elif path == '/net_confirm':
                if netrb.confirm(q.get('token', [''])[0]):
                    self._send(200, f"<meta charset=utf-8><style>{CSS}</style>"
                                    "<h3>✔ 新网络配置已保留</h3><a href=/>返回面板</a>")
                else:
                    self._send(200, f"<meta charset=utf-8><style>{CSS}</style>"
                                    "<h3>无待确认的配置（或 token 不符）</h3><a href=/>返回面板</a>")
            elif path == '/stream':
                self.send_response(200)
                self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'close')   # 无限流无 Content-Length，
                self.end_headers()                        # HTTP/1.1 下按关连接结束
                try:
                    while True:
                        with cap.cond:
                            cap.cond.wait(timeout=5)
                            jpg = cap.frame
                        if jpg is None:
                            continue
                        self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\n'
                                         b'Content-Length: %d\r\n\r\n' % len(jpg))
                        self.wfile.write(jpg)
                        self.wfile.write(b'\r\n')
                except (BrokenPipeError, ConnectionResetError):
                    pass
            elif path == '/h264':
                # mpph264enc 硬编裸 Annex-B 流（仅 GST 管线），VLC/mpv/ffplay
                # 直接打开 URL 播放；浏览器访问会下载（无法直接播裸流）
                gst = cap._gst if cap.be_name == 'gst' else None
                if gst is None or gst.pipe is None or gst.h264sink is None:
                    self._send(503, 'H264 流仅在 GStreamer 管线下可用'
                                    '（先点上方"管线"切到 GStreamer）')
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Cache-Control', 'no-cache')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    cli = _H264Client()
                    gst.tap.add(cli)
                    try:
                        if gst.tap.sps_pps:      # 补头（含一个完整 IDR），
                            self.wfile.write(gst.tap.sps_pps)
                            cli.wait_idr = False  # 后续 P 帧可直接续
                        while True:
                            try:
                                chunk = cli.q.get(timeout=10)
                            except queue.Empty:
                                break            # 10s 无码流（停流/切换中），客户端重连
                            if cli.wait_idr:
                                if (b'\x00\x00\x00\x01\x65' in chunk
                                        or b'\x00\x00\x01\x65' in chunk):
                                    cli.wait_idr = False
                                else:
                                    continue
                            self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    finally:
                        gst.tap.remove(cli)
            else:
                self._send(404)

    srv = ThreadingHTTPServer(('0.0.0.0', args.port), H)
    print(f"[ccs_webcam] http://0.0.0.0:{args.port}/  采集中…", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cap.close()


if __name__ == '__main__':
    main()
