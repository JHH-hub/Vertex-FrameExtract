#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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


def _ensure_deps():
    required = {
        'cv2': 'opencv-python',
        'numpy': 'numpy',
        'PIL': 'Pillow',
        'flask': 'flask',
        'flask_socketio': 'flask-socketio',
        'oxipng': 'pyoxipng',
    }
    missing = []
    for mod, pkg in required.items():
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q'] + missing)

_ensure_deps()


def _shim_missing_stdlib():
    """
    Some slim / embedded Python distributions ship without `pdb`.
    click>=8.2 does `import pdb` inside click.testing, which flask.testing
    imports, which flask_socketio imports -> hard ImportError on startup.
    Install a minimal no-op stub so the import chain resolves.
    """
    import importlib.util
    if importlib.util.find_spec('pdb') is not None:
        return
    import types
    stub = types.ModuleType('pdb')

    def _unavailable(*args, **kwargs):
        raise RuntimeError('pdb is not available in this Python distribution')

    stub.set_trace = _unavailable
    stub.post_mortem = _unavailable
    stub.pm = _unavailable
    stub.run = _unavailable
    stub.runcall = _unavailable
    stub.Pdb = type('Pdb', (), {'__init__': _unavailable})
    sys.modules['pdb'] = stub

_shim_missing_stdlib()

import cv2
import numpy as np
from PIL import Image
from flask import Flask, render_template, request, jsonify, send_from_directory, send_file
from flask_socketio import SocketIO
import re
import webbrowser

try:
    import oxipng
    HAS_OXIPNG = True
except Exception:
    oxipng = None
    HAS_OXIPNG = False

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


def cv_imread(path, flags=cv2.IMREAD_COLOR):
    data = np.fromfile(path, dtype=np.uint8)
    return cv2.imdecode(data, flags)

def cv_open_video(path):
    cap = cv2.VideoCapture(path)
    if cap.isOpened():
        return cap
    import tempfile
    ext = os.path.splitext(path)[1]
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False, dir=os.environ.get('TEMP'))
    tmp_path = tmp.name
    tmp.close()
    shutil.copy2(path, tmp_path)
    cap = cv2.VideoCapture(tmp_path)
    cap._tmp_path = tmp_path
    return cap

app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')



# ============ Image Compression Engine ============

