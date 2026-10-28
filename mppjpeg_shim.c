/* mppjpeg_shim.c — librockchip_mpp vepu JPEG 硬编的极简 ctypes 封装
 * 只暴露普通 C 函数，ccs_webcam.py 经 ctypes 调用：
 *   int  mppjpeg_open(int w, int h);                 0=ok（尝试开 2 个实例）
 *   int  mppjpeg_ninst(void);                        可用实例数（1 或 2）
 *   int  mppjpeg_encode(const char *uyvy, int len,   >0=jpeg 字节数, <0=失败
 *                       char *out, int cap);         单实例同步（兼容老调用）
 *   int  mppjpeg_submit(int idx, const char *uyvy, int len);  0=ok 拷贝+滤波+投递
 *   int  mppjpeg_get(int idx, char *out, int cap);   >0=jpeg 字节数 等该实例出包
 *   void mppjpeg_close(void);
 * 双实例流水（急速模式）：submit 是异步的（mpp 内部队列），submit 当前帧后
 * 立即 get 上一帧——拷贝/滤波的 CPU 时间与 vepu 引擎时间重叠，
 * 周期从 拷贝10+引擎10=20ms 降到 max(拷贝,引擎)≈10~18ms。
 * 外加 RGA 硬件缩放（1080p 源降到 720p 再进 vepu——vepu 直编 1080p
 * 实测 >1s/帧且因 stride 未对齐触发 IOMMU 缺页风暴，不可用）：
 *   int  uyvy_scale(const char *src, int sw, int sh,  0=ok
 *                   char *dst, int dw, int dh);
 * 外加采集端画面补偿（VGA 模拟采样偏暗/偏糊，MS9288C 无 I2C 可配置，
 * 只能数字补偿；python numpy 实测 75ms/帧太慢，合并进编码拷贝趟 ~3ms）：
 *   void mppjpeg_set_filter(int bright, int sharp);
 *        bright=亮度增益%(100=原始)  sharp=锐化强度0~100(0=关，横向3抽头)
 * submit/get 仅由采集循环单线程交替调用；encode 兼容路径同理。
 */
#include <string.h>
#include <pthread.h>

#include "rk_mpi.h"
#include "mpp_frame.h"
#include "mpp_buffer.h"
#include "mpp_packet.h"
#include "rk_venc_cfg.h"
#include "rga/RgaApi.h"
#include "rga/im2d.h"

int uyvy_scale(const char *src, int sw, int sh, char *dst, int dw, int dh)
{
    rga_buffer_t s = wrapbuffer_virtualaddr_t((void *)src, sw, sh, sw, sh,
                                              RK_FORMAT_UYVY_422);
    rga_buffer_t d = wrapbuffer_virtualaddr_t(dst, dw, dh, dw, dh,
                                              RK_FORMAT_UYVY_422);
    return imresize_t(s, d, 0, 0, INTER_LINEAR, 1) == IM_STATUS_SUCCESS ? 0 : -1;
}

typedef struct {
    MppCtx ctx;
    MppApi *mpi;
    MppBuffer in, out;
} EncSlot;

static EncSlot s_e[2];
static int s_ninst = 0;
static int s_w, s_h, s_fsize;
static unsigned char s_lut_y[256], s_lut_uv[256];
static unsigned char s_lut_yf[256];   /* 亮度增益 ∘ limited→full 复合 LUT */
static int s_sharp = 0;               /* 锐化强度 0~100，0=关 */
static int s_bright = 100;            /* 亮度增益 %，100=原始 */

static void build_luts(void)
{
    /* limited(16~235/240) → full(0~255)，与 python 侧 int32 公式一致 */
    for (int i = 0; i < 256; i++) {
        int y = (i - 16) * 255 / 219;
        int c = (i - 128) * 255 / 224 + 128;
        s_lut_y[i] = (unsigned char)(y < 0 ? 0 : y > 255 ? 255 : y);
        s_lut_uv[i] = (unsigned char)(c < 0 ? 0 : c > 255 ? 255 : c);
    }
}

