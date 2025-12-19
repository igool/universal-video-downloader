"""
微信 / H5 / 小程序 媒体嗅探（图片 + HLS + DASH）
修复版：支持 GUI / CLI / EXE 一致输出目录
"""

import os
import re
import hashlib
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime
from mitmproxy import http, ctx

# =======================================================
# 目录结构：优先用 GUI 传入的 WX_SNIFFER_WORKDIR
# =======================================================
WORKDIR = os.environ.get("WX_SNIFFER_WORKDIR", os.getcwd())
WORKDIR = str(Path(WORKDIR).resolve())
BASE_DIR = Path(WORKDIR) / "output"

IMG_DIR = BASE_DIR / "images"
IMG_CONVERT_DIR = IMG_DIR / "converted"

VIDEO_DIR = BASE_DIR / "videos"
M3U8_DIR = VIDEO_DIR / "m3u8"
TS_DIR = VIDEO_DIR / "ts"
MP4_DIR = VIDEO_DIR / "mp4"
MPD_DIR = VIDEO_DIR / "mpd"
M4S_DIR = VIDEO_DIR / "m4s"

IMAGE_URL_LOG = BASE_DIR / "image_urls.txt"
IMAGE_ALL_LOG = BASE_DIR / "image_all_urls.txt"
IMAGE_UNPARSED_LOG = BASE_DIR / "unparsed_debug.txt"

VIDEO_URL_LOG = BASE_DIR / "video_urls.txt"
VIDEO_ALL_LOG = BASE_DIR / "video_all_urls.txt"
VIDEO_ERROR_LOG = BASE_DIR / "video_errors.txt"

for d in [BASE_DIR, IMG_DIR, IMG_CONVERT_DIR,
          VIDEO_DIR, M3U8_DIR, TS_DIR, MP4_DIR, MPD_DIR, M4S_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ✅ 标记文件：确认脚本已加载 & 使用的工作目录
try:
    mark = BASE_DIR / "_addon_loaded.txt"
    mark.write_text(
        f"loaded_at={datetime.now().isoformat()}\n"
        f"pid={os.getpid()}\n"
        f"cwd={os.getcwd()}\n"
        f"workdir_env={os.environ.get('WX_SNIFFER_WORKDIR')}\n",
        encoding="utf-8"
    )
except Exception as e:
    try:
        print("[WX-SNIFFER] write mark failed:", e)
    except Exception:
        pass

try:
    ctx.log.warn(f"[WX-SNIFFER] loaded. WORKDIR={WORKDIR} OUTPUT={BASE_DIR}")
except Exception:
    print(f"[WX-SNIFFER] loaded. WORKDIR={WORKDIR} OUTPUT={BASE_DIR}")

# =======================================================
# 去重集合
# =======================================================
SEEN_IMAGE_URL = set()
SEEN_IMAGE_ALL_URL = set()
SEEN_VIDEO_URL = set()
SEEN_VIDEO_ALL_URL = set()


def url_key(url: str) -> str:
    return url.split("?", 1)[0]


def save_binary(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)


def append_line(path: Path, line: str):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
    except Exception as e:
        print(f"[LOG ERROR] {path}: {e}")


# =======================================================
# 未解析图片调试日志
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
# 图片候选 / 全量日志
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
# 视频候选 / 全量日志（HLS + DASH）
# =======================================================
def is_video_candidate(flow: http.HTTPFlow) -> bool:
    url = flow.request.pretty_url.lower()
    ct = flow.response.headers.get("Content-Type", "").lower()
    if url.endswith(".m3u8") or ".m3u8?" in url:
        return True
    if "m3u8" in url and ("api" in url or "/m3u8/" in url):
        return True
    if ct.startswith("application/vnd.apple.mpegurl") or ct.startswith("application/x-mpegurl"):
        return True
    if url.endswith(".ts") or ".ts?" in url:
        return True
    if ct == "video/mp2t":
        return True
    if url.endswith(".mpd") or ".mpd?" in url:
        return True
    if ct.startswith("application/dash+xml"):
        return True
    if url.endswith(".m4s") or ".m4s?" in url:
        return True
    if ".m4s" in url and (ct.startswith("video/") or ct.startswith("application/octet-stream")):
        return True
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
# 图片格式识别 / 文件名推断
# =======================================================
def detect_magic_ext(data: bytes) -> str:
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
    fmt = fmt.lower()
    mapping = {
        "jpg": "jpg", "jpeg": "jpg", "png": "png", "gif": "gif",
        "webp": "webp", "avif": "avif", "heic": "heic", "heif": "heif",
        "avif2webp": "webp", "heic2webp": "webp", "jpeg2webp": "webp",
        "png2webp": "webp", "avif2avif": "avif",
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


def detect_animated_avif(path: str) -> bool:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=nb_frames",
             "-of", "default=nk=1:nw=1", path],
            capture_output=True, text=True, timeout=10,
        )
        frames = result.stdout.strip()
        return frames.isdigit() and int(frames) > 1
    except Exception:
        return False


