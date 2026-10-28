# api_network.md — 网络配置（Network）

## 功能简介

eth0/eth1 的 DHCP/静态 IP 配置，由板上 systemd-networkd 执行
（静态配置写 `/etc/systemd/network/10-ccs-<iface>.network`，优先于
20-wired 的 DHCP）。带 90 秒确认回滚机制防失联。

## HTTP 端点表

| 方法 | 路径 | 参数 | 返回 |
|---|---|---|---|
| POST | `/net_apply` | 表单 `iface`,`mode`,`addr`,`gw`,`dns` | 成功：200 HTML（内含 `net_confirm?token=XXXX` 确认链接）；失败：303 `?err=...` |
| GET | `/net_confirm` | `token=XXXX` | 200 HTML：`已保留` 或 `无待确认的配置（或 token 不符）` |

`iface` 只允许 `eth0`/`eth1`（板上硬白名单）。`mode=dhcp` 时删除静态配置
文件；`mode=static` 时 `addr` 必须是 `192.168.1.50/24` 形式（带前缀），
`gw` 单 IP 可空，`dns` 可多个（空格或逗号分隔）。

网卡列表没有独立 JSON API：`ifaces()` 抓管理页 `GET /` 的"网络设置"表格。

## SDK 方法表（`ccs_sdk.network.Network`）

| 方法 | 说明 | 返回 |
|---|---|---|
| `ifaces()` | 网卡列表 | `[{iface, state, addr, mode}]`，mode 为 `'静态'`/`'DHCP'` |
| `apply(iface, mode, addr, gw, dns)` | 下发配置并启动 90s 回滚定时器 | token 字符串 |
| `confirm(token)` | 确认保留新配置 | bool（True=已保留） |

## 示例

```python
from ccs_sdk import Network

n = Network('192.168.1.222')
print(n.ifaces())

token = n.apply('eth0', 'static', addr='192.168.1.50/24',
                gw='192.168.1.1', dns='192.168.1.1')
# 旧地址连接已断！用新地址确认：
n2 = Network('192.168.1.50')
print(n2.confirm(token))    # True = 保留；超时未确认板上自动回滚
```

## 注意事项

- **应用静态地址后旧连接立刻断开**（板上 restart systemd-networkd）。
  确认必须在 90 秒内、在**新地址**下调 `confirm(token)`；超时未确认
  板上自动恢复旧配置并重启 networkd。
- token 由板上 `secrets.token_hex(4)` 生成（8 个 hex 字符），只有一次
  有效窗口；重复 apply 会取消前一个定时器。
- 改回 DHCP：`apply('eth0', 'dhcp')`（addr/gw/dns 留空即可）。
- token 也可以从管理页直接点确认链接（apply 成功页里就有）。
