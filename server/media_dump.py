"""
微信 / H5 / 小程序 媒体嗅探旗舰版脚本（图片 + HLS + DASH + MP4直链）
==========================================================
新增：MP4 直链（公众号视频常见：video/mp4 + 206 Range）
下载方式：requests 流式下载
证书模式：模式A（requests 走 mitmproxy 代理）——使用 mitmproxy 生成的 CA 进行 verify

你需要：
1) mitmproxy 已生成并安装证书（浏览器侧能正常抓 https）
2) Python 环境安装 requests：pip install requests

输出目录：
output/
  images/
  videos/
    m3u8/ ts/ mpd/ m4s/ mp4/ mp4_direct/
日志：
  image_all_urls.txt / image_urls.txt / unparsed_debug.txt
  video_all_urls.txt / video_urls.txt / video_errors.txt
"""

import os
import re
import hashlib
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from mitmproxy import http

import requests

# =======================================================
# 证书模式A：使用 mitmproxy CA 给 requests.verify
# =======================================================
def find_mitmproxy_ca() -> str:
    """
    mitmproxy 默认把 CA 放在 ~/.mitmproxy/
    Windows: C:\\Users\\<you>\\.mitmproxy\\mitmproxy-ca-cert.pem
    """
    ca_dir = Path.home() / ".mitmproxy"
    candidates = [
        ca_dir / "mitmproxy-ca-cert.pem",
        ca_dir / "mitmproxy-ca-cert.cer",
        ]
    for p in candidates:
        if p.exists():
            return str(p)
    return ""

MITM_CA = find_mitmproxy_ca()
if not MITM_CA:
    raise RuntimeError(
        "未找到 mitmproxy CA：请确认已生成证书。应存在于 ~/.mitmproxy/ 下，如 mitmproxy-ca-cert.pem"
    )

def verify_for_url(url: str):
    """
    模式A：requests 走 mitmproxy 代理，因此对被 MITM 的站点用 mitmproxy CA 校验。
    你遇到的 wxsmw.wxs.qq.com / smtcdns / qqvideo 等都在这里。
    """
    host = (urlparse(url).hostname or "").lower()
    if (
            host.endswith("wxs.qq.com")
            or host.endswith("qq.com")
            or host.endswith("smtcdns.com")
    ):
        return MITM_CA
    return True


# =======================================================
# 目录结构初始化
# =======================================================
BASE_DIR = "output"

IMG_DIR = os.path.join(BASE_DIR, "images")
IMG_CONVERT_DIR = os.path.join(IMG_DIR, "converted")

VIDEO_DIR = os.path.join(BASE_DIR, "videos")
M3U8_DIR = os.path.join(VIDEO_DIR, "m3u8")
TS_DIR = os.path.join(VIDEO_DIR, "ts")
MP4_DIR = os.path.join(VIDEO_DIR, "mp4")              # ffmpeg 合成输出
MPD_DIR = os.path.join(VIDEO_DIR, "mpd")
M4S_DIR = os.path.join(VIDEO_DIR, "m4s")
MP4_DIRECT_DIR = os.path.join(VIDEO_DIR, "mp4_direct")  # ★ 新增：直链 MP4

# 图片相关日志
IMAGE_URL_LOG = os.path.join(BASE_DIR, "image_urls.txt")
IMAGE_ALL_LOG = os.path.join(BASE_DIR, "image_all_urls.txt")
IMAGE_UNPARSED_LOG = os.path.join(BASE_DIR, "unparsed_debug.txt")

# 视频相关日志
VIDEO_URL_LOG = os.path.join(BASE_DIR, "video_urls.txt")    # m3u8 / mpd / mp4
VIDEO_ALL_LOG = os.path.join(BASE_DIR, "video_all_urls.txt")
VIDEO_ERROR_LOG = os.path.join(BASE_DIR, "video_errors.txt")

for d in [
    BASE_DIR, IMG_DIR, IMG_CONVERT_DIR,
    VIDEO_DIR, M3U8_DIR, TS_DIR, MP4_DIR, MPD_DIR, M4S_DIR, MP4_DIRECT_DIR
]:
    os.makedirs(d, exist_ok=True)


# =======================================================
# URL 去重（按“路径”去重，忽略 query）
# =======================================================
SEEN_IMAGE_URL = set()
SEEN_IMAGE_ALL_URL = set()

