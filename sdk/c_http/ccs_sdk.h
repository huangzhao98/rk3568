/* ccs_sdk.h — RK3568 CCS 采集板 HTTP API C SDK（与 Python 版 ccs_sdk 对齐）。
 *
 * 对应板子上的服务 ccs_webcam.py（默认 http://192.168.1.222:8080），
 * 端点、参数名、返回格式均以服务端 do_GET/do_POST 实际代码为准。
 *
 * 约定：
 * - 所有函数返回 0 = 成功，-1 = 失败（失败原因用 ccs_error() 取，与 Python
 *   版的 CCSApiError 消息对应，尽量带板上返回的原文）；
 * - 字符串输出分两类：调用者提供缓冲（char *out, size_t len），或 SDK
 *   malloc 返回（** 标记，调用者负责 free()）；
 * - 写操作的副作用在每个函数的注释里标明（对齐 Python 版 docstring）；
 * - 线程安全：不同 ccs_client_t 实例互不干扰；同一实例不要跨线程并发用
 *   （err 缓冲是实例内的）。
 */
#ifndef CCS_SDK_H
#define CCS_SDK_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- 客户端句柄 ---- */

typedef struct {
    char host[64];      /* 板子 IP 或主机名 */
    int port;           /* 服务端口，默认 8080 */
    int timeout_ms;     /* 单次 HTTP 请求超时（毫秒），MJPEG 流另算 */
    char err[512];      /* 最后一次失败的错误描述（CCSApiError 消息） */
} ccs_client_t;

/* 构造客户端。host 如 "192.168.1.222"；port 传 0 用默认 8080；
 * timeout_ms 传 0 用默认 5000（与 Python 版 timeout=5.0 一致）。
 * 返回堆上实例（ccs_free 释放），失败返回 NULL。无副作用（不发请求）。 */
ccs_client_t *ccs_new(const char *host, int port, int timeout_ms);

/* 释放客户端。 */
void ccs_free(ccs_client_t *c);

/* 最后一次失败的错误描述（实例内缓冲，下次调用会被覆盖）。 */
const char *ccs_error(const ccs_client_t *c);

/* ================= 视频采集（video.py） ================= */

/* GET /stat → 采集状态一行文本。
 * out 形如 "30.0 fps | running | 清晰1280x720 | hw编码 | HDMI\n"，五栏依次
 * fps / 状态机 / 编码档位(含源分辨率) / 编码器 / 通道。副作用: 无。 */
int ccs_video_stat(ccs_client_t *c, char *out, size_t len);

/* 抓一帧 JPEG：连 /stream 取第一个分帧后立即断开（板上没有单帧 JPEG
 * 端点，Python 版 snapshot() 同样这么实现）。
 * *jpg 为 malloc 的完整 JPEG 文件内容（可直接写 .jpg），调用者 free()。
 * 无信号期间板上长时间不出帧会读超时返回 -1。副作用: 无。 */
int ccs_video_snapshot(ccs_client_t *c, unsigned char **jpg, size_t *jpg_len);

/* MJPEG 帧回调：每收到一帧调一次。返回 0 继续拉流，非 0 停止拉流
 * （ccs_video_mjpeg_stream 随即返回 0）。 */
typedef int (*ccs_frame_cb)(const unsigned char *jpg, size_t len, void *user);

/* GET /stream MJPEG 无限流，逐帧回调，直到回调返回非 0 或连接断开。
 *
 * 板上按 "multipart/x-mixed-replace; boundary=frame" 输出，
 * Connection: close、**无 Content-Length**——所以不能用普通 HTTP 读法
 * （读响应体到 EOF 会永远读下去），必须按分帧头逐帧解析（分帧格式见
 * ccs_sdk.c 实现注释）。
 *
 * timeout_ms: 每次 socket 读超时；传 0 取 max(客户端超时, 10000)——
 * 无信号期间服务端 5 秒空转重试不出帧，太短会误超时（同 Python 版）。
 * 副作用: 占用板上一路流连接（板上 ThreadingHTTPServer 每连接一线程）。 */
