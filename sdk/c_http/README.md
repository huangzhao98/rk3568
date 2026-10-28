# ccs_http — RK3568 CCS 采集板 HTTP API C SDK

与 Python SDK（`../ccs_sdk/`）等价的 C 版本，跑在任何能连板子的电脑/嵌入式
Linux 上（板子管理服务 `ccs_webcam.py`，默认 `http://192.168.1.222:8080`）。

**零第三方依赖**：C99 + libc + BSD socket。HTTP 客户端是纯 socket 手写实现，
不依赖 libcurl——嵌入式裁剪场景（Buildroot/精简 rootfs）libcurl 及其依赖链
可能不存在，而板子服务只用到 HTTP/1.1 的一个很小子集。

## 编译

```sh
make                    # 本机 gcc，产出 libccshttp.a + examples/
make CC=arm-linux-gnueabihf-gcc   # 交叉编译（换成你的工具链前缀）
```

## 用法

把 `ccs_http.h/.c`、`ccs_sdk.h/.c` 四个文件拷进项目（或链接
`libccshttp.a`），一个客户端实例走天下：

```c
#include "ccs_sdk.h"

ccs_client_t *c = ccs_new("192.168.1.222", 0, 0);   /* 默认 8080 / 5s 超时 */

char stat[256];
if (ccs_video_stat(c, stat, sizeof(stat)) == 0)
    printf("%s", stat);          /* 30.0 fps | running | 清晰1280x720 | ... */
else
    fprintf(stderr, "%s\n", ccs_error(c));

unsigned char *jpg; size_t jpg_len;
if (ccs_video_snapshot(c, &jpg, &jpg_len) == 0) {   /* 存 .jpg 后 free(jpg) */ }

ccs_free(c);
```

约定：所有函数返回 `0` = 成功，`-1` = 失败（`ccs_error()` 取错误描述，
对应 Python 版的 `CCSApiError` 消息）；标着 "malloc" 的输出由调用者
`free()`。

## 示例

| 示例 | 内容 | 副作用 |
|---|---|---|
| `examples/demo_stat` | /stat + 当前通道 + 四路 GPIO 输入电平 | 无（全只读） |
| `examples/demo_snapshot` | 抓一帧存 snapshot.jpg + MJPEG 回调拉流 5 帧 | 无（占一路流连接，取完即断） |
| `examples/demo_uart` | 485 增量接收（since 偏移语义）；发送演示在注释里 | 默认只读；解开注释的 send 会真实发字节 |

```sh
./examples/demo_stat 192.168.1.222
./examples/demo_snapshot 192.168.1.222 snap.jpg
./examples/demo_uart 192.168.1.222
```

## 与 Python SDK 对照表

| 功能 | Python (`ccs_sdk`) | C (`ccs_sdk.h`) | HTTP 端点 |
|---|---|---|---|
| 采集状态 | `Video.stat()` | `ccs_video_stat()` | GET `/stat` |
| 当前通道 | `Channel.current()` | `ccs_channel_current()` | GET `/stat`（解析） |
| 抓单帧 JPEG | `Video.snapshot()` | `ccs_video_snapshot()` | GET `/stream`（取首帧） |
| MJPEG 流 | `Video.mjpeg_stream()` 生成器 | `ccs_video_mjpeg_stream()` 回调 | GET `/stream` |
| 重对齐 | `Video.realign()` | `ccs_video_realign()` | GET `/realign` |
| 相位微调 | `Video.nudge(d)` | `ccs_video_nudge(d)` | GET `/nudge?d=N` |
| 清晰/流畅档 | `Video.set_mode()` | `ccs_video_set_mode()` | GET `/set?half=0\|1` |
| 通道切换 | `Channel.switch(ch)` | `ccs_channel_switch(ch)` | POST `/channel` |
| 读 EDID | `Edid.get_hex()` | `ccs_edid_get_hex()` | GET `/`（抓 textarea） |
| 应用 HEX EDID | `Edid.apply_hex()` | `ccs_edid_apply_hex()` | POST `/edid_apply` |
| 出厂 EDID | `Edid.factory_reset()` | `ccs_edid_factory_reset()` | POST `/edid_preset` |
| GPIO 全表 | `Gpio.list()` | `ccs_gpio_list()` | GET `/`（抓表格） |
| GPIO 翻转 | `Gpio.set()` | `ccs_gpio_set()` | POST `/gpio_set`（ajax=1） |
| GPIO 输入电平 | `Gpio.read_inputs()` | `ccs_gpio_read_inputs()` | GET `/gpio_vals` |
| 串口发送 | `Uart.send()` | `ccs_uart_send()` / `ccs_uart_send_bytes()` | POST `/uart_send` |
| 串口接收 | `Uart.recv()` | `ccs_uart_recv()` | GET `/uart_recv` |
| 波特率 | `Uart.set_baud()` | `ccs_uart_set_baud()` | POST `/uart_set` |
| 网卡列表 | `Network.ifaces()` | `ccs_net_ifaces()` | GET `/`（抓表格） |
| 网络配置 | `Network.apply()` | `ccs_net_apply()` | POST `/net_apply` |
| 网络确认 | `Network.confirm()` | `ccs_net_confirm()` | GET `/net_confirm?token=` |
| 亮度 | `Image.set_brightness()` | `ccs_image_set_brightness()` | GET `/brightness?val=` |
| 锐化 | `Image.set_sharpen()` | `ccs_image_set_sharpen()` | GET `/sharpen?val=` |
| 键盘注入 | `Hid.key()/send_keyboard()/release_all()` | `ccs_hid_key()` / `ccs_hid_send_keyboard()` / `ccs_hid_release_all()` | POST `/hid` |
| 鼠标（笔） | `Hid.mouse_move()/click()/...` | `ccs_hid_mouse_move()` / `ccs_hid_click()` / ... | POST `/hid` |
| 触摸 | `Hid.touch()/tap()` | `ccs_hid_touch()` / `ccs_hid_tap()` | POST `/hid` |
| 绝对鼠标+滚轮 | `Hid.mouse_abs()` | `ccs_hid_mouse_abs()` | POST `/hid` |
| 右键 | `Hid.right_click()` | `ccs_hid_right_click()` | POST `/hid`（触摸长按） |

