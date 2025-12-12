"""
微信 / H5 / 小程序 图片 + 视频流 嗅探旗舰版脚本
================================================
功能一览：
1. 图片抓取：
   - 支持 jpg/png/gif/webp/avif/heic 等
   - 支持字节系 ImageX（含 ~tplv / imagex-fmt / avif2webp / avif2avif）
   - 自动根据 imagex-fmt / Content-Type / 魔数 / URL 后缀 判定扩展名
   - AVIF 自动转 JPG / GIF（动图 AVIF → GIF + 首帧 JPG）

2. 视频抓取：
   - 自动识别 m3u8（Content-Type: application/vnd.apple.mpegurl / x-mpegurl）
   - 保存 m3u8 到 output/videos/m3u8
   - 自动调用 ffmpeg 下载合成 MP4 到 output/videos/mp4
   - 可选保存 TS 切片 output/videos/ts

3. Anti-Cache（避免 304 空响应）：
   - request 阶段删除 If-Modified-Since / If-None-Match / Cache-Control / Pragma 等
   - 尽量强制服务器返回 200 + 完整实体内容

4. 日志体系：
   - output/image_all_urls.txt      → 所有“图片相关请求 URL”（不管是否成功保存，按路径去重）
   - output/image_urls.txt          → 真正进入 save_image 并尝试保存的 URL
   - output/unparsed_debug.txt      → 进入 save_image 但被丢弃 / 未能解析的详细调试信息
   - output/video_all_urls.txt      → 所有“视频相关请求 URL”（m3u8 / ts 等, 去重）
   - output/video_urls.txt          → 真正处理过的 m3u8 URL
   - output/video_errors.txt        → ffmpeg 调用失败、异常等日志

目录结构：
   output/
     images/
       converted/      # AVIF 转 JPG / GIF 输出
     videos/
       m3u8/
       ts/
       mp4/
"""

import os
import re
import hashlib
import subprocess
from urllib.parse import urlparse
from mitmproxy import http

# =======================================================
# 目录结构初始化
# =======================================================
BASE_DIR = "output"

IMG_DIR = os.path.join(BASE_DIR, "images")
IMG_CONVERT_DIR = os.path.join(IMG_DIR, "converted")

VIDEO_DIR = os.path.join(BASE_DIR, "videos")
M3U8_DIR = os.path.join(VIDEO_DIR, "m3u8")
TS_DIR = os.path.join(VIDEO_DIR, "ts")
MP4_DIR = os.path.join(VIDEO_DIR, "mp4")

# 图片相关日志
IMAGE_URL_LOG = os.path.join(BASE_DIR, "image_urls.txt")          # 成功进入 save_image 的 URL
IMAGE_ALL_LOG = os.path.join(BASE_DIR, "image_all_urls.txt")      # 所有图片相关 URL（按路径去重）
IMAGE_UNPARSED_LOG = os.path.join(BASE_DIR, "unparsed_debug.txt") # 进入 save_image 但未成功保存的调试信息

# 视频相关日志
VIDEO_URL_LOG = os.path.join(BASE_DIR, "video_urls.txt")          # 成功处理的 m3u8 URL
VIDEO_ALL_LOG = os.path.join(BASE_DIR, "video_all_urls.txt")      # 所有视频相关 URL（按路径去重）
VIDEO_ERROR_LOG = os.path.join(BASE_DIR, "video_errors.txt")      # ffmpeg / 解析错误信息

for d in [BASE_DIR, IMG_DIR, IMG_CONVERT_DIR, VIDEO_DIR, M3U8_DIR, TS_DIR, MP4_DIR]:
    os.makedirs(d, exist_ok=True)

# =======================================================
# URL 去重（按“路径”去重，忽略 query）
# =======================================================
SEEN_IMAGE_URL = set()       # 只在 save_image 中用
SEEN_IMAGE_ALL_URL = set()   # 所有图片请求记录去重

SEEN_VIDEO_URL = set()       # 已处理过的 m3u8
SEEN_VIDEO_ALL_URL = set()   # 所有视频相关请求记录去重


