"""ccs_sdk.gpio — GPIO 状态查询与白名单翻转（/gpio_vals /gpio_set）。

对应管理页"GPIO（原理图功能脚）"折叠块（全表 + 置高/置低按钮）。

板载背景（ccs_webcam.py，sysfs /sys/class/gpio）：
- 外部信号检测输入（只读）：LED_x_STATE = 外部 5V 信号经 TXS0104E 降到
  1.8V 进 SoC，同时直接驱动 LED。编号：GPIO2_B1=73 黄 / GPIO2_B2=74 绿 /
  GPIO2_C5=85 蓝 / GPIO2_C6=86 红。务必保持高阻输入——设为输出会与外部
  驱动对打，板上也只读暴露（GET /gpio_vals 实时电平 JSON）。
- 网页可翻转的白名单只有 {'WORKING_LEDEN_H'}，且该脚须无占用者
  （consumer 为 '' 或 'sysfs'）；电源/复位脚一律只读。不在白名单或
  被占用时 /gpio_set 回 403。
- GPIO 全表（/sys/kernel/debug/gpio 解析结果）只在管理页 HTML 里，
  list() 抓页面表格；实时电平用 read_inputs()（JSON API）。
"""

import html
import json
import re

from .client import CCSClient, CCSApiError

# 外部输入脚（与板上 GPIO_INPUTS 一致）：编号 → 名称
INPUTS = {73: 'LED_YELLOW_STATE', 74: 'LED_GREEN_STATE',
          85: 'LED_BLUE_STATE', 86: 'LED_RED_STATE'}


class Gpio(CCSClient):
    """GPIO 接口。构造参数（host/port/timeout）见 CCSClient。

    set() 只影响白名单内的输出脚（WORKING_LEDEN_H），只读、无副作用；
    list()/read_inputs() 都是纯查询。
    """

    def list(self):
        """GPIO 全表（只收有名脚），抓管理页 GPIO 表格。

        板上用正则解析 /sys/kernel/debug/gpio 后渲染成
        <tr><td>num</td><td>name</td><td>dir</td><td id=gvN>val</td>
        <td>consumer</td>...，这里按行抓回来（`id=gvN` 这个锚点是 GPIO 表
        独有，能把网络表/系统表的 <tr> 行区分开）。

        返回: list[dict]，每元素 {'num'(int), 'name', 'dir'('in'/'out'/''),
            'val'('hi'/'lo'/''), 'consumer'}。val 是页面生成时的快照，
            非实时——要实时电平用 read_inputs()。
        抛出: CCSApiError（连接失败）。
        副作用: 无。
        """
        page = self.get_text('/')
        out = []
        for row in re.finditer(
                r'<tr><td>(\d+)</td><td>([^<]*)</td><td>([^<]*)</td>'
                r'<td id=gv\d+>([^<]*)</td><td>([^<]*)</td>', page):
            num, name, d, v, consumer = row.groups()
            out.append({'num': int(num), 'name': html.unescape(name).strip(),
                        'dir': d, 'val': v,
                        'consumer': html.unescape(consumer).strip()})
        return out

    def set(self, num, val):
        """POST /gpio_set 翻转白名单脚电平。

        参数:
            num (int): GPIO 编号（须是白名单脚 WORKING_LEDEN_H 对应的号，
                具体编号看 list() 输出）。
            val (bool|int): 真值=置高，假值=置低。
        返回: str——板上文本 'gpio41 = hi' 之类。
        抛出: CCSApiError——403 '该脚不在白名单或已被占用'；500 写 sysfs
            失败（正文带 OSError 原文）。
        副作用: 改变板上实际引脚电平（sysfs 写 direction=out + value）。
        实现说明: 总是带 ajax=1——不带时板上成功回 303 整页跳转（网页表单
            用法），ajax 模式才回纯文本结果，SDK 要的是后者。
        """
        return self.post_text('/gpio_set',
                              {'num': str(int(num)),
                               'val': '1' if val else '0', 'ajax': '1'})

    def read_inputs(self):
        """GET /gpio_vals → 四路外部输入实时电平。

        返回: dict——{名称: bool}，如 {'LED_YELLOW_STATE': True, ...}
            （True=高电平）。名称由模块常量 INPUTS 映射；板上 JSON 键是
            编号字符串、值是 '0'/'1'。板上若某脚读失败会缺该键，
            这里同样缺（不补默认值）。
        抛出: CCSApiError（连接失败）。
        副作用: 无（板上已把这些脚常驻 export 为输入，读取不改方向）。
        """
        raw = self.get_json('/gpio_vals')
        return {INPUTS.get(int(num), f'gpio{num}'): v == '1'
                for num, v in raw.items()}

    def read_inputs_raw(self):
        """原始 {编号str: '0'/'1'} 字典（不映射名称、不转 bool）。

        返回/抛出同 read_inputs。副作用: 无。
        """
        return json.loads(self.get_text('/gpio_vals'))
