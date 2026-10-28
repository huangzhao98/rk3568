/*
 * ccs_align.c — 相位估计算法实现
 *
 * 与 ccs_webcam.py 的对应关系逐函数注明。所有像素常量阈值
 * （30/15/20/6/0.5 等）原样保留主服务的实测值。
 *
 * UYVY 内存布局：每行 w*2 字节，字节序 [U Y V Y]×w/2，
 * 像素 x 的亮度 Y 在字节偏移 y*w*2 + 2x + 1。
 * 隔 step 列采样即取字节偏移 1 + 2*step*i。
 */
#include "ccs_align.h"

#include <stdlib.h>
#include <string.h>

/* 行亮度轮廓：p[y] = 第 y 行抽样像素的亮度均值。
 * step=列抽样步长（主服务固定 8）。返回 0 成功 / -1 内存不足。 */
static int row_profile(const uint8_t *raw, int w, int h, int step, double *p)
{
    const int stride = w * 2;
    const int ncol = w / step;              /* 每行抽样点数 */
    int x, y;

    if (ncol < 1)
        return -1;
    for (y = 0; y < h; y++) {
        const uint8_t *row = raw + (size_t)y * (size_t)stride;
        unsigned long sum = 0;
        for (x = 0; x < ncol; x++)
            sum += row[1 + 2 * step * x];
        p[y] = (double)sum / (double)ncol;
    }
    return 0;
}

/* 行亮度轮廓 + 行内方差（split_fix 需要纹理强度判定）。
 * var[y] = E[Y^2] - E[Y]^2（用方差代替标准差，免去开方：
 * 阈值 std>30 等价 var>900，std>20 等价 var>400）。 */
static void row_profile_var(const uint8_t *raw, int w, int h, int step,
                            double *p, double *var)
{
    const int stride = w * 2;
    const int ncol = w / step;
    int x, y;

    for (y = 0; y < h; y++) {
        const uint8_t *row = raw + (size_t)y * (size_t)stride;
        unsigned long sum = 0, sum2 = 0;
        double m;
        for (x = 0; x < ncol; x++) {
            unsigned v = row[1 + 2 * step * x];
            sum += v;
            sum2 += v * v;
        }
        m = (double)sum / (double)ncol;
        p[y] = m;
        var[y] = (double)sum2 / (double)ncol - m * m;
    }
}

static double dabs(double x)
{
    return x < 0.0 ? -x : x;
}

int ccs_align_measure(const uint8_t *cur, const uint8_t *ref,
                      int w, int h, int *k_out)
{
    double *pc = NULL, *pr = NULL;
    double best_d = 1e9, sum_d = 0.0, avg_d;
    int best_k = 0, cnt = 0, conf = 0;
    int k, y;

    if (!cur || !ref || !k_out || w < 16 || h < 16)
        return -1;
    pc = malloc((size_t)h * sizeof(double));
    pr = malloc((size_t)h * sizeof(double));
    if (!pc || !pr || row_profile(cur, w, h, 8, pc) < 0
        || row_profile(ref, w, h, 8, pr) < 0) {
        free(pc);
        free(pr);
        return -1;
    }

    /* 粗搜：k 步长 2。d(k) = mean_y |pr[(y+k)%h] - pc[y]|
     * （Python np.roll(pr,-k) 即 pr 左移 k 位） */
    for (k = 0; k < h; k += 2) {
        double d = 0.0;
        for (y = 0; y < h; y++)
            d += dabs(pr[(y + k) % h] - pc[y]);
        d /= (double)h;
        sum_d += d;
        cnt++;
        if (d < best_d) {
            best_d = d;
            best_k = k;
        }
    }
    /* ±2 细化（含回绕） */
    for (k = best_k - 2; k <= best_k + 2; k++) {
        int kk = ((k % h) + h) % h;
        double d = 0.0;
        for (y = 0; y < h; y++)
            d += dabs(pr[(y + kk) % h] - pc[y]);
        d /= (double)h;
        if (d < best_d) {
            best_d = d;
            best_k = kk;
        }
    }

    avg_d = sum_d / (double)cnt;
    /* 置信闸门：最佳匹配既要绝对够好（<6），又要显著优于平均水平
     * （<0.5×均值）——平坦画面错位匹配误差也小，单阈值会误判 */
    conf = (best_d < 6.0 && best_d < 0.5 * avg_d);
    *k_out = best_k;
    free(pc);
    free(pr);
    return conf;
}

