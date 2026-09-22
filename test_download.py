"""build.py 下载层的回归测试。

覆盖:
  1. requests 分支走 resp.raw.stream(CHUNK, decode_content=False), 不做二次解压
  2. deflate 的 zlib / 裸 deflate 双模式嗅探
  3. decompressor.eof: 压缩流被截断必须报错并重试, 不能落盘半截文件
  4. expected_size 校验对词库下载同样生效 (清单 size / HEAD 探测)

用法: python test_download.py
"""

import gzip
import http.server
import shutil
import socketserver
import sys
import threading
import types
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402

PAYLOAD = b"# dict\n" + (b"line\n" * 20000)
OUT = Path(".build/decoder_test")
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def raw_deflate(data: bytes) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


def decoder_roundtrip(encoding, body, truncate_at=None):
    dec = build._Decoder(encoding)
    data = body if truncate_at is None else body[:truncate_at]
    out = b""
    for i in range(0, len(data), 977):  # 故意用奇怪的分块
        out += dec.feed(data[i:i + 977])
    out += dec.finish()
    return out


def test_decoders():
    print("--- 解码器 ---")
    check("gzip 解码", decoder_roundtrip("gzip", gzip.compress(PAYLOAD)) == PAYLOAD)
    check("gzip 多成员", decoder_roundtrip(
        "gzip", gzip.compress(PAYLOAD[:5000]) + gzip.compress(PAYLOAD[5000:])) == PAYLOAD)
    check("zlib 包装的 deflate", decoder_roundtrip("deflate", zlib.compress(PAYLOAD)) == PAYLOAD)
    check("裸 deflate", decoder_roundtrip("deflate", raw_deflate(PAYLOAD)) == PAYLOAD)
    check("identity 直传", decoder_roundtrip("", PAYLOAD) == PAYLOAD)

    # 逐字节喂入: 首块只有 1 字节, zlib 头嗅探会误判, 必须靠中途回退兜住
    dec = build._Decoder("deflate")
    body = raw_deflate(PAYLOAD)
    out = b"".join(dec.feed(bytes([b])) for b in body) + dec.finish()
    check("裸 deflate 逐字节喂入", out == PAYLOAD, f"{len(out)} 字节")

    dec = build._Decoder("deflate")
    body = zlib.compress(PAYLOAD)
    out = b"".join(dec.feed(bytes([b])) for b in body) + dec.finish()
    check("zlib deflate 逐字节喂入", out == PAYLOAD, f"{len(out)} 字节")

    # 截断: gzip 去掉末尾 8 字节(CRC/ISIZE)
    try:
        got = decoder_roundtrip("gzip", gzip.compress(PAYLOAD), truncate_at=-8)
        check("gzip 截断被识别", False, f"居然成功了 ({len(got)} 字节)")
    except OSError as exc:
        check("gzip 截断被识别", "截断" in str(exc), str(exc))

    # 截断: zlib 去掉尾部
    try:
        got = decoder_roundtrip("deflate", zlib.compress(PAYLOAD), truncate_at=-4)
        check("zlib 截断被识别", False, f"居然成功了 ({len(got)} 字节)")
    except OSError as exc:
        check("zlib 截断被识别", "截断" in str(exc), str(exc))

    # 截断: 裸 deflate 去掉尾部
    try:
        got = decoder_roundtrip("deflate", raw_deflate(PAYLOAD), truncate_at=-4)
        check("裸 deflate 截断被识别", False, f"居然成功了 ({len(got)} 字节)")
    except OSError as exc:
        check("裸 deflate 截断被识别", "截断" in str(exc), str(exc))

    # 混杂垃圾: 必须报错而不是静默产出垃圾
    try:
        decoder_roundtrip("gzip", b"not gzip at all" * 100)
        check("非 gzip 数据被拒绝", False)
    except OSError as exc:
        check("非 gzip 数据被拒绝", True, str(exc)[:60])


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "plain"
    truncate_body = False
    fail_times = 0
    head_calls = 0
    get_calls = 0

    def _body(self):
        if self.mode == "gzip":
            return gzip.compress(PAYLOAD), "gzip"
        if self.mode == "deflate":
            return zlib.compress(PAYLOAD), "deflate"
        if self.mode == "deflate-raw":
            return raw_deflate(PAYLOAD), "deflate"
        return PAYLOAD, None

    def do_HEAD(self):  # noqa: N802
        type(self).head_calls += 1
        body, enc = self._body()
        self.send_response(200)
        if enc:
            self.send_header("Content-Encoding", enc)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def do_GET(self):  # noqa: N802
        type(self).get_calls += 1
        if type(self).fail_times > 0:
            type(self).fail_times -= 1
            body, enc = self._body()
            self.send_response(200)
            if enc:
                self.send_header("Content-Encoding", enc)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[: max(1, len(body) // 3)])  # 截断
            self.wfile.flush()
            self.close_connection = True
            return
        body, enc = self._body()
        if type(self).truncate_body:
            body = body[: max(1, len(body) // 3)]
        self.send_response(200)
        if enc:
            self.send_header("Content-Encoding", enc)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve():
    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/dict.yaml"


def test_downloads():
    print("--- 下载 (urllib 后端) ---")
    for mode in ("plain", "gzip", "deflate", "deflate-raw"):
        Handler.mode = mode
        Handler.truncate_body = False
        Handler.fail_times = 0
        srv, url = serve()
        try:
            dest = OUT / f"dl_{mode}.yaml"
            dest.unlink(missing_ok=True)
            err = None
            try:
                build.http_download(url, dest, mode, expected_size=len(PAYLOAD))
            except SystemExit as exc:
                err = exc
            data = dest.read_bytes() if dest.is_file() else b""
            check(f"{mode} 下载内容正确", err is None and data == PAYLOAD,
                  f"bytes={len(data)} err={err}")
        finally:
            srv.shutdown()
            srv.server_close()

    # 截断响应 -> 必须重试, 且最终不能落盘半截文件
    Handler.mode = "gzip"
    Handler.truncate_body = True
    Handler.fail_times = 0
    srv, url = serve()
    try:
        dest = OUT / "truncated.yaml"
        dest.unlink(missing_ok=True)
        try:
            build.http_download(url, dest, "截断流")
            check("持续截断的流最终失败", False, "居然成功了")
        except SystemExit:
            check("持续截断的流最终失败", not dest.exists(), "未落盘残留文件")
    finally:
        srv.shutdown()
        srv.server_close()

    # 前两次截断, 第三次完整 -> 重试成功
    Handler.mode = "plain"
    Handler.truncate_body = False
    Handler.fail_times = 2
    srv, url = serve()
    try:
        dest = OUT / "retry.yaml"
        dest.unlink(missing_ok=True)
        ok = True
        try:
            build.http_download(url, dest, "重试", expected_size=len(PAYLOAD))
        except SystemExit:
            ok = False
        check("截断后可重试成功", ok and dest.read_bytes() == PAYLOAD)
    finally:
        srv.shutdown()
        srv.server_close()

    # expected_size 不符 -> 拒绝
    Handler.mode = "plain"
    Handler.fail_times = 0
    srv, url = serve()
    try:
        dest = OUT / "badsize.yaml"
        dest.unlink(missing_ok=True)
        try:
            build.http_download(url, dest, "大小不符", expected_size=len(PAYLOAD) + 1)
            check("expected_size 不符被拒绝", False)
        except SystemExit:
            check("expected_size 不符被拒绝", not dest.exists())
    finally:
        srv.shutdown()
        srv.server_close()

    # 词库清单: 声明的 size / HEAD 探测都要能传进 expected_size
    print("--- 词库清单 expected_size ---")
    Handler.mode = "plain"
    Handler.head_calls = 0
    srv, url = serve()
    try:
        manifest = OUT / "download.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            '[{"name": "declared", "url": "%s", "size": %d},'
            ' {"name": "probed", "url": "%s"}]' % (url, len(PAYLOAD), url),
            encoding="utf-8",
        )
        dicts = build.download_custom_dicts(OUT / "dicts", manifest)
        sizes = [d["declared_size"] for d in dicts]
        check("清单 size 生效", sizes[0] == len(PAYLOAD), f"{sizes[0]}")
        check("HEAD 探测生效", sizes[1] == len(PAYLOAD), f"{sizes[1]}")
        check("HEAD 被调用", Handler.head_calls >= 1, f"{Handler.head_calls} 次")
        check("落盘内容正确",
              (OUT / "dicts" / "dict.yaml").read_bytes() == PAYLOAD)
    finally:
        srv.shutdown()
        srv.server_close()


class RawStub:
    """复刻 urllib3 的原始流: decode_content=False 时必须原样吐字节。

    每次 stream() 都重新产出一份数据, 相当于真实服务器每次请求都重发响应体。
    """

    def __init__(self, body):
        self._body = body
        self.decode_content = False
        self.calls = []

    def stream(self, size, decode_content=True):
        self.calls.append((size, decode_content))
        if decode_content:
            raise AssertionError("build.py 不应要求 urllib3 解压")
        for i in range(0, len(self._body), 511):
            yield self._body[i:i + 511]


class StubResponse:
    def __init__(self, body, raw, encoding):
        self.status_code = 200
        self.reason = "OK"
        self.headers = {"Content-Length": str(len(body))}
        if encoding:
            self.headers["Content-Encoding"] = encoding
        self.raw = raw

    def raise_for_status(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def test_requests_stream_api():
    print("--- requests raw.stream 用法 ---")
    for mode, body in (("gzip", gzip.compress(PAYLOAD)),
                       ("deflate", zlib.compress(PAYLOAD)),
                       ("deflate-raw", raw_deflate(PAYLOAD)),
                       ("plain", PAYLOAD)):
        raw = RawStub(body)
        seen = []

        class Session:
            headers = {}

            def get(self, url, stream=False, timeout=None):
                seen.append(dict(self.headers))
                # 真实服务端只用 "deflate"; 裸 deflate 也是这个声明
                enc = {"plain": "", "gzip": "gzip"}.get(mode, "deflate")
                return StubResponse(body, raw, enc)

        mod = types.ModuleType("requests")
        mod.Session = Session
        sys.modules["requests"] = mod
        sys.modules.pop("build", None)
        import build as b2

        assert b2.HTTP_BACKEND == "requests", b2.HTTP_BACKEND
        dest = OUT / f"req_{mode}.yaml"
        dest.unlink(missing_ok=True)
        err = None
        try:
            b2.http_download("https://example.invalid/x", dest, mode,
                             expected_size=len(PAYLOAD))
        except SystemExit as exc:
            err = exc
        data = dest.read_bytes() if dest.is_file() else b""
        ok = (err is None and data == PAYLOAD
              and raw.calls and all(dc is False for _, dc in raw.calls)
              and seen[0].get("Accept-Encoding") == "identity")
        check(f"requests/{mode} 原始流+自行解压", ok,
              f"stream_calls={raw.calls} bytes={len(data)} err={err}")
        del sys.modules["requests"]
        sys.modules.pop("build", None)


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    test_decoders()
    test_downloads()
    test_requests_stream_api()
    print()
    failed = RESULTS.count(False)
    print(f"{len(RESULTS) - failed}/{len(RESULTS)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
