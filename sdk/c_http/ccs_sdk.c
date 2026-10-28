/* ccs_sdk.c — RK3568 CCS 采集板 HTTP API C SDK 实现。
 * 端点与语义对齐 Python 版 ccs_sdk（见各函数注释引用的对应模块）。
 */
#define _POSIX_C_SOURCE 200112L     /* nanosleep / strcasecmp 等 POSIX 声明 */

#include "ccs_sdk.h"
#include "ccs_http.h"

#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <ctype.h>
#include <time.h>
#include <unistd.h>

/* ---- 客户端句柄 ---- */

ccs_client_t *ccs_new(const char *host, int port, int timeout_ms)
{
    if (!host || !*host)
        return NULL;
    ccs_client_t *c = calloc(1, sizeof(*c));
    if (!c)
        return NULL;
    snprintf(c->host, sizeof(c->host), "%s", host);
    c->port = port > 0 ? port : 8080;
    c->timeout_ms = timeout_ms > 0 ? timeout_ms : 5000;
    return c;
}

void ccs_free(ccs_client_t *c)
{
    free(c);
}

const char *ccs_error(const ccs_client_t *c)
{
    return c ? c->err : "";
}

static int fail(ccs_client_t *c, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(c->err, sizeof(c->err), fmt, ap);
    va_end(ap);
    return -1;
}

/* 睡眠 sec 秒（供 HID 击键/右键长按的节拍用） */
static void sleep_sec(double sec)
{
    if (sec <= 0)
        return;
    struct timespec ts;
    ts.tv_sec = (time_t)sec;
    ts.tv_nsec = (long)((sec - (double)ts.tv_sec) * 1e9);
    nanosleep(&ts, NULL);
}

/* ---- HTTP 封装（对应 client.py 的 get_text/post_text/post_msg/get_msg） ---- */

/* 底层请求：连接层失败时把 ccs_http 的 err 搬进客户端 err */
static int req(ccs_client_t *c, const char *method, const char *path,
               const char *form_body, ccs_http_resp_t *resp)
{
    if (ccs_http_request(c->host, c->port, c->timeout_ms,
                         method, path, form_body, resp,
                         c->err, sizeof(c->err)) < 0)
        return -1;                      /* err 已填 */
    return 0;
}

/* GET 并要求 200（对应 client.py get_text） */
static int get_text(ccs_client_t *c, const char *path, ccs_http_resp_t *resp)
{
    if (req(c, "GET", path, NULL, resp) < 0)
        return -1;
    if (resp->status != 200) {
        int rc = fail(c, "GET %s → HTTP %d: %.200s", path, resp->status,
                      resp->body ? resp->body : "");
        ccs_http_resp_free(resp);
        return rc;
    }
    return 0;
}

/* 拼 urlencoded 表单体（pairs 是 key,value 交替、NULL 结尾的字符串列表）。
 * 返回 malloc 字符串，调用者 free。 */
static char *build_form(const char *first, ...)
{
    size_t cap = 256;
    char *buf = malloc(cap);
    if (!buf)
        return NULL;
    buf[0] = '\0';
    size_t len = 0;

    va_list ap;
    va_start(ap, first);
    for (const char *k = first; k; k = va_arg(ap, const char *)) {
        const char *v = va_arg(ap, const char *);
        if (!v)
            v = "";
        char *ek = ccs_urlencode(k);
        char *ev = ccs_urlencode(v);
        if (!ek || !ev) {
            free(ek);
            free(ev);
            free(buf);
            va_end(ap);
            return NULL;
        }
        size_t need = len + strlen(ek) + strlen(ev) + 2;
        if (need > cap) {
            while (cap < need)
                cap *= 2;
            char *nb = realloc(buf, cap);
            if (!nb) {
                free(ek);
                free(ev);
                free(buf);
                va_end(ap);
                return NULL;
            }
            buf = nb;
        }
        len += snprintf(buf + len, cap - len, "%s%s=%s",
                        len ? "&" : "", ek, ev);
        free(ek);
        free(ev);
    }
    va_end(ap);
    return buf;
}

/* POST 表单，200 正文类端点（对应 client.py post_text） */
static int post_text(ccs_client_t *c, const char *path, const char *form_body,
                     char *out, size_t len)
{
    ccs_http_resp_t resp;
    if (req(c, "POST", path, form_body, &resp) < 0)
        return -1;
    if (resp.status != 200) {
        int rc = fail(c, "POST %s → HTTP %d: %.200s", path, resp.status,
                      resp.body ? resp.body : "");
        ccs_http_resp_free(&resp);
        return rc;
    }
    if (out && len)
        snprintf(out, len, "%s", resp.body ? resp.body : "");
    ccs_http_resp_free(&resp);
    return 0;
}

/* URL 解码（查询串规则：'+'→空格，%XX→字节；与 parse_qs 一致）。
 * 原地解码 src 的副本到 out。 */
static void urldecode(const char *src, char *out, size_t len)
{
    size_t n = 0;
    while (*src && n + 1 < len) {
        if (*src == '+') {
            out[n++] = ' ';
            src++;
        } else if (*src == '%' && isxdigit((unsigned char)src[1]) &&
                   isxdigit((unsigned char)src[2])) {
            char hex[3] = {src[1], src[2], 0};
            out[n++] = (char)strtol(hex, NULL, 16);
            src += 3;
        } else {
            out[n++] = *src++;
        }
    }
    out[n] = '\0';
}

/* 从 303 Location（"/?msg=..." 或 "/?err=..."）解析 msg/err。
 * 返回 0 = 无 err（msg 拷到 msg_out，可为 NULL）；-1 = err 非空（进 c->err）。 */
