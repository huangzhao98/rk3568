/* bench_rkmpp.c — RK3568 rkmpp 硬编码可行性/性能基准
 * 用法: bench_rkmpp <h264|mjpeg> <width> <height> <frames> [uyvy|nv12]
 * 流程: mpp ENC 初始化 → 喂纯色测试帧 → 逐帧 put/get 计时 → 输出统计
 * 首帧码流写 /tmp/bench_rkmpp.out 供有效性检查（h264 含 SPS/PPS 头）。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "rk_mpi.h"
#include "mpp_frame.h"
#include "mpp_buffer.h"
#include "mpp_packet.h"
#include "rk_venc_cfg.h"

static double now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

int main(int argc, char **argv)
{
    if (argc < 5) {
        fprintf(stderr, "usage: %s <h264|mjpeg> <w> <h> <frames> [uyvy|nv12]\n", argv[0]);
        return 1;
    }
    int is_mjpeg = !strcmp(argv[1], "mjpeg");
    int w = atoi(argv[2]), h = atoi(argv[3]), nfrm = atoi(argv[4]);
    int nv12 = argc > 5 && !strcmp(argv[5], "nv12");
    MppCodingType coding = is_mjpeg ? MPP_VIDEO_CodingMJPEG : MPP_VIDEO_CodingAVC;
    MppFrameFormat fmt = nv12 ? MPP_FMT_YUV420SP : MPP_FMT_YUV422_UYVY;
    size_t fsize = nv12 ? (size_t)w * h * 3 / 2 : (size_t)w * h * 2;

    MppCtx ctx = NULL;
    MppApi *mpi = NULL;
    MPP_RET ret = mpp_create(&ctx, &mpi);
    if (ret || !ctx) { fprintf(stderr, "mpp_create failed %d\n", ret); return 1; }
    ret = mpp_init(ctx, MPP_CTX_ENC, coding);
    if (ret) { fprintf(stderr, "mpp_init failed %d\n", ret); return 1; }

    MppEncCfg cfg = NULL;
    mpp_enc_cfg_init(&cfg);
    mpp_enc_cfg_set_s32(cfg, "prep:width", w);
    mpp_enc_cfg_set_s32(cfg, "prep:height", h);
    mpp_enc_cfg_set_s32(cfg, "prep:hor_stride", w);
    mpp_enc_cfg_set_s32(cfg, "prep:ver_stride", h);
    mpp_enc_cfg_set_s32(cfg, "prep:format", fmt);
    mpp_enc_cfg_set_s32(cfg, "rc:mode", is_mjpeg ? MPP_ENC_RC_MODE_FIXQP : MPP_ENC_RC_MODE_CBR);
    mpp_enc_cfg_set_s32(cfg, "rc:bps_target", 4 * 1000 * 1000);
    mpp_enc_cfg_set_s32(cfg, "rc:bps_max", 6 * 1000 * 1000);
    mpp_enc_cfg_set_s32(cfg, "rc:bps_min", 1 * 1000 * 1000);
    mpp_enc_cfg_set_s32(cfg, "rc:fps_in_flex", 0);
    mpp_enc_cfg_set_s32(cfg, "rc:fps_in_num", 30);
    mpp_enc_cfg_set_s32(cfg, "rc:fps_in_denorm", 1);
    mpp_enc_cfg_set_s32(cfg, "rc:fps_out_flex", 0);
    mpp_enc_cfg_set_s32(cfg, "rc:fps_out_num", 30);
    mpp_enc_cfg_set_s32(cfg, "rc:fps_out_denorm", 1);
    mpp_enc_cfg_set_s32(cfg, "rc:gop", 60);
    mpp_enc_cfg_set_s32(cfg, "codec:type", coding);
    ret = mpi->control(ctx, MPP_ENC_SET_CFG, cfg);
    if (ret) { fprintf(stderr, "MPP_ENC_SET_CFG failed %d\n", ret); return 1; }

    /* h264: 拿 SPS/PPS 头，验证码流有效性 */
    FILE *out = fopen("/tmp/bench_rkmpp.out", "wb");
    if (!is_mjpeg) {
        MppBuffer hdr_buf = NULL;
        mpp_buffer_get(NULL, &hdr_buf, 4096);
        MppPacket hdr = NULL;
        mpp_packet_init(&hdr, hdr_buf, 4096);
        if (mpi->control(ctx, MPP_ENC_GET_HDR_SYNC, hdr) == 0) {
            size_t len = mpp_packet_get_length(hdr);
            void *pos = mpp_packet_get_pos(hdr);
            printf("header: %zu bytes, start %02x %02x %02x %02x\n", len,
                   ((unsigned char *)pos)[0], ((unsigned char *)pos)[1],
                   ((unsigned char *)pos)[2], ((unsigned char *)pos)[3]);
            if (out) fwrite(pos, 1, len, out);
        }
        mpp_packet_deinit(&hdr);
        mpp_buffer_put(hdr_buf);
    }

    /* 输入帧缓冲：渐变彩条测试图案 */
    MppBuffer frmbuf = NULL;
    ret = mpp_buffer_get(NULL, &frmbuf, fsize);
    if (ret || !frmbuf) { fprintf(stderr, "mpp_buffer_get(in) failed %d\n", ret); return 1; }
    unsigned char *fp = (unsigned char *)mpp_buffer_get_ptr(frmbuf);
    for (int y = 0; y < h; y++)
        for (int x = 0; x < w; x++) {
            int v = (x * 255 / w) ^ (y & 0x40 ? 0x80 : 0);
            if (nv12) {
                fp[y * w + x] = (unsigned char)v;
                if (!(y & 1) && !(x & 1)) {
                    fp[w * h + (y / 2) * w + (x & ~1)] = 128;
                    fp[w * h + (y / 2) * w + (x & ~1) + 1] = (unsigned char)(255 - v);
                }
            } else {
                unsigned char *p = fp + y * w * 2 + (x & ~1) * 2;
                if (!(x & 1)) { p[0] = 128; p[2] = (unsigned char)(255 - v); }
                p[1 + (x & 1) * 2] = (unsigned char)v;
            }
        }

    MppBuffer outbuf = NULL;
    mpp_buffer_get(NULL, &outbuf, fsize);

    double t_all0 = now_ms();
    double tmin = 1e9, tmax = 0, tsum = 0;
    int got = 0;
    size_t total_bytes = 0;
    for (int i = 0; i < nfrm; i++) {
        MppFrame frame = NULL;
        mpp_frame_init(&frame);
        mpp_frame_set_width(frame, w);
        mpp_frame_set_height(frame, h);
        mpp_frame_set_hor_stride(frame, w);
        mpp_frame_set_ver_stride(frame, h);
        mpp_frame_set_fmt(frame, fmt);
        mpp_frame_set_buffer(frame, frmbuf);
        mpp_frame_set_eos(frame, i == nfrm - 1);

        double t0 = now_ms();
        ret = mpi->encode_put_frame(ctx, frame);
        if (ret) { fprintf(stderr, "put_frame failed %d @%d\n", ret, i); return 1; }

        MppPacket packet = NULL;
        mpp_packet_init(&packet, outbuf, fsize);
        ret = mpi->encode_get_packet(ctx, &packet);
        double dt = now_ms() - t0;
        if (ret) { fprintf(stderr, "get_packet failed %d @%d\n", ret, i); return 1; }
        size_t len = packet ? mpp_packet_get_length(packet) : 0;
        if (len) {
            got++;
            total_bytes += len;
            if (out && got <= 2)
                fwrite(mpp_packet_get_pos(packet), 1, len, out);
        }
        if (packet) mpp_packet_deinit(&packet);
        mpp_frame_deinit(&frame);
        if (dt < tmin) tmin = dt;
        if (dt > tmax) tmax = dt;
        tsum += dt;
    }
    double t_all = now_ms() - t_all0;

    printf("%s %dx%d %s: %d/%d packets, %.1f KB total\n",
           is_mjpeg ? "MJPEG" : "H264", w, h, nv12 ? "NV12" : "UYVY",
           got, nfrm, total_bytes / 1024.0);
    printf("per-frame put+get: avg %.2f ms, min %.2f, max %.2f  => 理论编码上限 %.1f fps\n",
           tsum / nfrm, tmin, tmax, 1000.0 / (tsum / nfrm));
    printf("wall: %.0f ms for %d frames => %.1f fps\n", t_all, nfrm, 1000.0 * nfrm / t_all);

    if (out) fclose(out);
    mpp_buffer_put(outbuf);
    mpp_buffer_put(frmbuf);
    mpi->reset(ctx);
    mpp_destroy(ctx);
    return 0;
}
