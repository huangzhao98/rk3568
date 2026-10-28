"""ccs_sdk.hid — USB HID gadget 键鼠/触摸注入（POST /hid）。

对应管理页"实时预览"区块下方的"启用键鼠控制"（KVM）功能。

板载背景（ccs_dev/40_hid_gadget.sh，configfs HID gadget；被控上位机免驱
识别为 "CCS KVM Keyboard/Mouse"）：
- /dev/hidg0 'k' 键盘：boot protocol，8 字节报告 [mods, 0, k1..k6]
- /dev/hidg1 'm' 绝对坐标鼠标：6 字节报告 [btn, xL, xH, yL, yH, wheel]
  （网页未用此设备；滚轮只有这路有字节位）
- /dev/hidg2 't' 单点触摸 digitizer：6 字节 [tip|inrange|pad, contactID,
  xL, xH, yL, yH]
- /dev/hidg3 'p' 笔 digitizer：5 字节 [tip|barrel|inrange|pad, xL, xH,
  yL, yH]

坐标逻辑范围 0~32767（本模块对外用 0.0~1.0 归一化坐标，与网页 xy() 一致）。

为什么鼠标默认走 hidg3 笔而不是 hidg1 鼠标（网页实测结论）：Windows 把
"绝对坐标鼠标"整体锁死在主屏（0~32767 全范围都落主屏，永远够不到扩展
副屏），digitizer 类（笔/触摸）按显示器关联走，实测落在副屏；笔比触摸
多"悬停有光标"。右键不走笔（Windows 不吃侧键语义），用 hidg2 触摸流式
长按触发 Windows 原生"按住=右键"手势——必须 ~60Hz 连发约 1.3 秒，只发
一次按下再静默会被判联系点失效，长按不触发（实测）。

POST /hid 表单：d = "设备字母 十进制字节 ..."（空格分隔、URL 编码）。
返回 200 空 = 成功 / 503 = 上位机未连接或写入失败 / 400 = 参数错误。
"""

import time

from .client import CCSClient

# 与网页 KVM_JS 的 KEYMAP/MODS 一致（USB HID usage ID）。
# 键名是浏览器 KeyboardEvent.code 风格（'KeyA'/'Digit1'/'Enter'...），
# key() 方法额外接受单字符简写（'A'/'5'）。
KEYMAP = {
    'KeyA': 4, 'KeyB': 5, 'KeyC': 6, 'KeyD': 7, 'KeyE': 8, 'KeyF': 9,
    'KeyG': 10, 'KeyH': 11, 'KeyI': 12, 'KeyJ': 13, 'KeyK': 14, 'KeyL': 15,
    'KeyM': 16, 'KeyN': 17, 'KeyO': 18, 'KeyP': 19, 'KeyQ': 20, 'KeyR': 21,
    'KeyS': 22, 'KeyT': 23, 'KeyU': 24, 'KeyV': 25, 'KeyW': 26, 'KeyX': 27,
    'KeyY': 28, 'KeyZ': 29,
    'Digit1': 30, 'Digit2': 31, 'Digit3': 32, 'Digit4': 33, 'Digit5': 34,
    'Digit6': 35, 'Digit7': 36, 'Digit8': 37, 'Digit9': 38, 'Digit0': 39,
    'Enter': 40, 'Backspace': 42, 'Tab': 43, 'Space': 44, 'Minus': 45,
    'Equal': 46, 'BracketLeft': 47, 'BracketRight': 48, 'Backslash': 49,
    'Semicolon': 51, 'Quote': 52, 'Backquote': 53, 'Comma': 54, 'Period': 55,
    'Slash': 56, 'CapsLock': 57,
    'F1': 58, 'F2': 59, 'F3': 60, 'F4': 61, 'F5': 62, 'F6': 63, 'F7': 64,
    'F8': 65, 'F9': 66, 'F10': 67, 'F11': 68, 'F12': 69,
    'Insert': 73, 'Home': 74, 'PageUp': 75, 'Delete': 76, 'End': 77,
    'PageDown': 78, 'ArrowRight': 79, 'ArrowLeft': 80, 'ArrowDown': 81,
    'ArrowUp': 82,
}
MODS = {
    'ControlLeft': 1, 'ShiftLeft': 2, 'AltLeft': 4, 'MetaLeft': 8,
    'ControlRight': 16, 'ShiftRight': 32, 'AltRight': 64, 'MetaRight': 128,
}


