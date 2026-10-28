/* ccs_http.h — 最小 HTTP/1.1 客户端（纯 BSD socket 实现）。
 *
 * 为什么不依赖 libcurl：本 SDK 面向嵌入式裁剪场景（Buildroot/精简 rootfs），
 * libcurl 及其依赖链（openssl/zlib 等）在目标系统上可能不存在；板子服务
 * （ccs_webcam.py，Python http.server）只用到 HTTP/1.1 的一个很小子集，
 * 纯 socket 几百行即可覆盖，零外部依赖、交叉编译直接可用。
 *
 * 与 Python SDK（ccs_sdk/client.py）对齐的关键约定：
 * - POST 一律 application/x-www-form-urlencoded（板上 _post_form 只认这种）；
 * - **不跟随 303 重定向**：板上表单类端点（/edid_apply 等）把操作结果放在
 *   303 Location 的 ?msg=/?err= 查询串里，跟随跳转只会拿到管理页 HTML，
 *   操作结果反而丢了；
 * - 响应体动态缓冲：有 Content-Length 按长度读，没有则读到连接关闭
 *   （/stream 无限流是 Connection: close 无长度，由 ccs_http_open_stream
 *   单独处理，本模块普通请求一律发 Connection: close）。
 *
 * C99，仅依赖 libc + BSD socket（Linux 通用，无 POSIX 之外的扩展）。
 */
#ifndef CCS_HTTP_H
#define CCS_HTTP_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 一次 HTTP 请求的响应。body 为动态分配的缓冲区（文本场景保证以 '\0' 结尾，
 * 但二进制内容里可能含 NUL，长度一律以 body_len 为准）。 */
typedef struct {
    int status;          /* HTTP 状态码（200/303/403/500/503...） */
    char *body;          /* 响应体（malloc，用 ccs_http_resp_free 释放） */
    size_t body_len;     /* 响应体字节数 */
    char location[512];  /* Location 响应头原文（303 端点取 ?msg=/?err= 用），无则空串 */
} ccs_http_resp_t;

/* 发送一次 HTTP 请求（每次请求新建 TCP 连接，发 Connection: close）。
 *
 * 参数:
 *   host       板子 IP 或主机名，如 "192.168.1.222"
 *   port       服务端口，默认 8080
 *   timeout_ms 连接/读/写统一超时（毫秒）
 *   method     "GET" 或 "POST"
 *   path       以 / 开头的路径（含查询串），如 "/nudge?d=10"
 *   form_body  已 urlencode 的表单体（"a=1&b=2"），NULL 表示无请求体（GET）
 *   resp       输出响应（任何 HTTP 状态码都正常填充，含 4xx/5xx/303）
 *   err        连接层错误描述输出缓冲（可为 NULL）
 *   errlen     err 缓冲长度
 *
 * 返回: 0 = 收到 HTTP 响应（无论状态码，由调用者判定）；
 *      -1 = 连接层失败（DNS/连接拒绝/超时/读写错误），err 里有原因。
 *      与 Python 版一致：HTTP 错误码不算传输失败——303 是表单端点的成功
 *      路径，403/500 的正文要带回给调用者看原因。
 */
int ccs_http_request(const char *host, int port, int timeout_ms,
                     const char *method, const char *path,
                     const char *form_body,
                     ccs_http_resp_t *resp, char *err, size_t errlen);

/* 释放响应体。resp 内容归零，可安全重复调用。 */
void ccs_http_resp_free(ccs_http_resp_t *resp);

/* URL 编码（application/x-www-form-urlencoded 规则：空格→%20 而非 +，
 * 与 Python urllib.parse.urlencode 默认一致；板上 parse_qs 两种都认）。
 * 返回 malloc 字符串，调用者 free。失败返回 NULL。 */
char *ccs_urlencode(const char *s);

/* 打开 /stream 这类无限流连接：发 GET 请求、读响应头，返回 socket fd。
 * 调用者随后自行按分帧格式从 fd 读（见 ccs_sdk 的 ccs_video_mjpeg_stream
 * 实现），用完 close(fd)。与 ccs_http_request 的区别：不读响应体、不关连接。
 *
 * 参数: status_out 输出 HTTP 状态码（可为 NULL）。
 * 返回: >=0 socket fd；-1 失败（err 有原因）。
 */
int ccs_http_open_stream(const char *host, int port, int timeout_ms,
                         const char *path, int *status_out,
                         char *err, size_t errlen);

/* 从 fd 读一行（到 \n 为止，含 \n），返回读到的字节数；
 * 0 = 连接关闭，-1 = 错误/超时/行超长。供流式解析用。 */
int ccs_http_readline(int fd, char *buf, size_t cap);

/* 从 fd 精确读满 len 字节，返回实际读到的字节数（< len 即连接中断）。 */
size_t ccs_http_readn(int fd, void *buf, size_t len);

#ifdef __cplusplus
}
#endif

#endif /* CCS_HTTP_H */
