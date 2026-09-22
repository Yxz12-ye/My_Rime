#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建 Trime 配置产物。

把上游「万象拼音 pro」release 与本地 patch / skin 叠加，打包成一个可直接放进
Rime 用户目录的 zip。

方案 1（万象虎）: release rime-wanxiang-tiger-fuzhu.zip  +  zhhwux/wxzhh 仓库（覆盖）
方案 2（小鹤双拼）: release rime-wanxiang-flypy-fuzhu.zip（不覆盖仓库内容）

流程:
    1. 询问方案
    2. 下载 release, 解压到构建目录; 万象虎额外 clone 仓库并覆盖上去
    3. 复制 patch/wxh（或 patch/flypy）到构建目录
    4. 复制 patch/custom_dict 到构建目录, 并按 download.json 下载词库
    5. 复制 skin 到构建目录
    6. 打包成 zip, 版本信息写入 .version/

用法:
    python build.py                  # 交互式选择方案
    python build.py --scheme wxh     # 万象虎
    python build.py --scheme flypy   # 小鹤双拼
    python build.py --scheme wxh -y  # 全部使用默认值, 不提问
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- 配置

# 上游 release 仓库与「万象虎」配套仓库
RELEASE_REPO = "amzxyz/rime-wanxiang"
WXH_REPO = "zhhwux/wxzhh"

# 下载整个仓库时是否保留 .git（False = 复制时排除, 产物更干净）
KEEP_GIT_DIR = False

# 网络：超时（秒）、重试次数、分块大小
TIMEOUT = 120
RETRIES = 3
CHUNK = 1 << 20

# 固定请求「不使用传输压缩」。不能依赖 HTTP 库的自动解压:
#   requests/urllib3 会加 Accept-Encoding: gzip 并透明解压, 但 Content-Length
#   仍是压缩后的大小, 于是校验报「期望 22 KB, 实际 59 KB」;
#   urllib 不会自动解压, 于是把 gzip 流原样写进文件, 产物直接损坏。
# 这里统一要求 identity, 服务端/代理若仍然压缩则按 Content-Encoding 自行解压。
ACCEPT_ENCODING = "identity"

SCHEMES = {
    "wxh": {
        "key": "wxh",
        "label": "万象虎",
        "asset": "rime-wanxiang-tiger-fuzhu.zip",
        "patch_dir": "wxh",
        "zip_name": "Trime-wxh.zip",
        "use_wxh_repo": True,
    },
    "flypy": {
        "key": "flypy",
        "label": "小鹤双拼",
        "asset": "rime-wanxiang-flypy-fuzhu.zip",
        "patch_dir": "flypy",
        "zip_name": "Trime-flypy.zip",
        "use_wxh_repo": False,
    },
}

