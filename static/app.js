// ============ State ============
const state = {
  inputType: 'sequence',
  inputPath: '',
  outputPath: '',
  mode: 'fixed',
  analysisData: null,
  previewData: null,
  // Player
  originalFrames: [],
  extractedFrames: [],
  currentFrameIdx: 0,
  isPlaying: false,
  playTimer: null,
  playerView: 'compare',
  // Browser
  browserTarget: 'input', // 'input' or 'output'
  browserCurrentPath: '',
  // Processing
  lastOutputDir: ''
};

const socket = io();

// ============ Input Type ============
function setInputType(type) {
  state.inputType = type;
  document.querySelectorAll('.input-type-tabs .tab').forEach(t => {
    t.classList.toggle('active', t.dataset.type === type);
  });
}

// ============ Mode ============
function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll('.mode-card').forEach(c => {
    c.classList.toggle('active', c.dataset.mode === mode);
  });
  document.querySelectorAll('.mode-params').forEach(p => p.classList.add('hidden'));
  const el = document.getElementById('params-' + mode);
  if (el) el.classList.remove('hidden');
}

// ============ Compression label ============
function updateCompressionLabel(val) {
  const labels = {0:'无压缩',1:'1',2:'2',3:'3',4:'4',5:'快速',6:'平衡',7:'7',8:'8',9:'最大'};
  document.getElementById('compressionValue').textContent = val + ' · ' + (labels[val]||val);
}

// ============ Gather params ============
function gatherParams() {
  const p = {
    prefix: document.getElementById('prefix').value || 'frame_',
    digits: parseInt(document.getElementById('digits').value) || 4,
    compression: parseInt(document.getElementById('compression').value)
  };
  if (state.mode === 'fixed') {
    p.interval = parseInt(document.getElementById('interval').value) || 3;
  } else if (state.mode === 'custom') {
    p.skip = parseInt(document.getElementById('skip').value) || 2;
    p.take = parseInt(document.getElementById('take').value) || 1;
  } else if (state.mode === 'smart') {
    p.threshold = parseInt(document.getElementById('threshold').value) / 100;
  }
  return p;
}

// ============ Analyze ============
async function analyzeInput() {
  const path = document.getElementById('inputPath').value.trim();
  if (!path) return;
  state.inputPath = path;

  const infoEl = document.getElementById('inputInfo');
  infoEl.innerHTML = '<span class="loading-spinner"></span> 分析中...';

  try {
    const res = await fetch('/api/analyze', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({input_type: state.inputType, input_path: path})
    });
    const data = await res.json();
    if (data.error) {
      infoEl.textContent = '❌ ' + data.error;
      return;
    }

    state.analysisData = data;

    let info = '';
    if (data.type === 'video') {
      info = `📹 ${data.total_frames} 帧 · ${data.fps} fps · ${data.width}×${data.height} · ${data.duration}s`;
    } else {
      info = `🖼️ ${data.total_frames} 帧 · ${data.width}×${data.height}`;
    }
    infoEl.textContent = info;

    // Show thumb strip
    if (data.thumbnails && data.thumbnails.length > 0) {
      const strip = document.getElementById('thumbStrip');
      const scroll = document.getElementById('thumbStripScroll');
      strip.classList.remove('hidden');
      scroll.innerHTML = '';
      data.thumbnails.forEach(src => {
        const img = document.createElement('img');
        img.src = src;
        scroll.appendChild(img);
      });
    }
  } catch (e) {
    infoEl.textContent = '❌ 连接失败';
  }
}

// Auto-analyze on path change
let analyzeTimer = null;
document.getElementById('inputPath').addEventListener('input', () => {
  clearTimeout(analyzeTimer);
  analyzeTimer = setTimeout(analyzeInput, 600);
});
document.getElementById('inputPath').addEventListener('change', analyzeInput);

