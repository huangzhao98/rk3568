/* ccs_http.c — 最小 HTTP/1.1 客户端实现（纯 BSD socket，无 libcurl）。
 * 设计取舍见 ccs_http.h 头部注释。
 */
#define _POSIX_C_SOURCE 200112L /* getaddrinfo/strtok_r/setsockopt 等声明
                                 * （-std=c99 下 glibc 默认隐藏 POSIX） */
#include "ccs_http.h"

#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>
#include <errno.h>
#include <ctype.h>
#include <fcntl.h>
#include <netdb.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <sys/select.h>
#include <netinet/in.h>
#include <arpa/inet.h>

/* ---- 内部工具 ---- */

static void set_err(char *err, size_t errlen, const char *fmt, ...)
{
    if (!err || !errlen)
        return;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(err, errlen, fmt, ap);
    va_end(ap);
}

/* 带超时的 connect：先设非阻塞，connect 后 select 等可写，再查 SO_ERROR。 */
static int connect_timeout(int fd, const struct sockaddr *sa, socklen_t salen,
                           int timeout_ms)
{
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0)
        return -1;

    int rc = connect(fd, sa, salen);
    if (rc == 0)
        goto done;                      /* 本地地址可能瞬间完成 */
    if (errno != EINPROGRESS)
        return -1;

    fd_set wfds;
    FD_ZERO(&wfds);
    FD_SET(fd, &wfds);
    struct timeval tv;
    tv.tv_sec = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;
    rc = select(fd + 1, NULL, &wfds, NULL, &tv);
    if (rc <= 0)
        return -1;                      /* 0=超时 <0=错误 */

    int soerr = 0;
    socklen_t slen = sizeof(soerr);
    if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &slen) < 0 || soerr) {
        errno = soerr;
        return -1;
    }
done:
    /* 恢复阻塞模式，读写超时由 SO_RCVTIMEO/SO_SNDTIMEO 控制 */
    fcntl(fd, F_SETFL, flags);
    return 0;
}

/* 建立 TCP 连接（getaddrinfo 支持主机名和 IP 字面量），返回 fd 或 -1。 */
static int tcp_connect(const char *host, int port, int timeout_ms,
                       char *err, size_t errlen)
{
    char portstr[16];
    snprintf(portstr, sizeof(portstr), "%d", port);

    struct addrinfo hints;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_UNSPEC;        /* IPv4/IPv6 都接受 */
    hints.ai_socktype = SOCK_STREAM;

    struct addrinfo *res = NULL;
    int grc = getaddrinfo(host, portstr, &hints, &res);
    if (grc != 0) {
        set_err(err, errlen, "DNS 解析 %s 失败: %s", host, gai_strerror(grc));
        return -1;
    }

    int fd = -1;
    for (struct addrinfo *ai = res; ai; ai = ai->ai_next) {
        fd = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
        if (fd < 0)
            continue;
        if (connect_timeout(fd, ai->ai_addr, ai->ai_addrlen, timeout_ms) == 0)
            break;                      /* 成功 */
        close(fd);
        fd = -1;
    }
    freeaddrinfo(res);
    if (fd < 0) {
        set_err(err, errlen, "连接 %s:%d 失败: %s", host, port,
                strerror(errno));
        return -1;
    }

    /* 读写超时（阻塞模式下的兜底，流式读也靠它实现"每次读超时"语义） */
    struct timeval tv;
    tv.tv_sec = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    return fd;
}