/* 画面补偿参数：bright=亮度增益%(100=原始)，sharp=锐化0~100(0=关)。
 * 任何线程随时可调（只改 LUT/系数，不动编码器状态）。 */
void mppjpeg_set_filter(int bright, int sharp)
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

static int slot_open(EncSlot *e, int w, int h)
{
    if (mpp_create(&e->ctx, &e->mpi) || !e->ctx)
        return -1;
    if (mpp_init(e->ctx, MPP_CTX_ENC, MPP_VIDEO_CodingMJPEG))
        return -2;

    MppEncCfg cfg = NULL;
    mpp_enc_cfg_init(&cfg);
    mpp_enc_cfg_set_s32(cfg, "prep:width", w);
    mpp_enc_cfg_set_s32(cfg, "prep:height", h);
    mpp_enc_cfg_set_s32(cfg, "prep:hor_stride", w);
    mpp_enc_cfg_set_s32(cfg, "prep:ver_stride", h);
    mpp_enc_cfg_set_s32(cfg, "prep:format", MPP_FMT_YUV422_UYVY);
    mpp_enc_cfg_set_s32(cfg, "rc:mode", MPP_ENC_RC_MODE_FIXQP);
    mpp_enc_cfg_set_s32(cfg, "rc:fps_in_num", 60);
    mpp_enc_cfg_set_s32(cfg, "rc:fps_in_denorm", 1);
    mpp_enc_cfg_set_s32(cfg, "rc:fps_out_num", 60);
    mpp_enc_cfg_set_s32(cfg, "rc:fps_out_denorm", 1);
    mpp_enc_cfg_set_s32(cfg, "codec:type", MPP_VIDEO_CodingMJPEG);
    if (e->mpi->control(e->ctx, MPP_ENC_SET_CFG, cfg))
        return -3;

    if (mpp_buffer_get(NULL, &e->in, s_fsize))
        return -4;
    if (mpp_buffer_get(NULL, &e->out, s_fsize))
        return -5;
    return 0;
}

int mppjpeg_open(int w, int h)
{
    s_w = w;
    s_h = h;
    s_fsize = w * h * 2;
    build_luts();
    mppjpeg_set_filter(s_bright, s_sharp);   /* 重开编码器后恢复补偿参数 */

    memset(s_e, 0, sizeof(s_e));
    s_ninst = 0;
    if (slot_open(&s_e[0], w, h))
        return -1;
    s_ninst = 1;
    /* 第二实例失败不致命，调用方按 mppjpeg_ninst() 回退单实例同步编码 */
    if (slot_open(&s_e[1], w, h) == 0)
        s_ninst = 2;
    return 0;
}

int mppjpeg_ninst(void)
{
    return s_ninst;
}

/* 拷贝 + 锐化(横向3抽头) + 亮度增益 + limited→full 拉伸一趟完成
 * （UYVY = [U Y0 V Y1] 四字节组）。VGA 模拟采样的模糊只在水平方向
 * （像素时钟相位），垂直方向按行锁相是精确的，锐化只做横向。
 * 行首/行尾像素无左右邻居，跳过锐化直出。 */
static void filter_rows(unsigned char *d, const unsigned char *s, int r0, int r1)
{
    for (int r = r0; r < r1; r++) {
        const unsigned char *sr = s + r * s_w * 2;
        unsigned char *dr = d + r * s_w * 2;
        for (int x = 0; x + 1 < s_w; x += 2) {
            dr[2 * x]     = s_lut_uv[sr[2 * x]];
            dr[2 * x + 2] = s_lut_uv[sr[2 * x + 2]];
        }
        if (s_sharp) {
            for (int x = 0; x < s_w; x++) {
                int v = sr[2 * x + 1];
                if (x > 0 && x + 1 < s_w) {
                    v += s_sharp * (2 * v - sr[2 * x - 1] - sr[2 * x + 3]) / 100;
                    v = v < 0 ? 0 : v > 255 ? 255 : v;
                }
                dr[2 * x + 1] = s_lut_yf[v];
            }
        } else {
            for (int x = 0; x < s_w; x++)
                dr[2 * x + 1] = s_lut_yf[sr[2 * x + 1]];
        }
    }
}