// ============ Preview ============
async function requestPreview() {
  const path = document.getElementById('inputPath').value.trim();
  if (!path) { alert('请先输入路径'); return; }
  state.inputPath = path;

  const btn = document.getElementById('btnPreview');
  btn.innerHTML = '<span class="loading-spinner"></span> 分析中...';
  btn.disabled = true;

  try {
    const res = await fetch('/api/preview', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        input_type: state.inputType,
        input_path: path,
        mode: state.mode,
        params: gatherParams(),
        max_frames: 48
      })
    });
    const data = await res.json();
    if (data.error) {
      alert('预览失败: ' + data.error);
      return;
    }

    state.previewData = data;
    state.originalFrames = data.original_frames || [];
    state.extractedFrames = data.extracted_frames || [];
    state.currentFrameIdx = 0;

    // Show player
    document.getElementById('previewEmpty').style.display = 'none';
    document.getElementById('playerContainer').classList.remove('hidden');

    // Update stats
    const statsEl = document.getElementById('playerStats');
    statsEl.textContent = `${data.total_input} → ${data.total_output} 帧 · 压缩 ${data.compression_rate}%`;

    document.getElementById('originalCountLabel').textContent = state.originalFrames.length;
    document.getElementById('extractedCountLabel').textContent = state.extractedFrames.length;

    // Set FPS
    if (data.fps) {
      const targetFps = Math.min(Math.round(data.fps / 2), 24);
      document.getElementById('playFps').value = targetFps || 12;
    }

    // Timeline thumbs
    const thumbsEl = document.getElementById('timelineThumbs');
    thumbsEl.innerHTML = '';
    state.originalFrames.forEach((src, i) => {
      const img = document.createElement('img');
      img.src = src;
      img.onclick = () => seekFrame(i);
      if (i === 0) img.classList.add('active');
      thumbsEl.appendChild(img);
    });

    // Update timeline
    const maxLen = Math.max(state.originalFrames.length, state.extractedFrames.length);
    document.getElementById('timeline').max = maxLen - 1;
    document.getElementById('timeline').value = 0;

    renderFrame(0);

  } catch (e) {
    alert('预览请求失败');
  } finally {
    btn.innerHTML = `<svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M8 5v14l11-7z"/></svg> 预览抽帧效果`;
    btn.disabled = false;
  }
}

// ============ Player ============
function setPlayerView(view) {
  state.playerView = view;
  document.querySelectorAll('.player-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.view === view);
  });
  ['compare','original','extracted'].forEach(v => {
    const el = document.getElementById('view-' + v);
    if (el) el.classList.toggle('hidden', v !== view);
  });
  renderFrame(state.currentFrameIdx);
}

function renderFrame(idx) {
  state.currentFrameIdx = idx;

  const oLen = state.originalFrames.length;
  const eLen = state.extractedFrames.length;

  // Map idx proportionally for extracted
  const oIdx = Math.min(idx, oLen - 1);
  const eIdx = Math.min(Math.round(idx * (eLen - 1) / Math.max(oLen - 1, 1)), eLen - 1);

  const oSrc = state.originalFrames[oIdx] || '';
  const eSrc = state.extractedFrames[eIdx >= 0 ? eIdx : 0] || '';

  if (state.playerView === 'compare') {
    document.getElementById('canvasOriginal').src = oSrc;
    document.getElementById('canvasExtracted').src = eSrc;
  } else if (state.playerView === 'original') {
    document.getElementById('canvasSingleOriginal').src = oSrc;
  } else {
    document.getElementById('canvasSingleExtracted').src = eSrc;
  }

  // Update counter
  document.getElementById('frameCounter').textContent = `${oIdx + 1} / ${oLen}`;
  document.getElementById('timeline').value = idx;

  // Update thumb active
  const thumbs = document.querySelectorAll('#timelineThumbs img');
  thumbs.forEach((t, i) => t.classList.toggle('active', i === oIdx));
}

function togglePlay() {
  if (state.isPlaying) {
    stopPlay();
  } else {
    startPlay();
  }
}

function startPlay() {
  state.isPlaying = true;
  document.getElementById('playPauseIcon').setAttribute('d', 'M6 19h4V5H6v14zm8-14v14h4V5h-4z');

  const fps = parseInt(document.getElementById('playFps').value) || 12;
  const interval = 1000 / fps;

  state.playTimer = setInterval(() => {
    let next = state.currentFrameIdx + 1;
    if (next >= state.originalFrames.length) next = 0;
    renderFrame(next);
  }, interval);
}

