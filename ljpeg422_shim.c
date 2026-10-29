/* ljpeg422_shim.c — libjpeg-turbo 4:2:2 MJPEG 软编的 ctypes 封装
 * 为什么存在：vepu 硬编 JPEG 输出恒为 4:2:0（mpp HAL
 * hal_jpege_hdr.c 写死 "only output 420 bits"），纵向色度砍半——
 * 1px 高的彩色细线与相邻背景行平均后饱和度掉光，客户界面的绿色
 * 细线显示成灰色。本 shim 用板上 libjpeg-turbo 软编 4:2:2
 * （h2v1 采样，纵向色度全分辨率），细线颜色保住。
 * 与 mppjpeg_shim 同接口语义，ccs_webcam.py 经 ctypes 调用：
 *   int  ljpeg_open(int w, int h);                 0=ok（起 2 个编码线程）
 *   int  ljpeg_ninst(void);                        恒 1（对外同步接口）
 *   int  ljpeg_encode(const char *uyvy, int len,   >0=jpeg 字节数, <0=失败
 *                     char *out, int cap);
 *   void ljpeg_close(void);
 *   void ljpeg_set_filter(int bright, int sharp);  与 mpp shim 同语义
 *   void ljpeg_set_quality(int q);                 30~100，默认 85
 * 内部双槽流水：单线程 720p 4:2:2 q85 编码 ~19ms 不够 45fps 余量，
 * encode() 提交当前帧到槽 A 后立即取回槽 B 里上一帧的结果——
 * 有效周期 = max(编码, 采集周期)，代价是一帧延迟（~22ms，同 mpp 双实例
 * 流水的性质）；首帧同步编完直接返回，无错位。
 * 解交织+滤波拷贝趟 4 线程横带并行（同 mpp shim）。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <setjmp.h>
#include <pthread.h>
#include <jpeglib.h>

static int s_w, s_h, s_fsize;
static unsigned char s_lut_y[256], s_lut_uv[256];
static unsigned char s_lut_yf[256];
static int s_sharp = 0;
static int s_bright = 100;
static int s_quality = 85;

static void build_luts(void)
{
    for (int i = 0; i < 256; i++) {
        int y = (i - 16) * 255 / 219;
        int c = (i - 128) * 255 / 224 + 128;
        s_lut_y[i] = (unsigned char)(y < 0 ? 0 : y > 255 ? 255 : y);
        s_lut_uv[i] = (unsigned char)(c < 0 ? 0 : c > 255 ? 255 : c);
    }
}

void ljpeg_set_filter(int bright, int sharp)
{
    if (bright < 20) bright = 20;
    if (bright > 200) bright = 200;
    s_bright = bright;
    s_sharp = sharp < 0 ? 0 : sharp > 100 ? 100 : sharp;
    for (int i = 0; i < 256; i++) {
        int v = i * s_bright / 100;
        s_lut_yf[i] = s_lut_y[v > 255 ? 255 : v];
    }
}

void ljpeg_set_quality(int q)
{
    if (q < 30) q = 30;
    if (q > 100) q = 100;
    s_quality = q;
}

/* ---- 编码槽：平面缓冲 + 独立编码线程 ---- */
typedef struct {
    pthread_t th;
    pthread_mutex_t mu;
    pthread_cond_t cv;
    int state;              /* 0 idle / 1 working / 2 done / 3 quit */
    int inited;             /* mu/cv 已初始化（close 里 destroy，防重复
                             * init 的 UB——重开编码器后 open 失败的根因） */
    unsigned char *y, *u, *v;   /* 解交织平面：y w*h, u/v (w/2)*h */
    unsigned char *out;         /* jpeg 输出 w*h*2 */
    long outlen;                /* done 时有效；<0 = 编码失败 */
} LSlot;

static LSlot s_sl[2];
static int s_turn = 0;          /* 下一帧投递的槽 */
static int s_calls = 0;         /* 已投递帧数（首帧同步返回用） */

/* libjpeg 错误处理：longjmp 回来返回失败，不能让它 exit */
struct ljpeg_err {
    struct jpeg_error_mgr pub;
    jmp_buf jb;
};

static void ljpeg_error_exit(j_common_ptr cinfo)
{
    struct ljpeg_err *e = (struct ljpeg_err *)cinfo->err;
    longjmp(e->jb, 1);
}

