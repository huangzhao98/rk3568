/*
 * demo_edid.c — 读出采集芯片当前 EDID 并以 hex 打印
 *
 * 用法：./demo_edid [subdev 节点]
 *   不传参数时自动扫描 /dev/v4l-subdev0..15，找第一个能读 EDID 的
 *   （即 TC358743 所在节点；主服务用 media-ctl 按 "tc35874x" 定位，
 *    效果相同）。扫描是纯只读操作，不影响正在运行的主服务。
 *
 * 写 EDID 的示例代码在文件尾部 write_example()——默认不调用，防误触。
 * 写 EDID 会让源端重新协商输出、破坏主服务的对齐状态，务必先停主服务。
 */
#include "../ccs_edid.h"

#include <errno.h>
#include <stdio.h>
#include <string.h>

/* 自动定位 TC358743 的 subdev 节点：逐个试读 EDID，能读的就是它 */
static const char *find_subdev(char *path, size_t path_size)
{
    int i;
    uint8_t probe[CCS_EDID_BLOCK];

    for (i = 0; i < 16; i++) {
        snprintf(path, path_size, "/dev/v4l-subdev%d", i);
        if (ccs_edid_read(path, probe, 1) > 0)
            return path;
    }
    return NULL;
}

/* 打印 EDID 摘要：厂商 ID / 监视器名 / 首个详细时序（对应
 * ccs_webcam.py edid_decode / edid_monitor_name 的最小移植） */
static void print_summary(const uint8_t *b, int len)
{
    int off;
    unsigned mid;

    if (len < 128)
        return;
    mid = ((unsigned)b[8] << 8) | b[9];
    printf("厂商 ID: %c%c%c\n",
           (char)(64 + ((mid >> 10) & 0x1f)),
           (char)(64 + ((mid >> 5) & 0x1f)),
           (char)(64 + (mid & 0x1f)));
    for (off = 54; off + 18 <= 126; off += 18) {    /* 描述符块 */
        const uint8_t *d = b + off;
        if (d[0] == 0 && d[1] == 0 && d[2] == 0 && d[3] == 0xFC) {
            char name[14];
            int n = 0, j;
            for (j = 5; j < 18 && d[j] != 0x0a; j++)
                name[n++] = (char)d[j];
            name[n] = '\0';
            printf("监视器名: %s\n", name);
        }
    }
    if (b[54] || b[55]) {                           /* 描述符 1 = 详细时序 */
        unsigned pclk = (unsigned)(b[54] | (b[55] << 8)) * 10000u;
        unsigned ha = (unsigned)b[56] | (((unsigned)b[58] >> 4) << 8);
        unsigned va = (unsigned)b[59] | (((unsigned)b[61] >> 4) << 8);
        printf("详细时序: %ux%u, 像素时钟 %.2f MHz\n",
               ha, va, (double)pclk / 1e6);
    }
}

/* ---- 写 EDID 示例（默认不调用！防误触） ----------------------------
 * 写之前先停主服务：systemctl stop ccs_webcam
 * 用法举例（取消 main 里注释后生效）：
 *   uint8_t edid[128];
 *   int n = ccs_edid_read(subdev, edid, 1);   // 读出当前 EDID 作底子
 *   edid[0x7e] 或任何字段按需修改……
 *   ccs_edid_write(subdev, edid, n, 1);       // fix=1 自动修校验和
 * 写入后主服务需要重对齐（它的网页面板"重新对齐"按钮，或重启服务）。
 * 恢复出厂 EDID：把 ccs_dev/edid_720p.hex.txt 的 128 字节解析后写入。
 */
static void write_example(const char *subdev)
{
    uint8_t edid[CCS_EDID_BLOCK];
    int n = ccs_edid_read(subdev, edid, 1);
    if (n <= 0) {
        fprintf(stderr, "读 EDID 失败，放弃写示例\n");
        return;
    }
    /* 示例：只改监视器名前 3 个字符为 "CCS"（描述符从 54 起每 18 字节，
     * 0xFC 类型是监视器名）。这里仅演示流程，字段偏移请按 EDID 规范来。 */
    {
        int off;
        for (off = 54; off + 18 <= 126; off += 18) {
            uint8_t *d = edid + off;
            if (d[0] == 0 && d[1] == 0 && d[2] == 0 && d[3] == 0xFC) {
                d[5] = 'C';
                d[6] = 'C';
                d[7] = 'S';
                break;
            }
        }
    }
    if (ccs_edid_write(subdev, edid, n, 1) == 1)
        printf("EDID 已写入并读回验证成功\n");
    else
        fprintf(stderr, "EDID 写入/验证失败: %s\n", strerror(errno));
}

int main(int argc, char **argv)
{
    char found[64];
    const char *subdev;
    uint8_t buf[2 * CCS_EDID_BLOCK];
    int n, i;

    if (argc > 1) {
        subdev = argv[1];
    } else {
        subdev = find_subdev(found, sizeof(found));
        if (!subdev) {
            fprintf(stderr, "未找到可读取 EDID 的 v4l-subdev 节点"
                            "（TC358743 未上线？）\n");
            return 1;
        }
    }

    n = ccs_edid_read(subdev, buf, 2);
    if (n < 0) {
        fprintf(stderr, "读 %s 失败: %s\n", subdev, strerror(errno));
        return 1;
    }
    if (n == 0) {
        printf("%s：芯片尚未写入 EDID\n", subdev);
        return 0;
    }
    printf("%s：读出 EDID %d 字节（%s）\n", subdev, n,
           ccs_edid_valid(buf, n) ? "校验和有效" : "校验和无效/垃圾 EDID");
    for (i = 0; i < n; i++) {
        if (i % 16 == 0)
            printf("%s", i ? "\n" : "");
        printf("%s%02x", (i % 16) ? " " : "", buf[i]);
    }
    printf("\n");
    print_summary(buf, n);

    /* 写 EDID 示例：默认注释掉防误触。确认主服务已停后再打开。
     * write_example(subdev);
     */
    (void)write_example;
    return 0;
}
