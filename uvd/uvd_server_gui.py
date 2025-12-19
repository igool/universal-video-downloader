import os
import sys
import threading
import uuid
import shutil
import traceback
from pathlib import Path
from typing import Dict, Any

from flask import Flask, request, jsonify
from flask_cors import CORS

from PySide6.QtCore import QThread, Signal, Slot
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTextEdit, QLabel, QLineEdit, QFileDialog, QMessageBox
)

import socket
import subprocess
import re

APP_TITLE = "Universal Video Downloader Server GUI"
DEFAULT_PORT = 18888
EXT_ORIGIN = "chrome-extension://cmmeiigobejkpakmfbnmopgcbohgdaol"


# ======================
# 工具函数
# ======================

def default_workdir() -> Path:
    home = Path.home()
    docs = home / "Documents"
    base = docs if docs.exists() else home
    return base / "uvd-server"


def port_is_free(host: str, port: int) -> bool:
    """检测端口是否空闲，仅用于提示"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def find_listening_pid_windows(port: int):
    """在 Windows 下查找占用端口的 PID，用于友好提示"""
    try:
        out = subprocess.check_output(
            ["cmd", "/c", f"netstat -ano | findstr :{port}"],
            text=True, encoding="utf-8", errors="ignore"
        )
    except Exception:
        return None

    for line in out.splitlines():
        if "LISTENING" in line.upper():
            m = re.search(r"\sLISTENING\s+(\d+)\s*$", line, re.IGNORECASE)
            if m:
                return int(m.group(1))
    return None


# ======================
# Flask 应用工厂
# ======================

def create_app(base_dir: Path, gui_log_emit=None) -> Flask:
    """
    把你原来的 Flask 代码封装成一个工厂函数。
    base_dir：用于放 cookies.txt 和 downloads 目录
    gui_log_emit：可选的日志回调，用于在 GUI 中输出日志
    """
    app = Flask(__name__)
    CORS(app, resources={r"/*": {"origins": EXT_ORIGIN}}, supports_credentials=True)

    # 让跨域头固定返回给 Chrome 插件
    @app.after_request
    def after_request(response):
        response.headers["Access-Control-Allow-Origin"] = EXT_ORIGIN
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

    # 路径配置：用 base_dir 替代原来的 __file__ 所在目录
    base_dir = Path(base_dir).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)

    cookie_file = base_dir / "cookies.txt"
    download_dir = base_dir / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)

    # 任务字典
    tasks: Dict[str, Dict[str, Any]] = {}

    def log(msg: str):
        if gui_log_emit:
            gui_log_emit(msg)
        else:
            print(msg)

    # 1. 更新 Cookie
    @app.post("/update_cookie")
    def update_cookie():
        data = request.get_json() or {}
        cookies = data.get("cookies", "")

        if not cookies:
            return {"status": "error", "message": "cookie 为空"}, 400

        with open(cookie_file, "w", encoding="utf-8") as f:
            f.write(cookies)

        log(f"[COOKIE] cookie 已更新，长度={len(cookies)}")
        return {"status": "ok", "message": "cookie 已更新"}

    # 2. URL 平台识别
    def detect_platform(url: str):
        u = url.lower()
        if "youtube" in u or "youtu.be" in u:
            return "youtube"
        if "bilibili" in u:
            return "bilibili"
        if "douyin" in u:
            return "douyin"
        if "tiktok" in u:
            return "tiktok"
        if "instagram" in u:
            return "instagram"
        if "twitter" in u or "x.com" in u:
            return "twitter"
        return "generic"

    # 3. 视频下载参数
    def build_video_opts(platform, task_id, node_path):
        import yt_dlp  # 延迟导入，避免 GUI 启动慢

        opts = {
            "outtmpl": f"{download_dir}/{platform}/%(title)s-%(id)s.%(ext)s",
            "merge_output_format": "mp4",
            "progress_hooks": [lambda d: progress_hook(task_id, d)],
            "cookiefile": str(cookie_file) if cookie_file.exists() else None,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitlesformat": "srt",
            "subtitleslangs": ["auto", "zh-Hans"],
            "retries": 20,
            "extractor_retries": 10,
        }

        # YouTube 需要 nsig 解密
        if platform == "youtube":
            opts.update({
                "format": "bestvideo+bestaudio/best",
                "exec": node_path,
                "extractor_args": {
                    "youtube": {
                        "player_client": ["web", "android", "web_safari"]
                    }
                }
            })

        elif platform == "bilibili":
            opts.update({
                "format": "bestvideo+bestaudio/best",
                "http_headers": {"Referer": "https://www.bilibili.com"}
            })

        elif platform in ["douyin", "tiktok"]:
            opts.update({
                "format": "mp4",
            })

        else:
            opts.update({"format": "best"})

        return opts

    # 4. 音频下载参数
    def build_audio_opts(task_id):
        import yt_dlp  # 延迟导入

        audio_dir = download_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)

        return {
            "format": "bestaudio/best",
            "outtmpl": f"{audio_dir}/%(title)s-%(id)s.%(ext)s",
            "progress_hooks": [lambda d: progress_hook(task_id, d)],
            "cookiefile": str(cookie_file) if cookie_file.exists() else None,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320"
            }],
            "retries": 20,
            "extractor_retries": 10,
        }

    # 5. 进度回调
    def progress_hook(task_id, d):
        if d.get("status") == "downloading":
            tasks[task_id]["progress"] = d.get("_percent_str", "0%")
        elif d.get("status") == "finished":
            tasks[task_id]["progress"] = "100%"

    # 6. 下载线程
    def download_worker(task_id, url, mode):
        import yt_dlp

        platform = detect_platform(url)
        tasks[task_id]["status"] = f"downloading-{mode}"

        node_path = shutil.which("node") or shutil.which("node.exe")

        if mode == "audio":
            ydl_opts = build_audio_opts(task_id)
        else:
            ydl_opts = build_video_opts(platform, task_id, node_path)

        log(f"[TASK] {task_id} 开始下载：{url} mode={mode} platform={platform}")

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            tasks[task_id]["status"] = "finished"
            log(f"[TASK] {task_id} 下载完成")

        except Exception as e:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["error"] = str(e)
            log(f"[TASK] {task_id} 下载失败：{e}")

    # 7. 创建任务
    @app.post("/task/create")
    def create_task():
        req = request.get_json() or {}
        url = req.get("url")
        mode = req.get("mode", "video")  # video 或 audio

        if not url:
            return jsonify({"status": "error", "message": "url 不能为空"}), 400

        task_id = str(uuid.uuid4())
        tasks[task_id] = {
            "task_id": task_id,
            "url": url,
            "mode": mode,
            "platform": detect_platform(url),
            "status": "queued",
            "progress": "0%"
        }

        t = threading.Thread(target=download_worker, args=(task_id, url, mode), daemon=True)
        t.start()

        return jsonify(tasks[task_id])

    # 8. 查询任务
    @app.get("/task/<task_id>")
    def get_task(task_id):
        if task_id not in tasks:
            return jsonify({"error": "task not found"}), 404
        return jsonify(tasks[task_id])

    log(f"[SERVER] Flask app 初始化完成，base_dir={base_dir}")
    return app


# ======================
# Flask 服务线程
# ======================

class FlaskServerThread(QThread):
    log = Signal(str)
    stopped = Signal(int)  # 0=正常停止，1=异常

    def __init__(self, base_dir: Path, host: str, port: int):
        super().__init__()
        self.base_dir = Path(base_dir)
        self.host = host
        self.port = port
        self._server = None
        self._ctx = None

    def _emit_log(self, msg: str):
        self.log.emit(msg)

    def run(self):
        try:
            # 创建 Flask app
            app = create_app(self.base_dir, gui_log_emit=self._emit_log)

            from werkzeug.serving import make_server
            self._server = make_server(self.host, self.port, app)
            self._ctx = app.app_context()
            self._ctx.push()

            self.log.emit("========================================")
            self.log.emit(f"[SERVER] Flask 启动中：http://{self.host}:{self.port}")
            self.log.emit(f"[SERVER] 工作目录: {self.base_dir}")
            self.log.emit("========================================")

            # 阻塞式循环，直到 shutdown() 被调用
            self._server.serve_forever()
            self.stopped.emit(0)

        except OSError as e:
            self.log.emit(f"[ERROR] 端口 {self.port} 可能被占用：{e}")
            self.stopped.emit(1)
        except Exception:
            self.log.emit("[ERROR] Flask 服务器异常退出：")
            self.log.emit(traceback.format_exc())
            self.stopped.emit(1)
        finally:
            try:
                if self._ctx is not None:
                    self._ctx.pop()
            except Exception:
                pass

    def stop(self):
        if self._server is not None:
            try:
                self.log.emit("[SERVER] 收到停止指令，正在关闭 Flask ...")
                self._server.shutdown()
            except Exception as e:
                self.log.emit(f"[WARN] 关闭服务器失败：{e}")


# ======================
# GUI 主窗口
# ======================

from PySide6.QtWidgets import QTextEdit  # 放在上面也行，这里只是保证导入

class UvdGui(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(900, 650)

        self.server_thread: FlaskServerThread | None = None

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(
            "🔧 Universal Video Downloader Server\n"
            "· 启动本地 Flask 服务，供浏览器插件调用\n"
            "· 支持视频 / 音频下载，自动保存到工作目录\n"
        ))

        # 工作目录
        row = QHBoxLayout()
        row.addWidget(QLabel("工作目录:"))
        self.workdir_edit = QLineEdit(str(default_workdir()))
        row.addWidget(self.workdir_edit)

        btn_pick = QPushButton("选择…")
        btn_pick.clicked.connect(self.pick_workdir)
        row.addWidget(btn_pick)

        btn_open = QPushButton("打开 downloads")
        btn_open.clicked.connect(self.open_download_dir)
        row.addWidget(btn_open)

        layout.addLayout(row)

        # 端口设置
        row_port = QHBoxLayout()
        row_port.addWidget(QLabel("监听端口:"))
        self.port_edit = QLineEdit(str(DEFAULT_PORT))
        self.port_edit.setFixedWidth(80)
        row_port.addWidget(self.port_edit)
        row_port.addStretch(1)
        layout.addLayout(row_port)

        # 控制按钮
        row_btn = QHBoxLayout()
        self.btn_start = QPushButton("启动服务")
        self.btn_stop = QPushButton("停止服务")
        self.btn_stop.setEnabled(False)

        self.btn_start.clicked.connect(self.start_server)
        self.btn_stop.clicked.connect(self.stop_server)

        row_btn.addWidget(self.btn_start)
        row_btn.addWidget(self.btn_stop)
        layout.addLayout(row_btn)

        # 日志
        layout.addWidget(QLabel("运行日志："))
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        layout.addWidget(self.log_box)

    def append_log(self, text: str):
        self.log_box.append(text.rstrip("\n"))

    # ---- 槽函数 ----

    @Slot()
    def pick_workdir(self):
        d = QFileDialog.getExistingDirectory(self, "选择工作目录", self.workdir_edit.text())
        if d:
            self.workdir_edit.setText(d)

    @Slot()
    def open_download_dir(self):
        base = Path(self.workdir_edit.text() or default_workdir()).resolve()
        dl = base / "downloads"
        dl.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(str(dl))  # noqa
        elif sys.platform == "darwin":
            os.system(f'open "{dl}"')
        else:
            os.system(f'xdg-open "{dl}"')

    @Slot()
    def start_server(self):
        if self.server_thread and self.server_thread.isRunning():
            QMessageBox.information(self, "提示", "服务已在运行")
            return

        base_dir = Path(self.workdir_edit.text().strip() or default_workdir()).resolve()
        base_dir.mkdir(parents=True, exist_ok=True)

        port_text = self.port_edit.text().strip() or str(DEFAULT_PORT)
        try:
            port = int(port_text)
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            QMessageBox.critical(self, "端口错误", f"无效的端口号：{port_text}")
            return

        # 端口占用检查
        if not port_is_free("127.0.0.1", port):
            pid = find_listening_pid_windows(port) if sys.platform.startswith("win") else None
            msg = f"端口 {port} 可能已被占用。\n"
            if pid:
                msg += f"占用 PID = {pid}\n"
            msg += "请更换端口或先关闭占用该端口的程序。"
            QMessageBox.warning(self, "端口占用", msg)
            # 可以允许继续启动（例如只是提示），这里选择直接返回：
            return

        self.append_log("========================================")
        self.append_log(f"[GUI] 即将启动 Flask 服务: http://127.0.0.1:{port}")
        self.append_log(f"[GUI] 工作目录: {base_dir}")
        self.append_log("========================================")

        self.server_thread = FlaskServerThread(base_dir, "127.0.0.1", port)
        self.server_thread.log.connect(self.append_log)
        self.server_thread.stopped.connect(self.on_server_stopped)
        self.server_thread.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)

    @Slot()
    def stop_server(self):
        if not self.server_thread:
            return
        self.append_log("[GUI] 请求停止 Flask 服务...")
        self.server_thread.stop()

    @Slot(int)
    def on_server_stopped(self, code: int):
        self.append_log(f"[GUI] Flask 服务已退出，code={code}")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.server_thread = None


# ======================
# 入口
# ======================

def main():
    try:
        app = QApplication(sys.argv)
        w = UvdGui()
        w.show()
        sys.exit(app.exec())
    except Exception:
        err = traceback.format_exc()
        try:
            Path("uvd_gui_error.log").write_text(err, encoding="utf-8")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