static int parse_location_msg(ccs_client_t *c, const char *location,
                              char *msg_out, size_t msg_len)
{
    const char *q = strchr(location, '?');
    if (!q)
        return fail(c, "303 Location 无查询串: %s", location);
    q++;
    char msg[512] = "", err[512] = "";
    /* 按 & 拆 k=v（板上 msg/err 只有一个键，但按通用做法解析） */
    char tmp[1024];
    snprintf(tmp, sizeof(tmp), "%s", q);
    char *save = NULL;
    for (char *kv = strtok_r(tmp, "&", &save); kv;
         kv = strtok_r(NULL, "&", &save)) {
        char *eq = strchr(kv, '=');
        if (!eq)
            continue;
        *eq = '\0';
        if (strcmp(kv, "msg") == 0)
            urldecode(eq + 1, msg, sizeof(msg));
        else if (strcmp(kv, "err") == 0)
            urldecode(eq + 1, err, sizeof(err));
    }
    if (err[0])
        return fail(c, "%s", err);      /* 板上校验失败，err 原文带出 */
    if (msg_out && msg_len)
        snprintf(msg_out, msg_len, "%s", msg);
    return 0;
}

/* POST 表单，303 重定向类端点（对应 client.py post_msg：
 * /edid_apply /edid_build /edid_preset） */
static int post_msg(ccs_client_t *c, const char *path, const char *form_body,
                    char *msg, size_t len)
{
    ccs_http_resp_t resp;
    if (req(c, "POST", path, form_body, &resp) < 0)
        return -1;
    int rc;
    if (resp.status == 303) {
        rc = parse_location_msg(c, resp.location, msg, len);
    } else if (resp.status == 200) {
        /* 板上罕见回 200 时退而返回正文（同 Python 版） */
        if (msg && len)
            snprintf(msg, len, "%s", resp.body ? resp.body : "");
        rc = 0;
    } else {
        rc = fail(c, "POST %s → HTTP %d: %.200s", path, resp.status,
                  resp.body ? resp.body : "");
    }
    ccs_http_resp_free(&resp);
    return rc;
}

/* GET 版 303 端点（对应 client.py get_msg：/realign、/set?half=N） */
static int get_msg(ccs_client_t *c, const char *path, char *msg, size_t len)
{
    ccs_http_resp_t resp;
    if (req(c, "GET", path, NULL, &resp) < 0)
        return -1;
    int rc;
    if (resp.status == 303) {
        rc = parse_location_msg(c, resp.location, msg, len);
    } else if (resp.status == 200) {
        if (msg && len)
            snprintf(msg, len, "%s", resp.body ? resp.body : "");
        rc = 0;
    } else {
        rc = fail(c, "GET %s → HTTP %d: %.200s", path, resp.status,
                  resp.body ? resp.body : "");
    }
    ccs_http_resp_free(&resp);
    return rc;
}

/* ---- 极简 JSON 取值（不引第三方库：板上 JSON 都是扁平小对象） ---- */

/* 在 JSON 文本里找 "key" 并返回冒号之后的位置；找不到返回 NULL。 */
static const char *json_find(const char *json, const char *key)
{
    char pat[96];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = json;
    while ((p = strstr(p, pat)) != NULL) {
        const char *colon = p + strlen(pat);
        while (*colon == ' ' || *colon == '\t')
            colon++;
        if (*colon == ':') {
            colon++;
            while (*colon == ' ' || *colon == '\t')
                colon++;
            return colon;
        }
        p += strlen(pat);               /* 出现在字符串值里，继续往后找 */
    }
    return NULL;
}

/* 取 JSON 字符串值（处理 \" \\ \/ \n \r \t \b \f 与 \uXXXX→UTF-8 转义）。
 * 返回 0 成功；非字符串标量也按原样拷出（够本 SDK 用）。 */
static int json_get_str(const char *json, const char *key,
                        char *out, size_t len)
{
    const char *p = json_find(json, key);
    if (!p)
        return -1;
    size_t n = 0;
    if (*p != '"') {                    /* 数字/布尔等标量原样拷 */
        while (*p && *p != ',' && *p != '}' && *p != ']' && n + 1 < len)
            out[n++] = *p++;
        out[n] = '\0';
        return 0;
    }
    p++;
    while (*p && *p != '"' && n + 4 < len) {
        if (*p == '\\' && p[1]) {
            p++;
            switch (*p) {
            case 'n': out[n++] = '\n'; break;
            case 'r': out[n++] = '\r'; break;
            case 't': out[n++] = '\t'; break;
            case 'b': out[n++] = '\b'; break;
            case 'f': out[n++] = '\f'; break;
            case 'u': {
                /* \uXXXX → UTF-8（板上 JSON 来自 json.dumps，非 ASCII
                 * 默认转 \uXXXX；UART 数据含中文时会走到这里） */
                unsigned code = 0;
                for (int i = 0; i < 4 && p[1] && isxdigit((unsigned char)p[1]);
                     i++, p++)
                    code = code * 16 +
                           (unsigned)(isdigit((unsigned char)p[1])
                                      ? p[1] - '0'
                                      : tolower((unsigned char)p[1]) - 'a' + 10);
                if (code < 0x80) {
                    out[n++] = (char)code;
                } else if (code < 0x800) {
                    out[n++] = (char)(0xC0 | (code >> 6));
                    out[n++] = (char)(0x80 | (code & 0x3F));
                } else {
                    out[n++] = (char)(0xE0 | (code >> 12));
                    out[n++] = (char)(0x80 | ((code >> 6) & 0x3F));
                    out[n++] = (char)(0x80 | (code & 0x3F));
                }
                break;
            }
            default: out[n++] = *p; break;  /* \" \\ \/ 原样 */
            }
            p++;
        } else {
            out[n++] = *p++;
        }
    }
    out[n] = '\0';
    return 0;
}

/* 取 JSON 整数值。 */
static int json_get_long(const char *json, const char *key, long *out)
{
    const char *p = json_find(json, key);
    if (!p)
        return -1;
    *out = strtol(p, NULL, 10);
    return 0;
}

/* ---- HTML 工具（页面抓取类方法用，对齐 Python 版 html.unescape） ---- */

/* HTML 实体还原（覆盖板上 esc() 会用到的：&amp; &lt; &gt; &quot; &#39;
 * 及数字实体 &#NN; / &#xNN;）。 */
