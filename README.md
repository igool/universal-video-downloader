# 📦 安装说明（Installation Guide）

## 1. 必要依赖（Dependencies）

请确保系统已安装以下环境：

### 🐍 **conda**

用于管理 Python 多环境，建议安装 Anaconda 或 Miniconda。

### 🟩 **Node.js**

用于运行本地 JavaScript 执行环境（Chrome 插件辅助逻辑需要）。

### 🎬 **FFmpeg**

负责音视频合并、转码、音频提取。

安装示例（Linux）：

```bash
sudo apt install ffmpeg
```

### 📥 **yt-dlp**

负责解析站点的视频、音频、字幕资源。

安装：

```bash
pip install yt-dlp
```

---

## 2. 安装 Chrome 插件（Chrome Extension）

1. 打开 Chrome 浏览器
2. 访问地址：`chrome://extensions`
3. 开启右上角 **“开发者模式（Developer mode）”**
4. 点击 **“加载已解压的扩展程序（Load unpacked）”**
5. 选择本项目中的 **`chrome-extension/`** 文件夹

插件加载成功后，即可自动捕获网页中的视频 / 音频资源请求。

---

## 3. 安装 Python 依赖（requirements）

创建并激活你准备使用的 Python 环境（建议 conda）：

```bash
conda create -n uvd python=3.9
conda activate uvd
```

安装第三方依赖：

```bash
pip install -r requirements.txt
```

---

## 4. 启动多渠道下载 Server

运行核心服务：

```bash
python server-muavcookie.py
```

该服务整合了：

* 音视频下载
* Cookie 管理
* 多平台支持（YouTube、X、Bilibili、网易云音乐等）
* 任务调度与下载状态管理

Server 启动后会监听来自插件的下载请求。

---

## 5. 使用流程（How to Use）

1. 打开一个含有视频或音频内容的网页
2. 点击浏览器右上角的 Universal Video Downloader 插件图标
3. **先点击「刷新 Cookie」**

   * 确保平台登录态生效
   * 避免无法解析视频源
4. 选择 **“下载音频”** 或 **“下载视频”**
5. 插件会跳转到新的确认页面

   * 在页面中进行下载设置确认
   * 点击「下载」

下载内容会默认保存到：

```
./download/
```
## 6. 执行程序（How to Use）
1. 基于浏览器的音视频下载工具打包
路径：/uvd/uvd_server_gui.py
```
   pyinstaller ^
   --name uvd_server_gui ^
   --onefile ^
   --noconsole ^
   uvd_server_gui.py

```
2. 基于mitmproxy的静默视频图片下载工具打包
路径：/sniffer/wx_sniffer_gui.py，打包的项目地址是https://github.com/igool/wx_sniffer_app
```
pyinstaller -y ^
  --noconsole ^
  --name wx-sniffer ^
  --add-data "wx_sniffer_addon.py;." ^
  wx_sniffer_gui.py
```
---

# ⚠️ 重要提示（Important Notice）

### 1️⃣ 仅限学习使用

本程序 **仅限个人学习、研究使用**，禁止任何商业用途。
因使用本工具造成的账号封禁、违规、法律纠纷等后果，概不负责。

### 2️⃣ 不适合完全零基础用户

本工具适合 **有一定开发能力** 的用户进行轻量使用。
如你完全是小白或需要一对一指导，本人可提供 **适当付费技术支持**。
<img src="https://igool.github.io/images/wecom_qrcode.jpg">
---