class ImageCompressor:
    """
    Multi-strategy image compressor.

    Lossless tiers (pixel-identical output):
        png_lossless : Pillow optimize + oxipng (zopfli-class DEFLATE re-encode)
        webp_lossless: WebP lossless mode
    Lossy tiers:
        pngq  : color quantization to indexed PNG, then oxipng
        webp  : WebP lossy
        jpeg  : JPEG lossy
    """

    FORMAT_PNG = 'png'
    FORMAT_PNG_QUANTIZE = 'pngq'
    FORMAT_WEBP_LOSSLESS = 'webpl'
    FORMAT_WEBP = 'webp'
    FORMAT_JPEG = 'jpeg'

    LOSSLESS_FORMATS = {'png', 'webpl'}

    @staticmethod
    def _oxipng_bytes(data, level=4):
        """Run oxipng on PNG bytes. Lossless - only re-encodes DEFLATE stream."""
        if not HAS_OXIPNG:
            return data
        try:
            out = oxipng.optimize_from_memory(
                data,
                level=level,
                strip=oxipng.StripChunks.safe(),
            )
            return out if len(out) < len(data) else data
        except Exception:
            return data

    @staticmethod
    def save(img, fpath, fmt='png', quality=85, colors=256,
             png_compress=6, oxipng_level=4):
        base = os.path.splitext(fpath)[0]
        if fmt in (ImageCompressor.FORMAT_WEBP, ImageCompressor.FORMAT_WEBP_LOSSLESS):
            fpath = base + '.webp'
        elif fmt == ImageCompressor.FORMAT_JPEG:
            fpath = base + '.jpg'
        else:
            fpath = base + '.png'

        if fmt == ImageCompressor.FORMAT_PNG:
            # Lossless PNG: Pillow optimize -> oxipng re-encode
            work = img if img.mode in ('RGB', 'RGBA', 'P', 'L') else img.convert('RGB')
            buf = BytesIO()
            work.save(buf, 'PNG', optimize=True, compress_level=max(png_compress, 9))
            data = ImageCompressor._oxipng_bytes(buf.getvalue(), oxipng_level)
            with open(fpath, 'wb') as f:
                f.write(data)

        elif fmt == ImageCompressor.FORMAT_PNG_QUANTIZE:
            fpath = ImageCompressor._save_quantized_png(
                img, fpath, colors, png_compress, oxipng_level
            )

        elif fmt == ImageCompressor.FORMAT_WEBP_LOSSLESS:
            work = img if img.mode in ('RGB', 'RGBA') else img.convert('RGB')
            work.save(fpath, 'WEBP', lossless=True, method=6)

        elif fmt == ImageCompressor.FORMAT_WEBP:
            work = img if img.mode == 'RGBA' else img.convert('RGB')
            work.save(fpath, 'WEBP', quality=quality, method=6)

        elif fmt == ImageCompressor.FORMAT_JPEG:
            work = img.convert('RGB') if img.mode != 'RGB' else img
            work.save(fpath, 'JPEG', quality=quality, optimize=True,
                      subsampling='4:2:0' if quality < 90 else '4:4:4')

        return fpath

    @staticmethod
    def _pick_quantize_method(img, colors):
        """
        Try both MEDIANCUT and FASTOCTREE, keep whichever gives smaller output
        at comparable quality. MEDIANCUT usually wins on quality, FASTOCTREE on
        size for photographic content.
        """
        candidates = []
        for method in (Image.Quantize.MEDIANCUT, Image.Quantize.FASTOCTREE):
            try:
                q = img.quantize(colors=colors, method=method,
                                 dither=Image.Dither.FLOYDSTEINBERG)
                buf = BytesIO()
                q.save(buf, 'PNG', optimize=True, compress_level=9)
                candidates.append((len(buf.getvalue()), q, buf.getvalue()))
            except Exception:
                continue
        if not candidates:
            return None, None
        candidates.sort(key=lambda t: t[0])
        return candidates[0][1], candidates[0][2]

    @staticmethod
    def _save_quantized_png(img, fpath, colors=256, png_compress=6, oxipng_level=4):
        colors = max(2, min(256, colors))

        if img.mode == 'RGBA':
            # Preserve alpha exactly, quantize RGB only
            r, g, b, a = img.split()
            rgb_img = Image.merge('RGB', (r, g, b))
            q, _ = ImageCompressor._pick_quantize_method(rgb_img, colors)
            if q is None:
                img.save(fpath, 'PNG', optimize=True, compress_level=9)
                return fpath
            qr, qg, qb = q.convert('RGB').split()
            result = Image.merge('RGBA', (qr, qg, qb, a))
            buf = BytesIO()
            result.save(buf, 'PNG', optimize=True, compress_level=9)
            data = ImageCompressor._oxipng_bytes(buf.getvalue(), oxipng_level)
        else:
            work = img.convert('RGB') if img.mode != 'RGB' else img
            q, raw = ImageCompressor._pick_quantize_method(work, colors)
            if q is None:
                work.save(fpath, 'PNG', optimize=True, compress_level=9)
                return fpath
            data = ImageCompressor._oxipng_bytes(raw, oxipng_level)

        with open(fpath, 'wb') as f:
            f.write(data)
        return fpath

    @staticmethod
    def estimate_savings(original_size, compressed_size):
        if original_size <= 0:
            return 0.0
        return round((1 - compressed_size / original_size) * 100, 1)

    @staticmethod
    def is_lossless(fmt):
        return fmt in ImageCompressor.LOSSLESS_FORMATS

    @staticmethod
    def get_format_ext(fmt):
        exts = {
            'png': '.png',
            'pngq': '.png',
            'webpl': '.webp',
            'webp': '.webp',
            'jpeg': '.jpg',
        }
        return exts.get(fmt, '.png')



# ============ Frame Extractor ============

