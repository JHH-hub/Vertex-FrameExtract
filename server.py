#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
序列帧抽帧工具 - Web 版后端
Flask + WebSocket 实现
"""

import os
import sys
import json
import time
import base64
import shutil
import threading
import subprocess
from pathlib import Path
from typing import List, Optional
from io import BytesIO

# ---- 自动安装缺失依赖 ----
def _ensure_deps():
    required = {
        'cv2': 'opencv-python',
        'numpy': 'numpy',
        'PIL': 'Pillow',
        'flask': 'flask',
        'flask_socketio': 'flask-socketio',
    }
    missing = []
    for mod, pkg in required.items():
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"[自动安装] 缺失依赖: {', '.join(missing)}")
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q'] + missing)

_ensure_deps()

import cv2
import numpy as np
from PIL import Image
from flask import Flask, render_template, request, jsonify, send_from_directory, send_file
from flask_socketio import SocketIO
import re
import webbrowser

# ---- PyInstaller 打包兼容：资源路径 ----
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
    _bundle_dir = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _bundle_dir = BASE_DIR

app = Flask(__name__,
            static_folder=os.path.join(_bundle_dir, 'static'),
            template_folder=os.path.join(_bundle_dir, 'templates'))
app.config['SECRET_KEY'] = 'frame-extractor-2026'


# ---- 中文路径兼容：OpenCV 不支持非 ASCII 路径 ----
def cv_imread(path, flags=cv2.IMREAD_COLOR):
    """cv2.imread 中文路径兼容"""
    data = np.fromfile(path, dtype=np.uint8)
    return cv2.imdecode(data, flags)

def cv_open_video(path):
    """cv2.VideoCapture 中文路径兼容"""
    # 优先尝试直接打开
    cap = cv2.VideoCapture(path)
    if cap.isOpened():
        return cap
    # 中文路径 fallback：复制到临时文件
    import tempfile
    ext = os.path.splitext(path)[1]
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False, dir=os.environ.get('TEMP'))
    tmp_path = tmp.name
    tmp.close()
    shutil.copy2(path, tmp_path)
    cap = cv2.VideoCapture(tmp_path)
    cap._tmp_path = tmp_path  # 记录临时文件路径，便于清理
    return cap
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2GB 上传限制
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ============ 帧提取核心 ============

class FrameExtractor:
    """帧提取核心类"""

    def __init__(self):
        self.cancel_flag = False

    def extract_from_video(self, video_path, output_dir, mode, params, callback=None):
        cap = cv_open_video(video_path)
        if not cap.isOpened():
            raise ValueError("无法打开视频文件")

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        os.makedirs(output_dir, exist_ok=True)

        if mode == 'fixed':
            selected = self._select_fixed(total_frames, params['interval'])
        elif mode == 'custom':
            selected = self._select_custom(total_frames, params['skip'], params['take'])
        elif mode == 'smart':
            selected = self._select_smart_video(cap, total_frames, params['threshold'], callback)
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        else:
            selected = list(range(total_frames))

        saved = 0
        prefix = params.get('prefix', 'frame_')
        digits = params.get('digits', 4)
        compression = params.get('compression', 6)

        for i, idx in enumerate(selected):
            if self.cancel_flag:
                cap.release()
                return {'status': 'cancelled'}

            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if ret:
                fname = f"{prefix}{str(saved).zfill(digits)}.png"
                fpath = os.path.join(output_dir, fname)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb)
                img.save(fpath, 'PNG', compress_level=compression)
                saved += 1

            if callback:
                callback((i + 1) / len(selected), f"提取帧 {saved}/{len(selected)}")

        cap.release()
        return {
            'status': 'success',
            'total_input': total_frames,
            'total_output': saved,
            'compression_rate': round((1 - saved / total_frames) * 100, 1) if total_frames > 0 else 0,
            'fps': fps,
            'width': width,
            'height': height,
            'selected_indices': selected
        }

    def extract_from_sequence(self, input_dir, output_dir, mode, params, callback=None):
        files = self._get_sorted_images(input_dir)
        if not files:
            raise ValueError("未找到图片文件")

        total = len(files)
        os.makedirs(output_dir, exist_ok=True)

        if mode == 'fixed':
            selected = self._select_fixed(total, params['interval'])
        elif mode == 'custom':
            selected = self._select_custom(total, params['skip'], params['take'])
        elif mode == 'smart':
            selected = self._select_smart_sequence(files, params['threshold'], callback)
        else:
            selected = list(range(total))

        saved = 0
        prefix = params.get('prefix', 'frame_')
        digits = params.get('digits', 4)
        compression = params.get('compression', 6)
        size_in = 0
        size_out = 0

        for i, idx in enumerate(selected):
            if self.cancel_flag:
                return {'status': 'cancelled'}

            src = files[idx]
            size_in += os.path.getsize(src)
            fname = f"{prefix}{str(saved).zfill(digits)}.png"
            fpath = os.path.join(output_dir, fname)

            img = Image.open(src)
            if img.mode != 'RGBA':
                img = img.convert('RGB')
            img.save(fpath, 'PNG', compress_level=compression)
            size_out += os.path.getsize(fpath)
            saved += 1

            if callback:
                callback((i + 1) / len(selected), f"处理帧 {saved}/{len(selected)}")

        # 获取第一张图的尺寸
        first = Image.open(files[0])
        w, h = first.size

        return {
            'status': 'success',
            'total_input': total,
            'total_output': saved,
            'compression_rate': round((1 - saved / total) * 100, 1) if total > 0 else 0,
            'size_input_mb': round(size_in / (1024 * 1024), 2),
            'size_output_mb': round(size_out / (1024 * 1024), 2),
            'width': w,
            'height': h,
            'selected_indices': selected
        }

    def analyze_input(self, input_type, input_path):
        """分析输入源，返回信息和预览缩略图"""
        if input_type == 'video':
            cap = cv_open_video(input_path)
            if not cap.isOpened():
                raise ValueError("无法打开视频文件")

            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            duration = total / fps if fps > 0 else 0

            # 取几个均匀分布的缩略图
            thumbs = []
            sample_count = min(total, 12)
            indices = [int(i * total / sample_count) for i in range(sample_count)]
            for idx in indices:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ret, frame = cap.read()
                if ret:
                    thumbs.append(self._frame_to_thumb(frame, max_w=160))
            cap.release()

            return {
                'type': 'video',
                'total_frames': total,
                'fps': round(fps, 2),
                'width': w,
                'height': h,
                'duration': round(duration, 2),
                'thumbnails': thumbs,
                'sample_indices': indices
            }
        else:
            files = self._get_sorted_images(input_path)
            if not files:
                raise ValueError("未找到图片文件")

            total = len(files)
            first = Image.open(files[0])
            w, h = first.size

            thumbs = []
            sample_count = min(total, 12)
            indices = [int(i * total / sample_count) for i in range(sample_count)]
            for idx in indices:
                img = Image.open(files[idx])
                thumbs.append(self._pil_to_thumb(img, max_w=160))

            return {
                'type': 'sequence',
                'total_frames': total,
                'width': w,
                'height': h,
                'thumbnails': thumbs,
                'sample_indices': indices
            }

    def get_preview_frames(self, input_type, input_path, mode, params, max_frames=60):
        """获取预览帧序列（用于动画预览）"""
        if input_type == 'video':
            cap = cv_open_video(input_path)
            if not cap.isOpened():
                raise ValueError("无法打开视频文件")
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS)
        else:
            files = self._get_sorted_images(input_path)
            if not files:
                raise ValueError("未找到图片文件")
            total = len(files)
            fps = 24  # 默认帧率

        # 计算选中帧
        if mode == 'fixed':
            selected = self._select_fixed(total, params['interval'])
        elif mode == 'custom':
            selected = self._select_custom(total, params['skip'], params['take'])
        elif mode == 'smart':
            if input_type == 'video':
                selected = self._select_smart_video(cap, total, params['threshold'])
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            else:
                selected = self._select_smart_sequence(files, params['threshold'])
        else:
            selected = list(range(total))

        all_indices = list(range(total))

        # 限制预览帧数量（取均匀子集），避免传输太多数据
        def subsample(lst, max_n):
            if len(lst) <= max_n:
                return lst
            step = len(lst) / max_n
            return [lst[int(i * step)] for i in range(max_n)]

        preview_all = subsample(all_indices, max_frames)
        preview_selected = subsample(selected, max_frames)

        # 生成缩略图
        original_thumbs = []
        extracted_thumbs = []

        if input_type == 'video':
            for idx in preview_all:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ret, frame = cap.read()
                if ret:
                    original_thumbs.append(self._frame_to_thumb(frame, max_w=240))
            for idx in preview_selected:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ret, frame = cap.read()
                if ret:
                    extracted_thumbs.append(self._frame_to_thumb(frame, max_w=240))
            cap.release()
        else:
            for idx in preview_all:
                img = Image.open(files[idx])
                original_thumbs.append(self._pil_to_thumb(img, max_w=240))
            for idx in preview_selected:
                img = Image.open(files[idx])
                extracted_thumbs.append(self._pil_to_thumb(img, max_w=240))

        return {
            'total_input': total,
            'total_output': len(selected),
            'compression_rate': round((1 - len(selected) / total) * 100, 1) if total > 0 else 0,
            'fps': fps,
            'original_frames': original_thumbs,
            'extracted_frames': extracted_thumbs,
            'selected_indices': selected
        }

    # ---- 抽帧算法 ----

    def _select_fixed(self, total, interval):
        return list(range(0, total, interval))

    def _select_custom(self, total, skip, take):
        sel = []
        i = 0
        while i < total:
            for j in range(take):
                if i + j < total:
                    sel.append(i + j)
            i += take + skip
        return sel

    def _select_smart_video(self, cap, total, threshold, callback=None):
        selected = [0]
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ret, prev = cap.read()
        if not ret:
            return selected
        prev_gray = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)
        prev_gray = cv2.resize(prev_gray, (320, 240))

        for i in range(1, total):
            if self.cancel_flag:
                break
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ret, frame = cap.read()
            if not ret:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.resize(gray, (320, 240))
            diff = np.mean(cv2.absdiff(prev_gray, gray)) / 255.0
            if diff >= threshold:
                selected.append(i)
                prev_gray = gray
            if callback and i % 20 == 0:
                callback(i / total * 0.5, f"分析帧差异 {i}/{total}")
        return selected

    def _select_smart_sequence(self, files, threshold, callback=None):
        selected = [0]
        prev = cv_imread(files[0], cv2.IMREAD_GRAYSCALE)
        if prev is None:
            return selected
        prev = cv2.resize(prev, (320, 240))
        total = len(files)

        for i in range(1, total):
            if self.cancel_flag:
                break
            img = cv_imread(files[i], cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            img = cv2.resize(img, (320, 240))
            diff = np.mean(cv2.absdiff(prev, img)) / 255.0
            if diff >= threshold:
                selected.append(i)
                prev = img
            if callback and i % 10 == 0:
                callback(i / total * 0.5, f"分析帧差异 {i}/{total}")
        return selected

    # ---- 工具方法 ----

    def _get_sorted_images(self, directory):
        exts = {'.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.tga', '.webp'}
        files = []
        for f in os.listdir(directory):
            if Path(f).suffix.lower() in exts:
                files.append(os.path.join(directory, f))
        files.sort(key=lambda x: self._extract_number(x))
        return files

    def _extract_number(self, path):
        nums = re.findall(r'\d+', os.path.basename(path))
        return int(nums[-1]) if nums else 0

    def _frame_to_thumb(self, frame, max_w=160):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        return self._pil_to_thumb(img, max_w)

    def _pil_to_thumb(self, img, max_w=160):
        w, h = img.size
        if w > max_w:
            ratio = max_w / w
            img = img.resize((max_w, int(h * ratio)), Image.LANCZOS)
        buf = BytesIO()
        if img.mode == 'RGBA':
            img.save(buf, format='PNG', compress_level=6)
            mime = 'image/png'
        else:
            img.convert('RGB').save(buf, format='JPEG', quality=80)
            mime = 'image/jpeg'
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:{mime};base64,{b64}"

    def cancel(self):
        self.cancel_flag = True


extractor = FrameExtractor()

# ============ 路由 ============

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/browse', methods=['POST'])
def browse():
    """供前端发起浏览对话框 - 返回路径建议"""
    data = request.json
    browse_type = data.get('type', 'directory')  # 'directory' or 'file'
    path = data.get('path', '')
    
    if not path:
        path = os.path.expanduser('~')
    
    if not os.path.exists(path):
        path = os.path.expanduser('~')
    
    if os.path.isfile(path):
        path = os.path.dirname(path)
    
    items = []
    try:
        for entry in sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.name.startswith('.'):
                continue
            if entry.is_dir():
                items.append({'name': entry.name, 'type': 'dir', 'path': entry.path})
            elif browse_type == 'file':
                ext = Path(entry.name).suffix.lower()
                if ext in {'.mp4', '.avi', '.mov', '.mkv', '.flv', '.webm'}:
                    size_mb = entry.stat().st_size / (1024 * 1024)
                    items.append({'name': entry.name, 'type': 'file', 'path': entry.path, 'size': f'{size_mb:.1f} MB'})
    except PermissionError:
        pass

    parent = str(Path(path).parent)
    return jsonify({'current': path, 'parent': parent, 'items': items})


@app.route('/api/drives', methods=['GET'])
def drives():
    """获取可用驱动器列表"""
    drive_list = []
    if sys.platform == 'win32':
        import string
        for letter in string.ascii_uppercase:
            drive = f'{letter}:\\'
            if os.path.exists(drive):
                drive_list.append(drive)
    else:
        drive_list = ['/']
    return jsonify({'drives': drive_list})


# ============ 文件上传（拖拽） ============

UPLOAD_DIR = os.path.join(BASE_DIR, '_uploads')

@app.route('/api/upload', methods=['POST'])
def upload_file():
    """接收拖拽上传的视频文件，保存到临时目录，返回本地路径"""
    if 'file' not in request.files:
        return jsonify({'error': '没有文件'}), 400

    f = request.files['file']
    if not f.filename:
        return jsonify({'error': '文件名为空'}), 400

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    # 保留原始文件名
    safe_name = f.filename.replace('/', '_').replace('\\', '_')
    save_path = os.path.join(UPLOAD_DIR, safe_name)

    # 如果同名文件已存在，先删除
    if os.path.exists(save_path):
        os.remove(save_path)

    f.save(save_path)
    return jsonify({'path': save_path, 'filename': safe_name})


@app.route('/api/analyze', methods=['POST'])
def analyze():
    """分析输入源"""
    data = request.json
    input_type = data.get('input_type', 'sequence')
    input_path = data.get('input_path', '')

    if not input_path or not os.path.exists(input_path):
        return jsonify({'error': '路径不存在'}), 400

    try:
        info = extractor.analyze_input(input_type, input_path)
        return jsonify(info)
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/preview', methods=['POST'])
def preview():
    """获取预览帧（含动画预览数据）"""
    data = request.json
    input_type = data.get('input_type', 'sequence')
    input_path = data.get('input_path', '')
    mode = data.get('mode', 'fixed')
    params = data.get('params', {})
    max_frames = data.get('max_frames', 48)

    if not input_path or not os.path.exists(input_path):
        return jsonify({'error': '路径不存在'}), 400

    try:
        extractor.cancel_flag = False
        result = extractor.get_preview_frames(input_type, input_path, mode, params, max_frames)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@socketio.on('start_extract')
def handle_extract(data):
    """WebSocket 处理抽帧请求"""
    input_type = data.get('input_type', 'sequence')
    input_path = data.get('input_path', '')
    output_path = data.get('output_path', '')
    mode = data.get('mode', 'fixed')
    params = data.get('params', {})

    if not output_path:
        if input_type == 'video':
            output_path = os.path.join(os.path.dirname(input_path), 'output')
        else:
            output_path = os.path.join(input_path, 'output')

    extractor.cancel_flag = False

    def progress_cb(pct, msg):
        socketio.emit('progress', {'percent': round(pct * 100, 1), 'message': msg})

    def run():
        try:
            socketio.emit('progress', {'percent': 0, 'message': '开始处理...'})
            if input_type == 'video':
                result = extractor.extract_from_video(input_path, output_path, mode, params, progress_cb)
            else:
                result = extractor.extract_from_sequence(input_path, output_path, mode, params, progress_cb)
            result['output_dir'] = output_path
            socketio.emit('complete', result)
        except Exception as e:
            socketio.emit('error', {'message': str(e)})

    thread = threading.Thread(target=run, daemon=True)
    thread.start()


@socketio.on('cancel_extract')
def handle_cancel():
    extractor.cancel()
    socketio.emit('progress', {'percent': 0, 'message': '正在取消...'})


@app.route('/api/open_folder', methods=['POST'])
def open_folder():
    """打开文件夹"""
    data = request.json
    folder = data.get('path', '')
    if folder and os.path.isdir(folder):
        if sys.platform == 'win32':
            os.startfile(folder)
        elif sys.platform == 'darwin':
            os.system(f'open "{folder}"')
        else:
            os.system(f'xdg-open "{folder}"')
        return jsonify({'ok': True})
    return jsonify({'error': '目录不存在'}), 400


@app.route('/api/find_path', methods=['POST'])
def find_path():
    """通过文件/文件夹名在全盘搜索完整路径（Windows用where命令，快速可靠）"""
    data = request.json
    name = data.get('name', '')
    item_type = data.get('type', 'file')

    if not name:
        return jsonify({'path': None})

    # Windows: 用 where /r 逐盘搜索（原生命令，快速且无编码问题）
    if sys.platform == 'win32':
        import string
        for letter in string.ascii_uppercase:
            drive = f'{letter}:\\'
            if not os.path.exists(drive):
                continue
            try:
                result = subprocess.run(
                    ['where', '/r', drive, name],
                    capture_output=True, text=True, timeout=15,
                    encoding='utf-8', errors='replace'
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        if item_type == 'folder' and os.path.isdir(line):
                            return jsonify({'path': line})
                        elif item_type == 'file' and os.path.isfile(line):
                            return jsonify({'path': line})
            except (subprocess.TimeoutExpired, Exception):
                continue
    else:
        # macOS/Linux: 用 find 命令
        home = os.path.expanduser('~')
        try:
            result = subprocess.run(
                ['find', home, '-name', name, '-maxdepth', '8'],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if item_type == 'folder' and os.path.isdir(line):
                        return jsonify({'path': line})
                    elif item_type == 'file' and os.path.isfile(line):
                        return jsonify({'path': line})
        except (subprocess.TimeoutExpired, Exception):
            pass

    return jsonify({'path': None})


# ============ 启动 ============

def main():
    port = 7860
    url = f"http://127.0.0.1:{port}"
    print(f"\n{'='*50}")
    print(f"  序列帧抽帧工具 - Web 版")
    print(f"  访问地址: {url}")
    print(f"{'='*50}\n")

    # 延迟打开浏览器
    def open_browser():
        time.sleep(1.5)
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()
    socketio.run(app, host='127.0.0.1', port=port, debug=False, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    main()
