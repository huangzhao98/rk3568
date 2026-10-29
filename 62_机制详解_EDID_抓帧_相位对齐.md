# 62 机制详解：EDID 写入 / 抓帧 / 相位对齐（2026-09-23，以 ccs_webcam.py 2412 行为准）

> 注：2026-10-26 "开机自动对齐加固"（50 文档 §13）改动后文件约 2913 行，
> 文中 web:NNN 行号仍以 09-23 版为基准，机制描述已就地更新到最新行为。

> 读者：接手项目的嵌入式工程师。目标：看完能用 C 或命令行独立复现三件事——
> 写 EDID、从 /dev/video0 拿到画面、消除垂直卷屏。
> 所有机制均标注 ccs_webcam.py 行号（文中简写 `web:NNN`）或对应脚本行号；
> 代码里没有的机制不写。板端同源文件在 /usr/sbin/，主机端权威目录 ccs_dev/。

## 0. 链路与文件总览

```
HDMI 源 ──> TC358743(U15, i2c-4 0x0F) ─┐
                                       ├─ TMUX121(U20, SEL=gpio41) ─> MIPI CSI 4-lane
VGA 源 ──> MS9288C ──> TC358743(U22) ─┘    (dphy0 → mipi_csi2 host → rkcif_mipi_lvds)
                                                          │
                                                    /dev/video0  ──> ccs_webcam.py
```

- 两颗 TC358743 经 TMUX121 二选一，设备树里永远只有 `4-000f` 一个节点
  （ccs_channel.sh:5-7 注释；10_rk3568-atk-ccs.dtsi:168-198）。
- MIPI 走线：tc358743_out → dphy0_in_tc358743(endpoint@4) → dphy_rkcif_out →
  mipi_csi2_input → mipi_csi2_output → cif_mipi_in（dtsi:184-305）。
  全程 `data-lanes = <1 2 3 4>`（4-lane），`link-frequencies = 297MHz`
  （=594Mbps/lane，dtsi:196）。**不能跳过 mipi_csi2 host**，否则 rkcif 零中断零帧
  （dtsi:258-259 注释）。
- 本文三个机制对应的代码区：
  EDID = web:150-303 + ccs_init.sh；抓帧 = web:92-95(ioctl 号) + Capture 类 web:571-859
  + v4l2_cap.py；对齐 = web:964-1348 + 采集主循环 web:1993-2100。

---

## 1. 怎么写 EDID

### 1.1 项目用到的 128 字节字段

`build_edid()`（web:180-222）从明文字段生成完整 EDID。字节布局：

| 偏移 | 内容 | 代码 |
|---|---|---|
| 0~7 | 固定头 `00 ff ff ff ff ff ff 00` | web:195 |
| 8~9 | 厂商 ID：3 字母各取 5bit（`ord(c)-64`）拼成 16bit 大端 | web:196-199 |
| 10~11 | 产品码，uint16 小端 | web:200 |
| 12~15 | 序列号，uint32 小端 | web:201 |
| 16 / 17 | 生产周 / 年份-1990 | web:202 |
| 18~19 | EDID 版本 1.4 | web:203 |
| 20 | 0x80 = 数字输入 | web:204 |
| 21~22 | 屏幕物理尺寸 hcm/vcm（cm） | web:205 |
| 23 | gamma 2.2（写 120） | web:206 |
| 24 | 特性位 0x06（沿出厂值） | web:207 |
| 25~34 | 色度坐标 10 字节（EDID_CHROM，沿出厂 720p EDID 实测值） | web:160, 208 |
| 38~53 | 标准时序：全填 0x01（无） | web:209-210 |
| 54~71 | 详细时序描述符 DTD（`_dtd()`，web:163-177） | web:211 |
| 72~89 | 监视器名描述符 `00 00 00 FC 00` + 名字 + `0x0A` + 空格补齐 | web:212-218 |
| 90~94, 108~112 | 哑描述符 ×2 `00 00 00 10 00` | web:219-220 |
| 127 | 校验和：`(-sum(ed[:127])) & 0xff`（全 128 字节和 ≡ 0 mod 256） | web:221 |

DTD 18 字节内部（`_dtd`，web:163-177）：pclk 小端（单位 10kHz）→ 行有效/消隐低字节
→ 高 4bit 拼接 → 场有效/消隐同法 → 同步偏移/脉宽低位 → 高低位拼字节 b[11] →
物理尺寸 mm（hcm*10/vcm*10）→ b[17]=0x1e（数字分离同步 +hsync +vsync）。

分辨率预设就三张表项（`EDID_TIMINGS`，web:153-157），pclk 单位 10kHz：

| 预设 | pclk | 像素时钟 | 行/场 |
|---|---|---|---|
| 720p60 | 7425 | 74.25MHz | 1280(+370) × 720(+30) |
| 1080p60 | 14850 | 148.5MHz | 1920(+280) × 1080(+45) |
| 1080p30 | 7425 | 74.25MHz | 同上（靠帧率减半） |

对照出厂文件 edid_720p.hex.txt 可验证：字节 54 起 `01 1d` = pclk 7425，
`00 72 51` = ha 1280 / hb 370，名字区 `43 43 53 2d 48 44 4d 49` = "CCS-HDMI"。