function stopPlay() {
  state.isPlaying = false;
  document.getElementById('playPauseIcon').setAttribute('d', 'M8 5v14l11-7z');
  clearInterval(state.playTimer);
}

function playerPrev() {
  stopPlay();
  let idx = state.currentFrameIdx - 1;
  if (idx < 0) idx = state.originalFrames.length - 1;
  renderFrame(idx);
}

function playerNext() {
  stopPlay();
  let idx = state.currentFrameIdx + 1;
  if (idx >= state.originalFrames.length) idx = 0;
  renderFrame(idx);
}

function seekFrame(val) {
  stopPlay();
  renderFrame(parseInt(val));
}

// Keyboard shortcuts
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
  if (e.code === 'ArrowLeft') { e.preventDefault(); playerPrev(); }
  if (e.code === 'ArrowRight') { e.preventDefault(); playerNext(); }
});

// ============ Extract ============
function startExtract() {
  const path = document.getElementById('inputPath').value.trim();
  if (!path) { alert('请先选择输入源'); return; }

  const btnStart = document.getElementById('btnStart');
  const btnCancel = document.getElementById('btnCancel');
  const progressSection = document.getElementById('progressSection');

  btnStart.disabled = true;
  btnCancel.classList.remove('hidden');
  progressSection.classList.remove('hidden');

  document.getElementById('progressFill').style.width = '0%';
  document.getElementById('progressText').textContent = '准备中...';
  document.getElementById('progressPercent').textContent = '0%';

  socket.emit('start_extract', {
    input_type: state.inputType,
    input_path: path,
    output_path: document.getElementById('outputPath').value.trim(),
    mode: state.mode,
    params: gatherParams()
  });
}

function cancelExtract() {
  socket.emit('cancel_extract');
}

socket.on('progress', data => {
  document.getElementById('progressFill').style.width = data.percent + '%';
  document.getElementById('progressText').textContent = data.message;
  document.getElementById('progressPercent').textContent = Math.round(data.percent) + '%';
});

socket.on('complete', data => {
  const btnStart = document.getElementById('btnStart');
  const btnCancel = document.getElementById('btnCancel');
  const progressSection = document.getElementById('progressSection');

  btnStart.disabled = false;
  btnCancel.classList.add('hidden');

  if (data.status === 'cancelled') {
    document.getElementById('progressText').textContent = '已取消';
    setTimeout(() => progressSection.classList.add('hidden'), 2000);
    return;
  }

  progressSection.classList.add('hidden');
  state.lastOutputDir = data.output_dir || '';

  // Show done modal
  const statsHtml = `
    <div><b>输入帧数:</b> ${data.total_input}</div>
    <div><b>输出帧数:</b> ${data.total_output}</div>
    <div><b>压缩率:</b> ${data.compression_rate}%</div>
    ${data.size_input_mb ? `<div><b>原始大小:</b> ${data.size_input_mb} MB</div>` : ''}
    ${data.size_output_mb ? `<div><b>输出大小:</b> ${data.size_output_mb} MB</div>` : ''}
    <div style="margin-top:8px;color:var(--text-muted);font-size:11px">${data.output_dir || ''}</div>
  `;
  document.getElementById('doneStats').innerHTML = statsHtml;
  document.getElementById('doneModal').classList.remove('hidden');
});

socket.on('error', data => {
  const btnStart = document.getElementById('btnStart');
  const btnCancel = document.getElementById('btnCancel');
  btnStart.disabled = false;
  btnCancel.classList.add('hidden');
  alert('处理失败: ' + data.message);
});

async function openOutputFolder() {
  if (state.lastOutputDir) {
    await fetch('/api/open_folder', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({path: state.lastOutputDir})
    });
  }
  closeDoneModal();
}

function closeDoneModal() {
  document.getElementById('doneModal').classList.add('hidden');
}

// ============ File Browser ============
async function openBrowser() {
  state.browserTarget = 'input';
  document.getElementById('browserTitle').textContent =
    state.inputType === 'video' ? '选择视频文件' : '选择序列帧文件夹';
  document.getElementById('browserModal').classList.remove('hidden');
  await loadDrives();
  const start = document.getElementById('inputPath').value.trim() || '';
  await browserNavigate(start || 'C:\\');
}

