"""ccs_sdk — RK3568 CCS 采集板 HTTP API Python SDK（零第三方依赖，仅标准库）。

板子管理页：http://<板子IP>:8080/ （板上服务 ccs_webcam.py）。
本包每个模块对应管理页的一个功能区块：

- client   CCSClient/CCSApiError：HTTP 基类与统一异常（无对应页面区块）
- video    Video：实时预览（/stat /stream /frame.raw /realign /nudge /set）
- hid      Hid：KVM 键鼠/触摸注入（/hid → /dev/hidg0~3）
- edid     Edid：EDID 管理（/edid_apply /edid_build /edid_preset）
- channel  Channel：采集通道 HDMI/VGA（/channel）
- network  Network：网络设置（/net_apply /net_confirm，90s 回滚）
- gpio     Gpio：GPIO 状态与白名单翻转（/gpio_vals /gpio_set）
- uart     Uart/UartConsole：串口控制台 RS232/RS485（/uart_recv 等）
- image    Image：亮度/锐化（/brightness /sharpen）

用法（全部类汇总导出）::

    from ccs_sdk import Video, Hid, Uart
    print(Video('192.168.1.222').stat())

每个功能类也可单独 import（模块间无交叉依赖，只依赖 client.py）::

    from ccs_sdk.video import Video
"""

from .client import CCSClient, CCSApiError
from .video import Video
from .hid import Hid
from .edid import Edid
from .channel import Channel
from .network import Network
from .gpio import Gpio
from .uart import Uart, UartConsole
from .image import Image

__all__ = [
    'CCSClient', 'CCSApiError',
    'Video', 'Hid', 'Edid', 'Channel', 'Network', 'Gpio',
    'Uart', 'UartConsole', 'Image',
]

__version__ = '1.0.0'