**产品码转 hex 的坑**：网页表单 `/edid_build` 里 product 是按 **16 进制**解析的
（web:2184 `int(form.get('product','0'), 16)`）——输入 `1234` 得到的是 0x1234=4660
而不是十进制 1234。自己在 C 里复现时注意 build_edid 的参数域校验
（web:190-193）：product ≤ 0xFFFF、serial ≤ 0xFFFFFFFF、year 1991~2245、
名字 1~13 个可打印 ASCII、厂商 ID 恰好 3 个大写字母。

### 1.2 写入路径与易失性

- **写到哪个节点**：不是 /dev/video0，而是 tc35874x 的 subdev（/dev/v4l-subdevX）。
  定位方法（`find_subdev`，web:123-124，ccs_init.sh:35 同法）：
  ```bash
  SUBDEV=$(media-ctl -d /dev/media0 -p 2>/dev/null | \
    awk '/tc35874x/{f=1} f && /device node name/{print $NF; exit}')
  ```
- **用什么命令**：`set_edid_hex()`（web:272-303）把 hex 文本落到 /tmp/edid_web.hex，
  然后（web:284-285）：
  ```bash
  v4l2-ctl -d $SUBDEV --set-edid file=/tmp/edid_web.hex,format=hex [--fix-edid-checksums]
  ```
  对应内核机制是 subdev 的 `VIDIOC_SUBDEV_S_EDID` ioctl（tc35874x 驱动把 EDID
  经 I2C 写进芯片内部 RAM 并翻转 HPD）；网页层不直接发 ioctl，全部走 v4l2-ctl。
- **写后必须回读验证**（web:286-296）：`--get-edid` 读回逐字节比较；勾了自动修复
  校验和时跳过每 128 字节块的最后一字节比较。验证通过才把这份存到
  /tmp/ccs_last_edid.txt（web:298-302），供切通道时补写到新芯片。
  ccs_init.sh:37-41 另记一坑：**v4l2-ctl 对该 subdev 会先报一个无害的 QUERYCAP
  错误、退出码恒 255，不能用退出码判断成败**，必须回读 grep "43 43 53"（"CCS"）。
- **易失性**：EDID 存在 TC358743 片内 RAM，**断电即丢**。每次开机由
  ccs_init.sh:27-48 重写出厂 720p EDID（/usr/sbin/edid_720p.hex.txt，web:97 的
  EDID_GOOD）；写 EDID 触发 HPD 翻转，源端随即重读并限输出 720p60
  （ccs_init.sh:29-30 注释）。切通道时 ccs_channel.sh:74-80 给新选中的芯片写
  出厂 EDID，ccs_channel_apply.sh:13-18 再用 /tmp/ccs_last_edid.txt 覆盖回用户
  上次应用的 EDID（/tmp 重启即清，所以跨重启只保出厂份）。

### 1.3 写完源端发生什么

写 EDID = HPD 翻转 → 源端重读 EDID、重新协商输出分辨率。网页 EDID 区提示原文
（web:1823-1824）："应用后源端会重新协商输出，采集自动重对齐，画面中断几秒属
正常。EDID 存在芯片 RAM，板子断电即丢（开机由 ccs_init.sh 重写）。"
三个 EDID 应用入口（/edid_apply、/edid_build、/edid_preset，web:2173-2203）
成功后都置 `cap.need_realign`，采集循环随即重走对齐序列（见第 3 节）。

**lane 数与分辨率限制**（web:1814-1815 网页警告原文）："1080p60 需 4-lane MIPI，
当前 EVB 为 2-lane 仅支持 720p60/1080p30；设了带不动的主信号会采不到帧，恢复
出厂 EDID 即可。" dtsi:186-188 注释给出带宽账：4-lane@594Mbps 上限 = 1080p60
（UYVY 需求 891Mbps/lane）；旧 EVB 转接模块仅 2-lane（lane3/4 未引出），上限
1080p30/720p60。CCS 正式板（0916A）4-lane 全引出。

### 1.4 网页 HEX 编辑 + 自动修复校验和

page_edid（web:1816-1822）的高级折叠块：textarea 里是 `get_edid_hex()` 读回的
当前 EDID（web:250-257，只收 16 字节/行的纯 hex 行），提交到 /edid_apply →
`set_edid_hex(hextext, fix='fix' in form)`（web:2174）。勾"自动修复校验和"就加
`--fix-edid-checksums`（web:284），由 v4l2-ctl 重算每块最后一字节；回读比较时
相应跳过块尾字节（web:291-294）。`edid_ok()`（web:921-928）另有有效性判断：
长度 ≥128、负载非全零、首块校验和正确——**全零负载是对齐老方案刷的垃圾
EDID**，绝不能当"原 EDID"恢复。

### 1.5 命令行完整复现

