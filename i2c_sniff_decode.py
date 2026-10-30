#!/usr/bin/env python3
"""I2C 嗅探解码器：把 gpiomon 边沿日志还原成 I2C 事务。

抓包（板上，内核需把 i2c3 引脚设为 GPIO 输入）：
    gpiomon -b gpiochip1 0 1 > /tmp/i2c_capture.log
    # gpiochip1 line0 = GPIO1_A0 = I2C3_SDA, line1 = GPIO1_A1 = I2C3_SCL

解码：
    python3 i2c_sniff_decode.py /tmp/i2c_capture.log

gpiomon 4.19 输出格式：
    event:  RISING EDGE offset: 0 timestamp: [    12345.678901234]
协议规则：
    START = SCL 高时 SDA 下降沿；STOP = SCL 高时 SDA 上升沿；
    数据位 = SCL 上升沿采样 SDA；每 9 位一组 = 8 数据 + ACK(0)/NACK(1)
"""
import re
import sys

EV = re.compile(r'event:\s+(RISING|FALLING) EDGE offset:\s*(\d+)\s+timestamp:\s*\[\s*([\d.]+)\]')


def parse(path):
    """支持三种输入：
    1. i2c_sniff.ko 轮询模式: '<ns> 2 <电平>'（电平 bit0=SDA bit1=SCL，
       两线真值同一次寄存器读取，无竞态，首推）
    2. i2c_sniff.ko IRQ 原始边沿流: '<ns> <0=SDA/1=SCL> <0/1>'（有 ~5% 回读竞态）
    3. gpiomon 日志: event: RISING/FALLING EDGE offset: N timestamp: [t]
    """
    evs = []
    prev_lvl = None
    for line in open(path):
        p = line.split()
        if len(p) == 3 and p[0][0].isdigit():
            if int(p[1]) == 2:  # 轮询记录：展开成单线事件
                t = int(p[0]) / 1e9
                lvl = int(p[2])
                if prev_lvl is None:
                    prev_lvl = lvl
                    continue
                diff = lvl ^ prev_lvl
                if diff & 2:  # SCL 先
                    evs.append((t, 1, (lvl >> 1) & 1))
                if diff & 1:  # SDA 后（START/STOP 判定用新 SCL 电平）
                    evs.append((t, 0, lvl & 1))
                prev_lvl = lvl
                continue
            evs.append((int(p[0]) / 1e9, int(p[1]), int(p[2])))
            continue
        m = EV.search(line)
        if m:
            edge = 1 if m.group(1) == 'RISING' else 0
            evs.append((float(m.group(3)), int(m.group(2)), edge))
    evs.sort(key=lambda e: e[0])
    return evs


def decode(evs):
    """电平事件流 → 事务列表。输入已是 (t, line, val) 电平序列。"""
    sda = scl = 1
    bits = []
    txn = None
    txns = []
    for ts, line, val in evs:
        if line == 0:
            if val == sda:
                continue
            sda = val
            if scl == 1:                    # SCL 高时 SDA 变化 = START/STOP
                if val == 0:                # START / repeated START
                    bits = []
                    txn = {'t': ts, 'bytes': []}
                    txns.append(txn)
                else:                       # STOP
                    if txn is not None:
                        txn['stop'] = True
                    txn = None
                    bits = []
        else:
            if val == scl:
                continue
            prev = scl
            scl = val
            if val == 1 and prev == 0 and txn is not None:  # SCL 上升沿采样
                bits.append(sda)
                if len(bits) == 9:
                    b = 0
                    for bit in bits[:8]:
                        b = (b << 1) | bit
                    txn['bytes'].append((b, bits[8] == 0))  # (数据, ACK?)
                    bits = []
    return txns


def render(txns):
    out = []
    for txn in txns:
        bs = txn.get('bytes', [])
        if not bs:
            continue
        addr_b, ack0 = bs[0]
        addr, rd = addr_b >> 1, addr_b & 1
        t = txn['t']
        if rd:
            data = ' '.join(f'{b:02x}{"" if a else "!"}' for b, a in bs[1:])
            out.append(f'{t:16.6f}  addr=0x{addr:02x} RD  ack={"Y" if ack0 else "N"}  data: {data}')
        else:
            data = ' '.join(f'{b:02x}{"" if a else "!"}' for b, a in bs[1:])
            out.append(f'{t:16.6f}  addr=0x{addr:02x} WR  ack={"Y" if ack0 else "N"}  data: {data}')
    return out


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/i2c_capture.log'
    evs = parse(path)
    # 轮询模式的记录是完整电平不是边沿，"最小间隔"指标对 IRQ 模式才有意义
    print(f'# {len(evs)} 个电平事件', file=sys.stderr)
    if len(evs) > 1:
        dt = (evs[-1][0] - evs[0][0])
        print(f'# 时长 {dt:.3f}s, 平均边沿间隔 {dt / max(len(evs) - 1, 1) * 1e6:.1f}us',
              file=sys.stderr)
        gaps = sorted((evs[i + 1][0] - evs[i][0] for i in range(len(evs) - 1)))
        print(f'# 最小边沿间隔 {gaps[0] * 1e6:.2f}us (小于 1us 说明可能丢边沿)', file=sys.stderr)
    txns = decode(evs)
    for line in render(txns):
        print(line)
    print(f'# 共 {len(txns)} 个事务', file=sys.stderr)


if __name__ == '__main__':
    main()
