# C SDK API 文档（ccs_http / ccs_sdk）

对应板子服务 `ccs_webcam.py`（默认 `http://192.168.1.222:8080`）。与 Python
SDK（`sdk/ccs_sdk/`）功能对齐；每个函数标注：功能 / 参数 / 返回 / 副作用。

**通用约定**
- 返回 `0` = 成功，`-1` = 失败；失败原因用 `ccs_error(c)` 取（实例内缓冲，
  下次调用覆盖），对应 Python 版的 `CCSApiError` 消息（尽量带板上原文）。
- 输出字符串：`char *out, size_t len` 由调用者提供缓冲；标注 **malloc**
  的由 SDK 分配、调用者 `free()`。
- 客户端不跨线程并发使用（err 缓冲在实例内）；多线程各建各的实例。

---

## 1. 客户端句柄

### `ccs_client_t *ccs_new(const char *host, int port, int timeout_ms)`
- 功能：构造客户端。`host` 板子 IP/主机名；`port` 传 0 = 默认 8080；
  `timeout_ms` 传 0 = 默认 5000（对齐 Python 版 timeout=5.0）。
- 返回：堆上实例（`ccs_free` 释放），失败 NULL。
- 副作用：无（不发任何请求）。

### `void ccs_free(ccs_client_t *c)` / `const char *ccs_error(const ccs_client_t *c)`
- 释放实例 / 取最后一次失败的错误描述。副作用：无。

---

## 2. 视频采集（对应 `video.py`）

### `int ccs_video_stat(ccs_client_t *c, char *out, size_t len)`
- 功能：GET `/stat`，采集状态一行文本，形如
  `30.0 fps | running | 清晰1280x720 | hw编码 | HDMI`（fps / 状态机 /
  编码档位(含源分辨率) / 编码器 / 通道）。
- 副作用：无。

### `int ccs_video_snapshot(ccs_client_t *c, unsigned char **jpg, size_t *jpg_len)`
- 功能：抓一帧 JPEG。板上没有单帧 JPEG 端点，实现为连 `/stream` 取第一个
  分帧后立即断开（同 Python 版）。`*jpg` 是 **malloc** 的完整 JPEG 文件
  内容（可直接写 .jpg）。
- 返回：-1 = 连接失败 / 读超时（无信号期间板上长时间不出帧）。
- 副作用：无。

### `int ccs_video_mjpeg_stream(ccs_client_t *c, ccs_frame_cb cb, void *user, int timeout_ms)`
- 功能：GET `/stream` MJPEG 无限流，逐帧回调 `cb(jpg, len, user)`；回调
  返回非 0 停止拉流。
- 分帧格式（板上写死）：`--frame\r\nContent-Type: image/jpeg\r\n`
  `Content-Length: N\r\n\r\n<N 字节 JPEG>\r\n`。无限流 Connection: close、
  无整体 Content-Length，必须按分帧头逐帧解析（SDK 内部处理）。
- 参数：`timeout_ms` 每次 socket 读超时；传 0 = max(客户端超时, 10000)
  ——无信号期间服务端 5 秒空转重试不出帧，太短会误超时（同 Python 版）。
- 副作用：占用板上一路流连接（每连接一线程）。

### `int ccs_video_realign(ccs_client_t *c, char *msg, size_t len)`
- 功能：GET `/realign` 触发重对齐序列（画面垂直卷动/错位时用）。
- 返回：`msg` = 板上文本 "已触发重对齐"。
- 副作用：画面中断几秒；解除 nudge 的手动相位锁定（roll_manual）。

### `int ccs_video_nudge(ccs_client_t *c, int d, char *msg, size_t len)`
- 功能：GET `/nudge?d=N` 消卷相位手动微调。`d=+1` 内容上移一行；
  `d=99999` 特殊语义 = 翻半屏（板上替换成 cap.h//2）。
- 返回：`msg` = 板上文本 "相位 +10 → 123"。
- 副作用：即时生效、板上自动保存；标记 roll_manual，自动 track 只记录
  不覆盖（realign 可解除）。

### `int ccs_video_set_mode(ccs_client_t *c, const char *mode)`
- 功能：清晰/流畅档切换（GET `/set?half=0|1`）。`mode` = `"clear"`/
  `"清晰"` 或 `"smooth"`/`"流畅"`。板上 303 不带 msg，返回 0 仅代表
  请求已受理。
- 副作用：只改运行时编码几何（清晰=源分辨率，流畅=640x360），即时生效，
  不落盘。

---

## 3. 通道（对应 `channel.py`）

### `int ccs_channel_current(ccs_client_t *c, char *out, size_t len)`
- 功能：当前通道（从 `/stat` 最后一栏解析），`out` = `"hdmi"`/`"vga"`。
- 副作用：无。

