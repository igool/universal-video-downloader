import zipfile
import re
import os
import shlex
import subprocess
import requests
from urllib.parse import urlparse


# ================================================================
# 图片识别正则
# ================================================================
IMAGE_PATTERNS = [
    r"https?://[^\"'\s]+\.(?:jpg|jpeg|png|gif|webp|bmp|svg)(?:\?[^\"'\s]*)?",
    r"/[^\"'\s]+\.(?:jpg|jpeg|png|gif|webp|bmp|svg)(?:\?[^\"'\s]*)?",
]


# ================================================================
# SAZ 解析：提取请求 & 响应（包含 headers）
# ================================================================
def parse_saz(saz_path):
    requests_map = {}
    responses_map = {}

    with zipfile.ZipFile(saz_path, "r") as z:
        namelist = z.namelist()

        req_files = [f for f in namelist if f.endswith("_c.txt")]
        resp_files = [f for f in namelist if f.endswith("_s.txt")]

        # ------------ 解析请求(_c.txt) ------------
        for rf in req_files:
            rid = rf.split("_")[0]
            raw = z.read(rf).decode("utf-8", "ignore")
            lines = raw.splitlines()
            if not lines:
                continue

            m = re.match(r"(GET|POST|HEAD|OPTIONS)\s+(\S+)\s+HTTP", lines[0])
            if not m:
                continue

            method = m.group(1)
            url = m.group(2)
            headers = {}

            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip()] = v.strip()

            requests_map[rid] = {
                "url": url,
                "method": method,
                "headers": headers
            }

        # ------------ 解析响应(_s.txt) ------------
        for sf in resp_files:
            rid = sf.split("_")[0]
            raw = z.read(sf).decode("utf-8", "ignore")

            header_block = raw.split("\r\n\r\n")[0]
            header_lines = header_block.splitlines()[1:]

            headers = {}
            for line in header_lines:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip()] = v.strip()

            content_type = headers.get("Content-Type", "").lower()

            responses_map[rid] = {
                "headers": headers,
                "content_type": content_type
            }

    return requests_map, responses_map


# ================================================================
# 查找 m3u8 视频（基于 Content-Type）
# ================================================================
def find_hls_entries(requests_map, responses_map):
    hls_list = []

    for rid, resp in responses_map.items():
        ct = resp["content_type"]
        if (ct.startswith("application/vnd.apple.mpegurl")
                or ct.startswith("application/x-mpegurl")):
            if rid in requests_map:
                hls_list.append(requests_map[rid])

    return hls_list


# ================================================================
# 识别图片 URL
# ================================================================
def extract_image_urls(saz_path):
    urls = set()

    with zipfile.ZipFile(saz_path, "r") as z:
        for name in z.namelist():
            if not (name.endswith(".txt") or name.endswith(".xml")):
                continue

            try:
                raw = z.read(name).decode("utf-8", "ignore")
            except:
                continue

            for pat in IMAGE_PATTERNS:
                for u in re.findall(pat, raw, flags=re.IGNORECASE):
                    urls.add(u)

    return list(urls)


# ================================================================
# 构造完整 URL（处理 /abc/xx.png 这种相对路径）
# ================================================================
def build_full_url(url, headers):
    if url.startswith("http"):
        return url

    host = headers.get("Host")
    if host:
        return f"http://{host}{url}"

    return url


# ================================================================
# 下载图片（带 header）
# ================================================================
def download_image(url, headers, save_dir):
    os.makedirs(save_dir, exist_ok=True)

    filename = urlparse(url).path.split("/")[-1] or "image.bin"
    save_path = os.path.join(save_dir, filename)

    print(f"🖼  图片下载: {url}")

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            with open(save_path, "wb") as f:
                f.write(resp.content)
            print(f"✔ 保存成功: {save_path}")
        else:
            print(f"❌ 下载失败 {resp.status_code}: {url}")
    except Exception as e:
        print(f"❌ 请求错误: {url} --> {e}")

    return save_path


# ================================================================
# 构建 ffmpeg 命令（带完整 header）
# ================================================================
def build_ffmpeg_cmd(url, headers, output_path):
    header_args = []

    for k, v in headers.items():
        header_args.append("-headers")
        header_args.append(f"{k}: {v}")

    cmd = [
        "ffmpeg",
        "-y",
        *header_args,
        "-i", url,
        "-c", "copy",
        output_path
    ]

    return cmd


# ================================================================
# 主流程（视频 + 图片）
# ================================================================
def extract_from_saz(saz_path, output_dir="./output"):
    os.makedirs(output_dir, exist_ok=True)

    print(f"📦 解析 SAZ 文件：{saz_path}")

    requests_map, responses_map = parse_saz(saz_path)

    # -------------------- 视频提取 --------------------
    print("\n🔍 搜索视频流 (m3u8)...")
    hls_entries = find_hls_entries(requests_map, responses_map)

    video_outputs = []

    if hls_entries:
        print(f"✔ 找到 {len(hls_entries)} 个视频流")
    else:
        print("⚠ 未找到视频流")

    for idx, entry in enumerate(hls_entries, 1):
        url = entry["url"]
        headers = entry["headers"]

        out_path = os.path.join(output_dir, f"video_{idx}.mp4")

        print("\n------------------------------------")
        print(f"🎬 导出视频 {idx}")
        print("------------------------------------")

        cmd = build_ffmpeg_cmd(url, headers, out_path)

        print("\n执行 ffmpeg 命令：")
        print(" ".join(shlex.quote(c) for c in cmd))

        subprocess.run(cmd)
        print(f"🎉 视频已保存：{out_path}")

        video_outputs.append(out_path)

    # -------------------- 图片提取 --------------------
    print("\n🔍 正在提取图片 URL...")
    img_urls = extract_image_urls(saz_path)
    print(f"✔ 找到 {len(img_urls)} 张图片")

    image_save_dir = os.path.join(output_dir, "images")
    os.makedirs(image_save_dir, exist_ok=True)

    for img_url in img_urls:
        # 找图片属于哪个请求（需要匹配 header）
        matched_header = None

        for rid, req in requests_map.items():
            # 精确匹配或前缀匹配
            if req["url"] == img_url or img_url.startswith(req["url"]):
                matched_header = req["headers"]
                break

        if not matched_header:
            continue

        full_url = build_full_url(img_url, matched_header)

        download_image(full_url, matched_header, image_save_dir)

    print("\n=====================================")
    print(" 🎉 所有资源已提取完成！")
    print("=====================================")
    print("📽 视频输出：")
    for v in video_outputs:
        print("  ✔", v)
    print("\n🖼 图片输出：")
    print(f"  ✔ {image_save_dir}")
    print("=====================================")


# ================================================================
# 启动入口
# ================================================================
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法： python saz_extractor_full.py your.saz [output_dir]")
        exit()

    saz_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "./output"

    extract_from_saz(saz_path, output_dir)