static int encode_422(LSlot *sl)
{
    struct jpeg_compress_struct cinfo;
    struct ljpeg_err jerr;
    unsigned char *dst = sl->out;
    unsigned long dstlen = (unsigned long)s_fsize;
    int cw = s_w / 2;

    cinfo.err = jpeg_std_error(&jerr.pub);
    jerr.pub.error_exit = ljpeg_error_exit;
    if (setjmp(jerr.jb)) {
        jpeg_destroy_compress(&cinfo);
        return -20;
    }
    jpeg_create_compress(&cinfo);
    jpeg_mem_dest(&cinfo, &dst, &dstlen);   /* 用槽内缓冲，写满即错不 realloc */
    cinfo.image_width = s_w;
    cinfo.image_height = s_h;
    cinfo.input_components = 3;
    cinfo.in_color_space = JCS_YCbCr;
    jpeg_set_defaults(&cinfo);
    cinfo.comp_info[0].h_samp_factor = 2;   /* 4:2:2：横向半采样，纵向全分辨率 */
    cinfo.comp_info[0].v_samp_factor = 1;
    cinfo.raw_data_in = TRUE;
    jpeg_set_quality(&cinfo, s_quality, TRUE);
    jpeg_start_compress(&cinfo, TRUE);

    /* h2v1：所有分量 v_samp=1，iMCU 行 = 8 行；720/360 均 8 对齐 */
    while (cinfo.next_scanline < cinfo.image_height) {
        JSAMPROW yrows[8], urows[8], vrows[8];
        JSAMPARRAY planes[3];
        int r0 = cinfo.next_scanline;
        for (int i = 0; i < 8; i++) {
            yrows[i] = sl->y + (r0 + i) * s_w;
            urows[i] = sl->u + (r0 + i) * cw;
            vrows[i] = sl->v + (r0 + i) * cw;
        }
        planes[0] = yrows;
        planes[1] = urows;
        planes[2] = vrows;
        jpeg_write_raw_data(&cinfo, planes, 8);
    }
    jpeg_finish_compress(&cinfo);
    dstlen = (unsigned long)s_fsize - cinfo.dest->free_in_buffer;
    jpeg_destroy_compress(&cinfo);
    return (int)dstlen;
}

static int s_dbg = -1;
#define DBG(...) do { \
    if (s_dbg < 0) s_dbg = getenv("LJDBG") ? 1 : 0; \
    if (s_dbg) { fprintf(stderr, __VA_ARGS__); fflush(stderr); } \
} while (0)

static void *enc_worker(void *arg)
{
    LSlot *sl = (LSlot *)arg;
    DBG("[lj] worker %p start\n", (void *)sl);
    for (;;) {
        pthread_mutex_lock(&sl->mu);
        while (sl->state != 1 && sl->state != 3)
            pthread_cond_wait(&sl->cv, &sl->mu);
        if (sl->state == 3) {
            pthread_mutex_unlock(&sl->mu);
            DBG("[lj] worker %p quit\n", (void *)sl);
            return NULL;
        }
        pthread_mutex_unlock(&sl->mu);
        int rc = encode_422(sl);
        DBG("[lj] worker %p encoded rc=%d\n", (void *)sl, rc);
        pthread_mutex_lock(&sl->mu);
        sl->outlen = rc;
        /* close() 可能在本帧编码期间置了 quit(3)——绝不能覆盖回 2，
         * 否则下一轮循环把 3 误判为可等待状态永久睡眠，join 死锁 */
        if (sl->state == 1)
            sl->state = 2;
        pthread_cond_signal(&sl->cv);
        pthread_mutex_unlock(&sl->mu);
    }
}

/* UYVY [U Y0 V Y1] → 槽内平面 Y + 半宽 U/V，一趟完成 limited→full
 * 拉伸、亮度增益、横向 3 抽头锐化（语义同 mpp shim 的 filter_rows） */
static void split_rows(LSlot *sl, const unsigned char *s, int r0, int r1)
{
    int cw = s_w / 2;
    for (int r = r0; r < r1; r++) {
        const unsigned char *sr = s + r * s_w * 2;
        unsigned char *yp = sl->y + r * s_w;
        unsigned char *up = sl->u + r * cw;
        unsigned char *vp = sl->v + r * cw;
        for (int x = 0; x < cw; x++) {
            up[x] = s_lut_uv[sr[4 * x]];
            vp[x] = s_lut_uv[sr[4 * x + 2]];
        }
        if (s_sharp) {
            for (int x = 0; x < s_w; x++) {
                int v = sr[2 * x + 1];
                if (x > 0 && x + 1 < s_w) {
                    v += s_sharp * (2 * v - sr[2 * x - 1] - sr[2 * x + 3]) / 100;
                    v = v < 0 ? 0 : v > 255 ? 255 : v;
                }
                yp[x] = s_lut_yf[v];
            }
        } else {
            for (int x = 0; x < s_w; x++)
                yp[x] = s_lut_yf[sr[2 * x + 1]];
        }
    }
}

typedef struct {
    LSlot *sl;
    const unsigned char *s;
    int r0, r1;
} SplitJob;

static void *split_worker(void *arg)
{
    SplitJob *j = (SplitJob *)arg;
    split_rows(j->sl, j->s, j->r0, j->r1);
    return NULL;
}

