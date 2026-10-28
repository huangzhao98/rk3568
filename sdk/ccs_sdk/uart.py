"""ccs_sdk.uart — RS232/RS485 串口控制台（/uart_recv /uart_set /uart_send）。

对应管理页"串口控制台（RS232 / RS485）"折叠块。

板载背景（ccs_webcam.py + ccs_dev 02_IO与复用配置总表）：
- RS232 = /dev/ttyS3（UART3，JP5B 出）；RS485 = /dev/ttyS4（UART4，JP5A 出）。
- RS485 方向由 Q10(SS8050) 硬件自动切换，无需软件 rs485 rts-gpio 控制。
- 板端每路串口常驻打开 + 后台接收线程，8N1 raw 模式；接收缓冲环形
  64KB（超出丢旧留新 32KB），rx_total 是**逻辑偏移**（单调递增，不因
  缓冲丢弃回退），客户端按 since 增量拉取：
    - since 落在缓冲窗口内 → 只回增量；
    - since 落后于缓冲起点（太久没拉，旧数据被冲掉）→ 给全量现存缓冲；
    - since=0 且从未拉过 → 给当前全部存量（最多 64KB）。
  返回的 next 字段是新的逻辑偏移，下次原样传回即可无缝续拉。
- 波特率白名单：9600/19200/38400/57600/115200/230400/460800/921600，
  默认 115200。
- GET /uart_recv?port=232|485&since=N&hex=0|1
    → JSON {"data": str, "next": int, "baud": int, "err": str}
    hex=1 时 data 是 'xx xx ' 格式字符串。
- POST /uart_set port,baud → 'baud=N'；POST /uart_send port,data,hex,nl
  → '已发 NB'（nl=1 追加 \\r\\n；hex=1 时 data 按 hex 字符串解析）。
"""

import threading

from .client import CCSClient


class Uart(CCSClient):
    """串口接口。构造参数（host/port/timeout）见 CCSClient。

    副作用都局限在串口本身：send 会真的往线上发字节，set_baud 立即
    改板上该路串口波特率（影响所有正在用它的会话——板端串口对象全局
    共享，网页控制台和 SDK 看到的是同一路）。
    """

    RS232 = '232'
    RS485 = '485'
    BAUDS = (9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)

    def send(self, port, data, hex=False, crlf=True):
        """POST /uart_send 发送数据。

        参数:
            port (str): Uart.RS232('232') 或 Uart.RS485('485')。
            data (str|bytes): str 按 UTF-8 编码发送；bytes 自动转 hex
                字符串并强制 hex 模式发原始字节（因为板上表单字段按
                UTF-8 解码，任意二进制直接塞 str 会被 'replace' 污染）。
            hex (bool): True 时 data 须为 hex 字符串（空格可选），板上
                bytes.fromhex 解析；发 str 文本时保持 False。
            crlf (bool): True 追加 \\r\\n（板上 nl=1），默认 True 与网页
                控制台"追加\\r\\n"勾选一致。
        返回: str——板上文本 '已发 NB'（N 为实际写入字节数，含 \\r\\n）。
        抛出: CCSApiError——hex 字符串非法 / 端口名错 / 写设备失败（500，
            正文带原因）。
        副作用: 往对应串口线上实际发出字节；485 方向由 Q10 硬件自动切，
            无需（也无法）软件控制。
        """
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).hex(' ')
            hex = True
        form = {'port': port, 'data': data,
                'hex': '1' if hex else '0', 'nl': '1' if crlf else '0'}
        return self.post_text('/uart_send', form)

    def recv(self, port, since=0, hex=False):
        """GET /uart_recv 增量拉取接收数据。

        参数:
            port (str): '232'/'485'。
            since (int): 上次返回的 next 值；0=从最早可用缓冲开始。
                语义见模块 docstring 的环形缓冲说明。
            hex (bool): True 时 data 为 'xx xx ' 格式字符串（收二进制用）。
        返回: (data, next)——data 是 str（文本或 hex 字符串），next 是
            int 逻辑偏移，下次调用原样传回。需要 baud/err 用 recv_ex()。
        抛出: CCSApiError——端口名错 / 设备打不开（500）。
        副作用: 无（纯读取，不消费板上数据——since 是客户端侧状态，
            其他会话互不影响）。
        """
        r = self.recv_ex(port, since, hex)
        return r['data'], r['next']

    def recv_ex(self, port, since=0, hex=False):
        """同 recv，但返回板上完整 JSON dict {data, next, baud, err}。

        err 非空表示板端接收线程出过异常（OSError repr）；baud 是该路
        当前波特率。
        参数/抛出/副作用同 recv。
        """
        return self.get_json(f'/uart_recv?port={port}&since={int(since)}'
                             f'&hex={1 if hex else 0}')

    def set_baud(self, port, baud):
        """POST /uart_set 设置波特率（8N1 固定，只改速率）。

        参数: baud (int)——须在 BAUDS 白名单内（板上校验，非法回 500
            'unsupported baud'）。
        返回: str——'baud=N'（板上生效值）。
        抛出: CCSApiError——非法波特率（500）或连接失败。
        副作用: 立即改板上该路串口速率；改的瞬间线上正在传的字节可能
            出错（双方须同步切换）。
        """
        return self.post_text('/uart_set', {'port': port, 'baud': str(int(baud))})


