import os
import sys
import traceback
import socket
import subprocess
import re
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal, Slot
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTextEdit, QLabel, QLineEdit, QFileDialog, QMessageBox
)

APP_TITLE = "微信媒体嗅探 GUI（内置 PythonRuntime + mitmdump）"
DEFAULT_PORT = 8080  # 仅用于占用检测提示，真正监听端口由 mitmproxy 决定


# ------------------ 路径与环境工具 ------------------ #

def app_base_dir() -> Path:
    """
    程序运行的基础目录：
    - 打包后：exe 所在目录（sys.executable.parent）
    - 源码运行：当前文件所在目录
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_runtime_root() -> Path:
    """
    内置运行时根目录：默认 ./python_runtime
    """
    return app_base_dir() / "python_runtime"


def get_runtime_mitmdump_exe() -> Path:
    """
    返回内置 mitmdump 的路径：
    期望在 ./python_runtime/Scripts/mitmdump.exe
    """
    root = get_runtime_root()
    return root / "Scripts" / "mitmdump.exe"


def default_workdir() -> str:
    home = Path.home()
    docs = home / "Documents"
    base = docs if docs.exists() else home
    return str(base / "wx-sniffer")


def resource_path(rel_path: str) -> Path:
    """
    嗅探脚本路径：
    期望 wx_sniffer_addon.py 与 exe 同目录。
    """
    base = app_base_dir()
    return base / rel_path


def port_is_free(host: str, port: int) -> bool:
    """检测端口是否空闲，仅用于提示（不强制）。"""
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


def find_listening_pid_windows(port: int) -> Optional[int]:
    """辅助：在 Windows 下定位占用端口的进程 ID。"""
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


# ------------------ 子进程 Runner ------------------ #

class MitmProcessRunner(QThread):
    """
    使用内置 mitmdump 运行：
        python_runtime/Scripts/mitmdump.exe -s wx_sniffer_addon.py

    完全不依赖系统 / conda 环境。
    """
    log = Signal(str)
    stopped = Signal(int)  # 0=正常退出，1=异常

    def __init__(self, workdir: str, addon_script_path: str, mitmdump_exe: Path):
        super().__init__()
        self.workdir = workdir
        self.addon_script_path = addon_script_path
        self.mitmdump_exe = mitmdump_exe
        self._proc: Optional[subprocess.Popen] = None

    def stop(self):
        """请求停止子进程。"""
        if self._proc and self._proc.poll() is None:
            try:
                self.log.emit("[INFO] 尝试终止 mitmdump 进程...")
                self._proc.terminate()
            except Exception as e:
                self.log.emit(f"[WARN] 停止进程失败: {e}")

    def run(self):
        try:
            os.makedirs(self.workdir, exist_ok=True)

            # 把工作目录通过环境变量传给 wx_sniffer_addon.py
            env = os.environ.copy()
            env["WX_SNIFFER_WORKDIR"] = self.workdir

            cmd = [
                str(self.mitmdump_exe),
                "-s",
                self.addon_script_path,
            ]

            self.log.emit("========================================")
            self.log.emit(f"[CMD] {' '.join(cmd)}")
            self.log.emit(f"[ENV] WX_SNIFFER_WORKDIR={self.workdir}")
            self.log.emit(f"[INFO] 子进程工作目录: {self.workdir}")
            self.log.emit("========================================")

            creationflags = 0
            if sys.platform.startswith("win"):
                # 隐藏黑框（可选）
                creationflags = subprocess.CREATE_NO_WINDOW

            self._proc = subprocess.Popen(
                cmd,
                cwd=self.workdir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )

            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                line = line.rstrip("\n")
                if line:
                    self.log.emit(line)

            ret = self._proc.wait()
            self._proc = None
            if ret == 0:
                self.stopped.emit(0)
            else:
                self.log.emit(f"[ERROR] mitmdump 退出码: {ret}")
                self.stopped.emit(1)

        except FileNotFoundError:
            self.log.emit(
                f"[ERROR] 未找到内置 mitmdump：{self.mitmdump_exe}\n"
                f"请确认 python_runtime/Scripts/mitmdump.exe 存在。"
            )
            self.stopped.emit(1)
        except Exception:
            err = traceback.format_exc()
            self.log.emit("[ERROR] 启动或运行 mitmdump 失败：")
            self.log.emit(err)
            self.stopped.emit(1)
        finally:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
            self._proc = None


# ------------------ GUI 主窗口 ------------------ #

class MitmGui(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(980, 700)

        self.runner: Optional[MitmProcessRunner] = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "说明：\n"
            "1️⃣ 本工具使用内置 python_runtime/Scripts/mitmdump.exe 运行抓包。\n"
            "2️⃣ 监听地址与端口使用 mitmproxy 默认配置（一般为 0.0.0.0:8080）。\n"
            "3️⃣ 所有抓到的图片/视频会保存到【工作目录/output】下。\n"
            "4️⃣ 请确认系统/手机代理已指向 127.0.0.1:8080，且证书已信任。"
        ))

        # --- 工作目录 ---
        row = QHBoxLayout()
        row.addWidget(QLabel("工作目录:"))
        self.workdir_edit = QLineEdit(default_workdir())
        row.addWidget(self.workdir_edit)

        btn_pick = QPushButton("选择…")
        btn_pick.clicked.connect(self.pick_workdir)
        row.addWidget(btn_pick)

        btn_open = QPushButton("打开 output")
        btn_open.clicked.connect(self.open_output_dir)
        row.addWidget(btn_open)

        layout.addLayout(row)

        # --- 显示内置 mitmdump 路径（只读） ---
        row_md = QHBoxLayout()
        row_md.addWidget(QLabel("内置 mitmdump:"))
        self.mitmdump_label = QLineEdit(str(get_runtime_mitmdump_exe()))
        self.mitmdump_label.setReadOnly(True)
        row_md.addWidget(self.mitmdump_label)
        layout.addLayout(row_md)

        # --- 控制按钮 ---
        row2 = QHBoxLayout()
        self.btn_start = QPushButton("启动抓包")
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self.start_mitm)
        self.btn_stop.clicked.connect(self.stop_mitm)
        row2.addWidget(self.btn_start)
        row2.addWidget(self.btn_stop)
        layout.addLayout(row2)

        # --- 日志窗口 ---
        layout.addWidget(QLabel("实时日志（包含 mitmdump 输出）："))
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        layout.addWidget(self.log_box)

    def append_log(self, s: str):
        self.log_box.append(s.rstrip("\n"))

    @Slot()
    def pick_workdir(self):
        d = QFileDialog.getExistingDirectory(self, "选择工作目录", self.workdir_edit.text())
        if d:
            self.workdir_edit.setText(d)

    @Slot()
    def open_output_dir(self):
        wd = Path(self.workdir_edit.text()).resolve()
        out_dir = wd / "output"
        out_dir.mkdir(parents=True, exist_ok=True)

        if sys.platform.startswith("win"):
            os.startfile(str(out_dir))  # noqa
        elif sys.platform == "darwin":
            os.system(f'open "{out_dir}"')
        else:
            os.system(f'xdg-open "{out_dir}"')

    @Slot()
    def start_mitm(self):
        if self.runner and self.runner.isRunning():
            QMessageBox.information(self, "提示", "抓包已在运行")
            return

        workdir = str(Path(self.workdir_edit.text().strip() or default_workdir()).resolve())
        os.makedirs(workdir, exist_ok=True)

        # 检查默认端口是否已被占用，仅提示用
        if not port_is_free("127.0.0.1", DEFAULT_PORT):
            pid = find_listening_pid_windows(DEFAULT_PORT) if sys.platform.startswith("win") else None
            msg = f"警告：默认端口 {DEFAULT_PORT} 可能已被其他进程占用。\n"
            if pid:
                msg += f"占用 PID = {pid}\n"
            msg += "如果占用进程是你手动启动的 mitmproxy，可以忽略，否则建议先关闭占用进程。"
            QMessageBox.warning(self, "端口可能冲突", msg)

        mitmdump_exe = get_runtime_mitmdump_exe()
        if not mitmdump_exe.exists():
            QMessageBox.critical(
                self,
                "缺少内置 mitmdump",
                f"未找到内置 mitmdump 可执行文件：\n{mitmdump_exe}\n\n"
                "请确认 python_runtime/Scripts/mitmdump.exe 存在。"
            )
            return

        addon_path = resource_path("wx_sniffer_addon.py")
        if not addon_path.exists():
            QMessageBox.critical(
                self, "缺少脚本",
                f"未找到 wx_sniffer_addon.py\n实际路径：{addon_path}\n\n"
                "请将嗅探脚本命名为 wx_sniffer_addon.py，并放在程序同目录。"
            )
            return

        self.append_log("========================================")
        self.append_log("[START] internal mitmdump")
        self.append_log(f"[INFO] workdir      = {workdir}")
        self.append_log(f"[INFO] addon       = {addon_path}")
        self.append_log(f"[INFO] mitmdump_exe= {mitmdump_exe}")
        self.append_log("[INFO] mitmproxy 将使用自己的默认监听地址与端口（一般为 0.0.0.0:8080）")
        self.append_log("========================================")

        self.runner = MitmProcessRunner(workdir, str(addon_path), mitmdump_exe)
        self.runner.log.connect(self.append_log)
        self.runner.stopped.connect(self.on_runner_stopped)
        self.runner.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)

    @Slot()
    def stop_mitm(self):
        if not self.runner:
            return
        self.append_log("[STOP] 请求停止 mitmdump...")
        self.runner.stop()

    @Slot(int)
    def on_runner_stopped(self, code: int):
        self.append_log(f"[EXIT] mitmdump exited. code={code}")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.runner = None


# ------------------ 入口 ------------------ #

def main():
    try:
        app = QApplication(sys.argv)
        w = MitmGui()
        w.show()
        sys.exit(app.exec())
    except Exception:
        err = traceback.format_exc()
        try:
            Path("startup_error.log").write_text(err, encoding="utf-8")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