```bash
# 0) 定位 subdev
SUBDEV=$(media-ctl -d /dev/media0 -p 2>/dev/null | \
  awk '/tc35874x/{f=1} f && /device node name/{print $NF; exit}')

# 1) 读当前 EDID
v4l2-ctl -d $SUBDEV --get-edid > /tmp/e.hex        # 退出码 255 也正常，看内容

# 2) 改一个字节并修校验和（例：产品码低字节取反）
python3 - <<'EOF'
d = bytearray(int(x,16) for x in open('/tmp/e.hex').read().split())
d[10] ^= 0xff                       # 产品码低字节（web:200 处）
d[127] = (-sum(d[:127])) & 0xff     # 校验和公式 = web:221
open('/tmp/e2.hex','w').write('\n'.join(
    ' '.join(f'{b:02x}' for b in d[i:i+16]) for i in range(0,128,16))+'\n')
EOF

# 3) 写回（也可以不手修校验和，交给 --fix-edid-checksums）
v4l2-ctl -d $SUBDEV --set-edid file=/tmp/e2.hex,format=hex
#   等价：v4l2-ctl -d $SUBDEV --set-edid file=/tmp/e.hex,format=hex --fix-edid-checksums

# 4) 回读验证（厂商区应见 "43 43 53"=CCS）
v4l2-ctl -d $SUBDEV --get-edid | grep -a "43 43 53"

# 5) 恢复出厂
v4l2-ctl -d $SUBDEV --set-edid file=/usr/sbin/edid_720p.hex.txt,format=hex
```

---

## 2. 怎么获取画面

### 2.1 格式与分辨率从哪来

- 像素格式恒为 **UYVY（UYVY8_2X8）**，每像素 2 字节。采集节点 /dev/video0
  （rkcif_mipi_lvds，VC0 数据落在 id0，ccs_init.sh:77-78 注释）。
- **分辨率不是拍脑袋定的，是问芯片的**：`query_dv()`（web:127-143）执行
  `v4l2-ctl -d $SUBDEV --query-dv-timings`，正则取 Active width/height 和
  Pixelclock/fps；无信号时全 0（`has_signal()` = w>0，web:146-147）。
  `_follow_source()`（web:891-917）在对齐时把源分辨率同步到 cap：主机会强制
  输出 EDID 之外的分辨率（EDID 720p 但显卡自定义 1080p），不跟随就只采到上半屏；
  分辨率变化时参考帧作废、roll 清零、硬编按新几何重建。

### 2.2 Capture 类开设备的 ioctl 顺序

ioctl 号硬编码在 web:92-95（arm64 数值）：

```python
VIDIOC_REQBUFS, VIDIOC_QUERYBUF = 0xC0145608, 0xC0585609
VIDIOC_QBUF, VIDIOC_DQBUF = 0xC058560F, 0xC0585611
VIDIOC_STREAMON, VIDIOC_STREAMOFF = 0x40045612, 0x40045613
MPLANE, MEM_MMAP = 9, 1        # VIDEO_CAPTURE_MPLANE / MMAP
```

**是 multiplanar（MPLANE）**，rkcif 只出 1 个 plane。两个结构体的手工打包
（v4l2_cap.py:23-33 注释版最清楚）：

- `struct v4l2_requestbuffers` 20 字节：`<IIII` = count, type=MPLANE,
  memory=MMAP, reserved（web:645-647，申请 4 个，断言 nbuf>=2，web:648-649）。
- `struct v4l2_buffer`（arm64）88 字节：index@0、type@4=MPLANE、memory@60=MMAP、
  planes 指针@64（指向 64 字节 plane 数组）、length@72=1（`_buf()`，web:614-621）。

完整顺序（对照 `_open`/`_alloc`/`stream_on`，web:623-679）：

```
1. open("/dev/video0", O_RDWR)                          web:624
2. VIDIOC_REQBUFS(count=4)                              web:647
3. 逐 buffer: VIDIOC_QUERYBUF → 取 plane.length(@plane+4)
   和 m.offset(@plane+8) → mmap(fd, plen, offset=poff)  web:652-658
   （注意：python 侧不发 VIDIOC_S_FMT！格式是 shell 里
    v4l2-ctl --set-fmt-video 配的，见 2.3）
4. 逐 buffer VIDIOC_QBUF 入队                            web:677-678
5. VIDIOC_STREAMON(MPLANE)                              web:679
6. 循环: select → VIDIOC_DQBUF → 读 mmap → VIDIOC_QBUF   web:687-711
```

取帧用"排干队列只留最新"策略（`grab_latest`，web:687-711）：第一次 select 带
timeout，之后 timeout=0 把队列里所有已完成的 buffer 全 DQBUF 出来，旧帧直接
QBUF 还回，只读最后一个 mmap 的前 `w*h*2` 字节。**mmap 比帧小（格式掉了、buffer
是旧尺寸）时 read 被截断，短帧返回 None**——交出去会编出绿屏（web:709-711）。

### 2.3 配格式的顺序坑（_config_stream，web:862-880）

5.10 内核的 `vb2_is_busy()` 看的是已申请 buffer 数而不是是否在流：**只要
REQBUFS 过，S_FMT 就被 rkcif 以 queue busy 拒绝**。所以配格式必须：

```python
cap.stream_off()      # 1. 停流
cap._free()           # 2. REQBUFS(count=0) 释放 buffer
# 3. media-ctl 给链路 5 个实体配 UYVY8_2X8/wxh：
#    'm00_b_tc35874x 4-000f':0、rockchip-csi2-dphy0:0/:1、
#    rockchip-mipi-csi2:0/:4
# 4. v4l2-ctl -d /dev/video0 --set-fmt-video=width=W,height=H,pixelformat=UYVY
# 5. v4l2-ctl --get-fmt-video 回读确认（S_FMT 可能静默失败！驱动会停在
#    640x480/RG10 默认格式，帧照流但内容是垃圾）
cap.realloc()         # 6. 按当前节点格式重新 REQBUFS+mmap
cap.stream_on()       # 7. 确认格式对了才开流
```