class UartConsole:
    """后台线程持续接收 + 回调的辅助类（不是 CCSClient 子类，组合 Uart）。

    用法::

        def on_data(text, port): print(port, '<<', text)
        con = UartConsole(Uart('192.168.1.222'), Uart.RS485, on_data)
        con.start()   # 后台轮询
        ...
        con.stop()

    参数:
        uart (Uart): 已构造的 Uart 实例（host/port/timeout 由它带）。
        port (str): Uart.RS232 / Uart.RS485。
        on_data (callable): on_data(data, port)，有新数据时回调；
            data 类型同 Uart.recv（文本或 hex 字符串）。
        interval (float): 轮询间隔秒，默认 1.0（与网页控制台刷新节奏
            一致；高吞吐场景可调小，注意板上环形缓冲只有 64KB）。
        hex (bool): True 时回调收到 hex 字符串。
        backlog (bool): True 时启动先把板上现存缓冲（最多 64KB）回调
            一次；默认 False——从当前偏移起只收新数据（启动时不被历史
            数据刷屏）。
        on_error (callable|None): on_error(err, port)，板上 err 字段非空
            或请求异常时回调；不传则错误被静默吞掉（线程继续跑）。
    """

    def __init__(self, uart, port, on_data, interval=1.0, hex=False,
                 backlog=False, on_error=None):
        self.uart = uart
        self.port = port
        self.on_data = on_data
        self.on_error = on_error
        self.interval = interval
        self.hex = hex
        self.backlog = backlog
        self._stop = threading.Event()
        self._thread = None

    def _run(self):
        try:
            _, nxt = self.uart.recv(self.port, 0, self.hex)
            # backlog=False 时把起点设成"当前最新偏移"，跳过存量缓冲——
            # 存量可能是很久之前的调试输出，对自动化没意义
            since = 0 if self.backlog else nxt
        except Exception as e:                    # noqa: BLE001 - 回调给用户
            if self.on_error:
                self.on_error(e, self.port)
            return
        while not self._stop.is_set():
            try:
                r = self.uart.recv_ex(self.port, since, self.hex)
                since = r['next']
                if r['data']:
                    self.on_data(r['data'], self.port)
                if r['err'] and self.on_error:
                    self.on_error(r['err'], self.port)
            except Exception as e:                # noqa: BLE001
                # 单次失败不退出（板子重启/网络抖动后自恢复），靠
                # on_error 上报，interval 后重试
                if self.on_error:
                    self.on_error(e, self.port)
            self._stop.wait(self.interval)        # 可中断的 sleep

    def start(self):
        """启动后台接收线程（守护线程，主进程退出自动结束）。

        返回: self（可链式）。抛出: RuntimeError——已在运行。
        副作用: 开始按 interval 轮询板上 /uart_recv。
        """
        if self._thread is not None:
            raise RuntimeError('已在运行')
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self, join=True):
        """停止后台接收。

        参数: join (bool)——True 时等当前轮询周期结束（最长
            interval + 2 秒）。
        副作用: 停止轮询；不影响板上缓冲（期间收到的数据下次 start
            仍可按偏移拉到，只要没超出 64KB 环形窗口）。
        """
        self._stop.set()
        if join and self._thread is not None:
            self._thread.join(timeout=self.interval + 2)
            self._thread = None
