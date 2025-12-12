import zipfile
import re
import os
import shlex
import subprocess
from urllib.parse import urlparse


def parse_saz(saz_path):
    """
    解析 SAZ 文件，提取请求与响应（按 session ID 匹配）
    返回：
        requests:  {rid: {"url":..., "method":..., "headers":{...}}}
        responses: {rid: {"headers":{...}, "content_type":...}}
    """
    requests = {}
    responses = {}

    with zipfile.ZipFile(saz_path, "r") as z:
        namelist = z.namelist()

        # 请求文件 *_c.txt
        req_files = [f for f in namelist if f.endswith("_c.txt")]
        # 响应文件 *_s.txt
        resp_files = [f for f in namelist if f.endswith("_s.txt")]

        # ---- 解析请求 ----
        for rf in req_files:
            rid = rf.split("_")[0]

            raw = z.read(rf).decode("utf-8", "ignore")
            lines = raw.splitlines()

            if not lines:
                continue

            # GET https://xxx.m3u8 HTTP/1.1
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

            requests[rid] = {
                "url": url,
                "method": method,
                "headers": headers
            }

        # ---- 解析响应 ----
        for sf in resp_files:
            rid = sf.split("_")[0]

            raw = z.read(sf).decode("utf-8", "ignore")

            # 提取响应头（第一段）
            header_block = raw.split("\r\n\r\n")[0]
            header_lines = header_block.splitlines()[1:]

            headers = {}
            for line in header_lines:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip()] = v.strip()

            content_type = headers.get("Content-Type", "").lower()

            responses[rid] = {
                "headers": headers,
                "content_type": content_type
            }

    return requests, responses


def find_hls_entries(requests, responses):
    """
    查找所有 m3u8 视频流（基于 Content-Type: application/vnd.apple.mpegurl）
    """
    hls_list = []

    for rid, resp in responses.items():
        ct = resp["content_type"]
        if ct.startswith("application/vnd.apple.mpegurl") or ct.startswith("application/x-mpegurl"):
            if rid in requests:
                hls_list.append(requests[rid])

    return hls_list


def build_ffmpeg_cmd(url, headers, output_path):
    """
    构造 ffmpeg 下载命令（带完整 header）
    """
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


def extract_hls_video(saz_path, output_dir="./output"):
    """
    主执行流程：
    1. 解析 SAZ
    2. 查找 m3u8
    3. 提取 header
    4. 调用 ffmpeg
    """

    os.makedirs(output_dir, exist_ok=True)

    print(f"📦 正在解析 SAZ 文件：{saz_path}")

    requests, responses = parse_saz(saz_path)

    print("🔍 正在查找视频流（m3u8）请求…")

    hls_entries = find_hls_entries(requests, responses)

    if not hls_entries:
        print("❌ 未发现 m3u8 视频流，请检查抓包是否完整。")
        return

    print(f"✔ 找到 {len(hls_entries)} 个 m3u8 视频流请求")

    outputs = []

    for idx, entry in enumerate(hls_entries, 1):
        url = entry["url"]
        headers = entry["headers"]

        # 输出文件名
        out_path = os.path.join(output_dir, f"video_{idx}.mp4")

        print("\n-------------------------------------------")
        print(f"🎬 开始处理视频 {idx}")
        print("-------------------------------------------")
        print(f"📌 m3u8 地址：\n{url}")
        print("\n📌 请求头（将带入 ffmpeg）：")
        for k, v in headers.items():
            print(f"{k}: {v}")

        print("\n🚀 正在调用 ffmpeg 下载…")

        cmd = build_ffmpeg_cmd(url, headers, out_path)
        print("\n执行命令：")
        print(" ".join(shlex.quote(c) for c in cmd))

        subprocess.run(cmd)

        print(f"\n🎉 视频已导出到：{out_path}")
        outputs.append(out_path)

    print("\n=====================================")
    print("   🎉 所有视频已处理完成！")
    print("=====================================")
    for o in outputs:
        print("✔", o)
    print("=====================================")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法：")
        print("  python saz_hls_extractor.py your.saz [output_dir]")
        exit()

    saz_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) >= 3 else "./output"

    extract_hls_video(saz_path, output_dir)
