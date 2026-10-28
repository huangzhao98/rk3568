/* demo_uart.c — RS485 串口收发演示（增量拉取的 since 偏移语义）。
 *
 * 用法: ./demo_uart [板子IP]
 *
 * 说明：
 * - recv 是纯读取（无副作用）；send 会真的往 485 线上发字节（演示用的
 *   send 调用放在注释里，接好从设备再启用）；
 * - since 是客户端侧的逻辑偏移：首次传 0 拉存量（最多 64KB 环形缓冲），
 *   之后把上次返回的 next 原样传回即可无缝续拉；
 * - 演示"只收新数据"的做法：先传 0 拿到当前偏移，跳过存量，再轮询增量
 *   （对应 Python 版 UartConsole backlog=False）。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "../ccs_sdk.h"

int main(int argc, char **argv)
{
    const char *host = argc > 1 ? argv[1] : "192.168.1.222";
    ccs_client_t *c = ccs_new(host, 0, 0);
    if (!c) {
        fprintf(stderr, "ccs_new 失败\n");
        return 1;
    }

    /* ---- 先拿当前偏移（跳过存量缓冲，只收之后的新数据） ---- */
    char *data = NULL;
    long next = 0, baud = 0;
    if (ccs_uart_recv(c, CCS_UART_RS485, 0, 0, &data, &next, &baud) < 0) {
        fprintf(stderr, "uart_recv 失败: %s\n", ccs_error(c));
        ccs_free(c);
        return 1;
    }
    printf("485 当前波特率: %ld，逻辑偏移: %ld（存量 %zu 字节已跳过）\n",
           baud, next, data ? strlen(data) : 0);
    free(data);

    /* ---- 发送演示（有副作用：往 485 线上真实发字节！默认注释掉）----
     * char msg[64];
     * // 发文本（自动追加 \r\n）：
     * if (ccs_uart_send(c, CCS_UART_RS485, "Hello from C SDK", 0, 1,
     *                   msg, sizeof(msg)) == 0)
     *     printf("发送: %s\n", msg);          // '已发 18B'
     * // 发原始字节（自动转 hex，二进制安全）：
     * unsigned char bin[] = {0x01, 0x03, 0x00, 0x00, 0x00, 0x02, 0xC4, 0x0B};
     * ccs_uart_send_bytes(c, CCS_UART_RS485, bin, sizeof(bin), 0,
     *                     msg, sizeof(msg));
     * // 改波特率（立即生效，影响所有会话；白名单 9600~921600）：
     * ccs_uart_set_baud(c, CCS_UART_RS485, 115200, msg, sizeof(msg));
     */

    /* ---- 轮询 10 秒收新数据（只读） ---- */
    printf("监听 485 新数据（10 秒）...\n");
    for (int i = 0; i < 10; i++) {
        data = NULL;
        if (ccs_uart_recv(c, CCS_UART_RS485, next, 0, &data, &next,
                          NULL) == 0) {
            if (data && data[0])
                printf("<< %s", data);  /* 串口数据自带换行 */
            free(data);
        } else {
            /* 单次失败不退出（板子重启/网络抖动后自恢复） */
            fprintf(stderr, "recv 失败（继续重试）: %s\n", ccs_error(c));
        }
        sleep(1);
    }

    ccs_free(c);
    return 0;
}
