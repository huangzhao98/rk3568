"""ccs_sdk.edid — 采集芯片 EDID 管理（/edid_apply /edid_build /edid_preset）。

对应管理页"EDID 管理"折叠块（明文设置表单 + 高级 HEX 编辑 + 恢复出厂）。

板载背景（ccs_webcam.py）：
- EDID 存在采集芯片（TC358743）RAM 里，板子断电即丢（开机由 ccs_init.sh
  重写出厂 720p EDID /usr/sbin/edid_720p.hex.txt）。
- 应用新 EDID 后源端会重新协商输出，板端自动触发重对齐，画面中断几秒
  属正常；对齐序列会保存/恢复当前 EDID，不会冲掉用户设置。
- 最近一次成功应用的 EDID 存 /tmp/ccs_last_edid.txt，切通道时补写到新芯片。
- 分辨率预设只有 '720p60' / '1080p60' / '1080p30' 三档；网页警告：1080p60
  需 4-lane MIPI，当前 EVB 为 2-lane 仅支持 720p60/1080p30。
- 这三个端点都是 303 重定向（成功 ?msg= / 失败 ?err=），由 post_msg 解析。
- 读当前 EDID 没有独立 API：芯片 hex 文本嵌在管理页 HTML 的
  <textarea name=edid> 里，get_hex() 抓管理页解析（页面结构变了会抛错，
  不会静默返回错数据）。
"""

import html
import re

from .client import CCSClient, CCSApiError

TIMINGS = ('720p60', '1080p60', '1080p30')   # 与板上 EDID_TIMINGS 一致


class Edid(CCSClient):
    """EDID 管理接口。构造参数（host/port/timeout）见 CCSClient。

    注意：apply_hex/build_apply/factory_reset 都有副作用——源端重新协商
    输出 + 板端自动重对齐，画面会中断几秒。
    """

    def get_hex(self):
        """读芯片当前 EDID。

        实现：GET / 抓管理页 <textarea name=edid> 内容（板上用
        v4l2-ctl --get-edid 读芯片后 HTML 转义嵌入页面，这里 unescape 回来）。

        返回: str——hex 文本（v4l2-ctl --get-edid 格式，每行 16 字节，
            结尾带换行，可直接喂回 apply_hex）。
        抛出: CCSApiError——连接失败，或页面里没找到文本框（页面结构变了）。
        副作用: 无。
        """
        page = self.get_text('/')
        m = re.search(r'<textarea name=edid>(.*?)</textarea>', page, re.S)
        if not m:
            raise CCSApiError('管理页里没找到 EDID 文本框（页面结构变了？）')
        return html.unescape(m.group(1)).strip() + '\n'

    def apply_hex(self, hextext, fix=True):
        """POST /edid_apply 直接应用 HEX EDID。

        参数:
            hextext (str): hex 文本（空格分隔、每行 16 字节或任意空白
                分隔均可，板上按空白 split 解析）；长度须为 128 或 256 字节。
            fix (bool): True 时带 fix 复选——板上加 --fix-edid-checksums
                自动修复校验和（读回验证时跳过每块最后一字节比较）。
        返回: str——板上 msg 'EDID 已生效，已触发重对齐'。
        抛出: CCSApiError——hex 格式错误/长度不对/读回不一致等
            （err 文本原样带出）。
        副作用: 写芯片 RAM（断电即丢）；源端重新协商输出；板端自动触发
            重对齐，画面中断几秒；成功应用的内容会存 /tmp/ccs_last_edid.txt
            供切通道时补写。
        """
        form = {'edid': hextext}
        if fix:
            form['fix'] = '1'   # 板上判定是 'fix' in form，值任意
        return self.post_msg('/edid_apply', form)

    def build_apply(self, name='CCS-HDMI', timing='720p60', mfr='CCS',
                    product=0x0001, serial=1, year=2026, week=1,
                    hcm=16, vcm=9):
        """POST /edid_build 按明文字段生成 EDID 并应用。

        参数（范围即板上 build_edid 的校验规则，超限板上回 err）:
            name (str): 监视器名称，1~13 个可打印 ASCII。
            timing (str): 分辨率预设，'720p60'/'1080p60'/'1080p30'
                （本方法先本地校验，非法直接 ValueError 不发请求）。
            mfr (str): 厂商 ID，3 个英文字母（板上转大写）。
            product (int): 产品码 0~0xFFFF。**板上按 16 进制解析表单值**
                （int(x, 16)），所以这里把 int 格式化为 4 位 hex 字符串
                再发——product=0x1234 发 '1234'，板上读回 0x1234；
                若误传十进制字符串语义会对不上。
            serial (int): 序列号 0~0xFFFFFFFF，板上按 10 进制解析。
            year (int): 生产年份 1991~2245。week (int): 0~53。
            hcm, vcm (int): 屏幕物理尺寸 cm，各 1~255。
        返回: str——板上 msg 'EDID 已生成并生效（名称/档位），重对齐中'。
        抛出: ValueError——timing 非法（本地拦截）；
            CCSApiError——板上校验失败（消息是 '参数错误: ...'）。
        副作用: 同 apply_hex（写芯片 + 重协商 + 自动重对齐）。
        """
        if timing not in TIMINGS:
            raise ValueError(f'timing 须为 {TIMINGS} 之一')
        form = {'name': name, 'timing': timing, 'mfr': mfr,
                'product': f'{product:04x}', 'serial': str(serial),
                'year': str(year), 'week': str(week),
                'hcm': str(hcm), 'vcm': str(vcm)}
        return self.post_msg('/edid_build', form)

    def factory_reset(self):
        """POST /edid_preset preset=720p：恢复出厂 720p EDID (CCS-HDMI)。

        场景：设了带不动的主信号（如 2-lane MIPI 上 1080p60）采不到帧时
        用它兜底恢复。

        返回: str——板上 msg '已恢复 720p EDID，重对齐中'。
        抛出: CCSApiError。
        副作用: 同 apply_hex（写芯片 + 自动重对齐，画面中断几秒）。
        """
        return self.post_msg('/edid_preset', {'preset': '720p'})
