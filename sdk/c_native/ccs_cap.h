/*
 * ccs_cap.h — CCS 板（RK3568）V4L2 视频采集：开设备 / 抓帧 / 关设备
 *
 * 对应主服务 ccs_webcam.py 的 Capture 类（ccs_dev/ccs_webcam.py:571 起）：
 *   - 设备节点 /dev/video0（rkcif），链路 TC358743(HDMI)/MS9288C(VGA)
 *     → MIPI-CSI2 → rkcif → V4L2。
 *   - buffer 类型为 multiplanar（V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE），
 *     内存方式 V4L2_MEMORY_MMAP，与 Python 侧 MPLANE=9 / MEM_MMAP=1 一致。
 *   - 像素格式 UYVY（V4L2_PIX_FMT_UYVY，YUV 4:2:2 打包，2 字节/像素，
 *     帧大小 = 宽×高×2）。不是 NV12/NV16——rkcif 在本项目链路里输出 UYVY。
 *
 * 注意：/dev/video0 同一时刻只能被一个使用者开流。ccs_webcam 主服务
 * 运行时本模块的 open/stream_on 会失败或抓不到帧，这是正常现象；
 * 使用本 SDK 的程序与主服务只能二选一运行。
 *
 * C99，仅依赖 libc + 内核 uapi 头（linux/videodev2.h）。
 */
#ifndef CCS_CAP_H
#define CCS_CAP_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 不透明句柄：一个已打开的采集设备 */
typedef struct ccs_cap ccs_cap_t;

/*
 * ccs_cap_open — 打开采集设备并按目标格式申请 mmap 帧缓冲
 *
 * 功能：open(dev) → VIDIOC_S_FMT 设置 UYVY/w×h（multiplanar）→
 *       VIDIOC_REQBUFS 申请 4 个 buffer → VIDIOC_QUERYBUF + mmap 映射。
 *       对应 ccs_webcam.py Capture._open/_alloc。本函数只准备缓冲，
 *       不开流，须再调 ccs_cap_stream_on()。
 * 参数：
 *   dev    设备节点路径，如 "/dev/video0"
 *   width  期望采集宽度（须与源实际输出一致，见 README"分辨率跟随"）
 *   height 期望采集高度
 * 返回：成功返回句柄；失败返回 NULL，errno 指示原因
 *       （EBUSY=设备被主服务占用；ENOMEM=无信号时节点格式 0x0 申请失败）。
 * 副作用：独占打开设备节点（内核 V4L2 单开流者语义）；申请并映射内核
 *         DMA buffer。出错时已自动清理全部资源。
 */
ccs_cap_t *ccs_cap_open(const char *dev, int width, int height);

/*
 * ccs_cap_stream_on — 全部 buffer 入队并开流
 * 对应 ccs_webcam.py Capture.stream_on：对每个 buffer VIDIOC_QBUF 后
 * VIDIOC_STREAMON。重复调用幂等性未定义，请勿重复开流。
 * 返回 0 成功 / -1 失败（errno）。
 */
int ccs_cap_stream_on(ccs_cap_t *cap);

/*
 * ccs_cap_stream_off — 停流（VIDIOC_STREAMOFF）
 * 停流后队列清空，再次 ccs_cap_read_frame 前必须重新 ccs_cap_stream_on。
 * 对已停流的句柄调用是安全的（返回 0）。
 */
int ccs_cap_stream_off(ccs_cap_t *cap);

/*
 * ccs_cap_read_frame — 抓最新一帧（排干队列只取最新，低延迟策略）
 *
 * 功能：对应 ccs_webcam.py Capture.grab_latest。先在 timeout_ms 内等
 *       第一帧，随后以 0 超时继续 DQBUF 排干队列，旧帧直接重新入队，
 *       只把最新一帧拷贝到调用方缓冲。这样调用方拿到的永远是"当前画面"，
 *       不会在处理慢时越积越旧。
 * 参数：
 *   cap        ccs_cap_open 返回且已 stream_on 的句柄
 *   out        调用方输出缓冲，至少 ccs_cap_frame_size() 字节
 *   out_size   out 缓冲大小（小于帧大小时本函数拒绝拷贝返回 -1/EINVAL）
 *   timeout_ms 等帧超时（毫秒）；0 = 不等待立即返回
 * 返回：
 *   >0  拷贝的帧字节数（= 宽×高×2，UYVY）
 *    0  超时无帧，或 mmap 缓冲比预期帧小（驱动格式掉了的短帧，
 *       对应 Python 返回 None 的场景，建议上层按"零帧"处理）
 *   -1  参数/系统错误（errno）
 * 副作用：拷贝期间该帧 buffer 已重新入队，设备持续采集不受影响。
 */
int ccs_cap_read_frame(ccs_cap_t *cap, void *out, size_t out_size,
                       int timeout_ms);

/* ccs_cap_frame_size — 单帧字节数（宽×高×2，UYVY 2 字节/像素） */
size_t ccs_cap_frame_size(const ccs_cap_t *cap);

/* ccs_cap_width / ccs_cap_height — 实际生效的帧几何（S_FMT 回读值） */
int ccs_cap_width(const ccs_cap_t *cap);
int ccs_cap_height(const ccs_cap_t *cap);

/*
 * ccs_cap_close — 停流、解除 mmap、释放 buffer（REQBUFS count=0）、关 fd
 * 对应 ccs_webcam.py Capture._free + stream_off + close。传 NULL 安全。
 */
void ccs_cap_close(ccs_cap_t *cap);

#ifdef __cplusplus
}
#endif
#endif /* CCS_CAP_H */