int ccs_video_mjpeg_stream(ccs_client_t *c, ccs_frame_cb cb, void *user,
                           int timeout_ms);

/* GET /realign 触发重对齐序列（画面垂直卷动/错位时用）。
 * 副作用: 画面中断几秒；解除 nudge 的手动相位锁定（roll_manual 标记）。 */
int ccs_video_realign(ccs_client_t *c, char *msg, size_t len);

/* GET /nudge?d=N 消卷相位手动微调（即时生效、板上自动保存）。
 * d=+1 内容上移一行；d=99999 特殊语义=翻半屏（板上替换成 cap.h//2）。
 * msg 输出板上文本 "相位 +10 → 123"。
 * 副作用: 调用后板上标记 roll_manual，自动 track 只记录不覆盖。 */
int ccs_video_nudge(ccs_client_t *c, int d, char *msg, size_t len);

/* 清晰/流畅档切换：GET /set?half=0|1。
 * mode: "clear"/"清晰" = 清晰档（全分辨率），"smooth"/"流畅" = 流畅档
 * (640x360)。板上 303 不带 msg，返回值仅代表请求已受理。
 * 副作用: 只改运行时编码几何，即时生效，不落盘。 */
int ccs_video_set_mode(ccs_client_t *c, const char *mode);

/* ================= 通道（channel.py） ================= */

/* 当前通道（从 /stat 文本最后一栏解析）。
 * out 输出 "hdmi" 或 "vga"。副作用: 无。 */
int ccs_channel_current(ccs_client_t *c, char *out, size_t len);

/* POST /channel ch=hdmi|vga 切换采集通道（TMUX121 复用开关）。
 * ⚠ 副作用很大: 写板上 /etc/ccs_channel（重启保持）+ systemd-run 延迟 1 秒
 * 重启 ccs_webcam 服务，约 15 秒内**所有** HTTP API（视频流/串口/GPIO）
 * 不可用，后续调用会连接失败——调用方需自行等待并重试。
 * msg 输出板上文本（"正在切换到 VGA，服务重启约 15 秒" 或 "已在 VGA 通道"）。 */
int ccs_channel_switch(ccs_client_t *c, const char *ch, char *msg, size_t len);

/* ================= EDID（edid.py） ================= */

/* 读芯片当前 EDID：GET / 抓管理页 <textarea name=edid> 内容。
 * *hex_out 为 malloc 的 hex 文本（v4l2-ctl --get-edid 格式，每行 16 字节，
 * 结尾带换行，可直接喂回 ccs_edid_apply_hex），调用者 free()。副作用: 无。 */
int ccs_edid_get_hex(ccs_client_t *c, char **hex_out);

/* POST /edid_apply 直接应用 HEX EDID。
 * hextext: hex 文本（空白分隔均可，板上按空白 split 解析），长度须为
 *          128 或 256 字节。
 * fix:     非 0 时带 fix 复选——板上加 --fix-edid-checksums 自动修复校验和。
 * 副作用: 写采集芯片 RAM（断电即丢）；源端重新协商输出；板端自动触发
 * 重对齐，画面中断几秒；成功内容存 /tmp/ccs_last_edid.txt 供切通道补写。 */
int ccs_edid_apply_hex(ccs_client_t *c, const char *hextext, int fix,
                       char *msg, size_t len);

/* POST /edid_preset preset=720p：恢复出厂 720p EDID（设了带不动的信号
 * 采不到帧时兜底恢复用）。副作用: 同 ccs_edid_apply_hex。 */
int ccs_edid_factory_reset(ccs_client_t *c, char *msg, size_t len);

/* ================= GPIO（gpio.py） ================= */

/* GPIO 全表条目（对应管理页 GPIO 表格一行）。val 是页面生成时的快照，
 * 非实时——要实时电平用 ccs_gpio_read_inputs()。 */
