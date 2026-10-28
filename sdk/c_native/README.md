# CCS 板端原生 C SDK（c_native）

跑在 RK3568 CCS 采集板上、可静态编译进用户自己的板端程序的 C 库。
功能覆盖主服务 `ccs_dev/ccs_webcam.py`（Python，约 2400 行，权威实现）
的三块核心能力：**V4L2 抓帧、采集芯片 EDID 读写、垂直卷动相位估计/软件消卷**。

- 语言：C99，仅依赖 libc + 内核 uapi 头（`linux/videodev2.h` 等），无第三方库
- 产物：`libccs_native.a` 静态库 + `examples/` 三个 demo
- 与本目录平级的 `../ccs_sdk`（Python SDK）走 HTTP 调主服务接口；
  本 SDK 直接操作设备节点，不经过主服务——**同一时刻只能有一个使用者开流**

## 硬件背景

```
HDMI 源 → TC358743(HDMI 路) ┐                    ┌→ rkcif → /dev/video0 (V4L2)
VGA  源 → MS9288C (VGA  路) ┴→ MIPI-CSI2(4-lane) ┘      → 用户程序
```

- 采集芯片经 I2C4 挂 `/dev/v4l-subdevX`（tc35874x 驱动，EDID 在片内 RAM，断电即丢）
- `/dev/video0` 输出 UYVY（YUV 4:2:2 打包，2 字节/像素），不是 NV12/NV16
- rkcif 的 DMA 相位锁定在"开流后收到的第一个数据"：有信号时开流会从帧中间
  开始写，画面垂直卷动且运行中不自愈。硬件无确定性对齐手段，只能
  **测相位 k + 软件消卷**（buffer 第 b 行 = 源画面第 (b+k)%h 行）

## 编译

```bash
make                # 默认：SDK prebuilts 的 Linaro 6.3.1 aarch64 交叉工具链
make CC=gcc         # 本机 x86 编译（调试用）/ 板端原生编译
make CC=aarch64-linux-gnu-gcc   # 其他已在 PATH 的工具链
make clean
```

产物：`libccs_native.a`、`examples/demo_capture`、`examples/demo_edid`、
`examples/demo_align`（全部 aarch64 静态链接库 + 动态链接可执行）。
`-Wall -Wextra` 零警告。

使用：把 `libccs_native.a` 与三个头文件放进自己的工程，
`gcc app.c -I<sdk目录> -L<sdk目录> -lccs_native`。

## 模块用法

### ccs_cap — V4L2 抓帧（对应 ccs_webcam.py Capture 类）

```c
ccs_cap_t *cap = ccs_cap_open("/dev/video0", 1280, 720);  /* S_FMT+REQBUFS+mmap */
ccs_cap_stream_on(cap);                                    /* QBUF×4 + STREAMON */
uint8_t *buf = malloc(ccs_cap_frame_size(cap));            /* = 宽×高×2 (UYVY) */
int n = ccs_cap_read_frame(cap, buf, size, 2000);          /* 排干队列取最新帧 */
/* n>0 帧字节数；0 超时/短帧；-1 错误 */
ccs_cap_stream_off(cap);
ccs_cap_close(cap);
```

要点：
- multiplanar（`V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE`）+ `V4L2_MEMORY_MMAP`，
  与 Python 侧 `MPLANE=9, MEM_MMAP=1` 一致；mmap 偏移取 `plane.m.mem_offset`
- `read_frame` 实现主服务 `grab_latest` 的低延迟策略：先等首帧，再 0 超时
  排干队列，旧帧重新入队，只留最新一帧
- media 链路各实体格式由 `ccs_init.sh`/主服务的 media-ctl 流程配置，
  本模块不重复配置；无信号开机时节点格式 0x0，REQBUFS 会失败（ENOMEM）

### ccs_edid — 采集芯片 EDID 读写（对应 get_edid_hex/set_edid_hex/edid_ok）

```c
uint8_t edid[128];
int n = ccs_edid_read("/dev/v4l-subdev2", edid, 1);   /* --get-edid 等价 */
ccs_edid_valid(edid, n);                              /* 校验和+非垃圾检查 */
ccs_edid_fix_checksum(edid, n);                       /* 修每块校验和 */
int ok = ccs_edid_write("/dev/v4l-subdev2", edid, n, 1); /* --set-edid 等价+读回验证 */
```

- ioctl 路径与 `v4l2-ctl --get-edid/--set-edid` 完全等价：板端 4.19 内核
  `v4l2-subdev.c` 直接处理 `VIDIOC_G_EDID/VIDIOC_S_EDID`（请求号 40/41，
  即新内核的 `VIDIOC_SUBDEV_G_EDID/S_EDID`），结构体为 `struct v4l2_edid`
  {pad, start_block, blocks, reserved[5], edid*}