static void html_unescape(const char *src, char *out, size_t len)
{
    size_t n = 0;
    while (*src && n + 1 < len) {
        if (*src == '&') {
            if (strncmp(src, "&amp;", 5) == 0) {
                out[n++] = '&'; src += 5; continue;
            } else if (strncmp(src, "&lt;", 4) == 0) {
                out[n++] = '<'; src += 4; continue;
            } else if (strncmp(src, "&gt;", 4) == 0) {
                out[n++] = '>'; src += 4; continue;
            } else if (strncmp(src, "&quot;", 6) == 0) {
                out[n++] = '"'; src += 6; continue;
            } else if (strncmp(src, "&#39;", 5) == 0) {
                out[n++] = '\''; src += 5; continue;
            } else if (src[1] == '#') {
                char *end = NULL;
                long code = (src[2] == 'x' || src[2] == 'X')
                            ? strtol(src + 3, &end, 16)
                            : strtol(src + 2, &end, 10);
                if (end && *end == ';' && code > 0 && code < 128) {
                    out[n++] = (char)code;
                    src = end + 1;
                    continue;
                }
            }
        }
        out[n++] = *src++;
    }
    out[n] = '\0';
}

/* 去掉首尾空白（原地） */
static void strip(char *s)
{
    char *p = s;
    while (isspace((unsigned char)*p))
        p++;
    if (p != s)
        memmove(s, p, strlen(p) + 1);
    size_t n = strlen(s);
    while (n > 0 && isspace((unsigned char)s[n - 1]))
        s[--n] = '\0';
}

/* 从 *pp 处匹配字面量 tag（跳过前导空白），匹配则 *pp 推进到 tag 之后。
 * 返回 1 匹配，0 不匹配。用于手写表格行解析。 */
static int eat(const char **pp, const char *tag)
{
    const char *p = *pp;
    if (strncmp(p, tag, strlen(tag)) != 0)
        return 0;
    *pp = p + strlen(tag);
    return 1;
}

/* 从 *pp 处抓取 <td> 单元格内容（到 '<' 为止），html_unescape + strip。
 * 返回 1 成功，0 结构不符。 */
static int grab_td(const char **pp, char *out, size_t len)
{
    const char *p = *pp;
    const char *lt = strchr(p, '<');
    if (!lt)
        return 0;
    size_t n = (size_t)(lt - p);
    if (n >= len)
        n = len - 1;
    char raw[256];
    if (n >= sizeof(raw))
        n = sizeof(raw) - 1;
    memcpy(raw, p, n);
    raw[n] = '\0';
    html_unescape(raw, out, len);
    strip(out);
    *pp = lt;
    return 1;
}

/* ================= 视频采集 ================= */

int ccs_video_stat(ccs_client_t *c, char *out, size_t len)
{
    ccs_http_resp_t resp;
    if (get_text(c, "/stat", &resp) < 0)
        return -1;
    snprintf(out, len, "%s", resp.body ? resp.body : "");
    ccs_http_resp_free(&resp);
    return 0;
}

/* MJPEG 流的分帧格式（板上 ccs_webcam.py 写死）：
 *   --frame\r\n
 *   Content-Type: image/jpeg\r\n
 *   Content-Length: N\r\n
 *   \r\n
 *   <N 字节 JPEG>\r\n
 * 无限流、Connection: close、无整体 Content-Length——只能逐行解析分帧头、
 * 按每帧自带的长度读帧体；读到 EOF 或读不满即结束。 */
int ccs_video_mjpeg_stream(ccs_client_t *c, ccs_frame_cb cb, void *user,
                           int timeout_ms)
{
    /* 无信号期间服务端 5 秒空转重试不出帧，默认 5 秒太短会误超时
     * （与 Python 版 max(self.timeout, 10) 一致） */
    int to = timeout_ms > 0 ? timeout_ms
                            : (c->timeout_ms > 10000 ? c->timeout_ms : 10000);

    int status = 0;
    int fd = ccs_http_open_stream(c->host, c->port, to, "/stream",
                                  &status, c->err, sizeof(c->err));
    if (fd < 0)
        return -1;
    if (status != 200) {
        close(fd);
        return fail(c, "GET /stream → HTTP %d", status);
    }

    int rc = 0;
    char line[256];
    for (;;) {
        /* 解析一个分帧头 */
        long length = -1;
        for (;;) {
            int n = ccs_http_readline(fd, line, sizeof(line));
            if (n < 0) {                /* 读超时或连接被服务端关闭 */
                fail(c, "/stream 读取失败（超时或连接中断）");
                rc = -1;
                goto out;
            }
            /* 去行尾 \r\n */
            while (n > 0 && (line[n - 1] == '\r' || line[n - 1] == '\n'))
                line[--n] = '\0';
            if (n == 0) {
                if (length >= 0)
                    break;              /* 空行结束分帧头 */
                continue;               /* 边界行前的空行 */
            }
            if (strncmp(line, "--frame", 7) == 0)
                continue;               /* 边界行 */
            if (strncasecmp(line, "Content-Length:", 15) == 0)
                length = strtol(line + 15, NULL, 10);
        }
        if (length <= 0)
            continue;

        unsigned char *jpg = malloc((size_t)length);
        if (!jpg) {
            fail(c, "内存不足");
            rc = -1;
            goto out;
        }
        size_t got = ccs_http_readn(fd, jpg, (size_t)length);
        if (got != (size_t)length) {    /* 读不满 = 连接断了 */
            free(jpg);
            fail(c, "/stream 读取失败（帧体读不满，连接中断）");
            rc = -1;
            goto out;
        }
        int stop = cb(jpg, (size_t)length, user);
        free(jpg);
        if (stop)
            break;                      /* 回调要求停止，正常结束 */
    }
out:
    close(fd);
    return rc;
}

/* snapshot 用回调：拷下第一帧就停 */
struct snapshot_ctx {
    unsigned char *jpg;
    size_t len;
};

