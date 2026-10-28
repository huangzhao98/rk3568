/* demo_snapshot.c — 抓一帧 JPEG 存 snapshot.jpg + MJPEG 拉流回调演示。
 *
 * 用法: ./demo_snapshot [板子IP] [输出文件]
 * 只读调用（占板上一路流连接，取到帧即断开），无副作用。
 *
 * 注意：无信号（align failed）期间板上不出帧，snapshot 会读超时失败；
 * VGA 通道对不齐时同样可能拿不到帧。
 */
#include <stdio.h>
#include <stdlib.h>
#include "../ccs_sdk.h"

/* MJPEG 帧回调：每帧打印大小，收满 5 帧后停止 */
static int on_frame(const unsigned char *jpg, size_t len, void *user)
{
    (void)jpg;
    int *n = user;
    printf("  第 %d 帧: %zu 字节 (JPEG 头 %02X %02X)\n", *n + 1, len,
           jpg[0], jpg[1]);            /* JPEG 以 FF D8 开头 */
    return ++*n >= 5;                  /* 返回非 0 = 停止拉流 */
}

int main(int argc, char **argv)
{
    const char *host = argc > 1 ? argv[1] : "192.168.1.222";
    const char *out_path = argc > 2 ? argv[2] : "snapshot.jpg";

    ccs_client_t *c = ccs_new(host, 0, 0);
    if (!c) {
        fprintf(stderr, "ccs_new 失败\n");
        return 1;
    }

    /* ---- 抓单帧存文件（/stream 取首帧，只读） ---- */
    unsigned char *jpg = NULL;
    size_t jpg_len = 0;
    if (ccs_video_snapshot(c, &jpg, &jpg_len) == 0) {
        FILE *f = fopen(out_path, "wb");
        if (!f) {
            fprintf(stderr, "打不开 %s\n", out_path);
            free(jpg);
            ccs_free(c);
            return 1;
        }
        fwrite(jpg, 1, jpg_len, f);
        fclose(f);
        printf("已存 %s（%zu 字节）\n", out_path, jpg_len);
        free(jpg);
    } else {
        /* 常见原因：无信号 / align failed 不出帧导致读超时 */
        fprintf(stderr, "snapshot 失败: %s\n", ccs_error(c));
    }

    /* ---- MJPEG 回调式拉流演示（收 5 帧即停，只读） ---- */
    printf("MJPEG 拉流（5 帧）:\n");
    int frames = 0;
    if (ccs_video_mjpeg_stream(c, on_frame, &frames, 0) < 0)
        fprintf(stderr, "stream 失败: %s\n", ccs_error(c));

    ccs_free(c);
    return 0;
}