def url_key(url: str) -> str:
    """以“去掉 query 的 URL 路径”作为 key"""
    return url.split("?", 1)[0]


def save_binary(path, content: bytes):
    """安全写入二进制文件"""
    with open(path, "wb") as f:
        f.write(content)


# =======================================================
# 工具：写文件日志（简单封装）
# =======================================================
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
    """
    判断一个 flow 是否“看起来像图片请求”
    用于记录到 IMAGE_ALL_LOG，不影响是否真正保存。
    """
    url = flow.request.pretty_url.lower()
    ct = flow.response.headers.get("Content-Type", "").lower()

    # 排除一些常见埋点（如百度统计 hm.gif）
    if "hm.baidu.com/hm.gif" in url:
        return False

    # 1) URL 后缀像图片
    if re.search(r"\.(jpg|jpeg|png|gif|bmp|webp|avif|heic|svg)(\?|$)", url):
        return True

    # 2) 含 tplv（字节图床常见参数）
    if "tplv" in url:
        return True

    # 3) Content-Type 像图片
    if ct.startswith("image/"):
        return True

    # 4) 常见微信/图床域关键字
    if any(x in url for x in ["mmbiz", "qlogo.cn", "mmbiz.qpic.cn", "pb.plusx.cn"]):
        return True

    return False


def log_all_image_url(flow: http.HTTPFlow):
    """
    记录所有“图片相关请求 URL”（去重，按路径）
    """
    url = flow.request.pretty_url
    key = url_key(url)
    if key in SEEN_IMAGE_ALL_URL:
        return
    SEEN_IMAGE_ALL_URL.add(key)

    ct = flow.response.headers.get("Content-Type", "").lower()
    append_line(IMAGE_ALL_LOG, f"{url}    [ct={ct}]")


# =======================================================
# 视频候选检测 & 全量 URL 记录
# =======================================================
def is_video_candidate(flow: http.HTTPFlow) -> bool:
    """
    判断是否为“视频相关请求”（m3u8 / ts / video/*）
    """
    url = flow.request.pretty_url.lower()
    ct = flow.response.headers.get("Content-Type", "").lower()

    # m3u8 相关
    if url.endswith(".m3u8") or ".m3u8?" in url:
        return True
    if "m3u8" in url and ("api" in url or "/m3u8/" in url):
        return True
    if ct.startswith("application/vnd.apple.mpegurl") or ct.startswith("application/x-mpegurl"):
        return True

    # TS / HLS 分片
    if url.endswith(".ts") or ".ts?" in url:
        return True
    if ct == "video/mp2t":
        return True

    # 兜底：Content-Type 看起来是视频
    if ct.startswith("video/"):
        return True

    return False


def log_all_video_url(flow: http.HTTPFlow):
    """
    记录所有“视频相关请求 URL”（去重，按路径）
    """
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


# =======================================================
# imagex-fmt → 扩展名映射表（字节跳动 ImageX 专用）
# =======================================================
def ext_from_imagex_fmt(fmt: str) -> str:
    fmt = fmt.lower()
    mapping = {
        "jpg": "jpg",
        "jpeg": "jpg",
        "png": "png",
        "gif": "gif",
        "webp": "webp",
        "avif": "avif",
        "heic": "heic",
        "heif": "heif",

        # 常见转码
        "avif2webp": "webp",
        "heic2webp": "webp",
        "jpeg2webp": "webp",
        "png2webp": "webp",
        "avif2avif": "avif",  # ImageX 再处理一遍 avif
    }

    ext = mapping.get(fmt)
    if ext:
        return ext

    # 兜底规则：xxx2avif / xxx2webp / xxx2jpg / xxx2png
    if fmt.endswith("2avif"):
        return "avif"
    if fmt.endswith("2webp"):
        return "webp"
    if fmt.endswith("2jpg") or fmt.endswith("2jpeg"):
        return "jpg"
    if fmt.endswith("2png"):
        return "png"

    return "bin"


