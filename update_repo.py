#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Niubiprass 源 - 插件自动更新脚本

用法：
    python3 update_repo.py                # 检查并更新（有新版才动）
    python3 update_repo.py --dry-run      # 只检查，不下载不推送
    python3 update_repo.py --only <pkg>   # 只更新指定包

逻辑：
    1. 读取 update-config.json 的源清单与监控清单
    2. 拉取每个源的 Packages 索引
    3. 对每个监控包，找到外部源里的最高版本
    4. 与本地 debs/ 里的版本比较，有新版就下载、替换
    5. 重算 Packages / Packages.gz / Packages.bz2 / Release，提交推送
"""
import os, re, sys, json, gzip, bz2, shutil, hashlib, subprocess, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
DEBS = os.path.join(ROOT, "debs")
CONFIG = os.path.join(ROOT, "update-config.json")
CACHE = os.path.join(ROOT, ".update-cache")
UA = {"User-Agent": "Mozilla/5.0 (compatible; Niubiprass-RepoUpdater/1.0)"}
PROXY = os.environ.get("REPO_PROXY", "")   # 例如 https://gh-proxy.com

DRY = "--dry-run" in sys.argv
ONLY = None
if "--only" in sys.argv:
    ONLY = sys.argv[sys.argv.index("--only") + 1]

def log(*a): print(*a, flush=True)

def fetch(url, timeout=45, proxy=False):
    """下载 URL。proxy=True 时套用 REPO_PROXY 前缀"""
    if proxy and PROXY and not url.startswith(PROXY):
        full = PROXY.rstrip("/") + "/" + url
    else:
        full = url
    try:
        req = urllib.request.Request(full, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        if url.endswith(".bz2"): return bz2.decompress(data).decode("utf-8", "replace")
        if url.endswith(".gz"):  return gzip.decompress(data).decode("utf-8", "replace")
        return data.decode("utf-8", "replace")
    except Exception:
        return None

def fetch_index(base, proxy=False):
    """按顺序尝试三种索引格式"""
    base = base.rstrip("/")
    for path in ("Packages", "Packages.bz2", "Packages.gz"):
        txt = fetch(f"{base}/{path}", proxy=proxy)
        if txt and "Package:" in txt:
            return txt
    return None

def parse_index(txt):
    """解析 Packages，返回 {(package, arch): [ {字段字典}, ... ]}"""
    out = {}
    for blk in txt.split("\n\n"):
        if "Package:" not in blk: continue
        d = dict(re.findall(r"^([A-Z][\w-]*): (.*)$", blk, re.M))
        pid = d.get("Package"); ver = d.get("Version"); arch = d.get("Architecture", "?")
        if not pid or not ver: continue
        # 多架构行（如 "iphoneos-arm64 iphoneos-arm64e"）拆分
        for a in arch.split():
            out.setdefault((pid, a), []).append(d)
    return out

def version_gt(v1, v2):
    """v1 > v2 ?  用 dpkg 的版本比较规则"""
    if not v1 or not v2: return False
    r = subprocess.run(["dpkg", "--compare-versions", v1, "gt", v2])
    return r.returncode == 0

def local_packages():
    """扫描 debs/ 里的本地包"""
    out = {}
    for fn in sorted(os.listdir(DEBS)):
        if not fn.endswith(".deb"): continue
        p = os.path.join(DEBS, fn)
        try:
            raw = subprocess.run(["dpkg-deb", "-f", p],
                                 capture_output=True, text=True, timeout=30).stdout
            d = dict(re.findall(r"^([A-Z][\w-]*): (.*)$", raw, re.M))
            pid, ver, arch = d.get("Package", ""), d.get("Version", ""), d.get("Architecture", "")
            if pid: out[(pid, arch)] = {"file": fn, "version": ver}
        except Exception as e:
            log(f"  ! 读取失败 {fn}: {e}")
    return out

def download(url, dest, proxy=False):
    if proxy and PROXY and not url.startswith(PROXY):
        url = PROXY.rstrip("/") + "/" + url
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            if os.path.getsize(dest) > 1024 and \
               subprocess.run(["dpkg-deb", "-I", dest], capture_output=True).returncode == 0:
                return True
        except Exception:
            pass
    return False

def rebuild_index():
    """重算 Packages / gz / bz2 / Release"""
    os.chdir(ROOT)
    pkgs = subprocess.run(["dpkg-scanpackages", "debs", "/dev/null"],
                          capture_output=True, text=True).stdout
    with open("Packages", "w") as f: f.write(pkgs)
    with open("Packages.gz", "wb") as f:
        f.write(gzip.compress(pkgs.encode(), 9))
    with open("Packages.bz2", "wb") as f:
        f.write(bz2.compress(pkgs.encode(), 9))
    lines = [
        "Origin: Niubiprass", "Label: Niubiprass", "Suite: stable", "Version: 1.0",
        "Codename: ios", "Architectures: iphoneos-arm64 iphoneos-arm64e",
        "Components: main", "Description: 只自用-支持穷逼 15 系统",
    ]
    for h in ("MD5Sum", "SHA1", "SHA256"):
        lines += ["", f"{h}:"]
        for fn in ("Packages", "Packages.gz", "Packages.bz2"):
            data = open(fn, "rb").read()
            size = len(data)
            if h == "MD5Sum":   digest = hashlib.md5(data).hexdigest()
            elif h == "SHA1":   digest = hashlib.sha1(data).hexdigest()
            else:               digest = hashlib.sha256(data).hexdigest()
            lines.append(f" {digest} {size} {fn}")
    open("Release", "w").write("\n".join(lines) + "\n")
    return len(re.findall(r"^Package:", pkgs, re.M))

def main():
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    sources, monitor = cfg["sources"], cfg["monitor"]
    os.makedirs(CACHE, exist_ok=True)

    log("=" * 68)
    log(f"Niubiprass 源更新检查  {'[DRY-RUN]' if DRY else ''}")
    log("=" * 68)

    # 1. 拉取外部源
    ext, src_map = {}, {}
    for s in sources:
        name, base = s["name"], s["url"]
        src_map[name] = s
        txt = fetch_index(base, proxy=s.get("proxy", False))
        if not txt:
            log(f"  × {name:12s} 无法访问")
            continue
        idx = parse_index(txt)
        ext[name] = idx
        log(f"  √ {name:12s} {len(set(k[0] for k in idx))} 包")

    # 2. 本地
    loc = local_packages()
    log(f"\n本地 debs/: {len(loc)} 个包\n")

    # 3. 比对
    updates, skipped = [], []
    for item in monitor:
        pid, arch = item["package"], item["arch"]
        if ONLY and pid != ONLY: continue
        cur = loc.get((pid, arch))
        if not cur:
            skipped.append((pid, arch, "本地无此包")); continue
        # 锁定包：本地为定制版（已去除源锁/弹窗），禁止外部源覆盖
        if item.get("locked"):
            skipped.append((pid, arch, f"已锁定 ({cur['version']}) 本地定制版，禁止外部覆盖")); continue
        # 找外部最高版本：精确架构优先，再退回同族架构；支持 altNames 别名
        fam = "arm64e" if arch.endswith("e") else "arm64"
        alt = item.get("altNames", [])
        def cands_for(idx, pid_):
            got = idx.get((pid_, arch), [])
            if not got:
                got = idx.get((pid_, arch.replace("arm64e", "arm64")), [])
            if not got:
                got = idx.get((pid_, arch.replace("arm64", "arm64e")), [])
            return got
        best_ver, best_src, best_entry, best_pid = None, None, None, None
        for name, idx in ext.items():
            for pid_ in [pid] + alt:
                for entry in cands_for(idx, pid_):
                    v = entry.get("Version")
                    if not best_ver or version_gt(v, best_ver):
                        best_ver, best_src, best_entry, best_pid = v, name, entry, pid_
        if not best_ver:
            skipped.append((pid, arch, "外部源未找到")); continue
        if version_gt(best_ver, cur["version"]):
            updates.append({"pid": pid, "arch": arch, "from": cur["version"],
                            "to": best_ver, "src": best_src, "entry": best_entry,
                            "src_pid": best_pid})
        else:
            skipped.append((pid, arch, f"已最新 ({cur['version']})"))

    # 2.5 autoAdd：把「尚未收录」的新插件首次收进源
    auto_add = cfg.get("autoAdd", [])
    added_note = []
    if auto_add:
        log(f"\n--- autoAdd 新插件检查（{len(auto_add)} 个）---")
        for item in auto_add:
            pid, arch = item["package"], item["arch"]
            if ONLY and pid != ONLY: continue
            if (pid, arch) in loc:
                log(f"  · {pid:44s} 已在源中，转常规更新"); continue
            alt = item.get("altNames", [])
            want_src = item.get("from")
            pool = {want_src: ext[want_src]} if (want_src and want_src in ext) else ext
            best_ver, best_src, best_entry, best_pid = None, None, None, None
            for name, idx in pool.items():
                for pid_ in [pid] + alt:
                    for a in (arch, arch.replace("arm64e", "arm64"), arch.replace("arm64", "arm64e")):
                        for entry in idx.get((pid_, a), []):
                            v = entry.get("Version")
                            if not best_ver or version_gt(v, best_ver):
                                best_ver, best_src, best_entry, best_pid = v, name, entry, pid_
            if not best_ver:
                log(f"  × {pid:44s} 外部源未找到，跳过"); continue
            log(f"  + {pid:44s} 新增 {best_ver}   [{best_src}]")
            added_note.append({"pid": pid, "arch": arch, "version": best_ver,
                               "src": best_src, "entry": best_entry, "isNew": True})
            updates.append({"pid": pid, "arch": arch, "from": "(新增)",
                            "to": best_ver, "src": best_src, "entry": best_entry,
                            "src_pid": best_pid, "isNew": True})
        log("")

    log("--- 检查结果 ---")
    if updates:
        for u in updates:
            log(f"  ↑ {u['pid']:44s} {u['from']}  →  {u['to']}   [{u['src']}]")
    else:
        log("  ✓ 所有插件均为最新，无需更新")
    for pid, arch, why in skipped:
        log(f"  · {pid:44s} {why}")

    if not updates:
        return 0
    if DRY:
        log(f"\n[DRY-RUN] 有 {len(updates)} 个可更新，未执行下载")
        return 0

    # 4. 下载替换
    log(f"\n--- 开始更新 {len(updates)} 个包 ---")
    for u in updates:
        entry = u["entry"]
        fn = entry.get("Filename") or ""
        base = src_map[u["src"]]["url"].rstrip("/")
        if fn.startswith("http"):
            url = fn
        else:
            url = base + "/" + fn.lstrip("./")
        ext_name = os.path.basename(fn) or f"{u['pid']}_{u['to']}_{u['arch']}.deb"
        dest = os.path.join(DEBS, ext_name)
        tmp = os.path.join(CACHE, ext_name)
        log(f"  ↓ {u['pid']} {u['to']}  ({u['src']})")
        if not download(url, tmp, proxy=("raw.githubusercontent.com" in url)):
            log(f"    ! 下载失败，跳过"); continue
        # 删除同 package+arch 的旧文件（新增包没有旧文件）
        old = loc.get((u["pid"], u["arch"]))
        if old and old["file"] != ext_name:
            op = os.path.join(DEBS, old["file"])
            if os.path.exists(op): os.remove(op); log(f"    - 移除旧包 {old['file']}")
        shutil.move(tmp, dest)
        log(f"    + {ext_name}")

    # 5. 重建索引
    n = rebuild_index()
    log(f"\n索引已重建：{n} 个包")

    # 6. 提交推送
    subprocess.run(["git", "config", "user.name", "Niubiprass"], cwd=ROOT)
    subprocess.run(["git", "config", "user.email", "Niubiprass@users.noreply.github.com"], cwd=ROOT)
    subprocess.run(["git", "add", "-A"], cwd=ROOT)
    new_p = [u for u in updates if u.get("isNew")]
    upd_p = [u for u in updates if not u.get("isNew")]
    parts = []
    if upd_p: parts.append("更新 " + ", ".join(f"{u['pid']}→{u['to']}" for u in upd_p))
    if new_p: parts.append("新增 " + ", ".join(f"{u['pid']}@{u['to']}" for u in new_p))
    msg = "chore(auto): " + "；".join(parts)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=ROOT)
    r = subprocess.run(["git", "push", "origin", "main"], cwd=ROOT, capture_output=True, text=True)
    log("推送：" + (r.stdout + r.stderr).strip().split("\n")[-1])
    shutil.rmtree(CACHE, ignore_errors=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