SEEN_VIDEO_URL = set()       # m3u8/mpd 去重
SEEN_VIDEO_ALL_URL = set()

SEEN_MP4_URL = set()         # mp4 直链去重（按路径）

DOWNLOADING = set()
DOWNLOADING_LOCK = threading.Lock()


def url_key(url: str) -> str:
    return url.split("?", 1)[0]


def save_binary(path, content: bytes):
    with open(path, "wb") as f:
        f.write(content)


def append_line(path: str, line: str):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
    except Exception as e:
        print(f"[LOG ERROR] {path}: {e}")


# =======================================================
# 图片相关：未解析 / 异常调试输出
# =======================================================
def log_unparsed_image(flow: http.HTTPFlow, reason: str, extra: str = ""):
    url = flow.request.pretty_url
    headers = flow.response.headers
    data = flow.response.content or b""
    length = len(data)

    ct = headers.get("Content-Type", "")
    imgx = headers.get("imagex-fmt", "")

    print(f"[UNPARSED IMG] reason={reason} len={length} url={url}")
    if extra:
        print(f"               extra={extra}")

    try:
        with open(IMAGE_UNPARSED_LOG, "a", encoding="utf-8") as f:
            f.write("\n================= UNPARSED IMAGE =================\n")
            f.write(f"REASON      : {reason}\n")
            if extra:
                f.write(f"EXTRA       : {extra}\n")
            f.write(f"URL         : {url}\n")
            f.write(f"LENGTH      : {length}\n")
            f.write(f"Content-Type: {ct}\n")
            f.write(f"imagex-fmt  : {imgx}\n")
            f.write("HEADERS:\n")
            for k, v in headers.items():
                f.write(f"  {k}: {v}\n")
            f.write("==================================================\n")
    except Exception as e:
        print("[UNPARSED-LOG ERROR]", e)


# =======================================================
# 图片候选检测 & 全量 URL 记录
# =======================================================
def is_image_candidate(flow: http.HTTPFlow) -> bool:
    url = flow.request.pretty_url.lower()
    ct = flow.response.headers.get("Content-Type", "").lower()

    if "hm.baidu.com/hm.gif" in url:
        return False

    if re.search(r"\.(jpg|jpeg|png|gif|bmp|webp|avif|heic|svg)(\?|$)", url):
        return True

    if "tplv" in url:
        return True

    if ct.startswith("image/"):
        return True

    if any(x in url for x in ["mmbiz", "qlogo.cn", "mmbiz.qpic.cn", "pb.plusx.cn"]):
        return True

    return False


def log_all_image_url(flow: http.HTTPFlow):
    url = flow.request.pretty_url
    key = url_key(url)
    if key in SEEN_IMAGE_ALL_URL:
        return
    SEEN_IMAGE_ALL_URL.add(key)

    ct = flow.response.headers.get("Content-Type", "").lower()
    append_line(IMAGE_ALL_LOG, f"{url}    [ct={ct}]")


# =======================================================
# 视频候选检测 & 全量 URL 记录（含 HLS + DASH + MP4）
# =======================================================
def is_video_candidate(flow: http.HTTPFlow) -> bool:
    url = flow.request.pretty_url.lower()
    ct = flow.response.headers.get("Content-Type", "").lower()

    # HLS：m3u8
    if url.endswith(".m3u8") or ".m3u8?" in url:
        return True
    if "m3u8" in url and ("api" in url or "/m3u8/" in url):
        return True
    if ct.startswith("application/vnd.apple.mpegurl") or ct.startswith("application/x-mpegurl"):
        return True

    # HLS：TS
    if url.endswith(".ts") or ".ts?" in url:
        return True
    if ct == "video/mp2t":
        return True

    # DASH：mpd
    if url.endswith(".mpd") or ".mpd?" in url:
        return True
    if ct.startswith("application/dash+xml"):
        return True

    # DASH：m4s
    if url.endswith(".m4s") or ".m4s?" in url:
        return True
    if ".m4s" in url and (ct.startswith("video/") or ct.startswith("application/octet-stream")):
        return True

    # MP4 直链
    if ct.startswith("video/mp4") or url.endswith(".mp4") or ".mp4?" in url:
        return True

    # 泛型视频兜底
    if ct.startswith("video/"):
        return True

    return False