/* 把请求头发送出去（Content-Length 由调用者算好放进 body 长度）。 */
static int send_request(int fd, const char *host, int port,
                        const char *method, const char *path,
                        const char *form_body)
{
    char head[2048];
    int n;
    if (form_body) {
        /* 板上 _post_form 只认 urlencoded 表单 */
        n = snprintf(head, sizeof(head),
                     "%s %s HTTP/1.1\r\n"
                     "Host: %s:%d\r\n"
                     "User-Agent: ccs-http-c/1.0\r\n"
                     "Connection: close\r\n"   /* 简化：一请求一连接，且让
                                               * 无 Content-Length 的响应以
                                               * 关连接收尾（读循环好判定） */
                     "Content-Type: application/x-www-form-urlencoded\r\n"
                     "Content-Length: %zu\r\n"
                     "\r\n",
                     method, path, host, port, strlen(form_body));
    } else {
        n = snprintf(head, sizeof(head),
                     "%s %s HTTP/1.1\r\n"
                     "Host: %s:%d\r\n"
                     "User-Agent: ccs-http-c/1.0\r\n"
                     "Connection: close\r\n"
                     "\r\n",
                     method, path, host, port);
    }
    if (n <= 0 || (size_t)n >= sizeof(head))
        return -1;

    /* 头部和表单体分两次写即可（小包，无需聚集写） */
    size_t off = 0;
    while (off < (size_t)n) {
        ssize_t w = send(fd, head + off, (size_t)n - off, 0);
        if (w <= 0)
            return -1;
        off += (size_t)w;
    }
    if (form_body) {
        size_t blen = strlen(form_body);
        off = 0;
        while (off < blen) {
            ssize_t w = send(fd, form_body + off, blen - off, 0);
            if (w <= 0)
                return -1;
            off += (size_t)w;
        }
    }
    return 0;
}

/* 读响应头（到 \r\n\r\n 为止），解析状态码 / Content-Length / Location。
 * 把头之后可能已经随头一起到达的正文前几个字节拷到 leftover（动态缓冲）。 */
static int read_headers(int fd, int *status, long *content_length,
                        char *location, size_t loc_cap,
                        char **leftover, size_t *leftover_len,
                        char *err, size_t errlen)
{
    /* 逐字节读头（头部很小，逐字节足够；避免缓冲超读吃掉正文） */
    size_t cap = 8192, len = 0;
    char *hbuf = malloc(cap);
    if (!hbuf) {
        set_err(err, errlen, "内存不足");
        return -1;
    }
    *leftover = NULL;
    *leftover_len = 0;

    /* 找 \r\n\r\n 终止序列 */
    int found = 0;
    while (len < cap) {
        char c;
        ssize_t r = recv(fd, &c, 1, 0);
        if (r <= 0) {
            free(hbuf);
            set_err(err, errlen, "读响应头失败: %s",
                    r == 0 ? "连接被关闭" : strerror(errno));
            return -1;
        }
        hbuf[len++] = c;
        if (len >= 4 && memcmp(hbuf + len - 4, "\r\n\r\n", 4) == 0) {
            found = 1;
            break;
        }
    }
    if (!found) {
        free(hbuf);
        set_err(err, errlen, "响应头超长（>%zu 字节）", cap);
        return -1;
    }
    hbuf[len] = '\0';

    /* 状态行：HTTP/1.1 200 OK */
    if (sscanf(hbuf, "HTTP/%*s %d", status) != 1) {
        free(hbuf);
        set_err(err, errlen, "响应状态行无法解析");
        return -1;
    }

    *content_length = -1;
    if (location && loc_cap)
        location[0] = '\0';

    /* 逐行解析头字段（大小写不敏感） */
    char *save = NULL;
    for (char *line = strtok_r(hbuf, "\r\n", &save); line;
         line = strtok_r(NULL, "\r\n", &save)) {
        if (strncasecmp(line, "Content-Length:", 15) == 0) {
            *content_length = strtol(line + 15, NULL, 10);
        } else if (location && loc_cap &&
                   strncasecmp(line, "Location:", 9) == 0) {
            const char *v = line + 9;
            while (*v == ' ')
                v++;
            snprintf(location, loc_cap, "%s", v);
        }
    }
    free(hbuf);
    return 0;
}

/* ---- 公开接口 ---- */