async function openOutputBrowser() {
  state.browserTarget = 'output';
  document.getElementById('browserTitle').textContent = '选择输出目录';
  document.getElementById('browserModal').classList.remove('hidden');
  await loadDrives();
  const start = document.getElementById('outputPath').value.trim() || '';
  await browserNavigate(start || 'C:\\');
}

function closeBrowser() {
  document.getElementById('browserModal').classList.add('hidden');
}

async function loadDrives() {
  try {
    const res = await fetch('/api/drives');
    const data = await res.json();
    const el = document.getElementById('browserDrives');
    el.innerHTML = '';
    (data.drives || []).forEach(d => {
      const btn = document.createElement('button');
      btn.className = 'drive-btn';
      btn.textContent = d;
      btn.onclick = () => browserNavigate(d);
      el.appendChild(btn);
    });
  } catch (e) {}
}

async function browserNavigate(path) {
  if (!path) return;
  state.browserCurrentPath = path;
  document.getElementById('browserPath').value = path;

  try {
    const res = await fetch('/api/browse', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        path: path,
        type: (state.browserTarget === 'input' && state.inputType === 'video') ? 'file' : 'directory'
      })
    });
    const data = await res.json();
    state.browserCurrentPath = data.current;
    document.getElementById('browserPath').value = data.current;

    const listEl = document.getElementById('browserList');
    listEl.innerHTML = '';

    (data.items || []).forEach(item => {
      const div = document.createElement('div');
      div.className = 'browser-item ' + item.type;
      div.innerHTML = `
        <span class="bi-icon">${item.type === 'dir' ? '📁' : '🎬'}</span>
        <span>${item.name}</span>
        ${item.size ? `<span class="bi-size">${item.size}</span>` : ''}
      `;
      div.onclick = () => {
        if (item.type === 'dir') {
          browserNavigate(item.path);
        } else {
          // Select file
          document.getElementById('inputPath').value = item.path;
          state.inputPath = item.path;
          closeBrowser();
          analyzeInput();
        }
      };
      listEl.appendChild(div);
    });
  } catch (e) {}
}

function browserUp() {
  const pathEl = document.getElementById('browserPath');
  const current = pathEl.value;
  // Go to parent
  fetch('/api/browse', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({path: current, type: 'directory'})
  }).then(r => r.json()).then(data => {
    if (data.parent && data.parent !== data.current) {
      browserNavigate(data.parent);
    }
  });
}

function browserConfirm() {
  const path = state.browserCurrentPath;
  if (state.browserTarget === 'input') {
    document.getElementById('inputPath').value = path;
    state.inputPath = path;
    closeBrowser();
    analyzeInput();
  } else {
    document.getElementById('outputPath').value = path;
    state.outputPath = path;
    closeBrowser();
  }
}

// ============ Drag & Drop ============
function initDragDrop() {
  const dropZone = document.getElementById('dropZone');
  const previewArea = document.getElementById('previewArea');
  const body = document.body;

  // 阻止全局默认拖拽行为
  ['dragenter','dragover','dragleave','drop'].forEach(evt => {
    body.addEventListener(evt, e => { e.preventDefault(); e.stopPropagation(); });
  });

  // 拖入高亮
  let dragCounter = 0;
  previewArea.addEventListener('dragenter', () => {
    dragCounter++;
    if (dropZone) dropZone.classList.add('drag-over');
    previewArea.classList.add('drag-over');
  });
  previewArea.addEventListener('dragleave', () => {
    dragCounter--;
    if (dragCounter <= 0) {
      dragCounter = 0;
      if (dropZone) dropZone.classList.remove('drag-over');
      previewArea.classList.remove('drag-over');
    }
  });
  previewArea.addEventListener('drop', e => {
    dragCounter = 0;
    if (dropZone) dropZone.classList.remove('drag-over');
    previewArea.classList.remove('drag-over');
    handleDrop(e);
  });

  // 也支持拖到输入框
  const inputEl = document.getElementById('inputPath');
  inputEl.addEventListener('drop', e => {
    e.preventDefault();
    handleDrop(e);
  });
}

