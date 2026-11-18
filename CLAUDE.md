# CLAUDE.md · Vertex-FrameExtract AI 协作导航文档

> 本文档与 `AGENTS.md` 保持同步，为 Claude Code 及其他 AI 编码助手提供 `Vertex-FrameExtract` 仓库的架构全景、核心代码索引、运行/打包命令与开发约束。修改代码前请先阅读本文档。

---

## 1. 项目定位与架构概览

`Vertex-FrameExtract`（序列帧抽帧工具 Web v1.0）是一个基于 **Python (Flask + Flask-SocketIO + OpenCV + Pillow + pyoxipng) + Web 前端** 的本地视频与序列帧抽帧、预览对比与多策略图像压缩工具：
- **核心能力**：
  - 支持**视频文件**（`.mp4/.avi/.mov/.mkv/.flv/.webm`）与**序列帧文件夹**（`.png/.jpg/.jpeg/.bmp/.tiff/.tga/.webp`）两种输入源。
  - 提供三种抽帧算法：**固定间隔**（每 N 帧抽 1 帧）、**自定义间隔**（隔 X 帧抽 Y 帧）、**智能抽帧**（基于 `320×240` 灰度帧差均值自动保留关键帧）。
  - 内置 `ImageCompressor` 多策略压缩引擎：支持 **PNG 无损**（Pillow optimize + `oxipng` DEFLATE 重编码）、**WebP 无损**、**PNG 量化**（自动对比 `MEDIANCUT` 与 `FASTOCTREE` 择优 + Alpha 通道无损保留 + `oxipng`）、**WebP 有损** 与 **JPEG**，并提供抽帧前单帧全格式压缩率与 PSNR 客观画质预估（`/api/estimate`）。
  - 提供原始帧 vs 抽帧后的同步双画面对比播放器、时间轴缩略图与服务端本机目录/盘符浏览器。
- **跨仓库关系**：本工具的抽帧核心已移植一份精简版（改用标准库 HTTP 轮询替代 Flask/SocketIO）至 `Vertex-AssetDesk/server.py`（前端位于 `Vertex-AssetDesk/tools/frame-extract/`）。本仓库是功能最完整（含 `ImageCompressor` 5 种格式压缩与 PSNR 测试、PyInstaller 独立 EXE 打包）的独立版本。

---

## 2. 核心文件索引

| 文件路径 | 作用与核心职责 |
| :--- | :--- |
| `server.py` | **后端主程序（约 1014 行）**。包含启动依赖自检 `_ensure_deps()`、精简版 Python `pdb` 兼容桩 `_shim_missing_stdlib()`、中文路径兼容读写（`cv_imread` / `cv_open_video`）、`ImageCompressor` 压缩引擎、`FrameExtractor` 抽帧与预览引擎、Flask REST 路由与 SocketIO 实时进度推送，监听 `127.0.0.1:7860`。 |
| `templates/index.html` | **Web 界面模板**。包含左侧参数控制栏（输入源切换、三种抽帧模式、五种输出格式与压缩参数、压缩效果测试）、右侧双画布对比播放器、文件浏览器模态框与完成统计弹窗。 |
| `static/app.js` | **前端核心交互逻辑（约 783 行）**。管理全局 `state`、Socket.IO 事件通信（`start_extract` / `cancel_extract` / `progress` / `complete`）、对比播放器帧同步映射、拖拽/原生文件上传（`/api/upload`）与服务端目录浏览。 |
| `static/style.css` | 暗色主题样式表。 |
| `requirements.txt` | Python 依赖清单：`flask>=3.0`、`flask-socketio>=5.3`、`opencv-python>=4.8`、`numpy>=1.24`、`Pillow>=10.0`、`pyoxipng>=9.0`。 |
| `run.bat` | 本地开发启动脚本（执行 `python server.py`）。 |
| `build.bat` | **PyInstaller 打包脚本**。将 `server.py`、`static/`、`templates/` 及 `抽帧1.ico` 打包为单文件 `dist\抽帧工具.exe`。 |
| `抽帧1.ico` | Windows 应用图标。 |
| `_uploads/` | 浏览器拖拽/选择本地视频文件时的服务端接收目录。 |

---

## 3. 后端核心类与接口速查（`server.py`）

