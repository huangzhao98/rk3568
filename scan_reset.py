#!/usr/bin/env python3
"""扫描轮询抓包轮次文件，找 MS8005F 复位后的初始化序列 / EDID 写入。
用法: python3 scan_reset.py /tmp/cap_r*.txt
复位特征：
  - EDID 头 00 ff ff ff ff ff ff 00 出现在某个 WR 事务里
  - 长 WR 事务（>30 字节，稳态没有）
  - 超长总线安静（>300ms；稳态轮询间隔 ~112ms）
  - 'MACROSILICON' 显示器名字串
"""
import glob
import importlib.util
import sys

spec = importlib.util.spec_from_file_location(
    'dec', '/home/huangzhao/rk3568/ccs_dev/i2c_sniff_decode.py')
dec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dec)

EDID_HDR = [0, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0]
NAME = [ord(c) for c in 'MACROSILICON']

paths = []
for a in sys.argv[1:]:
    paths.extend(sorted(glob.glob(a)))

for path in paths:
    evs = dec.parse(path)
    if not evs:
        print(path, '空')
        continue
    txns = dec.decode(evs)
    gaps = [(evs[i + 1][0] - evs[i][0], evs[i][0])
            for i in range(len(evs) - 1)]
    huge = [g for g in gaps if g[0] > 0.3]
    hits = []
    for t in txns:
        bs = [b for b, a in t.get('bytes', [])]
        wr = bs and not (bs[0] & 1)
        for i in range(len(bs) - 8):
            if bs[i:i + 8] == EDID_HDR:
                hits.append(('EDID头', t['t'], i, len(bs)))
                break
        for i in range(len(bs) - len(NAME)):
            if bs[i:i + len(NAME)] == NAME:
                hits.append(('名字串', t['t'], i, len(bs)))
                break
        if wr and len(bs) > 30:
            hits.append(('长WR(%dB)' % len(bs), t['t'], 0, len(bs)))
    flag = '  <<<< 有复位特征!' if (hits or huge) else ''
    print('%s 事件%d 事务%d 超长空隙%d个 命中%s%s' % (
        path, len(evs), len(txns), len(huge), hits[:5], flag))
