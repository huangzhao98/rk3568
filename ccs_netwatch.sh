#!/bin/sh
# ccs_netwatch.sh — 开机网络兜底
# eth0 配置为静态地址（/etc/systemd/network/10-ccs-eth0.network 含 Address=）时：
#   1. 等地址就绪（最多 15 秒），随后 30 秒窗口内反复 ping 网关；
#      通则保持静态退出。
#   2. 网关一直不通 → 回退 DHCP（配置文件改名 .disabled，让 20-wired.network
#      的 DHCP=yes 生效），最多等 25 秒拿地址。
#   3. DHCP 也拿不到地址（网络里没有 DHCP 服务器）→ 恢复静态配置兜底，
#      保证板子至少有静态地址可达，OLED 的 IP 行不为空。
# 回退期间 OLED 的 IP 行会自动跟随新地址。网页重新应用静态配置会重新生成本文件。
# 用法: ccs_netwatch.sh [网关IP]   （默认 192.168.1.1，仅供测试覆盖）
# 测试: DRYRUN=1 ccs_netwatch.sh 192.168.1.99

CONF=/etc/systemd/network/10-ccs-eth0.network
IFACE=eth0
GW=${1:-192.168.1.1}

have_addr() { ip -4 addr show dev "$IFACE" 2>/dev/null | grep -q 'inet '; }

[ -f "$CONF" ] || { echo "[netwatch] 无静态配置(已是DHCP)，退出"; exit 0; }
grep -q '^Address=' "$CONF" || { echo "[netwatch] 无 Address=，退出"; exit 0; }

# 等地址就绪（最多 15 秒）
i=0
while [ $i -lt 15 ]; do
    have_addr && break
    i=$((i+1)); sleep 1
done
if ! have_addr; then
    # 静态地址都没配上（多半是网线没插），不动配置，插上网线 networkd 会自动应用
    echo "[netwatch] $IFACE 无地址（可能未插网线），保持静态配置，退出"
    exit 0
fi

# 30 秒窗口：10 次 ping，每次超时 2 秒、间隔 1 秒
i=0
while [ $i -lt 10 ]; do
    if ping -c1 -W2 -I "$IFACE" "$GW" >/dev/null 2>&1; then
        echo "[netwatch] 静态地址可用（网关 $GW 可达），保持静态"
        exit 0
    fi
    i=$((i+1)); sleep 1
done

echo "[netwatch] 静态地址不通（网关 $GW 不可达），回退 DHCP"
if [ "$DRYRUN" = 1 ]; then
    echo "[netwatch] DRYRUN: 将执行 mv $CONF $CONF.disabled && systemctl restart systemd-networkd"
    exit 2
fi
mv "$CONF" "$CONF.disabled"
systemctl restart systemd-networkd

# 等 networkd 先摘掉静态地址，避免把残留的旧地址误判为 DHCP 成果
sleep 3
# 等 DHCP 拿地址（最多 25 秒）
i=0
while [ $i -lt 25 ]; do
    have_addr && { echo "[netwatch] DHCP 已获得地址，保持 DHCP"; exit 0; }
    i=$((i+1)); sleep 1
done

# DHCP 也没有（网络里无 DHCP 服务器），恢复静态兜底
echo "[netwatch] DHCP 25 秒未拿到地址，恢复静态配置兜底"
mv "$CONF.disabled" "$CONF"
systemctl restart systemd-networkd
i=0
while [ $i -lt 10 ]; do
    have_addr && break
    i=$((i+1)); sleep 1
done
have_addr && echo "[netwatch] 已恢复静态地址" || echo "[netwatch] 静态地址恢复失败（检查网线）"
exit 0