# =======================================================
# URL 后缀 fallback（图片）
# =======================================================
def ext_from_url(url: str):
    m = re.search(r"\.(jpg|jpeg|png|gif|bmp|webp|svg|avif|heic)(\?|$)", url, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    return None


# =======================================================
# 最终统一格式检测器 —— imagex-fmt > Content-Type > Magic > URL
# =======================================================
def detect_image_ext(flow: http.HTTPFlow, data: bytes) -> str:
    headers = flow.response.headers
    url = flow.request.pretty_url
    content_type = headers.get("Content-Type", "").lower()

    # 1) imagex-fmt 最优先
    fmt = headers.get("imagex-fmt")
    if fmt:
        return ext_from_imagex_fmt(fmt)

    # 2) Content-Type
    if content_type.startswith("image/"):
        return content_type.split("/")[1].split(";")[0].lower()

    # 3) Magic Number
    magic = detect_magic_ext(data)
    if magic:
        return magic

    # 4) URL 后缀
    url_ext = ext_from_url(url)
    if url_ext:
        return url_ext

    return "bin"   # 明确标记为未知格式


# =======================================================
# 文件名提取（图片）
# =======================================================
def extract_original_name(url: str) -> str:
    clean = url.split("?")[0]
    parts = clean.split("/")

    # 1) 原图名（DSC/IMGS/IMG/PXL/photo/mmexport）
    for p in parts:
        if re.match(r"(DSC|IMGS|IMG|PXL|photo|mmexport)[A-Za-z0-9_-]+\.", p, re.IGNORECASE):
            return p.split(".")[0]

    # 2) 倒数第二段作为候选
    if len(parts) > 2:
        cand = parts[-2]
        if re.match(r"[A-Za-z0-9_-]{3,}", cand) and "tplv" not in cand:
            return cand

    # 3) 去掉 tplv 参数后的最后一段
    last = re.split(r"[\*~]tplv", parts[-1])[0]
    last = last.split(".")[0]
    if re.match(r"[A-Za-z0-9_-]{3,}", last):
        return last

    # 4) 最终兜底：URL hash
    h = hashlib.md5(clean.encode()).hexdigest()[:10]
    return f"img_{h}"


# =======================================================
# AVIF 动图检测
# =======================================================
def detect_animated_avif(path: str) -> bool:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=nb_frames",
                "-of",
                "default=nk=1:nw=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        frames = result.stdout.strip()
        return frames.isdigit() and int(frames) > 1
    except Exception:
        return False


# =======================================================
# 转换 AVIF → GIF / JPG
# =======================================================
def convert_avif(path: str, name_root: str, animated: bool):
    if animated:
        gif_path = os.path.join(IMG_CONVERT_DIR, f"{name_root}.gif")
        jpg_path = os.path.join(IMG_CONVERT_DIR, f"{name_root}_first.jpg")

        subprocess.run(
            ["ffmpeg", "-y", "-i", path, gif_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-vframes", "1", jpg_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[AVIF→GIF] {gif_path}")
        print(f"[AVIF→JPG] {jpg_path}")
    else:
        out = os.path.join(IMG_CONVERT_DIR, f"{name_root}.jpg")
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, out],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[AVIF→JPG] {out}")


# =======================================================
# 保存图片（主函数）
# =======================================================
def save_image(flow: http.HTTPFlow):

    url = flow.request.pretty_url
    data = flow.response.content or b""

    # 0) 只处理 200 / 206，其他状态写到未解析日志
    status = flow.response.status_code
    if status not in (200, 206):
        log_unparsed_image(flow, "NON_200_STATUS", f"status={status}")
        return

    # 1) 跳过空内容 / 极小内容
    if len(data) < 5:
        log_unparsed_image(flow, "EMPTY_OR_TOO_SMALL")
        return

    # 2) URL 去重（只针对保存）
    k = url_key(url)
    if k in SEEN_IMAGE_URL:
        log_unparsed_image(flow, "DUPLICATE_URL")
        return
    SEEN_IMAGE_URL.add(k)

    # 3) 记录成功进入 save_image 的 URL
    append_line(IMAGE_URL_LOG, url)

    # 4) 生成文件名
    name_root = extract_original_name(url)

    # 5) 检测真实格式
    ext = detect_image_ext(flow, data)

    # 未知格式：只打日志，不保存为图片
    if ext == "bin":
        log_unparsed_image(flow, "UNKNOWN_FORMAT_BIN")
        return

    final_name = re.sub(r'[\\/:*?"<>|]', "_", f"{name_root}.{ext}")
    save_path = os.path.join(IMG_DIR, final_name)

    # 6) 保存原始图片
    save_binary(save_path, data)
    print(f"[IMG SAVE] {save_path}  (fmt={ext}, len={len(data)})")

    # 7) AVIF 自动转换
    if ext == "avif":
        animated = detect_animated_avif(save_path)
        convert_avif(save_path, name_root, animated)


# =======================================================
# 视频：m3u8 & TS 处理
# =======================================================
def save_m3u8_and_download(flow: http.HTTPFlow):
    """
    保存 m3u8 文件，并调用 ffmpeg 将其转为 MP4
    """
    url = flow.request.pretty_url
    data = flow.response.content or b""
    status = flow.response.status_code

    # 只处理 200 / 206
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

    # 文件名
    fname = url.split("/")[-1].split("?")[0] or "index.m3u8"
    if not fname.endswith(".m3u8"):
        fname += ".m3u8"

    m3u8_path = os.path.join(M3U8_DIR, fname)
    save_binary(m3u8_path, data)
    print(f"[M3U8 SAVE] {m3u8_path}")

    # 自动调用 ffmpeg 下载为 MP4
    mp4_name = fname.replace(".m3u8", ".mp4")
    mp4_path = os.path.join(MP4_DIR, mp4_name)

    cmd = [
        "ffmpeg",
        "-y",
        "-i", url,   # 直接用原始 URL，让服务器处理签名和重定向
        "-c", "copy",
        mp4_path
    ]

    try:
        subprocess.Popen(cmd)
        print(f"[FFMPEG] start download → {mp4_path}")
    except Exception as e:
        append_line(VIDEO_ERROR_LOG, f"[FFMPEG_ERROR] {e} url={url}")


def save_ts_segment(flow: http.HTTPFlow):
    """
    保存 TS 切片（可选，用于手工排查 HLS 片段）
    """
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
# URL 匹配规则 / 域名白名单（图片）
# =======================================================
TPLV_IMG_RE = re.compile(r".*[\*~]tplv", re.IGNORECASE)
IMAGE_RE = re.compile(r".*\.(jpg|jpeg|png|gif|bmp|webp|avif|heic)(\?.*)?$", re.IGNORECASE)

DOMAIN_WHITELIST = {
    "pb.plusx.cn",
    "plusx.cn",
    "live.photovision.cn",
    # 这里可以继续加你常见的图片 CDN 域名
}


# =======================================================
# mitmproxy 回调：响应阶段
# =======================================================
def response(flow: http.HTTPFlow):
    url = flow.request.pretty_url
    host = (urlparse(url).hostname or "").lower()
    content_type = (flow.response.headers.get("Content-Type", "")).lower()

    # 1) 图片部分：先记录所有“图片相关 URL”
    if is_image_candidate(flow):
        log_all_image_url(flow)

    # 然后尝试实际保存图片
    if host in DOMAIN_WHITELIST:
        save_image(flow)
    elif TPLV_IMG_RE.search(url):
        save_image(flow)
    elif IMAGE_RE.match(url):
        save_image(flow)
    elif content_type.startswith("image/"):
        save_image(flow)

    # 2) 视频部分：记录所有视频相关 URL
    if is_video_candidate(flow):
        log_all_video_url(flow)

        # m3u8 处理
        if (
            content_type.startswith("application/vnd.apple.mpegurl")
            or content_type.startswith("application/x-mpegurl")
            or url.endswith(".m3u8")
            or ".m3u8?" in url
        ):
            save_m3u8_and_download(flow)
            return

        # ts 片段
        if url.endswith(".ts") or ".ts?" in url or content_type == "video/mp2t":
            save_ts_segment(flow)
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