**无信号时节点格式是 0x0**：此时 REQBUFS 按 0 尺寸申请必失败（内核
vb2-dma-sg 已打补丁返回 ENOMEM 而不再 oops，web:630-632 注释；对应 50 文档/61
地图里的 videobuf2-dma-sg.c 修改）。`_open()` 的做法是先按目标分辨率用
media-ctl + v4l2-ctl 把链路格式配好再重试（web:635-640）；还失败就抛给 main，
main 里每 2s 重试直到链路就绪（web:1970-1977）——不能让无信号开机导致服务
连管理页都起不来。

### 2.4 帧拿到后做什么

主循环（`loop`，web:1993-2100）拿到 raw 后的流水线：

1. **软件消卷**（有 roll 时，web:2059-2074，详见第 3 节）；
2. 经单槽信箱 `cap.mail` 投给编码线程（web:2092-2094，编码追不上就丢帧）；
3. 编码线程 `enc_loop`（web:2102-2137）调 `uyvy_to_jpeg()`（web:784-859）：
   - 采集分辨率 >720p 先 `_scale_uyvy()` 降到 720p 框内（web:791-792；
     RGA 硬缩 `uyvy_scale` ~3ms，web:761-769；无 RGA 回退 PIL ~190ms 仅应急）。
     原因：vepu 硬编 720p ~11ms/帧而 1080p >1s/帧（`_enc_size`，web:884-888）。
   - **MJPEG 硬编走 ctypes 调 /usr/lib/libmppjpeg_shim.so**（web:34-41 加载，
     `mppjpeg_open/encode`，清晰档默认路径）；连续失败 3 次永久回退 PIL
     （web:836-837）。双实例流水 submit/get 实测无收益恒停用（web:609-611）。
   - PIL 回退路径用 **JPEG-YCbCr 捷径**：JPEG 本身就是 YCbCr，UYVY 拆出
     Y/U/V 平面、limited→full 用查找表 `_LUT_Y/_LUT_UV`（web:89-90，避免
     int16 溢出坑），U/V repeat 回全宽后 `Image.frombytes('YCbCr',...)` 直存
     JPEG，跳过 YUV→RGB 矩阵（web:841-858）。流畅档（half）再隔行隔列降到
     640x360 且固定走 PIL（web:799, 843-846）。
4. 编出的 JPEG 存 `cap.frame` 并 `cond.notify_all()`（web:2119-2122）。

`/stream` 端点（web:2378-2396）发 MJPEG：HTTP 头
`Content-Type: multipart/x-mixed-replace; boundary=frame`，每帧写
`--frame\r\nContent-Type: image/jpeg\r\nContent-Length: N\r\n\r\n` + JPEG + `\r\n`；
`Connection: close`（无限流无 Content-Length，HTTP/1.1 下按关连接结束）。
网页 `<img src="/stream">` 直接显示（web:1882）。

### 2.5 为什么 v4l2-ctl --stream-to 不行，落盘用 v4l2_cap.py

ccs_init.sh:80-82 注释原文："v4l2-ctl 1.22 的 --stream-to 在本驱动上写文件为空
（0 字节），落盘抓帧用 /usr/sbin/v4l2_cap.py"。验证流是否正常可以用
`--stream-to=/dev/null`，要拿 raw 数据就用 v4l2_cap.py——它就是 2.2 节那套
MPLANE mmap 流程的最小实现（68 行，只用标准库），抓 N 帧只存最后一帧
（前序帧可能不完整，v4l2_cap.py:61-63）。

### 2.6 命令行/代码复现要点

```bash
# 1) 配链路格式（5 个实体，少一个都收不到帧）
for ent in 'm00_b_tc35874x 4-000f:0' 'rockchip-csi2-dphy0:0' 'rockchip-csi2-dphy0:1' \
           'rockchip-mipi-csi2:0' 'rockchip-mipi-csi2:4'; do
  media-ctl -d /dev/media0 --set-v4l2 "\"${ent%:*}\":${ent##*:}[fmt:UYVY8_2X8/1280x720]"
done
# 2) 配节点格式并回读确认（必须回读！S_FMT 可静默失败）
v4l2-ctl -d /dev/video0 --set-fmt-video=width=1280,height=720,pixelformat=UYVY
v4l2-ctl -d /dev/video0 --get-fmt-video          # 应见 1280/720 和 'UYVY'
# 3) 查源实际时序（分辨率跟随的依据）
v4l2-ctl -d $SUBDEV --query-dv-timings
# 4) 抓帧落盘（--stream-to 会写 0 字节，别用）
python3 /usr/sbin/v4l2_cap.py /dev/video0 /tmp/frame.raw 3
python3 /usr/sbin/uyvy2png.py /tmp/frame.raw /tmp/frame.png 1280 720
# 流式验证（不落盘）可用：
v4l2-ctl -d /dev/video0 --stream-mmap --stream-count=100 --stream-to=/dev/null
```