def _xy32767(x, y):
    """归一化 0.0~1.0 → 0~32767（钳位到边界），与网页 xy() 一致。

    网页是 (clientX-rect.left)/rect.width 算比例再乘 32767；这里同样
    先 clamp 再取整，越界坐标落边缘而不是报错。
    """
    xi = round(min(max(float(x), 0.0), 1.0) * 32767)
    yi = round(min(max(float(y), 0.0), 1.0) * 32767)
    return xi, yi


class Hid(CCSClient):
    """键鼠/触摸注入。构造参数（host/port/timeout）见 CCSClient。

    所有方法返回 True=板上写入成功；上位机未连接 USB 时板上写 hidg
    失败回 503，post_text 抛 CCSApiError。
    副作用: 全部方法都会在被控上位机上产生真实输入事件，谨慎调用。
    """

    def _send(self, which, *data):
        """发一条 HID 报告：POST /hid d='<which> b0 b1 ...'。

        参数:
            which (str): 设备字母 'k'/'m'/'t'/'p'。
            *data (int): 报告各字节（& 0xff 截断）。
        返回: True（200 成功）。
        抛出: ValueError——设备字母非法；CCSApiError——400 参数错误 /
            503 上位机未连接。
        """
        if which not in ('k', 'm', 't', 'p'):
            raise ValueError("设备须为 'k'/'m'/'t'/'p'")
        d = ' '.join([which] + [str(b & 0xff) for b in data])
        self.post_text('/hid', {'d': d})
        return True

    # ---- 键盘（hidg0，8 字节 boot 报告）----

    def send_keyboard(self, mods=0, keys=()):
        """发完整键盘报告 [mods, 0, k1..k6]。

        参数:
            mods (int): 修饰键位掩码，用 MODS 组合，
                如 MODS['ControlLeft'] | MODS['ShiftLeft']。
            keys (Iterable[int]): 普通键 usage ID（见 KEYMAP），最多 6 个
                （boot 协议 6KRO），不足补 0。
        返回: True。
        抛出: CCSApiError。
        副作用: 键盘是**全状态上报**——报告里没列的键视为松开；要全部
            松开必须再发空报告（release_all()），否则上位机认为键一直按着。
        """
        ks = list(keys)[:6]
        ks += [0] * (6 - len(ks))
        return self._send('k', mods, 0, *ks)

    def release_all(self):
        """发空键盘报告，全部键松开。

        返回: True。抛出: CCSApiError。
        副作用: 松开的修饰键/普通键在上位机即时生效。
        """
        return self.send_keyboard(0, ())

    def key(self, name, mods=0, hold=0.05):
        """按一下指定键（按下 → hold 秒 → 松开，完整击键）。

        参数:
            name (str): 键名。接受 KEYMAP 里的完整名（'KeyA'、'Enter'、
                'F5'、'ArrowUp'...）或单字符简写（'A'~'Z' 不分大小写、
                '0'~'9'）。
            mods (int): 修饰键位掩码（见 MODS），如 Alt+F4 用
                key('F4', mods=MODS['AltLeft'])。
            hold (float): 按住时长秒数，默认 0.05。
        返回: True。
        抛出: ValueError——未知键名；CCSApiError。
        副作用: 上位机收到一次完整按键。
        """
        code = self._lookup(name)
        self.send_keyboard(mods, [code])
        time.sleep(hold)
        return self.release_all()

    @staticmethod
    def _lookup(name):
        """键名 → usage ID。单字符自动映射 Key?/Digit?，其余须全名。"""
        if name in KEYMAP:
            return KEYMAP[name]
        if len(name) == 1:
            c = name.upper()
            if 'A' <= c <= 'Z':
                return KEYMAP['Key' + c]
            if '0' <= c <= '9':
                return KEYMAP['Digit' + c]
        raise ValueError(f'未知键名: {name!r}（用 KEYMAP 里的名字，如 KeyA/Enter/F1）')

    # ---- 鼠标（hidg3 笔 digitizer，与网页鼠标模式一致）----

    def mouse_move(self, x, y, down=False):
        """移动指针到绝对坐标 (x, y)，可选按住左键（拖拽）。

        走 hidg3 笔 digitizer，报告 bits = 4(in-range) | 1(tip，左键)——
        与网页 sendMouse 一致。in-range 位保证悬停时上位机显示光标。

        参数:
            x, y (float): 归一化坐标 0.0~1.0（自动钳位到边界）。
            down (bool): True=按住左键移动（拖拽）。
        返回: True。抛出: CCSApiError。
        """
        xi, yi = _xy32767(x, y)
        bits = 4 | (1 if down else 0)
        return self._send('p', bits, xi & 0xff, xi >> 8, yi & 0xff, yi >> 8)

    def mouse_down(self, x=None, y=None):
        """按住左键。

        参数: x, y (float|None)——给坐标则先移动再按下；省略则只改按键
            位、坐标发 0（网页退出 KVM 控制时同款写法）。
        返回: True。抛出: CCSApiError。
        """
        if x is None:
            return self._send('p', 5, 0, 0, 0, 0)
        return self.mouse_move(x, y, down=True)

    def mouse_up(self, x=None, y=None):
        """松开左键（笔保持悬停 in-range，光标仍显示）。

        参数/返回/抛出同 mouse_down。
        """
        if x is None:
            return self._send('p', 4, 0, 0, 0, 0)
        return self.mouse_move(x, y, down=False)

    def click(self, x, y, hold=0.05):
        """在 (x, y) 左键点一下（按下 → hold 秒 → 松开）。

        参数: x, y (float) 归一化坐标；hold (float) 按住时长。
        返回: True。抛出: CCSApiError。
        """
        self.mouse_move(x, y, down=True)
        time.sleep(hold)
        return self.mouse_move(x, y, down=False)

    def right_click(self, x, y, interval=0.06, count=22):
        """在 (x, y) 点右键。

        实现与网页 rightClick() 一致：hidg2 触摸流式长按——以 ~60Hz
        连发 count 次 tip|in-range(3) 报告（默认 22 次 ≈ 1.3s），再发
        抬起(2)和全抬(0)。Windows 原生"按住=右键"手势，菜单落在触摸点
        本身，不依赖键盘焦点。只发一次会被 Windows 判联系点失效，
        长按不触发（实测），所以这里必须占用调用线程约 1.3 秒。

        参数: x, y (float) 归一化坐标；interval (float) 报告间隔秒；
            count (int) 按住报告次数。
        返回: True。抛出: CCSApiError。
        副作用: 阻塞调用线程约 interval*count 秒。
        """
        xi, yi = _xy32767(x, y)
        xl, xh, yl, yh = xi & 0xff, xi >> 8, yi & 0xff, yi >> 8
        for _ in range(count):
            self._send('t', 3, 0, xl, xh, yl, yh)
            time.sleep(interval)
        self._send('t', 2, 0, xl, xh, yl, yh)
        return self._send('t', 0, 0, 0, 0, 0, 0)

    # ---- 触摸（hidg2 单点 digitizer，6 字节报告）----

    def touch(self, x, y, down=True):
        """单点触摸按下/抬起。

        报告 bits = bit0 tip | bit1 in-range；抬起时坐标随意（网页抬起
        仍带当前坐标）。

        参数: x, y (float) 归一化坐标；down (bool) True=按下 False=抬起。
        返回: True。抛出: CCSApiError。
        """
        xi, yi = _xy32767(x, y)
        bits = (1 if down else 0) | 2
        return self._send('t', bits, 0, xi & 0xff, xi >> 8, yi & 0xff, yi >> 8)

    def tap(self, x, y, hold=0.05):
        """在 (x, y) 触屏点一下（按下 → hold 秒 → 抬起）。

        参数/返回/抛出同 click。
        """
        self.touch(x, y, down=True)
        time.sleep(hold)
        return self.touch(x, y, down=False)

    # ---- 绝对坐标鼠标（hidg1，6 字节；网页未用，滚轮只能走这路）----

    def mouse_abs(self, x, y, btn=0, wheel=0):
        """hidg1 绝对坐标鼠标报告 [btn, xL, xH, yL, yH, wheel]。

        参数:
            x, y (float): 归一化坐标。
            btn (int): 按键位掩码 bit0=左 bit1=右 bit2=中。
            wheel (int): 滚轮增量，有符号（-1 下滚 / +1 上滚），& 0xff
                发补码字节。
        返回: True。抛出: CCSApiError。
        副作用/注意: Windows 把绝对坐标鼠标整体锁死在主屏（0~32767 全
            范围都落主屏，够不到扩展副屏）——这是网页鼠标模式改用笔
            digitizer 的原因。仅在确认上位机单屏/是 Linux，或要用滚轮
            时用本方法。
        """
        xi, yi = _xy32767(x, y)
        return self._send('m', btn, xi & 0xff, xi >> 8, yi & 0xff, yi >> 8,
                          wheel & 0xff)
