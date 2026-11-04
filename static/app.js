const $ = (sel) => document.querySelector(sel);
const state = {
  folderId: null,
  items: [],
  path: [],
  meta: null,
  chunkSize: 8 * 1024 * 1024,
  uploading: false,
};

function formatBytes(bytes) {
  bytes = Number(bytes || 0);
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${units[index]}`;
}

function formatDate(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('zh-CN', { hour12: false });
}

function iconFor(item) {
  if (item.kind === 'folder') return '📁';
  const mime = item.mime || '';
  const name = (item.name || '').toLowerCase();
  if (mime.startsWith('video/') || /\.(mp4|webm|mov|mkv|avi)$/.test(name)) return '🎬';
  if (mime.startsWith('image/') || /\.(png|jpe?g|gif|webp|bmp|svg)$/.test(name)) return '🖼️';
  if (mime.startsWith('audio/') || /\.(mp3|wav|flac|m4a|ogg)$/.test(name)) return '🎵';
  if (mime === 'application/pdf' || name.endsWith('.pdf')) return '📕';
  if (/\.(zip|rar|7z|tar|gz)$/.test(name)) return '🗜️';
  if (/\.(docx?|xlsx?|pptx?)$/.test(name)) return '📄';
  return '📄';
}

function showToast(message, timeout = 2600) {
  const toast = $('#toast');
  toast.textContent = message;
  toast.classList.remove('hidden');
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => toast.classList.add('hidden'), timeout);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...options,
    headers: { ...(options.headers || {}) },
  });
  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('application/json')
    ? await response.json()
    : await response.text();
  if (response.status === 401) {
    showLogin();
    throw new Error('请先登录');
  }
  if (!response.ok) {
    const message = (payload && (payload.detail || payload.error)) || `请求失败 (${response.status})`;
    throw new Error(message);
  }
  return payload;
}

function showLogin() {
  $('#login-screen').classList.remove('hidden');
  $('#app').classList.add('hidden');
  setTimeout(() => $('#password').focus(), 50);
}

function showApp() {
  $('#login-screen').classList.add('hidden');
  $('#app').classList.remove('hidden');
}

function setStorageStatus(storage) {
  const pill = $('#storage-pill');
  const warning = $('#storage-warning');
  if (!storage) return;
  if (storage.connected) {
    pill.textContent = storage.mode === 'bot' ? 'Bot 已连接' : '账号已连接';
    pill.className = 'pill online';
    warning.classList.add('hidden');
  } else {
    pill.textContent = 'Telegram 未连接';
    pill.className = 'pill offline';
    warning.textContent = storage.error || 'Telegram 未连接，请检查 .env 配置。';
    warning.classList.remove('hidden');
  }
}

function renderCrumbs() {
  const crumbs = $('#crumbs');
  crumbs.replaceChildren();
  const root = document.createElement('button');
  root.className = `crumb${state.path.length ? '' : ' current'}`;
  root.textContent = '我的文件';
  root.addEventListener('click', () => loadFolder(null));
  crumbs.appendChild(root);
  state.path.forEach((item, index) => {
    const sep = document.createElement('span');
    sep.className = 'crumb-sep';
    sep.textContent = '/';
    crumbs.appendChild(sep);
    const button = document.createElement('button');
    button.className = `crumb${index === state.path.length - 1 ? ' current' : ''}`;
    button.textContent = item.name;
    button.addEventListener('click', () => loadFolder(item.id));
    crumbs.appendChild(button);
  });
}

function renderStats(stats) {
  const row = $('#stats-row');
  if (!stats) { row.replaceChildren(); return; }
  row.replaceChildren();
  const chips = [
    `${stats.files} 个文件`,
    `${stats.folders} 个文件夹`,
    `已用 ${formatBytes(stats.bytes)}`,
  ];
  for (const text of chips) {
    const chip = document.createElement('span');
    chip.className = 'stat-chip';
    chip.textContent = text;
    row.appendChild(chip);
  }
}

function makeAction(label, title, handler, danger = false) {
  const button = document.createElement('button');
  button.className = `icon-btn${danger ? ' danger' : ''}`;
  button.textContent = label;
  button.title = title;
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    handler();
  });
  return button;
}

function renderItems() {
  const body = $('#file-list');
  body.replaceChildren();
  $('#empty-state').classList.toggle('hidden', state.items.length > 0);

  for (const item of state.items) {
    const tr = document.createElement('tr');
    const nameTd = document.createElement('td');
    const nameWrap = document.createElement('div');
    nameWrap.className = 'name-cell';
    const icon = document.createElement('span');
    icon.className = 'file-icon';
    icon.textContent = iconFor(item);
    const name = document.createElement('button');
    name.className = 'name-btn';
    name.textContent = item.name;
    name.title = item.name;
    name.addEventListener('click', () => {
      if (item.kind === 'folder') loadFolder(item.id);
      else previewItem(item);
    });
    nameWrap.append(icon, name);
    nameTd.appendChild(nameWrap);

    const sizeTd = document.createElement('td');
    sizeTd.textContent = item.kind === 'folder' ? '—' : formatBytes(item.size);
    const dateTd = document.createElement('td');
    dateTd.className = 'meta-time';
    dateTd.textContent = formatDate(item.updated_at);

    const actionTd = document.createElement('td');
    actionTd.className = 'actions-col';
    const actions = document.createElement('div');
    actions.className = 'row-actions';
    if (item.kind === 'file') {
      actions.appendChild(makeAction('下载', '下载', () => downloadItem(item)));
      actions.appendChild(makeAction('分享', '创建分享链接', () => shareItem(item)));
    }
    actions.appendChild(makeAction('改名', '重命名', () => renameItem(item)));
    actions.appendChild(makeAction('删除', '删除', () => deleteItem(item), true));
    actionTd.appendChild(actions);

    tr.append(nameTd, sizeTd, dateTd, actionTd);
    body.appendChild(tr);
  }
}

async function loadFolder(folderId = null) {
  try {
    state.folderId = folderId;
    const params = folderId ? `?parent_id=${encodeURIComponent(folderId)}` : '';
    const data = await api(`/api/items${params}`);
    state.items = data.items || [];
    state.path = data.path || [];
    setStorageStatus(data.storage);
    renderCrumbs();
    renderStats(data.stats);
    renderItems();
  } catch (error) {
    showToast(error.message);
  }
}

async function createFolder() {
  const name = prompt('文件夹名称');
  if (!name || !name.trim()) return;
  try {
    await api('/api/folders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name.trim(), parent_id: state.folderId }),
    });
    await loadFolder(state.folderId);
    showToast('文件夹已创建');
  } catch (error) {
    showToast(error.message);
  }
}


async function renameItem(item) {
  const name = prompt('新名称', item.name);
  if (!name || !name.trim() || name.trim() === item.name) return;
  try {
    await api(`/api/items/${encodeURIComponent(item.id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name.trim() }),
    });
    await loadFolder(state.folderId);
    showToast('已重命名');
  } catch (error) {
    showToast(error.message);
  }
}
// 确定删除
// `x ${item.name} y`

