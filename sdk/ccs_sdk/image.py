"""ccs_sdk.image — 画质补偿：亮度 / 锐化（/brightness /sharpen）。

对应管理页"实时预览"区块标题行里的亮度/锐化滑条。

板载背景（ccs_webcam.py）：
- 亮度增益 20~200%（100=原始）：VGA 通道模拟采样偏暗的补偿。MS9288C 的
  SCL/SDA 原理图未接，调不了芯片增益，只能在采集端对 Y 分量做数字补偿
  （色度不动，色饱和度不变）。持久化到 /etc/ccs_brightness。
- 锐化强度 0~100（0=关闭）：补偿 VGA 模拟采样（ADC 相位对不齐像素时钟）
  造成的边缘模糊，unsharp mask 只作用在 Y 分量。持久化到 /etc/ccs_sharp。
- 硬编路径下补偿由 C shim 在编码拷贝趟里完成（~3ms），即时生效。
- 两个端点都是 GET，回 200 文本（'亮度 N%' / '锐化 N'）；val 非数字时
  板上静默忽略（仍回当前生效值），越界自动钳位。
"""

from .client import CCSClient


class Image(CCSClient):
    """画质补偿接口。构造参数（host/port/timeout）见 CCSClient。

    两个方法都即时生效且**持久化**（写 /etc/ccs_brightness、
    /etc/ccs_sharp，重启服务后仍生效）；复位用 100 / 0。
    """

    def set_brightness(self, val):
        """GET /brightness?val=N 设置亮度增益。

        参数: val (int)——20~200（100=原始；越界板上自动钳位；
            非数字板上静默忽略）。
        返回: str——板上文本 '亮度 N%'（N 是实际生效值，回读用它确认
            钳位结果）。
        抛出: CCSApiError（连接失败）。
        副作用: 即时改画质 + 持久化 /etc/ccs_brightness。
        """
        return self.get_text(f'/brightness?val={int(val)}')

    def set_sharpen(self, val):
        """GET /sharpen?val=N 设置锐化强度。

        参数: val (int)——0~100（0=关闭；越界自动钳位）。
        返回: str——板上文本 '锐化 N'。
        抛出: CCSApiError（连接失败）。
        副作用: 即时改画质 + 持久化 /etc/ccs_sharp。
        """
        return self.get_text(f'/sharpen?val={int(val)}')