typedef struct {
    int num;                /* GPIO 编号 */
    char name[64];          /* 原理图网络名 */
    char dir[8];            /* "in"/"out"/"" */
    char val[8];            /* "hi"/"lo"/"" */
    char consumer[64];      /* 占用者（""或"sysfs"表示可翻转） */
} ccs_gpio_t;

/* GPIO 全表（只收有名脚），抓管理页 GPIO 表格。
 * *arr 为 malloc 数组（*count 个元素），调用者 free()。副作用: 无。 */
int ccs_gpio_list(ccs_client_t *c, ccs_gpio_t **arr, int *count);

/* POST /gpio_set 翻转白名单脚电平。**总是带 ajax=1**——不带时板上成功回
 * 303 整页跳转（网页表单用法），ajax 模式才回纯文本结果（同 Python 版）。
 * 白名单只有 WORKING_LEDEN_H 且无占用者；否则板上回 403。
 * 副作用: 改变板上实际引脚电平（sysfs 写 direction=out + value）。 */
int ccs_gpio_set(ccs_client_t *c, int num, int val, char *msg, size_t len);

/* 四路外部输入实时电平条目（GPIO2_B1=73 黄 / B2=74 绿 / C5=85 蓝 /
 * C6=86 红，外部 5V 经 TXS0104E 降到 1.8V 进 SoC，只读）。 */
typedef struct {
    int num;                /* GPIO 编号（板上某脚读失败会缺该项） */
    char name[32];          /* LED_YELLOW_STATE / LED_GREEN_STATE / ... */
    int level;              /* 1=高电平 0=低电平 */
} ccs_gpio_input_t;

/* GET /gpio_vals → 四路外部输入实时电平（JSON 解析，SDK 内置极简 JSON
 * 取值函数，不引第三方库）。
 * out 数组容量至少 4；*count 输出实际条数（板上某脚读失败会缺键）。
 * 副作用: 无（板上已常驻 export 为输入，读取不改方向）。 */
int ccs_gpio_read_inputs(ccs_client_t *c, ccs_gpio_input_t out[4], int *count);

/* ================= 串口（uart.py） ================= */

#define CCS_UART_RS232 "232"    /* /dev/ttyS3 (UART3, JP5B) */
#define CCS_UART_RS485 "485"    /* /dev/ttyS4 (UART4, JP5A)，Q10 硬件自动方向 */

/* POST /uart_send 发送文本数据。
 * data: 要发的字符串（按 UTF-8 发送）；
 * hex:  非 0 时 data 须为 hex 字符串（空格可选），板上 bytes.fromhex 解析；
 * crlf: 非 0 追加 \r\n（板上 nl=1，与网页控制台默认一致）。
 * msg 输出板上文本 "已发 NB"（N 为实际写入字节数，含 \r\n）。
 * 副作用: 往对应串口线上实际发出字节。 */
int ccs_uart_send(ccs_client_t *c, const char *port, const char *data,
                  int hex, int crlf, char *msg, size_t len);

/* POST /uart_send 发送原始字节（自动转 hex 字符串并强制 hex 模式——板上
 * 表单字段按 UTF-8 解码，任意二进制直接塞文本会被 'replace' 污染，
 * 与 Python 版 bytes 分支同理）。
 * 副作用: 同 ccs_uart_send。 */
int ccs_uart_send_bytes(ccs_client_t *c, const char *port,
                        const unsigned char *data, size_t data_len,
                        int crlf, char *msg, size_t len);

/* GET /uart_recv 增量拉取接收数据。
 * since: 上次返回的 *next 值；0 = 从最早可用缓冲开始。板端接收缓冲是
 *   环形 64KB，rx_total 是**逻辑偏移**（单调递增，不因缓冲丢弃回退）：
 *   since 落在窗口内→只回增量；落后于缓冲起点→给全量现存缓冲。
 * hex:  非 0 时 *data 为 "xx xx " 格式字符串（收二进制用）。
 * *data 为 malloc 字符串，调用者 free()；*next 输出新逻辑偏移，下次原样
 *   传回即可无缝续拉；baud 输出该路当前波特率（可为 NULL）。
 * 副作用: 无（纯读取，不消费板上数据——since 是客户端侧状态）。 */
