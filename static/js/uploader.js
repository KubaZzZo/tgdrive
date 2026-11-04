/* ============================================================
 * uploader.js — 断点续传上传器
 *
 * 思路：浏览器按 chunk_size 切片，服务端用 Upload-Offset 记录已收字节，
 * 前端把 uploadId 写进 localStorage（按 目录+文件名+大小+修改时间 指纹），
 * 刷新/断网/关标签页后重新拖入同一文件，会自动从服务端 offset 继续。
 * ============================================================ */
(function (global) {
  'use strict';
  var LS_KEY = 'tgdrive_resume_v1';

  function loadMap() { try { return JSON.parse(localStorage.getItem(LS_KEY) || '{}') || {}; } catch (e) { return {}; } }
  function saveMap(m) { try { localStorage.setItem(LS_KEY, JSON.stringify(m)); } catch (e) {} }
  function fp(parentId, file) { return [parentId || '', file.name, file.size, file.lastModified || 0].join('|'); }

  function fmtEta(sec) {
    if (!isFinite(sec) || sec <= 0) return '—';
    if (sec < 60) return Math.ceil(sec) + ' 秒';
    var m = Math.floor(sec / 60);
    if (m < 60) return m + ' 分 ' + Math.ceil(sec % 60) + ' 秒';
    return Math.floor(m / 60) + ' 小时 ' + (m % 60) + ' 分';
  }

  function create(opts) {
    var api = opts.api, formatBytes = opts.formatBytes, showToast = opts.showToast;
    var updateUploadRow = opts.updateUploadRow, addUploadRow = opts.addUploadRow;
    var getParentId = opts.getParentId, onFinished = opts.onFinished;

    var jobs = {};      // uploadId -> job
    var queue = [];
    var active = 0;
    var MAX_ACTIVE = 2;

    function setRow(job, percent, text, isError) {
      if (job.rowId) updateUploadRow(job.rowId, percent, text, !!isError);
    }

    async function ensureUpload(job) {
      var map = loadMap();
      var key = fp(job.parentId, job.file);
      var known = map[key];
      if (known) {
        try {
          var info = await api('/api/uploads/' + encodeURIComponent(known));
          if (info && Number(info.size) === job.file.size) return info;
        } catch (e) { /* 服务端已清理，重建 */ }
        delete map[key]; saveMap(map);
      }
      var created = await api('/api/uploads', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: job.file.name,
          size: job.file.size,
          mime: job.file.type || 'application/octet-stream',
          parent_id: job.parentId
        })
      });
      map[key] = created.id; saveMap(map);
      return created;
    }

    async function runJob(job) {
      var uploadId = null;
      try {
        var info = await ensureUpload(job);
        uploadId = info.id;
        job.uploadId = uploadId;
        jobs[uploadId] = job;
        addUploadRow(uploadId, job.file.name);
        job.rowId = uploadId;

        var chunkSize = Number(info.chunk_size) || 8 * 1024 * 1024;
        var offset = Number(info.offset || info.received || 0);
        var speed = 0, lastT = performance.now(), lastB = offset, attempt = 0;
        setRow(job, 75 * offset / job.file.size, offset > 0 ? ('续传：已有 ' + formatBytes(offset)) : '上传中');

        while (offset < job.file.size) {
          if (job.canceled) throw new Error('已取消');
          while (job.paused) { await new Promise(function (r) { setTimeout(r, 400); }); if (job.canceled) throw new Error('已取消'); }

          var end = Math.min(offset + chunkSize, job.file.size);
          var body = job.file.slice(offset, end);
          var res = null;
          try {
            res = await fetch('/api/uploads/' + encodeURIComponent(uploadId), {
              method: 'PATCH',
              credentials: 'same-origin',
              headers: { 'Upload-Offset': String(offset), 'Content-Type': 'application/octet-stream' },
              body: body
            });
          } catch (netErr) {
            attempt += 1;
            if (attempt > 4) throw new Error('网络中断，重试 4 次仍失败');
            setRow(job, 75 * offset / job.file.size, '网络抖动，' + Math.pow(2, attempt) + 's 后重试…');
            await new Promise(function (r) { setTimeout(r, 1000 * Math.pow(2, attempt - 1)); });
            continue;
          }

          if (res.status === 409) {
            var d409 = await res.json().catch(function () { return {}; });
            offset = Number(d409.offset || 0); attempt = 0; continue;
          }
          if (!res.ok) {
            var derr = await res.json().catch(function () { return {}; });
            var msg = derr.detail || derr.error || ('上传失败 (' + res.status + ')');
            if (res.status >= 500 || res.status === 429) {
              attempt += 1;
              if (attempt > 4) throw new Error(msg);
              await new Promise(function (r) { setTimeout(r, 1000 * Math.pow(2, attempt - 1)); });
              continue;
            }
            throw new Error(msg);
          }

          var data = await res.json().catch(function () { return {}; });
          offset = data.offset != null ? Number(data.offset) : end;
          attempt = 0;

          var now = performance.now(), dt = (now - lastT) / 1000;
          if (dt >= 0.6) { speed = (offset - lastB) / dt; lastT = now; lastB = offset; }
          var eta = speed > 2048 ? (job.file.size - offset) / speed : null;
          setRow(job, 75 * offset / job.file.size,
            '上传中 ' + formatBytes(offset) + ' / ' + formatBytes(job.file.size) +
            (speed > 2048 ? ' · ' + formatBytes(speed) + '/s' : '') +
            (eta !== null ? ' · 剩 ' + fmtEta(eta) : ''));
        }

        setRow(job, 80, '提交到 Telegram…');
        await api('/api/uploads/' + encodeURIComponent(uploadId) + '/complete', { method: 'POST' });

        var guard = 0;
        while (true) {
          await new Promise(function (r) { setTimeout(r, 1200); });
          var u = await api('/api/uploads/' + encodeURIComponent(uploadId));
          if (u.state === 'done') {
            setRow(job, 100, '完成');
            var m2 = loadMap(); delete m2[fp(job.parentId, job.file)]; saveMap(m2);
            if (onFinished) onFinished(job, u);
            return true;
          }
          if (u.state === 'error') throw new Error(u.error || 'Telegram 保存失败');
          var pct = u.size ? 80 + (Number(u.progress || 0) / Number(u.size)) * 20 : 80;
          setRow(job, pct, 'Telegram 保存中 ' + formatBytes(u.progress || 0) + ' / ' + formatBytes(u.size));
          if (++guard > 1500) throw new Error('保存超时');
        }
      } catch (err) {
        var text = (err && err.message) || '上传失败';
        setRow(job, 0, text, true);
        if (showToast) showToast(job.file.name + '：' + text, 4800);
        if (uploadId) delete jobs[uploadId];
        return false;
      }
    }

    function pump() {
      while (active < MAX_ACTIVE && queue.length) {
        var job = queue.shift();
        if (job.canceled) continue;
        active += 1;
        runJob(job).finally(function () { active -= 1; pump(); });
      }
    }

    return {
      enqueue: function (fileList) {
        var files = Array.prototype.slice.call(fileList || []).filter(function (f) { return f.size > 0; });
        files.forEach(function (file) {
          queue.push({ file: file, parentId: getParentId(), paused: false, canceled: false, rowId: null });
        });
        pump();
        return files.length;
      },
      /** 返回服务端尚未完成的上传任务（用于提示用户重新选文件续传） */
      restore: async function () {
        try {
          var data = await api('/api/uploads');
          return (data.uploads || []).filter(function (u) { return Number(u.received) > 0; });
        } catch (e) { return []; }
      },
      cancel: async function (uploadId) {
        var job = jobs[uploadId];
        if (job) job.canceled = true;
        try { await api('/api/uploads/' + encodeURIComponent(uploadId), { method: 'DELETE' }); } catch (e) {}
      }
    };
  }

  global.TGUploader = { create: create };
})(window);