int ccs_align_split_fix(const uint8_t *cur, int w, int h)
{
    double *p, *var, desk;
    int top, bot, y, ok;

    if (!cur || w < 16 || h < 64)
        return -1;
    p = malloc((size_t)h * sizeof(double));
    var = malloc((size_t)h * sizeof(double));
    if (!p || !var) {
        free(p);
        free(var);
        return -1;
    }
    row_profile_var(cur, w, h, 8, p, var);

    desk = 0.0;                     /* 桌面基准亮度：帧中段 100 行均值 */
    for (y = h / 2; y < h / 2 + 100; y++)
        desk += p[y];
    desk /= 100.0;

    /* 任务栏行签名：行内方差高（有图标/托盘纹理）且明显亮过桌面 */
#define CCS_TB(yy) (var[yy] > 900.0 && p[yy] > desk + 15.0)

    top = 0;                        /* 顶截行数：小至 2 行也要修 */
    while (top < h / 8 && CCS_TB(top))
        top++;
    ok = (top >= 2);
    bot = 0;                        /* 底截行数：应含大半个任务栏 */
    if (ok) {
        while (bot < h / 6 && CCS_TB(h - 1 - bot))
            bot++;
        ok = (bot >= 20);
    }
    if (ok)                         /* 两截之间必须是桌面 */
        ok = !CCS_TB(top + 5) && !CCS_TB(h - bot - 5);

    free(p);
    free(var);
    return ok ? (h - top) % h : -1;
#undef CCS_TB
}

/* 取 double 数组中位数（复制后插入排序，h<=2160 足够快） */
static double median(const double *d, int n, double *scratch)
{
    int i, j;

    memcpy(scratch, d, (size_t)n * sizeof(double));
    for (i = 1; i < n; i++) {
        double v = scratch[i];
        for (j = i - 1; j >= 0 && scratch[j] > v; j--)
            scratch[j + 1] = scratch[j];
        scratch[j + 1] = v;
    }
    return (n & 1) ? scratch[n / 2]
                   : (scratch[n / 2 - 1] + scratch[n / 2]) * 0.5;
}

int ccs_align_seam(const uint8_t *const frames[3], int w, int h)
{
    double *p = NULL, *d = NULL, *scratch = NULL;
    int pos[3];
    int f, y, ret = -1;

    if (!frames || !frames[0] || !frames[1] || !frames[2] || w < 16 || h < 16)
        return -1;
    p = malloc((size_t)h * sizeof(double));
    d = malloc((size_t)h * sizeof(double));
    scratch = malloc((size_t)h * sizeof(double));
    if (!p || !d || !scratch)
        goto out;

    for (f = 0; f < 3; f++) {
        double med, strongest, second, gate;
        int ys;

        if (row_profile(frames[f], w, h, 8, p) < 0)
            goto out;
        /* d[y] = |p[y] - p[y-1]|，d[0] = 回绕边 |p[0]-p[h-1]| */
        for (y = 0; y < h; y++)
            d[y] = dabs(p[y] - p[(y + h - 1) % h]);
        ys = 0;
        for (y = 1; y < h; y++)
            if (d[y] > d[ys])
                ys = y;
        strongest = d[ys];
        med = median(d, h, scratch);

        /* 闸门 1：强度 >= 15 且 >= 4×中位数（没有显著接缝 = 可能
         * 已对齐或画面太平坦，不该动） */
        gate = 4.0 * med;
        if (strongest < (gate > 15.0 ? gate : 15.0))
            goto out;
        /* 闸门 2：唯一性。±3 行邻域外的次强跳变；最强须 >= 1.8×次强 */
        second = 0.0;
        for (y = 0; y < h; y++) {
            int dc = (y - ys + h) % h;      /* 环行距离 */
            if (dc > h / 2)
                dc = h - dc;
            if (dc > 3 && d[y] > second)
                second = d[y];
        }
        if (strongest < 1.8 * second)
            goto out;
        pos[f] = ys;
    }
    /* 闸门 3：时间稳定性，3 帧候选位置一致（±1 行） */
    {
        int mn = pos[0], mx = pos[0];
        for (f = 1; f < 3; f++) {
            if (pos[f] < mn) mn = pos[f];
            if (pos[f] > mx) mx = pos[f];
        }
        if (mx - mn > 1)
            goto out;
    }
    ret = (h - pos[2]) % h;

out:
    free(p);
    free(d);
    free(scratch);
    return ret;
}

void ccs_align_deroll(const uint8_t *in, uint8_t *out, int w, int h, int k)
{
    size_t size = (size_t)w * (size_t)h * 2u;
    size_t n;

    if (!in || !out || w <= 0 || h <= 0)
        return;
    k = ((k % h) + h) % h;
    n = (size_t)k * (size_t)w * 2u;
    /* Python: out = raw[-n:] + raw[:-n]（整体循环上移 k 行） */
    memcpy(out, in + size - n, n);
    memcpy(out + n, in, size - n);
}

int ccs_align_nudge(int roll, int delta, int h)
{
    if (h <= 0)
        return 0;
    if (delta == CCS_ALIGN_FLIP_HALF)       /* 主服务 nudge(99999)：翻半屏 */
        delta = h / 2;
    return ((roll + delta) % h + h) % h;
}