static int snapshot_cb(const unsigned char *jpg, size_t len, void *user)
{
    struct snapshot_ctx *ctx = user;
    ctx->jpg = malloc(len);
    if (!ctx->jpg)
        return 1;
    memcpy(ctx->jpg, jpg, len);
    ctx->len = len;
    return 1;                           /* 取到首帧即断开 */
}

int ccs_video_snapshot(ccs_client_t *c, unsigned char **jpg, size_t *jpg_len)
{
    struct snapshot_ctx ctx = {NULL, 0};
    if (ccs_video_mjpeg_stream(c, snapshot_cb, &ctx, 0) < 0) {
        free(ctx.jpg);
        return -1;
    }
    if (!ctx.jpg)
        return fail(c, "/stream 未产出任何帧");
    *jpg = ctx.jpg;
    *jpg_len = ctx.len;
    return 0;
}

int ccs_video_realign(ccs_client_t *c, char *msg, size_t len)
{
    return get_msg(c, "/realign", msg, len);
}

int ccs_video_nudge(ccs_client_t *c, int d, char *msg, size_t len)
{
    char path[64];
    snprintf(path, sizeof(path), "/nudge?d=%d", d);
    ccs_http_resp_t resp;
    if (get_text(c, path, &resp) < 0)
        return -1;
    if (msg && len)
        snprintf(msg, len, "%s", resp.body ? resp.body : "");
    ccs_http_resp_free(&resp);
    return 0;
}

int ccs_video_set_mode(ccs_client_t *c, const char *mode)
{
    int half;
    if (mode && (strcmp(mode, "clear") == 0 || strcmp(mode, "清晰") == 0))
        half = 0;
    else if (mode && (strcmp(mode, "smooth") == 0 || strcmp(mode, "流畅") == 0))
        half = 1;
    else
        return fail(c, "mode 须为 'clear'/'smooth'（或 '清晰'/'流畅'）");
    char path[32];
    snprintf(path, sizeof(path), "/set?half=%d", half);
    /* 板上 303 不带 msg，请求被受理即算成功（同 Python 版） */
    return get_msg(c, path, NULL, 0);
}

/* ================= 通道 ================= */

int ccs_channel_current(ccs_client_t *c, char *out, size_t len)
{
    /* /stat 文本最后一栏就是通道：'... | hw编码 | HDMI' */
    char stat[256];
    if (ccs_video_stat(c, stat, sizeof(stat)) < 0)
        return -1;
    char *bar = strrchr(stat, '|');
    const char *ch = bar ? bar + 1 : stat;
    snprintf(out, len, "%s", ch);
    strip(out);
    /* 转小写（'HDMI'→'hdmi'） */
    for (char *p = out; *p; p++)
        *p = (char)tolower((unsigned char)*p);
    return 0;
}

int ccs_channel_switch(ccs_client_t *c, const char *ch, char *msg, size_t len)
{
    if (!ch || (strcasecmp(ch, "hdmi") != 0 && strcasecmp(ch, "vga") != 0))
        return fail(c, "ch 须为 'hdmi' 或 'vga'");
    char lower[8];
    snprintf(lower, sizeof(lower), "%s", ch);
    for (char *p = lower; *p; p++)
        *p = (char)tolower((unsigned char)*p);
    char *form = build_form("ch", lower, NULL);
    if (!form)
        return fail(c, "内存不足");
    int rc = post_text(c, "/channel", form, msg, len);
    free(form);
    return rc;
}

/* ================= EDID ================= */

int ccs_edid_get_hex(ccs_client_t *c, char **hex_out)
{
    /* 读当前 EDID 没有独立 API：芯片 hex 文本嵌在管理页 HTML 的
     * <textarea name=edid> 里（板上 v4l2-ctl --get-edid 读出后 HTML 转义
     * 嵌入），抓页面解析（页面结构变了报错，不静默返回错数据） */
    ccs_http_resp_t resp;
    if (get_text(c, "/", &resp) < 0)
        return -1;
    const char *start = strstr(resp.body, "<textarea name=edid>");
    if (!start) {
        ccs_http_resp_free(&resp);
        return fail(c, "管理页里没找到 EDID 文本框（页面结构变了？）");
    }
    start += strlen("<textarea name=edid>");
    const char *end = strstr(start, "</textarea>");
    if (!end) {
        ccs_http_resp_free(&resp);
        return fail(c, "EDID 文本框未闭合（页面结构变了？）");
    }
    size_t n = (size_t)(end - start);
    char *raw = malloc(n + 1);
    char *txt = malloc(n + 2);
    if (!raw || !txt) {
        free(raw);
        free(txt);
        ccs_http_resp_free(&resp);
        return fail(c, "内存不足");
    }
    memcpy(raw, start, n);
    raw[n] = '\0';
    ccs_http_resp_free(&resp);
    html_unescape(raw, txt, n + 1);
    free(raw);
    strip(txt);
    strcat(txt, "\n");                  /* 与 Python 版一致：结尾带换行 */
    *hex_out = txt;
    return 0;
}

int ccs_edid_apply_hex(ccs_client_t *c, const char *hextext, int fix,
                       char *msg, size_t len)
{
    char *form;
    if (fix)
        /* 板上判定是 'fix' in form，值任意（同 Python 版 form['fix']='1'） */
        form = build_form("edid", hextext, "fix", "1", NULL);
    else
        form = build_form("edid", hextext, NULL);
    if (!form)
        return fail(c, "内存不足");
    int rc = post_msg(c, "/edid_apply", form, msg, len);
    free(form);
    return rc;
}

int ccs_edid_factory_reset(ccs_client_t *c, char *msg, size_t len)
{
    char *form = build_form("preset", "720p", NULL);
    if (!form)
        return fail(c, "内存不足");
    int rc = post_msg(c, "/edid_preset", form, msg, len);
    free(form);
    return rc;
}

/* ================= GPIO ================= */

