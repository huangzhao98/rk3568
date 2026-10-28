# api_uart.md — 串口控制台 RS232 / RS485（Uart / UartConsole）

## 功能简介

两路板载串口的收发与波特率设置。RS232 = /dev/ttyS3（UART3，JP5B 出）；
RS485 = /dev/ttyS4（UART4，JP5A 出），方向由 Q10(SS8050) 硬件自动切换，
无需软件控制 RTS。板端每路常驻打开 + 后台接收线程（8N1 raw），接收缓冲
环形 64KB，客户端按逻辑偏移 since 增量拉取。

## HTTP 端点表

| 方法 | 路径 | 参数 | 返回 |
|---|---|---|---|
| GET | `/uart_recv` | `port=232\|485`, `since=N`（逻辑偏移，默认 0）, `hex=0\|1` | 200 JSON `{"data": str, "next": int, "baud": int, "err": str}`；串口不可用 500 文本 |
| POST | `/uart_set` | `port`, `baud` | 200 `baud=N` / 500 `设置失败: ...` |
| POST | `/uart_send` | `port`, `data`, `hex=0\|1`, `nl=0\|1` | 200 `已发 NB` / 500 `发送失败: ...` |

细节（与板上 `UartPort` 一致）：
- 波特率白名单：9600/19200/38400/57600/115200/230400/460800/921600，
  默认 115200；非法值 `set_baud` 抛 `ValueError('unsupported baud')` → 500。
- `since` 落后于缓冲起点时给全量缓冲（最多 64KB 存量）；`next` 是新的
  逻辑偏移，下次传它即可只拿增量。
- `hex=1` 时 `data` 为 `'xx xx '` 格式字符串；`uart_send` 的 `hex=1`
  时 `data` 按 hex 字符串解析（空格可选），`nl=1` 追加 `\r\n`。

## SDK 方法表（`ccs_sdk.uart.Uart`）

| 方法 | 说明 | 返回 |
|---|---|---|
| `send(port, data, hex=False, crlf=True)` | 发送。data: str（UTF-8）或 bytes（自动转 hex 发原始字节） | str（`已发 NB`） |
| `recv(port, since=0, hex=False)` | 增量接收 | `(data, next)` |
| `recv_ex(port, since=0, hex=False)` | 同上但返回完整 dict | `{data, next, baud, err}` |
| `set_baud(port, baud)` | 设置波特率（白名单内） | str（`baud=N`） |

类常量：`Uart.RS232='232'`、`Uart.RS485='485'`、`Uart.BAUDS`。

### `UartConsole(uart, port, on_data, interval=1.0, hex=False, backlog=False, on_error=None)`

后台线程持续接收 + 回调的辅助类：

- `on_data(data, port)`：有新数据时回调
- `on_error(e, port)`：板上 err 非空或请求失败时回调
- `backlog=True`：先把板上 64KB 存量缓冲回调一次；默认从当前偏移起只收新数据
- `start()` 启动守护线程；`stop()` 停止

## 示例

```python
import time
from ccs_sdk import Uart, UartConsole

u = Uart('192.168.1.222')
u.set_baud(Uart.RS485, 115200)
u.send(Uart.RS485, 'Hello')                 # 自动追加 \r\n

# 轮询方式
since = u.recv(Uart.RS485, 0)[1]            # 跳过存量
time.sleep(1)
data, since = u.recv(Uart.RS485, since)

# 回调方式
con = UartConsole(u, Uart.RS485,
                  on_data=lambda d, p: print(p, '<<', repr(d)))
con.start()
u.send(Uart.RS485, b'\x01\x03\x00\x00\x00\x02')   # bytes 自动走 hex 模式
time.sleep(5)
con.stop()
```

## 注意事项

- 485 方向硬件自动，SDK 侧不需要（也无法）控制收发方向。
- 收发都走 HTTP 轮询，不是实时流——`UartConsole` 默认 1 秒一拉，
  高吞吐场景注意 64KB 环形缓冲会丢旧数据。
- 板端接收线程异常时 JSON 的 `err` 字段非空（`recv_ex()` 或
  `UartConsole(on_error=...)` 可拿到）。
- 两路串口对象板上惰性打开，第一次访问某端口时才 open 设备节点。