C 语言复现就是 v4l2_cap.py 的直译：open → REQBUFS(4, MPLANE, MMAP) →
QUERYBUF/mmap ×n → QBUF ×n → STREAMON → select/DQBUF/read/QBUF → STREAMOFF。
注意 `struct v4l2_buffer` 在 arm64 是 88 字节、planes 指针在偏移 64
（v4l2_cap.py:23-30）。

---

## 3. 怎么对齐相位防卷屏

> **2026-10-27 更正（延迟解静音补丁已落地）**：本节 3.1 的部分结论已过时。
> tc35874x 补丁（kernel commit ed20e8029 + 53deb98f6）消除了服务重启/驱动
> 轮询造成的相位自摇，且**重锁相位也不再随机**：失锁期间按住 VI_MUTE 强制
> 静音、TMDS 连续锁定 4s 后才解除（deferred unmute），帧结构只在干净锁定下
> 重建，相位钉回常数 C0（720p 实测 C0=707，3 次重启+1 次 HPD 重锁全部一致）。
> 终版架构 = 延迟解静音驱动 + roll 持久化（/var/lib/ccs_roll）+ 测量链能测
> 就测 + seam-track 自愈（弱证据不落盘不进参考帧）+ /realign 手动兜底。
> 详见 `50_内核定制修改存档.md` §16。
> （2026-10-29：seam-track 已整体删除（§25 零图像识别），且"相位钉回常数"
> 的模型也被 §27 修正——恒定的是 TX 帧结构，DMA 侧首流相位仍随机，
> 靠 FS 帧头重锚钉 0。）

### 3.1 卷屏的成因（文件头注释，web:7-15 实测结论）

> **2026-10-29 再更正（§27，本段旧结论已被逐帧实测推翻）**：
> rkcif 的 DMA 相位规律是——
> - **TX 未出数时布防（开机首流 / 静音后 / 脏重锁后）→ 锁到帧中间，
>   相位随机**（实测卷 78/171/281/590 行不等）；
> - **TX 已流式时重启采集管线 → rkcif 按 FS 短包（帧头）重新锁定，
>   相位恒 0**（4/4 实测 corr=1.000）。
> 因此硬件层面**存在确定性对齐**：信号稳定流式后再做一次
> `_config_stream`（FS 帧头重锚）即钉 0，这就是现行 align 的
> "TX 已流式，重启管线按帧头重锁相位"步骤（reboot×3 全 shift=0）。
> 下面原文中"不等 FS 短包""不存在确定性对齐"的描述仅对"首流布防早于
> 数据"场景成立，保留作历史记录。

rkcif 的 DMA 相位锁定在"**开流后收到的第一个数据**"，不等 FS 短包。有信号时
开流 → 从帧中间开始写 → 画面垂直卷动，且**运行中永不自行恢复**。运行中源端
断信号再恢复（休眠/拔插）、驱动轮询触发 format_change 静音+重开流，同样会把
相位改随机。硬件层面**不存在确定性对齐**（align_and_start docstring，
web:1190-1191：relock 后相位随机 ±31 行，mute/unmute 后相位全随机；5.10 内核
无 subdev s_stream ioctl）——所以本项目一律"测量相位 + 软件循环移位消卷"。

**HDMI 与 VGA 的差异在本项目里的实际形态**：VGA 路是 MS9288C 把模拟 VGA 转成
HDMI 再进同一颗（另一路）TC358743，对采集侧而言两路都是数字 HDMI 时序，**卷屏
机制和消卷代码完全共用**（同一个 align_and_start）。MS9288C 的 SCL/SDA 原理图
未接（打×），固件独立运行，**代码里没有任何对 VGA 侧做硬件相位/增益调整的
通道**（ccs_init.sh:50-52；set_bright docstring web:720-721 明说"调不了芯片增益，
只能在采集端对 Y 分量做数字补偿"）。VGA 模拟采样带来的偏暗/模糊由 3.7 节的
数字亮度/锐化补偿处理。

### 3.2 对齐序列 align_and_start（web:1184-1301）

老方案"刷垃圾 EDID 造静默窗口"已废弃（会让 Windows 反复断连副屏，是对齐闪烁
循环的根源，docstring web:1185-1189）。现方案：

```
1. cap.in_align.set()                       屏蔽 watchdog（web:1196）
2. 开 TcI2c 后台线程以 20Hz 清芯片中断锁存（web:1199-1204，见 3.6）
3. 最多重试 3 轮（max_retry=3），每轮：
   a. cap.state = f"aligning#{attempt+1}"   （web:1206，网页状态栏可见）
   b. 无信号 → break 快速失败               （web:1207-1208）
   c. _follow_source 跟随源分辨率            （web:1209）
   d. stream off/on 重配格式开流             （_config_stream，web:1214）
      → s_stream 传导芯片解除静音（唯一途径），从帧中间恢复输出，
        相位随机但之后恒定，交给软件消卷
   e. 等内容帧连续稳定 10 帧（行亮度峰值 >30 才算内容帧；
      窗口 75s；连续 5 次空帧 ≈20s 放弃本轮）（web:1218-1236）
   f. 测相位 k（四级链，见 3.3），置信则 cap.roll = k（web:1278）
   g. 置信才更新参考帧 cap.ref 并持久化（接缝结果不落盘）（web:1282-1285）
4. 全失败 → cap.state = "align failed"（web:1296），画面保持 roll=0，
   等用户点"重新对齐"或看门狗再次触发
5. finally：停清锁存线程、in_align.clear()（web:1298-1301）
   成功后清锁存再续 5s：恢复瞬态攒下的锁存若被驱动 1s 轮询读到会触发
   format_change 静音+重开流改变相位（web:1291-1294）
```

