# api_video.md — 视频采集（Video）

## 功能简介

HDMI/VGA 源经采集芯片进 rkcif DMA，实时编码 JPEG 输出。提供状态查询、
单帧抓取、MJPEG 无限流、原始帧导出、画面重对齐与相位微调、清晰/流畅
两档切换。

## HTTP 端点表

| 方法 | 路径 | 参数 | 返回 |
|---|---|---|---|
| GET | `/stat` | 无 | 200 文本：`{fps:.1f} fps \| {state} \| {模式} \| {enc}编码 \| {HDMI\|VGA}\n` |
| GET | `/stream` | 无 | 200 `multipart/x-mixed-replace; boundary=frame` MJPEG 无限流（`Connection: close`，无 Content-Length） |
| GET | `/frame.raw` | 无 | 200 原始 UYVY 帧字节流（`application/octet-stream`，w*h*2）；无帧时 503 空 |
| GET | `/realign` | 无 | 303 跳 `/?msg=已触发重对齐` |
| GET | `/nudge` | `d=N`（整数） | 200 文本 `相位 {d:+d} → {roll}`；`d=99999` 语义化为翻半屏 |
| GET | `/set` | `half=0\|1` | 303 跳 `/`（0=清晰，1=流畅） |

注意：**没有 `/frame.jpg` 端点**。SDK 的 `snapshot()` 是从 `/stream`
取第一个分帧后断开实现的。

## SDK 方法表（`ccs_sdk.video.Video`）

| 方法 | 说明 | 返回 |
|---|---|---|
| `stat()` | 采集状态文本 | str |
| `snapshot(timeout=None)` | 抓一帧 JPEG | bytes |
| `mjpeg_stream(timeout=None)` | MJPEG 流生成器，逐帧 yield | 生成器（bytes） |
| `frame_raw()` | 原始 UYVY 帧 | bytes（无帧抛 CCSApiError） |
| `realign()` | 触发重对齐序列 | str（板上 msg） |
| `nudge(d)` | 消卷相位微调，`d=99999` 翻半屏 | str（如 `相位 +10 → 123`） |
| `set_mode(mode)` | `'clear'/'smooth'`（或 `'清晰'/'流畅'`） | str |

类常量：`Video.CLEAR='clear'`、`Video.SMOOTH='smooth'`。

## 示例

```python
from ccs_sdk import Video

v = Video('192.168.1.222')
print(v.stat())

# 抓一帧
open('snap.jpg', 'wb').write(v.snapshot())

# 连续流（取 100 帧演示）
for i, jpg in enumerate(v.mjpeg_stream()):
    if i >= 100:
        break
    ...

# 画面垂直卷动时：先重对齐，不够再微调
v.realign()
v.nudge(-10)          # 内容下移 10 行
v.set_mode('smooth')  # 网络差时切流畅档（640x360）
```

## 注意事项

- rkcif 的 DMA 相位锁定在开流后第一个数据；源端断信号再恢复（休眠/拔插）
  会破坏对齐，板上有 watchdog 自动重对齐，手动可用 `realign()`。
- `nudge()` 手动微调后板上标记"用户目视=最终仲裁"，自动 track 不再覆盖；
  `realign()` 会解除该标记。
- 流畅档是编码前隔行隔列降采样到 640x360；清晰档为源分辨率（>720p 的源
  先 RGA 缩到 720p 再编码——vepu 硬编 1080p 超时不可用）。
- `/stream` 无信号期间服务端 5 秒空转重试、不出帧，`mjpeg_stream()` 的
  socket 超时默认取 `max(self.timeout, 10)`，超时抛 `CCSApiError`。
- `frame_raw()` 返回的是采集几何（非编码几何）的 UYVY 字节流，
  大小 w*h*2，需自行知道当前采集分辨率（`/stat` 里带）。