int ccs_gpio_list(ccs_client_t *c, ccs_gpio_t **arr, int *count)
{
    /* 板上把 /sys/kernel/debug/gpio 解析后渲染成
     * <tr><td>num</td><td>name</td><td>dir</td><td id=gvN>val</td>
     * <td>consumer</td>...，按行抓回（`id=gvN` 锚点是 GPIO 表独有，
     * 能把网络表/系统表的行区分开——同 Python 版正则） */
    ccs_http_resp_t resp;
    if (get_text(c, "/", &resp) < 0)
        return -1;

    int cap = 32, n = 0;
    ccs_gpio_t *list = malloc(sizeof(*list) * (size_t)cap);
    if (!list) {
        ccs_http_resp_free(&resp);
        return fail(c, "内存不足");
    }

    const char *p = resp.body;
    while ((p = strstr(p, "<tr><td>")) != NULL) {
        const char *q = p + strlen("<tr><td>");
        if (!isdigit((unsigned char)*q)) {
            p = q;
            continue;                   /* GPIO 行首列是编号数字 */
        }
        char *endp = NULL;
        long num = strtol(q, &endp, 10);
        ccs_gpio_t g;
        memset(&g, 0, sizeof(g));
        g.num = (int)num;
        const char *r = endp;
        /* </td><td>name</td><td>dir</td><td id=gvN>val</td><td>consumer */
        if (!eat(&r, "</td><td>")) { p = q; continue; }
        if (!grab_td(&r, g.name, sizeof(g.name))) { p = q; continue; }
        if (!eat(&r, "</td><td>")) { p = q; continue; }
        if (!grab_td(&r, g.dir, sizeof(g.dir))) { p = q; continue; }
        /* val 列带 id=gvN 锚点（N 是编号数字，位数不定） */
        if (strncmp(r, "</td><td id=gv", 14) != 0) { p = q; continue; }
        r += 14;
        while (isdigit((unsigned char)*r))
            r++;
        if (!eat(&r, ">")) { p = q; continue; }
        if (!grab_td(&r, g.val, sizeof(g.val))) { p = q; continue; }
        if (!eat(&r, "</td><td>")) { p = q; continue; }
        if (!grab_td(&r, g.consumer, sizeof(g.consumer))) { p = q; continue; }

        if (n == cap) {
            cap *= 2;
            ccs_gpio_t *nl = realloc(list, sizeof(*list) * (size_t)cap);
            if (!nl)
                break;
            list = nl;
        }
        list[n++] = g;
        p = r;
    }
    ccs_http_resp_free(&resp);
    *arr = list;
    *count = n;
    return 0;
}

int ccs_gpio_set(ccs_client_t *c, int num, int val, char *msg, size_t len)
{
    char numstr[16];
    snprintf(numstr, sizeof(numstr), "%d", num);
    /* 必须带 ajax=1：不带时板上成功回 303 整页跳转（网页表单用法），
     * ajax 模式才回纯文本结果（同 Python 版） */
    char *form = build_form("num", numstr, "val", val ? "1" : "0",
                            "ajax", "1", NULL);
    if (!form)
        return fail(c, "内存不足");
    int rc = post_text(c, "/gpio_set", form, msg, len);
    free(form);
    return rc;
}

/* 外部输入脚编号 → 名称（与板上 GPIO_INPUTS / Python 版 INPUTS 一致） */
static const struct {
    int num;
    const char *name;
} GPIO_INPUTS[] = {
    {73, "LED_YELLOW_STATE"},
    {74, "LED_GREEN_STATE"},
    {85, "LED_BLUE_STATE"},
    {86, "LED_RED_STATE"},
};

int ccs_gpio_read_inputs(ccs_client_t *c, ccs_gpio_input_t out[4], int *count)
{
    /* /gpio_vals 返回 {"73": "1", "74": "0", ...}（键是编号字符串，
     * 值 '0'/'1'；板上某脚读失败会缺该键，这里同样缺，不补默认值） */
    ccs_http_resp_t resp;
    if (get_text(c, "/gpio_vals", &resp) < 0)
        return -1;
    int n = 0;
    for (size_t i = 0; i < sizeof(GPIO_INPUTS) / sizeof(GPIO_INPUTS[0]); i++) {
        char key[12];
        snprintf(key, sizeof(key), "%d", GPIO_INPUTS[i].num);
        char val[8];
        if (json_get_str(resp.body, key, val, sizeof(val)) == 0) {
            out[n].num = GPIO_INPUTS[i].num;
            snprintf(out[n].name, sizeof(out[n].name), "%s",
                     GPIO_INPUTS[i].name);
            out[n].level = (val[0] == '1') ? 1 : 0;
            n++;
        }
    }
    ccs_http_resp_free(&resp);
    *count = n;
    return 0;
}

/* ================= 串口 ================= */

int ccs_uart_send(ccs_client_t *c, const char *port, const char *data,
                  int hex, int crlf, char *msg, size_t len)
{
    char *form = build_form("port", port, "data", data,
                            "hex", hex ? "1" : "0",
                            "nl", crlf ? "1" : "0", NULL);
    if (!form)
        return fail(c, "内存不足");
    int rc = post_text(c, "/uart_send", form, msg, len);
    free(form);
    return rc;
}

int ccs_uart_send_bytes(ccs_client_t *c, const char *port,
                        const unsigned char *data, size_t data_len,
                        int crlf, char *msg, size_t len)
{
    /* 转 "xx xx " hex 字符串并强制 hex 模式（板上表单按 UTF-8 解码，
     * 任意二进制直接塞文本会被 'replace' 污染——同 Python 版 bytes 分支） */
    char *hexstr = malloc(data_len * 3 + 1);
    if (!hexstr)
        return fail(c, "内存不足");
    char *p = hexstr;
    for (size_t i = 0; i < data_len; i++)
        p += sprintf(p, "%02x ", data[i]);
    int rc = ccs_uart_send(c, port, hexstr, 1, crlf, msg, len);
    free(hexstr);
    return rc;
}

