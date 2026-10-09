#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Niubiprass 源 - APT 索引生成器（唯一入口）

无论 .deb 通过哪种途径进入仓库（网页上传 / git push / Actions 自动下载 /
手动丢在任意目录），只要文件出现在仓库内，本脚本都会：
    1. 把 debs/ 之外的 .deb 收拢进 debs/（任意上传路径都能被索引）
    2. 以实际存在的文件为准重建 Packages / Packages.gz / Packages.bz2
    3. 依据重建结果生成带校验和的 Release

关键点：
    dpkg-scanpackages 依赖 `dpkg-deb -f`，它会把 control 文件解包到 /tmp 再读取。
    有些第三方 deb 的 control 文件权限是 000，非 root 环境（如 GitHub Actions
    的 runner）读取时会被 Permission denied 卡住，导致整个扫描以 exit 25 失败，
    索引永远生成不了。因此这里改用 `dpkg-deb --ctrl-tarfile` 直接把 control
    归档流读进内存解析，不受文件权限影响；单个包解析失败只跳过，不会拖垮全量。

被 .github/workflows/auto.yml 与 update_repo.py 共同调用，保证逻辑唯一。

用法：
    python3 build_index.py
"""
import os
import sys
import io
import json
import gzip
import bz2
import shutil
import hashlib
import tarfile
import subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
DEBS = os.path.join(ROOT, "debs")
CONFIG = os.path.join(ROOT, "update-config.json")
# 这些目录里的 .deb 不作为仓库包收拢（构建缓存 / 版本控制元数据等）
SKIP_DIRS = {".git", ".update-cache", "node_modules", "__pycache__"}


def log(*a):
    print(*a, flush=True)


def _unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base}({i}){ext}"):
        i += 1
    return f"{base}({i}){ext}"


def collect_debs():
    """把仓库内 debs/ 之外的所有 .deb 移动到 debs/。

    这样无论用户把包上传到哪个目录，都会被索引收录。
    返回 [(原相对路径, 新相对路径), ...]。
    """
    os.makedirs(DEBS, exist_ok=True)
    debs_real = os.path.realpath(DEBS)
    moved = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        if os.path.realpath(dirpath) == debs_real:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.lower().endswith(".deb"):
                continue
            src = os.path.join(dirpath, fn)
            dest = _unique_path(os.path.join(DEBS, fn))
            if os.path.realpath(src) == os.path.realpath(dest):
                continue
            shutil.move(src, dest)
            moved.append((os.path.relpath(src, ROOT), os.path.relpath(dest, ROOT)))
    return moved


def control_text(deb):
    """直接从 control 归档流里读取 control 文件内容，不受 000 权限影响。"""
    r = subprocess.run(["dpkg-deb", "--ctrl-tarfile", deb], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b"").decode("utf-8", "replace").strip() or "dpkg-deb 读取失败")
    last_err = None
    for mode in ("r:", "r:gz", "r:xz", "r:bz2"):
        try:
            with tarfile.open(fileobj=io.BytesIO(r.stdout), mode=mode) as tf:
                for m in tf.getmembers():
                    if os.path.basename(m.name) == "control":
                        f = tf.extractfile(m)
                        if f is not None:
                            return f.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    raise RuntimeError(f"无法解析 control 归档: {last_err}")


def parse_fields(text):
    out = {}
    for line in text.splitlines():
        if not line or line[0] in " \t" or ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip()
    return out


def read_control_fields(path):
    """读取 .deb 的 control 字段（权限无关，供索引与更新脚本共用）。"""
    return parse_fields(control_text(path))


def file_hashes(path):
    md5, sha1, sha256 = hashlib.md5(), hashlib.sha1(), hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            md5.update(chunk)
            sha1.update(chunk)
            sha256.update(chunk)
    return md5.hexdigest(), sha1.hexdigest(), sha256.hexdigest()


def load_pins():
    """读取 update-config.json 里 monitor 的 pinVersion，返回 {(package, arch): version}。"""
    pins = {}
    try:
        cfg = json.load(open(CONFIG, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return pins
    for item in cfg.get("monitor", []):
        ver = item.get("pinVersion")
        if ver and item.get("package") and item.get("arch"):
            pins[(item["package"], item["arch"])] = ver
    return pins


def version_gt(v1, v2):
    """v1 > v2 ?（沿用 dpkg 版本比较规则，失败时退回字符串比较）"""
    if not v1 or not v2:
        return False
    try:
        return subprocess.run(
            ["dpkg", "--compare-versions", v1, "gt", v2]
        ).returncode == 0
    except FileNotFoundError:
        return v1 > v2


def build_entries():
    """遍历 debs/ 下所有 .deb，生成 Packages 条目文本。

    同一 (Package, Architecture) 只保留版本最高的一个；
    单个包解析失败仅跳过并告警，不影响其余包。
    """
    pins = load_pins()
    best = {}
    for fn in sorted(os.listdir(DEBS)):
        if not fn.lower().endswith(".deb"):
            continue
        path = os.path.join(DEBS, fn)
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        try:
            text = control_text(path)
            fields = parse_fields(text)
            pkg = fields.get("Package")
            ver = fields.get("Version")
            arch = fields.get("Architecture", "")
            if not pkg or not ver:
                raise RuntimeError("缺少 Package/Version 字段")
        except Exception as e:  # noqa: BLE001
            log(f"  ! 跳过无法解析的包 {rel}: {e}")
            continue
        key = (pkg, arch)
        pinned = pins.get(key)
        if pinned and ver != pinned:
            continue
        if key in best and not version_gt(ver, best[key]["version"]):
            continue
        try:
            md5, sha1, sha256 = file_hashes(path)
        except Exception as e:  # noqa: BLE001
            log(f"  ! 跳过无法读取的包 {rel}: {e}")
            continue
        block = text.strip("\n")
        block += (
            f"\nFilename: {rel}"
            f"\nSize: {os.path.getsize(path)}"
            f"\nMD5sum: {md5}"
            f"\nSHA1: {sha1}"
            f"\nSHA256: {sha256}"
        )
        best[key] = {"version": ver, "block": block}
    return [best[k]["block"] for k in sorted(best)]


def _write_release():
    lines = [
        "Origin: Niubiprass",
        "Label: Niubiprass",
        "Suite: stable",
        "Version: 1.0",
        "Codename: ios",
        "Architectures: iphoneos-arm64 iphoneos-arm64e",
        "Components: main",
        "Description: 只自用-支持穷逼 15 系统",
    ]
    for h in ("MD5Sum", "SHA1", "SHA256"):
        lines.append("")
        lines.append(f"{h}:")
        for fn in ("Packages", "Packages.gz", "Packages.bz2"):
            with open(os.path.join(ROOT, fn), "rb") as f:
                data = f.read()
            if h == "MD5Sum":
                digest = hashlib.md5(data).hexdigest()
            elif h == "SHA1":
                digest = hashlib.sha1(data).hexdigest()
            else:
                digest = hashlib.sha256(data).hexdigest()
            lines.append(f" {digest} {len(data)} {fn}")
    with open(os.path.join(ROOT, "Release"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def build():
    """执行完整重建流程，返回收录的包数量。"""
    moved = collect_debs()
    for src, dest in moved:
        log(f"  → 收拢 {src}  =>  {dest}")

    blocks = build_entries()
    body = "\n\n".join(blocks) + ("\n" if blocks else "")

    with open(os.path.join(ROOT, "Packages"), "w", encoding="utf-8") as f:
        f.write(body)
    with open(os.path.join(ROOT, "Packages.gz"), "wb") as f:
        buf = io.BytesIO()
        # mtime=0：保证相同内容生成的 gz 完全一致，避免每次运行都产生无意义改动
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as gz:
            gz.write(body.encode("utf-8"))
        f.write(buf.getvalue())
    with open(os.path.join(ROOT, "Packages.bz2"), "wb") as f:
        f.write(bz2.compress(body.encode("utf-8"), 9))
    _write_release()

    if moved:
        log(f"已收拢 {len(moved)} 个散落 .deb")
    log(f"索引已重建：{len(blocks)} 个包")
    return len(blocks)


if __name__ == "__main__":
    build()
    sys.exit(0)
