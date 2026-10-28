# api_hid.md — 键鼠/触摸注入（Hid）

## 功能简介

板子通过 USB Type-C 以 configfs HID gadget 身份接被控上位机（免驱识别为
"CCS KVM Keyboard/Mouse"），把 HTTP 下发的报告写进 `/dev/hidg*`，
实现远程键鼠/触摸注入（KVM 场景）。

## HTTP 端点表

| 方法 | 路径 | 参数 | 返回 |
|---|---|---|---|
| POST | `/hid` | 表单 `d` = `设备字母 十进制字节 ...`（空格分隔，URL 编码） | 200 空 = 成功；503 = 上位机未连接/写失败；400 = 参数错误 |

设备字母与报告格式（与 `40_hid_gadget.sh` 描述符一致）：

| 字母 | 设备 | 报告长度 | 格式 |
|---|---|---|---|
| `k` | `/dev/hidg0` 键盘(boot) | 8 | `[mods, 0, k1..k6]` |
| `m` | `/dev/hidg1` 绝对坐标鼠标 | 6 | `[btn, xL, xH, yL, yH, wheel]` |
| `t` | `/dev/hidg2` 单点触摸 | 6 | `[tip\|inrange\|pad, contactID, xL, xH, yL, yH]` |
| `p` | `/dev/hidg3` 笔 digitizer | 5 | `[tip\|barrel\|inrange\|pad, xL, xH, yL, yH]` |

坐标 X/Y 均为 16 位小端，逻辑范围 0~32767。

## SDK 方法表（`ccs_sdk.hid.Hid`）

坐标对外一律用 0.0~1.0 归一化（与网页 `xy()` 一致）。

| 方法 | 走设备 | 说明 |
|---|---|---|
| `send_keyboard(mods=0, keys=())` | k | 完整键盘报告（最多 6 键，全状态上报） |
| `release_all()` | k | 全部键松开 |
| `key(name, mods=0, hold=0.05)` | k | 按一下指定键（'A'/'Enter'/'F5'...） |
| `mouse_move(x, y, down=False)` | p | 移动/拖拽（bit2=in-range 悬停显光标） |
| `mouse_down(x,y)` / `mouse_up(x,y)` | p | 左键按下/松开 |
| `click(x, y, hold=0.05)` | p | 左键点一下 |
| `right_click(x, y)` | t | 右键（触摸流式长按 ~1.3s） |
| `touch(x, y, down=True)` / `tap(x, y)` | t | 单点触摸按下/抬起、点一下 |
| `mouse_abs(x, y, btn=0, wheel=0)` | m | 绝对鼠标（含滚轮；Windows 下锁主屏） |

常量：`KEYMAP`（键名→usage ID，与网页 JS 一致）、`MODS`（修饰键位掩码，
如 `MODS['ControlLeft']`）。

## 示例

```python
from ccs_sdk import Hid, MODS

h = Hid('192.168.1.222')
h.click(0.5, 0.5)                 # 屏幕中心左键
h.right_click(0.5, 0.5)           # 中心右键
h.key('Enter')                    # 回车
h.key('F4', mods=MODS['AltLeft']) # Alt+F4
h.mouse_move(0.2, 0.3, down=True) # 拖拽起点
h.mouse_move(0.8, 0.6, down=True)
h.mouse_up(0.8, 0.6)              # 松开
```

## 注意事项

- **键盘是全状态上报**：按下后必须再发空报告（`release_all()`）才算松开，
  否则上位机认为键一直按着。`key()` 已内置按下→松开。
- 网页鼠标模式走 `p`（笔 digitizer）而不是 `m`（绝对鼠标）：Windows 把
  绝对坐标鼠标整体锁死在主屏（0~32767 全范围落主屏，够不到扩展副屏），
  digitizer 类按显示器关联才落得到副屏。`mouse_abs()` 仅建议用于滚轮
  或确认上位机是 Linux/单屏的场景。
- 右键不走笔（Windows 不吃笔的侧键语义），`right_click()` 用触摸流式
  长按实现——必须 ~60Hz 连发约 1.3 秒，只发一次 Windows 判联系点失效
  不触发。
- 上位机未连接/重新枚举时板上写 hidg 失败回 503，SDK 抛 `CCSApiError`；
  板端下次写入会自动重开设备（断连自适应）。
- 板上 /hid 开 HTTP/1.1 keep-alive 复用连接（50Hz 报告场景曾 55ms/次），
  SDK 的 `urllib` opener 每次新建连接，高频注入场景注意节奏。
