/* demo_stat.c — 只读演示：采集状态 + 当前通道 + 四路 GPIO 外部输入电平。
 *
 * 用法: ./demo_stat [板子IP]        （默认 192.168.1.222）
 * 本示例全部为只读调用，无副作用。
 */
#include <stdio.h>
#include "../ccs_sdk.h"

int main(int argc, char **argv)
{
    const char *host = argc > 1 ? argv[1] : "192.168.1.222";
    ccs_client_t *c = ccs_new(host, 0, 0);      /* 默认 8080 端口 / 5 秒超时 */
    if (!c) {
        fprintf(stderr, "ccs_new 失败\n");
        return 1;
    }

    /* ---- 采集状态（GET /stat，只读） ---- */
    char stat[256];
    if (ccs_video_stat(c, stat, sizeof(stat)) == 0)
        printf("采集状态: %s", stat);
    else
        fprintf(stderr, "stat 失败: %s\n", ccs_error(c));

    /* ---- 当前通道（从 /stat 解析，只读） ---- */
    char ch[16];
    if (ccs_channel_current(c, ch, sizeof(ch)) == 0)
        printf("当前通道: %s\n", ch);
    else
        fprintf(stderr, "channel 失败: %s\n", ccs_error(c));

    /* ---- 四路外部输入实时电平（GET /gpio_vals，只读） ---- */
    ccs_gpio_input_t inputs[4];
    int count = 0;
    if (ccs_gpio_read_inputs(c, inputs, &count) == 0) {
        printf("GPIO 外部输入:\n");
        for (int i = 0; i < count; i++)
            printf("  GPIO%-4d %-18s = %s\n", inputs[i].num, inputs[i].name,
                   inputs[i].level ? "高" : "低");
    } else {
        fprintf(stderr, "gpio_vals 失败: %s\n", ccs_error(c));
    }

    /* ---- 写操作示例（默认不执行！）----
     * 以下调用有实际副作用，演示故注释掉，确认后果后再启用：
     *
     * char msg[256];
     * // 切换采集通道：服务重启约 15 秒，期间所有 API 不可用！
     * ccs_channel_switch(c, "hdmi", msg, sizeof(msg));
     *
     * // 触发重对齐：画面中断几秒
     * ccs_video_realign(c, msg, sizeof(msg));
     *
     * // 相位微调：d=99999 翻半屏；标记 roll_manual 停掉自动 track
     * ccs_video_nudge(c, 1, msg, sizeof(msg));
     *
     * // 亮度/锐化：即时生效且持久化到 /etc（复位用 100 / 0）
     * ccs_image_set_brightness(c, 120, msg, sizeof(msg));
     * ccs_image_set_sharpen(c, 30, msg, sizeof(msg));
     *
     * // GPIO 置高：只限白名单脚 WORKING_LEDEN_H（编号看 gpio_list）
     * ccs_gpio_set(c, 41, 1, msg, sizeof(msg));
     */

    ccs_free(c);
    return 0;
}