### 核心类
- `ImageCompressor`：
  - `save(img, fpath, fmt, quality, colors, png_compress, oxipng_level)`：统一保存入口，支持 `png`、`pngq`、`webpl`、`webp`、`jpeg`。
  - `_save_quantized_png(...)`：针对 `RGBA` 图像先分离 Alpha 通道，仅对 `RGB` 执行 `_pick_quantize_method()`（对比 `MEDIANCUT` 和 `FASTOCTREE` + Floyd-Steinberg 抖动后取体积更小者），再合并原始 Alpha 通道并用 `oxipng` 二次无损压缩。
- `FrameExtractor`：
  - `analyze_input(input_type, input_path)`：分析总帧数、FPS、宽高、时长并抽取最多 12 张 Base64 缩略图。
  - `get_preview_frames(input_type, input_path, mode, params, max_frames=60)`：生成原始帧与抽帧后序列的对比预览缩略图（默认上限 48/60 帧）。
  - `estimate_compression(input_type, input_path, params)`：取视频/序列帧中间帧，测试全部 5 种格式的输出体积（KB）、节省比例（%）与有损格式的 PSNR（dB）。
  - `extract_from_video(...)` / `extract_from_sequence(...)`：执行批量抽帧与压缩落盘，支持 `cancel_flag` 中途取消与进度回调。

### HTTP 路由与 Socket.IO 事件
- `GET /`：渲染 `templates/index.html`。
- `GET /api/drives`：枚举 Windows 盘符（`C:\`、`D:\` 等）。
- `POST /api/browse`：浏览服务端目录或过滤视频文件。
- `POST /api/upload`：接收前端拖拽/选择的视频文件并存至 `_uploads/`。
- `POST /api/analyze`：分析输入源元数据与缩略图条。
- `POST /api/preview`：获取对比播放器帧数据。
- `POST /api/estimate`：单帧全格式压缩率与 PSNR 评估。
- `POST /api/open_folder`：调用系统文件管理器（`os.startfile`）打开输出目录。
- `POST /api/find_path`：按文件名/文件夹名检索本地路径。
- Socket.IO：`start_extract`（启动后台线程抽帧） / `cancel_extract`（取消任务） → 推送 `progress`、`complete`、`error`。

---

## 4. 运行与打包命令

### 安装依赖与启动服务
```powershell
# 安装依赖
pip install -r requirements.txt

# 启动服务（默认监听 http://127.0.0.1:7860 并自动打开浏览器）
python server.py
# 或直接运行：
.\run.bat
```

### 打包独立 EXE
```powershell
# 运行一键打包脚本，生成 dist\抽帧工具.exe
.\build.bat
```

---

## 5. 开发约束与注意事项（AI 必读）

1. **Windows 中文路径兼容性（严禁直接裸调 `cv2.imread`）**：
   - OpenCV 在 Windows 下直接使用 `cv2.imread()` 或 `cv2.VideoCapture()` 读取含中文路径的文件常会失败。
   - 读取图片**必须**使用 `cv_imread(path, flags)`（内部通过 `np.fromfile` + `cv2.imdecode` 实现）；打开视频**必须**使用 `cv_open_video(path)`（打不开时自动拷贝到 `%TEMP%` ASCII 临时文件名再打开）。
2. **PyInstaller 打包资源路径与隐式导入**：
   - 静态资源与模板目录必须通过 `_bundle_dir = sys._MEIPASS if getattr(sys, 'frozen', False) else BASE_DIR` 定位，而运行期生成的数据（如 `_uploads`）必须使用 `BASE_DIR`。
   - Flask-SocketIO 在 PyInstaller 打包时依赖 `--hidden-import=engineio.async_drivers.threading`、`flask_socketio`、`socketio`、`engineio`，修改 `build.bat` 时切勿删减这些参数。
   - 保持 `_shim_missing_stdlib()` 在导入 `flask` / `flask_socketio` 之前执行，防止精简版 Python 缺少 `pdb` 导致启动崩溃。
3. **`oxipng` 可选降级保证**：
   - `pyoxipng` 导入包裹在 `try...except` 中（`HAS_OXIPNG` 标志）。任何新增的 PNG 优化逻辑必须在 `HAS_OXIPNG == False` 时平滑回退到 Pillow 原生 `optimize=True`，不得抛出未捕获异常。
4. **Git 提交卫生**：
   - 不要将新产生的测试视频、抽帧输出图片、`build/`、`dist/` 提交到版本库。
   - 修改 `AGENTS.md` 时必须同步更新 `CLAUDE.md`。
