# api_image.md — 画质补偿：亮度 / 锐化（Image）

## 功能简介

采集端画质数字补偿：亮度增益（VGA 通道模拟采样偏暗的补偿）与锐化
（VGA 采样模糊的补偿），即时生效并持久化到板上 `/etc/ccs_brightness`、
`/etc/ccs_sharp`。

## HTTP 端点表

| 方法 | 路径 | 参数 | 返回 |
|---|---|---|---|
| GET | `/brightness` | `val=20~200`（默认 100） | 200 文本 `亮度 N%` |
| GET | `/sharpen` | `val=0~100`（默认 0） | 200 文本 `锐化 N` |

`val` 非数字时板上静默忽略（仍回当前生效值）；越界自动钳位到
[20,200] / [0,100]。

## SDK 方法表（`ccs_sdk.image.Image`）

| 方法 | 说明 | 返回 |
|---|---|---|
| `set_brightness(val)` | 亮度增益 20~200%，100=原始 | str（`亮度 N%`） |
| `set_sharpen(val)` | 锐化强度 0~100，0=关闭 | str（`锐化 N`） |

## 示例

```python
from ccs_sdk import Image

img = Image('192.168.1.222')
print(img.set_brightness(130))   # VGA 偏暗时提亮
print(img.set_sharpen(40))       # 补偿采样模糊
print(img.set_brightness(100))   # 复位
print(img.set_sharpen(0))
```

## 注意事项

- 亮度只作用在 Y 分量（色度不动，色饱和度不变）。MS9288C 的 SCL/SDA
  原理图未接，调不了芯片增益，只能采集端数字补偿。
- 锐化是 unsharp mask（`y + k*(y - 十字模糊)`，k=sharp/50），只作用 Y。
- 硬编（vepu）路径下补偿由 C shim 在编码拷贝趟里完成（~3ms），无额外
  开销；老 shim 回退 Python numpy（锐化 ~75ms/帧，仅应急）。
- 两参数都持久化，重启服务后仍生效；复位用 100 / 0。
