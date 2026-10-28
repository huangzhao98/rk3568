"""ccs_sdk.network — 网络配置（/net_apply /net_confirm，带回滚防失联）。

对应管理页"网络设置"折叠块（网卡表格 + 静态/DHCP 表单）。

板载背景（ccs_webcam.py）：
- 网络由 systemd-networkd 管理；静态配置写 /etc/systemd/network/
  10-ccs-<iface>.network（按网卡一文件，优先于 20-wired 的 DHCP）。
- 只允许配置 eth0/eth1。mode=dhcp 时删除该文件；mode=static 时 addr 必须
  是 '192.168.1.50/24' 形式（带前缀），gw/dns 可空，dns 多个用空格或逗号。
- 回滚机制（NetRollback）：应用成功后板上启动 90 秒定时器，期间必须用
  返回的 token 调 /net_confirm 确认（在**新地址**下调！），超时未确认
  自动恢复旧配置并 restart systemd-networkd——防改错地址失联。
- /net_apply 成功回 200 HTML 页面（不是 303），确认链接里的 token 嵌在
  HTML 里，apply() 用正则抓出返回。
- 网卡列表没有独立 JSON API：ifaces() 抓管理页"网络设置"表格解析
  （页面结构变了会抛错，不会静默返回错数据）。
"""

import html
import re
import urllib.parse

from .client import CCSClient, CCSApiError


class Network(CCSClient):
    """网络配置接口。构造参数（host/port/timeout）见 CCSClient。

    ⚠ apply() 的副作用最大：板上立刻 restart systemd-networkd，改静态
    地址时旧连接（包括发起请求的这个 TCP 连接）随即断开；90 秒内不在
    新地址 confirm() 就自动回滚。自动化脚本里务必先想好确认路径。
    """

    def ifaces(self):
        """网卡列表，抓管理页网络表格（板上解析 ip -brief addr）。

        返回: list[dict]，每元素 {'iface', 'state', 'addr', 'mode'}：
            iface 接口名（只收 eth*/wlan*/can*）；state 如 'UP'/'DOWN'；
            addr 地址串（多个空格分隔，无地址是 '-'）；mode 是 '静态' 或
            'DHCP'（板上按 10-ccs-<iface>.network 里有无 Address= 判定）。
        抛出: CCSApiError——连接失败，或页面里没找到网络表格。
        副作用: 无。
        """
        page = self.get_text('/')
        # 表格头 <th>接口</th><th>状态</th>...，头行之后到 </table> 是数据行
        m = re.search(r'<th>接口</th>.*?</tr>(.*?)</table>', page, re.S)
        if not m:
            raise CCSApiError('管理页里没找到网络表格（页面结构变了？）')
        out = []
        for row in re.finditer(
                r'<tr><td>([^<]+)</td><td>([^<]*)</td>'
                r'<td>([^<]*)</td><td>([^<]*)</td></tr>', m.group(1)):
            # 板上嵌页面前过 esc()，这里 unescape 还原
            name, state, addr, mode = (html.unescape(x).strip() for x in row.groups())
            out.append({'iface': name, 'state': state,
                        'addr': addr, 'mode': mode})
        return out

    def apply(self, iface, mode='dhcp', addr='', gw='', dns=''):
        """POST /net_apply 下发网络配置，并启动板上 90 秒回滚定时器。

        参数:
            iface (str): 'eth0'/'eth1'（板上硬白名单，其他直接 err）。
            mode (str): 'dhcp'（删除静态配置文件）或 'static'。
            addr (str): static 必填，形如 '192.168.1.50/24'（带前缀）。
            gw (str): 网关单 IP，可空。dns (str): 空格/逗号分隔，可空。
        返回: str——确认 token（板上 secrets.token_hex(4)，8 个 hex 字符），
            90 秒内传给 confirm() 用。
        抛出: CCSApiError——参数被板上拒绝（303 ?err= 原文），或成功页里
            没抓到 token（页面结构变了）。
        副作用: 写/删 /etc/systemd/network/10-ccs-<iface>.network 并
            restart systemd-networkd——**旧地址的连接立刻断开**；板上
            90 秒未确认自动回滚旧配置。重复 apply 会取消前一个定时器。
        """
        form = {'iface': iface, 'mode': mode,
                'addr': addr, 'gw': gw, 'dns': dns}
        status, headers, body = self._request('POST', '/net_apply', form)
        if status == 303:                       # 参数错误 → ?err=
            loc = headers.get('Location', '')
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(loc).query)
            raise CCSApiError(q.get('err', [f'HTTP 303 {loc}'])[0])
        if status != 200:
            raise CCSApiError(
                f'POST /net_apply → HTTP {status}: {body.decode("utf-8", "replace")[:200]}')
        # 成功页是 HTML，token 只出现在确认链接里，正则抓
        m = re.search(r'net_confirm\?token=([0-9a-f]+)',
                      body.decode('utf-8', 'replace'))
        if not m:
            raise CCSApiError('net_apply 成功页里没抓到确认 token（页面结构变了？）')
        return m.group(1)

    def confirm(self, token):
        """GET /net_confirm?token=... 确认保留新配置。

        必须在 90 秒窗口内、**新地址**下调用（用新地址构造 Network 实例
        再调）。token 匹配则板上取消回滚定时器。

        参数: token (str)——apply() 的返回值。
        返回: bool——True=已保留新配置；False=无待确认配置或 token 不符。
            板上两种结果都回 200 HTML（不成功不是错误），所以按页面文本
            判定而不是状态码。
        抛出: CCSApiError（连接失败）。
        副作用: True 时板上取消回滚定时器，新配置永久生效。
        """
        text = self.get_text(f'/net_confirm?token={token}')
        return '已保留' in text
