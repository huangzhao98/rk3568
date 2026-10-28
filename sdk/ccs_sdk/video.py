"""ccs_sdk.video — 视频采集：状态、抓帧、MJPEG 流、对齐、清晰/流畅档。

对应管理页"实时预览"区块（状态行 + <img src="/stream"> + 清晰/流畅/
重新对齐/消卷微调按钮）。

板载背景（ccs_webcam.py）：
- 采集链：TC358743（HDMI→MIPI-CSI2）经 rkcif DMA 采 UYVY 帧；
  源分辨率 >720p 时先 RGA 缩到 720p 再编码（vepu 硬编 1080p 超时不可用）。
- rkcif 的 DMA 相位锁定在开流后收到的第一个数据，源端断信号再恢复会破坏
  对齐（画面垂直卷动）→ /realign 触发重对齐序列，/nudge 手动微调相位。
- 清晰/流畅两档：清晰档 vepu 硬编全分辨率；流畅档隔行隔列降到 640x360。
- 注意：板上**没有 /frame.jpg 端点**，snapshot() 实现为从 /stream 取
  第一个分帧后断开。
- /frame.raw 返回当前原始 UYVY 帧（采集几何 w*h*2 字节，调试用），
  无帧时板上回 503。
"""

import http.client

from .client import CCSClient, CCSApiError