未移植项（有意省略）：`Edid.build_apply()`（明文生成 EDID，管理页表单
场景为主，可用 `ccs_edid_apply_hex()` 替代）、`Video.frame_raw()`（调试用
原始 UYVY 帧）、`UartConsole`（后台线程轮询——C 里用 `ccs_uart_recv()`
自己循环即可，`examples/demo_uart.c` 有示范）。需要可照 Python 版补齐。

## ⚠ 写操作副作用警示表

| 函数 | 副作用 | 危险度 |
|---|---|---|
| `ccs_channel_switch` | 写 /etc/ccs_channel（重启保持）+ **服务重启约 15 秒，期间所有 API 不可用** | 高 |
| `ccs_net_apply` | restart systemd-networkd，**旧地址连接立刻断开**；90 秒未在新地址 `ccs_net_confirm` 自动回滚 | 高 |
| `ccs_hid_*`（全部） | 在被控上位机上产生**真实键鼠/触摸事件**；键盘是全状态上报，不调 `release_all` 键会一直"按着" | 高 |
| `ccs_edid_apply_hex` / `ccs_edid_factory_reset` | 写采集芯片 RAM；源端重新协商输出；自动重对齐，**画面中断几秒** | 中 |
| `ccs_video_realign` | 画面中断几秒；解除 nudge 手动相位锁定 | 中 |
| `ccs_video_nudge` | 即时改相位并自动保存；标记 roll_manual 停掉自动 track | 中 |
| `ccs_image_set_brightness` / `set_sharpen` | 即时改画质 + **持久化** /etc/ccs_brightness、/etc/ccs_sharp（复位 100 / 0） | 中 |
| `ccs_uart_set_baud` | 立即改板上串口速率（全局共享，影响所有会话） | 中 |
| `ccs_uart_send` / `send_bytes` | 往 232/485 线上真实发字节 | 中 |
| `ccs_gpio_set` | 改板上引脚电平（仅白名单 WORKING_LEDEN_H，否则 403） | 低 |
| `ccs_video_set_mode` | 只改运行时编码几何，即时生效不落盘 | 低 |

只读（无副作用）：`ccs_video_stat` / `ccs_channel_current` /
`ccs_video_snapshot` / `ccs_video_mjpeg_stream`（占一路流连接）/
`ccs_edid_get_hex` / `ccs_gpio_list` / `ccs_gpio_read_inputs` /
`ccs_uart_recv` / `ccs_net_ifaces` / `ccs_net_confirm`（确认成功才取消
回滚定时器，属预期行为）。

## 文件清单

```
c_http/
├── ccs_http.h / ccs_http.c   最小 HTTP/1.1 客户端（纯 BSD socket）
├── ccs_sdk.h  / ccs_sdk.c    与 Python 版对齐的全部功能接口
├── Makefile                  静态库 libccshttp.a + examples
├── examples/                 demo_stat / demo_snapshot / demo_uart
└── docs/api_c_http.md        逐函数 API 文档（功能/参数/返回/副作用）
```

详细 API 文档见 [docs/api_c_http.md](docs/api_c_http.md)；板上端点的权威
定义以 `ccs_webcam.py` 的 `do_GET`/`do_POST` 为准。