function handleDrop(e) {
  const items = e.dataTransfer.items;
  const files = e.dataTransfer.files;

  if (!items && !files) return;

  // 尝试用 DataTransferItem 判断是文件还是文件夹
  if (items && items.length > 0) {
    const item = items[0];
    
    if (item.webkitGetAsEntry) {
      const entry = item.webkitGetAsEntry();
      if (entry) {
        if (entry.isDirectory) {
          // 文件夹无法通过浏览器上传，提示粘贴路径
          showDropHint('folder', entry.name);
          return;
        }
        // 是文件 → 走下面的上传逻辑
      }
    }
  }

  // 视频文件 → 直接上传到后端
  if (files && files.length > 0) {
    const file = files[0];
    // Electron 环境有 file.path 可直接用
    if (file.path) {
      applyDropPath(file.path);
      return;
    }
    // 普通浏览器 → 上传文件
    uploadFile(file);
  }
}

function uploadFile(file) {
  const infoEl = document.getElementById('inputInfo');
  const inputEl = document.getElementById('inputPath');
  const videoExts = ['mp4','avi','mov','mkv','flv','webm'];
  const ext = file.name.split('.').pop().toLowerCase();

  if (!videoExts.includes(ext)) {
    infoEl.textContent = `⚠️ 不支持的文件格式: .${ext}，请拖入视频文件`;
    return;
  }

  // 显示上传进度
  inputEl.value = '';
  inputEl.placeholder = `正在导入「${file.name}」...`;
  infoEl.innerHTML = '<span class="loading-spinner"></span> 正在导入文件，请稍候...';

  const formData = new FormData();
  formData.append('file', file);

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/upload');

  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = Math.round(e.loaded / e.total * 100);
      infoEl.innerHTML = `<span class="loading-spinner"></span> 正在导入... ${pct}%`;
    }
  };

  xhr.onload = () => {
    if (xhr.status === 200) {
      const data = JSON.parse(xhr.responseText);
      if (data.path) {
        applyDropPath(data.path);
      } else {
        infoEl.textContent = '❌ 上传失败';
      }
    } else {
      infoEl.textContent = '❌ 上传失败: ' + xhr.statusText;
    }
  };

  xhr.onerror = () => {
    infoEl.textContent = '❌ 网络错误，上传失败';
    inputEl.placeholder = '粘贴路径、拖入文件或点击浏览...';
  };

  xhr.send(formData);
}

function showDropHint(type, name) {
  // 仅用于文件夹拖入提示（文件夹无法通过浏览器上传内容）
  const infoEl = document.getElementById('inputInfo');
  const inputEl = document.getElementById('inputPath');
  inputEl.value = '';
  inputEl.placeholder = '请粘贴文件夹的完整路径...';
  infoEl.textContent = `⚠️ 检测到文件夹「${name}」，浏览器无法读取文件夹路径，请粘贴完整路径`;
  inputEl.focus();
}

function applyDropPath(path) {
  // 自动判断类型
  const ext = path.split('.').pop().toLowerCase();
  const videoExts = ['mp4','avi','mov','mkv','flv','webm'];
  if (videoExts.includes(ext)) {
    setInputType('video');
  } else {
    setInputType('sequence');
  }

  document.getElementById('inputPath').value = path;
  state.inputPath = path;
  analyzeInput();
}

// ============ Native File/Folder Picker ============
function handleNativeFile(input) {
  if (input.files && input.files.length > 0) {
    const file = input.files[0];
    if (file.path) {
      setInputType('video');
      applyDropPath(file.path);
    } else {
      // 普通浏览器 → 上传
      setInputType('video');
      uploadFile(file);
    }
    input.value = '';
  }
}

function handleNativeFolder(input) {
  if (input.files && input.files.length > 0) {
    const file = input.files[0];
    if (file.webkitRelativePath) {
      // webkitRelativePath 格式: "folderName/subfile.png"，没有完整路径
      const folderName = file.webkitRelativePath.split('/')[0];
      showDropHint('folder', folderName);
    }
    input.value = '';
  }
}

// 初始化
initDragDrop();
