"""ccs_sdk.client — CCSClient 基类：HTTP 封装 + 统一异常 CCSApiError。

对应板子上的服务：ccs_webcam.py（http.server.ThreadingHTTPServer，
默认监听 8080 端口，HTTP/1.1 keep-alive）。管理页 http://<板子IP>:8080/
上的所有操作最终都落到这个模块封装的 HTTP 调用上；除 MJPEG 流
（/stream，video.py 里用 http.client 单独处理）外，SDK 全部端点都走这里。

板上端点的两类返回约定（与 ccs_webcam.py 的 do_GET/do_POST 一致）：
- 表单类端点（/edid_apply /edid_build /edid_preset /realign /set）：
  成功回 303 跳转到 /?msg=...，失败回 303 跳 /?err=...。
  → 本模块**不跟随重定向**（_NoRedirect），由 post_msg()/get_msg()
    解析 Location 里的 msg/err——如果跟随 303 跳到 /，拿到的只是一整页
    管理面板 HTML，操作结果反而丢了。
- 数据类端点（/stat /uart_recv /gpio_vals /nudge /brightness /sharpen
  /channel /uart_set /uart_send /gpio_set ajax 模式）：回 200 正文，
  非 200 即失败（403/500/503 正文里有原因文本）。

POST 一律 application/x-www-form-urlencoded（板上 _post_form 只认这种）。
"""

import json
import urllib.error
import urllib.parse
import urllib.request