int ccs_uart_recv(ccs_client_t *c, const char *port, long since, int hex,
                  char **data, long *next, long *baud)
{
    char path[128];
    snprintf(path, sizeof(path), "/uart_recv?port=%s&since=%ld&hex=%d",
             port, since, hex ? 1 : 0);
    ccs_http_resp_t resp;
    if (get_text(c, path, &resp) < 0)
        return -1;
    /* 板上 JSON: {"data": str, "next": int, "baud": int, "err": str} */
    long dlen = strlen(resp.body) + 1;  /* 转义还原后只会更短 */
    char *buf = malloc((size_t)dlen);
    if (!buf) {
        ccs_http_resp_free(&resp);
        return fail(c, "内存不足");
    }
    if (json_get_str(resp.body, "data", buf, (size_t)dlen) < 0) {
        free(buf);
        ccs_http_resp_free(&resp);
        return fail(c, "/uart_recv 返回里没有 data 字段");
    }
    if (json_get_long(resp.body, "next", next) < 0) {
        free(buf);
        ccs_http_resp_free(&resp);
        return fail(c, "/uart_recv 返回里没有 next 字段");
    }
    if (baud && json_get_long(resp.body, "baud", baud) < 0)
        *baud = -1;
    ccs_http_resp_free(&resp);
    *data = buf;
    return 0;
}

int ccs_uart_set_baud(ccs_client_t *c, const char *port, int baud,
                      char *msg, size_t len)
{
    char baudstr[16];
    snprintf(baudstr, sizeof(baudstr), "%d", baud);
    char *form = build_form("port", port, "baud", baudstr, NULL);
    if (!form)
        return fail(c, "内存不足");
    int rc = post_text(c, "/uart_set", form, msg, len);
    free(form);
    return rc;
}

/* ================= 网络 ================= */

int ccs_net_ifaces(ccs_client_t *c, ccs_net_iface_t **arr, int *count)
{
    /* 网卡列表没有独立 JSON API：抓管理页"网络设置"表格（板上解析
     * ip -brief addr 渲染）。表头 <th>接口</th> 之后到 </table> 是数据行
     * （同 Python 版正则）。 */
    ccs_http_resp_t resp;
    if (get_text(c, "/", &resp) < 0)
        return -1;
    const char *th = strstr(resp.body, "<th>接口</th>");
    if (!th) {
        ccs_http_resp_free(&resp);
        return fail(c, "管理页里没找到网络表格（页面结构变了？）");
    }
    const char *p = strstr(th, "</tr>");
    const char *table_end = strstr(th, "</table>");
    if (!p || !table_end) {
        ccs_http_resp_free(&resp);
        return fail(c, "网络表格结构不完整（页面结构变了？）");
    }
    p += strlen("</tr>");

    int cap = 8, n = 0;
    ccs_net_iface_t *list = malloc(sizeof(*list) * (size_t)cap);
    if (!list) {
        ccs_http_resp_free(&resp);
        return fail(c, "内存不足");
    }

    while (p < table_end) {
        const char *row = strstr(p, "<tr><td>");
        if (!row || row >= table_end)
            break;
        const char *r = row + strlen("<tr><td>");
        ccs_net_iface_t it;
        memset(&it, 0, sizeof(it));
        /* <tr><td>iface</td><td>state</td><td>addr</td><td>mode</td></tr> */
        if (!grab_td(&r, it.iface, sizeof(it.iface))) break;
        if (!eat(&r, "</td><td>")) break;
        if (!grab_td(&r, it.state, sizeof(it.state))) break;
        if (!eat(&r, "</td><td>")) break;
        if (!grab_td(&r, it.addr, sizeof(it.addr))) break;
        if (!eat(&r, "</td><td>")) break;
        if (!grab_td(&r, it.mode, sizeof(it.mode))) break;
        /* 只收 ethX/wlanX/canX 前缀的接口（与板上渲染一致） */
        if (strncmp(it.iface, "eth", 3) != 0 &&
            strncmp(it.iface, "wlan", 4) != 0 &&
            strncmp(it.iface, "can", 3) != 0) {
            p = r;
            continue;
        }
        if (n == cap) {
            cap *= 2;
            ccs_net_iface_t *nl = realloc(list, sizeof(*list) * (size_t)cap);
            if (!nl)
                break;
            list = nl;
        }
        list[n++] = it;
        p = r;
    }
    ccs_http_resp_free(&resp);
    *arr = list;
    *count = n;
    return 0;
}

int ccs_net_apply(ccs_client_t *c, const char *iface, const char *mode,
                  const char *addr, const char *gw, const char *dns,
                  char *token, size_t token_len)
{
    char *form = build_form("iface", iface, "mode", mode,
                            "addr", addr ? addr : "",
                            "gw", gw ? gw : "",
                            "dns", dns ? dns : "", NULL);
    if (!form)
        return fail(c, "内存不足");
    ccs_http_resp_t resp;
    int rc = req(c, "POST", "/net_apply", form, &resp);
    free(form);
    if (rc < 0)
        return -1;
    if (resp.status == 303) {           /* 参数错误 → ?err= */
        rc = parse_location_msg(c, resp.location, NULL, 0);
        ccs_http_resp_free(&resp);
        return rc;
    }
    if (resp.status != 200) {
        rc = fail(c, "POST /net_apply → HTTP %d: %.200s", resp.status,
                  resp.body ? resp.body : "");
        ccs_http_resp_free(&resp);
        return rc;
    }
    /* 成功页是 HTML，token 只出现在确认链接里：net_confirm?token=XXXXXXXX */
    const char *t = strstr(resp.body, "net_confirm?token=");
    if (!t) {
        ccs_http_resp_free(&resp);
        return fail(c, "net_apply 成功页里没抓到确认 token（页面结构变了？）");
    }
    t += strlen("net_confirm?token=");
    size_t n = 0;
    while (isxdigit((unsigned char)t[n]) && n + 1 < token_len)
        n++;
    if (n == 0) {
        ccs_http_resp_free(&resp);
        return fail(c, "net_apply 确认 token 为空（页面结构变了？）");
    }
    memcpy(token, t, n);
    token[n] = '\0';
    ccs_http_resp_free(&resp);
    return 0;
}