ROOT = Path(__file__).resolve().parent
VERSION_DIR = ROOT / ".version"
DEFAULT_WORKDIR = ROOT / ".build"


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def die(msg: str) -> "None":
    print(f"[build] 错误: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


# ---------------------------------------------------------------- 网络

def _requests():
    """优先使用 requests, 未安装时返回 None（回落到 urllib）。"""
    try:
        import requests  # noqa: F401
    except ImportError:
        return None
    return requests


HTTP_BACKEND = "requests" if _requests() else "urllib"


def http_get_json(url: str) -> dict:
    """请求 JSON 接口（GitHub API）。"""
    headers = _with_identity_encoding(
        {"User-Agent": "trime-build-script", "Accept": "application/vnd.github+json"}
    )
    reqs = _requests()
    if reqs is not None:
        resp = reqs.get(url, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    import urllib.request

    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _identity_headers() -> dict:
    """请求头: 不让服务端/中间代理对响应体做传输压缩。"""
    return {"User-Agent": "trime-build-script", "Accept-Encoding": ACCEPT_ENCODING}


def _with_identity_encoding(headers: dict) -> dict:
    """把调用方给的请求头补上 identity（显式给了别的值则尊重调用方）。"""
    merged = dict(headers)
    merged.setdefault("Accept-Encoding", ACCEPT_ENCODING)
    return merged


def _iter_chunks(resp, size: int = CHUNK):
    """把 requests / urllib 两种响应统一成「不断产出未解压的 bytes 块」。

    requests 分支必须走 resp.raw.stream(..., decode_content=False): 读到的是未解压
    的原始字节, 与下面的 Content-Encoding 处理配套。不能混用 iter_content(), 它会按
    urllib3 的策略解码, 与自行解压叠加后会导致大小/内容不一致。
    """
    raw = getattr(resp, "raw", None)
    stream = getattr(raw, "stream", None)
    if callable(stream):
        for chunk in stream(size, decode_content=False):
            if chunk:
                yield chunk
        return
    read = getattr(resp, "read", None)
    if callable(read):
        while True:
            chunk = read(size)
            if not chunk:
                return
            yield chunk
        return
    for chunk in resp:
        if chunk:
            yield chunk


def _open_stream_requests(url: str, headers: dict):
    """requests 后端: 打开流式响应; Response.__exit__ 会释放连接。"""
    reqs = _requests()
    session = reqs.Session()
    session.headers.update(headers)
    resp = session.get(url, stream=True, timeout=TIMEOUT)
    resp.raw.decode_content = False  # 原始字节由 _iter_chunks + _Decoder 处理
    return resp


def _open_stream_urllib(url: str, headers: dict):
    """urllib 后端: 打开流式响应, 失败时关闭连接再抛出。"""
    import urllib.request

    opener = urllib.request.build_opener()
    try:
        return opener.open(urllib.request.Request(url, headers=headers), timeout=TIMEOUT)
    except BaseException:
        opener.close()
        raise


class _Decoder:
    """按 Content-Encoding 流式解压, 并区分「正常结束」与「流被截断」。

    - deflate: 服务端可能给 zlib 包装 (RFC 1950) 也可能给裸 deflate (RFC 1951),
      故嗅探头两个字节: 判定为 zlib 就按 zlib 解, 否则按裸 deflate 解; 判断不了的
      一两字节先缓存, 解压中途再出错也可以切到另一种模式重来。
    - 结束时检查 decompressor.eof: 为假说明压缩流没读完（连接被截断、代理截流、
      服务端少发数据）, 必须报错交给上层重试, 而不是把半截文件当成功。
    """

    def __init__(self, encoding: str):
        name = (encoding or "").strip().lower()
        self.name = name
        self._pending = b""
        self._wrote = 0
        self._tried_raw = False
        self._zlib = None
        if not name or name in ("identity", "none", "chunked"):
            self.encoding = "identity"
        elif name in ("gzip", "x-gzip", "zlib", "deflate"):
            self.encoding = name
            self._zlib = _zlib()
            self._dec = self._make(47)  # gzip 与 zlib 头都自动识别
        else:
            die(f"不支持的响应压缩格式 Content-Encoding: {encoding}")

    @property
    def active(self) -> bool:
        return self._zlib is not None

    def _make(self, wbits: int):
        return self._zlib.decompressobj(wbits)

    def _raw(self) -> None:
        """改按裸 deflate 重来, 并把尚未判定格式时缓存的字节一起重喂。"""
        self._dec = self._make(-15)
        self._tried_raw = True
        if self._pending:
            buffered, self._pending = self._pending, b""
            self._dec.decompress(buffered)

    def _can_retry_raw(self) -> bool:
        """能否改按裸 deflate 重来。

        重喂的是当前分块（加上未判定的缓存字节）, 丢弃它们之前在 zlib 模式下产出的
        数据, 因此不会重复输出; 只要还没试过裸模式就允许切换。
        """
        return self.name == "deflate" and not self._tried_raw

    @staticmethod
    def _sniff(chunk: bytes) -> str:
        """嗅探 deflate 头部: "zlib" / "raw" / "undecided"（还判断不了）。"""
        if not chunk:
            return "undecided"
        if len(chunk) < 2:
            # 第一字节 CM 必须是 8 才可能是 zlib, 但校验字节要第二个字节才能算
            return "undecided" if (chunk[0] & 0x0F) == 8 else "raw"
        if (chunk[0] & 0x0F) == 8 and ((chunk[0] << 8) | chunk[1]) % 31 == 0:
            return "zlib"
        return "raw"

    def _finish_member(self) -> bytes:
        """gzip 允许多个成员拼接: 本成员读完就再开一个继续解, 否则后续成员会丢。"""
        extra = b""
        for _ in range(2):
            if not self._dec.eof or self.name not in ("gzip", "x-gzip"):
                return extra
            leftover = self._dec.unused_data
            if not leftover:
                return extra
            self._dec = self._make(47)
            extra += self._dec.decompress(leftover)
        return extra

    def feed(self, chunk: bytes) -> bytes:
        """喂入一段原始字节, 返回解压出的数据（可能为空）。"""
        if not self.active:
            return chunk
        if self.name == "deflate" and not self._tried_raw:
            if not self._pending and len(chunk) < 2:
                self._pending = chunk  # 头两字节不够, 等下一块再判定
                return b""
            how = self._sniff(self._pending + chunk)
            if how == "undecided":
                self._pending += chunk
                return b""
            if how == "raw":
                self._raw()
            chunk = self._pending + chunk
            self._pending = b""
        out = b""
        for _ in range(2):
            try:
                out = self._dec.decompress(chunk)
                break
            except self._zlib.error as exc:
                if not self._can_retry_raw():
                    raise OSError(f"响应解压失败 ({self.name}): {exc}") from exc
                self._raw()
        else:
            raise OSError(f"响应解压失败 ({self.name}): 两种 deflate 格式都无法解析")
        out += self._finish_member()
        self._wrote += len(out)
        return out

    def finish(self) -> bytes:
        """流结束: 补喂缓存, flush 剩余数据, 并确认压缩流完整。"""
        if not self.active:
            return b""
        if self._pending:
            # 整个响应只有 1 个字节: 无法构成任何完整压缩流
            self._pending = b""
            raise OSError(f"响应压缩数据不完整 ({self.name}): 数据不足一个压缩头")
        tail = self._dec.flush()
        if self._dec.eof:
            return tail
        raise OSError(
            f"响应压缩流被截断 ({self.name}): 解压器未到达流末尾, 数据不完整"
        )


def _zlib():
    import zlib

    return zlib


def _write_body(chunks, tmp: Path, dec: _Decoder, on_chunk=None) -> int:
    """把响应体写进临时文件（需要时边读边解压）, 返回落盘字节数。

    on_chunk(落盘字节, 已读字节) 每块调用一次, 用于刷新进度。
    """
    written = read = 0
    with tmp.open("wb") as fh:
        for chunk in chunks:
            read += len(chunk)
            out = dec.feed(chunk)
            if out:
                fh.write(out)
                written += len(out)
            if on_chunk is not None:
                on_chunk(written, read)
        tail = dec.finish()  # 结尾校验: 压缩流被截断会在这里抛错
        if tail:
            fh.write(tail)
            written += len(tail)
    if on_chunk is not None:
        on_chunk(written, read)
    return written


def _check_file_magic(path: Path) -> None:
    """按扩展名做一次廉价的文件头校验, 挡住「传输层把内容搞坏」的情况。"""
    with path.open("rb") as fh:
        head = fh.read(4)
    name = path.name.lower()
    if name.endswith(".zip"):
        if head[:4] != b"PK\x03\x04":
            raise OSError(f"内容不是 zip (文件头 {head!r}), 疑似传输损坏或被改写")
    elif name.endswith(".gz"):
        if head[:2] != b"\x1f\x8b":
            raise OSError(f"内容不是 gzip (文件头 {head!r}), 疑似已被解压或损坏")


def _download_once(url: str, dest: Path, desc: str, tmp: Path,
                   expected_size: int | None) -> int:
    """单次尝试: 流式下载 -> 必要时解压 -> 大小校验 -> 原子替换 dest。"""
    started = time.time()
    with _open_stream(url, _identity_headers()) as resp:
        status = getattr(resp, "status_code", None)
        if status is None:
            status = getattr(resp, "status", 200)
        raise_for_status = getattr(resp, "raise_for_status", None)
        if callable(raise_for_status):
            raise_for_status()
        if not 200 <= int(status) < 300:
            raise OSError(f"HTTP {status}")

        # 已要求 identity; 服务端/代理若仍然压缩, 这里按声明自行解压
        headers = resp.headers
        declared = int(headers.get("Content-Length") or 0)
        dec = _Decoder(headers.get("Content-Encoding"))
        # 压缩响应的 Content-Length 是压缩后的大小, 进度按原始大小算
        bar_total = expected_size if (dec.active and expected_size) else declared
        written = _write_body(_iter_chunks(resp), tmp, dec, on_chunk=_Progress(bar_total, started))

    if dec.active:
        log(f"        服务端返回 {dec.name} 压缩响应, 已解压为原始文件")

    if written == 0:
        raise OSError("下载内容为空")
    if expected_size and written != expected_size:
        raise OSError(f"大小不符: 期望 {expected_size} 字节, 实际 {written} 字节")
    if declared and not dec.active and written != declared:
        raise OSError(f"大小不符: 期望 {declared} 字节 (Content-Length), 实际 {written} 字节")

    _check_file_magic(tmp)
    tmp.replace(dest)
    log(f"完成 {desc}: {written / 1e6:.1f} MB, 用时 {time.time() - started:.1f}s")
    return written


def _open_stream(url: str, headers: dict):
    if _requests() is not None:
        return _open_stream_requests(url, headers)
    return _open_stream_urllib(url, headers)


def http_download(url: str, dest: Path, desc: str = "",
                  expected_size: int | None = None) -> Path:
    """下载文件到 dest, 带重试; 返回 dest。

    落盘的一定是「未压缩的原始文件」: 请求显式要求 identity, 服务端/代理若仍然
    返回压缩内容则按 Content-Encoding 解压后再写入。expected_size 是上游声明的
    原始大小（如 GitHub API 的 asset["size"]）, 用于端到端校验。
    """
    desc = desc or dest.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None

    for attempt in range(1, RETRIES + 1):
        tmp = dest.with_name(dest.name + ".part")
        try:
            log(f"下载 {desc} (第 {attempt}/{RETRIES} 次) <- {url}")
            _download_once(url, dest, desc, tmp, expected_size)
            return dest
        except Exception as exc:  # noqa: BLE001 - 网络异常种类多, 统一重试
            last_err = exc
            tmp.unlink(missing_ok=True)
            log(f"失败: {exc}")
            if attempt < RETRIES:
                time.sleep(2 * attempt)

    die(f"下载 {desc} 失败: {last_err}")
    raise AssertionError("unreachable")  # 让类型检查器知道这里不会返回


class _Progress:
    """下载进度条。压缩响应下 total 是原始大小、落盘字节也按原始大小统计。"""

    def __init__(self, total: int, started: float):
        self.total = total
        self.started = started
        self._last = -1
        self._done = False

    def __call__(self, written: int, read: int) -> None:
        if not self.total:
            return
        if written != self.total and written - self._last < 8 * CHUNK:
            return
        if written == self.total:
            if self._done:
                return
            self._done = True
        self._last = written
        pct = min(written * 100 / self.total, 100.0)
        speed = read / max(time.time() - self.started, 1e-6) / 1e6
        print(f"\r        {pct:5.1f}%  {written / 1e6:6.1f}/{self.total / 1e6:.1f} MB"
              f"  {speed:5.1f} MB/s", end="", flush=True)
        if written == self.total:
            print(flush=True)


# ---------------------------------------------------------------- 文件操作

def _on_rm_error(func, path, _exc_info):
    """rmtree 回调: 清掉只读属性后重试（Windows 上 git 对象文件常为只读）。"""
    try:
        os.chmod(path, 0o700)
        func(path)
    except OSError:
        pass


def robust_rmtree(path: Path) -> None:
    """尽力删除目录树; 失败时打印警告而不是静默留下垃圾。"""
    if not path.exists():
        return
    for attempt in range(3):
        try:
            shutil.rmtree(path, onerror=_on_rm_error)
        except OSError:
            pass
        if not path.exists():
            return
        # Windows 上杀毒/索引偶尔短暂占用句柄, 等一会再来一次
        time.sleep(0.3 * (attempt + 1))
    if path.exists():
        log(f"警告: 无法完全删除 {path}, 请手动清理")


def copy_tree(src: Path, dst: Path, exclude=(), desc: str = "") -> int:
    """把 src 目录内容合并进 dst（覆盖同名文件）。返回复制的文件数。"""
    if not src.is_dir():
        die(f"目录不存在: {src}")
    exclude = {e.lower() for e in exclude}
    count = 0
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        if any(part.lower() in exclude for part in rel.parts):
            continue
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            count += 1
    if desc:
        log(f"{desc}: {count} 个文件 -> {dst}")
    return count


def extract_zip(zip_path: Path, dst: Path) -> int:
    """解压 zip 到 dst（扁平解压, 覆盖同名文件）。"""
    dst.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            if bad is not None:
                die(f"压缩包损坏: {bad}")
            names = zf.namelist()
            zf.extractall(dst)
    except zipfile.BadZipFile as exc:
        # 下载环节已保证落盘的是原始 zip；走到这里通常是文件被换掉或手工放错
        die(f"{zip_path} 不是有效的 zip: {exc}")
    log(f"解压 {zip_path.name}: {len(names)} 项 -> {dst}")
    return len(names)


def zip_dir(src: Path, zip_path: Path) -> tuple[int, int]:
    """把 src 打包成 zip（UTF-8 文件名）。返回 (文件数, 字节数)。"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in src.rglob("*") if p.is_file())
    total = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in files:
            arc = path.relative_to(src).as_posix()
            zf.write(path, arc)
            total += path.stat().st_size
    return len(files), total


# ---------------------------------------------------------------- git

def require_git() -> str:
    exe = shutil.which("git")
    if not exe:
        die("未找到 git 命令, 方案「万象虎」需要 git 才能下载仓库 2")
    return exe


def clone_repo(url: str, dst: Path) -> str:
    """clone 仓库并返回 HEAD 提交哈希。"""
    git = require_git()
    robust_rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    log(f"clone {url} -> {dst}")
    proc = subprocess.run(
        [git, "clone", "--depth", "1", url, str(dst)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        die(f"git clone 失败:\n{proc.stderr.strip()}")
    rev = subprocess.run(
        [git, "-C", str(dst), "rev-parse", "HEAD"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if rev.returncode != 0:
        die(f"读取提交哈希失败:\n{rev.stderr.strip()}")
    head = rev.stdout.strip()
    log(f"仓库 HEAD = {head}")
    return head


# ---------------------------------------------------------------- 各步骤

def pick_scheme(preset: str | None, assume_yes: bool) -> dict:
    """步骤 1: 询问方案。"""
    if preset:
        key = preset.lower()
        if key not in SCHEMES:
            die(f"未知方案 {preset!r}, 可选: {', '.join(SCHEMES)}")
        scheme = SCHEMES[key]
        log(f"方案: {scheme['label']} ({scheme['key']})")
        return scheme

    print("请选择输入方案:")
    print("  1. 万象虎")
    print("  2. 小鹤双拼")
    if assume_yes:
        scheme = SCHEMES["wxh"]
        log(f"未提供输入且指定了 -y, 使用默认方案: {scheme['label']}")
        return scheme

    while True:
        try:
            answer = input("输入 1 或 2 [1]: ").strip()
        except EOFError:
            answer = ""
        if answer in ("", "1"):
            return SCHEMES["wxh"]
        if answer == "2":
            return SCHEMES["flypy"]
        print("  请输入 1 或 2")


def fetch_release(scheme: dict, cache: Path) -> dict:
    """步骤 2a: 查最新 release 并下载对应 zip。"""
    api = f"https://api.github.com/repos/{RELEASE_REPO}/releases/latest"
    asset_url = None
    asset_size = 0
    tag = released = ""
    try:
        rel = http_get_json(api)
        tag = rel.get("tag_name") or ""
        released = rel.get("published_at") or ""
        for asset in rel.get("assets", []):
            if asset.get("name") == scheme["asset"]:
                asset_url = asset.get("browser_download_url")
                asset_size = int(asset.get("size") or 0)
                break
        if not asset_url:
            log(f"release {tag} 中未找到 {scheme['asset']}, 回落到 latest 下载地址")
    except Exception as exc:  # noqa: BLE001 - 接口偶发失败不致命
        log(f"读取 release 信息失败 ({exc}), 回落到 latest 下载地址")

    if not asset_url:
        asset_url = (f"https://github.com/{RELEASE_REPO}/releases/latest/download/"
                     f"{scheme['asset']}")

    # asset_size 来自 release 元数据, 用它校验下载结果而不是只信 Content-Length
    zip_path = cache / scheme["asset"]
    http_download(asset_url, zip_path, f"release {scheme['asset']}",
                  expected_size=asset_size or None)
    return {
        "repo": RELEASE_REPO,
        "asset": scheme["asset"],
        "tag": tag,
        "published_at": released,
        "url": asset_url,
        "size": zip_path.stat().st_size,
        "declared_size": asset_size or None,
    }


def fetch_wxh_repo(cache: Path) -> dict:
    """步骤 2b: clone 仓库 2 并记录最后一次提交的哈希。"""
    url = f"https://github.com/{WXH_REPO}.git"
    src = cache / "wxzhh"
    head = clone_repo(url, src)
    return {"repo": WXH_REPO, "url": url, "commit": head}


def _remote_size(url: str) -> int:
    """用 HEAD 取上游声明的原始大小; 取不到返回 0（只影响校验强度, 不致命）。

    仍带 identity 头: 若服务端对 HEAD 也返回压缩内容, 其 Content-Length 是压缩后
    的大小, 不能当期望值, 这种情况直接放弃校验。
    """
    try:
        headers = _identity_headers()
        reqs = _requests()
        if reqs is not None:
            with reqs.head(url, headers=headers, timeout=TIMEOUT, allow_redirects=True) as resp:
                if resp.status_code >= 400:
                    return 0
                if (resp.headers.get("Content-Encoding") or "identity").lower() != "identity":
                    return 0
                return int(resp.headers.get("Content-Length") or 0)

        import urllib.request

        req = urllib.request.Request(url, headers=headers, method="HEAD")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            if (resp.headers.get("Content-Encoding") or "identity").lower() != "identity":
                return 0
            return int(resp.headers.get("Content-Length") or 0)
    except Exception:  # noqa: BLE001 - HEAD 不被支持时静默降级
        return 0


def download_custom_dicts(dest_dir: Path, manifest: Path) -> list[dict]:
    """步骤 4b: 根据 download.json 下载词库到 dest_dir。

    每条可写 size 显式声明原始字节数; 未写则用 HEAD 探测上游 Content-Length 做校验。
    """
    if not manifest.is_file():
        die(f"缺少词库清单: {manifest}")
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"{manifest} 不是合法 JSON: {exc}")
    if not isinstance(entries, list):
        die(f"{manifest} 应为数组")

    results = []
    for entry in entries:
        name, url = entry.get("name"), entry.get("url")
        if not name or not url:
            die(f"{manifest} 中存在缺少 name/url 的条目: {entry}")
        # 用 URL 的最后一段做文件名, 与 user.dict.yaml 同级
        filename = Path(url.split("?")[0]).name
        target = dest_dir / filename
        expected = int(entry.get("size") or 0) or _remote_size(url)
        if expected:
            log(f"词库 {name} 上游声明大小: {expected} 字节")
        http_download(url, target, f"词库 {name}", expected_size=expected or None)
        results.append({"name": name, "url": url, "file": filename,
                        "size": target.stat().st_size,
                        "declared_size": expected or None})
    return results


def write_version(key: str, payload: dict) -> Path:
    """把版本信息写入 .version/。"""
    VERSION_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["built_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    payload["script"] = Path(__file__).name
    path = VERSION_DIR / f"{key}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    log(f"版本信息写入 {path}")
    return path


# ---------------------------------------------------------------- 主流程

def build(scheme: dict, workdir: Path, out_zip: Path, keep_work: bool,
          skip_download: bool) -> dict:
    cache = workdir / "cache"
    build_dir = workdir / "rime"
    cache.mkdir(parents=True, exist_ok=True)
    robust_rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)

    rel_zip = cache / scheme["asset"]

    # --- 步骤 2: release 解压 + 仓库覆盖
    if skip_download and rel_zip.is_file():
        log(f"复用已下载的 {rel_zip}")
        rel_info = {"repo": RELEASE_REPO, "asset": rel_zip.name, "tag": "",
                    "published_at": "", "url": "", "size": rel_zip.stat().st_size}
    else:
        rel_info = fetch_release(scheme, cache)

    extract_zip(rel_zip, build_dir)

    wxh_info = None
    if scheme["use_wxh_repo"]:
        repo_src = cache / "wxzhh"
        if skip_download and (repo_src / ".git").exists():
            log(f"复用已 clone 的 {repo_src}")
            head = subprocess.run(
                [require_git(), "-C", str(repo_src), "rev-parse", "HEAD"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            ).stdout.strip()
        else:
            wxh_info = fetch_wxh_repo(cache)
            head = wxh_info["commit"]
        exclude = () if KEEP_GIT_DIR else (".git",)
        copy_tree(repo_src, build_dir, exclude=exclude, desc="仓库 2 覆盖")
        if wxh_info is None:
            wxh_info = {"repo": WXH_REPO, "url": f"https://github.com/{WXH_REPO}.git",
                        "commit": head}
    else:
        log("小鹤双拼: 不下载仓库 2, release 内容保持不变")

    # --- 步骤 3: patch
    patch_dir = ROOT / "patch" / scheme["patch_dir"]
    copy_tree(patch_dir, build_dir, desc=f"patch/{scheme['patch_dir']}")

    # --- 步骤 4: 自定义词库
    custom_src = ROOT / "patch" / "custom_dict"
    if not custom_src.is_dir():
        die(f"缺少目录: {custom_src}")
    copy_tree(custom_src, build_dir / "custom_dict", desc="patch/custom_dict")
    dicts = download_custom_dicts(build_dir / "custom_dict", custom_src / "download.json")

    # --- 步骤 5: 皮肤
    skin_src = ROOT / "skin"
    copy_tree(skin_src, build_dir, desc="skin")

    # --- 步骤 6: 打包
    if out_zip.exists():
        out_zip.unlink()
    files, raw = zip_dir(build_dir, out_zip)
    log(f"打包完成: {out_zip} ({files} 个文件, 原始 {raw / 1e6:.1f} MB, "
        f"压缩后 {out_zip.stat().st_size / 1e6:.1f} MB)")

    info = {
        "scheme": scheme["key"],
        "scheme_label": scheme["label"],
        "release": rel_info,
        "wxh_repo": wxh_info,
        "custom_dicts": dicts,
        "files": files,
        "zip": out_zip.name,
        "zip_size": out_zip.stat().st_size,
        "http_backend": HTTP_BACKEND,
    }
    write_version(scheme["key"], info)

    if not keep_work:
        robust_rmtree(workdir)
        log("已清理构建目录")
    else:
        log(f"构建目录保留在 {workdir}")
    return info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="构建 Trime 配置产物 (万象虎 / 小鹤双拼)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--scheme", choices=sorted(SCHEMES), help="直接指定方案, 跳过询问")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="全程使用默认值（默认方案为万象虎）")
    parser.add_argument("-o", "--output", type=Path, help="输出 zip 路径")
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR,
                        help=f"构建临时目录 (默认 {DEFAULT_WORKDIR})")
    parser.add_argument("--keep-workdir", action="store_true", help="构建完成后保留临时目录")
    parser.add_argument("--skip-download", action="store_true",
                        help="复用临时目录中已下载的文件 (调试用)")
    args = parser.parse_args(argv)

    print("=" * 60)
    print("Trime 配置构建脚本")
    print(f"工作目录: {ROOT}")
    print(f"HTTP 后端: {HTTP_BACKEND}"
          + ("" if HTTP_BACKEND == "requests" else "  (未安装 requests)"))
    print("=" * 60)

    scheme = pick_scheme(args.scheme, args.yes)
    out_zip = (args.output or (ROOT / scheme["zip_name"])).resolve()
    workdir = args.workdir.resolve()

    # 产物不能放进临时目录: 打包后清理 workdir 会把产物一起删掉（以前会静默删掉）
    if out_zip == workdir or workdir in out_zip.parents:
        die(f"输出路径不能位于临时目录内: {out_zip}\n"
            f"      临时目录 {workdir} 构建结束后会被清理; 请换一个 -o 路径, "
            f"或加 --keep-workdir 保留临时目录")

    started = time.time()
    info = build(scheme, workdir, out_zip,
                 args.keep_workdir, args.skip_download)

    print("-" * 60)
    print(f"方案      : {info['scheme_label']}")
    print(f"release   : {info['release']['asset']} {info['release']['tag']}".rstrip())
    if info["wxh_repo"]:
        print(f"仓库 2    : {info['wxh_repo']['repo']} @ {info['wxh_repo']['commit'][:12]}")
    print(f"自定义词库: {', '.join(d['name'] for d in info['custom_dicts']) or '无'}")
    print(f"产物      : {out_zip}  ({info['files']} 个文件)")
    print(f"版本记录  : {VERSION_DIR / (scheme['key'] + '.json')}")
    print(f"总用时    : {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[build] 已中断", file=sys.stderr)
        raise SystemExit(130)