def log_all_video_url(flow: http.HTTPFlow):
    url = flow.request.pretty_url
    key = url_key(url)
    if key in SEEN_VIDEO_ALL_URL:
        return
    SEEN_VIDEO_ALL_URL.add(key)

    ct = flow.response.headers.get("Content-Type", "").lower()
    append_line(VIDEO_ALL_LOG, f"{url}    [ct={ct}]")


# =======================================================
# Magic Number 识别（图片）
# =======================================================
def detect_magic_ext(data: bytes):
    if data.startswith(b"\xFF\xD8\xFF"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data[4:12] == b"ftypavif":
        return "avif"
    if len(data) >= 12 and data[4:12] in (b"ftypheic", b"ftypheif"):
        return "heic"
    return None


def ext_from_imagex_fmt(fmt: str) -> str:
    fmt = (fmt or "").lower()
    mapping = {
        "jpg": "jpg",
        "jpeg": "jpg",
        "png": "png",
        "gif": "gif",
        "webp": "webp",
        "avif": "avif",
        "heic": "heic",
        "heif": "heif",

        "avif2webp": "webp",
        "heic2webp": "webp",
        "jpeg2webp": "webp",
        "png2webp": "webp",
        "avif2avif": "avif",
    }
    ext = mapping.get(fmt)
    if ext:
        return ext
    if fmt.endswith("2avif"):
        return "avif"
    if fmt.endswith("2webp"):
        return "webp"
    if fmt.endswith("2jpg") or fmt.endswith("2jpeg"):
        return "jpg"
    if fmt.endswith("2png"):
        return "png"
    return "bin"


def ext_from_url(url: str):
    m = re.search(r"\.(jpg|jpeg|png|gif|bmp|webp|svg|avif|heic)(\?|$)", url, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    return None


def detect_image_ext(flow: http.HTTPFlow, data: bytes) -> str:
    headers = flow.response.headers
    url = flow.request.pretty_url
    content_type = headers.get("Content-Type", "").lower()

    fmt = headers.get("imagex-fmt")
    if fmt:
        return ext_from_imagex_fmt(fmt)

    if content_type.startswith("image/"):
        return content_type.split("/")[1].split(";")[0].lower()

    magic = detect_magic_ext(data)
    if magic:
        return magic

    url_ext = ext_from_url(url)
    if url_ext:
        return url_ext

    return "bin"


def extract_original_name(url: str) -> str:
    clean = url.split("?")[0]
    parts = clean.split("/")

    for p in parts:
        if re.match(r"(DSC|IMGS|IMG|PXL|photo|mmexport)[A-Za-z0-9_-]+\.", p, re.IGNORECASE):
            return p.split(".")[0]

    if len(parts) > 2:
        cand = parts[-2]
        if re.match(r"[A-Za-z0-9_-]{3,}", cand) and "tplv" not in cand:
            return cand

    last = re.split(r"[\*~]tplv", parts[-1])[0]
    last = last.split(".")[0]
    if re.match(r"[A-Za-z0-9_-]{3,}", last):
        return last

    h = hashlib.md5(clean.encode()).hexdigest()[:10]
    return f"img_{h}"


# =======================================================
# AVIF 动图检测 & 转换
# =======================================================
def detect_animated_avif(path: str) -> bool:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=nb_frames", "-of", "default=nk=1:nw=1", path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        frames = result.stdout.strip()
        return frames.isdigit() and int(frames) > 1
    except Exception:
        return False


def convert_avif(path: str, name_root: str, animated: bool):
    if animated:
        gif_path = os.path.join(IMG_CONVERT_DIR, f"{name_root}.gif")
        jpg_path = os.path.join(IMG_CONVERT_DIR, f"{name_root}_first.jpg")

        subprocess.run(["ffmpeg", "-y", "-i", path, gif_path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["ffmpeg", "-y", "-i", path, "-vframes", "1", jpg_path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        print(f"[AVIF→GIF] {gif_path}")
        print(f"[AVIF→JPG] {jpg_path}")
    else:
        out = os.path.join(IMG_CONVERT_DIR, f"{name_root}.jpg")
        subprocess.run(["ffmpeg", "-y", "-i", path, out],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[AVIF→JPG] {out}")


# =======================================================
# 保存图片
# =======================================================
TPLV_IMG_RE = re.compile(r".*[\*~]tplv", re.IGNORECASE)
IMAGE_RE = re.compile(r".*\.(jpg|jpeg|png|gif|bmp|webp|avif|heic)(\?.*)?$", re.IGNORECASE)

DOMAIN_WHITELIST = {
    "pb.plusx.cn",
    "plusx.cn",
    "live.photovision.cn",
}


def save_image(flow: http.HTTPFlow):
    url = flow.request.pretty_url
    data = flow.response.content or b""

    status = flow.response.status_code
    if status not in (200, 206):
        log_unparsed_image(flow, "NON_200_STATUS", f"status={status}")
        return

    if len(data) < 5:
        log_unparsed_image(flow, "EMPTY_OR_TOO_SMALL")
        return

    k = url_key(url)
    if k in SEEN_IMAGE_URL:
        log_unparsed_image(flow, "DUPLICATE_URL")
        return
    SEEN_IMAGE_URL.add(k)

    append_line(IMAGE_URL_LOG, url)

    name_root = extract_original_name(url)
    ext = detect_image_ext(flow, data)
    if ext == "bin":
        log_unparsed_image(flow, "UNKNOWN_FORMAT_BIN")
        return

    final_name = re.sub(r'[\\/:*?"<>|]', "_", f"{name_root}.{ext}")
    save_path = os.path.join(IMG_DIR, final_name)
    save_binary(save_path, data)
    print(f"[IMG SAVE] {save_path}  (fmt={ext}, len={len(data)})")

    if ext == "avif":
        animated = detect_animated_avif(save_path)
        convert_avif(save_path, name_root, animated)


# =======================================================
# HLS：m3u8 & TS
# =======================================================
def save_m3u8_and_download(flow: http.HTTPFlow):
    url = flow.request.pretty_url
    data = flow.response.content or b""
    status = flow.response.status_code

    if status not in (200, 206):
        append_line(VIDEO_ERROR_LOG, f"[NON_200_M3U8] status={status} url={url}")
        return
    if len(data) < 10:
        append_line(VIDEO_ERROR_LOG, f"[SMALL_M3U8] len={len(data)} url={url}")
        return

    k = url_key(url)
    if k in SEEN_VIDEO_URL:
        return
    SEEN_VIDEO_URL.add(k)

    append_line(VIDEO_URL_LOG, url)

    fname = url.split("/")[-1].split("?")[0] or "index.m3u8"
    if not fname.endswith(".m3u8"):
        fname += ".m3u8"

    m3u8_path = os.path.join(M3U8_DIR, fname)
    save_binary(m3u8_path, data)
    print(f"[M3U8 SAVE] {m3u8_path}")

    mp4_name = fname.replace(".m3u8", ".mp4")
    mp4_path = os.path.join(MP4_DIR, mp4_name)

    cmd = ["ffmpeg", "-y", "-i", url, "-c", "copy", mp4_path]
    try:
        subprocess.Popen(cmd)
        print(f"[FFMPEG HLS] start download → {mp4_path}")
    except Exception as e:
        append_line(VIDEO_ERROR_LOG, f"[FFMPEG_HLS_ERROR] {e} url={url}")


def save_ts_segment(flow: http.HTTPFlow):
    url = flow.request.pretty_url
    data = flow.response.content or b""
    if len(data) < 10:
        return

    fname = url.split("/")[-1].split("?")[0] or "segment.ts"
    fname = re.sub(r'[\\/:*?"<>|]', "_", fname)

    save_path = os.path.join(TS_DIR, fname)
    save_binary(save_path, data)
    print(f"[TS SAVE] {save_path} (len={len(data)})")


# =======================================================
# DASH：mpd & m4s
# =======================================================
def save_mpd_and_download(flow: http.HTTPFlow):
    url = flow.request.pretty_url
    data = flow.response.content or b""
    status = flow.response.status_code

    if status not in (200, 206):
        append_line(VIDEO_ERROR_LOG, f"[NON_200_MPD] status={status} url={url}")
        return
    if len(data) < 10:
        append_line(VIDEO_ERROR_LOG, f"[SMALL_MPD] len={len(data)} url={url}")
        return

    k = url_key(url)
    if k in SEEN_VIDEO_URL:
        return
    SEEN_VIDEO_URL.add(k)

    append_line(VIDEO_URL_LOG, url)

    fname = url.split("/")[-1].split("?")[0] or "manifest.mpd"
    if not fname.endswith(".mpd"):
        fname += ".mpd"

    mpd_path = os.path.join(MPD_DIR, fname)
    save_binary(mpd_path, data)
    print(f"[MPD SAVE] {mpd_path}")

    mp4_name = fname.replace(".mpd", ".mp4")
    mp4_path = os.path.join(MP4_DIR, mp4_name)

    cmd = ["ffmpeg", "-y", "-i", url, "-c", "copy", mp4_path]
    try:
        subprocess.Popen(cmd)
        print(f"[FFMPEG DASH] start download → {mp4_path}")
    except Exception as e:
        append_line(VIDEO_ERROR_LOG, f"[FFMPEG_DASH_ERROR] {e} url={url}")


def save_m4s_segment(flow: http.HTTPFlow):
    url = flow.request.pretty_url
    data = flow.response.content or b""
    if len(data) < 10:
        return

    fname = url.split("/")[-1].split("?")[0] or "segment.m4s"
    fname = re.sub(r'[\\/:*?"<>|]', "_", fname)
    save_path = os.path.join(M4S_DIR, fname)
    save_binary(save_path, data)
    print(f"[M4S SAVE] {save_path} (len={len(data)})")


# =======================================================
# ★ MP4 直链：requests 流式下载（新增，模式A证书）
# =======================================================
def is_mp4_candidate(flow: http.HTTPFlow) -> bool:
    url = flow.request.pretty_url.lower()
    ct = (flow.response.headers.get("Content-Type", "") or "").lower()
    if ct.startswith("video/mp4"):
        return True
    if url.endswith(".mp4") or ".mp4?" in url:
        return True
    return False


def pick_download_headers(flow: http.HTTPFlow) -> dict:
    """
    从原始请求头提取复刻下载环境的关键头（防盗链常用）
    """
    h = flow.request.headers
    out = {}

    for k in ["Referer", "Origin", "User-Agent", "Cookie", "Accept"]:
        if k in h and h.get(k):
            out[k] = h.get(k)

    return out


def stream_download_mp4(url: str, headers: dict, out_path: str, timeout=(10, 60), max_retries=3):
    """
    requests 流式下载，支持断点续传（若 .part 已存在则续传）
    模式A：verify 使用 mitmproxy CA（verify_for_url）
    """
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    tmp_path = out_path + ".part"
    existing = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0

    for attempt in range(1, max_retries + 1):
        req_headers = dict(headers or {})

        if existing > 0:
            req_headers["Range"] = f"bytes={existing}-"

        try:
            verify_arg = verify_for_url(url)
            print(f"[MP4 DL] attempt={attempt} existing={existing} verify={verify_arg} url={url[:80]}")

            with requests.get(
                    url,
                    headers=req_headers,
                    stream=True,
                    timeout=timeout,
                    allow_redirects=True,
                    verify=verify_arg,
            ) as r:
                if r.status_code not in (200, 206):
                    raise RuntimeError(f"HTTP {r.status_code}")

                # 若我们请求了 Range，但服务器返回 200（不支持/忽略 Range），就从头下
                if existing > 0 and r.status_code == 200:
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                    existing = 0

                mode = "ab" if existing > 0 else "wb"

                total = None
                cr = r.headers.get("Content-Range")
                if cr and "/" in cr:
                    try:
                        total = int(cr.split("/")[-1])
                    except Exception:
                        total = None
                elif r.headers.get("Content-Length"):
                    try:
                        total = existing + int(r.headers.get("Content-Length"))
                    except Exception:
                        total = None

                downloaded = existing
                last_log = time.time()

                with open(tmp_path, mode) as f:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)

                        now = time.time()
                        if now - last_log >= 2:
                            if total:
                                pct = downloaded * 100 / total
                                print(f"[MP4 DL] {os.path.basename(out_path)}  {pct:.1f}%  ({downloaded}/{total})")
                            else:
                                print(f"[MP4 DL] {os.path.basename(out_path)}  ({downloaded} bytes)")
                            last_log = now

            # 完成：原子替换
            if os.path.exists(out_path):
                os.remove(out_path)
            os.rename(tmp_path, out_path)
            print(f"[MP4 DONE] {out_path}")
            return

        except Exception as e:
            append_line(VIDEO_ERROR_LOG, f"[MP4_DOWNLOAD_ERROR] attempt={attempt} err={e} url={url} out={out_path}")
            print(f"[MP4 ERROR] attempt={attempt} {e}")
            if attempt == max_retries:
                return
            time.sleep(1.5 * attempt)
            existing = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0


def start_mp4_download_once(flow: http.HTTPFlow):
    """
    对同一个 mp4 URL（按路径）只触发一次后台下载，避免 Range 多次触发
    """
    url = flow.request.pretty_url
    status = flow.response.status_code
    ct = (flow.response.headers.get("Content-Type", "") or "").lower()

    if status not in (200, 206):
        return
    if not is_mp4_candidate(flow):
        return

    k = url_key(url)
    if k in SEEN_MP4_URL:
        return
    SEEN_MP4_URL.add(k)

    append_line(VIDEO_URL_LOG, url)

    base = url.split("?")[0].split("/")[-1] or "video.mp4"
    if not base.endswith(".mp4"):
        base += ".mp4"
    base = re.sub(r'[\\/:*?"<>|]', "_", base)

    h = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
    out_path = os.path.join(MP4_DIRECT_DIR, f"{base[:-4]}_{h}.mp4")

    headers = pick_download_headers(flow)

    with DOWNLOADING_LOCK:
        if k in DOWNLOADING:
            return
        DOWNLOADING.add(k)

    def worker():
        try:
            print(f"[MP4 START] {out_path}  ct={ct}  status={status}")
            stream_download_mp4(url, headers, out_path)
        finally:
            with DOWNLOADING_LOCK:
                DOWNLOADING.discard(k)

    threading.Thread(target=worker, daemon=True).start()


# =======================================================
# mitmproxy 回调：响应阶段
# =======================================================
def response(flow: http.HTTPFlow):
    url = flow.request.pretty_url
    host = (urlparse(url).hostname or "").lower()
    content_type = (flow.response.headers.get("Content-Type", "")).lower()

    # 1) 图片：记录所有图片相关 URL
    if is_image_candidate(flow):
        log_all_image_url(flow)

    # 保存图片
    if host in DOMAIN_WHITELIST:
        save_image(flow)
    elif TPLV_IMG_RE.search(url):
        save_image(flow)
    elif IMAGE_RE.match(url):
        save_image(flow)
    elif content_type.startswith("image/"):
        save_image(flow)

    # 2) 视频：记录所有视频相关 URL（HLS + DASH + MP4）
    if is_video_candidate(flow):
        log_all_video_url(flow)

        # MP4 直链：优先处理（公众号常见）
        if is_mp4_candidate(flow):
            start_mp4_download_once(flow)
            return

        # HLS：m3u8
        if (
                content_type.startswith("application/vnd.apple.mpegurl")
                or content_type.startswith("application/x-mpegurl")
                or url.endswith(".m3u8")
                or ".m3u8?" in url
        ):
            save_m3u8_and_download(flow)
            return

        # HLS：ts
        if url.endswith(".ts") or ".ts?" in url or content_type == "video/mp2t":
            save_ts_segment(flow)
            return

        # DASH：mpd
        if (
                url.endswith(".mpd")
                or ".mpd?" in url
                or content_type.startswith("application/dash+xml")
        ):
            save_mpd_and_download(flow)
            return

        # DASH：m4s
        if url.endswith(".m4s") or ".m4s?" in url or ".m4s" in url:
            save_m4s_segment(flow)
            return


# =======================================================
# mitmproxy 回调：请求阶段（anti-cache）
# =======================================================
def request(flow: http.HTTPFlow):
    """
    删除条件缓存头，防止服务器返回 304 Not Modified，
    强制返回 200 + 完整实体内容。
    """
    remove_headers = [
        "If-Modified-Since",
        "If-None-Match",
        "If-Range",
        "Cache-Control",
        "Pragma",
    ]

    modified = False
    for h in remove_headers:
        if h in flow.request.headers:
            flow.request.headers.pop(h, None)
            modified = True

    if modified:
        print(f"[ANTICACHE] Removed cache headers for: {flow.request.pretty_url[:80]}")
