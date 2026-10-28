# api_gpio.md — GPIO（Gpio）

## 功能简介

查询原理图功能脚状态（方向/电平/占用者）、白名单脚翻转、四路外部输入
信号实时电平。

## HTTP 端点表

| 方法 | 路径 | 参数 | 返回 |
|---|---|---|---|
| GET | `/gpio_vals` | 无 | 200 JSON `{编号: '0'\|'1', ...}`，仅四路外部输入脚（73/74/85/86） |
| POST | `/gpio_set` | 表单 `num=N`, `val=0\|1`, `ajax`（存在则回纯文本不整页跳转） | ajax 模式：200 文本 `gpioN = hi/lo`；403 `该脚不在白名单或已被占用`；500 写失败 |

GPIO 全表（板上解析 `/sys/kernel/debug/gpio`）只在管理页 `GET /` 的
HTML 表格里，`list()` 抓页面解析。

## SDK 方法表（`ccs_sdk.gpio.Gpio`）

| 方法 | 说明 | 返回 |
|---|---|---|
| `list()` | GPIO 全表（只收有名脚） | `[{num, name, dir, val, consumer}]` |
| `set(num, val)` | 白名单脚置高/低（val 真值=高） | str（如 `gpio41 = hi`） |
| `read_inputs()` | 四路外部输入实时电平 | `{名称: bool}`（True=高） |
| `read_inputs_raw()` | 同上但不映射名称 | `{编号str: '0'/'1'}` |

模块常量 `INPUTS`：`{73: 'LED_YELLOW_STATE', 74: 'LED_GREEN_STATE',
85: 'LED_BLUE_STATE', 86: 'LED_RED_STATE'}`。

## 示例

```python
from ccs_sdk import Gpio

g = Gpio('192.168.1.222')
print(g.read_inputs())
# {'LED_YELLOW_STATE': False, 'LED_GREEN_STATE': True, ...}

for row in g.list():
    print(row['num'], row['name'], row['dir'], row['val'], row['consumer'])

print(g.set(41, 1))     # 仅当 gpio41 是 WORKING_LEDEN_H 且无占用者时成功
```

## 注意事项

- **LED_x_STATE 是外部输入，只读**：外部 5V 信号经 TXS0104E 电平转换降到
  1.8V 进 SoC（GPIO2_B1=73 黄 / GPIO2_B2=74 绿 / GPIO2_C5=85 蓝 /
  GPIO2_C6=86 红），同时直接驱动 LED。务必保持高阻输入——设为输出会与
  外部驱动对打，板上也不允许翻转这些脚。
- 网页可翻转的白名单只有 `WORKING_LEDEN_H`，且要求该脚 consumer 为
  `''` 或 `'sysfs'`（无占用者）；电源/复位脚一律只读。违规回 403，
  SDK 抛 `CCSApiError`。
- `/gpio_vals` 只含四路输入脚（板上常驻 export 为输入）；其他脚的实时
  电平看 `list()` 里的 `val`（页面生成时的快照，非实时）。