int ccs_uart_recv(ccs_client_t *c, const char *port, long since, int hex,
                  char **data, long *next, long *baud);

/* POST /uart_set 设置波特率（8N1 固定，只改速率）。
 * baud 须在白名单内：9600/19200/38400/57600/115200/230400/460800/921600。
 * msg 输出 "baud=N"。
 * 副作用: 立即改板上该路串口速率（全局共享，影响所有会话）；改的瞬间
 * 线上正在传的字节可能出错。 */
int ccs_uart_set_baud(ccs_client_t *c, const char *port, int baud,
                      char *msg, size_t len);

/* ================= 网络（network.py） ================= */

/* 网卡信息（对应管理页"网络设置"表格一行）。 */
typedef struct {
    char iface[16];         /* eth0/eth1/...（只收 ethX/wlanX/canX） */
    char state[16];         /* "UP"/"DOWN" */
    char addr[160];         /* 地址串（多个空格分隔，无地址是 "-"） */
    char mode[16];          /* "静态" 或 "DHCP" */
} ccs_net_iface_t;

/* 网卡列表，抓管理页网络表格（板上解析 ip -brief addr）。
 * *arr 为 malloc 数组，调用者 free()。副作用: 无。 */
int ccs_net_ifaces(ccs_client_t *c, ccs_net_iface_t **arr, int *count);

/* POST /net_apply 下发网络配置，并启动板上 90 秒回滚定时器。
 * iface: "eth0"/"eth1"（板上硬白名单）；
 * mode:  "dhcp"（删除静态配置文件）或 "static"；
 * addr:  static 必填，形如 "192.168.1.50/24"（带前缀）；
 * gw/dns: 可空（dns 多个用空格或逗号）。
 * token 输出确认令牌（8 个 hex 字符），90 秒内传给 ccs_net_confirm()。
 * ⚠ 副作用最大: 写/删 /etc/systemd/network/10-ccs-<iface>.network 并
 * restart systemd-networkd——**旧地址的连接（包括本请求这条）立刻断开**；
 * 90 秒未确认自动回滚旧配置。务必先想好在新地址下的确认路径。 */
int ccs_net_apply(ccs_client_t *c, const char *iface, const char *mode,
                  const char *addr, const char *gw, const char *dns,
                  char *token, size_t token_len);

/* GET /net_confirm?token=... 确认保留新配置。
 * 必须在 90 秒窗口内、**新地址**下调用（用新地址 ccs_new 一个客户端再调）。
 * *kept 输出 1=已保留新配置（板上取消回滚定时器），0=无待确认配置或
 * token 不符（不算错误，板上两种结果都回 200 HTML）。 */
int ccs_net_confirm(ccs_client_t *c, const char *token, int *kept);

/* ================= 画质（image.py） ================= */

/* GET /brightness?val=N 设置亮度增益（VGA 通道模拟采样偏暗的补偿，只作用
 * Y 分量）。val: 20~200（100=原始；越界板上自动钳位）。
 * msg 输出板上文本 "亮度 N%"（N 是实际生效值）。
 * 副作用: 即时改画质 + 持久化 /etc/ccs_brightness（复位用 100）。 */
int ccs_image_set_brightness(ccs_client_t *c, int val, char *msg, size_t len);

/* GET /sharpen?val=N 设置锐化强度（unsharp mask 只作用 Y 分量）。
 * val: 0~100（0=关闭；越界自动钳位）。msg 输出 "锐化 N"。
 * 副作用: 即时改画质 + 持久化 /etc/ccs_sharp（复位用 0）。 */
int ccs_image_set_sharpen(ccs_client_t *c, int val, char *msg, size_t len);

/* ================= HID 键鼠/触摸注入（hid.py） ================= */