### `int ccs_channel_switch(ccs_client_t *c, const char *ch, char *msg, size_t len)`
- 功能：POST `/channel ch=hdmi|vga` 切换采集通道（TMUX121 复用开关；
  SW2 拨码闭合时硬件强制 VGA，软件设置无效）。
- 返回：`msg` = 板上文本（"正在切换到 VGA，服务重启约 15 秒" 或
  "已在 VGA 通道"）。
- 副作用：**大**——写 /etc/ccs_channel（重启保持）+ 延迟 1 秒重启
  ccs_webcam 服务，约 15 秒内所有 HTTP API（视频流/串口/GPIO）不可用，
  调用方需自行等待重试。

---

## 4. EDID（对应 `edid.py`）

### `int ccs_edid_get_hex(ccs_client_t *c, char **hex_out)`
- 功能：读芯片当前 EDID。无独立 API：抓管理页 `<textarea name=edid>`
  内容（板上 v4l2-ctl --get-edid 读出后 HTML 转义嵌入，SDK unescape
  还原）。`*hex_out` 为 **malloc** hex 文本（每行 16 字节，结尾带换行，
  可直接喂回 `ccs_edid_apply_hex`）。
- 副作用：无。

### `int ccs_edid_apply_hex(ccs_client_t *c, const char *hextext, int fix, char *msg, size_t len)`
- 功能：POST `/edid_apply` 直接应用 HEX EDID。`hextext` 长度须为 128 或
  256 字节（空白分隔均可）；`fix` 非 0 时板上加 --fix-edid-checksums
  自动修复校验和。
- 返回：`msg` = 板上文本 "EDID 已生效，已触发重对齐"；板上校验失败时
  `ccs_error()` 带 err 原文。
- 副作用：写芯片 RAM（断电即丢）；源端重新协商输出；自动重对齐，画面
  中断几秒；成功内容存 /tmp/ccs_last_edid.txt 供切通道补写。

### `int ccs_edid_factory_reset(ccs_client_t *c, char *msg, size_t len)`
- 功能：POST `/edid_preset preset=720p`，恢复出厂 720p EDID（设了带不动
  的信号采不到帧时兜底）。
- 副作用：同上（写芯片 + 自动重对齐，画面中断几秒）。

---

## 5. GPIO（对应 `gpio.py`）

### `int ccs_gpio_list(ccs_client_t *c, ccs_gpio_t **arr, int *count)`
- 功能：GPIO 全表（只收有名脚），抓管理页 GPIO 表格（`id=gvN` 锚点
  区分于网络表）。`*arr` 为 **malloc** 数组，元素含
  `num/name/dir("in"/"out")/val("hi"/"lo")/consumer`。`val` 是页面生成时
  的快照，非实时——实时电平用 `ccs_gpio_read_inputs`。
- 副作用：无。

### `int ccs_gpio_set(ccs_client_t *c, int num, int val, char *msg, size_t len)`
- 功能：POST `/gpio_set` 翻转白名单脚电平（`val` 非 0 = 置高）。**总是
  带 ajax=1**——不带时板上成功回 303 整页跳转（网页表单用法），ajax
  模式才回纯文本结果。
- 返回：`msg` = 板上文本 "gpio41 = hi" 之类；非白名单或被占用时
  `ccs_error()` = "POST /gpio_set → HTTP 403: ..."。
- 副作用：改板上实际引脚电平（sysfs 写 direction=out + value）。白名单
  只有 `WORKING_LEDEN_H` 且无占用者。

### `int ccs_gpio_read_inputs(ccs_client_t *c, ccs_gpio_input_t out[4], int *count)`
- 功能：GET `/gpio_vals` 四路外部输入实时电平（JSON 用 SDK 内置极简
  取值函数解析，不引第三方库）。`out` 容量至少 4，`*count` 返回实际条数
  （板上某脚读失败会缺该键，不补默认值）。
- 输出：`{num, name, level}`——GPIO2_B1=73 LED_YELLOW_STATE（黄）/
  B2=74 绿 / C5=85 蓝 / C6=86 红；`level` 1=高 0=低。
- 副作用：无（板上常驻 export 为输入，读取不改方向；这些脚务必保持
  高阻输入）。

---

## 6. 串口（对应 `uart.py`）

`CCS_UART_RS232` = "232"（/dev/ttyS3，JP5B）；`CCS_UART_RS485` = "485"
（/dev/ttyS4，JP5A，Q10 硬件自动方向，无需软件控制）。

### `int ccs_uart_send(ccs_client_t *c, const char *port, const char *data, int hex, int crlf, char *msg, size_t len)`
- 功能：POST `/uart_send` 发送。`hex` 非 0 时 `data` 须为 hex 字符串
  （空格可选）；`crlf` 非 0 追加 `\r\n`（与网页控制台默认一致）。
