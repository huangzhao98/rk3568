/*
 * ccs_edid.c — EDID 读写 ioctl 实现
 *
 * 本文件自带 struct v4l2_edid 布局与 ioctl 号，不依赖系统 videodev2.h
 * 的版本：交叉工具链（Linaro 6.3.1）自带的 4.4 内核头里只有
 * VIDIOC_G_EDID 宏而没有 struct v4l2_edid 定义。布局与内核 uapi
 * 完全一致（4.19/5.10/6.x 均未变过）：
 *
 *   struct v4l2_edid {
 *       __u32 pad;         // subdev pad 号，TC358743 必须为 0
 *       __u32 start_block; // 起始块号（128 字节/块），驱动要求从 0 写
 *       __u32 blocks;      // 块数；G_EDID 传 0 = 只查询当前块数
 *       __u32 reserved[5];
 *       __u8 *edid;        // 用户态数据指针
 *   };
 *
 * 板端 4.19 内核 drivers/media/v4l2-core/v4l2-subdev.c 对 subdev 节点
 * 直接处理 VIDIOC_G_EDID / VIDIOC_S_EDID（请求号 40/41），转发到
 * tc358743 驱动的 get_edid/set_edid 回调（tc358743.c:1738/1769）：
 *   - g_edid：start_block==0 && blocks==0 → 只回填 blocks（当前块数）；
 *     芯片从未写过 EDID 返回 -ENODATA。
 *   - s_edid：pad 必须为 0、start_block 必须为 0、块数超上限返回
 *     -E2BIG；写入前会解析/校验 EDID 里的 CEC 物理地址。
 */
#include "ccs_edid.h"

#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

/* 与内核 uapi struct v4l2_edid 逐字段等价的私有定义（见文件头注释） */
struct ccs_v4l2_edid {
    uint32_t pad;
    uint32_t start_block;
    uint32_t blocks;
    uint32_t reserved[5];
    uint8_t *edid;
};

/* 请求号与内核 VIDIOC_G_EDID/VIDIOC_S_EDID（= VIDIOC_SUBDEV_*_EDID）相同 */
#define CCS_VIDIOC_G_EDID _IOWR('V', 40, struct ccs_v4l2_edid)
#define CCS_VIDIOC_S_EDID _IOWR('V', 41, struct ccs_v4l2_edid)

static int xioctl(int fd, unsigned long req, void *arg)
{
    int r;
    do {
        r = ioctl(fd, req, arg);
    } while (r < 0 && errno == EINTR);
    return r;
}

/* 在已打开的 fd 上读 EDID，返回字节数 / 0（ENODATA）/ -1 */
static int edid_read_fd(int fd, uint8_t *buf, int max_blocks)
{
    struct ccs_v4l2_edid ed;

    memset(&ed, 0, sizeof(ed));
    /* 查询：blocks=0 → 驱动回填当前已写入的块数 */
    if (xioctl(fd, CCS_VIDIOC_G_EDID, &ed) < 0)
        return (errno == ENODATA) ? 0 : -1;
    if (ed.blocks == 0)
        return 0;
    if ((int)ed.blocks > max_blocks)
        ed.blocks = (uint32_t)max_blocks;   /* 缓冲装不下就只读前 N 块 */

    ed.edid = buf;                          /* start_block 保持 0 */
    if (xioctl(fd, CCS_VIDIOC_G_EDID, &ed) < 0)
        return (errno == ENODATA) ? 0 : -1;
    return (int)ed.blocks * CCS_EDID_BLOCK;
}

int ccs_edid_read(const char *subdev, uint8_t *buf, int max_blocks)
{
    int fd, ret, saved;

    if (!subdev || !buf || max_blocks < 1) {
        errno = EINVAL;
        return -1;
    }
    fd = open(subdev, O_RDWR);              /* 与 v4l2-ctl 相同的打开方式 */
    if (fd < 0)
        return -1;
    ret = edid_read_fd(fd, buf, max_blocks);
    saved = errno;
    close(fd);
    errno = saved;
    return ret;
}

int ccs_edid_write(const char *subdev, const uint8_t *buf, int len, int fix)
{
    uint8_t *tmp = NULL, *data;
    uint8_t back[2 * CCS_EDID_BLOCK];
    struct ccs_v4l2_edid ed;
    int fd, blocks, n, i, ok = 0, saved = 0;

    if (!subdev || !buf || (len != CCS_EDID_BLOCK && len != 2 * CCS_EDID_BLOCK)) {
        errno = EINVAL;
        return -1;
    }
    blocks = len / CCS_EDID_BLOCK;
    data = (uint8_t *)buf;
    if (fix) {                              /* 修校验和：在副本上做 */
        tmp = malloc((size_t)len);
        if (!tmp)
            return -1;
        memcpy(tmp, buf, (size_t)len);
        ccs_edid_fix_checksum(tmp, len);
        data = tmp;
    }

    fd = open(subdev, O_RDWR);
    if (fd < 0) {
        saved = errno;
        goto out;
    }
    memset(&ed, 0, sizeof(ed));
    ed.blocks = (uint32_t)blocks;           /* pad=0, start_block=0 */
    ed.edid   = data;
    if (xioctl(fd, CCS_VIDIOC_S_EDID, &ed) < 0) {
        saved = errno;
        close(fd);
        goto out;
    }

    /* 读回验证（ccs_webcam.py set_edid_hex 同款流程）：
     * fix=1 时跳过每块最后一字节（那是我们修的校验和，调用方原文
     * 可能故意是错的）；fix=0 时逐字节全比。 */
    n = edid_read_fd(fd, back, blocks);
    saved = errno;
    close(fd);
    if (n != len)
        goto out;                           /* 读回失败/长度不符 → 0 */
    ok = 1;
    for (i = 0; i < len; i++) {
        if (fix && (i % CCS_EDID_BLOCK) == CCS_EDID_BLOCK - 1)
            continue;
        if (data[i] != back[i]) {
            ok = 0;
            break;
        }
    }

out:
    free(tmp);
    if (saved)
        errno = saved;
    return saved ? -1 : ok;
}

void ccs_edid_fix_checksum(uint8_t *buf, int len)
{
    int off, i;

    if (!buf || len <= 0)
        return;
    for (off = 0; off + CCS_EDID_BLOCK <= len; off += CCS_EDID_BLOCK) {
        unsigned sum = 0;
        for (i = 0; i < CCS_EDID_BLOCK - 1; i++)
            sum += buf[off + i];
        buf[off + CCS_EDID_BLOCK - 1] = (uint8_t)(0u - (sum & 0xffu));
    }
}

int ccs_edid_valid(const uint8_t *buf, int len)
{
    unsigned sum = 0;
    int i, nonzero = 0;

    if (!buf || len < CCS_EDID_BLOCK)
        return 0;
    /* 负载非全零：跳过 8 字节文件头（00 ff ff ff ff ff ff 00），
     * 全零负载 = 主服务对齐时写的垃圾 EDID，不算有效 EDID */
    for (i = 8; i < CCS_EDID_BLOCK - 1; i++)
        if (buf[i]) {
            nonzero = 1;
            break;
        }
    if (!nonzero)
        return 0;
    for (i = 0; i < CCS_EDID_BLOCK; i++)
        sum += buf[i];
    return (sum % 256) == 0;
}