/* 修饰键位掩码（与网页 KVM_JS 的 MODS 一致） */
#define CCS_MOD_LCTRL   1
#define CCS_MOD_LSHIFT  2
#define CCS_MOD_LALT    4
#define CCS_MOD_LMETA   8
#define CCS_MOD_RCTRL   16
#define CCS_MOD_RSHIFT  32
#define CCS_MOD_RALT    64
#define CCS_MOD_RMETA   128

/* 发完整键盘报告 [mods, 0, k1..k6]（hidg0，boot 协议 6KRO）。
 * keys 是普通键 usage ID 数组（不足 6 个补 0）。
 * ⚠ 键盘是**全状态上报**——报告里没列的键视为松开；要全部松开必须再
 * 调 ccs_hid_release_all()，否则上位机认为键一直按着。
 * 副作用: 在被控上位机上产生真实键盘事件。 */
int ccs_hid_send_keyboard(ccs_client_t *c, unsigned mods,
                          const unsigned char *keys, int nkeys);

/* 发空键盘报告，全部键松开。 */
int ccs_hid_release_all(ccs_client_t *c);

/* 按一下指定键（按下 → hold_sec 秒 → 松开，完整击键）。
 * name: 键名，接受完整名（"KeyA"/"Enter"/"F5"/"ArrowUp"...）或单字符
 * 简写（"A"~"Z" 不分大小写、"0"~"9"）；mods 用 CCS_MOD_* 组合。
 * 副作用: 上位机收到一次完整按键。 */
int ccs_hid_key(ccs_client_t *c, const char *name, unsigned mods,
                double hold_sec);

/* 移动指针到绝对坐标 (x, y)（hidg3 笔 digitizer，与网页鼠标模式一致）。
 * x, y: 归一化坐标 0.0~1.0（自动钳位到边界）；down: 非 0=按住左键（拖拽）。
 * 副作用: 上位机指针移动/拖拽。 */
int ccs_hid_mouse_move(ccs_client_t *c, double x, double y, int down);

/* 按住/松开左键。x<0 表示不给坐标（只改按键位，坐标发 0，网页退出 KVM
 * 控制时同款写法）；否则先移动再按下/松开。 */
int ccs_hid_mouse_down(ccs_client_t *c, double x, double y);
int ccs_hid_mouse_up(ccs_client_t *c, double x, double y);

/* 在 (x, y) 左键点一下（按下 → hold_sec 秒 → 松开）。 */
int ccs_hid_click(ccs_client_t *c, double x, double y, double hold_sec);

/* 在 (x, y) 点右键（hidg2 触摸流式长按，与网页 rightClick() 一致：
 * 以 ~60Hz 连发 count 次按下报告——默认 interval=0.06, count=22 ≈ 1.3s——
 * 再发抬起。Windows 原生"按住=右键"手势；只发一次会被判联系点失效，
 * 长按不触发（实测），所以本函数**阻塞调用线程约 interval*count 秒**）。
 * interval/count 传 0 用默认值。 */
int ccs_hid_right_click(ccs_client_t *c, double x, double y,
                        double interval, int count);

/* 单点触摸按下/抬起（hidg2 digitizer）。down: 非 0=按下，0=抬起。 */
int ccs_hid_touch(ccs_client_t *c, double x, double y, int down);

/* 在 (x, y) 触屏点一下（按下 → hold_sec 秒 → 抬起）。 */
int ccs_hid_tap(ccs_client_t *c, double x, double y, double hold_sec);

/* hidg1 绝对坐标鼠标报告 [btn, xL, xH, yL, yH, wheel]（滚轮只能走这路）。
 * btn: 按键位掩码 bit0=左 bit1=右 bit2=中；wheel: 滚轮增量（-1 下/+1 上）。
 * ⚠ 注意: Windows 把绝对坐标鼠标整体锁死在主屏（够不到扩展副屏）——
 * 这是网页鼠标模式改用笔 digitizer 的原因。仅在确认上位机单屏/是
 * Linux，或要用滚轮时用。 */
int ccs_hid_mouse_abs(ccs_client_t *c, double x, double y,
                      unsigned btn, int wheel);

#ifdef __cplusplus
}
#endif

#endif /* CCS_SDK_H */
