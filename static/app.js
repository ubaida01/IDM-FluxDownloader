/* Flux Download Manager — dependency-free, accessible client. */
(() => {
  'use strict';

  const state = {
    jobs: [], filter: 'all', search: '', sort: 'newest', config: null,
    probe: null, qualities: [], quality: 'best', formatsExpanded: true,
    mediaMode: 'video', qualityByMode: { video: 'best', audio: 'audio:source' },
    jobSaveDir: null, pendingDefaultDir: null, folderPurpose: null, folderReturn: null, folderData: null, folderBusy: false,
    modal: null, busy: false, fetching: false, menu: null,
  };
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const icon = (name, extra = '') => `<svg class="icon ${extra}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
  const escapeHTML = (value) => String(value == null ? '' : value).replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[char]));
  const clamp = (n, min, max) => Math.min(max, Math.max(min, n));
  const formatBytes = (value) => {
    const n = Number(value) || 0;
    if (n < 1024) return `${Math.round(n)} B`;
    const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
    let num = n;
    let i = -1;
    do { num /= 1024; i++; } while (num >= 1024 && i < units.length - 1);
    return `${num >= 100 ? num.toFixed(0) : num >= 10 ? num.toFixed(1) : num.toFixed(2)} ${units[i]}`;
  };
  const formatSpeed = (n) => `${formatBytes(n)}/s`;
  const formatEta = (seconds) => {
    if (seconds == null || !Number.isFinite(Number(seconds))) return 'Estimating…';
    seconds = Math.max(0, Math.ceil(Number(seconds)));
    if (seconds >= 3600) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m remaining`;
    if (seconds >= 60) return `${Math.floor(seconds / 60)}m ${seconds % 60}s remaining`;
    return `${seconds}s remaining`;
  };
  const safeImageUrl = (url) => {
    try {
      const obj = new URL(url);
      return obj.protocol === 'http:' || obj.protocol === 'https:' ? obj.href : '';
    } catch { return ''; }
  };
  const hostname = (url) => {
    try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return 'Direct URL'; }
  };
  const actionable = (status) => ['downloading', 'processing', 'pausing', 'queued', 'paused', 'failed', 'cancelled'].includes(status);
  const activeStatus = (status) => ['downloading', 'processing', 'pausing'].includes(status);
  const shortError = (text, length = 105) => String(text || '').length > length ? String(text).slice(0, length) + '…' : text;

  async function api(path, options = {}) {
    let response;
    try {
      response = await fetch(path, {
        ...options,
        headers: { 'Accept': 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
        cache: 'no-store',
      });
    } catch {
      throw new Error('Cannot reach the local download engine. Check that the server is running.');
    }
    let data;
    try { data = await response.json(); } catch { data = {}; }
    if (!response.ok) {
      const detail = data?.detail;
      let message = Array.isArray(detail) ? detail.map(e => e.msg).join('; ') : String(detail || `Request failed (HTTP ${response.status})`);
      throw new Error(message);
    }
    return data;
  }

  function toast(message, type = 'success') {
    const el = document.createElement('div');
    el.className = `toast ${type === 'error' ? 'is-error' : ''}`;
    el.innerHTML = icon(type === 'error' ? 'alert' : 'checkcircle') + `<span>${escapeHTML(message)}</span>`;
    $('#toast-stack').appendChild(el);
    setTimeout(() => { el.remove(); }, 4300);
  }

  function displayConfig(config) {
    if (!config) return;
    state.config = config;
    const free = Number(config.disk_free || 0);
    const total = Number(config.disk_total || 0);
    $('#storage-remaining').textContent = `${formatBytes(free)} available`;
    $('#storage-bar').style.width = `${total ? clamp(100 * (total - free) / total, 0, 100) : 0}%`;
    if (state.modal !== 'settings-modal' || !state.pendingDefaultDir) state.pendingDefaultDir = config.download_dir || '';
    $('#download-directory').textContent = state.pendingDefaultDir || config.download_dir || '';
    $('#setting-concurrency').value = config.settings?.concurrency ?? 3;
    $('#setting-connections').value = config.settings?.connections_per_download ?? 4;
    $('#setting-limit').value = config.settings?.speed_limit_kbps ?? 0;
    $('#setting-retries').value = config.settings?.auto_retries ?? 3;
    const caps = config.capabilities || {};
    $('#capabilities-list').innerHTML = `
      <div><span class="${caps.yt_dlp ? 'component-good' : 'component-bad'}">${caps.yt_dlp ? '●' : '○'}</span> yt-dlp — ${caps.yt_dlp ? 'Available for video and audio sites' : 'Not installed (direct files still work)'}</div>
      <div><span class="${caps.ffmpeg ? 'component-good' : 'component-bad'}">${caps.ffmpeg ? '●' : '○'}</span> FFmpeg — ${caps.ffmpeg ? 'Ready to merge and convert media' : 'Not found — merging/conversion may fail'}</div>`;
    const good = caps.ffmpeg && caps.yt_dlp;
    $('#health-label').textContent = good ? 'All systems ready' : caps.yt_dlp || caps.ffmpeg ? 'Partially ready' : 'File mode ready';
    $('.health-dot').classList.toggle('warning', !good);
  }

  async function loadConfig() {
    try { displayConfig(await api('/api/config')); }
    catch (error) { $('#health-label').textContent = 'Engine offline'; $('.health-dot').classList.add('warning'); }
  }

  async function loadJobs() {
    if (state.fetching) return;
    state.fetching = true;
    try {
      const response = await api('/api/jobs');
      state.jobs = response.jobs || [];
      draw();
    } catch (err) {
      $('#health-label').textContent = 'Engine offline';
      $('.health-dot').classList.add('warning');
    } finally {
      state.fetching = false;
    }
  }

  function draw() {
    const jobs = state.jobs;
    const running = jobs.filter(job => activeStatus(job.status));
    const waiting = jobs.filter(job => job.status === 'queued');
    const completed = jobs.filter(job => job.status === 'completed');
    const failed = jobs.filter(job => job.status === 'failed');
    const rate = running.reduce((n, job) => n + (Number(job.speed) || 0), 0);
    const completedBytes = completed.reduce((n, job) => n + (Number(job.total_bytes) || 0), 0);
    $('#stat-active').textContent = running.length;
    $('#stat-queued').textContent = waiting.length;
    $('#stat-completed').textContent = completed.length;
    $('#stat-data').textContent = formatBytes(completedBytes);
    $('#stat-speed').textContent = running.length ? `${formatSpeed(rate)} right now` : 'Waiting for transfers';
    $('#activity-speed').textContent = formatSpeed(rate);
    $('#activity-running').textContent = running.length;
    $('#activity-queue').textContent = waiting.length;
    const today = new Date().toDateString();
    $('#activity-today').textContent = completed.filter(job => new Date(job.updated_at).toDateString() === today).length;
    $('.activity-wave').classList.toggle('active', rate > 0);
    $('#nav-all').textContent = jobs.length;
    $('#nav-downloading').textContent = running.length;
    $('#nav-queued').textContent = waiting.length;
    $('#nav-completed').textContent = completed.length;
    $('#nav-failed').textContent = failed.length;
    $('#total-badge').textContent = jobs.length;
    const query = state.search.trim().toLowerCase();
    const visible = jobs.filter(job => {
      const statusMatch = state.filter === 'all' || (state.filter === 'downloading' ? activeStatus(job.status)
        : state.filter === 'failed' ? ['failed', 'cancelled'].includes(job.status)
          : state.filter === 'queued' ? ['queued', 'paused'].includes(job.status)
            : job.status === state.filter);
      if (!statusMatch) return false;
      return !query || `${job.title} ${job.filename || ''} ${job.url} ${job.quality_label}`.toLowerCase().includes(query);
    });
    visible.sort((a, b) => {
      switch (state.sort) {
        case 'oldest': return a.created_at.localeCompare(b.created_at);
        case 'priority': return b.priority - a.priority || b.created_at.localeCompare(a.created_at);
        case 'name': return a.title.localeCompare(b.title);
        case 'size': return (b.total_bytes || 0) - (a.total_bytes || 0);
        default: return b.created_at.localeCompare(a.created_at);
      }
    });
    const list = $('#download-list');
    list.innerHTML = visible.length ? visible.map(renderRow).join('') : emptyState();
    $('#list-summary').textContent = visible.length ? `Showing ${visible.length} of ${jobs.length} transfers` : 'No matching downloads';
  }

  function classify(job) {
    const ext = (job.filename || job.title || '').toLowerCase().split('.').pop();
    if (job.kind === 'media' || ['mkv', 'mp4', 'webm', 'mov', 'avi'].includes(ext)) {
      if (job.quality?.startsWith('audio:') || ['m4a', 'mp3', 'flac', 'wav', 'opus'].includes(ext)) return ['music', 'music'];
      return ['video', 'video'];
    }
    if (['mp3', 'm4a', 'wav', 'opus', 'flac'].includes(ext)) return ['music', 'music'];
    if (['zip', '7z', 'rar', 'gz', 'tar', 'iso'].includes(ext)) return ['archive', 'folder'];
    if (['pdf', 'docx', 'doc', 'txt', 'csv', 'xlsx', 'pptx', 'epub'].includes(ext)) return ['document', 'file'];
    return ['file', 'file'];
  }

  function renderRow(job) {
    const [type, glyph] = classify(job);
    const id = escapeHTML(job.id);
    const status = job.status;
    const total = Number(job.total_bytes || 0);
    const received = Number(job.downloaded_bytes || 0);
    const percentage = total ? clamp(Math.round(received * 100 / total), 0, 100) : 0;
    const active = ['downloading', 'processing'].includes(status);
    const progress = active || status === 'paused' || status === 'pausing';
    const width = total ? percentage : 0;
    const subline = status === 'downloading' ? `${formatSpeed(job.speed || 0)}${job.eta != null ? ` · ${formatEta(job.eta)}` : ''}`
      : status === 'processing' ? 'Merging or processing media…'
        : status === 'queued' ? 'Waiting for a free slot'
          : status === 'paused' ? 'Paused — ready to resume'
            : status === 'pausing' ? 'Finishing the current network operation…'
              : status === 'failed' ? shortError(job.error || 'Download failed')
                : status === 'cancelled' ? 'Download cancelled'
                  : job.filename || job.quality_label || 'Original file';
    const percentText = total ? `${percentage}%` : '';
    const statusLabels = { downloading: 'Downloading', processing: 'Processing', pausing: 'Pausing', queued: 'Queued',
      paused: 'Paused', completed: 'Completed', failed: 'Failed', cancelled: 'Cancelled' };
    const source = hostname(job.url);
    const thumb = job.kind === 'media' ? safeImageUrl(job.thumbnail) : '';
    const visual = thumb ? `<img src="${escapeHTML(thumb)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display='none'">` : icon(glyph);
    let primary = '';
    if (status === 'completed') primary = `<a class="row-action action-primary" href="/api/jobs/${id}/file" title="Save completed file" aria-label="Save ${escapeHTML(job.title)}">${icon('download')}</a>`;
    else if (status === 'paused' || status === 'failed' || status === 'cancelled') primary = `<button class="row-action action-primary" data-job-action="resume" data-id="${id}" title="Resume or retry" aria-label="Resume ${escapeHTML(job.title)}">${icon('play')}</button>`;
    else if (status !== 'pausing') primary = `<button class="row-action action-primary" data-job-action="pause" data-id="${id}" title="Pause download" aria-label="Pause ${escapeHTML(job.title)}">${icon('pause')}</button>`;
    const menu = state.menu === job.id ? `<div class="row-more" role="menu">
      <button data-job-action="copy" data-id="${id}">${icon('copy')} Copy source URL</button>
      <button data-job-action="copy-location" data-id="${id}">${icon('folder')} Copy ${status === 'completed' ? 'saved file' : 'destination'} path</button>
      ${job.status === 'queued' ? `<button data-job-action="priority-up" data-id="${id}">${icon('arrowup')} Increase priority</button><button data-job-action="priority-down" data-id="${id}">${icon('clock')} Lower priority</button>` : ''}
      ${actionable(job.status) && job.status !== 'cancelled' && job.status !== 'failed' ? `<button data-job-action="cancel" data-id="${id}">${icon('x')} Cancel download</button>` : ''}
      <button class="danger-menu" data-job-action="delete" data-id="${id}">${icon('trash')} Remove from list</button>
      ${job.status === 'completed' ? `<button class="danger-menu" data-job-action="delete-file" data-id="${id}">${icon('trash')} Delete saved file</button>` : ''}
    </div>` : '';
    const note = progress ? `<div class="row-progress ${status === 'paused' ? 'paused' : ''} ${total ? '' : 'indeterminate'}"><span style="width:${width ? width + '%' : ''}"></span></div><div class="file-subline">${escapeHTML(subline)}${percentText ? `<span class="dot-separator">·</span>${percentText}` : ''}</div>`
      : `<div class="file-source" title="${escapeHTML(job.final_path || job.save_dir || '')}">${icon('globe')} ${escapeHTML(source)} <span>·</span> ${escapeHTML(subline)}</div>`;
    return `<article class="download-row" data-row-id="${id}">
      <div class="file-info"><div class="file-visual type-${type}">${visual}</div><div class="file-main"><div class="file-title" title="${escapeHTML(job.title)}">${escapeHTML(job.title)}</div>${note}</div></div>
      <div class="file-size">${total ? formatBytes(total) : received ? formatBytes(received) : '—'}<small>${total && received && progress ? `${formatBytes(received)} saved` : escapeHTML(job.quality_label || '')}</small></div>
      <div><span class="status-chip status-${escapeHTML(status)}" title="${escapeHTML(job.error || status)}">${escapeHTML(statusLabels[status] || status)}</span></div>
      <div class="row-actions">${primary}<button class="row-action" data-job-action="menu" data-id="${id}" aria-label="More actions for ${escapeHTML(job.title)}" title="More actions">${icon('more')}</button>${menu}</div>
    </article>`;
  }

  function emptyState() {
    const isFilter = state.filter !== 'all' || !!state.search;
    const title = isFilter ? 'Nothing found here.' : 'Nothing in your queue. Yet.';
    const body = isFilter ? 'Try a different search or category to find your downloads.'
      : "Great things start with a single link. Add your first download and we'll take care of the rest.";
    return `<div class="empty-state"><span class="empty-art">${icon(isFilter ? 'search' : 'inbox')}</span>
      <h3>${escapeHTML(title)}</h3><p>${escapeHTML(body)}</p>
      ${isFilter ? '<button class="btn btn-secondary" data-action="reset-filter">Show all downloads</button>' : `<button class="btn btn-primary" data-action="add">${icon('plus')} Add your first download</button>`}</div>`;
  }

  function setFilter(filter) {
    state.filter = filter;
    state.menu = null;
    $$('.nav-item[data-view]').forEach(el => el.classList.toggle('active', el.dataset.view === filter));
    $$('.filter-tab').forEach(el => { const selected = el.dataset.filter === filter; el.classList.toggle('selected', selected); el.setAttribute('aria-selected', String(selected)); });
    $('#sidebar').classList.remove('open');
    $('#mobile-scrim').hidden = true;
    draw();
  }

  function showModal(id) {
    closeModal();
    state.modal = id;
    const el = $(`#${id}`);
    el.hidden = false;
    document.body.style.overflow = 'hidden';
  }
  function closeModal() {
    $$('.modal-backdrop').forEach(el => { el.hidden = true; });
    state.modal = null;
    document.body.style.overflow = '';
  }

  function getUrls() {
    const text = $('#url-entry').value.trim();
    return (text.match(/https?:\/\/[^\s]+/gi) || []).map(url => url.replace(/[),;]+$/, '')).filter(Boolean);
  }
  function chosenKind() { return $('input[name="download-kind"]:checked')?.value || 'auto'; }
  function addError(message) {
    const box = $('#add-error');
    box.hidden = !message;
    box.textContent = message || '';
  }
  function setBusy(value) {
    state.busy = value;
    $('#analyze-btn').disabled = value;
    $('#submit-download').disabled = value;
    $('#analyze-btn').innerHTML = value ? `${icon('refresh')} Working…` : `${icon('search')} Inspect link`;
  }
  function syncSubmitLabel() {
    $('#submit-label').textContent = $('#playlist-check').checked ? 'Import playlist'
      : (chosenKind() === 'media' || state.probe?.kind === 'media')
        ? state.mediaMode === 'audio' ? 'Download audio' : 'Download video'
        : 'Start download';
  }
  function updateDestinationLabel() {
    $('#job-save-dir').textContent = state.jobSaveDir || state.config?.download_dir || 'Default downloads folder';
    $('#job-save-dir').title = $('#job-save-dir').textContent;
  }
  function openAdd(prefill = '') {
    state.probe = null;
    state.quality = 'best';
    state.qualities = [];
    state.mediaMode = 'video';
    state.qualityByMode = { video: 'best', audio: 'audio:source' };
    state.formatsExpanded = true;
    state.jobSaveDir = state.config?.download_dir || null;
    $('#url-entry').value = prefill || '';
    $('input[name="download-kind"][value="auto"]').checked = true;
    $('input[name="media-output"][value="video"]').checked = true;
    $('#playlist-check').checked = false;
    $('#probe-result').hidden = true;
    $('#probe-message').hidden = false;
    $('#quality-area').hidden = true;
    $('#toggle-formats').hidden = true;
    updateDestinationLabel();
    updateMediaModeVisibility();
    addError('');
    showModal('add-modal');
    $('#url-entry').focus();
  }
  function resetInspection() {
    state.probe = null;
    state.qualities = [];
    state.quality = state.qualityByMode[state.mediaMode];
    $('#probe-result').hidden = true;
    $('#probe-message').hidden = false;
    $('#probe-message').querySelector('strong').textContent = 'Quality-aware downloads';
    $('#probe-message').querySelector('span').textContent = 'Inspect a single link to see actual video resolutions and source audio bitrates.';
    addError('');
    updateMediaModeVisibility();
  }
  function updateMediaModeVisibility() {
    const media = chosenKind() === 'media' || state.probe?.kind === 'media';
    $('#media-choice').hidden = !media;
    if (media) {
      const audioOnly = Boolean(state.probe?.kind === 'media' && state.probe.supports_video === false);
      $('input[name="media-output"][value="video"]').disabled = audioOnly;
      if (audioOnly) state.mediaMode = 'audio';
      $('input[name="media-output"][value="' + state.mediaMode + '"]').checked = true;
      $('#media-choice-tip').textContent = audioOnly ? 'This source contains audio only; video quality is unavailable.'
        : state.probe ? 'Choose from formats actually reported by this source. Converted MP3 bitrates do not increase fidelity.'
          : 'Inspect the link to see which picture resolutions and audio bitrates the site actually provides.';
      renderQualities();
    } else {
      $('#quality-area').hidden = true;
    }
    syncSubmitLabel();
  }
  function displayProbe(info) {
    state.probe = info;
    $('#probe-message').hidden = true;
    const thumbnail = safeImageUrl(info.thumbnail);
    const prefix = thumbnail ? `<img class="probe-thumbnail" src="${escapeHTML(thumbnail)}" referrerpolicy="no-referrer" alt="Media thumbnail">`
      : `<span class="probe-thumbnail" style="display:flex;align-items:center;justify-content:center;color:#a9a4f2">${icon(info.kind === 'media' ? 'video' : 'file')}</span>`;
    $('#probe-result').innerHTML = `${prefix}<div style="min-width:0"><div class="probe-title" title="${escapeHTML(info.title)}">${escapeHTML(info.title)}</div><div class="probe-sub">${info.kind === 'media' ? 'Media' : 'Direct file'}${info.max_height ? ` · up to ${Number(info.max_height)}p` : ''}${info.size ? ' · ' + formatBytes(info.size) : ''}${info.format_count ? ' · ' + info.format_count + ' source formats' : ''}</div></div>`;
    $('#probe-result').hidden = false;
    state.formatsExpanded = true;
    updateMediaModeVisibility();
  }
  function genericQualityOptions() {
    if (state.mediaMode === 'video') return [{ value: 'best', label: 'Best video + audio',
      detail: 'Highest quality from the source. Inspect the link for available resolutions.', recommended: true }];
    const basics = [
      { value: 'audio:source', label: 'Best original audio', detail: 'Keep source quality, no artificial bitrate increase', recommended: true },
      { value: 'audio:mp3', label: 'MP3 · best VBR', detail: 'Convert using FFmpeg · high variable bitrate', group: 'MP3 conversion' },
    ];
    for (const bitrate of [320, 256, 192, 128, 96, 64]) basics.push({
      value: `audio:mp3:${bitrate}`, label: `MP3 · ${bitrate} kbps`,
      detail: 'Converted bitrate; does not add detail missing from the original', group: 'MP3 conversion',
    });
    return basics;
  }
  function renderQualities() {
    const info = state.probe;
    const options = info?.kind === 'media'
      ? (state.mediaMode === 'video' ? info.video_quality_options : info.audio_quality_options) || []
      : genericQualityOptions();
    state.qualities = options;
    const recommended = options.find(item => item.recommended)?.value || options[0]?.value;
    const saved = state.qualityByMode[state.mediaMode];
    const selected = options.some(item => item.value === saved) ? saved : recommended;
    state.quality = selected || (state.mediaMode === 'video' ? 'best' : 'audio:source');
    state.qualityByMode[state.mediaMode] = state.quality;
    $('#quality-area').hidden = !(chosenKind() === 'media' || info?.kind === 'media');
    $('#quality-heading-label').textContent = state.mediaMode === 'audio' ? 'AUDIO QUALITY' : 'VIDEO RESOLUTION & QUALITY';
    $('#quality-hint').textContent = state.mediaMode === 'audio' ? 'Original source recommended' : 'Highest available preselected';
    const hiddenGroup = state.mediaMode === 'audio' ? 'Source audio streams' : 'Specific formats';
    const hasSpecific = options.some(opt => opt.group === hiddenGroup);
    $('#toggle-formats').hidden = !hasSpecific;
    $('#toggle-formats').innerHTML = `${state.formatsExpanded ? 'Hide' : 'Show'} ${state.mediaMode === 'audio' ? 'source audio tracks' : 'individual video streams'} ${icon('chevron')}`;
    const shown = options.filter(opt => opt.group !== hiddenGroup || state.formatsExpanded);
    let previousGroup = null;
    $('#quality-list').innerHTML = shown.map(opt => {
      const section = opt.group || 'Recommended';
      const caption = section !== previousGroup ? `<div class="quality-section-caption">${escapeHTML(section)}</div>` : '';
      previousGroup = section;
      return `${caption}<label class="quality-item"><input type="radio" name="quality" value="${escapeHTML(opt.value)}" ${state.quality === opt.value ? 'checked' : ''}>
        <span><strong>${escapeHTML(opt.label)}</strong><small>${escapeHTML(opt.detail || '')}</small></span>${opt.recommended ? '<em class="reco">RECOMMENDED</em>' : ''}</label>`;
    }).join('');
  }
  function changeMediaMode(mode) {
    if (!['video', 'audio'].includes(mode)) return;
    state.mediaMode = mode;
    renderQualities();
    syncSubmitLabel();
  }

  async function inspect() {
    addError('');
    const urls = getUrls();
    if (!urls.length) { addError('Paste a valid HTTP or HTTPS link first.'); return; }
    if ($('#playlist-check').checked) {
      resetInspection();
      $('#probe-message').querySelector('strong').textContent = 'Playlist import';
      $('#probe-message').querySelector('span').textContent = 'Playlist entries will be detected and queued when you choose Import playlist.';
      return;
    }
    if (urls.length > 1) {
      resetInspection();
      $('#probe-message').querySelector('strong').textContent = `${urls.length} links detected`;
      $('#probe-message').querySelector('span').textContent = 'Batch mode will add all links to the queue. Use Video / audio for best-quality media downloads.';
      return;
    }
    setBusy(true);
    try {
      const data = await api('/api/probe', { method: 'POST', body: JSON.stringify({ url: urls[0], kind: chosenKind() }) });
      displayProbe(data);
      toast('Link inspected. Select your preferred quality.');
    } catch (err) { addError(err.message); }
    finally { setBusy(false); }
  }

  async function submitDownload() {
    if (state.busy) return;
    addError('');
    const urls = getUrls();
    if (!urls.length) { addError('Paste at least one valid HTTP or HTTPS link.'); return; }
    if (urls.length > 40 && !$('#playlist-check').checked) { addError('Batch downloads are limited to 40 links at once.'); return; }
    setBusy(true);
    try {
      let result, number = 1;
      const kind = chosenKind();
      const quality = (kind === 'media' || state.probe?.kind === 'media')
        ? state.quality || (state.mediaMode === 'audio' ? 'audio:source' : 'best') : 'best';
      const save_dir = state.jobSaveDir || null;
      if ($('#playlist-check').checked) {
        if (urls.length !== 1) throw new Error('Provide one playlist URL at a time.');
        result = await api('/api/playlists/import', { method: 'POST', body: JSON.stringify({ url: urls[0], quality: kind === 'file' ? 'best' : quality, max_items: 100, save_dir }) });
        number = result.count;
      } else if (urls.length > 1) {
        result = await api('/api/jobs/batch', { method: 'POST', body: JSON.stringify({ urls, kind, quality, save_dir }) });
        number = result.added?.length || urls.length;
      } else {
        const info = state.probe;
        result = await api('/api/jobs', { method: 'POST', body: JSON.stringify({ url: urls[0],
          kind: info?.kind || kind, quality: info?.kind === 'media' || kind === 'media' ? quality : 'original',
          save_dir,
          title: info?.title || null, thumbnail: info?.thumbnail || null, duration: info?.duration || null,
          size: info?.size || null }) });
      }
      closeModal();
      state.filter = 'all';
      setFilter('all');
      toast(number === 1 ? 'Download added to your queue.' : `${number} downloads added to your queue.`);
      await loadJobs();
    } catch (err) { addError(err.message); }
    finally { setBusy(false); }
  }

  async function saveSettings() {
    const concurrency = Number($('#setting-concurrency').value);
    const speed_limit_kbps = Number($('#setting-limit').value);
    const connections_per_download = Number($('#setting-connections').value);
    const auto_retries = Number($('#setting-retries').value);
    if (!Number.isInteger(speed_limit_kbps) || speed_limit_kbps < 0 || speed_limit_kbps > 1_000_000) {
      toast('Enter a valid nonnegative speed limit.', 'error'); return;
    }
    const button = $('#save-settings'); button.disabled = true;
    try {
      await api('/api/config', { method: 'PATCH', body: JSON.stringify({ concurrency, connections_per_download, speed_limit_kbps, auto_retries,
        default_save_dir: state.pendingDefaultDir || state.config?.download_dir }) });
      await loadConfig(); closeModal(); toast('Preferences saved.');
    } catch (err) { toast(err.message, 'error'); }
    finally { button.disabled = false; }
  }

  async function jobAction(action, id) {
    const job = state.jobs.find(x => x.id === id);
    if (!job) return;
    state.menu = null;
    if (action === 'copy-location') {
      try { await navigator.clipboard.writeText(job.final_path || job.save_dir || ''); toast('Saved location copied.'); }
      catch { toast('Could not access clipboard.', 'error'); }
      return;
    }
    if (action === 'copy') {
      try { await navigator.clipboard.writeText(job.url); toast('Source link copied to clipboard.'); }
      catch { toast('Clipboard permission denied. Try copying the URL manually.', 'error'); }
      return;
    }
    if (action === 'delete' || action === 'delete-file') {
      const deleteFile = action === 'delete-file';
      const question = deleteFile ? `Permanently delete "${job.title}" from this computer and remove it from your history?`
        : `Remove "${job.title}" from the download list? Completed files stay on your computer.`;
      if (!window.confirm(question)) return;
      try {
        await api(`/api/jobs/${encodeURIComponent(id)}${deleteFile ? '?delete_file=true' : ''}`, { method: 'DELETE' });
        toast(deleteFile ? 'Saved file and history entry deleted.' : 'Download removed from the list.');
        await loadJobs();
      } catch (err) { toast(err.message, 'error'); }
      return;
    }
    try {
      if (action === 'priority-up' || action === 'priority-down') {
        const priority = clamp(job.priority + (action === 'priority-up' ? 1 : -1), -10, 10);
        await api(`/api/jobs/${id}/priority`, { method: 'PATCH', body: JSON.stringify({ priority }) });
        toast('Queue priority updated.');
      } else {
        await api(`/api/jobs/${id}/${action}`, { method: 'POST' });
        const verbs = { pause: 'Pausing download…', resume: 'Download queued for resuming.', cancel: 'Cancelling download…' };
        toast(verbs[action] || 'Download updated.');
      }
      await loadJobs();
    } catch (err) { toast(err.message, 'error'); }
  }

  function closeMobile() {
    $('#sidebar').classList.remove('open');
    $('#mobile-scrim').hidden = true;
  }

  async function globalAction(action) {
    switch (action) {
      case 'add': openAdd(); closeMobile(); break;
      case 'paste': {
        openAdd();
        try { const text = await navigator.clipboard.readText(); if (text) $('#url-entry').value = text; }
        catch { toast('Paste a URL using Ctrl+V / ⌘V.'); }
        break;
      }
      case 'export': window.location.href = '/api/export'; closeMobile(); break;
      case 'settings': await loadConfig(); state.pendingDefaultDir = state.config?.download_dir || ''; $('#download-directory').textContent = state.pendingDefaultDir; showModal('settings-modal'); closeMobile(); break;
      case 'help': showModal('help-modal'); break;
      case 'refresh': await Promise.all([loadJobs(), loadConfig()]); toast('Download list refreshed.'); break;
      case 'clear': {
        const completed = state.jobs.filter(job => job.status === 'completed').length;
        if (!completed) { toast('No completed downloads to clear.'); break; }
        if (!window.confirm(`Remove ${completed} completed downloads from history? Files will remain saved.`)) break;
        try { const result = await api('/api/completed', { method: 'DELETE' }); toast(`Cleared ${result.cleared} completed entries.`); await loadJobs(); }
        catch (err) { toast(err.message, 'error'); }
        break;
      }
      case 'menu': $('#sidebar').classList.add('open'); $('#mobile-scrim').hidden = false; break;
      case 'reset-filter': $('#search').value = ''; state.search = ''; setFilter('all'); break;
    }
  }

  function folderError(message) {
    $('#folder-error').hidden = !message;
    $('#folder-error').textContent = message || '';
  }
  async function openFolderPicker(purpose) {
    state.folderPurpose = purpose;
    state.folderReturn = purpose === 'job' ? 'add-modal' : 'settings-modal';
    state.folderData = null;
    showModal('folder-modal');
    $('#folder-create-form').hidden = true;
    const startingPath = purpose === 'job'
      ? state.jobSaveDir || state.config?.download_dir : state.pendingDefaultDir || state.config?.download_dir;
    await browseFolder(startingPath || null);
  }
  async function browseFolder(path) {
    if (state.folderBusy) return;
    state.folderBusy = true;
    folderError('');
    try {
      const data = await api('/api/folders' + (path ? '?path=' + encodeURIComponent(path) : ''));
      state.folderData = data;
      $('#folder-current').textContent = data.path;
      $('#folder-current').title = data.path;
      $('#folder-custom-path').value = data.path;
      $('#folder-up').disabled = !data.parent;
      $('#folder-roots').innerHTML = data.roots.map(root => `<button class="folder-root ${data.path === root.path ? 'active' : ''}" type="button" data-folder-target="${escapeHTML(root.path)}">${icon('folder')} ${escapeHTML(root.name)}</button>`).join('');
      $('#folder-list').innerHTML = data.directories.length
        ? data.directories.map(entry => `<button class="folder-list-item" type="button" data-folder-target="${escapeHTML(entry.path)}">${icon('folder')}<span>${escapeHTML(entry.name)}</span><span class="folder-arrow">›</span></button>`).join('')
        : '<div class="folder-empty">No subfolders here. You can select this folder or create a new one.</div>';
    } catch (error) { folderError(error.message); }
    finally { state.folderBusy = false; }
  }
  function returnFromFolderPicker() { showModal(state.folderReturn || 'add-modal'); folderError(''); }
  async function selectFolder() {
    if (!state.folderData || state.folderBusy) return;
    const button = $('#folder-select');
    button.disabled = true;
    folderError('');
    try {
      const result = await api('/api/folders/validate', { method: 'POST', body: JSON.stringify({ path: state.folderData.path }) });
      if (state.folderPurpose === 'job') {
        state.jobSaveDir = result.path;
        updateDestinationLabel();
      } else {
        state.pendingDefaultDir = result.path;
        $('#download-directory').textContent = result.path;
      }
      returnFromFolderPicker();
    } catch (err) { folderError(err.message); }
    finally { button.disabled = false; }
  }
  async function createFolder() {
    if (!state.folderData) return;
    folderError('');
    const name = $('#folder-create-name').value.trim();
    if (!name) { folderError('Enter a name for the new folder.'); return; }
    const button = $('#folder-create-save'); button.disabled = true;
    try {
      const result = await api('/api/folders', { method: 'POST', body: JSON.stringify({ parent: state.folderData.path, name }) });
      $('#folder-create-name').value = '';
      $('#folder-create-form').hidden = true;
      await browseFolder(result.path);
      toast('Folder created. Select it to use it.');
    } catch (err) { folderError(err.message); }
    finally { button.disabled = false; }
  }
  $('#folder-roots').addEventListener('click', e => {
    const target = e.target.closest('[data-folder-target]'); if (target) browseFolder(target.dataset.folderTarget);
  });
  $('#folder-list').addEventListener('click', e => {
    const target = e.target.closest('[data-folder-target]'); if (target) browseFolder(target.dataset.folderTarget);
  });
  $('#folder-up').addEventListener('click', () => { if (state.folderData?.parent) browseFolder(state.folderData.parent); });
  $('#folder-go').addEventListener('click', () => browseFolder($('#folder-custom-path').value.trim()));
  $('#folder-custom-path').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); browseFolder(e.target.value.trim()); }
  });
  $('#folder-create-toggle').addEventListener('click', () => {
    $('#folder-create-form').hidden = !$('#folder-create-form').hidden;
    if (!$('#folder-create-form').hidden) $('#folder-create-name').focus();
  });
  $('#folder-create-save').addEventListener('click', createFolder);
  $('#folder-create-name').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); createFolder(); } });
  $('#folder-select').addEventListener('click', selectFolder);
  $('#folder-close').addEventListener('click', returnFromFolderPicker);
  $('#folder-cancel').addEventListener('click', returnFromFolderPicker);

  document.addEventListener('click', (event) => {
    const close = event.target.closest('[data-close]');
    if (close) { closeModal(); return; }
    const action = event.target.closest('[data-job-action]');
    if (action) {
      event.preventDefault(); event.stopPropagation();
      if (action.dataset.jobAction === 'menu') {
        state.menu = state.menu === action.dataset.id ? null : action.dataset.id;
        draw();
      } else jobAction(action.dataset.jobAction, action.dataset.id);
      return;
    }
    const tab = event.target.closest('[data-filter], .nav-item[data-view]');
    if (tab) { setFilter(tab.dataset.filter || tab.dataset.view); return; }
    const global = event.target.closest('[data-action]');
    if (global) { globalAction(global.dataset.action); return; }
    if (state.menu && !event.target.closest('.row-more')) { state.menu = null; draw(); }
    if (event.target.classList.contains('modal-backdrop')) closeModal();
  });
  $('#mobile-scrim').addEventListener('click', closeMobile);
  $('#search').addEventListener('input', (event) => { state.search = event.target.value; draw(); });
  $('#sort').addEventListener('change', (event) => { state.sort = event.target.value; draw(); });
  $('#url-entry').addEventListener('input', resetInspection);
  $$('input[name="download-kind"]').forEach(input => input.addEventListener('change', resetInspection));
  $('#playlist-check').addEventListener('change', resetInspection);
  $('#quality-list').addEventListener('change', (e) => { if (e.target.name === 'quality') { state.quality = e.target.value; state.qualityByMode[state.mediaMode] = e.target.value; } });
  $$('input[name="media-output"]').forEach(input => input.addEventListener('change', (e) => changeMediaMode(e.target.value)));
  $('#toggle-formats').addEventListener('click', () => { state.formatsExpanded = !state.formatsExpanded; renderQualities(); });
  $('#analyze-btn').addEventListener('click', inspect);
  $('#submit-download').addEventListener('click', submitDownload);
  $('#save-settings').addEventListener('click', saveSettings);
  $('#choose-job-folder').addEventListener('click', () => openFolderPicker('job'));
  $('#choose-default-folder').addEventListener('click', () => openFolderPicker('settings'));
  $('#copy-path').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(state.pendingDefaultDir || state.config?.download_dir || ''); toast('Download path copied.'); }
    catch { toast('Could not access clipboard.', 'error'); }
  });
  document.addEventListener('keydown', (event) => {
    const editable = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);
    if (event.key === 'Escape') { if (state.modal === 'folder-modal') returnFromFolderPicker(); else if (state.modal) closeModal(); else if (state.menu) { state.menu = null; draw(); } else closeMobile(); }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); openAdd(); }
    if (event.key === '/' && !editable && !state.modal) { event.preventDefault(); $('#search').focus(); }
  });
  document.addEventListener('dragover', (e) => { if (e.dataTransfer?.types?.includes('text/uri-list')) e.preventDefault(); });
  document.addEventListener('drop', (e) => {
    const uri = e.dataTransfer?.getData('text/uri-list') || e.dataTransfer?.getData('text/plain');
    if (uri && /^https?:\/\//im.test(uri.trim())) { e.preventDefault(); openAdd(uri.trim()); }
  });

  loadConfig();
  loadJobs();
  setInterval(() => { if (!document.hidden) loadJobs(); }, 1200);
  setInterval(() => { if (!document.hidden) loadConfig(); }, 30000);
})();
