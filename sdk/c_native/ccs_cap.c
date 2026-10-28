/*
 * ccs_cap.c — V4L2 采集实现（multiplanar + mmap）
 *
 * ioctl 路径与 ccs_webcam.py Capture 类逐条对应：
 *   Python 里裸结构体常量 VIDIOC_REQBUFS=0xC0145608 等即内核 uapi 宏；
 *   这里直接用 <linux/videodev2.h> 的结构体，语义完全一致。
 *
 * 关键结构体字段说明（multiplanar 特有的易错点）：
 *   struct v4l2_buffer.m.planes 指向调用方提供的 struct v4l2_plane 数组，
 *   v4l2_buffer.length 是"plane 数量"（UYVY 单 plane = 1），不是字节数！
 *   QUERYBUF 返回 plane.length=该 plane 字节数、plane.m.mem_offset=mmap 偏移。
 */
#include "ccs_cap.h"

#include <errno.h>
#include <fcntl.h>
#include <sys/time.h>           /* struct timeval：部分内核头里 v4l2_buffer 要用 */
#include <linux/videodev2.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/select.h>
#include <unistd.h>

#define CCS_REQ_BUFS 4          /* 申请的 buffer 数，与主服务一致 */

struct ccs_cap {
    int      fd;                /* /dev/video0 文件描述符 */
    int      width, height;     /* S_FMT 回读的实际几何 */
    size_t   frame_size;        /* width*height*2（UYVY） */
    unsigned nbuf;              /* REQBUFS 实际批下的 buffer 数 */
    void   **map;               /* 每个 buffer 的 mmap 地址 */
    size_t  *map_len;           /* 每个 buffer 的映射长度 */
    int      streaming;         /* 是否已开流 */
};

/* 组装一个 multiplanar 的 v4l2_buffer（QBUF/DQBUF/QUERYBUF 共用形态）。
 * planes 由调用方持有，调用前已清零。 */
static void ccs_buf_init(struct v4l2_buffer *b, struct v4l2_plane *pl,
                         unsigned index)
{
    memset(b, 0, sizeof(*b));
    memset(pl, 0, sizeof(*pl));
    b->index    = index;
    b->type     = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    b->memory   = V4L2_MEMORY_MMAP;
    b->m.planes = pl;
    b->length   = 1;            /* plane 数量：UYVY 单 plane */
}

/* 重试版 ioctl：被信号打断（EINTR）时重试 */
static int xioctl(int fd, unsigned long req, void *arg)
{
    int r;
    do {
        r = ioctl(fd, req, arg);
    } while (r < 0 && errno == EINTR);
    return r;
}

ccs_cap_t *ccs_cap_open(const char *dev, int width, int height)
{
    ccs_cap_t *cap = NULL;
    struct v4l2_format fmt;
    struct v4l2_requestbuffers req;
    unsigned i;

    cap = calloc(1, sizeof(*cap));
    if (!cap)
        return NULL;
    cap->fd = -1;

    cap->fd = open(dev, O_RDWR);
    if (cap->fd < 0)
        goto fail;

    /* 设格式：multiplanar + UYVY。对应主服务
     * v4l2-ctl -d /dev/video0 --set-fmt-video=width=W,height=H,pixelformat=UYVY
     * 注意：media 链路各实体（tc35874x/dphy/mipi-csi2）的格式由
     * ccs_init.sh / 主服务用 media-ctl 配好，本 SDK 不重复配置；
     * 若节点格式异常导致下面 REQBUFS 失败，说明链路未初始化，
     * 应先运行主服务的初始化流程（见 README）。 */
    memset(&fmt, 0, sizeof(fmt));
    fmt.type                 = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    fmt.fmt.pix_mp.width     = (uint32_t)width;
    fmt.fmt.pix_mp.height    = (uint32_t)height;
    fmt.fmt.pix_mp.pixelformat = V4L2_PIX_FMT_UYVY;
    /* S_FMT 可能静默改成别的值：必须以驱动回读的 width/height 为准 */
    if (xioctl(cap->fd, VIDIOC_S_FMT, &fmt) < 0)
        goto fail;
    cap->width  = (int)fmt.fmt.pix_mp.width;
    cap->height = (int)fmt.fmt.pix_mp.height;
    cap->frame_size = (size_t)cap->width * (size_t)cap->height * 2u;

    /* 申请 buffer：对应 Capture._alloc 的 REQBUFS(4, MPLANE, MMAP) */
    memset(&req, 0, sizeof(req));
    req.count  = CCS_REQ_BUFS;
    req.type   = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    req.memory = V4L2_MEMORY_MMAP;
    if (xioctl(cap->fd, VIDIOC_REQBUFS, &req) < 0)
        goto fail;              /* 无信号时节点格式 0x0 → ENOMEM，属预期 */
    if (req.count < 2) {
        errno = ENOMEM;
        goto fail;
    }
    cap->nbuf = req.count;

    cap->map     = calloc(cap->nbuf, sizeof(*cap->map));
    cap->map_len = calloc(cap->nbuf, sizeof(*cap->map_len));
    if (!cap->map || !cap->map_len) {
        errno = ENOMEM;
        goto fail;
    }

    /* QUERYBUF 拿每个 buffer 的 plane 长度/偏移，然后 mmap。
     * mmap 偏移必须取 plane.m.mem_offset（多平面下不是 buffer 级字段）。 */
    for (i = 0; i < cap->nbuf; i++) {
        struct v4l2_buffer b;
        struct v4l2_plane pl;
        ccs_buf_init(&b, &pl, i);
        if (xioctl(cap->fd, VIDIOC_QUERYBUF, &b) < 0)
            goto fail;
        cap->map_len[i] = pl.length;
        cap->map[i] = mmap(NULL, pl.length, PROT_READ | PROT_WRITE,
                           MAP_SHARED, cap->fd, (off_t)pl.m.mem_offset);
        if (cap->map[i] == MAP_FAILED) {
            cap->map[i] = NULL;
            goto fail;
        }
    }
    return cap;

fail:
    {
        int saved = errno;
        ccs_cap_close(cap);
        errno = saved;
    }
    return NULL;
}