`aligning#N` 就是第 N 轮重试（共 3 轮）；`align failed` = 3 轮都没拿到稳定内容帧
或没测出置信相位——常见于源端完全无信号、或画面内容让四级检测全部不置信。

### 3.3 相位 k 的测量（四级链，web:1237-1270）

`cap.roll` 语义（web:591）：**buffer 第 b 行 = 源画面第 (b+roll)%h 行**。
测量只逐行看亮度轮廓（`_row_profile`，web:983-988：UYVY 亮度在奇数字节，
隔 8 列采样，1080p 全列相关 ~800ms 会卡采集循环，采样后 ~100ms）。
按优先级（注释 web:1237-1243）：

| 级 | 函数 | 机制 | 关键阈值 |
|---|---|---|---|
| 1 分截签名 | `_split_fix` web:1012-1039 | 任务栏被帧边界切成上下两截：帧顶/帧底连续行 std 高且亮度明显亮过桌面，中间是大段桌面。绝对锚点，不依赖参考帧 | 行判据 std>30 且 p>桌面+15；顶截 2~h/8 行；底截 ≥20 行（<h/6）；两截间必须桌面。命中 k = h−顶截行数 |
| 2 任务栏结构 | `_taskbar_roll` web:1107-1150 / `_taskbar_roll_cwf` web:1153-1181 | 扫描全部候选相位（步长 2，±1 细化）找 Windows 任务栏顶沿/横带；自带几何校验不会自我锁死 | 贴底带 36 行×(h/720)；带内 std≥12、主色覆盖率≥0.7；贴底判据 \|Δp(h-4,h-20)\|≤max(20, 0.25×顶沿)（2026-10-26 起，原固定 20 会误拒带图标的深色任务栏）；顶沿跳变≥15 且≥1.5×次强（次强剔 best_k±3 行邻域，同一物理边在相邻相位强度相同）；cwf 版：40 格中偏离格占比>0.5 为任务栏行，带高 30~100 行且唯一 |
| 3 悬浮横带 | `_floating_band_roll` web:1073-1104 | 纯色背景上唯一高对比横带（彩色任务栏兜底）；带底转到帧底 | 带长 20~80 行×(h/720)，完整在帧内部，带外行必须平坦（偏差≤5） |
| 4 参考帧相关 | `_measure_roll` web:991-1009 | 与已对齐参考帧做行轮廓循环相关（粗搜步长 2 再 ±2 细化），全场景通用 | 置信条件：best_d<6 且 best_d < 0.5×全体候选均值（防平坦画面误判） |
| 5 接缝检测 | `_seam_roll`/`_seam_pos` web:1042-1070 | 卷绕帧接缝=源底行与顶行拼接处的异常跳变；**自举时只在无参考帧时启用、结果永不落盘**（曾把任务栏顶沿误当接缝污染参考帧自我锁死，web:1264-1268）；2026-10-26 起 track 也做接缝复检（见 3.6） | 三重闸门：跳变≥15 且≥4×中位数；最强≥1.8×次强（±3 行邻域外），不唯一时**双峰仲裁**——两强边夹出 15~100·s 行的带则取带下沿（任务栏/Dock 卷到帧中间的场景，50 文档 §13）；连续 3 帧位置一致（±1 行） |

选出 k 后还有防中毒校验：消卷结果若仍带分截签名说明 k 是错的，拒绝置信
（web:1271-1275）。全不置信则 k=0 保持不动。

### 3.4 参考帧哪来（_load_ref 自举）

`cap.ref` 是"已对齐参考帧"（web:594）。来源两个：

1. **对齐置信时保存**：web:1282-1285，持久化到 /var/lib/ccs_ref.raw
   （`REF_PATH` web:966-968，`_save_ref` web:977-981）；
2. **开机/对齐时加载**：`_load_ref`（web:969-975）按当前几何
   `w*h*2` 校验后装入——任务栏自动隐藏/全屏场景重启后无锚点，靠它做轮廓
   相关自举。分辨率变化时参考帧作废（_follow_source，web:914）。
3. **用户 nudge 后重存**：用户目视确认过的画面才是可信参考帧
   （`nudge_save`，web:2075-2079）。

### 3.5 消卷执行与 nudge 微调

消卷在采集主循环（web:2059-2074）：`cap.roll != 0` 时把 raw 循环移动 k 行——

```python
st = cap.w * 2          # 每行字节数
n = cap.roll * st
b[:len(raw)-n] = raw[n:];  b[len(raw)-n:] = raw[:n]   # 乒乓 buffer，替代 bytes 拼接
```

（乒乓缓冲 bytearray 是因 ctypes c_char_p 只收 bytes，`_cp()` 零拷贝转换，web:55-60。）

