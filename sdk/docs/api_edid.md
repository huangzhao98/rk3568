# api_edid.md — EDID 管理（Edid）

## 功能简介

读取/改写采集芯片（TC358743）RAM 里的 EDID，控制源端输出协商。支持直接
应用 HEX、按明文字段生成、恢复出厂 720p 三种方式。

## HTTP 端点表

| 方法 | 路径 | 参数 | 返回 |
|---|---|---|---|
| POST | `/edid_apply` | `edid`=hex 文本（128/256 字节）；`fix`=存在则自动修复校验和 | 303 `?msg=EDID 已生效，已触发重对齐` / `?err=...` |
| POST | `/edid_build` | `name`,`timing`,`mfr`,`product`(hex),`serial`,`year`,`week`,`hcm`,`vcm` | 303 `?msg=...已生成并生效...` / `?err=参数错误: ...` |
| POST | `/edid_preset` | `preset=720p` | 303 `?msg=已恢复 720p EDID，重对齐中` / `?err=未知预设` |

读当前 EDID 没有独立 API：hex 文本嵌在管理页 `GET /` 的
`<textarea name=edid>` 里，`get_hex()` 抓页面解析。

`edid_build` 参数约束（板上 `build_edid()` 校验）：
- `timing` ∈ {`720p60`, `1080p60`, `1080p30`}
- `name` 1~13 个 ASCII（可打印）；`mfr` 3 个大写英文字母
- `product` 按 16 进制解析（`int(x,16)`）；`serial` 0~0xFFFFFFFF
- `year` 1991~2245；`week` 0~53；`hcm`/`vcm` 1~255（屏幕尺寸 cm）

## SDK 方法表（`ccs_sdk.edid.Edid`）

| 方法 | 说明 | 返回 |
|---|---|---|
| `get_hex()` | 读芯片当前 EDID（hex 文本，每行 16 字节） | str |
| `apply_hex(hextext, fix=True)` | 直接应用 HEX EDID | str（板上 msg） |
| `build_apply(name, timing, mfr, product, serial, year, week, hcm, vcm)` | 明文生成并应用 | str（板上 msg） |
| `factory_reset()` | 恢复出厂 720p EDID (CCS-HDMI) | str（板上 msg） |

类外常量：`TIMINGS = ('720p60', '1080p60', '1080p30')`。

## 示例

```python
from ccs_sdk import Edid

e = Edid('192.168.1.222')
print(e.get_hex())                          # 当前 EDID hex

# 明文字段生成（1080p30，2-lane MIPI 能带动的最高档）
print(e.build_apply(name='CCS-HDMI', timing='1080p30', mfr='CCS',
                    product=0x0002, serial=1, year=2026, week=1,
                    hcm=16, vcm=9))

# 玩坏了恢复出厂
print(e.factory_reset())
```

## 注意事项

- EDID 存在芯片 RAM，**板子断电即丢**（开机由 `ccs_init.sh` 重写出厂
  720p EDID `/usr/sbin/edid_720p.hex.txt`）。
- 应用成功后板端自动触发重对齐，画面中断几秒属正常；对齐序列会保存/
  恢复当前 EDID，不会冲掉用户设置。
- 最近一次成功应用的 EDID 存板上 `/tmp/ccs_last_edid.txt`，切通道时
  自动补写到新选中的芯片（两路芯片各有独立 EDID）。
- **1080p60 需 4-lane MIPI，当前 EVB 为 2-lane，仅支持 720p60/1080p30**。
  设了带不动的主信号会采不到帧，`factory_reset()` 恢复即可。
- `apply_hex` 写入后板上会读回逐字节验证；`fix=True` 时校验和字节跳过
  比较（校验和被修复过）。读回不一致会报 `读回内容不一致（校验和错误？
  可勾选自动修复）`。