- 返回：`msg` = 板上文本 "已发 NB"（N 含 \r\n）。
- 副作用：往对应串口线上实际发出字节。

### `int ccs_uart_send_bytes(ccs_client_t *c, const char *port, const unsigned char *data, size_t data_len, int crlf, char *msg, size_t len)`
- 功能：发原始字节——自动转 "xx xx " hex 字符串并强制 hex 模式（板上
  表单按 UTF-8 解码，任意二进制直接塞文本会被 'replace' 污染）。
- 副作用：同上。

### `int ccs_uart_recv(ccs_client_t *c, const char *port, long since, int hex, char **data, long *next, long *baud)`
- 功能：GET `/uart_recv` 增量拉取。`since` 传上次返回的 `*next`；
  0 = 从最早可用缓冲开始。板端接收缓冲环形 64KB，`rx_total` 是**逻辑
  偏移**（单调递增，不因缓冲丢弃回退）：since 在窗口内→只回增量；
  落后于缓冲起点→给全量现存缓冲。
- 返回：`*data` 为 **malloc** 字符串（`hex` 非 0 时是 "xx xx " 格式）；
  `*next` 新逻辑偏移，下次原样传回；`baud` 可为 NULL。
- 副作用：无（纯读取，不消费板上数据；since 是客户端侧状态，会话间
  互不影响）。

### `int ccs_uart_set_baud(ccs_client_t *c, const char *port, int baud, char *msg, size_t len)`
- 功能：POST `/uart_set` 设波特率（8N1 固定）。白名单：9600/19200/
  38400/57600/115200/230400/460800/921600。
- 返回：`msg` = "baud=N"。
- 副作用：立即改板上该路串口速率（全局共享，影响所有会话）；切换瞬间
  线上在传的字节可能出错。

---

## 7. 网络（对应 `network.py`）

### `int ccs_net_ifaces(ccs_client_t *c, ccs_net_iface_t **arr, int *count)`
- 功能：网卡列表，抓管理页"网络设置"表格（板上解析 ip -brief addr）。
  `*arr` 为 **malloc** 数组，元素含 `iface/state/addr/mode("静态"/"DHCP")`；
  只收 ethX/wlanX/canX 前缀接口。
- 副作用：无。

### `int ccs_net_apply(ccs_client_t *c, const char *iface, const char *mode, const char *addr, const char *gw, const char *dns, char *token, size_t token_len)`
- 功能：POST `/net_apply` 下发配置并启动板上 90 秒回滚定时器。
  `iface` = "eth0"/"eth1"（硬白名单）；`mode` = "dhcp"（删静态配置文件）/
  "static"（`addr` 必填，形如 "192.168.1.50/24" 带前缀）；`gw`/`dns`
  可空（dns 多个空格或逗号分隔）。
- 返回：`token` 输出确认令牌（8 hex 字符），90 秒内传给
  `ccs_net_confirm`。
- 副作用：**最大**——写/删 /etc/systemd/network/10-ccs-<iface>.network
  并 restart systemd-networkd，**旧地址的连接（含本请求这条）立刻断开**；
  90 秒未确认自动回滚旧配置。务必先想好在新地址下的确认路径。

### `int ccs_net_confirm(ccs_client_t *c, const char *token, int *kept)`
- 功能：GET `/net_confirm?token=...` 确认保留新配置。必须在 90 秒窗口
  内、**新地址**下调用（用新地址 `ccs_new` 一个客户端再调）。
- 返回：`*kept` = 1 已保留 / 0 无待确认或 token 不符（不算错误，板上
  两种结果都回 200 HTML，按页面文本判定）。
- 副作用：`*kept`=1 时板上取消回滚定时器，新配置永久生效。

---

## 8. 画质（对应 `image.py`）

### `int ccs_image_set_brightness(ccs_client_t *c, int val, char *msg, size_t len)`
- 功能：GET `/brightness?val=N` 亮度增益（VGA 模拟采样偏暗的补偿，只
  作用 Y 分量）。`val` 20~200（100=原始，越界自动钳位）。
- 返回：`msg` = 板上文本 "亮度 N%"（N 是实际生效值）。
- 副作用：即时改画质 + **持久化** /etc/ccs_brightness（复位用 100）。

### `int ccs_image_set_sharpen(ccs_client_t *c, int val, char *msg, size_t len)`
- 功能：GET `/sharpen?val=N` 锐化强度（unsharp mask 只作用 Y 分量）。
  `val` 0~100（0=关闭，越界自动钳位）。
- 返回：`msg` = "锐化 N"。
- 副作用：即时改画质 + **持久化** /etc/ccs_sharp（复位用 0）。

---

## 9. HID 键鼠/触摸注入（对应 `hid.py`）