class CCSApiError(Exception):
    """SDK 统一异常。

    以下情况都会抛这个异常（消息里尽量带上板子返回的文本，方便定位）：
    - 连接失败 / 超时（如板子断电、IP 不通、服务重启中的 15 秒窗口）；
    - HTTP 状态码非预期（数据类端点非 200，如 403/500/503）；
    - 303 端点 Location 里的 err 非空（板上校验失败，如 EDID 格式错误）；
    - 页面抓取类方法没匹配到预期结构（管理页 HTML 结构变了）。
    """


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随 301/302/303 重定向的 handler。

    为什么需要：urllib 默认会把 303 自动转成 GET 跳到 / 管理页，而板上
    表单类端点把操作结果放在 303 的 Location 查询串（?msg=/?err=）里。
    返回 None 让 urllib 把 303 当 HTTPError 抛出，上层就能拿到 Location。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CCSClient:
    """HTTP 客户端基类，所有功能模块（Video/Hid/Edid/...）都继承它。

    参数:
        host (str): 板子 IP 或主机名，如 '192.168.1.222'。
        port (int): 管理服务端口，默认 8080（ccs_webcam.py --port）。
        timeout (float): 单次 HTTP 请求超时秒数，默认 5.0。
            注意 MJPEG 流（Video.mjpeg_stream）不适用此值，见 video.py。

    副作用: 无（构造只是准备 opener，不发任何请求）。
    """

    def __init__(self, host, port=8080, timeout=5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        # 每个实例独立 opener：挂上了 _NoRedirect，303 不会被自动跟随
        self._opener = urllib.request.build_opener(_NoRedirect)

    @property
    def base(self):
        """服务根 URL，如 'http://192.168.1.222:8080'。"""
        return f'http://{self.host}:{self.port}'

    def _request(self, method, path, form=None):
        """最底层的请求入口，所有 get_*/post_* 都汇到这里。

        参数:
            method (str): 'GET' 或 'POST'。
            path (str): 以 / 开头的路径（含查询串），如 '/nudge?d=10'。
            form (dict|None): POST 表单字段；None 表示无请求体（GET）。

        返回:
            (status, headers, body): status 是 int 状态码，headers 是
            响应头（取 Location 用），body 是 bytes 响应体。

        抛出:
            CCSApiError: 仅连接层失败（URLError/超时/连接重置）时抛。
            任何 HTTP 状态码（含 4xx/5xx/303）都**不抛**，正常返回给上层
            判定——因为 303 是表单端点的成功路径，403/500 的正文要带回给
            调用者看原因。
        """
        url = self.base + path
        data = None
        if form is not None:
            # 板上 _post_form 用 parse_qs 解析，只认 urlencoded 表单
            data = urllib.parse.urlencode(form).encode('utf-8')
        req = urllib.request.Request(url, data=data, method=method)
        try:
            resp = self._opener.open(req, timeout=self.timeout)
            status, headers = resp.status, resp.headers
            body = resp.read()
            resp.close()
        except urllib.error.HTTPError as e:
            # HTTPError 既是异常也是响应对象：4xx/5xx 会走到这里，
            # 303 也因 _NoRedirect 走到这里。e.read() 取错误页正文。
            status, headers = e.code, e.headers
            try:
                body = e.read()
            except OSError:
                body = b''
        except (urllib.error.URLError, OSError) as e:
            # 超时是 socket.timeout（OSError 子类），连接拒绝是 URLError
            # 包装的 ConnectionRefusedError——合并成统一异常
            raise CCSApiError(f'连接 {url} 失败: {e}') from e
        return status, headers, body

    # ---- GET ----

    def get_bytes(self, path):
        """GET 并要求 200，返回原始字节。

        参数: path (str) 路径（含查询串）。
        返回: bytes 响应体（如 /frame.raw 的 UYVY 帧）。
        抛出: CCSApiError——非 200（消息带响应文本前 200 字符）或连接失败。
        """
        status, _, body = self._request('GET', path)
        if status != 200:
            raise CCSApiError(
                f'GET {path} → HTTP {status}: {body.decode("utf-8", "replace")[:200]}')
        return body

    def get_text(self, path):
        """GET 并要求 200，返回按 UTF-8 解码的文本（非法字节替换）。

        参数/返回/抛出同 get_bytes，返回值为 str。
        """
        return self.get_bytes(path).decode('utf-8', 'replace')

    def get_json(self, path):
        """GET 并要求 200，返回 json.loads 后的对象。

        用于 /uart_recv、/gpio_vals 这类 JSON 端点。
        参数/抛出同 get_bytes；返回类型取决于端点（dict）。
        """
        return json.loads(self.get_text(path))

    # ---- POST ----

    def post_text(self, path, form):
        """POST 表单，200 正文类端点专用（/hid /channel /uart_*
        /gpio_set ajax）。

        参数:
            path (str): 端点路径。
            form (dict): 表单字段（值会 urlencode）。

        返回: str——板上回的操作结果文本（如 '已发 5B'、'baud=115200'）。
        抛出: CCSApiError——非 200（如 /hid 的 503 上位机未连接、
            /gpio_set 的 403 不在白名单），消息带板上正文。
        """
        status, _, body = self._request('POST', path, form)
        text = body.decode('utf-8', 'replace')
        if status != 200:
            raise CCSApiError(f'POST {path} → HTTP {status}: {text}')
        return text

    def post_msg(self, path, form):
        """POST 表单，303 重定向类端点专用（/edid_apply /edid_build
        /edid_preset）。

        板上这类端点成功/失败都回 303，结果文本在 Location 的
        ?msg=/?err= 查询串里，本方法负责解析。

        参数: 同 post_text。
        返回: str——板上 msg（如 'EDID 已生效，已触发重对齐'）；
            若板上罕见地回了 200（不会发生于上述端点），退而返回正文。
        抛出: CCSApiError——Location 里 err 非空（参数被板上拒绝），
            或状态码既非 303 也非 200。
        """
        status, headers, body = self._request('POST', path, form)
        if status == 303:
            loc = headers.get('Location', '')
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(loc).query)
            if q.get('err', [''])[0]:
                raise CCSApiError(q['err'][0])
            return q.get('msg', [''])[0]
        text = body.decode('utf-8', 'replace')
        if status != 200:
            raise CCSApiError(f'POST {path} → HTTP {status}: {text}')
        return text

    def get_msg(self, path):
        """GET 版 303 端点（/realign、/set?half=N）：解析 Location 的
        msg/err。

        参数: path (str) 含查询串的路径。
        返回: str——板上 msg；/set 的 303 不带 msg 时返回空字符串。
        抛出: CCSApiError——err 非空或状态码异常。
        """
        status, headers, body = self._request('GET', path)
        if status == 303:
            loc = headers.get('Location', '')
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(loc).query)
            if q.get('err', [''])[0]:
                raise CCSApiError(q['err'][0])
            return q.get('msg', [''])[0]
        text = body.decode('utf-8', 'replace')
        if status != 200:
            raise CCSApiError(f'GET {path} → HTTP {status}: {text}')
        return text