int ccs_cap_stream_on(ccs_cap_t *cap)
{
    unsigned i;
    enum v4l2_buf_type type;

    if (!cap) {
        errno = EINVAL;
        return -1;
    }
    for (i = 0; i < cap->nbuf; i++) {
        struct v4l2_buffer b;
        struct v4l2_plane pl;
        ccs_buf_init(&b, &pl, i);
        if (xioctl(cap->fd, VIDIOC_QBUF, &b) < 0)
            return -1;
    }
    type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    if (xioctl(cap->fd, VIDIOC_STREAMON, &type) < 0)
        return -1;
    cap->streaming = 1;
    return 0;
}

int ccs_cap_stream_off(ccs_cap_t *cap)
{
    enum v4l2_buf_type type;

    if (!cap) {
        errno = EINVAL;
        return -1;
    }
    if (!cap->streaming)
        return 0;
    type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    if (xioctl(cap->fd, VIDIOC_STREAMOFF, &type) < 0)
        return -1;
    cap->streaming = 0;
    return 0;
}

int ccs_cap_read_frame(ccs_cap_t *cap, void *out, size_t out_size,
                       int timeout_ms)
{
    unsigned idxs[32];          /* 排干队列收到的 buffer 下标，nbuf<=32 */
    unsigned n = 0, i, last;

    if (!cap || !out || out_size < cap->frame_size || cap->nbuf > 32) {
        errno = EINVAL;
        return -1;
    }

    /* 排干队列：第一帧等 timeout_ms，之后 0 超时继续收，直到队列空。
     * 旧帧全部重新入队，只保留最新一帧 —— Capture.grab_latest 同款策略。 */
    for (;;) {
        fd_set rfds;
        struct timeval tv;
        struct v4l2_buffer b;
        struct v4l2_plane pl;
        int r;

        FD_ZERO(&rfds);
        FD_SET(cap->fd, &rfds);
        tv.tv_sec  = (n == 0) ? timeout_ms / 1000 : 0;
        tv.tv_usec = (n == 0) ? (long)(timeout_ms % 1000) * 1000L : 0;
        r = select(cap->fd + 1, &rfds, NULL, NULL, &tv);
        if (r < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        if (r == 0)
            break;              /* 队列已空（或首帧超时） */
        ccs_buf_init(&b, &pl, 0);
        if (xioctl(cap->fd, VIDIOC_DQBUF, &b) < 0)
            break;              /* 竞态停流等：按队列空处理 */
        if (n < cap->nbuf)
            idxs[n++] = b.index;
        else {                  /* 理论上不会发生，防御性归还 */
            ccs_buf_init(&b, &pl, b.index);
            xioctl(cap->fd, VIDIOC_QBUF, &b);
        }
    }
    if (n == 0)
        return 0;               /* 超时无帧 */

    for (i = 0; i + 1 < n; i++) {   /* 旧帧重新入队 */
        struct v4l2_buffer b;
        struct v4l2_plane pl;
        ccs_buf_init(&b, &pl, idxs[i]);
        xioctl(cap->fd, VIDIOC_QBUF, &b);
    }

    last = idxs[n - 1];
    /* mmap 比帧小 = 驱动格式掉了、buffer 是旧尺寸：短帧交出去上层会
     * 编出绿屏，按"零帧"返回 0（主服务同样返回 None 走重对齐）。 */
    if (cap->map_len[last] >= cap->frame_size)
        memcpy(out, cap->map[last], cap->frame_size);
    {
        struct v4l2_buffer b;
        struct v4l2_plane pl;
        ccs_buf_init(&b, &pl, last);
        xioctl(cap->fd, VIDIOC_QBUF, &b);
    }
    return (cap->map_len[last] >= cap->frame_size)
           ? (int)cap->frame_size : 0;
}

size_t ccs_cap_frame_size(const ccs_cap_t *cap)
{
    return cap ? cap->frame_size : 0;
}

int ccs_cap_width(const ccs_cap_t *cap)
{
    return cap ? cap->width : 0;
}

int ccs_cap_height(const ccs_cap_t *cap)
{
    return cap ? cap->height : 0;
}

void ccs_cap_close(ccs_cap_t *cap)
{
    unsigned i;

    if (!cap)
        return;
    if (cap->fd >= 0) {
        struct v4l2_requestbuffers req;

        ccs_cap_stream_off(cap);
        if (cap->map) {
            for (i = 0; i < cap->nbuf; i++)
                if (cap->map[i])
                    munmap(cap->map[i], cap->map_len[i]);
        }
        /* REQBUFS count=0 释放内核 buffer，对应 Capture._free */
        memset(&req, 0, sizeof(req));
        req.type   = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
        req.memory = V4L2_MEMORY_MMAP;
        xioctl(cap->fd, VIDIOC_REQBUFS, &req);
        close(cap->fd);
    }
    free(cap->map);
    free(cap->map_len);
    free(cap);
}
