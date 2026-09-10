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
    headers = {"User-Agent": "trime-build-script", "Accept": "application/vnd.github+json"}
    reqs = _requests()
    if reqs is not None:
        resp = reqs.get(url, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    import urllib.request

    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def http_download(url: str, dest: Path, desc: str = "") -> Path:
    """下载文件到 dest，带重试；返回 dest。"""
    desc = desc or dest.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None

    for attempt in range(1, RETRIES + 1):
        tmp = dest.with_name(dest.name + ".part")
        try:
            log(f"下载 {desc} (第 {attempt}/{RETRIES} 次) <- {url}")
            got = 0
            started = time.time()
            reqs = _requests()
            if reqs is not None:
                with reqs.get(url, stream=True, timeout=TIMEOUT,
                              headers={"User-Agent": "trime-build-script"}) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("Content-Length") or 0)
                    with tmp.open("wb") as fh:
                        for chunk in resp.iter_content(CHUNK):
                            if not chunk:
                                continue
                            fh.write(chunk)
                            got += len(chunk)
                            _progress(got, total, started)
            else:
                import urllib.request

                req = urllib.request.Request(url, headers={"User-Agent": "trime-build-script"})
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    total = int(resp.headers.get("Content-Length") or 0)
                    with tmp.open("wb") as fh:
                        while True:
                            chunk = resp.read(CHUNK)
                            if not chunk:
                                break
                            fh.write(chunk)
                            got += len(chunk)
                            _progress(got, total, started)

            if got == 0:
                raise OSError("下载内容为空")
            if total and got != total:
                raise OSError(f"大小不符: 期望 {total} 字节, 实际 {got} 字节")

            tmp.replace(dest)
            log(f"完成 {desc}: {got / 1e6:.1f} MB, 用时 {time.time() - started:.1f}s")
            return dest
        except Exception as exc:  # noqa: BLE001 - 网络异常种类多, 统一重试
            last_err = exc
            tmp.unlink(missing_ok=True)
            log(f"失败: {exc}")
            if attempt < RETRIES:
                time.sleep(2 * attempt)

    die(f"下载 {desc} 失败: {last_err}")


def _progress(got: int, total: int, started: float) -> None:
    if not total:
        return
    if got % (8 * CHUNK) < CHUNK or got == total:
        pct = got * 100 / total
        speed = got / max(time.time() - started, 1e-6) / 1e6
        print(f"\r        {pct:5.1f}%  {got / 1e6:6.1f}/{total / 1e6:.1f} MB  {speed:5.1f} MB/s",
              end="", flush=True)
        if got == total:
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
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad is not None:
            die(f"压缩包损坏: {bad}")
        names = zf.namelist()
        zf.extractall(dst)
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
    tag = released = ""
    try:
        rel = http_get_json(api)
        tag = rel.get("tag_name") or ""
        released = rel.get("published_at") or ""
        for asset in rel.get("assets", []):
            if asset.get("name") == scheme["asset"]:
                asset_url = asset.get("browser_download_url")
                break
        if not asset_url:
            log(f"release {tag} 中未找到 {scheme['asset']}, 回落到 latest 下载地址")
    except Exception as exc:  # noqa: BLE001 - 接口偶发失败不致命
        log(f"读取 release 信息失败 ({exc}), 回落到 latest 下载地址")

    if not asset_url:
        asset_url = (f"https://github.com/{RELEASE_REPO}/releases/latest/download/"
                     f"{scheme['asset']}")

    zip_path = cache / scheme["asset"]
    http_download(asset_url, zip_path, f"release {scheme['asset']}")
    return {
        "repo": RELEASE_REPO,
        "asset": scheme["asset"],
        "tag": tag,
        "published_at": released,
        "url": asset_url,
        "size": zip_path.stat().st_size,
    }


def fetch_wxh_repo(cache: Path) -> dict:
    """步骤 2b: clone 仓库 2 并记录最后一次提交的哈希。"""
    url = f"https://github.com/{WXH_REPO}.git"
    src = cache / "wxzhh"
    head = clone_repo(url, src)
    return {"repo": WXH_REPO, "url": url, "commit": head}


def download_custom_dicts(dest_dir: Path, manifest: Path) -> list[dict]:
    """步骤 4b: 根据 download.json 下载词库到 dest_dir。"""
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
        http_download(url, target, f"词库 {name}")
        results.append({"name": name, "url": url, "file": filename,
                        "size": target.stat().st_size})
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

    started = time.time()
    info = build(scheme, args.workdir.resolve(), out_zip,
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
