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
    // Keep leading zeros (e.g. 029), do not strip
    return `${parts.label}-${parts.number}`;
  }

  /** Same 品番 if label matches and numeric values equal (099 == 99). */
  function codesMatch(a, b) {
    const pa = parseCodeParts(a);
    const pb = parseCodeParts(b);
    if (!pa || !pb) return formatDisplayCode(a) === formatDisplayCode(b);
    return pa.label === pb.label && Number(pa.number) === Number(pb.number);
  }

  /** Local history hit with a real cover for instant gallery (server remains source of truth). */
  function findHistoryByCode(code) {
    if (!code || !parseCodeParts(code)) return null;
    const list = loadHistory();
    for (const rec of list) {
      if (!rec) continue;
      const works = historySessionWorks(rec);
      const hit = works.find((w) => w && w.code && codesMatch(w.code, code)) ||
        (rec.code && codesMatch(rec.code, code) ? rec : null);
      if (!hit) continue;
      const cover = (hit.cover || rec.cover) && String(hit.cover || rec.cover).trim();
      if (cover && !isNowPrintingUrl(cover)) {
        return rec;
      }
    }
    return null;
  }

  function isNowPrintingUrl(url) {
    return /now_printing/i.test(String(url || ''));
  }

  function slimRelatedForHistory(items) {
    return (Array.isArray(items) ? items : []).slice(0, 13).map((r) => ({
      code: r.code || '',
      title: r.title || '',
      title_zh: r.title_zh || r.titleZh || '',
      cover: r.cover || '',
      cid: r.cid || '',
      stills: Array.isArray(r.stills) ? r.stills.slice(0, 10) : [],
      why: r.why || '',
      line: relatedLineFromRaw(r),
    }));
  }

  function relatedLineFromRaw(r) {
    const why = String((r && r.why) || '');
    let rl = (r && r.line) || '';
    if (rl === 'title') rl = 'theme';
    if (rl === 'theme' || rl === 'keyword' || rl === 'actress') return rl;
    if (rl === 'multi' || rl === 'candidate' || rl === 'main') return rl;
    if (/候選|candidate/i.test(why)) return 'candidate';
    if (/多圖/i.test(why)) return 'multi';
    if (/演員|女優|actress/i.test(why)) return 'actress';
    if (/關鍵字|keyword/i.test(why)) return 'keyword';
    if (/主題|theme|片名相近/i.test(why)) return 'theme';
    return rl || 'theme';
  }

  /** Theme/keyword/actress siblings only — never main, multi-shot, or title candidates. */
  function isRelatedBucketItem(r) {
    if (!r) return false;
    const line = String(r.line || '');
    const why = String(r.why || '');
    if (line === 'theme' || line === 'keyword' || line === 'actress' || line === 'title') return true;
    if (line === 'main' || line === 'multi' || line === 'candidate') return false;
    if (/候選|candidate/i.test(why) || /多圖/i.test(why)) return false;
    if (/主題|theme|片名相近/i.test(why)) return true;
    if (/女優|actress|演員/i.test(why)) return true;
    if (/關鍵字|keyword/i.test(why)) return true;
    return false;
  }

  function pickRelatedSource(src, allowFallback, fb) {
    src = src || {};
    if (Array.isArray(src.related_by_title) && src.related_by_title.length) {
      return src.related_by_title;
    }
    if (Array.isArray(src.related) && src.related.length) {
      const only = src.related.filter(isRelatedBucketItem);
      if (only.length) return only;
    }
    if (allowFallback && fb && fb !== src) {
      if (Array.isArray(fb.related_by_title) && fb.related_by_title.length) {
        return fb.related_by_title;
      }
      if (Array.isArray(fb.related) && fb.related.length) {
        const only = fb.related.filter(isRelatedBucketItem);
        if (only.length) return only;
      }
    }
    return [];
  }

  function relatedNeedsTitleZh(related) {
    return (related || []).some((r) => r && r.code && !String(r.title_zh || r.titleZh || '').trim());
  }

  function relatedBucketsNeedFill(related, opts) {
    opts = opts || {};
    const counts = { theme: 0, keyword: 0, actress: 0 };
    (related || []).forEach((r) => {
      const ln = relatedLineFromRaw(r);
      if (counts[ln] != null) counts[ln] += 1;
    });
    const hasTitle = !!(opts.title && String(opts.title).trim());
    const hasActress = !!(opts.actress && String(opts.actress).trim());
    if (hasTitle && counts.theme < 5) return true;
    if (hasTitle && counts.keyword < 5) return true;
    if (hasActress && counts.actress < 3) return true;
    return false;
  }

  function workNeedsTitleZh(w) {
    if (!w) return false;
    const code = String(w.code || '').trim();
    if (!code || code === '片名搜尋' || !parseCodeParts(code)) return false;
    return !String(w.title_zh || w.titleZh || '').trim();
  }

  function workNeedsStillsFill(w) {
    if (!w || !String(w.cid || '').trim()) return false;
    const n = (Array.isArray(w.stills) ? w.stills : []).filter(Boolean).length;
    return n < 10;
  }

  function backfillWorkStillsLocal(w) {
    if (!workNeedsStillsFill(w)) return w;
    const extra = stillUrls(String(w.cid), 10);
    return Object.assign({}, w, { stills: mergeStillsKeepExisting(w.stills, extra) });
  }

  function backfillWorkTreeLocal(w) {
    if (!w) return w;
    let next = backfillWorkStillsLocal(w);
    const rel = next.related || [];
    let relChanged = false;
    const nextRel = rel.map((r) => {
      const fr = backfillWorkStillsLocal(r);
      if (fr !== r) relChanged = true;
      return fr;
    });
    if (relChanged) next = Object.assign({}, next, { related: nextRel });
    return next;
  }

  function mergeStillsKeepExisting(prev, incoming) {
    const out = [];
    const seen = {};
    (Array.isArray(prev) ? prev : []).concat(Array.isArray(incoming) ? incoming : []).forEach((u) => {
      const s = String(u || '').trim();
      if (!s || /now_printing/i.test(s) || seen[s]) return;
      seen[s] = true;
      out.push(s);
    });
    return out.slice(0, 20);
  }

  function capRelatedBuckets(items) {
    const buckets = { theme: [], keyword: [], actress: [] };
    const caps = { theme: 5, keyword: 5, actress: 3 };
    const seen = {};
    (items || []).forEach((r) => {
      if (!r || !r.code) return;
      const key = parseCodeParts(String(r.code)) ? formatDisplayCode(String(r.code)) : String(r.code);
      if (seen[key]) return;
      const line = relatedLineFromRaw(r);
      if (!buckets[line] || buckets[line].length >= caps[line]) return;
      seen[key] = true;
      buckets[line].push(Object.assign({}, r, { line: line }));
    });
    return buckets.theme.concat(buckets.keyword, buckets.actress);
  }

  function mergeRelatedIncremental(existing, incoming) {
    const byKey = {};
    const order = [];
    function keyOf(r) {
      return r && r.code && parseCodeParts(String(r.code))
        ? formatDisplayCode(String(r.code))
        : String((r && r.code) || '');
    }
    slimRelatedForHistory(existing).concat(slimRelatedForHistory(incoming)).forEach((r) => {
      const k = keyOf(r);
      if (!k) return;
      if (!byKey[k]) {
        byKey[k] = Object.assign({}, r);
        order.push(k);
        return;
      }
      const cur = byKey[k];
      ['title', 'title_zh', 'cover', 'why', 'line', 'actress', 'cid'].forEach((f) => {
        if (!cur[f] && r[f]) cur[f] = r[f];
      });
      cur.stills = mergeStillsKeepExisting(cur.stills, r.stills);
    });
    return capRelatedBuckets(order.map((k) => byKey[k]));
  }

  function mergeTitleZhIntoRelated(existing, incoming) {
    return mergeRelatedIncremental(existing, incoming);
  }

  function backfillHistoryTitleZh(list, donor) {
    if (!donor || !Array.isArray(list)) return list;
    const donorWorks = Array.isArray(donor.works) && donor.works.length
      ? donor.works
      : Array.isArray(donor.results) && donor.results.length
        ? donor.results
        : [donor];
    return list.map((rec) => {
      if (!rec) return rec;
      const works = historySessionWorks(rec);
      let changed = false;
      const nextWorks = works.map((w) => {
        const match = donorWorks.find((d) => d && w && w.code && d.code && codesMatch(w.code, d.code));
        if (!match) return w;
        changed = true;
        const nw = Object.assign({}, w);
        const donorZh = String(match.title_zh || match.titleZh || '').trim();
        if (!String(nw.title_zh || '').trim() && donorZh) nw.title_zh = donorZh;
        const donorStills = Array.isArray(match.stills) ? match.stills : [];
        if (donorStills.length) nw.stills = mergeStillsKeepExisting(nw.stills, donorStills);
        const donorRelated = Array.isArray(match.related_by_title) && match.related_by_title.length
          ? match.related_by_title
          : (match.related || []).filter(isRelatedBucketItem);
        if (donorRelated.length) {
          nw.related = nw.related && nw.related.length
            ? mergeRelatedIncremental(nw.related, donorRelated)
            : slimRelatedForHistory(donorRelated);
        }
        if (match.cover && !nw.cover) nw.cover = match.cover;
        return nw;
      });
      if (!changed) return rec;
      const first = nextWorks[0] || rec;
      return Object.assign({}, rec, {
        works: nextWorks,
        title_zh: rec.title_zh || first.title_zh || '',
        related: first.related || rec.related,
        stills: first.stills || rec.stills,
        cover: rec.cover || first.cover,
      });
    });
  }

  function historySessionWorks(rec) {
    if (!rec) return [];
    if (Array.isArray(rec.works) && rec.works.length) return rec.works;
    return [
      {
        code: rec.code,
        title: rec.title,
        title_zh: rec.title_zh,
        cover: rec.cover,
        stills: rec.stills,
        actress: rec.actress,
        cid: rec.cid || '',
        related: rec.related || [],
        line: 'main',
      },
    ];
  }

  function slimWorkForHistory(item, fallback, line) {
    const src = item || {};
    const fb = fallback || {};
    const lineOut = line || src.line || 'main';
    const codeRaw = src.code || fb.code || '';
    const code = codeRaw && parseCodeParts(String(codeRaw))
      ? formatDisplayCode(String(codeRaw))
      : String(codeRaw || '');
    const relatedSrc = pickRelatedSource(src, lineOut === 'main', fb);
    return {
      code: code,
      title: src.title || fb.title || '',
      title_zh: src.title_zh || src.titleZh || fb.title_zh || '',
      cover: src.cover || fb.cover || '',
      cid: src.cid || fb.cid || '',
      stills: Array.isArray(src.stills) ? src.stills.slice(0, 12) : (fb.stills || []).slice(0, 12),
      actress: src.actress || fb.actress || '',
      line: lineOut,
      related: slimRelatedForHistory(relatedSrc),
    };
  }

  function sessionWorksFromIdentify(data) {
    if (!data) return [];
    const hasResults = Array.isArray(data.results) && data.results.length;
    const rows = hasResults ? data.results : [data];
    const works = [];
    const seen = {};
    function pushWork(item, line) {
      if (!item || isRelatedBucketItem(item)) return;
      const work = slimWorkForHistory(item, data, line);
      if (!work.code && !work.title) return;
      const key = work.code ? String(work.code).toUpperCase() : ('t:' + work.title);
      if (seen[key]) return;
      seen[key] = true;
      works.push(work);
    }
    rows.forEach((item, i) => {
      const line = i === 0
        ? 'main'
        : (item && item.line === 'candidate' ? 'candidate' : ((item && item.line) || 'multi'));
      pushWork(item, line);
    });
    // Title-search alternatives belong on the vertical axis of this session
    if (!hasResults) {
      const apiCands = Array.isArray(data.candidates) ? data.candidates : [];
      if (apiCands.length >= 2) {
        apiCands.forEach((c) => pushWork(c, 'candidate'));
      }
    }
    return works;
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


  function stripEmptyParens(s) {
    return String(s || '')
      .replace(/[（(]\s*[）)]/g, '')
      .replace(/\s+/g, ' ')
      .trim();
  }

  /**
   * Display title: 日本語（中文）only when title_zh is a real source string.
   * Missing Chinese → Japanese only (no empty （）). Never invent Chinese.
   */
  function formatDisplayTitle(titleJa, titleZh) {
    let ja = stripEmptyParens(titleJa);
    let zh = stripEmptyParens(titleZh);
    if (zh && ja && zh === ja) zh = '';
    if (!ja && !zh) return '（無標題）';
    if (!zh) return ja || '（無標題）';
    if (!ja) return zh;
    if (ja.includes('（' + zh + '）') || ja.includes('(' + zh + ')')) return ja;
    return ja + '（' + zh + '）';
  }

  /** Clipboard string for 番號+名稱: CODE then newline then the on-screen title. */
  function formatCodeTitleClipboard(code, displayTitle) {
    const c = String(code || '').trim();
    const t = String(displayTitle || '').trim();
    if (c && t) return c + '\n' + t;
    return c || t;
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
    // Never invent DMM CID from the display code: padded guesses (dosd00008)
    // often redirect to now_printing after the server already cleared cover.
    const cid = String(raw.cid || '');
    let cover = String(raw.cover || raw.cover_url || '').trim();
    if (isNowPrintingUrl(cover)) cover = '';
    if (!cover && cid && !isNowPrintingUrl(cid)) {
      cover = coverUrl(cid);
      if (isNowPrintingUrl(cover)) cover = '';
    }
    let stills = Array.isArray(raw.stills) && raw.stills.length
      ? raw.stills.map((u) => String(u || '')).filter((u) => u && !isNowPrintingUrl(u))
      : [];
    if (!stills.length && cid) {
      stills = stillUrls(cid, 10);
    }
    const lineOut = line || raw.line || 'main';
    let relatedByTitle = [];
    if (lineOut !== 'theme' && lineOut !== 'keyword' && lineOut !== 'actress') {
      const relRaw = Array.isArray(raw.related_by_title) && raw.related_by_title.length
        ? raw.related_by_title
        : (Array.isArray(raw.related) ? raw.related.filter(isRelatedBucketItem) : []);
      relatedByTitle = relRaw.map((r) => workFromApi(r, relatedLineFromRaw(r)));
    }
    return {
      code,
      title: raw.title ? String(raw.title) : '',
      titleZh: String(raw.title_zh || raw.titleZh || '').trim(),
      actress: raw.actress ? String(raw.actress) : '',
      studio: raw.studio ? String(raw.studio) : '',
      cid,
      line: lineOut,
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
      // Collapse so sticky expanded panel does not eat the gallery viewport
      setProgressCollapsed(true);
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

    // Prefer explicit results[] from multi-identify (vertical = this query's mains only)
    if (Array.isArray(data.results) && data.results.length) {
      const items = data.results.map((r, i) =>
        workFromApi(r, i === 0 ? 'main' : (r.line || 'multi'))
      );
      items.forEach((w) => {
        if (w.code && !w.titleOnly) seenCodes.add(String(w.code).toUpperCase());
      });
      // Related stays nested on each main — never extra vertical rows
      if (items[0] && (!items[0].relatedByTitle || !items[0].relatedByTitle.length)
          && Array.isArray(data.related_by_title) && data.related_by_title.length) {
        items[0].relatedByTitle = data.related_by_title.map((r) =>
          workFromApi(r, relatedLineFromRaw(r))
        );
      } else if (items[0] && (!items[0].relatedByTitle || !items[0].relatedByTitle.length)
          && Array.isArray(data.related) && data.related.length) {
        const nested = data.related.filter(isRelatedBucketItem);
        if (nested.length) {
          items[0].relatedByTitle = nested.map((r) => workFromApi(r, relatedLineFromRaw(r)));
        }
      }
      let notice = data.related_note || data.message || null;
      return { items: items, notice: notice || null, source: 'api' };
    }

    const main = workFromApi(data, 'main');
    if (main.code && !main.titleOnly) seenCodes.add(String(main.code).toUpperCase());

    const items = [main];
    // Title-search alternative 番號: extra vertical works for this query (not related)
    const apiCands = Array.isArray(data.candidates) ? data.candidates : [];
    if (apiCands.length >= 2) {
      apiCands.forEach((c) => {
        if (!c || !c.code) return;
        const key = String(c.code).toUpperCase();
        if (seenCodes.has(key)) return;
        seenCodes.add(key);
        items.push(workFromApi(c, 'candidate'));
      });
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
    if (main.titleOnly && items.length < 2) {
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
    card.appendChild(buildWorkActions(w));
    card.appendChild(coverWrap);
    appendStillsScroll(card, w, '劇照（橫滑）');
    return card;
  }

  const ICON_COPY =
    '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="5.2" y="3" width="7.6" height="10.2" rx="1.4" fill="none" stroke="currentColor" stroke-width="1.35"/><path d="M3.4 5.4h1.4v8.4c0 .7.5 1.2 1.15 1.2H11" fill="none" stroke="currentColor" stroke-width="1.35"/></svg>';
  const ICON_DL =
    '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.2v8.2" fill="none" stroke="currentColor" stroke-width="1.45" stroke-linecap="round"/><path d="M4.6 8.4 8 11.8l3.4-3.4" fill="none" stroke="currentColor" stroke-width="1.45" stroke-linecap="round" stroke-linejoin="round"/><path d="M3.2 13.8h9.6" fill="none" stroke="currentColor" stroke-width="1.45" stroke-linecap="round"/></svg>';

  function stopCarouselBubble(e) {
    if (!e) return;
    if (e.stopPropagation) e.stopPropagation();
  }

  function bindWorkAction(btn, handler) {
    btn.addEventListener('pointerdown', stopCarouselBubble);
    btn.addEventListener('touchstart', stopCarouselBubble, { passive: true });
    btn.addEventListener('click', (e) => {
      stopCarouselBubble(e);
      if (e && e.preventDefault) e.preventDefault();
      handler(e);
    });
  }

  function buildWorkActions(w) {
    const bar = document.createElement('div');
    bar.className = 'work-actions';
    bar.setAttribute('role', 'group');
    bar.setAttribute('aria-label', '複製與下載');
    const displayTitle = formatDisplayTitle(w.title, w.titleZh);
    const specs = [
      { mark: '番', icon: ICON_COPY, label: '複製番號', run: () => copyWorkField(w.code, '已複製') },
      { mark: '名', icon: ICON_COPY, label: '複製名稱', run: () => copyWorkField(displayTitle, '已複製') },
      {
        mark: '合',
        icon: ICON_COPY,
        label: '複製番號與名稱',
        run: () => copyWorkField(formatCodeTitleClipboard(w.code, displayTitle), '已複製'),
      },
      { mark: '', icon: ICON_DL, label: '下載封面與劇照', run: () => downloadWorkMedia(w) },
    ];
    specs.forEach((spec) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'work-action' + (spec.mark ? '' : ' work-action-dl');
      btn.setAttribute('aria-label', spec.label);
      btn.title = spec.label;
      btn.innerHTML = spec.icon + (spec.mark ? '<span class="work-action-mark">' + spec.mark + '</span>' : '');
      bindWorkAction(btn, spec.run);
      bar.appendChild(btn);
    });
    return bar;
  }

  let toastTimer = null;
  function showToast(msg) {
    const el = $('lfp-toast');
    if (!el) {
      setStatus(msg, 'ok');
      return;
    }
    el.textContent = msg;
    el.classList.remove('hidden');
    el.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      el.classList.add('hidden');
      el.hidden = true;
    }, 2400);
  }

  function copyTextFallback(str) {
    return new Promise((resolve, reject) => {
      const ta = document.createElement('textarea');
      ta.value = str;
      ta.setAttribute('readonly', '');
      ta.setAttribute('aria-hidden', 'true');
      ta.style.cssText =
        'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0.01;padding:0;border:0;z-index:9999;';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      try {
        ta.setSelectionRange(0, str.length);
      } catch (_) {}
      let ok = false;
      try {
        ok = document.execCommand('copy');
      } catch (_) {}
      document.body.removeChild(ta);
      if (ok) resolve();
      else reject(new Error('copy_failed'));
    });
  }

  function copyTextToClipboard(text) {
    const str = String(text == null ? '' : text);
    if (!str) return Promise.reject(new Error('empty'));
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      return navigator.clipboard.writeText(str).catch(() => copyTextFallback(str));
    }
    return copyTextFallback(str);
  }

  function copyWorkField(text, okMsg) {
    copyTextToClipboard(text)
      .then(() => showToast(okMsg || '已複製'))
      .catch(() => showToast('複製失敗'));
  }

  function workDownloadUrls(w) {
    const urls = [];
    const seen = {};
    function add(u) {
      const s = String(u || '').trim();
      if (!s || isNowPrintingUrl(s) || seen[s]) return;
      seen[s] = true;
      urls.push(s);
    }
    add(w && w.cover);
    (w && w.stills ? w.stills : []).forEach(add);
    return urls;
  }

  function triggerBlobDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || 'image.jpg';
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => {
      try {
        URL.revokeObjectURL(url);
      } catch (_) {}
    }, 4000);
  }

  function workDownloadFilename(code, url, index, coverUrl) {
    const stem = (code && parseCodeParts(code) ? formatDisplayCode(code) : code) || 'work';
    const safe = String(stem).replace(/[^A-Za-z0-9._-]+/g, '_') || 'work';
    if (index === 0 && coverUrl && url === coverUrl) return safe + '-cover.jpg';
    const n = coverUrl && index > 0 ? index : index + 1;
    return safe + '-jp-' + String(n).padStart(2, '0') + '.jpg';
  }

  async function downloadOneImage(url, filename) {
    const res = await fetch('/api/cdn-file?url=' + encodeURIComponent(url));
    if (!res.ok) throw new Error('cdn');
    const buf = await res.arrayBuffer();
    const blob = new Blob([buf], { type: 'image/jpeg' });
    triggerBlobDownload(blob, filename);
  }

  function clickCdnDownload(url, filename) {
    const a = document.createElement('a');
    a.href = '/api/cdn-file?url=' + encodeURIComponent(url);
    a.download = filename || 'image.jpg';
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  async function downloadWorkMedia(w) {
    const urls = workDownloadUrls(w);
    if (!urls.length) {
      showToast('沒有可下載的圖片');
      return;
    }
    const total = urls.length;
    for (let i = 0; i < total; i++) {
      showToast('下載中（' + (i + 1) + '/' + total + '）');
      const fname = workDownloadFilename(w.code, urls[i], i, w.cover);
      try {
        await downloadOneImage(urls[i], fname);
      } catch (_) {
        clickCdnDownload(urls[i], fname);
      }
      if (i < total - 1) await sleep(450);
    }
    showToast('下載中（' + total + '/' + total + '）');
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
    // Reset scroll so carousel hint / pager clear sticky chrome + safe area
    try { window.scrollTo(0, 0); } catch (_) {}
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
          related: (r.related || []).slice(0, 8),
          works: (r.works || []).map((w) => ({
            ...w,
            stills: (w.stills || []).slice(0, 6),
            related: (w.related || []).slice(0, 8),
          })),
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
    const works = sessionWorksFromIdentify(data);
    if (!works.length) return;

    const userShots = await buildUserShotThumbs(userFiles || []);
    const first = works[0];
    const rec = {
      id: 'h_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7),
      ts: Date.now(),
      kind: 'session',
      code: first.code,
      title: first.title,
      title_zh: first.title_zh || '',
      cover: first.cover || '',
      stills: first.stills || [],
      actress: first.actress || '',
      userShots: userShots,
      related: first.related || [],
      works: works,
    };

    let list = loadHistory();
    list.unshift(rec);
    list = backfillHistoryTitleZh(list, rec);
    list = backfillHistoryTitleZh(list, data);
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

  /** Every stored row can open — success/failure, session or legacy. Cover/title/ok do not gate. */
  function historyRecordIsOpenable(rec) {
    return !!(rec && rec.id);
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
      const works = historySessionWorks(rec);
      const first = works[0] || rec || {};
      const row = document.createElement('div');
      row.className = 'history-item';
      row.setAttribute('role', 'button');
      row.tabIndex = 0;
      const thumbSrc =
        rec.cover ||
        first.cover ||
        (rec.userShots && rec.userShots[0]) ||
        '';
      let thumbHtml;
      if (thumbSrc) {
        thumbHtml =
          '<img class="history-thumb" src="' +
          escapeHtml(thumbSrc) +
          '" alt="" loading="lazy" referrerpolicy="no-referrer" />';
      } else {
        thumbHtml = '<div class="history-thumb placeholder">無圖</div>';
      }
      const codeLabel = rec.code || first.code || (rec.ok === false ? '未找到' : '—');
      const titleJa = rec.title || first.title || '';
      const titleZh = rec.title_zh || first.title_zh || first.titleZh || '';
      let titleText = formatDisplayTitle(titleJa, titleZh);
      if (titleText === '（無標題）' && rec.message) titleText = String(rec.message);
      row.innerHTML =
        thumbHtml +
        '<div class="history-meta">' +
        '<p class="history-code">' +
        escapeHtml(codeLabel) +
        (works.length > 1 ? ' <span class="badge">' + works.length + ' 部</span>' : '') +
        '</p>' +
        '<p class="history-title">' +
        escapeHtml(titleText) +
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
      const open = (e) => {
        if (e) {
          e.preventDefault();
          e.stopPropagation();
        }
        if (!historyRecordIsOpenable(rec)) return;
        openHistoryDetail(rec.id);
      };
      row.addEventListener('click', open);
      row.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          open(e);
        }
      });
      historyList.appendChild(row);
    });
  }

  function identifyPayloadFromHistory(rec) {
    const works = historySessionWorks(rec);
    const first = works[0] || rec || {};
    return {
      ok: true,
      code: first.code || rec.code,
      title: first.title || rec.title || '',
      title_zh: first.title_zh || rec.title_zh || '',
      actress: first.actress || rec.actress || '',
      cover: first.cover || rec.cover || '',
      stills: Array.isArray(first.stills) ? first.stills : (rec.stills || []),
      related_by_title: first.related || rec.related || [],
      from_offline_cache: true,
      // Always nested: vertical = session works, horizontal = each work's related
      results: works.map((w, i) => ({
        ok: true,
        code: w.code,
        title: w.title,
        title_zh: w.title_zh,
        actress: w.actress,
        cid: w.cid,
        cover: w.cover,
        stills: w.stills || [],
        related_by_title: w.related || [],
        line: i === 0 ? 'main' : (w.line || 'multi'),
      })),
    };
  }

  function persistHistoryWork(recId, workIndex, patch) {
    const list = loadHistory();
    const idx = list.findIndex((x) => x.id === recId);
    if (idx < 0) return;
    const works = historySessionWorks(list[idx]);
    if (works[workIndex]) works[workIndex] = Object.assign({}, works[workIndex], patch);
    list[idx].works = works;
    if (workIndex === 0 && patch.related) list[idx].related = patch.related;
    if (patch.title_zh && !list[idx].title_zh) list[idx].title_zh = patch.title_zh;
    saveHistory(list);
  }

  async function fillWorkRelatedGaps(work, recId, workIndex) {
    const before = work;
    work = backfillWorkTreeLocal(work);
    if (work !== before) {
      const localPatch = { stills: work.stills, cid: work.cid };
      if (Array.isArray(work.related)) localPatch.related = work.related;
      persistHistoryWork(recId, workIndex, localPatch);
    }
    const related = work.related || [];
    const need =
      relatedNeedsTitleZh(related) ||
      relatedBucketsNeedFill(related, { title: work.title, actress: work.actress }) ||
      workNeedsTitleZh(work);
    if (!need) return work;
    try {
      const res = await fetch('/api/related-by-title', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: work.title || '',
          code: work.code || '',
          actress: work.actress || '',
          seed: related,
        }),
      });
      const data = await res.json();
      if (data && data.ok && Array.isArray(data.related_by_title)) {
        const incoming = data.related_by_title;
        const merged = related && related.length
          ? mergeRelatedIncremental(related, incoming)
          : slimRelatedForHistory(incoming);
        const patch = { related: merged, stills: work.stills, cid: work.cid };
        const incomingZh = String(data.title_zh || '').trim();
        if (incomingZh && !String(work.title_zh || work.titleZh || '').trim()) {
          patch.title_zh = incomingZh;
        }
        work = backfillWorkTreeLocal(Object.assign({}, work, patch));
        patch.stills = work.stills;
        patch.related = work.related;
        persistHistoryWork(recId, workIndex, patch);
      }
    } catch (_) {}
    return work;
  }

  function paintHistoryDetail(rec) {
    historyDetailEl.innerHTML = '';
    if (rec.ok === false && rec.message) {
      const notice = document.createElement('div');
      notice.className = 'notice';
      notice.textContent = String(rec.message);
      historyDetailEl.appendChild(notice);
    }
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
    const works = historySessionWorks(rec);
    const paintWorks = works.filter((w) => {
      if (!w) return false;
      const code = String(w.code || '').trim();
      if (code && code !== '片名搜尋') return true;
      if (String(w.title || '').trim()) return true;
      if (String(w.cover || '').trim()) return true;
      if (Array.isArray(w.stills) && w.stills.length) return true;
      return false;
    });
    if (!paintWorks.length) {
      if (!(rec.ok === false && rec.message)) {
        const empty = document.createElement('div');
        empty.className = 'notice';
        empty.textContent = rec.message || '此筆沒有可顯示的作品。';
        historyDetailEl.appendChild(empty);
      }
      return;
    }
    const payload = identifyPayloadFromHistory(Object.assign({}, rec, { works: paintWorks }));
    const result = galleryFromIdentify(payload);
    (result.items || []).forEach((w) => {
      historyDetailEl.appendChild(buildWorkCarousel(w));
    });
  }

  async function enrichHistoryDetail(rec, id) {
    try {
      const works = historySessionWorks(rec);
      let changed = false;
      const nextWorks = [];
      for (let i = 0; i < works.length; i++) {
        const next = await fillWorkRelatedGaps(works[i], id, i);
        if (
          next !== works[i] ||
          JSON.stringify(next && next.related) !== JSON.stringify(works[i] && works[i].related) ||
          String((next && next.title_zh) || '') !== String((works[i] && works[i].title_zh) || '')
        ) {
          changed = true;
        }
        nextWorks.push(next);
      }
      if (viewingHistoryId !== id) return;
      if (!changed) return;
      const fresh = loadHistory().find((x) => x.id === id) || Object.assign({}, rec, { works: nextWorks });
      paintHistoryDetail(fresh);
    } catch (_) {}
  }

  function openHistoryDetail(id) {
    const rec = loadHistory().find((x) => x.id === id);
    if (!rec) return false;
    viewingHistoryId = id;
    paintHistoryDetail(rec);
    showScreen('history-detail');
    hideUserShots();
    hideProgress();
    enrichHistoryDetail(rec, id);
    return true;
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

    // Code-only: if local history has title+cover, show gallery immediately (server still refreshes)
    let historyPreviewShown = false;
    if (code && !imgs.length && !title) {
      const hist = findHistoryByCode(code);
      if (hist && (hist.title || hist.cover || (hist.works && hist.works.length))) {
        try {
          const previewData = identifyPayloadFromHistory(hist);
          previewData.message = '瀏覽紀錄快取（等候伺服器確認）';
          const result = galleryFromIdentify(previewData);
          renderGallery(result);
          setStatus('瀏覽紀錄快取 · 伺服器查詢中…', 'busy');
          historyPreviewShown = true;
        } catch (_) {}
      }
    }

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
        // Keep progress collapsed (not expanded) above gallery; auto-hide shortly
        setProgressCollapsed(true);
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
        // Auto-hide progress so it cannot permanently cover gallery bottom/footer
        const hideRun = myRun;
        setTimeout(() => {
          if (hideRun !== runId) return;
          if (progressPanel && progressPanel.classList.contains('is-complete')) {
            hideProgress();
          }
        }, 1400);
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
    window.__lfpHistory = {
      sessionWorksFromIdentify,
      identifyPayloadFromHistory,
      galleryFromIdentify,
      historySessionWorks,
      isRelatedBucketItem,
      formatDisplayTitle,
      formatCodeTitleClipboard,
      historyRecordIsOpenable,
      renderHistoryList,
      openHistoryDetail,
      relatedNeedsTitleZh,
      relatedBucketsNeedFill,
      workNeedsTitleZh,
      workDownloadFilename,
      workDownloadUrls,
    };
  } catch (_) {}
})();
