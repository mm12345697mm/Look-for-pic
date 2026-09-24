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
  const USER_SHOT_THUMB_MAX_SIDE = 480;
  const USER_SHOT_SESSION_MAX = 20;
  // Bottom「關鍵字再搜」row only. Main carousel keyword bucket stays at 5.
  const KEYWORD_RESEARCH_CAP = 10;

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
  const progressSkipRow = $('progress-skip-row');
  const btnSkipSlot = $('btn-skip-slot');
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
  let batchFiles = [];
  let identifyBusy = false;
  let promoteBusy = false;
  let lastPromoteTask = null;
  let promoteEpoch = 0;

  function batchQueryKeepsFrames(fileCount, busy, openFrame) {
    return fileCount > 1 && !!(busy || openFrame);
  }

  function galleryHasOpenFrame() {
    return (lastGalleryItems || []).some((w) => w && (w.unidentified || w.titleOnly));
  }
  /** Object URLs / dataURLs for sticky comparison (cleared on 重新開始) */
  let lastUserShotUrls = [];
  let viewingHistoryId = null;
  /** Last painted gallery mains (so 手動修正 can patch in place). */
  let lastGalleryItems = [];

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
    const s = String(url || '');
    return /now_printing/i.test(s) || /\/noimage\//i.test(s);
  }

  function slimRelatedForHistory(items) {
    return (Array.isArray(items) ? items : []).slice(0, 13).map((r) => ({
      code: r.code || '',
      title: r.title || '',
      title_zh: r.title_zh || r.titleZh || '',
      actress: r.actress || '',
      actress_zh: r.actress_zh || r.actressZh || '',
      cover: r.cover || '',
      cid: r.cid || '',
      stills: Array.isArray(r.stills) ? r.stills.slice(0, 10) : [],
      why: r.why || '',
      line: relatedLineFromRaw(r),
      keyword_hits: r.keyword_hits || r.keywordHits || 0,
      matched_keywords: normalizeKeywordList(
        r.matched_keywords || r.matchedKeywords || r.hit_keywords || r.hitKeywords
      ),
    }));
  }

  function whyIsActressBucket(why) {
    return /同女優|同演員/.test(String(why || ''));
  }

  function whyIsThemeBucket(why) {
    const text = String(why || '');
    return /片名|同系列|主題相近/.test(text) || text.indexOf('主題') === 0;
  }

  function relatedLineFromRaw(r) {
    const why = String((r && r.why) || '');
    let rl = (r && r.line) || '';
    if (rl === 'title') rl = 'theme';
    // Explicit bucket wins. A 同女優 note that only mentions 主題 in passing
    // stays actress so it cannot sit between 片名 and 關鍵字.
    if (rl === 'keyword') return 'keyword';
    if (rl === 'theme' && whyIsActressBucket(why) && !whyIsThemeBucket(why)) return 'actress';
    if (rl === 'theme' || rl === 'actress') return rl;
    if (rl === 'multi' || rl === 'candidate' || rl === 'main') return rl;
    if (/候選|candidate/i.test(why)) return 'candidate';
    if (/多圖/i.test(why)) return 'multi';
    if (whyIsThemeBucket(why)) return 'theme';
    if (/關鍵字|keyword/i.test(why)) return 'keyword';
    if (whyIsActressBucket(why) || /演員|女優|actress/i.test(why)) return 'actress';
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

  function workNeedsThemeKeywords(work) {
    const raw = (work && (work.theme_keywords || work.themeKeywords)) || [];
    const items = Array.isArray(raw)
      ? raw
      : (typeof raw === 'string' ? raw.split(/[\s,，、・/|]+/) : []);
    // BOD / VOL stored on an old card is not a theme. Refresh from the title.
    if (items.some((item) => isEditionKeyword(item))) return true;
    const related = (work && work.related) || [];
    const hasKw = related.some((r) => relatedLineFromRaw(r) === 'keyword');
    if (!hasKw) return false;
    return !normalizeKeywordList(raw).length;
  }

  function relatedHasPersistedMembership(related) {
    return (related || []).some((r) => r && String(r.code || '').trim());
  }

  function relatedBucketsNeedFill(related, opts) {
    // Caps are maxima. A saved list — even a short one — is finished.
    // Search only when related was never stored and we have a title or actress.
    opts = opts || {};
    if (relatedHasPersistedMembership(related)) return false;
    const hasTitle = !!(opts.title && String(opts.title).trim());
    const hasActress = !!(opts.actress && String(opts.actress).trim());
    return hasTitle || hasActress;
  }

  function applyTitleZhOntoRelated(existing, incoming) {
    const byCode = {};
    (incoming || []).forEach((r) => {
      if (!r || !r.code) return;
      byCode[String(r.code)] = r;
    });
    return (existing || []).map((r) => {
      if (!r || !r.code) return r;
      const hit = byCode[String(r.code)];
      const zh = hit ? String(hit.title_zh || hit.titleZh || '').trim() : '';
      if (!zh || String(r.title_zh || r.titleZh || '').trim()) return r;
      return Object.assign({}, r, { title_zh: zh });
    });
  }

  function workNeedsTitleZh(w) {
    if (!w) return false;
    const code = String(w.code || '').trim();
    if (!code || code === '片名搜尋' || !parseCodeParts(code)) return false;
    return !String(w.title_zh || w.titleZh || '').trim();
  }

  function isLocalCoverUrl(url) {
    return /^(data:|blob:)/i.test(String(url || '').trim());
  }

  function workHasUsableCover(w) {
    const url = String((w && w.cover) || '').trim();
    if (!url || isNowPrintingUrl(url)) return false;
    if (w && w.titleOnly && !isLocalCoverUrl(url)) return false;
    return true;
  }

  function workHasUsableTitle(w) {
    const t = formatDisplayTitle(w && w.title, w && (w.titleZh || w.title_zh));
    return !!(t && t !== '（無標題）');
  }

  /** Missing cover and/or name — user can fix without the bot. */
  function workNeedsManualFix(w) {
    if (!w || w.skipped) return false;
    return !workHasUsableCover(w) || !workHasUsableTitle(w);
  }

  function mergeManualFixIntoWork(oldW, data, localCoverUrl) {
    const usable =
      data &&
      typeof data === 'object' &&
      (parseCodeParts(String(data.code || '')) ||
        String(data.title || '').trim() ||
        String(data.cover || data.cover_url || '').trim());
    const incoming = usable ? workFromApi(data, (oldW && oldW.line) || 'main') : null;
    const next = Object.assign({}, oldW || {});
    if (incoming) {
      if (incoming.code && (!incoming.titleOnly || parseCodeParts(incoming.code))) {
        next.code = incoming.code;
      }
      if (incoming.title) next.title = incoming.title;
      if (incoming.titleZh) next.titleZh = incoming.titleZh;
      if (incoming.actress) next.actress = incoming.actress;
      if (incoming.studio) next.studio = incoming.studio;
      if (incoming.cid) next.cid = incoming.cid;
      if (incoming.cover) next.cover = incoming.cover;
      if (incoming.stills && incoming.stills.length) next.stills = incoming.stills;
      if (incoming.relatedByTitle && incoming.relatedByTitle.length) {
        next.relatedByTitle = incoming.relatedByTitle;
      }
    }
    if ((!next.cover || isNowPrintingUrl(next.cover)) && localCoverUrl) {
      next.cover = localCoverUrl;
    }
    if (parseCodeParts(next.code) && next.code !== '片名搜尋') next.titleOnly = false;
    else if (isLocalCoverUrl(next.cover)) next.titleOnly = !!next.titleOnly;
    return next;
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
      ['title', 'title_zh', 'cover', 'why', 'line', 'actress', 'cid', 'keyword_hits'].forEach((f) => {
        if (!cur[f] && r[f]) cur[f] = r[f];
      });
      if ((!cur.matched_keywords || !cur.matched_keywords.length) && r.matched_keywords && r.matched_keywords.length) {
        cur.matched_keywords = r.matched_keywords;
      }
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
    const skippedSlot = !!(src.skipped);
    const codeRaw = skippedSlot ? '' : (src.code || fb.code || '');
    const code = skippedSlot
      ? ''
      : codeRaw && parseCodeParts(String(codeRaw))
        ? formatDisplayCode(String(codeRaw))
        : String(codeRaw || '');
    const skipped = !!(src.skipped);
    const relatedSrc = skipped ? [] : pickRelatedSource(src, lineOut === 'main', fb);
    // Another 番號 must not inherit this query's Chinese title. Missing
    // title_zh stays Japanese-only; never invent a translation.
    const sameCode = !!(
      src.code &&
      fb.code &&
      codesMatch(String(src.code), String(fb.code))
    );
    const titleZh = String(src.title_zh || src.titleZh || '').trim()
      || (sameCode ? String(fb.title_zh || fb.titleZh || '').trim() : '');
    return {
      code: code,
      title: skippedSlot ? String(src.title || '').trim() : (src.title || fb.title || ''),
      title_zh: skippedSlot ? '' : titleZh,
      cover: skippedSlot ? '' : (src.cover || fb.cover || ''),
      cid: skippedSlot ? '' : (src.cid || fb.cid || ''),
      stills: skippedSlot
        ? []
        : Array.isArray(src.stills) ? src.stills.slice(0, 12) : (fb.stills || []).slice(0, 12),
      actress: skippedSlot ? '' : (src.actress || fb.actress || ''),
      actress_zh: skippedSlot
        ? ''
        : String(src.actress_zh || src.actressZh || fb.actress_zh || fb.actressZh || '').trim(),
      studio: skippedSlot ? '' : (src.studio || fb.studio || ''),
      studio_zh: skippedSlot
        ? ''
        : String(src.studio_zh || src.studioZh || fb.studio_zh || fb.studioZh || '').trim(),
      line: lineOut,
      related: slimRelatedForHistory(relatedSrc),
      theme_keywords: skippedSlot ? [] : normalizeKeywordList(src.theme_keywords || src.themeKeywords),
      keyword_queries: skippedSlot ? [] : normalizeKeywordList(src.keyword_queries || src.keywordQueries),
      visual_mismatch: !!(src.visual_mismatch || src.visualMismatch),
      visual_note: String(src.visual_note || src.visualNote || '').trim(),
      unidentified: skippedSlot ? false : !!(src.unidentified || src.frame_unidentified),
      skipped: skippedSlot,
      user_preview: String(src.user_preview || src.userPreview || '').trim(),
      from_image_index: src.from_image_index || src.fromImageIndex || null,
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
      if (!work.code && !work.title && !work.skipped) return;
      const idx = work.from_image_index;
      const key = idx
        ? 'i:' + idx
        : work.unidentified
          ? 'u:' + works.length
          : work.code
            ? String(work.code).toUpperCase()
            : 't:' + work.title;
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

  // One-character kinship chips from patterns like 彼女の妹. Other 1-char scraps stay out.
  const RELATION_CHIP = { '妹': 1, '姉': 1, '兄': 1, '弟': 1, '私': 1, '僕': 1, '俺': 1, '君': 1 };

  function foldEditionKey(s) {
    return String(s || '')
      .trim()
      .replace(/[Ａ-Ｚａ-ｚ]/g, (ch) => String.fromCharCode(ch.charCodeAt(0) - 0xfee0))
      .replace(/[\s.．·・‐－\-]+/g, '')
      .toLowerCase();
  }

  /** Disc/edition junk. Never a chip. OL and VR are not in this set.
   * A glued pair such as 交尾BOD is junk too. BD inside BDSM is not.
   */
  function isEditionKeyword(s) {
    const raw = String(s || '').trim();
    if (!raw) return false;
    if (/第\s*[0-9０-９一二三四五六七八九十百千〇零]+\s*[巻話章集回]/.test(raw)) return true;
    if (/blu[\s\-‐－]?ray/i.test(raw)) return true;
    if (/ブルーレイ/.test(raw)) return true;
    const key = foldEditionKey(raw);
    return /(?:^|[^a-z0-9])(?:vol(?:ume)?|ep(?:isode)?|bod|bd|dvd|uhd|bluray|4k)[0-9]*(?:[^a-z0-9]|$)/.test(key);
  }

  function keywordTokenOk(s) {
    if (!s || s.length > 24) return false;
    if (isEditionKeyword(s)) return false;
    if (s.length >= 2) return true;
    return !!RELATION_CHIP[s];
  }

  /**
   * Longer chip covers a shorter one: の-phrase sides (息子の家庭教師 covers
   * 家庭教師 and 息子; 義妹 does not cover 妹) or a glued prefix (巨乳沼 / 巨乳).
   */
  function chipContainsPart(compound, part) {
    compound = String(compound || '');
    part = String(part || '');
    if (!compound || !part || compound === part || part.length >= compound.length) return false;
    const bits = compound.split('の');
    if (bits.length === 2) return bits[0] === part || bits[1] === part;
    return compound.indexOf(part) === 0;
  }

  /** Put a compound just before its own shorter chips. Leave unrelated order. */
  function orderCompoundsBeforeParts(found) {
    const items = (found || []).slice();
    const compounds = items.filter((tok) => items.some((other) => chipContainsPart(tok, other)));
    compounds.sort((a, b) => b.length - a.length);
    compounds.forEach((comp) => {
      const parts = items.filter((other) => chipContainsPart(comp, other));
      if (!parts.length || items.indexOf(comp) < 0) return;
      const earliest = Math.min.apply(null, parts.map((part) => items.indexOf(part)));
      if (items.indexOf(comp) < earliest) return;
      items.splice(items.indexOf(comp), 1);
      const at = Math.min.apply(null, parts.map((part) => items.indexOf(part)));
      items.splice(at, 0, comp);
    });
    return items;
  }

  /** Unique chip labels from an API list. Does not invent keywords from a title.
   *  ノーブラ誘惑 is two chips, never one compound.
   */
  function normalizeKeywordList(raw) {
    const out = [];
    const seen = {};
    const items = Array.isArray(raw)
      ? raw
      : (typeof raw === 'string' ? raw.split(/[\s,，、・/|]+/) : []);
    items.forEach((item) => {
      const rawTok = String(item || '').trim();
      const pieces = rawTok === 'ノーブラ誘惑' ? ['ノーブラ', '誘惑'] : [rawTok];
      pieces.forEach((s) => {
        if (!keywordTokenOk(s) || seen[s]) return;
        seen[s] = true;
        out.push(s);
      });
    });
    return orderCompoundsBeforeParts(out).slice(0, 10);
  }

  /**
   * Traditional Chinese gloss for a keyword chip. Lexicon only — never a
   * guessed catalog title. Same-script words (巨乳, 誘惑) still get the
   * parenthetical the chip rule asks for.
   */
  const KEYWORD_GLOSS = {
    '巨乳': '巨乳',
    '美乳': '美乳',
    '爆乳': '爆乳',
    'ノーブラ': '無胸罩',
    '誘惑': '誘惑',
    '水泳部': '游泳社',
    '合宿': '集訓',
    '媚薬': '媚藥',
    '媚藥': '媚藥',
    '水着': '泳衣',
    'スク水': '學校泳衣',
    'スクール水着': '學校泳衣',
    '満員': '滿員',
    '滿員': '滿員',
    '電車': '電車',
    '痴漢': '痴漢',
    '癡漢': '痴漢',
    '彼女': '女朋友',
    '妹': '妹妹',
    '彼女の妹': '女朋友的妹妹',
    '姉': '姐姐',
    '兄': '哥哥',
    '弟': '弟弟',
    '息子': '兒子',
    'ママ': '媽媽',
    '家庭教師': '家教',
    '息子の家庭教師': '兒子的家教',
    '肉欲教育': '肉慾教育',
    '羞恥教育': '羞恥教育',
    '10秒挿入': '10秒插入',
    '眼鏡': '眼鏡',
    'メガネ': '眼鏡',
    '地味': '土味',
    '美人': '美人',
    'OL': 'OL',
    '中出し': '中出',
    '叔母': '叔母',
    'ナマ乳沼': '生乳沼',
    '巨乳沼': '巨乳沼',
    'オイル': '精油',
    '温泉': '溫泉',
    '人妻': '人妻',
    '痴女': '痴女',
    'パンスト': '絲襪',
    '夜行バス': '夜間巴士',
    '指マン': '指交',
    '声我慢': '忍住聲音',
    '羞恥': '羞恥',
    '美尻': '美臀',
    '乳首': '乳頭',
    '女教師': '女教師',
    'ナース': '護士',
    '女医': '女醫師',
    '秘書': '秘書',
    '童貞': '處男',
    '絶倫': '性能力強',
    '搾精': '榨精',
    '顔射': '顏射',
    '拘束': '拘束',
    '監禁': '監禁',
    '調教': '調教',
    '開発': '開發',
    '開發': '開發',
    'マッサージ': '按摩',
    'エステ': '美容',
    '寝取': '寢取',
    '義妹': '義妹',
    '義母': '義母',
    '義父': '義父',
    '義姉': '義姉',
    '義兄': '義兄',
    '下着': '內衣',
    '通勤': '通勤',
    '会社': '公司',
    'オフィス': '辦公室',
    '毎朝': '每天早上',
    '毎晩': '每天晚上',
    '交尾': '交尾',
    '逆NTR': '反向NTR',
    'CA': '空姐',
    'VR': 'VR',
    'SP': '特別篇',
  };

  function keywordGloss(tok) {
    const s = String(tok || '').trim();
    if (!s || !Object.prototype.hasOwnProperty.call(KEYWORD_GLOSS, s)) return '';
    return KEYWORD_GLOSS[s];
  }

  /** Chip label: 日文（中文）. Unmapped tokens stay as-is (no invented gloss). */
  function formatKeywordChip(tok) {
    const s = String(tok || '').trim();
    const zh = keywordGloss(s);
    if (!s) return '';
    if (!zh) return s;
    if (s.indexOf('（' + zh + '）') !== -1 || s.indexOf('(' + zh + ')') !== -1) return s;
    return s + '（' + zh + '）';
  }

  function formatKeywordListLabel(prefix, keywords) {
    const kws = normalizeKeywordList(keywords);
    if (!kws.length) return prefix;
    return prefix + '（' + kws.map(formatKeywordChip).join('・') + '）';
  }

  function cjkScript(ch) {
    const c = ch.charCodeAt(0);
    if ((c >= 0x30A0 && c <= 0x30FF) || (c >= 0xFF66 && c <= 0xFF9D)) return 'kata';
    if (c >= 0x3040 && c <= 0x309F) return 'hira';
    if (c >= 0x4E00 && c <= 0x9FFF) return 'han';
    if (/[A-Za-z0-9]/.test(ch)) return 'latin';
    return 'break';
  }

  /**
   * Split display text so a CJK token is one unbreakable span.
   * Keywords stay whole (夜行バス). Katakana+okurigana stays whole (イカされて).
   * Long Han runs (Chinese sentences) stay breakable. Does not insert ellipsis.
   */
  function segmentDisplayText(text, keywords) {
    const src = String(text || '');
    if (!src) return [];
    const covered = new Array(src.length).fill(false);
    const atoms = [];
    const kws = normalizeKeywordList(keywords).slice().sort((a, b) => b.length - a.length);
    kws.forEach((kw) => {
      if (!kw || kw.length < 2) return;
      let from = 0;
      while (from <= src.length - kw.length) {
        const at = src.indexOf(kw, from);
        if (at < 0) break;
        let free = true;
        for (let i = at; i < at + kw.length; i++) {
          if (covered[i]) {
            free = false;
            break;
          }
        }
        if (free) {
          for (let i = at; i < at + kw.length; i++) covered[i] = true;
          atoms.push({ start: at, end: at + kw.length });
        }
        from = at + kw.length;
      }
    });
    let i = 0;
    while (i < src.length) {
      if (covered[i]) {
        i += 1;
        continue;
      }
      const kind = cjkScript(src.charAt(i));
      if (kind === 'break') {
        i += 1;
        continue;
      }
      let j = i + 1;
      while (j < src.length && !covered[j] && cjkScript(src.charAt(j)) === kind) j += 1;
      if (
        kind === 'kata' &&
        j - i <= 6 &&
        j < src.length &&
        !covered[j] &&
        cjkScript(src.charAt(j)) === 'hira'
      ) {
        while (j < src.length && !covered[j] && cjkScript(src.charAt(j)) === 'hira') j += 1;
      }
      const len = j - i;
      const atomic = kind !== 'han' || len <= 8;
      if (atomic) {
        for (let k = i; k < j; k++) covered[k] = true;
        atoms.push({ start: i, end: j });
      }
      i = j;
    }
    atoms.sort((a, b) => a.start - b.start);
    const out = [];
    let cursor = 0;
    atoms.forEach((span) => {
      if (span.start > cursor) out.push({ text: src.slice(cursor, span.start), atom: false });
      out.push({ text: src.slice(span.start, span.end), atom: true });
      cursor = span.end;
    });
    if (cursor < src.length) out.push({ text: src.slice(cursor), atom: false });
    return out.filter((seg) => seg.text);
  }

  function setProtectedText(el, text, keywords) {
    if (!el) return;
    el.textContent = '';
    segmentDisplayText(text, keywords).forEach((seg) => {
      const span = document.createElement('span');
      if (seg.atom) span.className = 'cjk-atom';
      span.textContent = seg.text;
      el.appendChild(span);
    });
  }

  /** Gallery mains (single or each multi-image slot). Candidates and related slides do not. */
  function workShowsKeywordChips(work) {
    const line = String((work && work.line) || 'main');
    return line === 'main' || line === 'multi';
  }

  function copyKeywordFields(work, src) {
    if (!work || !src) return work;
    if (!work.themeKeywords || !work.themeKeywords.length) {
      const kws = normalizeKeywordList(src.theme_keywords || src.themeKeywords);
      if (kws.length) work.themeKeywords = kws;
    }
    if (!work.keywordQueries || !work.keywordQueries.length) {
      const qs = normalizeKeywordList(src.keyword_queries || src.keywordQueries);
      if (qs.length) work.keywordQueries = qs;
    }
    return work;
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

  /**
   * Actress / studio / series name. Chinese only when the catalog supplied it.
   * Kanji names still get the parenthetical. No name_zh → Japanese only.
   */
  function formatPersonName(nameJa, nameZh) {
    const ja = stripEmptyParens(nameJa);
    const zh = stripEmptyParens(nameZh);
    if (!ja) return zh || '';
    if (!zh || zh === ja) return ja;
    if (ja.indexOf('（' + zh + '）') !== -1 || ja.indexOf('(' + zh + ')') !== -1) return ja;
    return ja + '（' + zh + '）';
  }

  /** Clipboard string for 番號+名稱: CODE then newline then the on-screen title. */
  function formatCodeTitleClipboard(code, displayTitle) {
    const c = String(code || '').trim();
    const t = String(displayTitle || '').trim();
    if (c && t) return c + '\n' + t;
    return c || t;
  }

  function isRelatedCarouselLine(line) {
    return line === 'theme' || line === 'keyword' || line === 'actress';
  }

  function isCatalogMediaUrl(url) {
    const s = String(url || '').trim();
    return /^https?:\/\//i.test(s) && !isLocalCoverUrl(s) && !isNowPrintingUrl(s);
  }

  function workFromApi(raw, line) {
    const rawCode = raw.code == null ? '' : String(raw.code);
    const skipped = !!(raw.skipped);
    const unidentified = !skipped && !!(raw.unidentified || raw.frame_unidentified);
    const titleOnly =
      !skipped &&
      (unidentified ||
        !rawCode ||
        rawCode === 'TITLE-SEARCH' ||
        rawCode.toLowerCase() === 'null' ||
        !parseCodeParts(rawCode));
    const code = skipped
      ? ''
      : unidentified
        ? '未辨識'
        : titleOnly
          ? rawCode === 'TITLE-SEARCH' || !rawCode
            ? '片名搜尋'
            : formatDisplayCode(rawCode)
          : formatDisplayCode(rawCode);
    const lineOut = line || raw.line || 'main';
    const relatedSlide = isRelatedCarouselLine(lineOut);
    const preview = String(raw.user_preview || raw.userPreview || '').trim();
    // Never invent DMM CID from the display code: padded guesses (dosd00008)
    // often redirect to now_printing after the server already cleared cover.
    const cid = String(raw.cid || '');
    let cover = String(raw.cover || raw.cover_url || '').trim();
    if (isNowPrintingUrl(cover) || !isCatalogMediaUrl(cover) || (preview && cover === preview)) {
      cover = '';
    }
    // The query image is not the main cover. A cid restores the catalog jacket.
    if (!skipped && !cover && cid && !isNowPrintingUrl(cid)) {
      cover = coverUrl(cid);
      if (!isCatalogMediaUrl(cover)) cover = '';
    }
    if (skipped) cover = '';
    let stills = Array.isArray(raw.stills) && raw.stills.length
      ? raw.stills.map((u) => String(u || '')).filter((u) => isCatalogMediaUrl(u) && u !== preview)
      : [];
    if (skipped) {
      stills = [];
    } else if (!stills.length && cid && !isNowPrintingUrl(cid)) {
      stills = stillUrls(cid, 10);
    }
    let relatedByTitle = [];
    if (!relatedSlide) {
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
      actressZh: String(raw.actress_zh || raw.actressZh || '').trim(),
      studio: raw.studio ? String(raw.studio) : '',
      studioZh: String(raw.studio_zh || raw.studioZh || '').trim(),
      cid,
      line: lineOut,
      why: raw.why ? String(raw.why) : '',
      cover,
      stills,
      titleOnly,
      unidentified,
      skipped,
      userPreview: relatedSlide ? '' : String(raw.user_preview || raw.userPreview || '').trim(),
      fromImageIndex: raw.from_image_index || raw.fromImageIndex || null,
      relatedByTitle,
      themeKeywords: normalizeKeywordList(raw.theme_keywords || raw.themeKeywords),
      keywordQueries: normalizeKeywordList(raw.keyword_queries || raw.keywordQueries),
      matchedKeywords: normalizeKeywordList(
        raw.matched_keywords || raw.matchedKeywords || raw.hit_keywords || raw.hitKeywords
      ),
      visualMismatch: !!(raw.visual_mismatch || raw.visualMismatch),
      visualNote: String(raw.visual_note || raw.visualNote || '').trim(),
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
  let activeIdentifyJobId = '';
  let identifyImageTotal = 0;
  let slotUploadFiles = [];
  let lastIdentifyPayload = null;
  let historySavePromise = null;
  let skipRequestBusy = false;

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

  function phaseLabelForEvent(evt) {
    if (!evt || evt.status !== 'active') return '';
    if (evt.phase) return String(evt.phase);
    const step = evt.step;
    const detail = String(evt.detail || '');
    if (step === 'vision') return '辨識中';
    if (step === 'search') return '目錄查詢';
    if (step === 'verify' || step === 'cover') return '封面鎖定';
    if (step === 'done' && detail.indexOf('相關') !== -1) return '相關作品';
    return '';
  }

  function listStepRows(root) {
    if (!root) return [];
    if (root.querySelectorAll) {
      const found = root.querySelectorAll('.progress-step');
      if (found && found.length) return Array.from(found);
    }
    return Array.from(root.children || []).filter((n) => {
      if (!n) return false;
      if (n.dataset && n.dataset.step) return true;
      return String(n.className || '').indexOf('progress-step') !== -1;
    });
  }

  function findStepRow(root, stepId) {
    if (!root) return null;
    if (root.querySelector) {
      const hit = root.querySelector('[data-step="' + stepId + '"]');
      if (hit) return hit;
    }
    const rows = listStepRows(root);
    for (let i = 0; i < rows.length; i++) {
      if (rows[i] && rows[i].dataset && rows[i].dataset.step === stepId) return rows[i];
    }
    return null;
  }

  function setStepPhase(li, phase) {
    if (!li) return;
    const text = phase ? String(phase) : '';
    let el = li.querySelector && li.querySelector('.step-phase');
    if (!el && li.children) {
      el = Array.from(li.children).find((n) => {
        if (!n) return false;
        if (n.classList && n.classList.contains('step-phase')) return true;
        return String(n.className || '').indexOf('step-phase') !== -1;
      }) || null;
    }
    if (!el && text && document && document.createElement) {
      el = document.createElement('span');
      el.className = 'step-phase';
      li.appendChild(el);
    }
    if (el) {
      el.hidden = !text;
      el.textContent = text;
    }
    if (li.dataset) {
      if (text) li.dataset.phase = text;
      else delete li.dataset.phase;
    }
  }

  function setSkipSlotVisible(on) {
    const show = !!on;
    if (progressSkipRow) progressSkipRow.hidden = !show;
    if (btnSkipSlot) {
      btnSkipSlot.hidden = !show;
      if (!show) btnSkipSlot.disabled = false;
    }
  }

  function hideProgress() {
    if (!progressPanel) return;
    progressPanel.classList.add('hidden');
    progressPanel.classList.remove('is-complete', 'is-failed');
    progressPanel.setAttribute('aria-busy', 'false');
    setProgressCollapsed(false);
    progressFinished = false;
    activeIdentifyJobId = '';
    setSkipSlotVisible(false);
  }

  // High-water mark for one identify run. SSE and job polls can arrive out of
  // order; a later slot must not jump back to 搜尋第 3/4 or an earlier percent.
  // The denominator is part of that mark: 搜尋第 2/4 is 61% and 搜尋第 2/7 is
  // 59%, so a stale 4-image poll looks "ahead" of the live 7-image run unless
  // N itself is refused.
  let progressEpoch = 0;
  let activeIdentifyReader = null;
  let progressHigh = {
    jobId: '',
    started: false,
    stepIndex: -1,
    percent: -1,
    imageIndex: null,
    imageTotal: null,
    phaseRank: 0,
    runImageCount: 0,
    epoch: 0,
  };

  function resetProgressHigh(jobId) {
    progressHigh = {
      jobId: jobId == null ? '' : String(jobId),
      started: false,
      stepIndex: -1,
      percent: -1,
      imageIndex: null,
      imageTotal: null,
      phaseRank: 0,
      runImageCount: 0,
      epoch: 0,
    };
  }

  function releaseIdentifyReader() {
    const reader = activeIdentifyReader;
    activeIdentifyReader = null;
    if (!reader) return;
    try {
      const cancel = reader.cancel();
      if (cancel && typeof cancel.catch === 'function') cancel.catch(function () {});
    } catch (_) {}
  }

  // A new upload starts a new run. Slot, percent, and denominator high-water
  // reset. The previous stream and its job poll stop painting this panel.
  function beginIdentifyProgress(imageCount) {
    progressEpoch += 1;
    releaseIdentifyReader();
    const count = parseInt(imageCount, 10);
    resetProgressHigh('');
    progressHigh.epoch = progressEpoch;
    progressHigh.runImageCount = count > 0 ? count : 0;
  }

  function bindIdentifyJob(jobId) {
    const id = String(jobId || '');
    if (!id) return;
    if (!progressHigh.jobId) progressHigh.jobId = id;
    else if (progressHigh.jobId !== id) {
      const epoch = progressHigh.epoch || 0;
      const runImageCount = progressHigh.runImageCount || 0;
      resetProgressHigh(id);
      progressHigh.epoch = epoch;
      progressHigh.runImageCount = runImageCount;
    }
  }

  function noteProgressImageCount(imageCount) {
    const count = parseInt(imageCount, 10);
    if (!(count > 0)) return;
    progressHigh.runImageCount = Math.max(progressHigh.runImageCount || 0, count);
  }

  function progressStepIndex(stepId) {
    const ids = listStepRows(progressStepsEl).map((n) => (n.dataset && n.dataset.step) || '');
    let idx = ids.indexOf(stepId);
    if (idx < 0) idx = DEFAULT_STEPS.findIndex((s) => s.id === stepId);
    return idx;
  }

  function imageProgressFromDetail(detail) {
    const text = String(detail || '');
    const match = text.match(/(?:搜尋第|封面鎖定第|辨識第|相關作品|第)\s*(\d+)\s*\/\s*(\d+)/);
    if (!match) return null;
    const slot = parseInt(match[1], 10);
    const total = parseInt(match[2], 10);
    if (!(slot > 0) || !(total > 0)) return null;
    // 「相關作品 1/4」counts merged works after search. It is not the upload count.
    const relatedOnly = text.indexOf('相關作品') !== -1 && !/(?:搜尋第|封面鎖定第|辨識第)/.test(text);
    return { slot: slot, total: relatedOnly ? null : total };
  }

  function imageSlotFromDetail(detail) {
    const parsed = imageProgressFromDetail(detail);
    return parsed ? parsed.slot : null;
  }

  function imageCountFromSteps(steps) {
    const list = Array.isArray(steps) ? steps : [];
    for (let i = 0; i < list.length; i++) {
      const label = String((list[i] && list[i].label) || '');
      const match = label.match(/（\s*(\d+)\s*張）/);
      if (!match) continue;
      const n = parseInt(match[1], 10);
      if (n > 0) return n;
    }
    return 0;
  }

  function phaseRankOf(evt) {
    let name = (evt && evt.phase) || '';
    if (!name) name = phaseLabelForEvent(evt) || '';
    if (!name) {
      const detail = String((evt && evt.detail) || '');
      if (detail.indexOf('封面鎖定') !== -1) name = '封面鎖定';
      else if (detail.indexOf('搜尋第') !== -1 || detail.indexOf('目錄') !== -1) name = '目錄查詢';
      else if (detail.indexOf('相關') !== -1) name = '相關作品';
      else if (detail.indexOf('辨識') !== -1) name = '辨識中';
    }
    const order = ['辨識中', '目錄查詢', '封面鎖定', '相關作品'];
    const idx = order.indexOf(String(name));
    return idx < 0 ? 0 : idx + 1;
  }

  function progressRunSuperseded(epoch, jobId) {
    if ((progressHigh.epoch || 0) !== epoch) return true;
    if (jobId && progressHigh.jobId && progressHigh.jobId !== jobId) return true;
    return false;
  }

  function progressEventWouldRewind(evt) {
    if (!evt) return false;
    if (evt.jobId && progressHigh.jobId && String(evt.jobId) !== String(progressHigh.jobId)) return true;
    const parsed = imageProgressFromDetail(evt.detail);
    const eventTotal = parsed ? parsed.total : null;
    const runCount = progressHigh.runImageCount || 0;
    // A 4-image snapshot must not paint over a 7-image run, even when its
    // percent is higher (搜尋第 2/4 is 61%, 搜尋第 2/7 is 59%).
    if (eventTotal != null && runCount > 0 && eventTotal < runCount) return true;
    if (eventTotal != null && progressHigh.imageTotal != null && eventTotal < progressHigh.imageTotal) return true;
    if (!progressHigh.started) return false;
    const stepIndex = progressStepIndex(evt.step);
    const percent = typeof evt.progress === 'number' && !Number.isNaN(evt.progress) ? evt.progress : null;
    const imageIndex = parsed ? parsed.slot : null;
    const phase = phaseRankOf(evt);
    if (stepIndex >= 0 && progressHigh.stepIndex >= 0 && stepIndex < progressHigh.stepIndex) return true;
    if (percent != null && progressHigh.percent >= 0 && percent + 1e-6 < progressHigh.percent) return true;
    if (stepIndex !== progressHigh.stepIndex) return false;
    if (
      imageIndex != null &&
      progressHigh.imageIndex != null &&
      imageIndex < progressHigh.imageIndex
    ) {
      return true;
    }
    if (
      imageIndex != null &&
      progressHigh.imageIndex != null &&
      imageIndex === progressHigh.imageIndex &&
      phase &&
      progressHigh.phaseRank &&
      phase < progressHigh.phaseRank
    ) {
      return true;
    }
    return false;
  }

  function commitProgressHigh(evt) {
    const stepIndex = progressStepIndex(evt.step);
    const percent = typeof evt.progress === 'number' && !Number.isNaN(evt.progress) ? evt.progress : null;
    const parsed = imageProgressFromDetail(evt.detail);
    const imageIndex = parsed ? parsed.slot : null;
    const imageTotal = parsed ? parsed.total : null;
    const phase = phaseRankOf(evt);
    const stepUp = stepIndex > progressHigh.stepIndex;
    if (stepIndex >= 0) progressHigh.stepIndex = Math.max(progressHigh.stepIndex, stepIndex);
    if (percent != null) progressHigh.percent = Math.max(progressHigh.percent, percent);
    if (imageTotal != null) {
      progressHigh.imageTotal =
        progressHigh.imageTotal == null ? imageTotal : Math.max(progressHigh.imageTotal, imageTotal);
    }
    if (stepUp) {
      progressHigh.imageIndex = imageIndex;
      progressHigh.phaseRank = phase;
    } else if (imageIndex != null) {
      if (progressHigh.imageIndex == null || imageIndex > progressHigh.imageIndex) {
        progressHigh.imageIndex = imageIndex;
        progressHigh.phaseRank = phase;
      } else if (imageIndex === progressHigh.imageIndex) {
        progressHigh.phaseRank = Math.max(progressHigh.phaseRank, phase);
      }
    }
    progressHigh.started = true;
  }

  function showProgress(steps, imageCount) {
    if (!progressPanel) return;
    const keepJob = progressHigh.jobId;
    const keepEpoch = progressHigh.epoch || 0;
    resetProgressHigh(keepJob);
    progressHigh.epoch = keepEpoch;
    const passed = parseInt(imageCount, 10);
    const fromSteps = imageCountFromSteps(steps);
    progressHigh.runImageCount = passed > 0 ? passed : fromSteps > 0 ? fromSteps : 0;
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
      li.innerHTML =
        '<span class="step-mark" aria-hidden="true"></span>' +
        '<span class="step-label">' + escapeHtml(s.label) + '</span>' +
        '<span class="step-phase" hidden></span>';
      progressStepsEl.appendChild(li);
    });
    progressDetailEl.textContent = '';
    progressBar.style.width = '0%';
    progressPct.textContent = '0%';
    updateProgressSummary('處理中：準備中…');
    setProgressCollapsed(false);
    setSkipSlotVisible(false);
    progressPanel.classList.remove('hidden');
    progressPanel.setAttribute('aria-busy', 'true');
  }

  function applyProgressEvent(evt) {
    if (!evt || !evt.step) return;
    if (progressEventWouldRewind(evt)) return;
    const step = evt.step;
    const status = evt.status || 'active';
    progressState[step] = status;
    const li = findStepRow(progressStepsEl, step);
    if (li) {
      li.className = 'progress-step is-' + status;
      setStepPhase(li, phaseLabelForEvent(evt));
    }
    if (status === 'active') {
      listStepRows(progressStepsEl).forEach((row) => {
        if (row !== li) setStepPhase(row, '');
      });
    }
    if (status === 'active' || status === 'done') {
      const ids = listStepRows(progressStepsEl).map((n) => n.dataset.step);
      const idx = ids.indexOf(step);
      for (let i = 0; i < idx; i++) {
        if (progressState[ids[i]] === 'pending') {
          progressState[ids[i]] = 'done';
          const prev = findStepRow(progressStepsEl, ids[i]);
          if (prev) {
            prev.className = 'progress-step is-done';
            setStepPhase(prev, '');
          }
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
    commitProgressHigh(evt);
    const skipUi = skipControlState(evt, identifyImageTotal, activeIdentifyJobId);
    setSkipSlotVisible(!progressFinished && skipUi.visible);
  }

  async function requestSkipCurrentSlot() {
    if (!activeIdentifyJobId || skipRequestBusy || !btnSkipSlot) return;
    skipRequestBusy = true;
    btnSkipSlot.disabled = true;
    const previous = btnSkipSlot.textContent;
    btnSkipSlot.textContent = '跳過中…';
    try {
      const res = await fetch(
        '/api/identify/jobs/' + encodeURIComponent(activeIdentifyJobId) + '/skip',
        { method: 'POST' }
      );
      let data = null;
      try {
        data = await res.json();
      } catch (_) {
        data = null;
      }
      if (!data || data.ok === false) {
        showToast((data && data.message) || '目前無法跳過這張');
        return;
      }
      const total = Number(data.image_count) || identifyImageTotal;
      const index = Number(data.skipped_index) || 0;
      if (index && total) {
        const line = '已跳過第 ' + index + '/' + total + ' 張';
        if (progressDetailEl) progressDetailEl.textContent = line;
        updateProgressSummary('處理中：' + line);
      }
    } catch (_) {
      showToast('目前無法跳過這張');
    } finally {
      skipRequestBusy = false;
      if (btnSkipSlot) {
        btnSkipSlot.disabled = false;
        btnSkipSlot.textContent = previous || '跳過這張';
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

  function resumePayloadFromJob(job) {
    if (!job || (job.status !== 'done' && job.status !== 'error')) return null;
    const data = job.result;
    if (!data || typeof data !== 'object') return null;
    return data;
  }

  // Four images × 600s, plus a minute so the last slot can be written.
  const IDENTIFY_JOB_FOLLOW_MS = 4 * 600 * 1000 + 60 * 1000;

  async function followIdentifyJob(jobId, onProgress) {
    const id = String(jobId || '');
    const epoch = progressHigh.epoch || 0;
    if (progressRunSuperseded(epoch, '')) {
      const err = new Error('superseded');
      err.superseded = true;
      throw err;
    }
    bindIdentifyJob(id);
    const deadline = Date.now() + IDENTIFY_JOB_FOLLOW_MS;
    let wait = 400;
    while (Date.now() < deadline) {
      await sleep(wait);
      wait = Math.min(5000, wait + 400);
      if (progressRunSuperseded(epoch, id)) {
        const err = new Error('superseded');
        err.superseded = true;
        throw err;
      }
      let body = null;
      try {
        const res = await fetch('/api/identify/jobs/' + encodeURIComponent(id), { cache: 'no-store' });
        try {
          body = await res.json();
        } catch (_) {
          body = null;
        }
      } catch (_) {
        continue;
      }
      if (progressRunSuperseded(epoch, id)) {
        const err = new Error('superseded');
        err.superseded = true;
        throw err;
      }
      const job = body && body.job;
      if (!job) continue;
      if (job.progress && onProgress) {
        const stamped = Object.assign({ jobId: id }, job.progress);
        if (!progressEventWouldRewind(stamped)) onProgress(stamped);
      }
      const ready = resumePayloadFromJob(job);
      if (ready) return ready;
      // A late heartbeat can look stalled. Keep the real result; do not
      // paint 時間不夠 / 尚未查完 / 尚未鎖定 while the job can still finish.
    }
    const err = new Error('查詢還在伺服器上，請稍後再開');
    err.jobId = jobId;
    err.followed = true;
    throw err;
  }

  async function apiIdentifyStream({ images, image, code, title } = {}, onProgress, options) {
    options = options || {};
    const fd = new FormData();
    const imgs = images && images.length ? images : image ? [image] : [];
    appendImagesToFormData(fd, imgs);
    if (code) fd.append('code', code);
    if (title) fd.append('title', title);
    if (options.slotIndex) fd.append('slot_index', String(options.slotIndex));
    if (options.sessionId) fd.append('session_id', String(options.sessionId));

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
    activeIdentifyReader = reader;
    const epoch = progressHigh.epoch || 0;
    const decoder = new TextDecoder();
    let buffer = '';
    let finalData = null;
    let httpStatus = res.status;
    let jobId = '';

    try {
    while (true) {
      if (progressRunSuperseded(epoch, jobId)) {
        return { status: 0, data: null, superseded: true };
      }
      const { done, value } = await reader.read();
      if (progressRunSuperseded(epoch, jobId)) {
        return { status: 0, data: null, superseded: true };
      }
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
        if (evt.type === 'job' && evt.job_id) {
          if (!options.quiet) {
            jobId = String(evt.job_id);
            bindIdentifyJob(jobId);
            if (evt.image_count) noteProgressImageCount(evt.image_count);
            activeIdentifyJobId = jobId;
          }
        } else if (evt.type === 'steps' && Array.isArray(evt.steps)) {
          if (!options.quiet) showProgress(evt.steps, progressHigh.runImageCount || undefined);
        } else if (evt.type === 'progress') {
          if (!options.quiet) {
            if (progressRunSuperseded(epoch, jobId)) {
              return { status: 0, data: null, superseded: true };
            }
            if (jobId) evt.jobId = jobId;
            if (onProgress) onProgress(evt);
            else applyProgressEvent(evt);
          }
        } else if (evt.type === 'result') {
          finalData = evt.data;
          if (typeof evt.status === 'number') httpStatus = evt.status;
        }
      }
    }
    } finally {
      if (activeIdentifyReader === reader) activeIdentifyReader = null;
    }
    if (progressRunSuperseded(epoch, jobId)) {
      return { status: 0, data: null, superseded: true };
    }
    if (!finalData && jobId) {
      try {
        finalData = await followIdentifyJob(jobId, onProgress);
      } catch (e) {
        if (e && !e.jobId) e.jobId = jobId;
        throw e;
      }
    }
    if (!finalData) {
      const err = new Error('stream_incomplete');
      if (jobId) err.jobId = jobId;
      throw err;
    }
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

  /**
   * The same-actress miss note is only honest when that bucket is empty.
   * 「僅顯示主作品」cannot sit on a carousel that already has related cards.
   * Other clauses (視覺鎖定, 片名找到 N 個番號) stay.
   */
  function honestRelatedNotice(notice, items) {
    const text = String(notice || '').trim();
    if (!text) return null;
    const related = [];
    (items || []).forEach((w) => {
      const rel = (w && (w.relatedByTitle || w.related_by_title)) || [];
      rel.forEach((r) => related.push(r));
    });
    const hasActress = related.some((r) => relatedLineFromRaw(r) === 'actress');
    const hasAny = related.length > 0;
    if (!hasActress && !hasAny) return text;
    const kept = [];
    text.split(/[；;]/).forEach((part) => {
      let p = String(part || '').trim();
      if (!p) return;
      // Strip inside the clause so a title banner glued with 「—」 survives.
      if (hasActress) {
        p = p.replace(/線上目錄未取得同女優相關/g, '');
        p = p.replace(/無法取得線上相關/g, '');
      }
      if (hasActress || hasAny) {
        p = p.replace(/僅顯示主作品(?:\s*CDN)?/g, '');
      }
      if (hasAny && !hasActress) {
        p = p.replace(/無法取得線上相關/g, '');
      }
      p = p.replace(/^[—\-\s。．.]+|[—\-\s。．.]+$/g, '').replace(/\s{2,}/g, ' ').trim();
      if (!p || kept.indexOf(p) !== -1) return;
      kept.push(p);
    });
    return kept.join('；') || null;
  }

  function noticeForGallery(data, items, notice) {
    const cleaned = honestRelatedNotice(notice, items);
    if (cleaned) return cleaned;
    const msg = data && data.message ? String(data.message) : '';
    if (msg && msg !== String(notice || '')) return honestRelatedNotice(msg, items);
    return null;
  }

  /**
   * Put a single-image identify result back into one multi-batch slot.
   * Other slots, including their related lists, stay the same objects.
   */
  function mergeSlotRetryIntoIdentify(data, slotIndex, single) {
    const base = data && typeof data === 'object' ? data : {};
    const results = Array.isArray(base.results) ? base.results.slice() : [];
    const index = Number(slotIndex);
    let at = results.findIndex((r) => r && Number(r.from_image_index) === index);
    if (at < 0) at = index - 1;
    if (at < 0 || at >= results.length) return base;
    const prev = results[at] || {};
    const src =
      single && Array.isArray(single.results) && single.results[0] ? single.results[0] : single || {};
    const next = Object.assign({}, src);
    next.from_image_index = prev.from_image_index || index;
    next.line = at === 0 ? 'main' : 'multi';
    next.skipped = false;
    next.ok = src.ok !== false;
    if (!next.user_preview && prev.user_preview) next.user_preview = prev.user_preview;
    const preview = String(next.user_preview || prev.user_preview || '');
    const cover = String(next.cover || '');
    if (!cover || cover === preview || /^(data:|blob:)/i.test(cover)) {
      next.cover = null;
    }
    results[at] = next;
    const out = Object.assign({}, base, { results: results });
    if (at === 0) {
      out.code = next.code || '';
      out.title = next.title || '';
      out.cover = next.cover || '';
      out.related_by_title = next.related_by_title || [];
    }
    return out;
  }

  function replaceHistorySessionWorks(recId, works) {
    if (!recId) return false;
    const list = loadHistory();
    const i = list.findIndex((x) => x && x.id === recId);
    if (i < 0) return false;
    const prev = list[i];
    const first = (works && works[0]) || {};
    list[i] = Object.assign({}, prev, {
      works: works,
      code: first.code || '',
      title: first.title || '',
      title_zh: first.title_zh || '',
      cover: first.cover || '',
      stills: first.stills || [],
      actress: first.actress || '',
      related: first.related || [],
    });
    saveHistory(list);
    return true;
  }

  /** Show 跳過這張 only for the active vision/search slot, with the original N. */
  function skipControlState(evt, imageTotal, jobId) {
    const total = Number(imageTotal) || 0;
    const hidden = { visible: false, index: 0, total: total };
    if (!jobId || total < 2 || !evt || evt.status !== 'active') return hidden;
    if (evt.step !== 'vision' && evt.step !== 'search') return hidden;
    const detail = String(evt.detail || '');
    if (/時間不夠|尚未查完|尚未鎖定/.test(detail)) return hidden;
    const matched = detail.match(/第\s*(\d+)\s*\/\s*(\d+)\s*張/);
    const index = matched ? Number(matched[1]) : Number(evt.image_index) || 0;
    const denom = matched ? Number(matched[2]) : Number(evt.image_count) || 0;
    if (!index || denom !== total) return hidden;
    return { visible: true, index: index, total: denom };
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
      items.forEach((w, i) => {
        copyKeywordFields(w, (data.results && data.results[i]) || null);
        if (i === 0) copyKeywordFields(w, data);
      });
      let notice = data.related_note || data.message || null;
      return { items: items, notice: noticeForGallery(data, items, notice), source: 'api' };
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
      notice: noticeForGallery(data, items, notice),
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
    identifyBusy = false;
    batchFiles = [];
    slotUploadFiles = [];
    lastIdentifyPayload = null;
    historySavePromise = null;
    identifyImageTotal = 0;
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
    const card = wrap && wrap.closest ? wrap.closest('.card') : wrap && wrap.parentElement;
    const w = wrap && wrap._lfpWork;
    if (card && w && !card.querySelector('.manual-fix')) {
      card.appendChild(buildManualFixPanel(Object.assign({}, w, { cover: '' }), card));
    }
  }

  function appendCover(coverWrap, w) {
    coverWrap._lfpWork = w;
    const url = (w.cover || '').trim();
    const preview = String((w && w.userPreview) || '').trim();
    // Main cover is the catalog jacket. The query image stays in the upload strip.
    const catalog = isCatalogMediaUrl(url) && url !== preview;
    if (w.skipped || !catalog) {
      coverWrap.classList.add('is-empty');
      if (w.skipped) coverWrap.classList.add('is-skipped');
      const ph = document.createElement('div');
      ph.className = 'cover-placeholder';
      if (w.skipped && preview && /^(data:|blob:)/i.test(preview)) {
        coverWrap.classList.add('has-upload-preview');
        const shot = document.createElement('img');
        shot.className = 'skipped-upload-preview';
        shot.alt = '已跳過的上傳圖';
        shot.src = preview;
        coverWrap.appendChild(shot);
      }
      ph.innerHTML = w.skipped
        ? '<strong>已跳過</strong><span>這張先略過，可按重新辨識</span>'
        : w.unidentified
          ? '<strong>未辨識</strong><span>未讀到番號或片名，已保留這張</span>'
          : w.titleOnly
            ? '<strong>尚未解析番號</strong><span>無法載入 CDN 封面 — 請手動輸入番號</span>'
            : '<strong>暫無封面</strong><span>請確認番號</span>';
      coverWrap.appendChild(ph);
      return;
    }
    const coverImg = document.createElement('img');
    coverImg.alt = w.code + ' 封面';
    coverImg.loading = 'lazy';
    coverImg.decoding = 'async';
    // Display: the browser loads pics.dmm.co.jp directly (no CORS).
    // Share/download: fetch('/api/cdn-file') on the Railway server.
    // JUFE-271 can paint here while the proxy is blocked — do not require
    // the proxy for on-screen cover. When the proxy lands, swap src to a
    // same-origin blob URL so share does not need a tainted canvas.
    coverImg.referrerPolicy = 'no-referrer';
    coverImg.src = url;
    coverImg.addEventListener('error', () => onImgError(coverImg));
    warmCoverFromProxy(coverImg, url);
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
      if (!isCatalogMediaUrl(url)) return;
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

  function relatedHeadingForList(relatedList, keywords) {
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
    if (hasKeyword) {
      const kws = normalizeKeywordList(keywords);
      parts.push(kws.length ? formatKeywordListLabel('關鍵字相關', kws) : '關鍵字');
    }
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
    const keywords = w.themeKeywords || w.theme_keywords;
    const codeEl = document.createElement('p');
    codeEl.className = 'card-code';
    codeEl.textContent = w.code || '';
    meta.appendChild(codeEl);
    const titleEl = document.createElement('p');
    titleEl.className = 'card-title';
    setProtectedText(titleEl, formatDisplayTitle(w.title, w.titleZh), keywords);
    meta.appendChild(titleEl);
    if (w.actress || w.studio) {
      const actressEl = document.createElement('p');
      actressEl.className = 'card-actress';
      const act = formatPersonName(w.actress, w.actressZh || w.actress_zh);
      const stu = formatPersonName(w.studio, w.studioZh || w.studio_zh);
      actressEl.textContent = act
        ? '女優：' + act + (stu ? ' · ' + stu : '')
        : stu;
      meta.appendChild(actressEl);
    }
    if (w.visualMismatch) {
      const noteEl = document.createElement('p');
      noteEl.className = 'card-visual-note';
      setProtectedText(
        noteEl,
        w.visualNote || '未核對圖片（人物／衣服／姿勢與這張上傳圖不符）',
        keywords
      );
      meta.appendChild(noteEl);
    }
    const badge = w.skipped ? '已跳過' : (opts.badgeLabel || lineLabel(w.line));
    const hitKeywords =
      badge === '關鍵字'
        ? normalizeKeywordList(w.matchedKeywords || w.matched_keywords || w.hitKeywords || w.hit_keywords)
        : [];
    const badgeRow = document.createElement('span');
    badgeRow.className = 'card-badge-row';
    const badgeEl = document.createElement('span');
    badgeEl.className = w.line === 'multi' ? 'card-line line-multi' : 'card-line';
    badgeEl.textContent = badge;
    badgeRow.appendChild(badgeEl);
    if (hitKeywords.length) {
      const hitWrap = document.createElement('span');
      hitWrap.className = 'card-hit-keywords';
      hitKeywords.forEach((kw) => {
        const hit = document.createElement('span');
        hit.className = 'card-hit-kw';
        hit.textContent = formatKeywordChip(kw);
        hitWrap.appendChild(hit);
      });
      badgeRow.appendChild(hitWrap);
    }
    meta.appendChild(badgeRow);

    const coverWrap = document.createElement('div');
    coverWrap.className = 'cover-wrap';
    appendCover(coverWrap, w);

    card.appendChild(meta);
    if (!w.skipped) card.appendChild(buildWorkActions(w, coverWrap));
    card.appendChild(coverWrap);
    if (w.skipped) card.appendChild(buildReidentifyButton(w));
    if (workNeedsManualFix(w)) {
      card.appendChild(buildManualFixPanel(w, card));
    }
    if (!w.skipped) appendStillsScroll(card, w, '劇照（橫滑）');
    if (w.skipped) card.classList.add('is-skipped');
    return card;
  }

  function buildReidentifyButton(w) {
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'btn btn-reidentify';
    retry.textContent = '重新辨識';
    bindWorkAction(retry, () => {
      if (retry.disabled) return;
      retry.disabled = true;
      retry.textContent = '重新辨識中…';
      reidentifySkippedSlot(w).finally(() => {
        if (!retry.isConnected && retry.parentNode == null) return;
        retry.disabled = false;
        retry.textContent = '重新辨識';
      });
    });
    return retry;
  }

  let manualFixFileCb = null;

  function pickManualFixFile() {
    const input = $('file-manual-fix');
    if (!input) return Promise.resolve(null);
    return new Promise((resolve) => {
      manualFixFileCb = resolve;
      try {
        input.click();
      } catch (_) {
        manualFixFileCb = null;
        resolve(null);
      }
    });
  }

  function patchGallerySessionWork(oldW, nextW) {
    lastGalleryItems = (lastGalleryItems || []).map((item) => {
      if (!item) return item;
      if (
        item === oldW ||
        (oldW &&
          item.code &&
          oldW.code &&
          codesMatch(item.code, oldW.code) &&
          (item.line || 'main') === (oldW.line || 'main'))
      ) {
        return nextW;
      }
      if (Array.isArray(item.relatedByTitle)) {
        let changed = false;
        const rel = item.relatedByTitle.map((r) => {
          if (
            r === oldW ||
            (oldW && r && oldW.code && r.code && codesMatch(oldW.code, r.code))
          ) {
            changed = true;
            return nextW;
          }
          return r;
        });
        if (changed) return Object.assign({}, item, { relatedByTitle: rel });
      }
      return item;
    });
  }

  function replaceWorkOnScreen(card, oldW, nextW) {
    patchGallerySessionWork(oldW, nextW);
    if (viewingHistoryId) {
      const rec = loadHistory().find((x) => x.id === viewingHistoryId);
      if (rec) {
        paintHistoryDetail(rec);
        return;
      }
    }
    if (!card || !card.parentElement) return;
    const slide = card.parentElement;
    const block =
      card.closest && typeof card.closest === 'function'
        ? card.closest('.work-carousel-block')
        : null;
    const firstSlide = block && block.querySelector && block.querySelector('.work-carousel-slide');
    const isMain = !!(block && firstSlide && (firstSlide === slide || firstSlide.contains(card)));
    if (block && isMain && block.parentElement) {
      block.replaceWith(buildWorkCarousel(nextW));
      return;
    }
    const badge = lineLabel(nextW.line);
    const nextCard = buildWorkCard(nextW, { slide: !!slide && slide.classList && slide.classList.contains('work-carousel-slide'), badgeLabel: badge });
    card.replaceWith(nextCard);
  }

  async function applyManualWorkFix(oldW, fields, card) {
    const codeRaw = String((fields && fields.code) || '').trim();
    const titleRaw = String((fields && fields.title) || '').trim();
    const file = fields && fields.file;
    const code = parseCodeParts(codeRaw) ? formatDisplayCode(codeRaw) : '';
    const titleLooksCode = !code && AV_CODE_INPUT_RE.test(titleRaw);
    const sendCode = code || (titleLooksCode ? formatDisplayCode(titleRaw) : '');
    const sendTitle = titleLooksCode ? '' : titleRaw;
    if (!sendCode && !sendTitle && !file) {
      showToast('請輸入番號／片名或上傳圖片');
      return { ok: false, reason: 'empty' };
    }

    let localCover = '';
    if (file) {
      try {
        localCover =
          (await downscaleFileToDataUrl(file, 280 * 1024, 720)) ||
          (await smallFileDataUrl(file, 280 * 1024)) ||
          '';
      } catch (_) {
        localCover = '';
      }
    }

    let data = null;
    if (sendCode || sendTitle || file) {
      try {
        const res = await apiIdentify({
          images: file ? [file] : [],
          code: sendCode || '',
          title: sendTitle || '',
        });
        data = res && res.data;
      } catch (err) {
        data = null;
        if (!file) {
          showToast((err && err.message) || '修正失敗');
          return { ok: false, reason: 'fetch' };
        }
      }
    }

    const identified =
      data &&
      data.ok &&
      (parseCodeParts(String(data.code || '')) ||
        (data.title && String(data.title).trim()) ||
        (Array.isArray(data.results) && data.results.length));
    if (!identified && !localCover) {
      showToast((data && data.message) || '找不到作品，請再試番號或上傳封面');
      return { ok: false, reason: 'miss' };
    }

    const source = identified
      ? Array.isArray(data.results) && data.results[0]
        ? data.results[0]
        : data
      : null;
    const nextW = mergeManualFixIntoWork(oldW, source, localCover);
    persistManualWorkFix(oldW, nextW, {});
    replaceWorkOnScreen(card, oldW, nextW);
    showToast(workNeedsManualFix(nextW) ? '已套用，仍可再補資料' : '已更新');
    return { ok: true, work: nextW };
  }

  function buildManualFixPanel(w, card) {
    const box = document.createElement('div');
    box.className = 'manual-fix';
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'manual-fix-toggle';
    toggle.textContent = '手動修正';
    toggle.setAttribute('aria-expanded', 'false');
    const form = document.createElement('div');
    form.className = 'manual-fix-form hidden';
    form.innerHTML =
      '<label class="manual-fix-label">番號' +
      '<input class="manual-fix-code" type="text" inputmode="text" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="例：JUFE-271" /></label>' +
      '<label class="manual-fix-label">片名' +
      '<input class="manual-fix-title" type="text" inputmode="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="日文或中文片名" /></label>' +
      '<div class="manual-fix-row">' +
      '<button type="button" class="btn btn-secondary btn-sm manual-fix-upload">上傳圖片</button>' +
      '<span class="manual-fix-file-name" hidden></span>' +
      '</div>' +
      '<div class="manual-fix-row">' +
      '<button type="button" class="btn btn-sm manual-fix-apply">套用</button>' +
      '<button type="button" class="btn btn-ghost btn-sm manual-fix-cancel">取消</button>' +
      '</div>';
    const codeEl = form.querySelector('.manual-fix-code');
    const titleEl = form.querySelector('.manual-fix-title');
    const nameEl = form.querySelector('.manual-fix-file-name');
    const uploadBtn = form.querySelector('.manual-fix-upload');
    const applyBtn = form.querySelector('.manual-fix-apply');
    const cancelBtn = form.querySelector('.manual-fix-cancel');
    if (codeEl && w && w.code && parseCodeParts(w.code)) codeEl.value = formatDisplayCode(w.code);
    if (titleEl && w && w.title) titleEl.value = w.title;
    let picked = null;

    function setOpen(on) {
      form.classList.toggle('hidden', !on);
      toggle.setAttribute('aria-expanded', on ? 'true' : 'false');
      toggle.hidden = !!on;
    }
    bindWorkAction(toggle, () => setOpen(true));
    if (cancelBtn) {
      bindWorkAction(cancelBtn, () => {
        picked = null;
        if (nameEl) {
          nameEl.hidden = true;
          nameEl.textContent = '';
        }
        setOpen(false);
      });
    }
    if (uploadBtn) {
      bindWorkAction(uploadBtn, () => {
        pickManualFixFile().then((file) => {
          if (!file) return;
          picked = file;
          if (nameEl) {
            nameEl.hidden = false;
            nameEl.textContent = file.name || '已選圖片';
          }
        });
      });
    }
    if (applyBtn) {
      bindWorkAction(applyBtn, () => {
        if (applyBtn.disabled) return;
        applyBtn.disabled = true;
        applyBtn.textContent = '套用中…';
        applyManualWorkFix(
          w,
          {
            code: codeEl ? codeEl.value : '',
            title: titleEl ? titleEl.value : '',
            file: picked,
          },
          card
        ).finally(() => {
          applyBtn.disabled = false;
          applyBtn.textContent = '套用';
        });
      });
    }
    [form, toggle].forEach((el) => {
      el.addEventListener('pointerdown', stopCarouselBubble);
      el.addEventListener('touchstart', stopCarouselBubble, { passive: true });
    });
    box.appendChild(toggle);
    box.appendChild(form);
    return box;
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
      if (e && !e.currentTarget) e.currentTarget = btn;
      handler(e);
    });
  }

  /** A related card can be extended only when it already has a real 品番. */
  function reliablePromoteCode(work) {
    const raw = String((work && work.code) || '').trim();
    if (!raw || raw === '片名搜尋' || raw === '未辨識' || raw === 'TITLE-SEARCH') return '';
    if (raw.toLowerCase() === 'null') return '';
    if (!parseCodeParts(raw)) return '';
    return formatDisplayCode(raw);
  }

  function slotLineForPromote(parent) {
    return String((parent && parent.line) || '') === 'multi' ? 'multi' : 'main';
  }

  function catalogOnlyUrl(url, cid) {
    let cover = String(url || '').trim();
    if (!isCatalogMediaUrl(cover)) cover = '';
    if (!cover && cid) {
      const restored = coverUrl(cid);
      if (isCatalogMediaUrl(restored)) cover = restored;
    }
    return cover;
  }

  function sameCodeGloss(prior, code, fieldZh, fieldSnake) {
    if (!prior || !code || !prior.code || !codesMatch(String(prior.code), String(code))) return '';
    return String(prior[fieldZh] || prior[fieldSnake] || '').trim();
  }

  /**
   * Code-identify payload as the replacement main for one carousel.
   * Cover/stills stay catalog jackets. Chinese gloss is this 品番's own
   * (identify response, else the related card). The previous main's title_zh
   * is never copied onto a different code.
   */
  function promotedGalleryWork(identifyData, parent, priorRelated) {
    if (!identifyData) return null;
    const built = galleryFromIdentify(identifyData);
    const first = built && built.items && built.items[0];
    if (!first || !reliablePromoteCode(first)) return null;
    const work = Object.assign({}, first);
    work.line = slotLineForPromote(parent);
    work.userPreview = '';
    work.cover = catalogOnlyUrl(work.cover, work.cid);
    work.stills = (Array.isArray(work.stills) ? work.stills : []).filter((u) => isCatalogMediaUrl(u));
    if (!String(work.titleZh || '').trim()) {
      work.titleZh = sameCodeGloss(priorRelated, work.code, 'titleZh', 'title_zh');
    }
    if (!String(work.actressZh || '').trim()) {
      const zh = sameCodeGloss(priorRelated, work.code, 'actressZh', 'actress_zh');
      if (zh) work.actressZh = zh;
    }
    if (!String(work.studioZh || '').trim()) {
      const zh = sameCodeGloss(priorRelated, work.code, 'studioZh', 'studio_zh');
      if (zh) work.studioZh = zh;
    }
    if (Array.isArray(work.relatedByTitle)) {
      work.relatedByTitle = work.relatedByTitle.map((r) => {
        if (!r) return r;
        const next = Object.assign({}, r);
        next.userPreview = '';
        next.cover = catalogOnlyUrl(next.cover, next.cid);
        next.stills = (Array.isArray(next.stills) ? next.stills : []).filter((u) => isCatalogMediaUrl(u));
        return next;
      });
    }
    return work;
  }

  function galleryWorkToHistoryWork(work) {
    const line = slotLineForPromote(work);
    const related = (work.relatedByTitle || []).map((r) => ({
      code: (r && r.code) || '',
      title: (r && r.title) || '',
      title_zh: (r && (r.titleZh || r.title_zh)) || '',
      cover: catalogOnlyUrl(r && r.cover, r && r.cid),
      cid: (r && r.cid) || '',
      stills: (Array.isArray(r && r.stills) ? r.stills : []).filter((u) => isCatalogMediaUrl(u)),
      why: (r && r.why) || '',
      line: (r && r.line) || relatedLineFromRaw(r),
      actress: (r && r.actress) || '',
      actress_zh: (r && (r.actressZh || r.actress_zh)) || '',
      studio: (r && r.studio) || '',
      studio_zh: (r && (r.studioZh || r.studio_zh)) || '',
      matched_keywords: (r && (r.matchedKeywords || r.matched_keywords)) || [],
    }));
    return slimWorkForHistory(
      {
        code: work.code,
        title: work.title,
        title_zh: work.titleZh || work.title_zh || '',
        cover: catalogOnlyUrl(work.cover, work.cid),
        cid: work.cid || '',
        stills: (work.stills || []).filter((u) => isCatalogMediaUrl(u)),
        actress: work.actress || '',
        actress_zh: work.actressZh || work.actress_zh || '',
        studio: work.studio || '',
        studio_zh: work.studioZh || work.studio_zh || '',
        related_by_title: related,
        theme_keywords: work.themeKeywords || work.theme_keywords || [],
        keyword_queries: work.keywordQueries || work.keyword_queries || [],
        line: line,
      },
      {},
      line
    );
  }

  function historyWorkToGallery(w, index) {
    if (!w) return null;
    const line = w.line || (index === 0 ? 'main' : 'multi');
    return workFromApi(
      Object.assign({}, w, {
        related_by_title:
          Array.isArray(w.related_by_title) && w.related_by_title.length
            ? w.related_by_title
            : w.related || [],
        title_zh: w.title_zh || w.titleZh || '',
        theme_keywords: w.theme_keywords || w.themeKeywords,
        keyword_queries: w.keyword_queries || w.keywordQueries,
      }),
      line
    );
  }

  function codesOrTitlesMatch(a, b) {
    const ac = String((a && a.code) || '');
    const bc = String((b && b.code) || '');
    if (parseCodeParts(ac) && parseCodeParts(bc)) return codesMatch(ac, bc);
    if (ac && bc && ac !== bc) return false;
    const at = String((a && a.title) || '');
    const bt = String((b && b.title) || '');
    if (at || bt) return at === bt;
    return ac === bc;
  }

  function findGalleryHistoryIndex(list, items) {
    const rows = Array.isArray(list) ? list : [];
    const gallery = Array.isArray(items) ? items : [];
    for (let i = 0; i < rows.length; i++) {
      const works = historySessionWorks(rows[i]);
      if (works.length !== gallery.length || !works.length) continue;
      let ok = true;
      for (let j = 0; j < works.length; j++) {
        if (!codesOrTitlesMatch(works[j], gallery[j])) {
          ok = false;
          break;
        }
      }
      if (ok) return i;
    }
    return -1;
  }

  function elHasClass(el, name) {
    if (!el || !name) return false;
    if (el.classList && typeof el.classList.contains === 'function' && el.classList.contains(name)) {
      return true;
    }
    return (' ' + String(el.className || '') + ' ').indexOf(' ' + name + ' ') !== -1;
  }

  function closestEl(node, className) {
    let el = node;
    while (el) {
      if (elHasClass(el, className)) return el;
      el = el.parentNode || el.parentElement || null;
    }
    return null;
  }

  function surfaceForCard(card) {
    let el = card;
    while (el) {
      if (el.id === 'history-detail' || elHasClass(el, 'history-detail')) return 'history';
      if (el.id === 'gallery-cards') return 'gallery';
      el = el.parentNode || el.parentElement || null;
    }
    return viewingHistoryId ? 'history' : 'gallery';
  }

  function carouselSlotIndex(block) {
    const parent = block && (block.parentNode || block.parentElement);
    if (!parent || !parent.children) return 0;
    let idx = 0;
    const kids = parent.children;
    for (let i = 0; i < kids.length; i++) {
      const kid = kids[i];
      if (kid === block) return idx;
      if (elHasClass(kid, 'work-carousel-block')) idx += 1;
    }
    return 0;
  }

  function blockAtSlot(root, index) {
    if (!root || !root.children || index < 0) return null;
    let idx = 0;
    const kids = root.children;
    for (let i = 0; i < kids.length; i++) {
      if (!elHasClass(kids[i], 'work-carousel-block')) continue;
      if (idx === index) return kids[i];
      idx += 1;
    }
    return null;
  }

  function replaceNode(oldNode, newNode) {
    if (!oldNode || !newNode || oldNode === newNode) return false;
    if (typeof oldNode.replaceWith === 'function') {
      oldNode.replaceWith(newNode);
      return true;
    }
    const parent = oldNode.parentNode || oldNode.parentElement;
    if (parent && typeof parent.replaceChild === 'function') {
      parent.replaceChild(newNode, oldNode);
      return true;
    }
    if (parent && parent.children) {
      const kids = parent.children;
      for (let i = 0; i < kids.length; i++) {
        if (kids[i] !== oldNode) continue;
        if (typeof kids.splice !== 'function') return false;
        kids.splice(i, 1, newNode);
        newNode.parentNode = parent;
        oldNode.parentNode = null;
        return true;
      }
    }
    return false;
  }

  function writePromotedHistorySlot(listIndex, slotIndex, histWork) {
    const list = loadHistory();
    if (listIndex < 0 || listIndex >= list.length) return null;
    const rec = list[listIndex];
    const works = historySessionWorks(rec).slice();
    const at = slotIndex >= 0 && slotIndex < works.length ? slotIndex : 0;
    works[at] = histWork;
    const next = Object.assign({}, rec, { works: works });
    if (at === 0) {
      next.code = histWork.code || '';
      next.title = histWork.title || '';
      next.title_zh = histWork.title_zh || '';
      next.cover = histWork.cover || '';
      next.stills = histWork.stills || [];
      next.actress = histWork.actress || '';
      next.related = histWork.related || [];
    }
    list[listIndex] = next;
    saveHistory(list);
    return next;
  }

  function rememberPromotedSession(itemsBefore, slotIndex, histWork, surface) {
    const list = loadHistory();
    let idx = -1;
    if (surface === 'history' && viewingHistoryId) {
      idx = list.findIndex((x) => x && x.id === viewingHistoryId);
    }
    if (idx < 0) idx = findGalleryHistoryIndex(list, itemsBefore);
    if (idx >= 0) {
      const next = writePromotedHistorySlot(idx, slotIndex, histWork);
      if (surface === 'history' && next && viewingHistoryId && next.id === viewingHistoryId) {
        paintHistoryDetail(next);
      }
      return next && next.id;
    }
    const gallery = (itemsBefore || []).slice();
    const works = gallery.map((w, i) => (i === slotIndex ? histWork : galleryWorkToHistoryWork(w)));
    if (!works.length) works.push(histWork);
    const first = works[0] || histWork;
    const rec = {
      id: 'h_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7),
      ts: Date.now(),
      kind: 'session',
      ok: true,
      code: first.code || '',
      title: first.title || '',
      title_zh: first.title_zh || '',
      cover: first.cover || '',
      stills: first.stills || [],
      actress: first.actress || '',
      related: first.related || [],
      userShots: [],
      works: works,
    };
    const nextList = loadHistory();
    nextList.unshift(rec);
    saveHistory(nextList.slice(0, HISTORY_MAX));
    return rec.id;
  }

  /**
   * Replace one vertical slot — the carousel this related card belongs to —
   * with a code identify of that 品番. Sibling uploads stay. The query image
   * is not sent and is not used as the catalog jacket.
   */
  function applyPromotedMain(identifyData, priorRelated, slotIndex, surface) {
    const onHistory = surface === 'history' && !!viewingHistoryId;
    let parent = null;
    let beforeItems = [];
    if (onHistory) {
      const rec = loadHistory().find((x) => x && x.id === viewingHistoryId);
      const works = historySessionWorks(rec);
      if (slotIndex < 0 || slotIndex >= works.length) slotIndex = 0;
      parent = historyWorkToGallery(works[slotIndex], slotIndex);
      beforeItems = works.map((w, i) => historyWorkToGallery(w, i));
    } else {
      beforeItems = (lastGalleryItems || []).slice();
      if (!beforeItems.length) slotIndex = 0;
      else if (slotIndex < 0 || slotIndex >= beforeItems.length) slotIndex = 0;
      parent = beforeItems[slotIndex] || null;
    }
    const work = promotedGalleryWork(identifyData, parent, priorRelated);
    if (!work) return null;
    const hist = galleryWorkToHistoryWork(work);
    if (!onHistory) {
      const items = beforeItems.slice();
      if (!items.length) items.push(work);
      else items[slotIndex] = work;
      lastGalleryItems = items;
      const block = blockAtSlot(galleryCards, slotIndex);
      const nextBlock = buildWorkCarousel(work);
      if (!block || !replaceNode(block, nextBlock)) {
        const notice = galleryNotice && !galleryNotice.hidden ? galleryNotice.textContent : null;
        renderGallery({ items: items, notice: notice });
      } else if (galleryCount) {
        galleryCount.textContent = String(items.length) + ' 部';
      }
    } else {
      const live = (lastGalleryItems || []).slice();
      const liveParent = live[slotIndex];
      if (
        liveParent &&
        parent &&
        liveParent.code &&
        parent.code &&
        codesMatch(String(liveParent.code), String(parent.code))
      ) {
        live[slotIndex] = work;
        lastGalleryItems = live;
        const block = blockAtSlot(galleryCards, slotIndex);
        if (block) replaceNode(block, buildWorkCarousel(work));
      }
    }
    rememberPromotedSession(beforeItems, slotIndex, hist, onHistory ? 'history' : 'gallery');
    return work;
  }

  async function identifyCodeForPromote(code) {
    const payload = { code: code };
    // A new code identify is its own run. A later upload supersedes it, and a
    // stale multi-image poll must not paint over this one.
    beginIdentifyProgress(0);
    promoteEpoch = progressEpoch;
    try {
      const streamed = await apiIdentifyStream(payload, applyProgressEvent);
      if (streamed && streamed.superseded) {
        const err = new Error('superseded');
        err.superseded = true;
        throw err;
      }
      return streamed && streamed.data;
    } catch (streamErr) {
      if (streamErr && (streamErr.followed || streamErr.superseded)) throw streamErr;
      if (streamErr && streamErr.jobId) return followIdentifyJob(streamErr.jobId, applyProgressEvent);
      const classic = await apiIdentify(payload);
      return classic && classic.data;
    }
  }

  function settlePromoteProgress() {
    if ((progressHigh.epoch || 0) !== promoteEpoch) return;
    if (!progressPanel || progressPanel.classList.contains('hidden')) return;
    hideProgress();
  }

  /**
   * 「以此為主」: identify that related 品番 by code and make it the main of
   * this carousel (cover, stills, related buckets, keyword chips). Other
   * uploads in the session stay on the vertical axis.
   */
  async function promoteRelatedToMain(relatedWork, card, opts) {
    opts = opts || {};
    const code = reliablePromoteCode(relatedWork);
    if (!code) {
      showToast('這部沒有可用番號，無法設為主作品');
      return { ok: false, reason: 'nocode' };
    }
    if (promoteBusy) {
      showToast('正在設為主作品…');
      return { ok: false, reason: 'busy' };
    }
    promoteBusy = true;
    const surface = opts.surface || surfaceForCard(card);
    const block = closestEl(card, 'work-carousel-block');
    const slotIndex = typeof opts.slotIndex === 'number' ? opts.slotIndex : carouselSlotIndex(block);
    showToast('正在以 ' + code + ' 延伸…', { persist: true });
    try {
      const data = await identifyCodeForPromote(code);
      const work = applyPromotedMain(data, relatedWork, slotIndex, surface);
      settlePromoteProgress();
      if (!work) {
        showToast((data && data.message) || '找不到這部作品');
        return { ok: false, reason: 'miss', data: data };
      }
      showToast('已以 ' + formatDisplayCode(work.code) + ' 為主作品');
      return { ok: true, work: work, slotIndex: slotIndex };
    } catch (err) {
      if (err && err.superseded) return { ok: false, reason: 'superseded' };
      settlePromoteProgress();
      showToast((err && err.message) || '延伸失敗');
      return { ok: false, reason: 'error' };
    } finally {
      promoteBusy = false;
    }
  }

  function buildWorkActions(w, coverWrap) {
    const bar = document.createElement('div');
    bar.className = 'work-actions';
    bar.setAttribute('role', 'group');
    const displayTitle = formatDisplayTitle(w.title, w.titleZh);
    const canPromote = isRelatedCarouselLine(String((w && w.line) || '')) && !!reliablePromoteCode(w);
    bar.setAttribute('aria-label', canPromote ? '複製、下載與以此為主' : '複製與下載');
    const specs = [
      { mark: '番', icon: ICON_COPY, label: '複製番號', run: () => copyWorkField(w.code, '已複製') },
      { mark: '名', icon: ICON_COPY, label: '複製名稱', run: () => copyWorkField(displayTitle, '已複製') },
      {
        mark: '合',
        icon: ICON_COPY,
        label: '複製番號與名稱',
        run: () => copyWorkField(formatCodeTitleClipboard(w.code, displayTitle), '已複製'),
      },
      {
        mark: '',
        icon: ICON_DL,
        label: '下載封面與劇照',
        run: () =>
          downloadWorkMedia(w, {
            coverImg:
              coverWrap && typeof coverWrap.querySelector === 'function'
                ? coverWrap.querySelector('img')
                : null,
          }),
      },
    ];
    if (canPromote) {
      specs.push({
        text: '以此為主',
        label: '以此為主',
        extraClass: ' work-action-promote',
        run: (e) => {
          const host = (e && (e.currentTarget || e.target)) || null;
          lastPromoteTask = promoteRelatedToMain(w, host);
        },
      });
    }
    specs.forEach((spec) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'work-action' + (spec.extraClass || (spec.mark ? '' : ' work-action-dl'));
      btn.setAttribute('aria-label', spec.label);
      btn.title = spec.label;
      if (spec.text) btn.textContent = spec.text;
      else btn.innerHTML = spec.icon + (spec.mark ? '<span class="work-action-mark">' + spec.mark + '</span>' : '');
      bindWorkAction(btn, spec.run);
      bar.appendChild(btn);
    });
    return bar;
  }

  let toastTimer = null;
  let toastClickHandler = null;

  function clearToastAction(el) {
    if (!el) return;
    el.classList.remove('is-action');
    el.setAttribute('role', 'status');
    el.removeAttribute('tabindex');
    if (toastClickHandler) {
      el.removeEventListener('click', toastClickHandler);
      toastClickHandler = null;
    }
  }

  function hideToast() {
    const el = $('lfp-toast');
    if (toastTimer) {
      clearTimeout(toastTimer);
      toastTimer = null;
    }
    if (!el) return;
    clearToastAction(el);
    el.classList.add('hidden');
    el.hidden = true;
  }

  function showToast(msg, opts) {
    opts = opts || {};
    const el = $('lfp-toast');
    if (!el) {
      setStatus(msg, 'ok');
      return;
    }
    if (toastTimer) {
      clearTimeout(toastTimer);
      toastTimer = null;
    }
    clearToastAction(el);
    el.textContent = msg;
    el.classList.remove('hidden');
    el.hidden = false;
    if (typeof opts.onClick === 'function') {
      el.classList.add('is-action');
      el.setAttribute('role', 'button');
      el.setAttribute('tabindex', '0');
      toastClickHandler = function (e) {
        if (e && e.preventDefault) e.preventDefault();
        const fn = opts.onClick;
        hideToast();
        fn();
      };
      el.addEventListener('click', toastClickHandler);
    }
    if (opts.persist) return;
    const ms = typeof opts.ms === 'number' ? opts.ms : 2400;
    toastTimer = setTimeout(hideToast, ms);
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

  function workFileStem(code) {
    const stem = (code && parseCodeParts(code) ? formatDisplayCode(code) : code) || 'work';
    return String(stem).replace(/[^A-Za-z0-9._-]+/g, '_') || 'work';
  }

  function workDownloadFilename(code, url, index, coverUrl) {
    const safe = workFileStem(code);
    const u = String(url || '').trim();
    const c = String(coverUrl || '').trim();
    // Name by URL, not slot: the jacket must stay *-cover.jpg even if it is not index 0.
    if (c && u === c) return safe + '-cover.jpg';
    const n = index > 0 ? index : index + 1;
    return safe + '-jp-' + String(n).padStart(2, '0') + '.jpg';
  }

  /**
   * Cover is always its own JPEG (*-cover.jpg). Stills are *-jp-01.jpg…
   * Same URL as the jacket is not turned into a still (keep the cover name).
   * Different URLs (pl jacket vs jp sample) are both kept.
   */
  function workDownloadItems(w) {
    const code = w && w.code;
    const cover = String((w && w.cover) || '').trim();
    const items = [];
    const seen = {};
    if (cover && !isNowPrintingUrl(cover)) {
      items.push({
        role: 'cover',
        url: cover,
        filename: workDownloadFilename(code, cover, 0, cover),
      });
      seen[cover] = true;
    }
    let stillN = 0;
    const stills = w && w.stills ? w.stills : [];
    for (let i = 0; i < stills.length; i++) {
      const s = String(stills[i] || '').trim();
      if (!s || isNowPrintingUrl(s) || seen[s]) continue;
      seen[s] = true;
      stillN += 1;
      items.push({
        role: 'still',
        url: s,
        filename: workDownloadFilename(code, s, stillN, cover),
      });
    }
    return items;
  }

  function workDownloadUrls(w) {
    return workDownloadItems(w).map(function (it) {
      return it.url;
    });
  }

  function cdnProxyUrl(url) {
    return '/api/cdn-file?url=' + encodeURIComponent(url);
  }

  /**
   * Same-work DMM jacket variants only (pl ↔ ps, digital ↔ mono/movie).
   * Never invent jp/js sample stills as a "cover".
   */
  function dmmCoverVariantUrls(url) {
    const primary = String(url || '').trim();
    const out = [];
    const seen = {};
    function add(u) {
      const s = String(u || '').trim();
      if (!s || seen[s]) return;
      seen[s] = true;
      out.push(s);
    }
    add(primary);
    if (!primary) return out;

    function swapJacketSuffix(u, from, to) {
      const re = new RegExp(from + '\\.jpg(\\?.*)?$', 'i');
      if (!re.test(u)) return '';
      return u.replace(re, to + '.jpg$1');
    }

    add(swapJacketSuffix(primary, 'pl', 'ps'));
    add(swapJacketSuffix(primary, 'ps', 'pl'));

    const pics = 'https://pics.dmm.co.jp';
    let m = primary.match(
      /^https?:\/\/pics\.dmm\.(?:co\.jp|com)\/digital\/video\/([^/?#]+)\/[^/?#]+?(pl|ps)\.jpg(\?.*)?$/i
    );
    if (m) {
      const cid = m[1];
      const q = m[3] || '';
      add(pics + '/mono/movie/adult/' + cid + '/' + cid + 'pl.jpg' + q);
      add(pics + '/mono/movie/adult/' + cid + '/' + cid + 'ps.jpg' + q);
    }
    m = primary.match(
      /^https?:\/\/pics\.dmm\.(?:co\.jp|com)\/mono\/movie\/adult\/([^/?#]+)\/[^/?#]+?(pl|ps)\.jpg(\?.*)?$/i
    );
    if (m) {
      const cid = m[1];
      const q = m[3] || '';
      add(DMM_PICS + '/' + cid + '/' + cid + 'pl.jpg' + q);
      add(DMM_PICS + '/' + cid + '/' + cid + 'ps.jpg' + q);
    }
    for (let i = 0; i < out.length; i++) {
      const u = out[i];
      if (u.indexOf('pics.dmm.co.jp/digital/') !== -1) {
        add(u.replace('pics.dmm.co.jp', 'pics.dmm.com'));
      } else if (u.indexOf('pics.dmm.com/digital/') !== -1) {
        add(u.replace('pics.dmm.com', 'pics.dmm.co.jp'));
      }
    }
    return out;
  }

  function jpegFileFromBytes(buf, filename) {
    const fileName = filename || 'image.jpg';
    const parts = buf ? [buf] : [];
    try {
      return new File(parts, fileName, { type: 'image/jpeg' });
    } catch (_) {
      try {
        return new Blob(parts, { type: 'image/jpeg' });
      } catch (__) {
        return null;
      }
    }
  }

  function jpegFileFromBlob(blob, filename) {
    return jpegFileFromBytes(blob, filename);
  }

  function shareSheetPayload(files) {
    // Files only: iOS hides「儲存影像」if text/url are mixed in.
    return { files: files };
  }

  function canShareImageFiles(files) {
    if (!files || !files.length) return false;
    if (typeof navigator.share !== 'function') return false;
    if (typeof navigator.canShare !== 'function') return true;
    try {
      return !!navigator.canShare({ files: files });
    } catch (_) {
      return false;
    }
  }

  function hasTransientUserActivation() {
    try {
      if (navigator.userActivation && typeof navigator.userActivation.isActive === 'boolean') {
        return navigator.userActivation.isActive;
      }
    } catch (_) {}
    return true;
  }

  function isShareAbort(err) {
    const name = err && err.name;
    const msg = String((err && err.message) || '');
    return name === 'AbortError' || /abort|cancel/i.test(msg);
  }

  async function fetchWorkImageBuffer(url) {
    const res = await fetch(cdnProxyUrl(url));
    if (!res.ok) throw new Error('cdn');
    const buf = await res.arrayBuffer();
    if (!buf || !buf.byteLength) throw new Error('empty');
    return buf;
  }

  async function fetchWorkImageBufferTries(url, tries) {
    const n = Math.max(1, tries || 1);
    let lastErr = null;
    for (let i = 0; i < n; i++) {
      try {
        return await fetchWorkImageBuffer(url);
      } catch (err) {
        lastErr = err;
      }
    }
    throw lastErr || new Error('cdn');
  }

  function displayedCoverLooksReady(img) {
    if (!img) return false;
    if (img.classList && img.classList.contains('img-broken')) return false;
    if (img._lfpCoverBlob && img._lfpCoverBlob.size) return true;
    const src = String(img.currentSrc || img.src || '');
    if (/^(blob:|data:)/i.test(src)) return true;
    const w = img.naturalWidth || img.width;
    const h = img.naturalHeight || img.height;
    return !!(w && h);
  }

  function findDisplayedCoverImg(w, opts) {
    if (opts && displayedCoverLooksReady(opts.coverImg)) return opts.coverImg;
    const code = w && w.code ? String(w.code) : '';
    const cover = String((w && w.cover) || '').trim();
    let nodes = [];
    try {
      const list =
        document.querySelectorAll && document.querySelectorAll('.cover-wrap img');
      if (list && list.length) nodes = list;
    } catch (_) {}
    for (let i = 0; i < nodes.length; i++) {
      const img = nodes[i];
      if (!displayedCoverLooksReady(img)) continue;
      const src = String(img.currentSrc || img.src || (img.getAttribute && img.getAttribute('src')) || '');
      if (cover && (src === cover || src.indexOf(cover) !== -1)) return img;
      if (code && img.alt === code + ' 封面') return img;
    }
    return null;
  }

  const warmedCoverByUrl = Object.create(null);

  function rememberWarmedCover(url, blob) {
    const k = String(url || '').trim();
    if (!k || !blob || !blob.size) return;
    warmedCoverByUrl[k] = blob;
  }

  function warmedCoverBlob(url) {
    const k = String(url || '').trim();
    return (k && warmedCoverByUrl[k]) || null;
  }

  function isCrossOriginDmmSrc(src) {
    return /^https?:\/\/pics\.dmm\.(co\.jp|com)\//i.test(String(src || '').trim());
  }

  function applyWarmedCoverToImg(img, url, blob) {
    rememberWarmedCover(url, blob);
    if (!img || !blob || !blob.size) return;
    img._lfpCoverBlob = blob;
    img._lfpCoverRemote = String(url || '').trim() || img._lfpCoverRemote || '';
    try {
      if (img._lfpCoverBlobUrl) {
        URL.revokeObjectURL(img._lfpCoverBlobUrl);
      }
    } catch (_) {}
    try {
      const obj = URL.createObjectURL(blob);
      img._lfpCoverBlobUrl = obj;
      img.src = obj;
    } catch (_) {}
  }

  /** Parallel with on-screen DMM <img>: cache a same-origin JPEG via /api/cdn-file. */
  function warmCoverFromProxy(img, url) {
    const u = String(url || '').trim();
    if (!u || isNowPrintingUrl(u)) return;
    const urls = dmmCoverVariantUrls(u);
    (async function () {
      for (let i = 0; i < urls.length; i++) {
        try {
          const buf = await fetchWorkImageBuffer(urls[i]);
          if (!buf) continue;
          let blob = null;
          try {
            blob = new Blob([buf], { type: 'image/jpeg' });
          } catch (_) {
            blob = null;
          }
          if (!blob || !blob.size) continue;
          applyWarmedCoverToImg(img, u, blob);
          return;
        } catch (_) {}
      }
    })();
  }

  function blobFromCanvas(canvas) {
    return new Promise(function (resolve, reject) {
      if (!canvas) {
        reject(new Error('canvas'));
        return;
      }
      if (typeof canvas.toBlob === 'function') {
        canvas.toBlob(
          function (b) {
            if (b) resolve(b);
            else reject(new Error('toBlob'));
          },
          'image/jpeg',
          0.92
        );
        return;
      }
      try {
        const dataUrl = canvas.toDataURL('image/jpeg', 0.92);
        fetch(dataUrl)
          .then(function (r) {
            return r.blob();
          })
          .then(resolve, reject);
      } catch (err) {
        reject(err);
      }
    });
  }

  async function blobFromSrc(src) {
    const u = String(src || '').trim();
    if (!u) return null;
    const res = await fetch(u);
    if (!res.ok) throw new Error('src');
    const blob = await res.blob();
    if (!blob || !blob.size) throw new Error('empty');
    return blob;
  }

  async function canvasExportDrawn(imgLike) {
    const w0 = imgLike.naturalWidth || imgLike.width;
    const h0 = imgLike.naturalHeight || imgLike.height;
    if (!w0 || !h0) return null;
    const canvas = document.createElement('canvas');
    canvas.width = w0;
    canvas.height = h0;
    const ctx = canvas.getContext && canvas.getContext('2d');
    if (!ctx || !ctx.drawImage) return null;
    ctx.drawImage(imgLike, 0, 0, w0, h0);
    const blob = await blobFromCanvas(canvas);
    return blob && blob.size ? blob : null;
  }

  function loadCorsCoverImage(src) {
    return new Promise(function (resolve, reject) {
      const im = new Image();
      im.crossOrigin = 'anonymous';
      im.referrerPolicy = 'no-referrer';
      im.onload = function () {
        resolve(im);
      };
      im.onerror = function () {
        reject(new Error('cors-img'));
      };
      im.src = src;
    });
  }

  /**
   * Jacket bytes already on the card — only same-origin / warmed proxy bytes.
   * A no-CORS DMM <img> taints canvas on iOS Safari (drawImage / createImageBitmap
   * / toBlob throw). Never treat that path as a cover fallback.
   */
  async function captureDisplayedCoverBlob(w, opts) {
    const img = findDisplayedCoverImg(w, opts);
    const cover = String((w && w.cover) || '').trim();
    if (img && img._lfpCoverBlob && img._lfpCoverBlob.size) return img._lfpCoverBlob;
    const warmed =
      warmedCoverBlob(cover) ||
      (img && warmedCoverBlob(img._lfpCoverRemote)) ||
      (img && warmedCoverBlob(img.currentSrc || img.src));
    if (warmed && warmed.size) return warmed;
    if (!img) return null;
    if (typeof img.decode === 'function') {
      try {
        await img.decode();
      } catch (_) {}
    }
    const src = String(img.currentSrc || img.src || '');
    if (isCrossOriginDmmSrc(src)) {
      return null;
    }
    if (/^(blob:|data:)/i.test(src) || (src && src.indexOf('/api/cdn-file') !== -1)) {
      try {
        const blob = await blobFromSrc(src);
        if (blob && blob.size) return blob;
      } catch (_) {}
    }
    if (typeof createImageBitmap === 'function') {
      try {
        const bmp = await createImageBitmap(img);
        const blob = await canvasExportDrawn(bmp);
        if (typeof bmp.close === 'function') bmp.close();
        if (blob && blob.size) return blob;
      } catch (_) {}
    }
    try {
      const blob = await canvasExportDrawn(img);
      if (blob && blob.size) return blob;
    } catch (_) {}
    if (src && /^https?:/i.test(src) && src.indexOf('/api/cdn-file') !== -1) {
      try {
        const clone = await loadCorsCoverImage(src);
        const blob = await canvasExportDrawn(clone);
        if (blob && blob.size) return blob;
      } catch (_) {}
    }
    if (src && !isCrossOriginDmmSrc(src)) {
      try {
        const blob = await blobFromSrc(src);
        if (blob && blob.size) return blob;
      } catch (_) {}
    }
    return null;
  }

  async function bufferFromCoverBlob(blob) {
    if (!blob) return null;
    if (typeof blob.arrayBuffer === 'function') {
      const buf = await blob.arrayBuffer();
      if (buf && buf.byteLength) return buf;
    }
    return blob;
  }

  /**
   * Jacket bytes: warmed same-origin blob first (display upgrade), then
   * /api/cdn-file (retries + pl/ps / mono; server also tries variants),
   * then blob:/cdn-file <img> src. Never draw a cross-origin DMM <img>.
   */
  async function fetchCoverImageBuffer(item, w, opts) {
    try {
      const local = await captureDisplayedCoverBlob(w, opts);
      const buf = await bufferFromCoverBlob(local);
      if (buf) return buf;
    } catch (_) {}
    const urls = dmmCoverVariantUrls(item && item.url);
    let lastErr = null;
    for (let i = 0; i < urls.length; i++) {
      const tries = i === 0 ? 2 : 1;
      try {
        return await fetchWorkImageBufferTries(urls[i], tries);
      } catch (err) {
        lastErr = err;
      }
    }
    try {
      const blob = await captureDisplayedCoverBlob(w, opts);
      const buf = await bufferFromCoverBlob(blob);
      if (buf) return buf;
    } catch (_) {}
    throw lastErr || new Error('cover');
  }

  function prefetchProgressToast(okCount, total, failed, meta) {
    let msg = '準備中（' + okCount + '/' + total + '）';
    if (meta && meta.coverFailed) {
      msg += ' · 封面無法下載';
      if (failed > 1) msg += ' · 失敗 ' + failed;
    } else if (failed) {
      msg += ' · 失敗 ' + failed;
    }
    showToast(msg, { persist: true });
  }

  function shareReadyMessage(result) {
    if (result && result.coverExpected && result.coverFailed) {
      const n = (result.files && result.files.length) || 0;
      let msg = '封面無法下載，已準備劇照 ' + n + ' 張';
      if (result.failed) msg += ' · 失敗 ' + result.failed;
      msg += ' · 點一下儲存';
      return msg;
    }
    if (result && result.failed) {
      return (
        '準備完成（' +
        result.files.length +
        '/' +
        result.total +
        '）· 點一下儲存到相簿'
      );
    }
    return '準備完成 · 點一下儲存到相簿';
  }

  async function prefetchWorkImageFiles(w, onProgress, opts) {
    const items = workDownloadItems(w);
    const total = items.length;
    const slots = new Array(total);
    let ok = 0;
    let failed = 0;
    let done = 0;
    const coverIdx = items.findIndex(function (it) {
      return it.role === 'cover';
    });
    let coverAttempted = coverIdx < 0;

    function report() {
      const coverFailedNow = coverIdx >= 0 && coverAttempted && !slots[coverIdx];
      if (onProgress)
        onProgress(ok, total, {
          ok: ok,
          failed: failed,
          done: done,
          total: total,
          coverFailed: coverFailedNow,
        });
    }
    report();

    async function fillSlot(i, tries, buffer) {
      const buf = buffer || (await fetchWorkImageBufferTries(items[i].url, tries));
      const file = jpegFileFromBytes(buf, items[i].filename);
      if (!file) throw new Error('file');
      slots[i] = file;
    }

    // Cover and stills in parallel: a slow/failing jacket (pl.jpg + variants)
    // must not keep the toast at 0/11 before stills enter the batch.
    const stillIdxs = [];
    for (let i = 0; i < items.length; i++) {
      if (i !== coverIdx) stillIdxs.push(i);
    }
    let cursor = 0;
    const workers = Math.min(3, stillIdxs.length) || 0;

    async function worker() {
      while (cursor < stillIdxs.length) {
        const i = stillIdxs[cursor++];
        try {
          await fillSlot(i, 1);
          ok += 1;
        } catch (_) {
          slots[i] = null;
          failed += 1;
        }
        done += 1;
        report();
      }
    }

    const coverJob = (async function () {
      if (coverIdx < 0) return;
      try {
        const buf = await fetchCoverImageBuffer(items[coverIdx], w, opts);
        await fillSlot(coverIdx, 1, buf);
        ok += 1;
      } catch (_) {
        slots[coverIdx] = null;
        failed += 1;
      }
      coverAttempted = true;
      done += 1;
      report();
    })();

    const jobs = [coverJob];
    for (let n = 0; n < workers; n++) jobs.push(worker());
    await Promise.all(jobs);

    const stillFiles = [];
    let coverFile = null;
    for (let i = 0; i < items.length; i++) {
      if (!slots[i]) continue;
      if (items[i].role === 'cover') coverFile = slots[i];
      else stillFiles.push(slots[i]);
    }
    // Cover last in the share list: iOS has been seen dropping the first file
    // of a multi-file share (jacket was index 0 → Photos got only the 10 stills).
    const files = coverFile ? stillFiles.concat([coverFile]) : stillFiles.slice();
    return {
      files: files,
      total: total,
      ok: ok,
      failed: failed,
      coverExpected: coverIdx >= 0,
      coverFailed: coverIdx >= 0 && !coverFile,
    };
  }

  function armTapToShare(files, readyMsg) {
    showToast(readyMsg || '準備完成 · 點一下儲存到相簿', {
      persist: true,
      onClick: function () {
        if (typeof navigator.share !== 'function') {
          showToast('請用 Safari 一次存入相簿（網頁無法直接寫入）');
          return;
        }
        Promise.resolve(navigator.share(shareSheetPayload(files))).catch(function (err) {
          if (isShareAbort(err)) return;
          showToast('此瀏覽器無法一次存入相簿，請用 Safari');
        });
      },
    });
  }

  async function offerSaveImageFiles(files, readyMsg) {
    if (!files || !files.length) {
      showToast('沒有可下載的圖片');
      return { ok: false, reason: 'empty' };
    }
    if (typeof navigator.share !== 'function') {
      showToast('請用 Safari 一次存入相簿（網頁無法直接寫入）');
      return { ok: false, reason: 'no-share' };
    }

    const shareable = canShareImageFiles(files);
    if (shareable && hasTransientUserActivation()) {
      try {
        await navigator.share(shareSheetPayload(files));
        hideToast();
        return { ok: true, reason: 'shared' };
      } catch (err) {
        if (isShareAbort(err)) {
          hideToast();
          return { ok: true, reason: 'abort' };
        }
      }
    }

    // Prefetch consumes the original tap; one follow-up tap opens one share sheet
    // for the whole set (iOS: 儲存影像). Never fire N <a download> clicks.
    armTapToShare(files, readyMsg);
    return { ok: true, reason: 'tap' };
  }

  let downloadBusy = false;

  async function downloadWorkMedia(w, opts) {
    const items = workDownloadItems(w);
    if (!items.length) {
      showToast('沒有可下載的圖片');
      return { ok: false, reason: 'empty' };
    }
    if (downloadBusy) {
      showToast('仍在準備圖片…');
      return { ok: false, reason: 'busy' };
    }
    downloadBusy = true;
    const total = items.length;
    try {
      prefetchProgressToast(0, total, 0);
      const result = await prefetchWorkImageFiles(
        w,
        function (okCount, tot, meta) {
          prefetchProgressToast(okCount, tot, meta && meta.failed, meta);
        },
        opts
      );
      if (!result.files.length) {
        showToast(
          result.coverExpected && result.coverFailed ? '封面無法下載，沒有可儲存的圖片' : '下載失敗'
        );
        return { ok: false, reason: result.coverFailed ? 'cover' : 'fetch' };
      }
      // Cover-fail no longer blocks stills. Only abort when every image failed.
      const readyMsg = shareReadyMessage(result);
      return await offerSaveImageFiles(result.files, readyMsg);
    } catch (_) {
      showToast('下載失敗');
      return { ok: false, reason: 'error' };
    } finally {
      downloadBusy = false;
    }
  }

  /**
   * One screenshot/main hit as a horizontal strip:
   * 主作品 ↔️ 相關1 ↔️ 相關2 … (stills inside each card still scroll sideways).
   * Multiple mains stack vertically in the gallery.
   */
  function buildWorkCarousel(mainWork) {
    const related = capRelatedBuckets(
      Array.isArray(mainWork.relatedByTitle) ? mainWork.relatedByTitle : []
    );
    const block = document.createElement('section');
    block.className = 'work-carousel-block';

    const head = document.createElement('div');
    head.className = 'work-carousel-head';
    const hint = document.createElement('div');
    hint.className = 'work-carousel-hint';
    const total = 1 + related.length;
    const themeKeywords = normalizeKeywordList(
      mainWork.themeKeywords || mainWork.theme_keywords
    );
    if (related.length) {
      setProtectedText(
        hint,
        '左右滑 · 主作品 ↔️ 相關（' +
          relatedHeadingForList(related, themeKeywords) +
          '）· ' +
          total +
          ' 張',
        themeKeywords
      );
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
      const line = relatedLineFromRaw(rw);
      let badge = '相關 ' + (i + 1);
      if (line === 'actress') badge = '同演員';
      else if (line === 'keyword') badge = '關鍵字';
      else if (line === 'theme') badge = '片名相近';
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

    // The main-work section is the carousel (card + stills + hint). Chips are
    // the next sibling, under that whole section — not between actress, the
    // copy buttons, and the cover inside the card, and not on related slides.
    const mainSection = document.createElement('div');
    mainSection.className = 'work-main-section';
    mainSection.appendChild(track);
    mainSection.appendChild(head);
    block.appendChild(mainSection);
    if (workShowsKeywordChips(mainWork)) {
      mountKeywordResearch(block, mainWork, related, themeKeywords);
    }
    return block;
  }

  /**
   * Keyword chips + 搜尋 as a sibling under the 主作品 section, outside the
   * card (not between actress / 番名合 and the cover) and not on 片名 / 關鍵字
   * / 同演員 slides or title-search candidate cards.
   * Re-search uses only the selected chips (server enforces multi-hit / cap 10).
   */
  function mountKeywordResearch(block, mainWork, related, themeKeywords) {
    const hasKeyword = (related || []).some((rw) => {
      const why = String((rw && rw.why) || '');
      const line = String((rw && rw.line) || '');
      return line === 'keyword' || why.includes('關鍵字');
    });
    const keywords = normalizeKeywordList(themeKeywords);
    if (!keywords.length && !hasKeyword) return;
    const panel = document.createElement('div');
    panel.className = 'kw-related-panel';
    const label = document.createElement('div');
    label.className = 'kw-related-label';
    setProtectedText(
      label,
      keywords.length ? formatKeywordListLabel('關鍵字相關', keywords) : '關鍵字相關',
      keywords
    );
    panel.appendChild(label);

    const research = document.createElement('div');
    research.className = 'kw-research-block';
    research.hidden = true;
    const researchLabel = document.createElement('div');
    researchLabel.className = 'kw-related-label';
    const researchTrack = document.createElement('div');
    researchTrack.className = 'kw-research-track';
    researchTrack.setAttribute('aria-label', '關鍵字再搜');
    research.appendChild(researchLabel);
    research.appendChild(researchTrack);

    const selected = {};
    let timer = 0;
    let seq = 0;

    function pickedList() {
      return keywords.filter((k) => selected[k]);
    }

    function showResearch(picked, state) {
      state = state || {};
      const headText = formatKeywordListLabel('關鍵字再搜', picked);
      research.hidden = false;
      if (state.loading) {
        setProtectedText(researchLabel, headText + ' · 搜尋中…', picked);
        researchTrack.innerHTML = '';
        researchTrack.hidden = true;
        return;
      }
      researchTrack.hidden = false;
      researchTrack.innerHTML = '';
      const items = state.items || [];
      if (state.error) {
        setProtectedText(researchLabel, headText + ' · 再搜失敗', picked);
        return;
      }
      if (!items.length) {
        setProtectedText(researchLabel, headText + ' · 沒有符合的作品', picked);
        return;
      }
      setProtectedText(researchLabel, headText, picked);
      items.slice(0, KEYWORD_RESEARCH_CAP).forEach((raw) => {
        const w = workFromApi(raw, 'keyword');
        const slide = document.createElement('div');
        slide.className = 'kw-research-slide';
        slide.appendChild(buildWorkCard(w, { slide: true, badgeLabel: '關鍵字' }));
        researchTrack.appendChild(slide);
      });
    }

    function clearResearch() {
      seq += 1;
      research.hidden = true;
      researchLabel.textContent = '';
      researchTrack.innerHTML = '';
      researchTrack.hidden = false;
    }

    async function runSearch() {
      const picked = pickedList();
      const my = ++seq;
      if (!picked.length) {
        research.hidden = true;
        researchLabel.textContent = '';
        researchTrack.innerHTML = '';
        return;
      }
      showResearch(picked, { loading: true, items: [] });
      try {
        const res = await fetch('/api/related-by-keywords', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            title: (mainWork && mainWork.title) || '',
            code: (mainWork && mainWork.code) || '',
            actress: (mainWork && mainWork.actress) || '',
            keywords: picked,
          }),
        });
        let data = null;
        try {
          data = await res.json();
        } catch (_) {
          data = null;
        }
        if (my !== seq) return;
        if (!res.ok || !data || data.ok === false) {
          showResearch(picked, { error: true, items: [] });
          return;
        }
        const rawItems = data.related || data.related_by_title || [];
        const items = (Array.isArray(rawItems) ? rawItems : []).filter((r) => {
          if (!r || !r.code) return false;
          if (mainWork && mainWork.code && codesMatch(String(r.code), String(mainWork.code))) return false;
          return true;
        });
        if (my !== seq) return;
        showResearch(picked, { items: items.slice(0, KEYWORD_RESEARCH_CAP) });
      } catch (_) {
        if (my !== seq) return;
        showResearch(picked, { error: true, items: [] });
      }
    }

    function scheduleSearch() {
      if (timer) clearTimeout(timer);
      // Drop a response that was started for the previous chip set.
      seq += 1;
      const token = seq;
      if (!pickedList().length) {
        research.hidden = true;
        researchLabel.textContent = '';
        researchTrack.innerHTML = '';
        researchTrack.hidden = false;
        return;
      }
      timer = setTimeout(() => {
        timer = 0;
        if (token !== seq) return;
        runSearch();
      }, 320);
    }

    if (keywords.length) {
      const row = document.createElement('div');
      row.className = 'kw-chip-row';
      row.setAttribute('role', 'group');
      row.setAttribute('aria-label', '關鍵字再搜');
      keywords.forEach((kw) => {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'kw-chip';
        chip.textContent = formatKeywordChip(kw);
        chip.setAttribute('data-kw', kw);
        chip.setAttribute('aria-pressed', 'false');
        chip.title = formatKeywordChip(kw);
        bindWorkAction(chip, () => {
          const on = !selected[kw];
          if (on) selected[kw] = true;
          else delete selected[kw];
          chip.classList.toggle('is-on', !!selected[kw]);
          chip.setAttribute('aria-pressed', selected[kw] ? 'true' : 'false');
          scheduleSearch();
        });
        row.appendChild(chip);
      });
      const searchBtn = document.createElement('button');
      searchBtn.type = 'button';
      searchBtn.className = 'kw-chip kw-search';
      searchBtn.textContent = '搜尋';
      bindWorkAction(searchBtn, () => {
        if (timer) {
          clearTimeout(timer);
          timer = 0;
        }
        if (!pickedList().length) {
          showToast('請先選關鍵字');
          clearResearch();
          return;
        }
        runSearch();
      });
      row.appendChild(searchBtn);
      panel.appendChild(row);
    }

    [panel, research].forEach((el) => {
      el.addEventListener('pointerdown', stopCarouselBubble);
      el.addEventListener('touchstart', stopCarouselBubble, { passive: true });
    });
    block.appendChild(panel);
    block.appendChild(research);
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

    lastGalleryItems = items || [];
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

  /** All persisted user-upload previews for a session. Legacy single-shot rows OK. */
  function historyUserShots(rec) {
    if (!rec) return [];
    let raw = rec.userShots;
    if (raw == null) raw = rec.userShot;
    if (raw == null) raw = rec.user_shots;
    if (typeof raw === 'string') raw = raw.trim() ? [raw] : [];
    if (!Array.isArray(raw)) return [];
    const out = [];
    const seen = {};
    for (let i = 0; i < raw.length; i++) {
      const s = String(raw[i] || '').trim();
      if (!s || seen[s]) continue;
      seen[s] = true;
      out.push(s);
    }
    return out;
  }

  function userShotThumbLimits(count) {
    const n = Math.max(1, count || 1);
    if (n <= 1) return { maxBytes: 96 * 1024, maxSide: 640 };
    if (n <= 3) return { maxBytes: 48 * 1024, maxSide: 480 };
    return { maxBytes: 28 * 1024, maxSide: 360 };
  }

  function isQuotaError(err) {
    if (!err) return false;
    const name = err.name;
    const code = err.code;
    const msg = String(err.message || '');
    return (
      name === 'QuotaExceededError' ||
      name === 'NS_ERROR_DOM_QUOTA_REACHED' ||
      code === 22 ||
      code === 1014 ||
      /quota/i.test(msg)
    );
  }

  function slimWorkMedia(w, stillN, relN) {
    if (!w) return w;
    return Object.assign({}, w, {
      stills: Array.isArray(w.stills) ? w.stills.slice(0, stillN) : [],
      related: Array.isArray(w.related) ? w.related.slice(0, relN) : [],
    });
  }

  function slimRecordMedia(r, stillN, relN) {
    return Object.assign({}, r, {
      stills: Array.isArray(r.stills) ? r.stills.slice(0, stillN) : [],
      related: Array.isArray(r.related) ? r.related.slice(0, relN) : [],
      works: (r.works || []).map(function (w) {
        return slimWorkMedia(w, stillN, relN);
      }),
    });
  }

  function withUserShots(r, shots) {
    return Object.assign({}, r, { userShots: shots });
  }

  function writeHistoryList(list) {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(list));
  }

  function saveHistory(list) {
    let next = Array.isArray(list) ? list.slice(0, HISTORY_MAX) : [];
    try {
      writeHistoryList(next);
      return true;
    } catch (e) {
      if (!isQuotaError(e)) {
        try {
          writeHistoryList(next);
          return true;
        } catch (_) {}
      }
    }
    try {
      next = next.map(function (r, i) {
        return slimRecordMedia(r, i === 0 ? 10 : 4, i === 0 ? 8 : 3);
      });
      writeHistoryList(next);
      return true;
    } catch (_) {}
    try {
      next = next.map(function (r, i) {
        const shots = historyUserShots(r);
        return withUserShots(
          slimRecordMedia(r, i === 0 ? 8 : 2, i === 0 ? 5 : 0),
          i === 0 ? shots : shots.slice(0, 1)
        );
      });
      writeHistoryList(next);
      return true;
    } catch (_) {}
    try {
      next = next.map(function (r, i) {
        return withUserShots(
          slimRecordMedia(r, i === 0 ? 4 : 0, 0),
          i === 0 ? historyUserShots(r) : []
        );
      });
      writeHistoryList(next);
      return true;
    } catch (_) {}
    for (let n = Math.min(next.length, 12); n >= 1; n--) {
      try {
        const keep = next.slice(0, n).map(function (r, i) {
          return withUserShots(
            slimRecordMedia(r, i === 0 ? 4 : 0, 0),
            i === 0 ? historyUserShots(r) : []
          );
        });
        writeHistoryList(keep);
        return true;
      } catch (_) {}
    }
    return false;
  }

  function downscaleFileToDataUrl(file, maxBytes, maxSide) {
    const cap = Math.max(8 * 1024, maxBytes || THUMB_MAX_BYTES);
    const side = Math.max(64, maxSide || USER_SHOT_THUMB_MAX_SIDE);
    return new Promise((resolve) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        try {
          const canvas = document.createElement('canvas');
          let w = img.naturalWidth || img.width;
          let h = img.naturalHeight || img.height;
          const scale = Math.min(1, side / Math.max(w, h, 1));
          w = Math.max(1, Math.round(w * scale));
          h = Math.max(1, Math.round(h * scale));
          canvas.width = w;
          canvas.height = h;
          const ctx = canvas.getContext('2d');
          ctx.drawImage(img, 0, 0, w, h);
          const maxChars = Math.ceil(cap * 1.37);
          let q = 0.7;
          let dataUrl = canvas.toDataURL('image/jpeg', q);
          while (dataUrl.length > maxChars && q > 0.28) {
            q -= 0.08;
            dataUrl = canvas.toDataURL('image/jpeg', q);
          }
          let shrink = 0;
          while (dataUrl.length > maxChars && shrink < 3) {
            canvas.width = Math.max(48, Math.round(canvas.width * 0.65));
            canvas.height = Math.max(48, Math.round(canvas.height * 0.65));
            ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
            dataUrl = canvas.toDataURL('image/jpeg', 0.48);
            shrink += 1;
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

  function smallFileDataUrl(file, maxBytes) {
    const cap = Math.max(8 * 1024, maxBytes || THUMB_MAX_BYTES);
    if (!file || file.size > cap) return Promise.resolve(null);
    return new Promise((resolve) => {
      if (typeof FileReader !== 'function') {
        resolve(null);
        return;
      }
      const fr = new FileReader();
      fr.onload = () => {
        resolve(typeof fr.result === 'string' && fr.result ? fr.result : null);
      };
      fr.onerror = () => resolve(null);
      try {
        fr.readAsDataURL(file);
      } catch (_) {
        resolve(null);
      }
    });
  }

  async function buildUserShotThumbs(files) {
    const list = (files || []).filter(Boolean).slice(0, USER_SHOT_SESSION_MAX);
    const limits = userShotThumbLimits(list.length);
    const out = [];
    for (let i = 0; i < list.length; i++) {
      let d = await downscaleFileToDataUrl(list[i], limits.maxBytes, limits.maxSide);
      if (!d) d = await smallFileDataUrl(list[i], limits.maxBytes);
      if (d) out.push(d);
    }
    return out;
  }

  async function appendHistoryFromIdentify(data, userFiles) {
    if (!data || !data.ok) return null;
    const works = sessionWorksFromIdentify(data);
    if (!works.length) return null;

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
    return rec.id;
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
        historyUserShots(rec)[0] ||
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
      const codeLabel = rec.code || first.code || (first.skipped ? '已跳過' : rec.ok === false ? '未找到' : '—');
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
      actress_zh: first.actress_zh || rec.actress_zh || '',
      studio: first.studio || rec.studio || '',
      studio_zh: first.studio_zh || rec.studio_zh || '',
      cover: first.cover || rec.cover || '',
      stills: Array.isArray(first.stills) ? first.stills : (rec.stills || []),
      related_by_title: first.related || rec.related || [],
      theme_keywords: normalizeKeywordList(first.theme_keywords || first.themeKeywords),
      keyword_queries: normalizeKeywordList(first.keyword_queries || first.keywordQueries),
      from_offline_cache: true,
      // Always nested: vertical = session works, horizontal = each work's related
      results: works.map((w, i) => ({
        ok: true,
        code: w.code,
        title: w.title,
        title_zh: w.title_zh,
        actress: w.actress,
        actress_zh: w.actress_zh || '',
        studio: w.studio || '',
        studio_zh: w.studio_zh || '',
        cid: w.cid,
        cover: w.cover,
        stills: w.stills || [],
        related_by_title: w.related || [],
        theme_keywords: normalizeKeywordList(w.theme_keywords || w.themeKeywords),
        keyword_queries: normalizeKeywordList(w.keyword_queries || w.keywordQueries),
        visual_mismatch: !!(w.visual_mismatch || w.visualMismatch),
        visual_note: String(w.visual_note || w.visualNote || '').trim(),
        unidentified: !!w.unidentified,
        skipped: !!w.skipped,
        user_preview: w.user_preview || '',
        ok: !w.skipped,
        from_image_index: w.from_image_index || null,
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

  function persistManualWorkFix(oldW, nextW, opts) {
    opts = opts || {};
    const patch = {
      code: nextW.code,
      title: nextW.title,
      title_zh: nextW.titleZh || nextW.title_zh || '',
      cover: nextW.cover || '',
      stills: nextW.stills || [],
      actress: nextW.actress || '',
      cid: nextW.cid || '',
    };
    if (Array.isArray(nextW.relatedByTitle) && nextW.relatedByTitle.length) {
      patch.related = slimRelatedForHistory(nextW.relatedByTitle);
    } else if (Array.isArray(nextW.related) && nextW.related.length) {
      patch.related = slimRelatedForHistory(nextW.related);
    }
    const recId = opts.historyId || viewingHistoryId;
    function stampRecord(rec, workIdx) {
      if (!rec) return;
      persistHistoryWork(rec.id, workIdx, patch);
      if (workIdx === 0) {
        const list = loadHistory();
        const i = list.findIndex((x) => x.id === rec.id);
        if (i < 0) return;
        list[i] = Object.assign({}, list[i], {
          code: patch.code || list[i].code,
          title: patch.title || list[i].title,
          title_zh: patch.title_zh || list[i].title_zh,
          cover: patch.cover || list[i].cover,
          stills: patch.stills && patch.stills.length ? patch.stills : list[i].stills,
        });
        saveHistory(list);
      }
    }
    if (recId) {
      const rec = loadHistory().find((x) => x.id === recId);
      if (rec) {
        const works = historySessionWorks(rec);
        let idx = typeof opts.workIndex === 'number' ? opts.workIndex : -1;
        if (idx < 0 && oldW && oldW.code) {
          idx = works.findIndex((w) => w && w.code && codesMatch(w.code, oldW.code));
        }
        if (idx < 0) idx = 0;
        stampRecord(rec, idx);
        return recId;
      }
    }
    const list = loadHistory();
    for (let i = 0; i < list.length; i++) {
      const works = historySessionWorks(list[i]);
      const idx = works.findIndex((w) => oldW && oldW.code && w && w.code && codesMatch(w.code, oldW.code));
      if (idx >= 0) {
        stampRecord(list[i], idx);
        return list[i].id;
      }
    }
    const rec = {
      id: 'h_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7),
      ts: Date.now(),
      kind: 'session',
      ok: true,
      code: nextW.code,
      title: nextW.title,
      title_zh: patch.title_zh,
      cover: patch.cover,
      stills: patch.stills,
      actress: patch.actress,
      related: patch.related || [],
      works: [Object.assign({ line: nextW.line || 'main' }, patch)],
    };
    const nextList = loadHistory();
    nextList.unshift(rec);
    saveHistory(nextList.slice(0, HISTORY_MAX));
    return rec.id;
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
      workNeedsTitleZh(work) ||
      workNeedsThemeKeywords(work);
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
        // A saved related list is frozen. A later search must not shrink 14↔9.
        // Chinese titles may be copied onto the same rows; membership stays.
        const frozen = relatedHasPersistedMembership(related);
        const merged = frozen
          ? applyTitleZhOntoRelated(related, incoming)
          : (related && related.length
            ? mergeRelatedIncremental(related, incoming)
            : slimRelatedForHistory(incoming));
        const patch = { related: merged, stills: work.stills, cid: work.cid };
        const incomingZh = String(data.title_zh || '').trim();
        if (incomingZh && !String(work.title_zh || work.titleZh || '').trim()) {
          patch.title_zh = incomingZh;
        }
        const incomingKw = normalizeKeywordList(data.theme_keywords);
        if (incomingKw.length) patch.theme_keywords = incomingKw;
        const incomingQ = normalizeKeywordList(data.keyword_queries);
        if (incomingQ.length) patch.keyword_queries = incomingQ;
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
    const shots = historyUserShots(rec);
    if (shots.length) {
      const head = document.createElement('div');
      head.className = 'user-shots-head';
      head.textContent = '你的截圖';
      const scroll = document.createElement('div');
      scroll.className = 'user-shots-scroll';
      shots.forEach((u, i) => {
        const img = document.createElement('img');
        img.src = u;
        img.alt = '你的截圖 ' + (i + 1);
        img.loading = 'lazy';
        img.decoding = 'async';
        bindLightboxable(img, shots, i);
        scroll.appendChild(img);
      });
      historyDetailEl.appendChild(head);
      historyDetailEl.appendChild(scroll);
    }
    const works = historySessionWorks(rec);
    const paintWorks = works.filter((w) => {
      if (!w) return false;
      if (w.skipped) return true;
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
          String((next && next.title_zh) || '') !== String((works[i] && works[i].title_zh) || '') ||
          JSON.stringify((next && next.theme_keywords) || []) !==
            JSON.stringify((works[i] && works[i].theme_keywords) || [])
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

  async function reidentifySkippedSlot(work) {
    const idx = Number(work && (work.fromImageIndex || work.from_image_index));
    const file = idx > 0 ? slotUploadFiles[idx - 1] : null;
    if (!file) {
      showToast('這張原圖不在了，請重新選取');
      return { ok: false, reason: 'missing-file' };
    }
    setStatus('重新辨識第 ' + idx + ' 張…', 'busy');
    let single = null;
    const sessionId = lastIdentifyPayload && lastIdentifyPayload.session_id;
    try {
      try {
        const streamed = await apiIdentifyStream(
          { images: [file] },
          null,
          { quiet: true, slotIndex: idx, sessionId: sessionId || '' }
        );
        single = streamed && streamed.data;
      } catch (_) {
        const classic = await apiIdentify({ images: [file] });
        single = classic && classic.data;
      }
    } catch (err) {
      setStatus('');
      showToast((err && err.message) || '重新辨識失敗');
      return { ok: false, reason: 'fetch' };
    }
    const identified =
      single &&
      (single.ok || single.code || single.title || (Array.isArray(single.results) && single.results.length));
    if (!identified) {
      setStatus('');
      showToast((single && single.message) || '重新辨識失敗');
      return { ok: false, reason: 'miss' };
    }
    const patched = mergeSlotRetryIntoIdentify(lastIdentifyPayload, idx, single);
    lastIdentifyPayload = patched;
    const result = galleryFromIdentify(patched);
    renderGallery(result);
    try {
      const recId = historySavePromise ? await historySavePromise : null;
      if (recId) replaceHistorySessionWorks(recId, sessionWorksFromIdentify(patched));
    } catch (_) {}
    setStatus('');
    showToast('已重新辨識這張');
    return { ok: true };
  }

  async function runIdentify({ images, image, code, title } = {}, myRun) {
    const imgs = images && images.length ? images : image ? [image] : [];
    identifyBusy = true;
    activeIdentifyJobId = '';
    if (imgs.length > 1) {
      batchFiles = imgs.slice();
      slotUploadFiles = imgs.slice();
      identifyImageTotal = imgs.length;
    } else {
      identifyImageTotal = 0;
    }
    const busyMsg = imgs.length > 1
      ? '多圖辨識中（' + imgs.length + ' 張）…'
      : imgs.length
        ? '看圖讀片名／搜尋中…'
        : title
          ? '以片名搜尋中…'
          : '載入畫廊…';
    setStatus(busyMsg, 'busy');
    beginIdentifyProgress(imgs.length);
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
      identifyBusy = false;
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
        lastIdentifyPayload = data;
        renderGallery(result);
        // Persist history (async thumbs)
        historySavePromise = appendHistoryFromIdentify(data, imgs).catch(() => null);
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
        if (myRun !== runId || (streamed && streamed.superseded)) return;
        data = streamed.data;
      } catch (streamErr) {
        if (myRun !== runId || (streamErr && streamErr.superseded)) return;
        if (streamErr && streamErr.followed) {
          throw streamErr;
        }
        if (streamErr && streamErr.jobId) {
          data = await followIdentifyJob(streamErr.jobId, applyProgressEvent);
        } else {
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
      }
      handleResult(data);
    } catch (e) {
      if (myRun !== runId || (e && e.superseded)) return;
      identifyBusy = false;
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
  if (btnSkipSlot) {
    btnSkipSlot.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      requestSkipCurrentSlot();
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
  const fileManualFix = $('file-manual-fix');
  if (fileManualFix) {
    fileManualFix.addEventListener('change', () => {
      const file = (fileManualFix.files && fileManualFix.files[0]) || null;
      fileManualFix.value = '';
      const cb = manualFixFileCb;
      manualFixFileCb = null;
      if (typeof cb === 'function') cb(file);
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
    const keep = batchQueryKeepsFrames(batchFiles.length, identifyBusy, galleryHasOpenFrame());
    const myRun = ++runId;
    hideOcrPrompt();
    if (!keep) hideUserShots();
    const payload = AV_CODE_INPUT_RE.test(v) ? { code: v } : { title: v };
    if (keep) payload.images = batchFiles.slice();
    runIdentify(payload, myRun);
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
      const keep = batchQueryKeepsFrames(batchFiles.length, identifyBusy, galleryHasOpenFrame());
      const newRun = ++runId;
      hideOcrPrompt();
      const payload = AV_CODE_INPUT_RE.test(v) ? { code: v } : { title: v };
      if (keep) payload.images = batchFiles.slice();
      runIdentify(payload, newRun);
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
      formatKeywordListLabel,
      segmentDisplayText,
      workShowsKeywordChips,
      normalizeKeywordList,
      workNeedsThemeKeywords,
      historyRecordIsOpenable,
      renderHistoryList,
      openHistoryDetail,
      paintHistoryDetail,
      historyUserShots,
      userShotThumbLimits,
      saveHistory,
      loadHistory,
      appendHistoryFromIdentify,
      relatedNeedsTitleZh,
      relatedBucketsNeedFill,
      showProgress,
      applyProgressEvent,
      beginIdentifyProgress,
      bindIdentifyJob,
      formatKeywordChip,
      formatPersonName,
      resumePayloadFromJob,
      followIdentifyJob,
      IDENTIFY_JOB_FOLLOW_MS,
      mergeSlotRetryIntoIdentify,
      skipControlState,
      replaceHistorySessionWorks,
      workNeedsTitleZh,
      workNeedsManualFix,
      batchQueryKeepsFrames,
      workHasUsableCover,
      workHasUsableTitle,
      mergeManualFixIntoWork,
      persistManualWorkFix,
      applyManualWorkFix,
      workDownloadFilename,
      workDownloadUrls,
      workDownloadItems,
      dmmCoverVariantUrls,
      shareSheetPayload,
      canShareImageFiles,
      jpegFileFromBlob,
      shareReadyMessage,
      downloadWorkMedia,
      offerSaveImageFiles,
      reliablePromoteCode,
      promotedGalleryWork,
      promoteRelatedToMain,
      promoteTask: () => lastPromoteTask,
      renderGallery,
    };
  } catch (_) {}
})();
