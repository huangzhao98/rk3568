# api_channel.md — 采集通道切换（Channel）

## 功能简介

两路采集芯片（HDMI / VGA）经 TMUX121 复用开关切换，SEL=gpio41
（1=VGA；未 export 时 R80 下拉默认 HDMI）。选择结果存板上
`/etc/ccs_channel`，重启保持。

## HTTP 端点表

| 方法 | 路径 | 参数 | 返回 |
|---|---|---|---|
| POST | `/channel` | 表单 `ch=hdmi\|vga` | 200/500 文本，如 `正在切换到 VGA，服务重启约 15 秒` |

查询当前通道没有独立 API：`GET /stat` 文本最后一栏即通道（`HDMI`/`VGA`），
`current()` 从 `/stat` 解析。

## SDK 方法表（`ccs_sdk.channel.Channel`）

| 方法 | 说明 | 返回 |
|---|---|---|
| `current()` | 当前通道 | `'hdmi'` / `'vga'` |
| `switch(ch)` | 切换通道（`'hdmi'`/`'vga'`） | str（板上文本） |

类常量：`Channel.HDMI='hdmi'`、`Channel.VGA='vga'`。

## 示例

```python
from ccs_sdk import Channel

c = Channel('192.168.1.222')
print(c.current())          # 'hdmi'
print(c.switch('vga'))      # 正在切换到 VGA，服务重启约 15 秒
```

## 注意事项

- **切换 = 重启采集服务**：板上写 `/etc/ccs_channel` 后用
  `systemd-run --on-active=1` 延迟 1 秒重启 ccs_webcam（直接 restart 会被
  cgroup 清理连坐杀掉）。重启期间（约 15 秒）**所有 HTTP API 不可用**，
  SDK 调用会抛 `CCSApiError`（连接失败），需等待后重试。
- 不做进程内热切换：实测句柄跨 unbind/bind 会触发内核 subdev_close
  空指针 oops 且 rkcif 卡死零帧（2026-09-21）。
- 两路芯片各有独立 EDID，切换后自动带上 `/tmp/ccs_last_edid.txt` 那份。
- 已在目标通道时板上直接返回 `已在 XXX 通道`，不重启。
- **SW2 拨码闭合时硬件强制 VGA 路，软件切换无效**。