class Video(CCSClient):
    """视频采集接口。构造参数（host/port/timeout）见 CCSClient。

    所有方法副作用均为只读或即时生效的画质/对齐调整；不影响板上持久配置
    （set_mode 只改运行时档位，不落盘）。
    """

    CLEAR = 'clear'      # 清晰档（/set?half=0）：全分辨率
    SMOOTH = 'smooth'    # 流畅档（/set?half=1）：640x360

    def stat(self):
        """GET /stat → 采集状态一行文本。

        返回: str，形如 '30.0 fps | running | 清晰1280x720 | hw编码 | HDMI\\n'
            五栏依次是 fps / 状态机 / 编码档位(含源分辨率) / 编码器 / 通道。
        抛出: CCSApiError（连接失败）。
        副作用: 无。
        """
        return self.get_text('/stat')

    def snapshot(self, timeout=None):
        """抓一帧 JPEG 图片。

        实现说明：板上没有单帧 JPEG 端点，这里是连 /stream 取第一个
        分帧后立即断开（生成器只迭代一次，for 退出即触发 finally 关连接）。

        参数: timeout (float|None) 读流超时秒数，None 用 max(self.timeout, 10)。
        返回: bytes——完整 JPEG 文件内容（可直接写 .jpg）。
        抛出: CCSApiError——无信号期间板上长时间不出帧会读超时；
            连接失败同样抛。
        副作用: 无。
        """
        for jpg in self.mjpeg_stream(timeout=timeout):
            return jpg
        raise CCSApiError('/stream 未产出任何帧')

    def frame_raw(self):
        """GET /frame.raw → 当前原始 UYVY 帧。

        返回: bytes，采集几何的 UYVY 字节流（w*h*2；尺寸见 /stat 里的
            源分辨率）。
        抛出: CCSApiError——板上无帧时回 503（'GET /frame.raw → HTTP 503'）。
        副作用: 无。
        """
        return self.get_bytes('/frame.raw')

    def mjpeg_stream(self, timeout=None):
        """GET /stream MJPEG 无限流，生成器逐帧 yield JPEG bytes。

        板上按 'multipart/x-mixed-replace; boundary=frame' 输出，
        Connection: close、无 Content-Length——所以不能用 urllib
        （response.read() 会读到天荒地老），这里用 http.client 直接
        操作底层的 buffered reader 按分帧解析。

        参数: timeout (float|None) 每次 socket 读的超时秒数；None 时取
            max(self.timeout, 10)——无信号期间服务端 5 秒空转重试不出帧，
            默认 5 秒太短会误超时。
        返回: 生成器，每次 yield 一帧 JPEG bytes。break 后生成器被回收
            （或显式 .close()）即断开连接（finally 里 conn.close()）。
        抛出: CCSApiError——非 200、读超时、连接中断。
        副作用: 占用板上一路流连接（板上 ThreadingHTTPServer 每连接一线程）。
        """
        to = timeout if timeout is not None else max(self.timeout, 10.0)
        conn = http.client.HTTPConnection(self.host, self.port, timeout=to)
        try:
            conn.request('GET', '/stream')
            resp = conn.getresponse()
            if resp.status != 200:
                raise CCSApiError(f'GET /stream → HTTP {resp.status}')
            # resp.fp 是 socket.makefile('rb') 的 buffered reader，
            # 支持 readline/read(n)——正好按行解析分帧头
            fp = resp.fp
            while True:
                # 分帧结构（板上写死的格式）：
                #   --frame\r\nContent-Type: image/jpeg\r\n
                #   Content-Length: N\r\n\r\n<jpeg>\r\n
                length = None
                while True:
                    line = fp.readline()
                    if not line:
                        return                      # 连接被服务端关闭
                    line = line.strip()
                    if not line:
                        if length is not None:
                            break                   # 空行结束分帧头
                        continue                    # 边界行前的空行
                    if line.startswith(b'--frame'):
                        continue
                    if line.lower().startswith(b'content-length:'):
                        length = int(line.split(b':', 1)[1].strip())
                if not length:
                    continue
                jpg = fp.read(length)
                if len(jpg) != length:
                    return                          # 读不满 = 连接断了
                yield jpg
        except (OSError, http.client.HTTPException) as e:
            raise CCSApiError(f'/stream 读取失败: {e}') from e
        finally:
            conn.close()

    def realign(self):
        """GET /realign → 触发重对齐序列。

        画面垂直卷动/错位时用。板上会走完整对齐流程（保存当前 EDID、
        造静默窗口、恢复 EDID、重新开流），期间屏蔽 watchdog。

        返回: str——板上 msg '已触发重对齐'。
        抛出: CCSApiError。
        副作用: 画面中断几秒属正常；解除用户 nudge 的手动相位锁定
            （roll_manual 标记）。
        """
        return self.get_msg('/realign')

    def nudge(self, d):
        """GET /nudge?d=N 消卷相位手动微调（即时生效、板上自动保存）。

        参数: d (int) 相位偏移行数。d=+1 内容上移一行；d=99999 是特殊
            语义——翻半屏（板上替换成 cap.h//2）。
        返回: str——板上文本 '相位 +10 → 123'（当前 roll 值）。
        抛出: CCSApiError。
        副作用: 用户目视=最终仲裁——调用后板上标记 roll_manual，自动
            track 只记录不覆盖；realign() 可解除。参考帧相位随之失效，
            由采集循环按新相位重存。
        """
        return self.get_text(f'/nudge?d={int(d)}')

    def set_mode(self, mode):
        """清晰/流畅档切换：GET /set?half=0|1。

        参数: mode (str)——Video.CLEAR/'clear'/'清晰' 或
            Video.SMOOTH/'smooth'/'流畅'。
        返回: str——生效档位名（'清晰' 或 '流畅'）。板上 303 不带 msg，
            所以返回值是本地按请求推的，不是板上原文。
        抛出: ValueError——mode 不是合法档位；CCSApiError——连接失败。
        副作用: 只改运行时编码几何（清晰=源分辨率，流畅=640x360），
            即时生效，不落盘、不重启服务。
        """
        if mode in (self.CLEAR, '清晰'):
            half = 0
        elif mode in (self.SMOOTH, '流畅'):
            half = 1
        else:
            raise ValueError("mode 须为 'clear'/'smooth'（或 '清晰'/'流畅'）")
        self.get_msg(f'/set?half={half}')
        return '流畅' if half else '清晰'