- 本模块自带该结构体与 ioctl 号定义（Linaro 工具链的 4.4 内核头里没有
  struct v4l2_edid），布局与内核 uapi 逐字节一致
- ⚠ 写 EDID 会让源端重新协商输出、中断画面几秒，主服务运行期间乱写会
  破坏其对齐状态——**写之前先停主服务**；写后需要重对齐

### ccs_align — 相位估计与软件消卷（对应 _measure_roll/_split_fix/_seam_roll）

```c
int k, conf = ccs_align_measure(cur, ref, w, h, &k); /* 参考帧轮廓相关 */
int k2 = ccs_align_split_fix(cur, w, h);             /* 分截签名（绝对锚点） */
int k3 = ccs_align_seam(last3frames, w, h);          /* 接缝检测（3 帧） */
ccs_align_deroll(raw, out, w, h, k);                 /* 循环上移 k 行消卷 */
int roll = ccs_align_nudge(roll, +10, h);            /* 微调/翻半屏 99999 */
```

算法与阈值全部照抄主服务实测值（注释里逐条注明对应 Python 函数）：

| C 函数 | Python 对应 | 方法 | 置信闸门 |
|---|---|---|---|
| `ccs_align_measure` | `_measure_roll` | 行亮度轮廓循环相关（隔 8 列采样，粗搜步长 2 再 ±2 细化） | best<6 且 best<0.5×均值 |
| `ccs_align_split_fix` | `_split_fix` | 任务栏被帧边界切成上下两截的签名 | 顶截≥2 行、底截≥20 行、中间是桌面 |
| `ccs_align_seam` | `_seam_roll` | 卷绕帧接缝（源底行/顶行拼接处亮度跳变） | 强度≥15 且≥4×中位数；≥1.8×次强；3 帧位置一致 |

建议的置信链顺序（同主服务）：`split_fix` → `measure`（有参考帧时）→
`seam`（无参考帧自举）。都不置信就保持 roll=0，由用户 nudge。

## 与 ccs_webcam.py 的对应关系总表

| SDK | Python 主服务 | 说明 |
|---|---|---|
| `ccs_cap_open/stream_on/read_frame/close` | `Capture._open/_alloc/stream_on/grab_latest/_free` | ioctl 序列逐条一致 |
| `ccs_edid_read/write` | `get_edid_hex/set_edid_hex`、`ccs_channel.sh` 的 v4l2-ctl 调用 | 同一 ioctl 号与结构体 |
| `ccs_edid_valid` | `edid_ok` | 排除对齐用垃圾 EDID |
| `ccs_edid_fix_checksum` | `build_edid` 末尾校验和 / `--fix-edid-checksums` | 每 128 字节块独立修正 |
| `ccs_align_*` | `_measure_roll/_split_fix/_seam_roll`、消卷切片、`/nudge` | 阈值原样移植 |

## examples/

- `demo_capture [dev] [w] [h]`：抓 10 帧存 `frame_NN.raw`（UYVY），打印首帧
  字节数/前 16 字节/亮度均值
- `demo_edid [subdev]`：自动扫描（或指定）subdev 读出 EDID，hex 打印 +
  厂商/监视器名/详细时序摘要；写 EDID 示例代码已给出但**默认注释掉防误触**
- `demo_align [dev] [w] [h]`：连抓 5 帧，三种方法估计卷动相位并打印建议的
  消卷/nudge 量

## 注意事项

1. **/dev/video0 被主服务占用时 demo_capture/demo_align 打不开是正常的**
   （open EBUSY 或抓不到帧）。用前先停主服务：
   `systemctl stop ccs_webcam`，用完 `systemctl start ccs_webcam`。
   demo_edid 只读操作不需要停。
2. 分辨率跟随：源实际输出分辨率以 `v4l2-ctl -d <subdev> --query-dv-timings`
   为准；主机会强制输出 EDID 之外的分辨率，按错误几何采集会得到半屏/绿屏。
3. 写 EDID 存在芯片 RAM，断电即丢；出厂 720p EDID 在
   `ccs_dev/edid_720p.hex.txt`（板端 `/usr/sbin/`）。
4. VGA 通道（MS9288C）与 HDMI 共用同一 /dev/video0 与 subdev 节点
   （TMUX121 切换，设备树始终只有 4-000f），本 SDK 对两通道同样适用；
   通道切换由主服务重启完成，SDK 不涉及。
5. 未移植项（主服务有、本 SDK 没有）：任务栏结构检测 `_taskbar_roll` /
   `_taskbar_roll_cwf` / `_floating_band_roll`（Windows 桌面特征兜底，
   体量大）、对齐序列编排 `align_and_start`（含 i2c 清中断锁存、防 watchdog
   循环）、相位持续跟踪 track 子进程、JPEG 编码/缩放/亮度锐化补偿。
   需要完整对齐流程时建议参考 `align_and_start` 自行编排本 SDK 的原语。