/* 4 核 A55 按横带并行（单线程 ~10ms/720p 帧 → 4 线程 ~3ms） */
typedef struct {
    unsigned char *d;
    const unsigned char *s;
    int r0, r1;
} FilterJob;

static void *filter_worker(void *arg)
{
    FilterJob *j = (FilterJob *)arg;
    filter_rows(j->d, j->s, j->r0, j->r1);
    return NULL;
}

static void filter_copy(unsigned char *d, const unsigned char *s)
{
    pthread_t th[4];
    FilterJob jobs[4];
    int step = (s_h + 3) / 4;
    int n = 0;
    for (int i = 0; i < 4 && i * step < s_h; i++) {
        jobs[n].d = d;
        jobs[n].s = s;
        jobs[n].r0 = i * step;
        jobs[n].r1 = (i + 1) * step < s_h ? (i + 1) * step : s_h;
        if (pthread_create(&th[n], NULL, filter_worker, &jobs[n]) == 0)
            n++;
        else
            filter_rows(d, s, jobs[n].r0, jobs[n].r1);   /* 创建失败就地跑 */
    }
    for (int i = 0; i < n; i++)
        pthread_join(th[i], NULL);
}

int mppjpeg_submit(int idx, const char *uyvy, int len)
{
    if (idx >= s_ninst || len > s_fsize)
        return -10;
    if (len != s_fsize)
        return -14;     /* 短帧（驱动格式掉了），拒绝编码——否则输出绿屏 JPEG */
    EncSlot *e = &s_e[idx];
    filter_copy((unsigned char *)mpp_buffer_get_ptr(e->in),
                (const unsigned char *)uyvy);

    MppFrame frame = NULL;
    mpp_frame_init(&frame);
    mpp_frame_set_width(frame, s_w);
    mpp_frame_set_height(frame, s_h);
    mpp_frame_set_hor_stride(frame, s_w);
    mpp_frame_set_ver_stride(frame, s_h);
    mpp_frame_set_fmt(frame, MPP_FMT_YUV422_UYVY);
    mpp_frame_set_buffer(frame, e->in);

    if (e->mpi->encode_put_frame(e->ctx, frame)) {
        mpp_frame_deinit(&frame);
        return -11;
    }
    mpp_frame_deinit(&frame);
    return 0;
}

int mppjpeg_get(int idx, char *out, int cap)
{
    if (idx >= s_ninst)
        return -10;
    EncSlot *e = &s_e[idx];
    MppPacket packet = NULL;
    mpp_packet_init(&packet, e->out, s_fsize);
    if (e->mpi->encode_get_packet(e->ctx, &packet) || !packet)
        return -12;
    int n = (int)mpp_packet_get_length(packet);
    if (n > 0 && n <= cap)
        memcpy(out, mpp_packet_get_pos(packet), n);
    else
        n = -13;
    mpp_packet_deinit(&packet);
    return n;
}

int mppjpeg_encode(const char *uyvy, int len, char *out, int cap)
{
    /* 单实例同步路径（兼容老调用）：submit + get 一把梭 */
    int r = mppjpeg_submit(0, uyvy, len);
    if (r)
        return r;
    return mppjpeg_get(0, out, cap);
}

void mppjpeg_close(void)
{
    for (int i = 0; i < 2; i++) {
        EncSlot *e = &s_e[i];
        if (e->ctx) {
            e->mpi->reset(e->ctx);
            mpp_destroy(e->ctx);
            e->ctx = NULL;
        }
        if (e->in) { mpp_buffer_put(e->in); e->in = NULL; }
        if (e->out) { mpp_buffer_put(e->out); e->out = NULL; }
    }
    s_ninst = 0;
}
