"""ccs_sdk.channel — 采集通道切换 HDMI / VGA（POST /channel）。

对应管理页"采集通道（HDMI / VGA）"折叠块。

板载背景（ccs_webcam.py）：
- 两路采集芯片经 TMUX121 复用开关切换，SEL=gpio41（1=VGA；未 export 时
  R80 下拉默认 HDMI）。SW2 拨码闭合时硬件强制 VGA 路，软件设置无效。
- 切换 = 写 /etc/ccs_channel + systemd-run 延迟 1s 重启 ccs_webcam 服务
  （约 15 秒），重启保持。不做进程内热切换——实测句柄跨 unbind/bind 会
  触发内核 subdev_close 空指针 oops 且 rkcif 卡死零帧（2026-09-21）。
- 两路芯片各有独立 EDID，切换后自动带上 /tmp/ccs_last_edid.txt。
- 查询当前通道没有独立 API：/stat 文本最后一栏就是通道（HDMI/VGA），
  current() 从 /stat 解析。
"""


from .client import CCSClient


class Channel(CCSClient):
    """通道切换接口。构造参数（host/port/timeout）见 CCSClient。

    ⚠ switch() 的副作用很大：采集服务整体重启约 15 秒，期间**所有**
    HTTP API（含视频流、串口、GPIO）都不可用，调用会抛 CCSApiError
    （连接失败）——调用方需自行等待并重试。
    """

    HDMI = 'hdmi'
    VGA = 'vga'

    def current(self):
        """当前通道。

        实现：/stat 文本形如 '30.0 fps | running | 清晰1280x720 | hw编码
        | HDMI'，取最后一个 '|' 分栏转小写。板上 chan_current() 读
        /sys/class/gpio/gpio41/value（1=VGA，未 export 默认 HDMI）。

        返回: str——'hdmi' 或 'vga'。
        抛出: CCSApiError（连接失败）。
        副作用: 无。
        """
        return self.get_text('/stat').strip().rsplit('|', 1)[-1].strip().lower()

    def switch(self, ch):
        """POST /channel ch=hdmi|vga 切换通道。

        参数: ch (str)——'hdmi'/'vga'（不区分大小写，本地先转小写并校验，
            非法值不发请求直接 ValueError）。
        返回: str——板上文本：切换中是 '正在切换到 VGA，服务重启约 15 秒'；
            已在目标通道是 '已在 VGA 通道'（此时板上直接返回、不重启）。
        抛出: ValueError——ch 非法；CCSApiError——写 /etc/ccs_channel
            失败（500）或连接失败。
        副作用: 写板上 /etc/ccs_channel（重启保持）；systemd-run 延迟 1 秒
            重启 ccs_webcam 服务，约 15 秒内所有 API 不可用；切换后新芯片
            自动补写 /tmp/ccs_last_edid.txt 那份 EDID。
        """
        ch = str(ch).lower()
        if ch not in ('hdmi', 'vga'):
            raise ValueError("ch 须为 'hdmi' 或 'vga'")
        return self.post_text('/channel', {'ch': ch})