class FrameExtractor:

    def __init__(self):
        self.cancel_flag = False

    def extract_from_video(self, video_path, output_dir, mode, params, callback=None):
        cap = cv_open_video(video_path)
        if not cap.isOpened():
            raise ValueError("Cannot open video file")

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
        out_format = params.get('format', 'png')
        quality = params.get('quality', 85)
        colors = params.get('colors', 256)
        png_compress = params.get('compression', 6)
        oxipng_level = params.get('oxipng_level', 4)
        size_out = 0

        for i, idx in enumerate(selected):
            if self.cancel_flag:
                cap.release()
                return {'status': 'cancelled'}

            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if ret:
                ext = ImageCompressor.get_format_ext(out_format)
                fname = f"{prefix}{str(saved).zfill(digits)}{ext}"
                fpath = os.path.join(output_dir, fname)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb)
                actual_path = ImageCompressor.save(
                    img, fpath, fmt=out_format,
                    quality=quality, colors=colors,
                    png_compress=png_compress,
                    oxipng_level=oxipng_level
                )
                size_out += os.path.getsize(actual_path)
                saved += 1

            if callback:
                callback((i + 1) / len(selected), f"frame {saved}/{len(selected)}")

        cap.release()
        return {
            'status': 'success',
            'total_input': total_frames,
            'total_output': saved,
            'compression_rate': round((1 - saved / total_frames) * 100, 1) if total_frames > 0 else 0,
            'fps': fps,
            'width': width,
            'height': height,
            'size_output_mb': round(size_out / (1024 * 1024), 2),
            'output_format': out_format,
            'lossless': ImageCompressor.is_lossless(out_format),
            'selected_indices': selected
        }

    def extract_from_sequence(self, input_dir, output_dir, mode, params, callback=None):
        files = self._get_sorted_images(input_dir)
        if not files:
            raise ValueError("No image files found")

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
        out_format = params.get('format', 'png')
        quality = params.get('quality', 85)
        colors = params.get('colors', 256)
        png_compress = params.get('compression', 6)
        oxipng_level = params.get('oxipng_level', 4)
        size_in = 0
        size_out = 0

        for i, idx in enumerate(selected):
            if self.cancel_flag:
                return {'status': 'cancelled'}

            src = files[idx]
            size_in += os.path.getsize(src)
            ext = ImageCompressor.get_format_ext(out_format)
            fname = f"{prefix}{str(saved).zfill(digits)}{ext}"
            fpath = os.path.join(output_dir, fname)

            img = Image.open(src)
            if img.mode not in ('RGB', 'RGBA'):
                img = img.convert('RGB')
            actual_path = ImageCompressor.save(
                img, fpath, fmt=out_format,
                quality=quality, colors=colors,
                png_compress=png_compress,
                oxipng_level=oxipng_level
            )
            size_out += os.path.getsize(actual_path)
            saved += 1

            if callback:
                callback((i + 1) / len(selected), f"frame {saved}/{len(selected)}")

        first = Image.open(files[0])
        w, h = first.size

        return {
            'status': 'success',
            'total_input': total,
            'total_output': saved,
            'compression_rate': round((1 - saved / total) * 100, 1) if total > 0 else 0,
            'size_input_mb': round(size_in / (1024 * 1024), 2),
            'size_output_mb': round(size_out / (1024 * 1024), 2),
            'file_compression_rate': ImageCompressor.estimate_savings(size_in, size_out),
            'width': w,
            'height': h,
            'output_format': out_format,
            'lossless': ImageCompressor.is_lossless(out_format),
            'selected_indices': selected
        }

    def analyze_input(self, input_type, input_path):
        if input_type == 'video':
            cap = cv_open_video(input_path)
            if not cap.isOpened():
                raise ValueError("Cannot open video file")

            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            duration = total / fps if fps > 0 else 0

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
                raise ValueError("No image files found")

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
        if input_type == 'video':
            cap = cv_open_video(input_path)
            if not cap.isOpened():
                raise ValueError("Cannot open video file")
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS)
        else:
            files = self._get_sorted_images(input_path)
            if not files:
                raise ValueError("No image files found")
            total = len(files)
            fps = 24

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

        def subsample(lst, max_n):
            if len(lst) <= max_n:
                return lst
            step = len(lst) / max_n
            return [lst[int(i * step)] for i in range(max_n)]

        preview_all = subsample(all_indices, max_frames)
        preview_selected = subsample(selected, max_frames)

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

    def estimate_compression(self, input_type, input_path, params):
        """
        Compress one sample frame with every format and report
        size + objective quality (PSNR / SSIM-ish) so the user can
        judge lossless vs lossy trade-off before running the job.
        """
        original_bytes = None

        if input_type == 'video':
            cap = cv_open_video(input_path)
            if not cap.isOpened():
                raise ValueError("Cannot open video file")
            mid = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) // 2
            cap.set(cv2.CAP_PROP_POS_FRAMES, mid)
            ret, frame = cap.read()
            cap.release()
            if not ret:
                raise ValueError("Cannot read video frame")
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            sample_img = Image.fromarray(rgb)
        else:
            files = self._get_sorted_images(input_path)
            if not files:
                raise ValueError("No image files found")
            mid_idx = len(files) // 2
            src_path = files[mid_idx]
            original_bytes = os.path.getsize(src_path)
            sample_img = Image.open(src_path)
            if sample_img.mode not in ('RGB', 'RGBA'):
                sample_img = sample_img.convert('RGB')

        reference = np.asarray(sample_img.convert('RGB')).astype(np.float64)

        import tempfile
        results = {}
        formats = [
            ('png',   'PNG 无损',   True),
            ('webpl', 'WebP 无损',  True),
            ('pngq',  'PNG 量化',   False),
            ('webp',  'WebP 有损',  False),
            ('jpeg',  'JPEG',       False),
        ]

        quality = params.get('quality', 85)
        colors = params.get('colors', 256)
        oxipng_level = params.get('oxipng_level', 4)

        for key, label, lossless in formats:
            with tempfile.NamedTemporaryFile(suffix='.tmp', delete=False) as tf:
                tmp_path = tf.name
            actual = None
            try:
                actual = ImageCompressor.save(
                    sample_img, tmp_path, fmt=key,
                    quality=quality, colors=colors,
                    oxipng_level=oxipng_level
                )
                size = os.path.getsize(actual)
                entry = {
                    'label': label,
                    'lossless': lossless,
                    'size_kb': round(size / 1024, 1),
                }
                if lossless:
                    entry['psnr'] = None
                else:
                    decoded = np.asarray(
                        Image.open(actual).convert('RGB')
                    ).astype(np.float64)
                    mse = float(np.mean((reference - decoded) ** 2))
                    entry['psnr'] = (
                        None if mse == 0
                        else round(20 * np.log10(255.0 / np.sqrt(mse)), 1)
                    )
                results[key] = entry
            except Exception as e:
                results[key] = {
                    'label': label, 'lossless': lossless,
                    'size_kb': -1, 'psnr': None
                }
            finally:
                for path in {tmp_path, actual}:
                    if not path:
                        continue
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

        # Baseline = original source file when available, else lossless PNG
        base_size_kb = (
            round(original_bytes / 1024, 1) if original_bytes
            else results.get('png', {}).get('size_kb', 0)
        )
        for key in results:
            sz = results[key]['size_kb']
            if sz > 0 and base_size_kb > 0:
                results[key]['saving'] = round((1 - sz / base_size_kb) * 100, 1)
            else:
                results[key]['saving'] = 0

        return {
            'baseline_kb': base_size_kb,
            'has_oxipng': HAS_OXIPNG,
            'formats': results,
        }

    # ---- Frame selection algorithms ----

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
                callback(i / total * 0.5, f"analyzing {i}/{total}")
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
                callback(i / total * 0.5, f"analyzing {i}/{total}")
        return selected

    # ---- Utility ----

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