**⚠ 代码内部不一致，接手必先核实**：主循环这段乒乓切片等价于
`raw[n:] + raw[:n]`（**左**循环移 k 行），而对齐函数、track 子进程、track
回写参考帧三处用的都是 `raw[-k*w*2:] + raw[:-k*w*2]`（**右**循环移 k 行，
web:1281, 1929, 2056）——两处方向相反。2026-09-21 2040 之前的主循环也是右旋
（`raw[-cap.roll*st:] + raw[:-cap.roll*st]`，见
backups/20260921_1938_VGA锐化补偿/ccs_webcam.py:1656），方向是在"乒乓 buffer 替代 bytes 拼接"这次性能改写中翻转的
（50 文档"周期性卡顿根因 + 急速优化"节（2026-09-21 晚~22 晨）第 2 条
只记了性能收益，未提方向）。用项目自身的
`_split_fix` 做合成帧仿真可复现：buffer[b]=src[(b+k)%h] 的卷绕帧，右旋后
任务栏正确归底、左旋后任务栏跑到帧顶（差 2k 行）。测量链（_split_fix/
_measure_roll/_taskbar_roll）与 align/track 全部自洽于右旋，唯一用左旋的是
每帧实际送显的这条路径。板上核实方法：让画面处于 roll≠0 的状态，对比
`curl :8080/frame.raw`（cap.raw，左旋后的送显帧）与 /tmp/align_last.raw
（对齐时存的原始卷绕帧，web:1286）的任务栏位置。

**nudge 按钮**（页面 web:1862-1866，处理 /nudge web:2323-2340）：

- `±1/±10/±50`：`cap.roll = (cap.roll + d) % cap.h`，roll+1 = 内容上移一行；
- `99999`（翻半屏）：语义化为 `d = cap.h // 2`（web:2331-2332）；
- 每个 nudge 的副作用：`cap.ref = None`（旧相位参考帧失效）、
  `cap.nudge_save = True`（下一帧消卷结果重存为新参考帧）、
  **`cap.roll_manual = True`**（用户目视=最终仲裁，后台 track 只记录不覆盖，
  直到下次重对齐才被解除，web:592, 1279, 2046）、`cap.roll_epoch += 1`
  （在飞 track 结果全部作废，防把用户刚调好的画面改回去，web:2040-2045）。

### 3.6 自动重对齐的触发与看门狗

`cap.need_realign` 置位的所有路径：

| 触发 | 代码位置 |
|---|---|
| 网页点"重新对齐" /realign | web:2320-2322 |
| EDID 应用/生成/恢复出厂 | web:2176, 2193, 2200 |
| 看门狗：信号掉过再恢复（休眠/拔插破坏 DMA 相位） | watchdog web:1323-1324 |
| 看门狗：节点格式掉回默认（有信号但 get-fmt-video 不含当前 WxH/UYVY，帧照流但绿屏） | web:1343-1348 |
| 看门狗：芯片 I2C 挂死（>10s 无帧 且（有信号 或 dv 查询耗时>2s），冷却 60s）→ stream_off 后 ccs_chip_reset.sh 硬复位，再重对齐 | web:1326-1342 |
| 采集循环：连续 ~12s 拿不到帧且有信号（链路死了） | web:2010-2016 |
| 切通道：写 /etc/ccs_channel + 1s 后重启服务（重启即走 align_and_start） | chan_switch web:513-530 |
| 开机首次 | web:1987 |

注：**没有周期性自动重对齐**（2026-10-26 用户明确要求）：开机对齐一次，后续
卷动由 /realign 接口或网页按钮手动触发；上表其余触发均为事件驱动。

两个防循环要点：对齐期间 `in_align` 置位时 watchdog 只更新信号基线不触发
（web:1319-1321）——对齐序列本身会引起信号掉/恢复，不屏蔽会被误判成外部事件
无限重对齐、fps 恒 0（web:12-15 实测踩坑）；切通道不做进程内热切换，因为
实测句柄跨 unbind/bind 触发内核 subdev_close 空指针 oops（web:515-517）。

**track 的接缝复检与微差死区**（2026-10-26 新增，50 文档 §13）：track 复测在
分截/任务栏/横带/参考帧四法都不置信时，用 `_seam_pos` 做跨调用累积——连续
3 次复测（~15s）接缝候选位置一致且 ≠当前 roll 才上报纠正（修"开机时顶底同色
接缝不可见、内容变化后错位暴露"的隐错位）；已对齐画面真接缝在帧边界回绕处，
k3==roll 不会误动。另：复测相位与当前 roll 环形距离 ≤3 行一律不动——检测器
窗口量化误差会让已对齐帧被答成 ±2 行微偏，采纳反而把好画面卷偏。

**对齐期 i2c 清锁存**（TcI2c，web:933-961）：从 subdev sysfs 链接推出
i2c 总线/地址（…/i2c-4/4-000f），用 I2C_RDWR ioctl（0x0707，16-bit 寄存器
地址）以 20Hz 把 SYS_INT..KEY_INT（0x8502~0x850F）全部 W1C 清掉。驱动 1s
轮询感知不到 SYNC/DE 变化就不会调 format_change 在恢复瞬态静音芯片——静音后
没有任何东西解除，帧永远回不来（docstring web:934-937）。

### 3.7 VGA 亮度/锐化补偿（与对齐的关系）

