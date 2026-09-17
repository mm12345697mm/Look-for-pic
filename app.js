/**
 * Look-for-pic Web — photo identify via /api/identify → gallery (auto, no confirm)
 * Features: multi-screenshot, sticky user shots, localStorage history, related_by_title
 */
(function () {
  'use strict';

  const DMM_PICS = 'https://pics.dmm.co.jp/digital/video';
  const PREFIX_ONE_LABELS = new Set([
    'nhdtc', 'nhdtb', 'nhdta', 'nhdts', 'nhdt',
    'sdmf', 'stars', 'sdde', 'sdmm', 'sdam', 'start', 'fsdss',
    // HAWA / DANDY(A) digital CIDs need leading "1" (without → now_printing)
    'hawa', 'dandy', 'dandya',
  ]);
  const DEMO_CODE = 'MIDA-616';
  const AV_CODE_INPUT_RE = /^[A-Za-z]{2,10}-?\d{1,5}$/;
  const HISTORY_KEY = 'lfp_identify_history_v1';
  const HISTORY_MAX = 50;
  const THUMB_MAX_BYTES = 150 * 1024;

  let runId = 0;

  // --- DOM ---
  const $ = (id) => document.getElementById(id);
  const screenHome = $('screen-home');
  const screenGallery = $('screen-gallery');
  const screenHistory = $('screen-history');
  const screenHistoryDetail = $('screen-history-detail');
  const statusEl = $('status');
  const progressPanel = $('progress-panel');
  const progressToggle = $('progress-toggle');
  const progressSummaryEl = $('progress-summary');
  const progressStepsEl = $('progress-steps');
  const progressDetailEl = $('progress-detail');
  const progressBar = $('progress-bar');
  const progressPct = $('progress-pct');
  const codeInput = $('code-input');
  const galleryCards = $('gallery-cards');
  const galleryCount = $('gallery-count');
  const galleryNotice = $('gallery-notice');
  const ocrOverlay = $('ocr-overlay');
  const ocrCodeInput = $('ocr-code-input');
  const filePick = $('file-pick');
  const fileCamera = $('file-camera');
  const pendingPanel = $('pending-panel');
  const pendingThumbs = $('pending-thumbs');
  const pendingLabel = $('pending-label');
  const userShotsPanel = $('user-shots-panel');
  const userShotsScroll = $('user-shots-scroll');
  const historyList = $('history-list');
  const historyCount = $('history-count');
  const historyEmpty = $('history-empty');
  const historyDetailEl = $('history-detail');

  /** @type {{ id: string, file: File, url: string }[]} */
  let pendingFiles = [];
  /** Object URLs / dataURLs for sticky comparison (cleared on 重新開始) */
  let lastUserShotUrls = [];
  let viewingHistoryId = null;

  // --- Normalize ---
  function normalizeCode(raw) {
    return String(raw || '')
      .trim()
      .toUpperCase()
      .replace(/[‐‑‒–—―ー−]/g, '-')
      .replace(/\s+/g, '')
      .replace(/[_／/]/g, '-');
  }

  function parseCodeParts(code) {
    const n = normalizeCode(code);
    const m = n.match(/^([A-Z]{2,10})-?(\d{1,5})$/);
    if (!m) return null;
    return { label: m[1], number: m[2] };
  }

  function codeToCid(code) {
    const parts = parseCodeParts(code);
    if (!parts) return null;
    const label = parts.label.toLowerCase();
    const num = parts.number.padStart(5, '0');
    if (PREFIX_ONE_LABELS.has(label)) return `1${label}${num}`;
    return `${label}${num}`;
  }

  function formatDisplayCode(code) {
    const parts = parseCodeParts(code);
    if (!parts) return normalizeCode(code);
    return `${parts.label}-${parts.number.replace(/^0+/, '') || '0'}`;
  }

  function coverUrl(cid) {
    return `${DMM_PICS}/${cid}/${cid}pl.jpg`;
  }

  function stillUrls(cid, count) {
    const n = count || 10;
    const out = [];
    for (let i = 1; i <= n; i++) out.push(`${DMM_PICS}/${cid}/${cid}jp-${i}.jpg`);
    return out;
  }


  /** 日本語タイトル（中文片名）— omit empty parentheses when no Chinese title */
  function formatDisplayTitle(titleJa, titleZh) {
    const ja = (titleJa || '').trim();
    const zh = (titleZh || '').trim();
    if (!ja && !zh) return '（無標題）';
    if (!zh) return ja || '（無標題）';
    if (!ja) return zh;
    // Avoid duplicating when OCR already Chinese-only or identical
    if (ja === zh) return ja;
    if (ja.includes('（' + zh + '）') || ja.includes('(' + zh + ')')) return ja;
    return ja + '（' + zh + '）';
  }

  function workFromApi(raw, line) {
    const rawCode = raw.code == null ? '' : String(raw.code);
    const titleOnly =
      !rawCode ||
      rawCode === 'TITLE-SEARCH' ||
      rawCode.toLowerCase() === 'null' ||
      !parseCodeParts(rawCode);
    const code = titleOnly
      ? rawCode === 'TITLE-SEARCH' || !rawCode
        ? '片名搜尋'
        : formatDisplayCode(rawCode)
      : formatDisplayCode(rawCode);
    const cid = titleOnly
      ? String(raw.cid || '')
      : String(raw.cid || codeToCid(code) || '');
    const cover = String(raw.cover || raw.cover_url || (cid ? coverUrl(cid) : ''));
    const stills = Array.isArray(raw.stills) && raw.stills.length
      ? raw.stills.slice()
      : cid
        ? stillUrls(cid, 10)
        : [];
    const relatedByTitle = Array.isArray(raw.related_by_title)
      ? raw.related_by_title.map((r) => {
          const why = String((r && r.why) || '');
          let rl = (r && r.line) || '';
          if (!rl) {
            if (/演員|女優|actress/i.test(why)) rl = 'actress';
            else if (/關鍵字|keyword/i.test(why)) rl = 'keyword';
            else rl = 'theme';
          }
          return workFromApi(r, rl);
        })
      : [];
    return {
      code,
      title: raw.title ? String(raw.title) : '',
      titleZh: raw.title_zh ? String(raw.title_zh) : (raw.titleZh ? String(raw.titleZh) : ''),
      actress: raw.actress ? String(raw.actress) : '',
      studio: raw.studio ? String(raw.studio) : '',
      cid,
      line: line || raw.line || 'main',
      why: raw.why ? String(raw.why) : '',
      cover,
      stills,
      titleOnly,
      relatedByTitle,
    };
  }

  const DEFAULT_STEPS = [
    { id: 'receive', label: '接收圖片／文字' },
    { id: 'vision', label: '看圖辨識（Gemini）' },
    { id: 'parse', label: '讀取番號／片名' },
    { id: 'verify', label: '核對片名與番號' },
    { id: 'search', label: '搜尋作品資料' },
    { id: 'cover', label: '抓取封面與劇照' },
    { id: 'done', label: '完成，進入畫廊' },
  ];

  let progressState = {};
  let progressLabels = {};
  let progressCollapsed = false;
  let progressCurrentLine = '準備中…';
  let progressFinished = false;

  function setProgressCollapsed(collapsed) {
    progressCollapsed = !!collapsed;
    if (!progressPanel) return;
    progressPanel.classList.toggle('is-collapsed', progressCollapsed);
    if (progressToggle) {
      progressToggle.setAttribute('aria-expanded', progressCollapsed ? 'false' : 'true');
    }
  }

  function updateProgressSummary(line) {
    progressCurrentLine = line || progressCurrentLine;
    if (progressSummaryEl) {
      progressSummaryEl.textContent = progressCurrentLine;
    }
  }

  function stepLabel(stepId) {
    return progressLabels[stepId] || (DEFAULT_STEPS.find((s) => s.id === stepId) || {}).label || stepId;
  }

  function hideProgress() {
    if (!progressPanel) return;
    progressPanel.classList.add('hidden');
    progressPanel.classList.remove('is-complete', 'is-failed');
    progressPanel.setAttribute('aria-busy', 'false');
    setProgressCollapsed(false);
    progressFinished = false;
  }

  function showProgress(steps) {
    if (!progressPanel) return;
    const list = steps && steps.length ? steps : DEFAULT_STEPS;
    progressState = {};
    progressLabels = {};
    progressFinished = false;
    progressPanel.classList.remove('is-complete', 'is-failed');
    progressStepsEl.innerHTML = '';
    list.forEach((s) => {
      progressState[s.id] = 'pending';
      progressLabels[s.id] = s.label;
      const li = document.createElement('li');
      li.className = 'progress-step is-pending';
      li.dataset.step = s.id;
      li.innerHTML = '<span class="step-mark" aria-hidden="true"></span><span class="step-label">' + escapeHtml(s.label) + '</span>';
      progressStepsEl.appendChild(li);
    });
    progressDetailEl.textContent = '';
    progressBar.style.width = '0%';
    progressPct.textContent = '0%';
    updateProgressSummary('處理中：準備中…');
    setProgressCollapsed(false);
    progressPanel.classList.remove('hidden');
    progressPanel.setAttribute('aria-busy', 'true');
  }

  function applyProgressEvent(evt) {
    if (!evt || !evt.step) return;
    const step = evt.step;
    const status = evt.status || 'active';
    progressState[step] = status;
    const li = progressStepsEl.querySelector('[data-step="' + step + '"]');
    if (li) {
      li.className = 'progress-step is-' + status;
    }
    if (status === 'active' || status === 'done') {
      const ids = Array.from(progressStepsEl.querySelectorAll('.progress-step')).map((n) => n.dataset.step);
      const idx = ids.indexOf(step);
      for (let i = 0; i < idx; i++) {
        if (progressState[ids[i]] === 'pending') {
          progressState[ids[i]] = 'done';
          const prev = progressStepsEl.querySelector('[data-step="' + ids[i] + '"]');
          if (prev) prev.className = 'progress-step is-done';
        }
      }
    }
    if (evt.detail) {
      progressDetailEl.textContent = String(evt.detail);
    }
    if (typeof evt.progress === 'number' && !Number.isNaN(evt.progress)) {
      const pct = Math.max(0, Math.min(100, Math.round(evt.progress * 100)));
      progressBar.style.width = pct + '%';
      progressPct.textContent = pct + '%';
    }

    const label = stepLabel(step);
    if (status === 'active') {
      updateProgressSummary('處理中：' + (evt.detail || label + '…'));
    } else if (status === 'error') {
      updateProgressSummary('失敗：' + (evt.detail || label));
      progressPanel.classList.add('is-failed');
      progressPanel.classList.remove('is-complete');
    } else if (step === 'done' && status === 'done') {
      updateProgressSummary('完成');
      progressPanel.classList.add('is-complete');
      progressPanel.classList.remove('is-failed');
      progressFinished = true;
      setProgressCollapsed(false);
      progressPanel.setAttribute('aria-busy', 'false');
    } else if (status === 'done' || status === 'skipped') {
      if (!progressFinished) {
        updateProgressSummary('處理中：' + label);
      }
    }
  }

  function toggleProgressCollapsed() {
    if (!progressPanel || progressPanel.classList.contains('hidden')) return;
    setProgressCollapsed(!progressCollapsed);
  }

  async function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  async function simulateProgress({ images, code, title }, signal) {
    const n = images && images.length ? images.length : 0;
    const plan = [
      { step: 'receive', status: 'active', detail: n > 1 ? '正在接收 ' + n + ' 張…' : '正在接收輸入…', progress: 0.05 },
      { step: 'receive', status: 'done', detail: n > 1 ? '已接收 ' + n + ' 張' : n ? '已接收圖片' : title ? '已接收片名' : '已接收番號', progress: 0.16 },
      {
        step: 'vision',
        status: n ? 'active' : 'skipped',
        detail: n > 1 ? '辨識第 1/' + n + ' 張…' : n ? 'Gemini 看圖辨識中…' : '無圖片，略過看圖',
        progress: n ? 0.2 : 0.33,
      },
    ];
    if (n) {
      plan.push({ step: 'vision', status: 'done', detail: n > 1 ? '已看完 ' + n + ' 張' : '看圖階段完成（或改 OCR）', progress: 0.33 });
    }
    plan.push(
      { step: 'parse', status: 'active', detail: '整理番號／片名…', progress: 0.4 },
      { step: 'parse', status: 'done', detail: code ? '番號：' + code : title ? '片名搜尋' : '解析中', progress: 0.5 },
      { step: 'search', status: 'active', detail: title ? '正在用片名搜尋…' : '搜尋作品資料…', progress: 0.55 },
    );
    for (const ev of plan) {
      if (signal && signal.aborted) return;
      applyProgressEvent(ev);
      await sleep(ev.status === 'active' ? 280 : 120);
    }
  }

  function finishSimulatedProgress(data) {
    applyProgressEvent({
      step: 'search',
      status: data && data.ok === false ? 'error' : 'done',
      detail: (data && data.message) || '搜尋完成',
      progress: 0.72,
    });
    applyProgressEvent({
      step: 'cover',
      status: data && data.cover ? 'done' : 'skipped',
      detail: data && data.cover ? 'CDN 封面就緒' : '無封面',
      progress: 0.88,
    });
    applyProgressEvent({
      step: 'done',
      status: data && (data.ok || data.title) ? 'done' : 'error',
      detail: data && (data.ok || data.title) ? '完成，進入畫廊' : (data && data.message) || '失敗',
      progress: 1,
    });
  }

  function appendImagesToFormData(fd, images) {
    if (!images || !images.length) return;
    // Do not append both `images` and `image` — server merges both and double-counts.
    if (images.length === 1) {
      fd.append('image', images[0], images[0].name || 'photo.jpg');
    } else {
      images.forEach((f, i) => {
        fd.append('images', f, f.name || 'photo' + (i + 1) + '.jpg');
      });
    }
  }

  async function apiIdentifyStream({ images, image, code, title } = {}, onProgress) {
    const fd = new FormData();
    const imgs = images && images.length ? images : image ? [image] : [];
    appendImagesToFormData(fd, imgs);
    if (code) fd.append('code', code);
    if (title) fd.append('title', title);

    let res;
    try {
      res = await fetch('/api/identify/stream', { method: 'POST', body: fd });
    } catch (e) {
      throw new Error('stream_unavailable');
    }
    const ct = (res.headers.get('content-type') || '').toLowerCase();
    if (!res.ok || !ct.includes('text/event-stream') || !res.body) {
      throw new Error('stream_unavailable');
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let finalData = null;
    let httpStatus = res.status;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split('\n\n');
      buffer = parts.pop() || '';
      for (const chunk of parts) {
        const lines = chunk.split('\n');
        let dataLine = '';
        for (const line of lines) {
          if (line.startsWith('data:')) dataLine += line.slice(5).trim();
        }
        if (!dataLine) continue;
        let evt;
        try {
          evt = JSON.parse(dataLine);
        } catch (_) {
          continue;
        }
        if (evt.type === 'steps' && Array.isArray(evt.steps)) {
          showProgress(evt.steps);
        } else if (evt.type === 'progress') {
          if (onProgress) onProgress(evt);
          else applyProgressEvent(evt);
        } else if (evt.type === 'result') {
          finalData = evt.data;
          if (typeof evt.status === 'number') httpStatus = evt.status;
        }
      }
    }
    if (!finalData) throw new Error('stream_incomplete');
    return { status: httpStatus, data: finalData };
  }

  async function apiIdentify({ images, image, code, title } = {}) {
    const fd = new FormData();
    const imgs = images && images.length ? images : image ? [image] : [];
    appendImagesToFormData(fd, imgs);
    if (code) fd.append('code', code);
    if (title) fd.append('title', title);
    const res = await fetch('/api/identify', { method: 'POST', body: fd });
    let data;
    try {
      data = await res.json();
    } catch (_) {
      throw new Error('伺服器回應無效');
    }
    return { status: res.status, data };
  }

  function galleryFromIdentify(data) {
    const seenCodes = new Set();

    function mapRelated(r) {
      const why = String(r.why || '');
      let line = r.line || 'related';
      if (!r.line) {
        if (/候選|candidate/i.test(why)) line = 'candidate';
        else if (/主題|theme|片名相近/i.test(why)) line = 'theme';
        else if (/女優|actress|演員/i.test(why)) line = 'actress';
        else if (/關鍵字|keyword/i.test(why)) line = 'keyword';
        else if (/多圖/i.test(why)) line = 'multi';
      }
      return workFromApi(r, line);
    }

    // Prefer explicit results[] from multi-identify
    if (Array.isArray(data.results) && data.results.length) {
      const items = data.results.map((r, i) => workFromApi(r, i === 0 ? 'main' : 'multi'));
      items.forEach((w, i) => {
        if (w.code && !w.titleOnly) seenCodes.add(String(w.code).toUpperCase());
        // Ensure related_by_title from this result row (not only first / top-level)
        if ((!w.relatedByTitle || !w.relatedByTitle.length) && data.results[i] && Array.isArray(data.results[i].related_by_title)) {
          w.relatedByTitle = data.results[i].related_by_title.map((r) => {
            const why = String((r && r.why) || '');
            let rl = (r && r.line) || '';
            if (!rl) {
              if (/演員|女優|actress/i.test(why)) rl = 'actress';
              else if (/關鍵字|keyword/i.test(why)) rl = 'keyword';
              else rl = 'theme';
            }
            return workFromApi(r, rl);
          });
        }
      });
      // Also merge non-multi related from first payload (theme/actress/candidates)
      const relatedRaw = Array.isArray(data.related) ? data.related.slice() : [];
      const extras = relatedRaw
        .filter((r) => String(r.line || '') !== 'multi' && !/多圖/.test(String(r.why || '')))
        .map(mapRelated)
        .filter((w) => {
          if (w.titleOnly || !w.code) return true;
          const key = String(w.code).toUpperCase();
          if (seenCodes.has(key)) return false;
          seenCodes.add(key);
          return true;
        });
      let notice = data.related_note || data.message || null;
      return { items: items.concat(extras), notice: notice || null, source: 'api' };
    }

    const main = workFromApi(data, 'main');
    if (main.code && !main.titleOnly) seenCodes.add(String(main.code).toUpperCase());

    const apiCands = Array.isArray(data.candidates) ? data.candidates : [];
    const relatedRaw = Array.isArray(data.related) ? data.related.slice() : [];
    if (apiCands.length >= 2) {
      const relatedCodes = new Set(
        relatedRaw
          .map((r) => (r && r.code ? String(r.code).toUpperCase() : ''))
          .filter(Boolean)
      );
      for (const c of apiCands) {
        const key = c && c.code ? String(c.code).toUpperCase() : '';
        if (!key || relatedCodes.has(key) || seenCodes.has(key)) continue;
        relatedRaw.push({ ...c, why: c.why || '片名候選', line: 'candidate' });
        relatedCodes.add(key);
      }
    }
    const extras = relatedRaw.map(mapRelated).filter((w) => {
      if (w.titleOnly || !w.code) return true;
      const key = String(w.code).toUpperCase();
      if (seenCodes.has(key)) return false;
      seenCodes.add(key);
      return true;
    });

    let items;
    if (main.titleOnly && extras.length) {
      items = extras;
    } else {
      items = [main, ...extras];
    }

    // Ensure main carries related_by_title from top-level if missing
    if (items[0] && (!items[0].relatedByTitle || !items[0].relatedByTitle.length)) {
      if (Array.isArray(data.related_by_title) && data.related_by_title.length) {
        items[0].relatedByTitle = data.related_by_title.map((r) => workFromApi(r, 'theme'));
      }
    }

    let notice = data.related_note || data.message || null;
    const titleBanner =
      data.search_mode === 'title' ||
      data.code === 'TITLE-SEARCH' ||
      (!data.code && data.title);
    if (titleBanner && data.title && !(notice && /找到 \d+ 個不同番號/.test(String(notice)))) {
      const banner = '以片名搜尋：' + String(data.title);
      if (!notice || notice.indexOf(banner) !== 0) {
        notice = notice ? banner + ' — ' + notice : banner;
      }
    }
    if (main.titleOnly && !extras.length) {
      notice = (notice ? notice + ' ' : '') + '尚無封面；請手動輸入正確番號。';
    }
    return {
      items,
      notice: notice || null,
      source: 'api',
    };
  }

  // --- UI helpers ---
  function setStatus(msg, kind) {
    if (!msg) {
      statusEl.className = 'status-bar';
      statusEl.textContent = '';
      return;
    }
    statusEl.className = 'status-bar visible' + (kind ? ' ' + kind : '');
    statusEl.innerHTML = kind === 'busy' ? '<span class="spinner"></span>' + escapeHtml(msg) : escapeHtml(msg);
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function showScreen(name) {
    screenHome.classList.toggle('active', name === 'home');
    screenGallery.classList.toggle('active', name === 'gallery');
    if (screenHistory) screenHistory.classList.toggle('active', name === 'history');
    if (screenHistoryDetail) screenHistoryDetail.classList.toggle('active', name === 'history-detail');
    if (name === 'home' || name === 'history') window.scrollTo(0, 0);
    if (name !== 'gallery') {
      // Keep sticky shots only on gallery
      if (name !== 'gallery' && userShotsPanel && name !== 'home') {
        /* leave as-is on home during identify */
      }
    }
  }

  function hideUserShots() {
    if (!userShotsPanel) return;
    userShotsPanel.classList.add('hidden');
    if (userShotsScroll) userShotsScroll.innerHTML = '';
    lastUserShotUrls = [];
  }

  function showUserShots(urls) {
    if (!userShotsPanel || !userShotsScroll) return;
    lastUserShotUrls = (urls || []).filter(Boolean);
    userShotsScroll.innerHTML = '';
    if (!lastUserShotUrls.length) {
      userShotsPanel.classList.add('hidden');
      return;
    }
    lastUserShotUrls.forEach((url, i) => {
      const img = document.createElement('img');
      img.src = url;
      img.alt = '你的截圖 ' + (i + 1);
      img.loading = 'lazy';
      img.decoding = 'async';
      bindLightboxable(img, lastUserShotUrls, i);
      userShotsScroll.appendChild(img);
    });
    userShotsPanel.classList.remove('hidden');
  }

  function resetBaseline() {
    runId += 1;
    setStatus('');
    hideProgress();
    hideOcrPrompt();
    hideUserShots();
    clearPending(true);
    galleryCards.innerHTML = '';
    galleryNotice.hidden = true;
    galleryNotice.textContent = '';
    codeInput.value = '';
    showScreen('home');
  }

  function lineLabel(line) {
    if (line === 'main') return '主作品';
    if (line === 'theme') return '主題相近';
    if (line === 'actress') return '同女優';
    if (line === 'keyword') return '關鍵字';
    if (line === 'candidate') return '片名候選';
    if (line === 'multi') return '多圖辨識';
    return '作品';
  }

  function onImgError(img) {
    const wrap = img.parentElement;
    img.classList.add('img-broken');
    img.removeAttribute('src');
    img.alt = '無封面';
    if (wrap && wrap.classList.contains('cover-wrap') && !wrap.querySelector('.cover-placeholder')) {
      wrap.classList.add('is-empty');
      const ph = document.createElement('div');
      ph.className = 'cover-placeholder';
      ph.innerHTML = '<strong>暫無封面</strong><span>請確認番號或改以番號搜尋</span>';
      wrap.appendChild(ph);
    }
  }

  function appendCover(coverWrap, w) {
    const url = (w.cover || '').trim();
    if (!url || w.titleOnly) {
      coverWrap.classList.add('is-empty');
      const ph = document.createElement('div');
      ph.className = 'cover-placeholder';
      ph.innerHTML = w.titleOnly
        ? '<strong>尚未解析番號</strong><span>無法載入 CDN 封面 — 請手動輸入番號</span>'
        : '<strong>暫無封面</strong><span>請確認番號</span>';
      coverWrap.appendChild(ph);
      return;
    }
    const coverImg = document.createElement('img');
    coverImg.src = url;
    coverImg.alt = w.code + ' 封面';
    coverImg.loading = 'lazy';
    coverImg.decoding = 'async';
    coverImg.referrerPolicy = 'no-referrer';
    coverImg.addEventListener('error', () => onImgError(coverImg));
    coverWrap.appendChild(coverImg);
    const set = workImageSet(w);
    bindLightboxable(coverImg, set, 0);
  }

  function appendStillsScroll(parent, w, labelText) {
    const stillsLabel = document.createElement('div');
    stillsLabel.className = 'stills-label';
    stillsLabel.textContent = labelText || '劇照（橫滑）';
    const stills = document.createElement('div');
    stills.className = 'stills-scroll';
    const set = workImageSet(w);
    const coverOffset = w && w.cover ? 1 : 0;
    (w.stills || []).forEach((url, i) => {
      const img = document.createElement('img');
      img.src = url;
      img.alt = (w.code || '') + ' 劇照 ' + (i + 1);
      img.loading = 'lazy';
      img.decoding = 'async';
      img.referrerPolicy = 'no-referrer';
      img.addEventListener('error', () => {
        img.style.display = 'none';
      });
      bindLightboxable(img, set, coverOffset + i);
      stills.appendChild(img);
    });
    parent.appendChild(stillsLabel);
    parent.appendChild(stills);
  }

  function relatedHeadingForList(relatedList) {
    const hasActress = (relatedList || []).some((rw) => {
      const why = String((rw && rw.why) || '');
      const line = String((rw && rw.line) || '');
      return line === 'actress' || why.includes('演員') || why.includes('女優');
    });
    const hasKeyword = (relatedList || []).some((rw) => {
      const why = String((rw && rw.why) || '');
      const line = String((rw && rw.line) || '');
      return line === 'keyword' || why.includes('關鍵字');
    });
    const hasTitle = (relatedList || []).some((rw) => {
      const why = String((rw && rw.why) || '');
      const line = String((rw && rw.line) || '');
      return line === 'theme' || line === 'title' || why.includes('片名') || why.includes('主題');
    });
    const parts = [];
    if (hasTitle) parts.push('片名');
    if (hasKeyword) parts.push('關鍵字');
    if (hasActress) parts.push('同演員');
    if (!parts.length) return '相關作品';
    return parts.join('／');
  }

  /** Single work card (cover + stills). Related works are NOT nested here. */
  function buildWorkCard(w, opts) {
    opts = opts || {};
    const card = document.createElement('article');
    card.className = 'card' + (opts.slide ? ' work-slide-card' : '');

    const meta = document.createElement('div');
    meta.className = 'card-meta';
    const lineClass = w.line === 'multi' ? ' card-line line-multi' : ' card-line';
    const badge = opts.badgeLabel || lineLabel(w.line);
    meta.innerHTML =
      '<p class="card-code">' +
      escapeHtml(w.code) +
      '</p>' +
      '<p class="card-title">' +
      escapeHtml(formatDisplayTitle(w.title, w.titleZh)) +
      '</p>' +
      (w.actress
        ? '<p class="card-actress">女優：' + escapeHtml(w.actress) + (w.studio ? ' · ' + escapeHtml(w.studio) : '') + '</p>'
        : w.studio
          ? '<p class="card-actress">' + escapeHtml(w.studio) + '</p>'
          : '') +
      '<span class="' +
      lineClass.trim() +
      '">' +
      escapeHtml(badge) +
      '</span>';

    const coverWrap = document.createElement('div');
    coverWrap.className = 'cover-wrap';
    appendCover(coverWrap, w);

    card.appendChild(meta);
    card.appendChild(coverWrap);
    appendStillsScroll(card, w, '劇照（橫滑）');
    return card;
  }

  /**
   * One screenshot/main hit as a horizontal strip:
   * 主作品 ↔️ 相關1 ↔️ 相關2 … (stills inside each card still scroll sideways).
   * Multiple mains stack vertically in the gallery.
   */
  function buildWorkCarousel(mainWork) {
    const related = Array.isArray(mainWork.relatedByTitle)
      ? mainWork.relatedByTitle.slice(0, 13)
      : [];
    const block = document.createElement('section');
    block.className = 'work-carousel-block';

    const head = document.createElement('div');
    head.className = 'work-carousel-head';
    const hint = document.createElement('div');
    hint.className = 'work-carousel-hint';
    const total = 1 + related.length;
    if (related.length) {
      hint.textContent =
        '左右滑 · 主作品 ↔️ 相關（' +
        relatedHeadingForList(related) +
        '）· ' +
        total +
        ' 張';
    } else {
      hint.textContent = '主作品（尚無相關可左右滑）';
    }
    const pager = document.createElement('div');
    pager.className = 'work-carousel-pager';
    pager.textContent = '1 / ' + total;
    head.appendChild(hint);
    head.appendChild(pager);
    const track = document.createElement('div');
    track.className = 'work-carousel-track';
    track.setAttribute('aria-label', '主作品與相關作品橫向切換');

    const slides = [];
    const mainSlide = document.createElement('div');
    mainSlide.className = 'work-carousel-slide';
    mainSlide.appendChild(
      buildWorkCard(mainWork, { slide: true, badgeLabel: lineLabel(mainWork.line || 'main') })
    );
    track.appendChild(mainSlide);
    slides.push(mainSlide);

    related.forEach((rw, i) => {
      const slide = document.createElement('div');
      slide.className = 'work-carousel-slide';
      const why = String((rw && rw.why) || '');
      const line = String((rw && rw.line) || '');
      let badge = '相關 ' + (i + 1);
      if (line === 'actress' || why.includes('演員') || why.includes('女優')) badge = '同演員';
      else if (line === 'keyword' || why.includes('關鍵字')) badge = '關鍵字';
      else if (line === 'theme' || why.includes('片名')) badge = '片名相近';
      slide.appendChild(buildWorkCard(rw, { slide: true, badgeLabel: badge }));
      track.appendChild(slide);
      slides.push(slide);
    });

    const updatePager = () => {
      if (!slides.length) return;
      const left = track.scrollLeft;
      const w = track.clientWidth || 1;
      let idx = Math.round(left / w);
      if (idx < 0) idx = 0;
      if (idx > slides.length - 1) idx = slides.length - 1;
      pager.textContent = idx + 1 + ' / ' + slides.length;
    };
    track.addEventListener('scroll', () => {
      window.requestAnimationFrame(updatePager);
    }, { passive: true });

    // Track first, then hint/pager as snug footer under stills (no stretch gap)
    block.appendChild(track);
    block.appendChild(head);
    return block;
  }

  // Back-compat alias (history detail may still call this name)
  function appendRelatedByTitle(card, relatedList) {
    // Prefer carousel: if card is already inside a carousel, skip.
    // Legacy: replace nested list with a note — callers should use buildWorkCarousel.
    if (!relatedList || !relatedList.length) return;
    const parent = card.parentElement;
    if (parent && parent.classList.contains('work-carousel-slide')) return;
    // Wrap this single card's parent insertion site is handled by callers now.
  }

  function renderGallery(result) {
    const { items, notice } = result;
    galleryCards.innerHTML = '';
    galleryCount.textContent = String(items.length) + ' 部';

    if (notice) {
      galleryNotice.hidden = false;
      galleryNotice.textContent = notice;
    } else {
      galleryNotice.hidden = true;
      galleryNotice.textContent = '';
    }

    // Vertical: each screenshot/main hit. Horizontal: main ↔️ related works.
    for (const w of items) {
      galleryCards.appendChild(buildWorkCarousel(w));
    }

    showScreen('gallery');
    setStatus('');
    window.scrollTo(0, 0);
  }

  // --- Pending multi files ---
  function revokePendingUrls() {
    pendingFiles.forEach((p) => {
      try {
        URL.revokeObjectURL(p.url);
      } catch (_) {}
    });
  }

  function clearPending(revoke) {
    if (revoke) revokePendingUrls();
    pendingFiles = [];
    renderPending();
  }

  function renderPending() {
    if (!pendingPanel) return;
    pendingThumbs.innerHTML = '';
    if (!pendingFiles.length) {
      pendingPanel.classList.add('hidden');
      pendingLabel.textContent = '已選 0 張';
      return;
    }
    pendingPanel.classList.remove('hidden');
    pendingLabel.textContent = '已選 ' + pendingFiles.length + ' 張';
    pendingFiles.forEach((p) => {
      const wrap = document.createElement('div');
      wrap.className = 'pending-thumb';
      const img = document.createElement('img');
      img.src = p.url;
      img.alt = p.file.name || '截圖';
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'thumb-remove';
      btn.setAttribute('aria-label', '移除');
      btn.textContent = '×';
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        pendingFiles = pendingFiles.filter((x) => x.id !== p.id);
        try {
          URL.revokeObjectURL(p.url);
        } catch (_) {}
        renderPending();
      });
      wrap.appendChild(img);
      wrap.appendChild(btn);
      pendingThumbs.appendChild(wrap);
    });
  }

  function isLikelyImageFile(f) {
    if (!f) return false;
    const t = (f.type || '').toLowerCase();
    if (t.indexOf('image/') === 0) return true;
    if (t && t.indexOf('image/') !== 0) return false;
    // iOS album picks often have empty MIME (esp. HEIC / camera)
    const name = (f.name || '').toLowerCase();
    if (/\.(jpe?g|png|gif|webp|heic|heif|bmp)$/.test(name)) return true;
    // Camera capture may be image.jpg or empty name with empty type
    if (!t) return true;
    return false;
  }

  function addFilesToPending(fileList) {
    const arr = Array.from(fileList || []).filter(isLikelyImageFile);
    if (!arr.length) {
      setStatus('沒有可用的圖片（請選 JPG／PNG／HEIC）', 'err');
      return;
    }
    arr.forEach((f) => {
      pendingFiles.push({
        id: 'f' + Date.now() + '_' + Math.random().toString(36).slice(2, 8),
        file: f,
        url: URL.createObjectURL(f),
      });
    });
    renderPending();
  }

  // --- History (localStorage) ---
  function loadHistory() {
    try {
      const raw = localStorage.getItem(HISTORY_KEY);
      const list = raw ? JSON.parse(raw) : [];
      return Array.isArray(list) ? list : [];
    } catch (_) {
      return [];
    }
  }

  function saveHistory(list) {
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, HISTORY_MAX)));
    } catch (e) {
      // Quota: drop older thumbs
      try {
        const slim = list.slice(0, Math.min(20, list.length)).map((r) => ({
          ...r,
          userShots: (r.userShots || []).slice(0, 1),
          stills: (r.stills || []).slice(0, 6),
        }));
        localStorage.setItem(HISTORY_KEY, JSON.stringify(slim));
      } catch (_) {}
    }
  }

  function downscaleFileToDataUrl(file, maxBytes) {
    return new Promise((resolve) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        try {
          const canvas = document.createElement('canvas');
          let w = img.naturalWidth || img.width;
          let h = img.naturalHeight || img.height;
          const maxSide = 720;
          const scale = Math.min(1, maxSide / Math.max(w, h));
          w = Math.max(1, Math.round(w * scale));
          h = Math.max(1, Math.round(h * scale));
          canvas.width = w;
          canvas.height = h;
          const ctx = canvas.getContext('2d');
          ctx.drawImage(img, 0, 0, w, h);
          let q = 0.72;
          let dataUrl = canvas.toDataURL('image/jpeg', q);
          while (dataUrl.length > maxBytes * 1.37 && q > 0.35) {
            q -= 0.08;
            dataUrl = canvas.toDataURL('image/jpeg', q);
          }
          // If still huge, shrink more
          if (dataUrl.length > maxBytes * 1.37) {
            canvas.width = Math.round(w * 0.6);
            canvas.height = Math.round(h * 0.6);
            ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
            dataUrl = canvas.toDataURL('image/jpeg', 0.55);
          }
          URL.revokeObjectURL(url);
          resolve(dataUrl);
        } catch (_) {
          URL.revokeObjectURL(url);
          resolve(null);
        }
      };
      img.onerror = () => {
        URL.revokeObjectURL(url);
        resolve(null);
      };
      img.src = url;
    });
  }

  async function buildUserShotThumbs(files) {
    const out = [];
    for (const f of files || []) {
      const d = await downscaleFileToDataUrl(f, THUMB_MAX_BYTES);
      if (d) out.push(d);
    }
    return out;
  }

  async function appendHistoryFromIdentify(data, userFiles) {
    if (!data || !data.ok) return;
    const mainCode = data.code && parseCodeParts(String(data.code)) ? formatDisplayCode(String(data.code)) : String(data.code || '');
    if (!mainCode && !data.title) return;

    const userShots = await buildUserShotThumbs(userFiles || []);
    const related = Array.isArray(data.related_by_title)
      ? data.related_by_title.slice(0, 13).map((r) => ({
          code: r.code || '',
          title: r.title || '',
          title_zh: r.title_zh || '',
          cover: r.cover || '',
          stills: Array.isArray(r.stills) ? r.stills.slice(0, 10) : [],
        }))
      : [];

    // Multi: save one record per result
    const toSave = Array.isArray(data.results) && data.results.length
      ? data.results
      : [data];

    let list = loadHistory();
    for (const item of toSave) {
      const code = item.code && parseCodeParts(String(item.code))
        ? formatDisplayCode(String(item.code))
        : String(item.code || mainCode || '');
      const rec = {
        id: 'h_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7),
        ts: Date.now(),
        code: code,
        title: item.title || data.title || '',
        title_zh: item.title_zh || data.title_zh || '',
        cover: item.cover || data.cover || '',
        stills: Array.isArray(item.stills) ? item.stills.slice(0, 12) : (data.stills || []).slice(0, 12),
        userShots: userShots,
        related: item.related_by_title
          ? item.related_by_title.slice(0, 13).map((r) => ({
              code: r.code || '',
              title: r.title || '',
              title_zh: r.title_zh || '',
              cover: r.cover || '',
              stills: Array.isArray(r.stills) ? r.stills.slice(0, 10) : [],
            }))
          : related,
        actress: item.actress || data.actress || '',
      };
      list.unshift(rec);
    }
    if (list.length > HISTORY_MAX) list = list.slice(0, HISTORY_MAX);
    saveHistory(list);
  }

  function formatTs(ts) {
    try {
      const d = new Date(ts);
      const pad = (n) => String(n).padStart(2, '0');
      return (
        d.getFullYear() +
        '/' +
        pad(d.getMonth() + 1) +
        '/' +
        pad(d.getDate()) +
        ' ' +
        pad(d.getHours()) +
        ':' +
        pad(d.getMinutes())
      );
    } catch (_) {
      return '';
    }
  }

  function renderHistoryList() {
    const list = loadHistory();
    historyList.innerHTML = '';
    historyCount.textContent = String(list.length);
    if (!list.length) {
      historyEmpty.hidden = false;
      return;
    }
    historyEmpty.hidden = true;
    list.forEach((rec) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'history-item';
      const thumbSrc = rec.cover || (rec.userShots && rec.userShots[0]) || '';
      let thumbHtml;
      if (thumbSrc) {
        thumbHtml = '<img class="history-thumb" src="' + escapeHtml(thumbSrc) + '" alt="" loading="lazy" referrerpolicy="no-referrer" />';
      } else {
        thumbHtml = '<div class="history-thumb placeholder">無圖</div>';
      }
      row.innerHTML =
        thumbHtml +
        '<div class="history-meta">' +
        '<p class="history-code">' +
        escapeHtml(rec.code || '—') +
        '</p>' +
        '<p class="history-title">' +
        escapeHtml(formatDisplayTitle(rec.title, rec.title_zh)) +
        '</p>' +
        '<p class="history-ts">' +
        escapeHtml(formatTs(rec.ts)) +
        '（台北）</p>' +
        '</div>';
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'history-item-del';
      del.setAttribute('aria-label', '刪除');
      del.textContent = '×';
      del.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        const next = loadHistory().filter((x) => x.id !== rec.id);
        saveHistory(next);
        renderHistoryList();
      });
      row.appendChild(del);
      row.addEventListener('click', () => openHistoryDetail(rec.id));
      historyList.appendChild(row);
    });
  }

  async function openHistoryDetail(id) {
    const rec = loadHistory().find((x) => x.id === id);
    if (!rec) return;
    viewingHistoryId = id;
    historyDetailEl.innerHTML = '';

    // User shots
    if (rec.userShots && rec.userShots.length) {
      const head = document.createElement('div');
      head.className = 'user-shots-head';
      head.textContent = '你的截圖';
      const scroll = document.createElement('div');
      scroll.className = 'user-shots-scroll';
      rec.userShots.forEach((u, i) => {
        const img = document.createElement('img');
        img.src = u;
        img.alt = '截圖 ' + (i + 1);
        bindLightboxable(img, rec.userShots, i);
        scroll.appendChild(img);
      });
      historyDetailEl.appendChild(head);
      historyDetailEl.appendChild(scroll);
    }

    const w = workFromApi(
      {
        code: rec.code,
        title: rec.title,
        title_zh: rec.title_zh || '',
        cover: rec.cover,
        stills: rec.stills,
        actress: rec.actress,
        related_by_title: rec.related || [],
      },
      'main'
    );
    // If no stored related, try fetch then show carousel
    if ((!rec.related || !rec.related.length) && rec.title) {
      try {
        const qs =
          '/api/related-by-title?title=' +
          encodeURIComponent(rec.title) +
          '&code=' +
          encodeURIComponent(rec.code || '') +
          (rec.actress ? '&actress=' + encodeURIComponent(rec.actress) : '');
        const res = await fetch(qs);
        const data = await res.json();
        if (data && data.ok && Array.isArray(data.related_by_title) && data.related_by_title.length) {
          w.relatedByTitle = data.related_by_title.map((r) => workFromApi(r, 'theme'));
          const list = loadHistory();
          const idx = list.findIndex((x) => x.id === id);
          if (idx >= 0) {
            list[idx].related = data.related_by_title.slice(0, 13).map((r) => ({
              code: r.code || '',
              title: r.title || '',
              cover: r.cover || '',
              stills: Array.isArray(r.stills) ? r.stills.slice(0, 10) : [],
              why: r.why || '',
              line: r.line || '',
            }));
            saveHistory(list);
          }
        }
      } catch (_) {}
    }
    historyDetailEl.appendChild(buildWorkCarousel(w));
    showScreen('history-detail');
    hideUserShots();
    hideProgress();
  }

  async function runIdentify({ images, image, code, title } = {}, myRun) {
    const imgs = images && images.length ? images : image ? [image] : [];
    const busyMsg = imgs.length > 1
      ? '多圖辨識中（' + imgs.length + ' 張）…'
      : imgs.length
        ? '看圖讀片名／搜尋中…'
        : title
          ? '以片名搜尋中…'
          : '載入畫廊…';
    setStatus(busyMsg, 'busy');
    showProgress(
      imgs.length > 1
        ? [
            { id: 'receive', label: '接收圖片（' + imgs.length + ' 張）' },
            { id: 'vision', label: '逐張看圖辨識' },
            { id: 'parse', label: '彙整番號／片名' },
            { id: 'verify', label: '核對片名與番號' },
            { id: 'search', label: '搜尋作品資料' },
            { id: 'cover', label: '抓取封面與劇照' },
            { id: 'done', label: '完成，進入畫廊' },
          ]
        : DEFAULT_STEPS
    );
    showScreen('home');

    // Sticky comparison previews (object URLs from pending or fresh)
    const previewUrls = imgs.map((f) => {
      const hit = pendingFiles.find((p) => p.file === f);
      return hit ? hit.url : URL.createObjectURL(f);
    });
    if (previewUrls.length) showUserShots(previewUrls);

    const handleResult = (data) => {
      if (myRun !== runId) return;
      const hasCode =
        data.code &&
        data.code !== 'TITLE-SEARCH' &&
        String(data.code).toLowerCase() !== 'null' &&
        parseCodeParts(String(data.code));
      const hasTitle = !!(data.title && String(data.title).trim());
      const hasResults = Array.isArray(data.results) && data.results.length > 0;

      if (!data.ok || (!hasCode && !hasTitle && !hasResults)) {
        const msg =
          data.message ||
          (data.vision_used === false && imgs.length
            ? '看圖辨識失敗，且未找到番號或片名'
            : '未找到番號或片名');
        setStatus(msg, 'err');
        applyProgressEvent({ step: 'done', status: 'error', detail: msg, progress: 1 });
        progressPanel.setAttribute('aria-busy', 'false');
        setProgressCollapsed(false);
        showOcrPrompt(myRun);
        return;
      }

      applyProgressEvent({
        step: 'done',
        status: 'done',
        detail: '完成',
        progress: 1,
      });
      const go = () => {
        if (myRun !== runId) return;
        setProgressCollapsed(false);
        if (progressPanel) {
          progressPanel.classList.remove('hidden');
          progressPanel.classList.add('is-complete');
          progressPanel.setAttribute('aria-busy', 'false');
        }
        const result = galleryFromIdentify(data);
        renderGallery(result);
        // Persist history (async thumbs)
        appendHistoryFromIdentify(data, imgs).catch(() => {});
        // Clear pending selection but keep sticky shots until 重新開始
        clearPending(false);
      };
      setTimeout(go, 280);
    };

    try {
      let data;
      try {
        const streamed = await apiIdentifyStream({ images: imgs, code, title }, applyProgressEvent);
        if (myRun !== runId) return;
        data = streamed.data;
      } catch (streamErr) {
        if (myRun !== runId) return;
        const abort = { aborted: false };
        const sim = simulateProgress({ images: imgs, code, title }, abort);
        try {
          const classic = await apiIdentify({ images: imgs, code, title });
          abort.aborted = true;
          await sim;
          if (myRun !== runId) return;
          data = classic.data;
          finishSimulatedProgress(data);
          await sleep(180);
        } catch (e) {
          abort.aborted = true;
          throw e;
        }
      }
      handleResult(data);
    } catch (e) {
      if (myRun !== runId) return;
      applyProgressEvent({ step: 'done', status: 'error', detail: (e && e.message) || String(e), progress: 1 });
      setStatus((e && e.message) || String(e), 'err');
      progressPanel.setAttribute('aria-busy', 'false');
      showOcrPrompt(myRun);
    }
  }

  function showOcrPrompt(myRun) {
    if (!ocrOverlay) return;
    ocrOverlay.classList.remove('hidden');
    ocrOverlay.hidden = false;
    ocrOverlay.setAttribute('aria-hidden', 'false');
    if (ocrCodeInput) {
      ocrCodeInput.value = '';
      ocrCodeInput.focus();
    }
    ocrOverlay.dataset.runId = String(myRun);
  }

  function hideOcrPrompt() {
    if (!ocrOverlay) return;
    ocrOverlay.classList.add('hidden');
    ocrOverlay.hidden = true;
    ocrOverlay.setAttribute('aria-hidden', 'true');
    delete ocrOverlay.dataset.runId;
  }

  function startPendingIdentify() {
    if (!pendingFiles.length) {
      setStatus('請先選取照片', 'err');
      return;
    }
    const myRun = ++runId;
    hideOcrPrompt();
    const files = pendingFiles.map((p) => p.file);
    runIdentify({ images: files }, myRun);
  }


  // --- Lightbox (viewport-fixed, swipe) ---
  const lightboxEl = $('lightbox');
  const lightboxImg = $('lightbox-img');
  const lightboxCounter = $('lightbox-counter');
  const lightboxPrevBtn = $('lightbox-prev');
  const lightboxNextBtn = $('lightbox-next');
  const lightboxCloseBtn = $('lightbox-close');
  const lightboxStage = $('lightbox-stage');

  let lightboxUrls = [];
  let lightboxIndex = 0;
  let lightboxTouchX = null;
  let lightboxTouchY = null;
  let lightboxScrollY = 0;

  function fullResUrl(url) {
    // Prefer existing pl/jp CDN URLs as-is (already full-res in our data)
    return String(url || '').trim();
  }

  function updateLightboxView() {
    if (!lightboxEl || !lightboxImg) return;
    const url = lightboxUrls[lightboxIndex] || '';
    lightboxImg.src = url;
    lightboxImg.alt = '預覽 ' + (lightboxIndex + 1) + '/' + lightboxUrls.length;
    if (lightboxCounter) {
      lightboxCounter.textContent = lightboxUrls.length
        ? lightboxIndex + 1 + ' / ' + lightboxUrls.length
        : '';
    }
    if (lightboxPrevBtn) lightboxPrevBtn.disabled = lightboxUrls.length <= 1;
    if (lightboxNextBtn) lightboxNextBtn.disabled = lightboxUrls.length <= 1;
  }

  function openLightbox(urls, startIndex) {
    const list = (urls || []).map(fullResUrl).filter(Boolean);
    if (!list.length || !lightboxEl) return;
    lightboxUrls = list;
    lightboxIndex = Math.max(0, Math.min(startIndex || 0, list.length - 1));
    updateLightboxView();
    lightboxScrollY = window.scrollY || window.pageYOffset || 0;
    document.body.classList.add('lightbox-open');
    // Lock scroll without jumping: use position fixed trick for iOS Safari
    document.body.style.position = 'fixed';
    document.body.style.top = '-' + lightboxScrollY + 'px';
    document.body.style.left = '0';
    document.body.style.right = '0';
    document.body.style.width = '100%';
    lightboxEl.hidden = false;
    lightboxEl.classList.remove('hidden');
  }

  function closeLightbox() {
    if (!lightboxEl) return;
    lightboxEl.classList.add('hidden');
    lightboxEl.hidden = true;
    if (lightboxImg) lightboxImg.removeAttribute('src');
    document.body.classList.remove('lightbox-open');
    document.body.style.position = '';
    document.body.style.top = '';
    document.body.style.left = '';
    document.body.style.right = '';
    document.body.style.width = '';
    window.scrollTo(0, lightboxScrollY || 0);
    lightboxUrls = [];
    lightboxIndex = 0;
  }

  function lightboxStep(delta) {
    if (lightboxUrls.length <= 1) return;
    lightboxIndex = (lightboxIndex + delta + lightboxUrls.length) % lightboxUrls.length;
    updateLightboxView();
  }

  function bindLightboxable(img, urls, index) {
    if (!img) return;
    img.style.cursor = 'zoom-in';
    img.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      openLightbox(urls, index);
    });
  }

  /** Collect cover + stills full URLs for a work */
  function workImageSet(w) {
    const urls = [];
    if (w && w.cover) urls.push(w.cover);
    (w && w.stills ? w.stills : []).forEach((u) => {
      if (u && urls.indexOf(u) === -1) urls.push(u);
    });
    return urls;
  }


  // --- Events ---

  if (progressToggle) {
    progressToggle.addEventListener('click', (e) => {
      e.preventDefault();
      toggleProgressCollapsed();
    });
  }

  if (lightboxCloseBtn) lightboxCloseBtn.addEventListener('click', (e) => { e.preventDefault(); closeLightbox(); });
  if (lightboxPrevBtn) lightboxPrevBtn.addEventListener('click', (e) => { e.preventDefault(); lightboxStep(-1); });
  if (lightboxNextBtn) lightboxNextBtn.addEventListener('click', (e) => { e.preventDefault(); lightboxStep(1); });
  if (lightboxEl) {
    lightboxEl.addEventListener('click', (e) => {
      const t = e.target;
      if (t && t.getAttribute && t.getAttribute('data-lightbox-close') === '1') {
        closeLightbox();
      }
    });
  }
  document.addEventListener('keydown', (e) => {
    if (!lightboxEl || lightboxEl.classList.contains('hidden')) return;
    if (e.key === 'Escape') closeLightbox();
    else if (e.key === 'ArrowLeft') lightboxStep(-1);
    else if (e.key === 'ArrowRight') lightboxStep(1);
  });
  if (lightboxStage) {
    lightboxStage.addEventListener('touchstart', (e) => {
      if (!e.touches || !e.touches.length) return;
      lightboxTouchX = e.touches[0].clientX;
      lightboxTouchY = e.touches[0].clientY;
    }, { passive: true });
    lightboxStage.addEventListener('touchend', (e) => {
      if (lightboxTouchX == null) return;
      const t = e.changedTouches && e.changedTouches[0];
      if (!t) { lightboxTouchX = null; return; }
      const dx = t.clientX - lightboxTouchX;
      const dy = t.clientY - (lightboxTouchY || t.clientY);
      lightboxTouchX = null;
      lightboxTouchY = null;
      if (Math.abs(dx) < 50 || Math.abs(dx) < Math.abs(dy)) return;
      if (dx < 0) lightboxStep(1);
      else lightboxStep(-1);
    }, { passive: true });
  }


  // Labels (for=file-pick / file-camera) open the picker natively on iOS.
  // Keep a sync programmatic fallback only if label association is missing.
  function openFilePickerSync(inputEl) {
    if (!inputEl) return;
    inputEl.click();
  }
  const btnPick = $('btn-pick');
  const btnCamera = $('btn-camera');
  if (btnPick && filePick && btnPick.getAttribute('for') !== 'file-pick') {
    btnPick.addEventListener('click', () => openFilePickerSync(filePick));
  }
  if (btnCamera && fileCamera && btnCamera.getAttribute('for') !== 'file-camera') {
    btnCamera.addEventListener('click', () => openFilePickerSync(fileCamera));
  }
  const btnPendingAdd = $('btn-pending-add');
  if (btnPendingAdd && filePick && btnPendingAdd.getAttribute('for') !== 'file-pick') {
    btnPendingAdd.addEventListener('click', () => openFilePickerSync(filePick));
  }
  if ($('btn-pending-start')) {
    $('btn-pending-start').addEventListener('click', startPendingIdentify);
  }
  if ($('btn-pending-clear')) {
    $('btn-pending-clear').addEventListener('click', () => clearPending(true));
  }

  // Snapshot FileList BEFORE clearing value — live FileList empties on iOS Safari.
  if (filePick) {
    filePick.addEventListener('change', () => {
      const files = Array.from(filePick.files || []);
      filePick.value = '';
      if (files.length) {
        addFilesToPending(files);
        showScreen('home');
      }
    });
  }
  if (fileCamera) {
    fileCamera.addEventListener('change', () => {
      const files = Array.from(fileCamera.files || []);
      fileCamera.value = '';
      if (files.length) {
        addFilesToPending(files);
        showScreen('home');
      }
    });
  }

  if ($('btn-demo')) {
    $('btn-demo').addEventListener('click', () => {
      const myRun = ++runId;
      hideOcrPrompt();
      hideUserShots();
      runIdentify({ code: DEMO_CODE }, myRun);
    });
  }

  function submitTypedInput() {
    const v = codeInput.value.trim();
    if (!v) {
      setStatus('請輸入番號或片名', 'err');
      return;
    }
    const myRun = ++runId;
    hideOcrPrompt();
    hideUserShots();
    if (AV_CODE_INPUT_RE.test(v)) {
      runIdentify({ code: v }, myRun);
    } else {
      runIdentify({ title: v }, myRun);
    }
  }

  if ($('btn-code')) $('btn-code').addEventListener('click', submitTypedInput);
  if (codeInput) {
    codeInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        submitTypedInput();
      }
    });
  }

  if ($('btn-reset')) $('btn-reset').addEventListener('click', resetBaseline);

  if ($('btn-ocr-cancel')) {
    $('btn-ocr-cancel').addEventListener('click', () => {
      hideOcrPrompt();
      setStatus('已取消，可重新選圖或輸入番號／片名');
    });
  }

  if ($('btn-ocr-submit')) {
    $('btn-ocr-submit').addEventListener('click', () => {
      const v = (ocrCodeInput && ocrCodeInput.value.trim()) || '';
      if (!v) return;
      const newRun = ++runId;
      hideOcrPrompt();
      if (AV_CODE_INPUT_RE.test(v)) {
        runIdentify({ code: v }, newRun);
      } else {
        runIdentify({ title: v }, newRun);
      }
    });
  }

  if (ocrCodeInput) {
    ocrCodeInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const btn = $('btn-ocr-submit');
        if (btn) btn.click();
      }
    });
  }

  // History nav
  if ($('btn-history')) {
    $('btn-history').addEventListener('click', () => {
      hideOcrPrompt();
      hideProgress();
      renderHistoryList();
      showScreen('history');
    });
  }
  if ($('btn-history-back')) {
    $('btn-history-back').addEventListener('click', () => showScreen('home'));
  }
  if ($('btn-history-detail-back')) {
    $('btn-history-detail-back').addEventListener('click', () => {
      renderHistoryList();
      showScreen('history');
    });
  }
  if ($('btn-history-clear')) {
    $('btn-history-clear').addEventListener('click', () => {
      if (!loadHistory().length) return;
      if (!confirm('確定清除全部辨識紀錄？')) return;
      saveHistory([]);
      renderHistoryList();
    });
  }
  if ($('btn-history-delete')) {
    $('btn-history-delete').addEventListener('click', () => {
      if (!viewingHistoryId) return;
      const next = loadHistory().filter((x) => x.id !== viewingHistoryId);
      saveHistory(next);
      viewingHistoryId = null;
      renderHistoryList();
      showScreen('history');
    });
  }
  // Test / automation hooks (non-production use)
  try {
    window.__lfpAddFiles = addFilesToPending;
    window.__lfpStartPending = startPendingIdentify;
    window.__lfpGetPendingCount = () => pendingFiles.length;
  } catch (_) {}
})();