int ccs_net_confirm(ccs_client_t *c, const char *token, int *kept)
{
    char path[128];
    snprintf(path, sizeof(path), "/net_confirm?token=%s", token);
    ccs_http_resp_t resp;
    if (get_text(c, path, &resp) < 0)
        return -1;
    /* 板上成功/失败都回 200 HTML，按页面文本判定（同 Python 版） */
    *kept = (resp.body && strstr(resp.body, "已保留")) ? 1 : 0;
    ccs_http_resp_free(&resp);
    return 0;
}

/* ================= 画质 ================= */

int ccs_image_set_brightness(ccs_client_t *c, int val, char *msg, size_t len)
{
    char path[48];
    snprintf(path, sizeof(path), "/brightness?val=%d", val);
    ccs_http_resp_t resp;
    if (get_text(c, path, &resp) < 0)
        return -1;
    if (msg && len)
        snprintf(msg, len, "%s", resp.body ? resp.body : "");
    ccs_http_resp_free(&resp);
    return 0;
}

int ccs_image_set_sharpen(ccs_client_t *c, int val, char *msg, size_t len)
{
    char path[48];
    snprintf(path, sizeof(path), "/sharpen?val=%d", val);
    ccs_http_resp_t resp;
    if (get_text(c, path, &resp) < 0)
        return -1;
    if (msg && len)
        snprintf(msg, len, "%s", resp.body ? resp.body : "");
    ccs_http_resp_free(&resp);
    return 0;
}

/* ================= HID 键鼠/触摸注入 ================= */

/* POST /hid d='<设备字母> <十进制字节> ...'（空格分隔）。
 * 返回 200 空 = 成功 / 503 = 上位机未连接或写入失败 / 400 = 参数错误。 */
static int hid_send(ccs_client_t *c, char which, const unsigned char *data,
                    int ndata)
{
    if (which != 'k' && which != 'm' && which != 't' && which != 'p')
        return fail(c, "设备须为 'k'/'m'/'t'/'p'");
    char d[128];
    int n = snprintf(d, sizeof(d), "%c", which);
    for (int i = 0; i < ndata; i++)
        n += snprintf(d + n, sizeof(d) - (size_t)n, " %u", data[i] & 0xff);
    char *form = build_form("d", d, NULL);
    if (!form)
        return fail(c, "内存不足");
    /* 成功正文为空，post_text 照样返回 0 */
    int rc = post_text(c, "/hid", form, NULL, 0);
    free(form);
    return rc;
}

/* 与网页 KVM_JS 的 KEYMAP 一致（USB HID usage ID） */
static const struct {
    const char *name;
    unsigned char code;
} KEYMAP[] = {
    {"KeyA", 4}, {"KeyB", 5}, {"KeyC", 6}, {"KeyD", 7}, {"KeyE", 8},
    {"KeyF", 9}, {"KeyG", 10}, {"KeyH", 11}, {"KeyI", 12}, {"KeyJ", 13},
    {"KeyK", 14}, {"KeyL", 15}, {"KeyM", 16}, {"KeyN", 17}, {"KeyO", 18},
    {"KeyP", 19}, {"KeyQ", 20}, {"KeyR", 21}, {"KeyS", 22}, {"KeyT", 23},
    {"KeyU", 24}, {"KeyV", 25}, {"KeyW", 26}, {"KeyX", 27}, {"KeyY", 28},
    {"KeyZ", 29},
    {"Digit1", 30}, {"Digit2", 31}, {"Digit3", 32}, {"Digit4", 33},
    {"Digit5", 34}, {"Digit6", 35}, {"Digit7", 36}, {"Digit8", 37},
    {"Digit9", 38}, {"Digit0", 39},
    {"Enter", 40}, {"Backspace", 42}, {"Tab", 43}, {"Space", 44},
    {"Minus", 45}, {"Equal", 46}, {"BracketLeft", 47}, {"BracketRight", 48},
    {"Backslash", 49}, {"Semicolon", 51}, {"Quote", 52}, {"Backquote", 53},
    {"Comma", 54}, {"Period", 55}, {"Slash", 56}, {"CapsLock", 57},
    {"F1", 58}, {"F2", 59}, {"F3", 60}, {"F4", 61}, {"F5", 62}, {"F6", 63},
    {"F7", 64}, {"F8", 65}, {"F9", 66}, {"F10", 67}, {"F11", 68},
    {"F12", 69},
    {"Insert", 73}, {"Home", 74}, {"PageUp", 75}, {"Delete", 76},
    {"End", 77}, {"PageDown", 78},
    {"ArrowRight", 79}, {"ArrowLeft", 80}, {"ArrowDown", 81}, {"ArrowUp", 82},
};

/* 键名 → usage ID；单字符自动映射 Key?/Digit?（同 Python 版 _lookup） */
static int key_lookup(const char *name)
{
    for (size_t i = 0; i < sizeof(KEYMAP) / sizeof(KEYMAP[0]); i++)
        if (strcmp(name, KEYMAP[i].name) == 0)
            return KEYMAP[i].code;
    if (strlen(name) == 1) {
        char up = (char)toupper((unsigned char)name[0]);
        char full[8];
        if (up >= 'A' && up <= 'Z')
            snprintf(full, sizeof(full), "Key%c", up);
        else if (up >= '0' && up <= '9')
            snprintf(full, sizeof(full), "Digit%c", up);
        else
            return -1;
        for (size_t i = 0; i < sizeof(KEYMAP) / sizeof(KEYMAP[0]); i++)
            if (strcmp(full, KEYMAP[i].name) == 0)
                return KEYMAP[i].code;
    }
    return -1;
}