int ccs_http_request(const char *host, int port, int timeout_ms,
                     const char *method, const char *path,
                     const char *form_body,
                     ccs_http_resp_t *resp, char *err, size_t errlen)
{
    memset(resp, 0, sizeof(*resp));

    int fd = tcp_connect(host, port, timeout_ms, err, errlen);
    if (fd < 0)
        return -1;

    int rc = -1;
    if (send_request(fd, host, port, method, path, form_body) < 0) {
        set_err(err, errlen, "发送请求失败: %s", strerror(errno));
        goto out;
    }

    long content_length = -1;
    char *leftover = NULL;
    size_t leftover_len = 0;
    if (read_headers(fd, &resp->status, &content_length,
                     resp->location, sizeof(resp->location),
                     &leftover, &leftover_len, err, errlen) < 0)
        goto out;
    free(leftover);     /* 逐字节读头不会产生 leftover，防御性释放 */

    /* 响应体动态缓冲：有 Content-Length 预分配，没有则边读边扩。
     * （板上普通端点都带 Content-Length；无长度的是 /stream，走
     *  ccs_http_open_stream，不会到这里。读到 EOF 的分支只是兜底。） */
    size_t cap = content_length > 0 ? (size_t)content_length + 1 : 4096;
    char *body = malloc(cap);
    if (!body) {
        set_err(err, errlen, "内存不足");
        goto out;
    }
    size_t len = 0;

    for (;;) {
        if (content_length >= 0 && len >= (size_t)content_length)
            break;                      /* 已读满声明长度 */
        if (len + 4096 + 1 > cap) {
            cap *= 2;
            char *nb = realloc(body, cap);
            if (!nb) {
                free(body);
                set_err(err, errlen, "内存不足");
                goto out;
            }
            body = nb;
        }
        ssize_t r = recv(fd, body + len, cap - len - 1, 0);
        if (r < 0) {
            free(body);
            set_err(err, errlen, "读响应体失败: %s", strerror(errno));
            goto out;
        }
        if (r == 0)
            break;                      /* EOF（无 Content-Length 时的正常结束） */
        len += (size_t)r;
    }
    body[len] = '\0';                   /* 文本场景方便当 C 字符串用 */
    resp->body = body;
    resp->body_len = len;
    rc = 0;

out:
    close(fd);
    return rc;
}

void ccs_http_resp_free(ccs_http_resp_t *resp)
{
    if (!resp)
        return;
    free(resp->body);
    memset(resp, 0, sizeof(*resp));
}

char *ccs_urlencode(const char *s)
{
    /* 保留字符集与 Python quote 默认一致：字母数字 + '_.-~' 原样，
     * 其余一律 %XX（含空格——parse_qs 对 %20 和 + 等价处理） */
    size_t len = strlen(s);
    char *out = malloc(len * 3 + 1);
    if (!out)
        return NULL;
    char *p = out;
    for (const unsigned char *c = (const unsigned char *)s; *c; c++) {
        if (isalnum(*c) || *c == '_' || *c == '.' || *c == '-' || *c == '~') {
            *p++ = (char)*c;
        } else {
            snprintf(p, 4, "%%%02X", *c);
            p += 3;
        }
    }
    *p = '\0';
    return out;
}

int ccs_http_open_stream(const char *host, int port, int timeout_ms,
                         const char *path, int *status_out,
                         char *err, size_t errlen)
{
    int fd = tcp_connect(host, port, timeout_ms, err, errlen);
    if (fd < 0)
        return -1;
    if (send_request(fd, host, port, "GET", path, NULL) < 0) {
        set_err(err, errlen, "发送请求失败: %s", strerror(errno));
        close(fd);
        return -1;
    }
    int status = 0;
    long cl = -1;
    char *lo = NULL;
    size_t lo_len = 0;
    if (read_headers(fd, &status, &cl, NULL, 0, &lo, &lo_len, err, errlen) < 0) {
        close(fd);
        return -1;
    }
    free(lo);   /* 逐字节读头不会有 leftover，这里只是防御性释放 */
    if (status_out)
        *status_out = status;
    return fd;
}

int ccs_http_readline(int fd, char *buf, size_t cap)
{
    size_t n = 0;
    while (n + 1 < cap) {
        char c;
        ssize_t r = recv(fd, &c, 1, 0);
        if (r <= 0)
            return n > 0 ? (int)n : -1; /* 读超时/断连；已读到部分先返回 */
        buf[n++] = c;
        if (c == '\n')
            break;
    }
    buf[n] = '\0';
    return (int)n;
}

size_t ccs_http_readn(int fd, void *buf, size_t len)
{
    size_t got = 0;
    while (got < len) {
        ssize_t r = recv(fd, (char *)buf + got, len - got, 0);
        if (r <= 0)
            break;                      /* 超时或断连 */
        got += (size_t)r;
    }
    return got;
}
