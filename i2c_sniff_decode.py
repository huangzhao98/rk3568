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
    evs = []
    for line in open(path):
        m = EV.search(line)
        if m:
            edge = 1 if m.group(1) == 'RISING' else 0
            evs.append((float(m.group(3)), int(m.group(2)), edge))
    return evs


def decode(evs):
    """事件流 → 事务列表 [(t_start, addr, is_read, [(data,ack)...], stopped)]"""
    sda = scl = 1
    bits = []          # 当前字节流中的位
    txn = None         # 当前事务 dict
    txns = []
    for ts, line, val in evs:
        if line == 0:
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
    print(f'# {len(evs)} 个边沿事件', file=sys.stderr)
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
