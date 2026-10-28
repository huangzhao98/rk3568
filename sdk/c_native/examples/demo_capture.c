/*
 * demo_capture.c — 抓 10 帧存 raw + 打印首帧信息
 *
 * 用法：./demo_capture [设备节点，默认 /dev/video0] [宽 默认1280] [高 默认720]
 * 输出：当前目录 frame_00.raw .. frame_09.raw（UYVY，宽×高×2 字节）
 *
 * 注意：/dev/video0 被 ccs_webcam 主服务占用时本 demo 打不开设备
 * （open 失败 EBUSY 或抓不到帧），这是正常的——先停主服务再用：
 *     systemctl stop ccs_webcam        # 用完后记得 systemctl start
 */
#include "../ccs_cap.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv)
{
    const char *dev = (argc > 1) ? argv[1] : "/dev/video0";
    int w = (argc > 2) ? atoi(argv[2]) : 1280;
    int h = (argc > 3) ? atoi(argv[3]) : 720;
    ccs_cap_t *cap;
    uint8_t *buf;
    size_t size;
    int i, got = 0;

    cap = ccs_cap_open(dev, w, h);
    if (!cap) {
        fprintf(stderr, "打开 %s 失败: %s\n", dev, strerror(errno));
        fprintf(stderr, "提示：/dev/video0 被 ccs_webcam 主服务占用时属正常，"
                        "需先停主服务（systemctl stop ccs_webcam）\n");
        return 1;
    }
    printf("已打开 %s，实际格式 %dx%d UYVY，帧 %zu 字节\n",
           dev, ccs_cap_width(cap), ccs_cap_height(cap),
           ccs_cap_frame_size(cap));

    if (ccs_cap_stream_on(cap) < 0) {
        fprintf(stderr, "开流失败: %s\n", strerror(errno));
        ccs_cap_close(cap);
        return 1;
    }

    size = ccs_cap_frame_size(cap);
    buf = malloc(size);
    if (!buf) {
        fprintf(stderr, "内存不足\n");
        ccs_cap_close(cap);
        return 1;
    }

    for (i = 0; i < 10; i++) {
        int n = ccs_cap_read_frame(cap, buf, size, 2000);
        if (n <= 0) {
            fprintf(stderr, "第 %d 帧：%s\n", i,
                    n == 0 ? "超时/短帧（无信号？）" : strerror(errno));
            continue;
        }
        if (got == 0) {                     /* 打印首帧信息 */
            unsigned long sum = 0;
            size_t px;
            int j;
            printf("首帧 %d 字节，前 16 字节:", n);
            for (j = 0; j < 16 && j < n; j++)
                printf(" %02x", buf[j]);
            printf("\n");
            for (px = 0; px < size; px += 2)    /* 亮度均值（奇数字节） */
                sum += buf[px + 1];
            printf("首帧亮度均值 %.1f（全黑≈16，正常画面 50~150）\n",
                   (double)sum / (double)(size / 2));
        }
        {
            char name[64];
            FILE *f;
            snprintf(name, sizeof(name), "frame_%02d.raw", got);
            f = fopen(name, "wb");
            if (!f) {
                fprintf(stderr, "写 %s 失败: %s\n", name, strerror(errno));
                continue;
            }
            fwrite(buf, 1, (size_t)n, f);
            fclose(f);
            printf("已保存 %s（%d 字节）\n", name, n);
        }
        got++;
    }

    free(buf);
    ccs_cap_close(cap);
    printf("完成：抓到 %d/10 帧\n", got);
    return got > 0 ? 0 : 1;
}