static void split_frame(LSlot *sl, const unsigned char *s)
{
    pthread_t th[4];
    SplitJob jobs[4];
    int step = (s_h + 3) / 4;
    int n = 0;
    for (int i = 0; i < 4 && i * step < s_h; i++) {
        jobs[n].sl = sl;
        jobs[n].s = s;
        jobs[n].r0 = i * step;
        jobs[n].r1 = (i + 1) * step < s_h ? (i + 1) * step : s_h;
        if (pthread_create(&th[n], NULL, split_worker, &jobs[n]) == 0)
            n++;
        else
            split_rows(sl, s, jobs[n].r0, jobs[n].r1);
    }
    for (int i = 0; i < n; i++)
        pthread_join(th[i], NULL);
}

static int slot_open(LSlot *sl)
{
    memset(sl, 0, sizeof(*sl));
    pthread_mutex_init(&sl->mu, NULL);
    pthread_cond_init(&sl->cv, NULL);
    sl->inited = 1;
    sl->y = malloc(s_w * s_h);
    sl->u = malloc(s_w * s_h / 2);
    sl->v = malloc(s_w * s_h / 2);
    sl->out = malloc(s_fsize);
    if (!sl->y || !sl->u || !sl->v || !sl->out)
        return -1;
    return pthread_create(&sl->th, NULL, enc_worker, sl) ? -2 : 0;
}

void ljpeg_close(void);

int ljpeg_open(int w, int h)
{
    if (w <= 0 || h <= 0 || (w % 16) || (h % 8))
        return -1;
    ljpeg_close();      /* 幂等：重开前先清干净（线程/锁/缓冲） */
    s_w = w;
    s_h = h;
    s_fsize = w * h * 2;
    build_luts();
    ljpeg_set_filter(s_bright, s_sharp);
    s_turn = 0;
    s_calls = 0;
    if (slot_open(&s_sl[0]) || slot_open(&s_sl[1]))
        return -2;
    return 0;
}

int ljpeg_ninst(void)
{
    return 1;
}

/* 等待槽到达目标状态；带 mu 进出。返回槽当前 outlen（state==2 时有效） */
static long slot_wait_done(LSlot *sl)
{
    pthread_mutex_lock(&sl->mu);
    while (sl->state == 1)
        pthread_cond_wait(&sl->cv, &sl->mu);
    long n = (sl->state == 2) ? sl->outlen : 0;
    sl->state = 0;
    pthread_mutex_unlock(&sl->mu);
    return n;
}

static void slot_submit(LSlot *cur, const unsigned char *uyvy)
{
    split_frame(cur, uyvy);
    pthread_mutex_lock(&cur->mu);
    cur->state = 1;
    pthread_cond_signal(&cur->cv);
    pthread_mutex_unlock(&cur->mu);
}

int ljpeg_encode(const char *uyvy, int len, char *out, int cap)
{
    if (len != s_fsize)
        return -14;     /* 短帧拒绝（同 mpp shim，防绿屏） */
    if (cap < s_fsize)
        return -15;
    LSlot *cur = &s_sl[s_turn];
    LSlot *prev = &s_sl[s_turn ^ 1];
    s_turn ^= 1;
    s_calls++;
    DBG("[lj] encode call%d cur=%d prevstate=%d\n",
        s_calls, (int)(cur - s_sl), prev->state);

    /* 当前槽必须先空闲（正常节奏下上一圈早已取走） */
    slot_wait_done(cur);
    slot_submit(cur, (const unsigned char *)uyvy);

    /* 首帧：另一槽还没有在编的帧，同步等当前槽编完直接返回；随后把同一帧
     * 再投一次当前槽预热流水——第 2 次调用起 prev 槽恒有在飞帧可返回，
     * 代价只是第 2 次返回第 1 帧的重复画面（流开头多一帧，不可见） */
    if (s_calls == 1) {
        long n = slot_wait_done(cur);
        if (n > 0 && n <= cap)
            memcpy(out, cur->out, n);
        if (n > 0) {
            slot_wait_done(cur);
            slot_submit(cur, (const unsigned char *)uyvy);
        }
        return n > 0 ? (int)n : (n < 0 ? (int)n : -16);
    }
    /* 常规：取回另一槽里上一帧的结果（多数情况早已编完，零等待） */
    long n = slot_wait_done(prev);
    if (n > 0 && n <= cap) {
        memcpy(out, prev->out, n);
        return (int)n;
    }
    return n < 0 ? (int)n : -16;
}

void ljpeg_close(void)
{
    for (int i = 0; i < 2; i++) {
        LSlot *sl = &s_sl[i];
        if (sl->th) {
            pthread_mutex_lock(&sl->mu);
            sl->state = 3;
            pthread_cond_signal(&sl->cv);
            pthread_mutex_unlock(&sl->mu);
            pthread_join(sl->th, NULL);
            sl->th = 0;
        }
        if (sl->inited) {
            pthread_mutex_destroy(&sl->mu);
            pthread_cond_destroy(&sl->cv);
            sl->inited = 0;
        }
        free(sl->y); free(sl->u); free(sl->v); free(sl->out);
        sl->y = sl->u = sl->v = sl->out = NULL;
    }
}