int ccs_hid_send_keyboard(ccs_client_t *c, unsigned mods,
                          const unsigned char *keys, int nkeys)
{
    /* boot 协议 8 字节报告 [mods, 0, k1..k6]，不足 6 键补 0 */
    unsigned char report[8] = {0};
    report[0] = (unsigned char)(mods & 0xff);
    for (int i = 0; i < 6 && i < nkeys; i++)
        report[2 + i] = keys[i];
    return hid_send(c, 'k', report, 8);
}

int ccs_hid_release_all(ccs_client_t *c)
{
    return ccs_hid_send_keyboard(c, 0, NULL, 0);
}

int ccs_hid_key(ccs_client_t *c, const char *name, unsigned mods,
                double hold_sec)
{
    int code = key_lookup(name);
    if (code < 0)
        return fail(c, "未知键名: %s（用 KEYMAP 里的名字，如 KeyA/Enter/F1）",
                    name);
    unsigned char k = (unsigned char)code;
    if (ccs_hid_send_keyboard(c, mods, &k, 1) < 0)
        return -1;
    sleep_sec(hold_sec > 0 ? hold_sec : 0.05);
    return ccs_hid_release_all(c);
}

/* 归一化 0.0~1.0 → 0~32767（钳位到边界，与网页 xy() 一致） */
static int xy32767(double v)
{
    if (v < 0.0)
        v = 0.0;
    if (v > 1.0)
        v = 1.0;
    return (int)(v * 32767.0 + 0.5);
}

int ccs_hid_mouse_move(ccs_client_t *c, double x, double y, int down)
{
    /* 走 hidg3 笔 digitizer：bits = 4(in-range) | 1(tip，左键)——
     * 与网页 sendMouse 一致。in-range 位保证悬停时上位机显示光标。 */
    int xi = xy32767(x), yi = xy32767(y);
    unsigned char report[5];
    report[0] = (unsigned char)(4 | (down ? 1 : 0));
    report[1] = (unsigned char)(xi & 0xff);
    report[2] = (unsigned char)((xi >> 8) & 0xff);
    report[3] = (unsigned char)(yi & 0xff);
    report[4] = (unsigned char)((yi >> 8) & 0xff);
    return hid_send(c, 'p', report, 5);
}

int ccs_hid_mouse_down(ccs_client_t *c, double x, double y)
{
    if (x < 0)
        return hid_send(c, 'p', (unsigned char[]){5, 0, 0, 0, 0}, 5);
    return ccs_hid_mouse_move(c, x, y, 1);
}

int ccs_hid_mouse_up(ccs_client_t *c, double x, double y)
{
    if (x < 0)
        return hid_send(c, 'p', (unsigned char[]){4, 0, 0, 0, 0}, 5);
    return ccs_hid_mouse_move(c, x, y, 0);
}

int ccs_hid_click(ccs_client_t *c, double x, double y, double hold_sec)
{
    if (ccs_hid_mouse_move(c, x, y, 1) < 0)
        return -1;
    sleep_sec(hold_sec > 0 ? hold_sec : 0.05);
    return ccs_hid_mouse_move(c, x, y, 0);
}

int ccs_hid_right_click(ccs_client_t *c, double x, double y,
                        double interval, int count)
{
    /* hidg2 触摸流式长按（与网页 rightClick() 一致）：~60Hz 连发 count 次
     * tip|in-range(3) 报告，再发抬起(2)和全抬(0)。Windows 原生"按住=右键"
     * 手势；只发一次会被判联系点失效，长按不触发（实测）。 */
    if (interval <= 0)
        interval = 0.06;
    if (count <= 0)
        count = 22;
    int xi = xy32767(x), yi = xy32767(y);
    unsigned char press[6] = {
        3, 0,
        (unsigned char)(xi & 0xff), (unsigned char)((xi >> 8) & 0xff),
        (unsigned char)(yi & 0xff), (unsigned char)((yi >> 8) & 0xff)
    };
    for (int i = 0; i < count; i++) {
        if (hid_send(c, 't', press, 6) < 0)
            return -1;
        sleep_sec(interval);
    }
    unsigned char lift[6] = {2, 0, press[2], press[3], press[4], press[5]};
    if (hid_send(c, 't', lift, 6) < 0)
        return -1;
    unsigned char all_up[6] = {0};
    return hid_send(c, 't', all_up, 6);
}

int ccs_hid_touch(ccs_client_t *c, double x, double y, int down)
{
    /* 报告 bits = bit0 tip | bit1 in-range；抬起时坐标随意（网页抬起仍带
     * 当前坐标） */
    int xi = xy32767(x), yi = xy32767(y);
    unsigned char report[6];
    report[0] = (unsigned char)((down ? 1 : 0) | 2);
    report[1] = 0;
    report[2] = (unsigned char)(xi & 0xff);
    report[3] = (unsigned char)((xi >> 8) & 0xff);
    report[4] = (unsigned char)(yi & 0xff);
    report[5] = (unsigned char)((yi >> 8) & 0xff);
    return hid_send(c, 't', report, 6);
}

int ccs_hid_tap(ccs_client_t *c, double x, double y, double hold_sec)
{
    if (ccs_hid_touch(c, x, y, 1) < 0)
        return -1;
    sleep_sec(hold_sec > 0 ? hold_sec : 0.05);
    return ccs_hid_touch(c, x, y, 0);
}

int ccs_hid_mouse_abs(ccs_client_t *c, double x, double y,
                      unsigned btn, int wheel)
{
    /* hidg1 绝对坐标鼠标 [btn, xL, xH, yL, yH, wheel]（滚轮只能走这路） */
    int xi = xy32767(x), yi = xy32767(y);
    unsigned char report[6];
    report[0] = (unsigned char)(btn & 0xff);
    report[1] = (unsigned char)(xi & 0xff);
    report[2] = (unsigned char)((xi >> 8) & 0xff);
    report[3] = (unsigned char)(yi & 0xff);
    report[4] = (unsigned char)((yi >> 8) & 0xff);
    report[5] = (unsigned char)(wheel & 0xff);  /* 有符号增量发补码字节 */
    return hid_send(c, 'm', report, 6);
}