def convert_avif(path: str, name_root: str, animated: bool):
    if animated:
        gif_path = IMG_CONVERT_DIR / f"{name_root}.gif"
        jpg_path = IMG_CONVERT_DIR / f"{name_root}_first.jpg"
        subprocess.run(["ffmpeg", "-y", "-i", path, str(gif_path)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["ffmpeg", "-y", "-i", path, "-vframes", "1", str(jpg_path)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[AVIF→GIF] {gif_path}")
        print(f"[AVIF→JPG] {jpg_path}")
    else:
        out = IMG_CONVERT_DIR / f"{name_root}.jpg"
        subprocess.run(["ffmpeg", "-y", "-i", path, str(out)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[AVIF→JPG] {out}")


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
    save_path = IMG_DIR / final_name
    save_binary(save_path, data)
    print(f"[IMG SAVE] {save_path}  (fmt={ext}, len={len(data)})")

    if ext == "avif":
        animated = detect_animated_avif(str(save_path))
        convert_avif(str(save_path), name_root, animated)


# ---------------- HLS / DASH ----------------
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

    m3u8_path = M3U8_DIR / fname
    save_binary(m3u8_path, data)
    print(f"[M3U8 SAVE] {m3u8_path}")

    mp4_name = fname.replace(".m3u8", ".mp4")
    mp4_path = MP4_DIR / mp4_name

    cmd = ["ffmpeg", "-y", "-i", url, "-c", "copy", str(mp4_path)]
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
    save_path = TS_DIR / fname
    save_binary(save_path, data)
    print(f"[TS SAVE] {save_path} (len={len(data)})")


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

    mpd_path = MPD_DIR / fname
    save_binary(mpd_path, data)
    print(f"[MPD SAVE] {mpd_path}")

    mp4_name = fname.replace(".mpd", ".mp4")
    mp4_path = MP4_DIR / mp4_name

    cmd = ["ffmpeg", "-y", "-i", url, "-c", "copy", str(mp4_path)]
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
    save_path = M4S_DIR / fname
    save_binary(save_path, data)
    print(f"[M4S SAVE] {save_path} (len={len(data)})")


TPLV_IMG_RE = re.compile(r".*[\*~]tplv", re.IGNORECASE)
IMAGE_RE = re.compile(r".*\.(jpg|jpeg|png|gif|bmp|webp|avif|heic)(\?.*)?$", re.IGNORECASE)
DOMAIN_WHITELIST = {"pb.plusx.cn", "plusx.cn", "live.photovision.cn"}


def response(flow: http.HTTPFlow):
    url = flow.request.pretty_url
    host = (urlparse(url).hostname or "").lower()
    content_type = (flow.response.headers.get("Content-Type", "")).lower()

    # 图片：先记全量，再按规则保存
    if is_image_candidate(flow):
        log_all_image_url(flow)

    if host in DOMAIN_WHITELIST or TPLV_IMG_RE.search(url) or IMAGE_RE.match(url) or content_type.startswith("image/"):
        save_image(flow)

    # 视频：全量记录 + 分类型处理
    if is_video_candidate(flow):
        log_all_video_url(flow)

        if (
                content_type.startswith("application/vnd.apple.mpegurl")
                or content_type.startswith("application/x-mpegurl")
                or url.endswith(".m3u8")
                or ".m3u8?" in url
        ):
            save_m3u8_and_download(flow)
            return

        if url.endswith(".ts") or ".ts?" in url or content_type == "video/mp2t":
            save_ts_segment(flow)
            return

        if url.endswith(".mpd") or ".mpd?" in url or content_type.startswith("application/dash+xml"):
            save_mpd_and_download(flow)
            return

        if url.endswith(".m4s") or ".m4s?" in url or ".m4s" in url:
            save_m4s_segment(flow)
            return


def request(flow: http.HTTPFlow):
    remove_headers = ["If-Modified-Since", "If-None-Match", "If-Range", "Cache-Control", "Pragma"]
    for h in remove_headers:
        flow.request.headers.pop(h, None)