async function deleteItem(item) {
  if (!confirm(`确定删除 ${item.name} 吗？文件夹内的内容也会一起删除。`)) return;
  try {
    const result = await api(`/api/items/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
    await loadFolder(state.folderId);
    showToast(result.warning || '已删除');
  } catch (error) {
    showToast(error.message);
  }
}

function downloadItem(item) {
  window.location.href = `/api/files/${encodeURIComponent(item.id)}/download?download=1`;
}

async function shareItem(item) {
  try {
    const result = await api(`/api/items/${encodeURIComponent(item.id)}/share`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expires_hours: 0 }),
    });
    try {
      await navigator.clipboard.writeText(result.url);
      showToast('分享链接已复制');
    } catch {
      prompt('分享链接', result.url);
    }
  } catch (error) {
    showToast(error.message);
  }
}

function previewItem(item) {
  const mime = item.mime || '';
  const src = `/api/files/${encodeURIComponent(item.id)}/download`;
  const body = $('#modal-body');
  body.replaceChildren();
  let element;
  if (mime.startsWith('video/')) {
    element = document.createElement('video');
    element.controls = true;
    element.autoplay = true;
    element.playsInline = true;
  } else if (mime.startsWith('image/')) {
    element = document.createElement('img');
  } else if (mime.startsWith('audio/')) {
    element = document.createElement('audio');
    element.controls = true;
    element.autoplay = true;
  } else {
    downloadItem(item);
    return;
  }
  element.src = src;
  body.appendChild(element);
  $('#modal').classList.remove('hidden');
}

function closeModal() {
  $('#modal').classList.add('hidden');
  $('#modal-body').replaceChildren();
}

function getUploadRow(id) {
  return document.querySelector(`[data-upload-id="${CSS.escape(id)}"]`);
}

function addUploadRow(id, name) {
  $('#upload-panel').classList.remove('hidden');
  const row = document.createElement('div');
  row.className = 'upload-row';
  row.dataset.uploadId = id;
  row.innerHTML = `
    <div class="upload-name"><span></span><span class="pct">0%</span></div>
    <div class="bar"><div></div></div>
    <div class="upload-status">准备上传...</div>
  `;
  row.querySelector('.upload-name span').textContent = name;
  const head = $('#upload-panel .upload-head');
  if (!head) {
    const header = document.createElement('div');
    header.className = 'upload-head';
    header.innerHTML = '<span>上传队列</span><button class="icon-btn" type="button">清空</button>';
    header.querySelector('button').addEventListener('click', () => {
      if (!state.uploading) $('#upload-panel').classList.add('hidden');
    });
    $('#upload-panel').prepend(header);
  }
  $('#upload-panel').appendChild(row);
}

function updateUploadRow(id, percent, status, isError = false) {
  const row = getUploadRow(id);
  if (!row) return;
  row.querySelector('.pct').textContent = `${Math.round(percent)}%`;
  row.querySelector('.bar > div').style.width = `${Math.max(0, Math.min(100, percent))}%`;
  const statusEl = row.querySelector('.upload-status');
  statusEl.textContent = status;
  statusEl.className = `upload-status${isError ? ' error' : ''}`;
}

const uploader = TGUploader.create({
  api,
  formatBytes,
  showToast,
  addUploadRow,
  updateUploadRow,
  getParentId: () => state.folderId,
  onFinished: () => loadFolder(state.folderId),
});

async function uploadOne(file) {
  uploader.enqueue([file]);
}

async function uploadFiles(fileList) {
  const count = uploader.enqueue(fileList);
  if (count) showToast(`已加入 ${count} 个上传任务`);
}

// 页面打开时提示可续传的任务（重新拖入同名文件即从断点继续）
setTimeout(async () => {
  const pending = await uploader.restore();
  if (pending.length) {
    const bytes = pending.reduce((sum, u) => sum + Number(u.received || 0), 0);
    showToast(`有 ${pending.length} 个未完成的上传（共 ${formatBytes(bytes)}），拖入同名文件可继续`, 7000);
  }
}, 1500);

async function init() {
  try {
    const meta = await api('/api/meta');
    state.meta = meta;
    state.chunkSize = meta.chunk_size || state.chunkSize;
    setStorageStatus(meta.storage);
    if (!meta.authenticated) {
      showLogin();
      return;
    }
    showApp();
    await loadFolder(null);
  } catch (error) {
    showLogin();
  }
}

$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('#login-error').textContent = '';
  try {
    await api('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: $('#password').value }),
    });
    $('#password').value = '';
    showApp();
    await loadFolder(null);
  } catch (error) {
    $('#login-error').textContent = error.message;
  }
});

$('#logout-btn').addEventListener('click', async () => {
  await api('/api/logout', { method: 'POST' });
  showLogin();
});
$('#new-folder-btn').addEventListener('click', createFolder);
$('#file-input').addEventListener('change', (event) => {
  uploadFiles(event.target.files);
  event.target.value = '';
});
$('#modal-close').addEventListener('click', closeModal);
$('#modal').addEventListener('click', (event) => {
  if (event.target === $('#modal')) closeModal();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeModal();
});

const dropZone = $('#drop-zone');
['dragenter', 'dragover'].forEach((name) => {
  dropZone.addEventListener(name, (event) => {
    event.preventDefault();
    dropZone.classList.add('dragover');
  });
});
['dragleave', 'drop'].forEach((name) => {
  dropZone.addEventListener(name, (event) => {
    event.preventDefault();
    dropZone.classList.remove('dragover');
  });
});
dropZone.addEventListener('drop', (event) => {
  if (event.dataTransfer && event.dataTransfer.files.length) uploadFiles(event.dataTransfer.files);
});
window.addEventListener('dragover', (event) => event.preventDefault());
window.addEventListener('drop', (event) => event.preventDefault());

init();