# ============ Routes ============

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/browse', methods=['POST'])
def browse():
    data = request.json
    browse_type = data.get('type', 'directory')
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


UPLOAD_DIR = os.path.join(BASE_DIR, '_uploads')

@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400

    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'empty filename'}), 400

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    safe_name = f.filename.replace('/', '_').replace('\\', '_')
    save_path = os.path.join(UPLOAD_DIR, safe_name)

    if os.path.exists(save_path):
        os.remove(save_path)

    f.save(save_path)
    return jsonify({'path': save_path, 'filename': safe_name})


@app.route('/api/analyze', methods=['POST'])
def analyze():
    data = request.json
    input_type = data.get('input_type', 'sequence')
    input_path = data.get('input_path', '')

    if not input_path or not os.path.exists(input_path):
        return jsonify({'error': 'path not found'}), 400

    try:
        info = extractor.analyze_input(input_type, input_path)
        return jsonify(info)
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/preview', methods=['POST'])
def preview():
    data = request.json
    input_type = data.get('input_type', 'sequence')
    input_path = data.get('input_path', '')
    mode = data.get('mode', 'fixed')
    params = data.get('params', {})
    max_frames = data.get('max_frames', 48)

    if not input_path or not os.path.exists(input_path):
        return jsonify({'error': 'path not found'}), 400

    try:
        extractor.cancel_flag = False
        result = extractor.get_preview_frames(input_type, input_path, mode, params, max_frames)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/estimate', methods=['POST'])
def estimate():
    data = request.json
    input_type = data.get('input_type', 'sequence')
    input_path = data.get('input_path', '')
    params = data.get('params', {})

    if not input_path or not os.path.exists(input_path):
        return jsonify({'error': 'path not found'}), 400

    try:
        result = extractor.estimate_compression(input_type, input_path, params)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@socketio.on('start_extract')
def handle_extract(data):
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
            socketio.emit('progress', {'percent': 0, 'message': 'starting...'})
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
    socketio.emit('progress', {'percent': 0, 'message': 'cancelling...'})


@app.route('/api/open_folder', methods=['POST'])
def open_folder():
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
    return jsonify({'error': 'dir not found'}), 400


@app.route('/api/find_path', methods=['POST'])
def find_path():
    data = request.json
    name = data.get('name', '')
    item_type = data.get('type', 'file')

    if not name:
        return jsonify({'path': None})

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


# ============ Main ============

def main():
    port = 7860
    url = f"http://127.0.0.1:{port}"
    print(f"\n{'='*50}")
    print(f"  Frame Extractor - Web")
    print(f"  URL: {url}")
    print(f"{'='*50}\n")

    def open_browser():
        time.sleep(1.5)
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()
    socketio.run(app, host='127.0.0.1', port=port, debug=False, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    main()
