/*
 * demo_align.c — 抓连续帧，估计垂直卷动相位
 *
 * 用法：./demo_align [设备节点，默认 /dev/video0] [宽] [高]
 *
 * 流程：连抓 5 帧，依次用三种方法估计相位 k（buffer 第 b 行 = 源画面
 * 第 (b+k)%h 行）：
 *   1. 分截签名 ccs_align_split_fix（绝对锚点，Windows 桌面+任务栏场景）
 *   2. 接缝检测 ccs_align_seam（最近 3 帧，不依赖具体内容）
 *   3. 参考帧相关 ccs_align_measure（第 0 帧作 ref 测第 4 帧——相位稳定
 *      时结果应为 0；非 0 说明采集中相位发生了漂移）
 * 命中任一方法时给出建议：裁切/显示起点 = 第 k 行（即先 ccs_align_deroll
 * 上移 k 行再使用），相对当前相位的 nudge 量 = (k - 当前roll) mod h。
 *
 * 同样需要 /dev/video0 空闲（主服务已停）。
 */
#include "../ccs_align.h"
#include "../ccs_cap.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define NFRAMES 5

int main(int argc, char **argv)
{
    const char *dev = (argc > 1) ? argv[1] : "/dev/video0";
    int w = (argc > 2) ? atoi(argv[2]) : 1280;
    int h = (argc > 3) ? atoi(argv[3]) : 720;
    ccs_cap_t *cap;
    uint8_t *frames[NFRAMES];
    size_t size;
    int i, got = 0, k, conf;

    cap = ccs_cap_open(dev, w, h);
    if (!cap) {
        fprintf(stderr, "打开 %s 失败: %s（主服务占用？）\n",
                dev, strerror(errno));
        return 1;
    }
    if (ccs_cap_stream_on(cap) < 0) {
        fprintf(stderr, "开流失败: %s\n", strerror(errno));
        ccs_cap_close(cap);
        return 1;
    }
    w = ccs_cap_width(cap);                 /* 以实际生效几何为准 */
    h = ccs_cap_height(cap);
    size = ccs_cap_frame_size(cap);

    for (i = 0; i < NFRAMES; i++) {
        frames[i] = malloc(size);
        if (!frames[i]) {
            fprintf(stderr, "内存不足\n");
            while (--i >= 0)
                free(frames[i]);
            ccs_cap_close(cap);
            return 1;
        }
    }

    for (i = 0; i < NFRAMES; i++) {         /* 连抓 5 帧，跳过超时帧 */
        int try_;
        for (try_ = 0; try_ < 3; try_++) {
            if (ccs_cap_read_frame(cap, frames[i], size, 2000) > 0)
                break;
        }
        if (try_ == 3) {
            fprintf(stderr, "抓不到帧（无信号？），已抓 %d 帧\n", got);
            goto out;
        }
        got++;
    }

    /* 方法 1：分截签名（绝对锚点） */
    k = ccs_align_split_fix(frames[got - 1], w, h);
    printf("分截签名 split_fix : %s\n",
           k >= 0 ? "" : "未命中（非任务栏分截画面）");
    if (k >= 0)
        printf("  -> 相位 k=%d，建议消卷：deroll 上移 %d 行\n", k, k);

    /* 方法 2：接缝检测（最近 3 帧） */
    if (got >= 3) {
        const uint8_t *last3[3] = { frames[got - 3], frames[got - 2],
                                    frames[got - 1] };
        k = ccs_align_seam(last3, w, h);
        printf("接缝检测   seam      : %s\n",
               k >= 0 ? "" : "未检出（无显著接缝或已对齐）");
        if (k >= 0)
            printf("  -> 相位 k=%d，建议消卷：deroll 上移 %d 行\n", k, k);
    }

    /* 方法 3：参考帧相关（第 0 帧 vs 最后一帧，相位稳定时应为 0） */
    conf = ccs_align_measure(frames[got - 1], frames[0], w, h, &k);
    if (conf < 0)
        printf("参考帧相关 measure    : 计算失败\n");
    else if (!conf)
        printf("参考帧相关 measure    : 不置信（画面太平坦/两帧内容变化大）\n");
    else {
        printf("参考帧相关 measure    : 帧间漂移 k=%d（0=相位稳定）\n", k);
        if (k != 0) {
            /* 相对"以第 0 帧为基准"的 nudge 量：roll_new = (0 + k) % h */
            int new_roll = ccs_align_nudge(0, k, h);
            printf("  -> 建议 nudge 量 %+d 行（新相位 roll=%d）\n", k, new_roll);
        }
    }

out:
    for (i = 0; i < NFRAMES; i++)
        free(frames[i]);
    ccs_cap_close(cap);
    return got >= 3 ? 0 : 1;
}