**全部函数都会在被控上位机上产生真实输入事件，谨慎调用。** 上位机未
连接 USB 时板上写 hidg 失败回 503（`ccs_error()` 带原因）。坐标一律
归一化 0.0~1.0（内部钳位后乘 32767，与网页 xy() 一致）。修饰键掩码用
`CCS_MOD_LCTRL/LSHIFT/LALT/LMETA/RCTRL/RSHIFT/RALT/RMETA`。

### `int ccs_hid_send_keyboard(ccs_client_t *c, unsigned mods, const unsigned char *keys, int nkeys)`
- 功能：发完整键盘报告 [mods, 0, k1..k6]（hidg0，boot 协议 6KRO，
  不足 6 键补 0）。
- 注意：键盘是**全状态上报**——报告里没列的键视为松开；全部松开必须
  调 `ccs_hid_release_all()`，否则上位机认为键一直按着。

### `int ccs_hid_release_all(ccs_client_t *c)`
- 发空键盘报告，全部键松开。

### `int ccs_hid_key(ccs_client_t *c, const char *name, unsigned mods, double hold_sec)`
- 功能：按一下指定键（按下 → `hold_sec` 秒 → 松开）。`name` 接受完整
  键名（"KeyA"/"Enter"/"F5"/"ArrowUp"...）或单字符简写（"A"~"Z"、
  "0"~"9"）；`mods` 组合修饰键（如 Alt+F4：`ccs_hid_key(c, "F4",
  CCS_MOD_LALT, 0.05)`）。

### `int ccs_hid_mouse_move(ccs_client_t *c, double x, double y, int down)`
- 功能：移动指针到 (x,y)，走 hidg3 笔 digitizer（bits = 4 in-range |
  1 tip，与网页鼠标模式一致；in-range 保证悬停有光标）。`down` 非 0 =
  按住左键拖拽。
- 为什么不用绝对鼠标：Windows 把绝对坐标鼠标锁死在主屏，够不到扩展
  副屏；digitizer 类按显示器关联走（网页实测结论）。

### `int ccs_hid_mouse_down(c, x, y)` / `int ccs_hid_mouse_up(c, x, y)`
- 按住/松开左键。`x < 0` 表示不给坐标（只改按键位，坐标发 0，网页退出
  KVM 时同款写法）。

### `int ccs_hid_click(ccs_client_t *c, double x, double y, double hold_sec)`
- 在 (x,y) 左键点一下。

### `int ccs_hid_right_click(ccs_client_t *c, double x, double y, double interval, int count)`
- 在 (x,y) 点右键：hidg2 触摸流式长按（~60Hz 连发 count 次按下报告，
  再发抬起）触发 Windows 原生"按住=右键"手势。只发一次会被判联系点
  失效（实测），所以**本函数阻塞调用线程约 interval×count 秒**（默认
  0.06×22 ≈ 1.3s）。`interval`/`count` 传 0 用默认值。

### `int ccs_hid_touch(c, x, y, down)` / `int ccs_hid_tap(c, x, y, hold_sec)`
- 单点触摸按下/抬起（hidg2 digitizer，bits = tip | in-range）；`tap`
  = 按下 → 停 → 抬起。

### `int ccs_hid_mouse_abs(ccs_client_t *c, double x, double y, unsigned btn, int wheel)`
- 功能：hidg1 绝对坐标鼠标报告 [btn, xL, xH, yL, yH, wheel]（滚轮只有
  这路有字节位）。`btn` bit0=左 bit1=右 bit2=中；`wheel` -1 下滚/+1 上滚。
- 注意：Windows 下整体锁死主屏（见上），仅在单屏/Linux 或要用滚轮时用。

---

## 10. 底层 HTTP（`ccs_http.h`，一般不需要直接用）

### `int ccs_http_request(host, port, timeout_ms, method, path, form_body, resp, err, errlen)`
- 一次请求一个 TCP 连接（Connection: close）。任何 HTTP 状态码都返回 0
  并填充 `resp`（含 303/4xx/5xx——303 是表单端点的成功路径，403/500
  正文要带回给调用者）；-1 仅是连接层失败。
- **不跟随 303**：板上表单端点把结果放在 Location 的 ?msg=/?err= 里，
  跟随跳转只会拿到管理页 HTML。`resp.location` 原样带出供解析。
- 响应体动态缓冲：有 Content-Length 按长度读，没有则读到 EOF。

### `int ccs_http_open_stream(...)` / `ccs_http_readline()` / `ccs_http_readn()`
- `/stream` 无限流专用：发请求读响应头后把 fd 交给调用者逐帧解析。

### `char *ccs_urlencode(const char *s)` / `void ccs_http_resp_free(resp)`
- URL 编码（malloc 返回）/ 释放响应体。
