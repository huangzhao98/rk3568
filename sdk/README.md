# ccs_sdk — RK3568 CCS 采集板 HTTP API Python SDK

面向上位机/自动化脚本的 SDK。板上跑 `ccs_webcam.py` 管理服务
（默认 `http://192.168.1.222:8080`），所有功能通过 HTTP API 暴露。
本 SDK 的端点、参数名、返回格式全部以 `ccs_webcam.py` 实际代码为准。

## 安装

零第三方依赖（只用标准库 `urllib` / `http.client`），Python 3.6+。
把整个 `sdk/` 目录拷到项目里，在 `sdk/` 目录下（或把 `sdk/` 加进
`sys.path`）直接 import 即可：

```python
from ccs_sdk import Video
print(Video('192.168.1.222').stat())
```

每个模块也可单独拷贝使用（`client.py` 是公共基类，需一起拷）：

```python
from ccs_sdk.video import Video        # 等效
```

## 快速开始

```python
from ccs_sdk import Video, Hid, Gpio, Uart

v = Video('192.168.1.222')
print(v.stat())                        # '30.0 fps | running | 清晰1280x720 | hw编码 | HDMI'
open('snap.jpg', 'wb').write(v.snapshot())

Hid('192.168.1.222').click(0.5, 0.5)   # 点屏幕中心
print(Gpio('192.168.1.222').read_inputs())

u = Uart('192.168.1.222')
u.send(Uart.RS485, 'Hello')            # 485 发 "Hello\r\n"
print(u.recv(Uart.RS485, since=0))
```

更多见 `examples/` 与 `docs/`。

## 功能 → 模块 → HTTP 端点 → 板载硬件 对照总表

| 功能 | 模块 / 类 | HTTP 端点 | 板载硬件 / 驱动 |
|---|---|---|---|
| 采集状态 | `video.Video.stat()` | GET `/stat` | TC358743(HDMI→CSI2)/MS9288C(VGA)，rkcif DMA |
| 抓单帧 JPEG | `video.Video.snapshot()` | GET `/stream`（取第一帧） | 同上，vepu 硬编 JPEG |
| MJPEG 实时流 | `video.Video.mjpeg_stream()` | GET `/stream` | 同上 |
| 原始 UYVY 帧 | `video.Video.frame_raw()` | GET `/frame.raw` | rkcif mmap 帧缓冲 |
| 重对齐 / 相位微调 | `video.Video.realign()/nudge()` | GET `/realign`、`/nudge?d=N` | rkcif DMA 相位 / 消卷乒乓缓冲 |
| 清晰/流畅档 | `video.Video.set_mode()` | GET `/set?half=0\|1` | 编码几何 1280x720 / 640x360 |
| 键鼠/触摸注入 | `hid.Hid` | POST `/hid` | USB configfs HID gadget：hidg0 键盘 / hidg1 绝对鼠标 / hidg2 触摸 / hidg3 笔 |
| EDID 管理 | `edid.Edid` | POST `/edid_apply` `/edid_build` `/edid_preset`；读当前 EDID 抓 `GET /` 页面 | TC358743 芯片 RAM（断电即丢，开机由 ccs_init.sh 重写） |
| 通道切换 HDMI/VGA | `channel.Channel` | POST `/channel`（当前通道从 `/stat` 解析） | TMUX121 复用开关，SEL=gpio41（SW2 拨码闭合时硬件强制 VGA） |
| 网络配置（带回滚） | `network.Network` | POST `/net_apply`、GET `/net_confirm?token=`（列表抓 `GET /` 页面） | systemd-networkd，eth0/eth1，90 秒未确认自动回滚 |
| GPIO 输入电平 | `gpio.Gpio.read_inputs()` | GET `/gpio_vals` | LED_x_STATE 外部输入（GPIO2_B1=73 黄 / B2=74 绿 / C5=85 蓝 / C6=86 红），只读 |
| GPIO 白名单翻转 | `gpio.Gpio.set()` | POST `/gpio_set`（ajax=1） | sysfs GPIO，仅 `WORKING_LEDEN_H` 且无占用者 |
| 串口收发 | `uart.Uart` / `UartConsole` | GET `/uart_recv`、POST `/uart_set` `/uart_send` | RS232=/dev/ttyS3(UART3,JP5B)；RS485=/dev/ttyS4(UART4,JP5A)，Q10 硬件自动方向 |
| 亮度 / 锐化 | `image.Image` | GET `/brightness?val=20~200`、`/sharpen?val=0~100` | 采集端 Y 分量数字补偿（C shim），持久化 /etc/ccs_brightness、/etc/ccs_sharp |

## 模块清单

| 文件 | 类 | 说明 |
|---|---|---|
| `ccs_sdk/client.py` | `CCSClient` / `CCSApiError` | 基类：host/port/timeout（默认 5s）、`_get`/`_post` 封装、303 msg/err 解析；连接失败或 HTTP 非 200 抛 `CCSApiError`（带响应文本） |
| `ccs_sdk/video.py` | `Video` | 采集状态、抓帧、MJPEG 流、对齐、清晰/流畅 |
| `ccs_sdk/hid.py` | `Hid` | 键盘 / 鼠标（笔 digitizer）/ 触摸 / 绝对鼠标注入 |
| `ccs_sdk/edid.py` | `Edid` | EDID 读取 / HEX 应用 / 明文生成 / 出厂恢复 |
| `ccs_sdk/channel.py` | `Channel` | HDMI/VGA 通道查询与切换 |
| `ccs_sdk/network.py` | `Network` | 网卡列表、静态/DHCP 配置、90s 回滚确认 |
| `ccs_sdk/gpio.py` | `Gpio` | GPIO 全表、白名单翻转、四路外部输入实时电平 |
| `ccs_sdk/uart.py` | `Uart` / `UartConsole` | RS232/RS485 收发、波特率；后台线程持续接收 + 回调 |
| `ccs_sdk/image.py` | `Image` | 亮度（20~200%）/ 锐化（0~100） |

## 统一异常

```python
from ccs_sdk import CCSApiError
```

连接失败、超时、HTTP 非 200、板上回 `?err=` 一律抛 `CCSApiError`，
消息里带响应文本，方便定位。

## 示例

| 文件 | 功能 |
|---|---|
| `examples/demo_video.py` | 抓一帧存 jpg + 打印 stat |
| `examples/demo_uart485.py` | 485 发 "Hello" 并打印 3 秒内收到的数据 |
| `examples/demo_hid.py` | 鼠标点一下屏幕中心 + 按 Enter |
| `examples/demo_gpio.py` | 打印四路外部输入电平 |

在 `sdk/` 目录下运行：`python3 examples/demo_video.py [板子IP]`