- `set_bright`（web:719-728）：亮度增益 20~200%（100=原始），生成 Y 分量
  LUT；`_apply_bright` 只调 UYVY 奇数字节（Y），色度不动色饱和度不变
  （web:730-737）。持久化 /etc/ccs_brightness（web:2341-2352）。
- `set_sharp`（web:739-743）：0~100，unsharp mask `y + k*(y−十字模糊)`，
  k=sharp/50（web:745-759），补偿 VGA 模拟采样（ADC 相位对不齐像素时钟）的
  边缘模糊。持久化 /etc/ccs_sharp。
- 硬编路径下补偿由 C shim 在编码拷贝趟里完成（`mppjpeg_set_filter`，
  web:713-717），python 侧不能再动数据否则双重补偿（web:796-802）；
  PIL/老 shim 才在 python 侧 numpy 做（慢，~80ms/帧仅应急）。
- **与对齐的关系：代码里没有耦合**。对齐/track 测量用的是 grab_latest 出来的
  原始帧（补偿只在编码路径施加），所以补偿参数不影响相位测量；反过来对齐也
  不会重置 bright/sharp（重启后由 web:1978-1985 从 /etc 恢复）。

### 3.8 "清晰/流畅"模式（half 参数）

`cap.half`（web:584，/set 端点 web:2365-2370，页面链接 web:1860）：

- **清晰（half=False）**：全编码几何（≤720p 原样，>720p 先 RGA 降到 720p），
  优先 vepu 硬编；状态栏如 `清晰1280x720(源1920x1080)`。
- **流畅（half=True）**：编码线程隔行隔列降采样到一半（720p→640x360，
  web:843-846），且 `hw_filter = not self.half and ...`（web:799）使硬编路径
  整体跳过——**流畅档固定 PIL 软编小图**，换最低延迟。

### 3.9 自己程序里怎么防卷屏（可操作步骤）

1. **确定性相位已可实现（2026-10-29 §27 更正）**：首流布防早于 TX 出数时
   相位随机，但 TX 已流式时重启采集管线，rkcif 按 FS 帧头重锁、相位恒 0
   （4/4 实测）。首选方案 = 信号稳定流式后再 `_config_stream` 一次钉 0；
   软件循环移位消卷通道保留作兜底（每帧按 k 移位，w*h*2 字节 buffer
   两段拷贝即可）。
2. **开流纪律**：配格式前必须 stream_off + REQBUFS(0)，配完回读
   get-fmt-video 确认，再 REQBUFS/mmap/STREAMON（2.3 节顺序）。
3. **稳帧判据**：开流后先等内容帧连续稳定（本项目：行亮度峰值>30 的帧
   连续 10 帧，窗口 75s）再测相位。
4. **测相位**：最小可用方案是"参考帧行亮度轮廓循环相关"
   （_measure_roll 60 行可直译 C）；有 Windows 桌面场景再加任务栏检测做
   绝对锚点；无参考帧时用接缝检测自举但结果别持久化。置信判断一定要带
   "显著优于平均"的相对条件，平坦画面绝对误差都小。
5. **防中毒**：消卷结果再跑一次分截签名自检，错了就拒绝置信（web:1271-1275）。
6. **看门狗**：周期性 `--query-dv-timings`，信号掉过再恢复 → 重测相位；
   顺带回读节点格式，掉回 640x480/RG10 默认也要重测。
7. **对齐期屏蔽自己的副作用**：重开流/清锁存引起的信号波动不能喂回触发
   逻辑（in_align 模式），否则无限循环。
8. **TC358743 专属**：对齐窗口内以 ~20Hz 清 0x8502~0x850F 中断锁存
   （I2C_RDWR，16-bit 地址，见 TcI2c web:946-951 的 ioctl 打包），
   防止驱动轮询在瞬态静音芯片。

---

## 附：关键数字速查

| 项 | 值 | 出处 |
|---|---|---|
| ioctl | REQBUFS 0xC0145608 / QUERYBUF 0xC0585609 / QBUF 0xC058560F / DQBUF 0xC0585611 / STREAMON 0x40045612 / STREAMOFF 0x40045613 | web:92-94 |
| buffer 类型 | MPLANE=9，MMAP=1，申请 4 个（≥2） | web:95, 646-649 |
| v4l2_buffer 布局 | 88B：index@0 type@4 memory@60 planes@64 length@72 | web:614-621 |
| I2C_RDWR | 0x0707，锁存寄存器 0x8502~0x850F，20Hz | web:951, 954-961 |
| EDID | 128B，校验和 (-sum[:127])&0xff，产品码按 hex 解析 | web:221, 2184 |
| MIPI | 4-lane，297MHz link-freq（594Mbps/lane），UYVY | dtsi:186-196 |
| 稳定判据 | 行亮度峰值>30 连续 10 帧，窗口 75s，空帧 5 次≈20s 放弃 | web:1218-1236 |
| track 复测 | 每 150 帧（~5s）投一帧，对齐后 10s 冷却，独立子进程 | web:2027-2035 |
| 挂死判定 | >10s 无帧 且（有信号 或 dv 查询>2s），复位冷却 60s | web:1326-1328 |
| 编码 | vepu 720p ~11ms/帧，1080p >1s → >720p 一律 RGA 降到 720p 再编；硬编连续失败 3 次永久回退 PIL | web:884-888, 836-837 |
