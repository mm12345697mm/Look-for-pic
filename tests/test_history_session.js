'use strict';

/**
 * History session shape: one query → one history row; related stays nested.
 * Loads app.js in a DOM stub so we exercise the real helpers.
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

function makeEl(tag, id) {
  const classes = new Set();
  const node = {
    tagName: String(tag || 'div').toUpperCase(),
    id: id || '',
    children: [],
    _listeners: {},
    _attrs: {},
    className: '',
    style: {},
    dataset: {},
    hidden: false,
    disabled: false,
    value: '',
    parentNode: null,
    _html: '',
    _text: '',
    get innerHTML() {
      return this._html;
    },
    set innerHTML(v) {
      this._html = String(v);
      if (!String(v)) this.children = [];
    },
    get textContent() {
      const kids = this.children || [];
      if (kids.length) {
        return kids.map((c) => (c && c.textContent != null ? String(c.textContent) : '')).join('');
      }
      return this._text || '';
    },
    set textContent(v) {
      this._text = v == null ? '' : String(v);
      this._html = this._text;
      this.children = [];
    },
    classList: {
      toggle(name, on) {
        if (arguments.length > 1) {
          if (on) classes.add(name);
          else classes.delete(name);
        } else if (classes.has(name)) classes.delete(name);
        else classes.add(name);
      },
      add(name) {
        classes.add(name);
      },
      remove(name) {
        classes.delete(name);
      },
      contains(name) {
        return classes.has(name);
      },
    },
    appendChild(child) {
      this.children.push(child);
      child.parentNode = this;
      return child;
    },
    addEventListener(type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    },
    removeEventListener(type, fn) {
      const list = this._listeners[type] || [];
      this._listeners[type] = list.filter((x) => x !== fn);
    },
    remove() {
      if (!this.parentNode) return;
      const kids = this.parentNode.children || [];
      const i = kids.indexOf(this);
      if (i >= 0) kids.splice(i, 1);
      this.parentNode = null;
    },
    setAttribute(k, v) {
      this._attrs[k] = v;
    },
    getAttribute(k) {
      return this._attrs[k] || '';
    },
    removeAttribute(k) {
      delete this._attrs[k];
    },
    click(ev) {
      const e = ev || {
        preventDefault() {},
        stopPropagation() {},
      };
      (this._listeners.click || []).forEach((fn) => fn(e));
    },
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
  };
  node._classes = classes;
  return node;
}

const byId = {};
function getEl(id) {
  if (!byId[id]) byId[id] = makeEl('div', id);
  return byId[id];
}

const store = {};
let anchorClicks = 0;
let canvasThumbN = 0;
const context = {
  window: {},
  document: {
    getElementById(id) {
      return getEl(id);
    },
    createElement(tag) {
      const el = makeEl(tag);
      if (String(tag).toLowerCase() === 'a') {
        const origClick = el.click.bind(el);
        el.click = function (ev) {
          anchorClicks += 1;
          return origClick(ev);
        };
      }
      if (String(tag).toLowerCase() === 'canvas') {
        el.width = 0;
        el.height = 0;
        el.getContext = () => ({
          drawImage() {},
        });
        el.toDataURL = () => {
          canvasThumbN += 1;
          return 'data:image/jpeg;base64,thumb' + canvasThumbN;
        };
        el.toBlob = (cb) => {
          const blob = new Blob([new Uint8Array([0xff, 0xd8, 0xff, 0xd9])], {
            type: 'image/jpeg',
          });
          setTimeout(() => cb && cb(blob), 0);
        };
      }
      return el;
    },
    addEventListener() {},
    body: makeEl('body'),
  },
  localStorage: {
    getItem(k) {
      return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null;
    },
    setItem(k, v) {
      store[k] = String(v);
    },
  },
  URL: {
    createObjectURL() {
      return 'blob:test';
    },
    revokeObjectURL() {},
  },
  FormData: class FormData {},
  Blob: typeof Blob !== 'undefined' ? Blob : class Blob {},
  File:
    typeof File !== 'undefined'
      ? File
      : class File extends (typeof Blob !== 'undefined' ? Blob : class Blob {}) {},
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  Image: class Image {
    constructor() {
      this.onload = null;
      this.onerror = null;
      this.naturalWidth = 400;
      this.naturalHeight = 300;
      this.width = 400;
      this.height = 300;
    }
    set src(v) {
      this._src = v;
      const self = this;
      setTimeout(() => {
        if (typeof self.onload === 'function') self.onload();
      }, 0);
    }
    get src() {
      return this._src;
    }
  },
  FileReader: class FileReader {
    constructor() {
      this.onload = null;
      this.onerror = null;
      this.result = null;
    }
    readAsDataURL(file) {
      this.result = 'data:image/jpeg;base64,file-' + ((file && file.name) || 'x');
      const self = this;
      setTimeout(() => {
        if (typeof self.onload === 'function') self.onload();
      }, 0);
    }
  },
  confirm: () => false,
  navigator: { userAgent: 'node-test', clipboard: { writeText: async () => {} } },
  console,
  setTimeout,
  clearTimeout,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
};
context.window = context;
context.globalThis = context;
context.self = context;
vm.createContext(context);
const src = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
vm.runInContext(src, context);

const H = context.window.__lfpHistory;
assert.ok(H, 'expected window.__lfpHistory test hook');

function related(n, line) {
  return Array.from({ length: n }, (_, i) => ({
    code: 'REL-' + String(i + 1).padStart(3, '0'),
    title: 'Related ' + (i + 1),
    title_zh: '相關' + (i + 1),
    line: line,
    why: line === 'actress' ? '同女優' : line === 'keyword' ? '關鍵字' : '片名相近',
  }));
}

// Single identify: related must not become extra session works
{
  const data = {
    ok: true,
    code: 'AAA-001',
    title: 'Main Work',
    title_zh: '主作品',
    cover: 'https://pics.dmm.co.jp/digital/video/aaa00001/aaa00001pl.jpg',
    stills: [],
    related_by_title: related(5, 'theme').concat(related(5, 'keyword'), related(3, 'actress')),
    related: related(5, 'theme'),
  };
  const works = H.sessionWorksFromIdentify(data);
  assert.strictEqual(works.length, 1, 'one session work for a single-query identify');
  assert.strictEqual(works[0].code, 'AAA-001');
  assert.ok(works[0].related.length >= 5, 'related nested on the session work');
  assert.ok(!works.some((w) => /^REL-/.test(w.code)), 'related codes are not top-level works');

  const rec = { id: 'h1', kind: 'session', works: works, code: works[0].code, related: works[0].related };
  const payload = H.identifyPayloadFromHistory(rec);
  assert.strictEqual(payload.results.length, 1);
  const gallery = H.galleryFromIdentify(payload);
  assert.strictEqual(gallery.items.length, 1, 'history replay: one vertical row');
  assert.ok(gallery.items[0].relatedByTitle.length >= 5, 'history replay: related on horizontal axis');
}

// Multi-image: vertical = session mains only
{
  const data = {
    ok: true,
    multi: true,
    code: 'BBB-001',
    title: 'Shot 1',
    related_by_title: related(3, 'theme'),
    related: [
      { code: 'BBB-002', title: 'Shot 2', why: '多圖辨識', line: 'multi' },
    ],
    results: [
      {
        ok: true,
        code: 'BBB-001',
        title: 'Shot 1',
        line: 'main',
        related_by_title: related(3, 'theme'),
      },
      {
        ok: true,
        code: 'BBB-002',
        title: 'Shot 2',
        line: 'multi',
        related_by_title: related(2, 'keyword'),
      },
    ],
  };
  const works = H.sessionWorksFromIdentify(data);
  assert.strictEqual(works.length, 2, 'multi-image session has two vertical works');
  assert.strictEqual(works[0].related.length, 3);
  assert.strictEqual(works[1].related.length, 2);

  const rec = { id: 'h2', kind: 'session', works: works, code: works[0].code };
  const gallery = H.galleryFromIdentify(H.identifyPayloadFromHistory(rec));
  assert.strictEqual(gallery.items.length, 2);
  assert.strictEqual(gallery.items[0].relatedByTitle.length, 3);
  assert.strictEqual(gallery.items[1].relatedByTitle.length, 2);
}

// Live gallery must not flatten related into extras
{
  const data = {
    ok: true,
    code: 'CCC-001',
    title: 'Main',
    related_by_title: related(5, 'theme'),
    related: related(5, 'theme'),
  };
  const gallery = H.galleryFromIdentify(data);
  assert.strictEqual(gallery.items.length, 1);
  assert.strictEqual(gallery.items[0].relatedByTitle.length, 5);
}

// Title-search candidates stay vertical (not related)
{
  const data = {
    ok: true,
    code: 'DDD-001',
    title: 'Query title',
    search_mode: 'title',
    related_by_title: related(2, 'theme'),
    candidates: [
      { code: 'DDD-001', title: 'A' },
      { code: 'DDD-002', title: 'B' },
    ],
  };
  const works = H.sessionWorksFromIdentify(data);
  assert.strictEqual(works.length, 2);
  assert.strictEqual(works[1].line, 'candidate');
  assert.ok(H.isRelatedBucketItem({ line: 'theme', why: '片名相近' }));
  assert.ok(!H.isRelatedBucketItem({ line: 'candidate', why: '片名候選' }));
  assert.ok(!H.isRelatedBucketItem({ code: 'AAA-001', title: 'Main' }));
}

// Chinese display: JP（中文）only when title_zh exists; never empty parentheses
{
  assert.strictEqual(
    H.formatDisplayTitle('日本語タイトル', '中文片名'),
    '日本語タイトル（中文片名）'
  );
  assert.strictEqual(H.formatDisplayTitle('日本語タイトル', ''), '日本語タイトル');
  assert.strictEqual(H.formatDisplayTitle('日本語タイトル', '   '), '日本語タイトル');
  assert.strictEqual(H.formatDisplayTitle('日本語タイトル（）', ''), '日本語タイトル');
  assert.strictEqual(H.formatDisplayTitle('日本語タイトル', '日本語タイトル'), '日本語タイトル');
  assert.ok(!H.formatDisplayTitle('日本語タイトル', '').includes('（）'));
  assert.strictEqual(
    H.formatCodeTitleClipboard('AAA-001', '日本語タイトル（中文片名）'),
    'AAA-001\n日本語タイトル（中文片名）'
  );
}

// Success and failure rows are both openable; cover/title/ok do not gate
{
  const success = {
    id: 'ok-row',
    kind: 'session',
    ok: true,
    code: 'EEE-001',
    title: 'Success Work',
    title_zh: '成功',
    cover: 'https://pics.dmm.co.jp/digital/video/eee00001/eee00001pl.jpg',
    works: [
      {
        code: 'EEE-001',
        title: 'Success Work',
        title_zh: '成功',
        cover: 'https://pics.dmm.co.jp/digital/video/eee00001/eee00001pl.jpg',
        related: related(2, 'theme'),
        line: 'main',
      },
    ],
  };
  const failure = {
    id: 'fail-row',
    kind: 'session',
    ok: false,
    code: '',
    title: '',
    cover: '',
    message: '未找到番號或片名',
    works: [{ code: '', title: '', related: [], line: 'main' }],
  };
  const legacyFail = { id: 'legacy-fail', code: 'FFF-001', title: '', cover: '' };
  assert.ok(H.historyRecordIsOpenable(success));
  assert.ok(H.historyRecordIsOpenable(failure));
  assert.ok(H.historyRecordIsOpenable(legacyFail));
  const gOk = H.galleryFromIdentify(H.identifyPayloadFromHistory(success));
  assert.strictEqual(gOk.items.length, 1);
  assert.strictEqual(gOk.items[0].relatedByTitle.length, 2);
  const gFail = H.galleryFromIdentify(H.identifyPayloadFromHistory(failure));
  assert.ok(gFail.items.length >= 1);
}

// Click path: success rows must open immediately even if related backfill never returns
{
  const success = {
    id: 'click-ok',
    kind: 'session',
    ok: true,
    ts: Date.now(),
    code: 'GGG-001',
    title: 'Click Success',
    title_zh: '點擊成功',
    cover: 'https://pics.dmm.co.jp/digital/video/ggg00001/ggg00001pl.jpg',
    works: [
      {
        code: 'GGG-001',
        title: 'Click Success',
        title_zh: '點擊成功',
        cover: 'https://pics.dmm.co.jp/digital/video/ggg00001/ggg00001pl.jpg',
        related: [],
        line: 'main',
      },
    ],
  };
  const failure = {
    id: 'click-fail',
    kind: 'session',
    ok: false,
    ts: Date.now() - 1000,
    code: '',
    title: '',
    cover: '',
    message: '未找到',
    works: [{ code: '', title: '', related: [], line: 'main' }],
  };
  store.lfp_identify_history_v1 = JSON.stringify([success, failure]);

  let fetchCalls = 0;
  context.fetch = () => {
    fetchCalls += 1;
    return new Promise(() => {});
  };

  H.renderHistoryList();
  const rows = getEl('history-list').children;
  assert.strictEqual(rows.length, 2, 'list renders success and failure');
  assert.strictEqual(rows[0].tagName, 'DIV', 'row is not a nested <button>');
  assert.ok(rows[0].children.some((c) => c.className === 'history-item-del'));

  const opened = H.openHistoryDetail('click-ok');
  assert.strictEqual(opened, true);
  assert.ok(
    getEl('screen-history-detail').classList.contains('active'),
    'success row opens detail without waiting for related fetch'
  );
  assert.ok(getEl('history-detail').children.length >= 1, 'session layout painted immediately');
  assert.ok(fetchCalls >= 1, 'related enrich starts in the background');

  const openedFail = H.openHistoryDetail('click-fail');
  assert.strictEqual(openedFail, true);
  assert.ok(getEl('screen-history-detail').classList.contains('active'));
}

// Related gap fingerprint: missing title_zh still needs a pass even at cap
{
  const full = related(5, 'theme').concat(related(5, 'keyword'), related(3, 'actress'));
  assert.ok(!H.relatedBucketsNeedFill(full, { title: 'テーマ', actress: '誰か' }));
  const fullNoZh = full.map((r) => Object.assign({}, r, { title_zh: '' }));
  assert.ok(H.relatedNeedsTitleZh(fullNoZh));
  assert.ok(H.relatedBucketsNeedFill([], { title: 'テーマ' }));
  assert.ok(!H.relatedBucketsNeedFill([], { title: '' }));
  const shortSaved = related(4, 'theme').concat(related(3, 'keyword'), related(2, 'actress'));
  assert.strictEqual(shortSaved.length, 9);
  assert.ok(!H.relatedBucketsNeedFill(shortSaved, { title: '巨乳水泳部', actress: '誰か' }));
  assert.ok(H.relatedBucketsNeedFill([], { title: '巨乳水泳部', actress: '誰か' }));
}

// Sub-progress sits beside the active step and does not rewrite identify.
{
  H.showProgress();
  const steps = getEl('progress-steps').children;
  assert.ok(steps.length >= 4, 'progress rows exist');
  function row(id) {
    return steps.find((n) => n.dataset && n.dataset.step === id);
  }
  H.applyProgressEvent({ step: 'vision', status: 'active', detail: 'Gemini 看圖辨識中…', progress: 0.2 });
  assert.strictEqual(row('vision').dataset.phase, '辨識中');
  H.applyProgressEvent({ step: 'search', status: 'active', detail: '搜尋作品資料…', progress: 0.55 });
  assert.ok(!row('vision').dataset.phase);
  assert.strictEqual(row('search').dataset.phase, '目錄查詢');
  H.applyProgressEvent({ step: 'cover', status: 'active', detail: '抓取封面', progress: 0.88 });
  assert.strictEqual(row('cover').dataset.phase, '封面鎖定');
  H.applyProgressEvent({ step: 'done', status: 'active', detail: '為每部作品補齊相關…', progress: 0.94 });
  assert.strictEqual(row('done').dataset.phase, '相關作品');
  const phase = (row('done').children || []).find((n) => String(n.className || '').indexOf('step-phase') !== -1);
  assert.ok(phase && phase.textContent === '相關作品');
  assert.ok(H.workNeedsTitleZh({ code: 'AAA-001', title: 'x', title_zh: '' }));
  assert.ok(!H.workNeedsTitleZh({ code: 'AAA-001', title: 'x', title_zh: '中文' }));
}

// Progress high-water: a later slot must not rewind to 搜尋第 3/4 or an earlier percent.
{
  const FALSE_TIMEOUT = ['時間不夠', '尚未查完', '尚未鎖定'];
  H.showProgress();
  H.bindIdentifyJob('job_forward');
  H.applyProgressEvent({
    step: 'search',
    status: 'active',
    detail: '搜尋第 3/4 張…',
    progress: 0.68,
    phase: '目錄查詢',
  });
  H.applyProgressEvent({
    step: 'search',
    status: 'active',
    detail: '封面鎖定第 3/4 張…',
    progress: 0.7,
    phase: '封面鎖定',
  });
  H.applyProgressEvent({
    step: 'search',
    status: 'active',
    detail: '搜尋第 4/4 張…',
    progress: 0.74,
    phase: '目錄查詢',
  });
  H.applyProgressEvent({
    step: 'search',
    status: 'active',
    detail: '封面鎖定第 4/4 張…',
    progress: 0.8,
    phase: '封面鎖定',
  });
  H.applyProgressEvent({
    step: 'search',
    status: 'active',
    detail: '搜尋第 3/4 張…',
    progress: 0.68,
    phase: '目錄查詢',
  });
  H.applyProgressEvent({
    step: 'vision',
    status: 'active',
    detail: '第 2/4 張',
    progress: 0.3,
    phase: '辨識中',
  });
  H.applyProgressEvent({
    step: 'receive',
    status: 'active',
    detail: '正在接收 4 張…',
    progress: 0.02,
  });
  const steps = getEl('progress-steps').children;
  const search = steps.find((row) => row.dataset && row.dataset.step === 'search');
  const vision = steps.find((row) => row.dataset && row.dataset.step === 'vision');
  assert.strictEqual(getEl('progress-detail').textContent, '封面鎖定第 4/4 張…');
  assert.strictEqual(getEl('progress-pct').textContent, '80%');
  assert.strictEqual(getEl('progress-bar').style.width, '80%');
  assert.ok(search && search.dataset.phase === '封面鎖定');
  assert.ok(String(search.className).indexOf('is-active') !== -1);
  assert.ok(vision && String(vision.className).indexOf('is-active') === -1);
  const blob =
    getEl('progress-detail').textContent +
    getEl('progress-summary').textContent +
    getEl('progress-pct').textContent;
  FALSE_TIMEOUT.forEach((phrase) => assert.ok(blob.indexOf(phrase) === -1, phrase));

  H.bindIdentifyJob('job_restart');
  H.applyProgressEvent({
    step: 'search',
    status: 'active',
    detail: '搜尋第 1/4 張…',
    progress: 0.55,
    phase: '目錄查詢',
  });
  assert.strictEqual(getEl('progress-detail').textContent, '搜尋第 1/4 張…');
  assert.strictEqual(getEl('progress-pct').textContent, '55%');
}

// 7-image run must not adopt a stale 4-image denominator. 搜尋第 2/4 is 61%
// and 搜尋第 2/7 is 59%, so percent alone would oscillate 7↔4.
{
  const FALSE_TIMEOUT = ['時間不夠', '尚未查完', '尚未鎖定'];
  const sevenSteps = [
    { id: 'receive', label: '接收圖片（7 張）' },
    { id: 'vision', label: '逐張看圖辨識' },
    { id: 'parse', label: '彙整番號／片名' },
    { id: 'verify', label: '核對片名與番號' },
    { id: 'search', label: '搜尋作品資料' },
    { id: 'cover', label: '抓取封面與劇照' },
    { id: 'done', label: '完成，進入畫廊' },
  ];
  const pct27 = 0.55 + 0.25 * (1 / 7);
  H.beginIdentifyProgress(7);
  H.showProgress(sevenSteps, 7);
  H.bindIdentifyJob('job_seven');
  H.applyProgressEvent({
    step: 'search',
    status: 'active',
    detail: '搜尋第 2/7 張…',
    progress: pct27,
    phase: '目錄查詢',
  });
  H.applyProgressEvent({
    step: 'search',
    status: 'active',
    detail: '搜尋第 2/4 張…',
    progress: 0.55 + 0.25 * (1 / 4),
    phase: '目錄查詢',
  });
  H.applyProgressEvent({
    step: 'search',
    status: 'active',
    detail: '搜尋第 3/4 張…',
    progress: 0.68,
    phase: '目錄查詢',
  });
  H.applyProgressEvent({
    step: 'search',
    status: 'active',
    detail: '搜尋第 1/7 張…',
    progress: 0.55,
    phase: '目錄查詢',
  });
  const steps = getEl('progress-steps').children;
  const receive = steps.find((row) => row.dataset && row.dataset.step === 'receive');
  assert.strictEqual(getEl('progress-detail').textContent, '搜尋第 2/7 張…');
  assert.strictEqual(getEl('progress-pct').textContent, Math.round(pct27 * 100) + '%');
  assert.ok(getEl('progress-detail').textContent.indexOf('/4') === -1);
  assert.ok(receive && String(receive.innerHTML).indexOf('7') !== -1);
  // Merged-work related counts are not the upload denominator.
  H.applyProgressEvent({
    step: 'done',
    status: 'active',
    detail: '相關作品 1/4…',
    progress: 0.95,
    phase: '相關作品',
  });
  H.applyProgressEvent({
    step: 'search',
    status: 'active',
    detail: '搜尋第 4/4 張…',
    progress: 0.8,
    phase: '封面鎖定',
  });
  assert.strictEqual(getEl('progress-detail').textContent, '相關作品 1/4…');
  assert.strictEqual(getEl('progress-pct').textContent, '95%');
  const blob =
    getEl('progress-detail').textContent +
    getEl('progress-summary').textContent +
    getEl('progress-pct').textContent;
  FALSE_TIMEOUT.forEach((phrase) => assert.ok(blob.indexOf(phrase) === -1, phrase));

  // A new upload resets. The same panel may then show a real 4-image run.
  H.beginIdentifyProgress(4);
  H.showProgress(
    [
      { id: 'receive', label: '接收圖片（4 張）' },
      { id: 'search', label: '搜尋作品資料' },
      { id: 'done', label: '完成，進入畫廊' },
    ],
    4
  );
  H.bindIdentifyJob('job_four_new');
  H.applyProgressEvent({
    step: 'search',
    status: 'active',
    detail: '搜尋第 2/4 張…',
    progress: 0.55 + 0.25 * (1 / 4),
    phase: '目錄查詢',
  });
  assert.strictEqual(getEl('progress-detail').textContent, '搜尋第 2/4 張…');
  assert.strictEqual(getEl('progress-pct').textContent, '61%');
}

// Multi-shot history: persist and render every user upload; legacy single-shot still OK
{
  function shotsOf(rec) {
    return Array.from(H.historyUserShots(rec), (s) => '' + s);
  }
  assert.strictEqual(shotsOf({ userShots: ['data:a', 'data:b', '', 'data:a'] }).join(','), 'data:a,data:b');
  assert.strictEqual(shotsOf({ userShot: 'data:only' }).join(','), 'data:only');
  assert.strictEqual(shotsOf({ user_shots: 'data:legacy' }).join(','), 'data:legacy');
  assert.strictEqual(shotsOf({ userShots: [] }).join(','), '');
  assert.ok(H.userShotThumbLimits(5).maxBytes < H.userShotThumbLimits(1).maxBytes);

  const work = {
    code: 'AAA-001',
    title: 'Main',
    related: related(1, 'theme'),
    line: 'main',
  };
  H.saveHistory([
    {
      id: 'h-multi',
      kind: 'session',
      code: 'AAA-001',
      title: 'Main',
      userShots: ['data:shot-1', 'data:shot-2', 'data:shot-3'],
      works: [work],
    },
  ]);
  const saved = H.loadHistory();
  assert.strictEqual(H.historyUserShots(saved[0]).length, 3, 'new save keeps all shots');

  H.paintHistoryDetail(saved[0]);
  const detail = getEl('history-detail');
  const scroll = detail.children.find((c) => c.className === 'user-shots-scroll');
  assert.ok(scroll, '你的截圖 strip is painted');
  assert.strictEqual(scroll.children.length, 3, 'every stored shot is rendered');
  assert.ok(scroll.children.every((c) => c.tagName === 'IMG'));

  H.paintHistoryDetail({
    id: 'h-legacy',
    userShot: 'data:one-only',
    works: [work],
  });
  const legacyScroll = getEl('history-detail').children.find((c) => c.className === 'user-shots-scroll');
  assert.strictEqual(legacyScroll.children.length, 1, 'legacy single shot still shows');
}

// Quota: drop older thumbs, never slice the newest session to one shot
{
  const origSet = context.localStorage.setItem;
  context.localStorage.setItem = (k, v) => {
    const parsed = JSON.parse(v);
    const total = parsed.reduce((n, r) => n + ((r.userShots && r.userShots.length) || 0), 0);
    if (total > 3) {
      const err = new Error('quota');
      err.name = 'QuotaExceededError';
      throw err;
    }
    store[k] = String(v);
  };
  const newest = {
    id: 'h-new',
    userShots: ['n1', 'n2', 'n3'],
    works: [{ code: 'BBB-001', title: 'New', related: related(2, 'theme'), stills: ['s1', 's2'] }],
  };
  const older = {
    id: 'h-old',
    userShots: ['o1', 'o2'],
    works: [{ code: 'CCC-001', title: 'Old', related: related(2, 'theme') }],
  };
  const ok = H.saveHistory([newest, older]);
  assert.strictEqual(ok, true);
  const loaded = H.loadHistory();
  assert.strictEqual(loaded[0].id, 'h-new');
  assert.strictEqual(
    (loaded[0].userShots || []).map((s) => '' + s).join(','),
    'n1,n2,n3',
    'newest keeps all shots under quota'
  );
  assert.ok(
    !loaded[1] || (loaded[1].userShots || []).length <= 1,
    'older session may be thinned'
  );
  context.localStorage.setItem = origSet.bind(context.localStorage);
}

// Download: jpeg filenames, never a zip; share payload is files-only
{
  const cover = 'https://pics.dmm.co.jp/digital/video/aaa00001/aaa00001pl.jpg';
  const still = 'https://pics.dmm.co.jp/digital/video/aaa00001/aaa00001jp-1.jpg';
  assert.strictEqual(H.workDownloadFilename('AAA-001', cover, 0, cover), 'AAA-001-cover.jpg');
  assert.strictEqual(H.workDownloadFilename('AAA-001', still, 1, cover), 'AAA-001-jp-01.jpg');
  assert.ok(!H.workDownloadFilename('AAA-001', cover, 0, cover).endsWith('.zip'));
  const urls = H.workDownloadUrls({ cover: cover, stills: [still] });
  assert.strictEqual(urls.length, 2);
  assert.strictEqual(urls[0], cover);
  assert.strictEqual(urls[1], still);
  const fakeFiles = [{ name: 'AAA-001-cover.jpg', type: 'image/jpeg' }];
  const payload = H.shareSheetPayload(fakeFiles);
  assert.deepStrictEqual(Object.keys(payload), ['files']);
  assert.strictEqual(payload.files, fakeFiles);
  assert.strictEqual(payload.text, undefined);
  assert.strictEqual(payload.url, undefined);

  // Cover is always its own file; same still URL does not replace *-cover.jpg
  const dup = H.workDownloadItems({ cover: cover, stills: [cover, still], code: 'AAA-001' });
  assert.strictEqual(dup.length, 2);
  assert.strictEqual(dup[0].role, 'cover');
  assert.strictEqual(dup[0].filename, 'AAA-001-cover.jpg');
  assert.strictEqual(dup[1].role, 'still');
  assert.strictEqual(dup[1].url, still);
  assert.strictEqual(dup[1].filename, 'AAA-001-jp-01.jpg');

  // Jacket pl vs sample jp stay both (HAWA-367 pattern: 1 cover + 10 stills)
  const hawaCover = 'https://pics.dmm.co.jp/digital/video/1hawa00367/1hawa00367pl.jpg';
  const hawaStills = Array.from({ length: 10 }, (_, i) =>
    'https://pics.dmm.co.jp/digital/video/1hawa00367/1hawa00367jp-' + (i + 1) + '.jpg'
  );
  const hawa = H.workDownloadItems({ code: 'HAWA-367', cover: hawaCover, stills: hawaStills });
  assert.strictEqual(hawa.length, 11);
  assert.strictEqual(hawa[0].role, 'cover');
  assert.strictEqual(hawa[0].url, hawaCover);
  assert.strictEqual(hawa[0].filename, 'HAWA-367-cover.jpg');
  assert.strictEqual(hawa[1].filename, 'HAWA-367-jp-01.jpg');
  assert.strictEqual(hawa[10].filename, 'HAWA-367-jp-10.jpg');
  assert.ok(hawa.every((it) => it.url !== hawaCover || it.role === 'cover'));
}

function walkNodes(node, acc) {
  acc = acc || [];
  if (!node) return acc;
  acc.push(node);
  (node.children || []).forEach((child) => walkNodes(child, acc));
  return acc;
}

// 彼女の妹 keeps the phrase and both halves, including the one-character 妹 chip
{
  assert.strictEqual(
    H.normalizeKeywordList(['ノーブラ誘惑', '巨乳', '彼女の妹', '彼女', '妹', '誘']).join('・'),
    'ノーブラ・誘惑・巨乳・彼女の妹・彼女・妹'
  );
  assert.strictEqual(
    H.normalizeKeywordList(['巨乳', '電車', '彼女', '妹', '彼女の妹']).join('・'),
    '巨乳・電車・彼女の妹・彼女・妹'
  );
  assert.strictEqual(
    H.normalizeKeywordList(['家庭教師', '10秒挿入', '肉欲教育', '息子の家庭教師', '息子', 'ママ']).join('・'),
    '息子の家庭教師・家庭教師・10秒挿入・肉欲教育・息子・ママ'
  );
  assert.strictEqual(H.normalizeKeywordList(['あ', '中']).join('・'), '');
  // Edition/format tags are not chips. OL stays. A glued BOD pair is dropped.
  assert.strictEqual(
    H.normalizeKeywordList(['BOD', '中出し', '叔母', 'VOL', 'Blu-ray', 'OL', '交尾BOD', 'VR']).join('・'),
    '中出し・叔母・OL・VR'
  );
  assert.strictEqual(
    H.formatKeywordListLabel('關鍵字相關', ['BOD', '中出し']),
    '關鍵字相關（' + H.formatKeywordChip('中出し') + '）'
  );
  assert.strictEqual(H.formatKeywordChip('ノーブラ'), 'ノーブラ（無胸罩）');
  assert.strictEqual(H.formatKeywordChip('誘惑'), '誘惑（誘惑）');
  assert.strictEqual(H.formatKeywordChip('水泳部'), '水泳部（游泳社）');
  assert.strictEqual(H.formatKeywordChip('合宿'), '合宿（集訓）');
  assert.strictEqual(H.formatKeywordChip('媚薬'), '媚薬（媚藥）');
  assert.strictEqual(H.formatKeywordChip('巨乳'), '巨乳（巨乳）');
  assert.strictEqual(H.formatPersonName('福田ゆあ', ''), '福田ゆあ');
  assert.ok(H.formatPersonName('福田ゆあ', '').indexOf('（') === -1);
  assert.strictEqual(H.formatPersonName('福田ゆあ', '福田由愛'), '福田ゆあ（福田由愛）');
  assert.strictEqual(H.IDENTIFY_JOB_FOLLOW_MS, 4 * 600 * 1000 + 60 * 1000);
  assert.strictEqual(H.resumePayloadFromJob({ status: 'stalled' }), null);
  assert.strictEqual(H.resumePayloadFromJob({ status: 'running', result: { ok: true } }), null);
  const doneJob = H.resumePayloadFromJob({ status: 'done', result: { ok: true, code: 'AAA-001' } });
  assert.strictEqual(doneJob.code, 'AAA-001');
}

// Keyword chips on a card that already has a 關鍵字 related section
{
  assert.strictEqual(
    H.formatKeywordListLabel('關鍵字相關', ['眼鏡', '地味', 'OL']),
    '關鍵字相關（' + ['眼鏡', '地味', 'OL'].map(H.formatKeywordChip).join('・') + '）'
  );
  assert.strictEqual(
    H.formatKeywordListLabel('關鍵字再搜', ['眼鏡', '地味']),
    '關鍵字再搜（' + ['眼鏡', '地味'].map(H.formatKeywordChip).join('・') + '）'
  );
  const data = {
    ok: true,
    code: 'JUFE-271',
    title: '地味な眼鏡',
    theme_keywords: ['眼鏡', '地味', 'OL'],
    keyword_queries: ['地味眼鏡', '眼鏡'],
    related_by_title: related(1, 'keyword'),
  };
  const works = H.sessionWorksFromIdentify(data);
  assert.strictEqual(works[0].theme_keywords.join('・'), '眼鏡・地味・OL');
  assert.strictEqual(works[0].keyword_queries.join('・'), '地味眼鏡・眼鏡');
  const payload = H.identifyPayloadFromHistory({ id: 'kw', works: works, code: 'JUFE-271' });
  assert.strictEqual(payload.theme_keywords.join('・'), '眼鏡・地味・OL');
  assert.strictEqual(payload.results[0].theme_keywords.join('・'), '眼鏡・地味・OL');
  assert.strictEqual(payload.results[0].keyword_queries.join('・'), '地味眼鏡・眼鏡');

  H.paintHistoryDetail({
    id: 'kw-paint',
    works: [
      {
        code: 'JUFE-271',
        title: '地味な眼鏡では隠し切れない美人OL',
        cover: 'https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271pl.jpg',
        stills: [],
        theme_keywords: ['眼鏡', '地味', 'OL'],
        keyword_queries: ['地味眼鏡'],
        related: related(1, 'keyword'),
        line: 'main',
      },
    ],
  });
  const nodes = walkNodes(getEl('history-detail'));
  const labels = nodes
    .filter((n) => n.className === 'kw-related-label')
    .map((n) => n.textContent);
  const glossed = '關鍵字相關（' + ['眼鏡', '地味', 'OL'].map(H.formatKeywordChip).join('・') + '）';
  assert.ok(labels.indexOf(glossed) !== -1, labels.join('|'));
  const chips = nodes.filter((n) => n.getAttribute && n.getAttribute('data-kw'));
  assert.strictEqual(chips.map((c) => c.getAttribute('data-kw')).join('・'), '眼鏡・地味・OL');
  assert.strictEqual(chips.map((c) => c.textContent).join('・'), ['眼鏡', '地味', 'OL'].map(H.formatKeywordChip).join('・'));
  const hint = nodes.find((n) => n.className === 'work-carousel-hint');
  assert.ok(
    hint && hint.textContent.indexOf(glossed) !== -1,
    hint && hint.textContent
  );
  const search = nodes.find((n) => String(n.className || '').indexOf('kw-search') !== -1);
  assert.ok(search && search.textContent === '搜尋');
  assert.ok(nodes.some((n) => String(n.className || '').indexOf('kw-research-block') !== -1));

  H.paintHistoryDetail({
    id: 'theme-only',
    works: [
      {
        code: 'AAA-001',
        title: '主題だけ',
        cover: 'https://pics.dmm.co.jp/digital/video/aaa00001/aaa00001pl.jpg',
        related: related(1, 'theme'),
        line: 'main',
      },
    ],
  });
  const themeNodes = walkNodes(getEl('history-detail'));
  assert.ok(!themeNodes.some((n) => n.getAttribute && n.getAttribute('data-kw')));
  assert.ok(!themeNodes.some((n) => String(n.className || '').indexOf('kw-related-panel') !== -1));
  // Same-actress miss note must not contradict a carousel that already has 同演員.
  {
    const dishonest = '線上目錄未取得同女優相關；僅顯示主作品 CDN。';
    const withActress = H.galleryFromIdentify({
      ok: true,
      code: 'DANDYA-001',
      title: '息子の家庭教師',
      message: '來源：avbase',
      related_note: dishonest,
      related_by_title: [
        { code: 'MDBK-434', title: '別作品', line: 'actress', why: '同演員', cover: 'https://example.com/a.jpg' },
        { code: 'KW-001', title: '家庭教師が10秒で挿入', line: 'keyword', why: '關鍵字×2', matched_keywords: ['家庭教師', '10秒挿入'] },
      ],
      theme_keywords: ['家庭教師', '10秒挿入', '肉欲教育', '息子の家庭教師', '息子', 'ママ'],
    });
    assert.ok(!String(withActress.notice || '').includes('未取得同女優'), withActress.notice);
    assert.ok(!String(withActress.notice || '').includes('僅顯示主作品'), withActress.notice);
    assert.strictEqual(withActress.notice, '來源：avbase');

    const themeOnly = H.galleryFromIdentify({
      ok: true,
      code: 'DANDYA-001',
      title: '息子の家庭教師',
      related_note: dishonest + '；視覺鎖定',
      related_by_title: [
        { code: 'AAA-001', title: '系列', line: 'theme', why: '片名相近' },
      ],
    });
    assert.ok(String(themeOnly.notice || '').includes('未取得同女優'), themeOnly.notice);
    assert.ok(!String(themeOnly.notice || '').includes('僅顯示主作品'), themeOnly.notice);
    assert.ok(String(themeOnly.notice || '').includes('視覺鎖定'), themeOnly.notice);

    const empty = H.galleryFromIdentify({
      ok: true,
      code: 'DANDYA-001',
      title: '息子の家庭教師',
      related_note: dishonest,
      related_by_title: [],
    });
    assert.ok(String(empty.notice || '').includes('未取得同女優'), empty.notice);
    assert.ok(String(empty.notice || '').includes('僅顯示主作品'), empty.notice);
  }

  assert.ok(!H.workNeedsThemeKeywords({ related: related(1, 'theme'), theme_keywords: [] }));
  assert.ok(H.workNeedsThemeKeywords({ related: related(1, 'keyword'), theme_keywords: [] }));
  assert.ok(!H.workNeedsThemeKeywords({ related: related(1, 'keyword'), theme_keywords: ['眼鏡'] }));
  // Cached BOD must be refreshed from the title, even when other chips remain.
  assert.ok(H.workNeedsThemeKeywords({ related: related(1, 'keyword'), theme_keywords: ['BOD', '中出し'] }));
  assert.ok(H.workNeedsThemeKeywords({ related: [], theme_keywords: ['BOD', '中出し'] }));
  assert.ok(!H.workNeedsThemeKeywords({ related: [], theme_keywords: ['叔母', '交尾'] }));
}

// Tutor-title multi-candidate: chips sit under the 主作品 block only.
// Candidate / related cards (DANDY-893, 片名 / 關鍵字 / 同演員) do not get a chip row.
// Hint names 片名 / 關鍵字 / 同演員 only for buckets that are actually present.
{
  const cover = 'https://pics.dmm.co.jp/digital/video/dandya00001/dandya00001pl.jpg';
  const storedKws = ['家庭教師', '10秒挿入', '肉欲教育', '息子の家庭教師', '息子', 'ママ'];
  const kws = ['息子の家庭教師', '家庭教師', '10秒挿入', '肉欲教育', '息子', 'ママ'];
  const titleJa = '「今日も息子の家庭教師とセックスしています」2人きりになったら10秒で挿入';
  const titleZh = '今天又跟兒子的家教上床了';
  assert.strictEqual(H.formatDisplayTitle(titleJa, ''), titleJa);
  assert.ok(H.formatDisplayTitle(titleJa, '').indexOf('（）') === -1);
  assert.strictEqual(H.formatDisplayTitle(titleJa, titleZh), titleJa + '（' + titleZh + '）');

  const data = {
    ok: true,
    code: 'DANDYA-001',
    title: titleJa,
    title_zh: titleZh,
    actress: '大浦真奈美',
    studio: 'DANDY',
    theme_keywords: storedKws,
    keyword_queries: ['家庭教師', '10秒挿入'],
    search_mode: 'title',
    related_by_title: [
      { code: 'SER-001', title: '同系列', line: 'theme', why: '片名相近', cover: cover },
      {
        code: 'KW-010',
        title: '肉欲教育ママ',
        line: 'keyword',
        why: '關鍵字×2',
        matched_keywords: ['肉欲教育', 'ママ'],
        cover: cover,
      },
      { code: 'MDBK-434', title: '別作品', line: 'actress', why: '同演員', cover: cover },
    ],
    candidates: [
      {
        code: 'DANDYA-001',
        title: titleJa,
        title_zh: titleZh,
        actress: '大浦真奈美',
        studio: 'DANDY',
        theme_keywords: storedKws,
        keyword_queries: ['家庭教師'],
        line: 'candidate',
      },
      {
        code: 'DANDY-893',
        title: '息子の家庭教師とセックスしています',
        actress: '竹内夏希',
        studio: 'DANDY',
        theme_keywords: storedKws,
        keyword_queries: ['家庭教師', '10秒挿入'],
        line: 'candidate',
      },
    ],
  };
  const works = H.sessionWorksFromIdentify(data);
  assert.strictEqual(works.length, 2);
  assert.strictEqual(works[0].code, 'DANDYA-001');
  assert.strictEqual(works[1].code, 'DANDY-893');
  assert.strictEqual(works[0].theme_keywords.join('・'), kws.join('・'));
  assert.strictEqual(works[1].theme_keywords.join('・'), kws.join('・'));
  assert.ok(!works[1].title_zh, 'do not invent Chinese for the other candidate');
  const gallery = H.galleryFromIdentify(data);
  assert.strictEqual(gallery.items[0].themeKeywords.join('・'), kws.join('・'));
  assert.strictEqual(gallery.items[1].code, 'DANDY-893');
  assert.strictEqual(gallery.items[1].themeKeywords.join('・'), kws.join('・'));
  assert.strictEqual(
    H.formatDisplayTitle(gallery.items[0].title, gallery.items[0].titleZh),
    titleJa + '（' + titleZh + '）'
  );

  H.paintHistoryDetail({ id: 'dandy-multi', works: works });
  const blocks = walkNodes(getEl('history-detail')).filter((n) => n.className === 'work-carousel-block');
  assert.strictEqual(blocks.length, 2);
  const hint0 = walkNodes(blocks[0]).find((n) => n.className === 'work-carousel-hint');
  assert.ok(hint0 && hint0.textContent.indexOf('片名') !== -1, hint0 && hint0.textContent);
  assert.ok(hint0 && hint0.textContent.indexOf('關鍵字') !== -1, hint0 && hint0.textContent);
  assert.ok(hint0 && hint0.textContent.indexOf('同演員') !== -1, hint0 && hint0.textContent);
  const mainKids = (blocks[0].children || []).map((c) => c.className);
  const sectionAt = mainKids.indexOf('work-main-section');
  const chipBlockAt = mainKids.indexOf('kw-related-panel');
  assert.strictEqual(sectionAt, 0, mainKids.join('|'));
  assert.ok(chipBlockAt > sectionAt, mainKids.join('|'));
  const mainSection = blocks[0].children[sectionAt];
  assert.ok(walkNodes(mainSection).some((n) => n.className === 'work-carousel-track'));
  assert.ok(!walkNodes(mainSection).some((n) => n.className === 'kw-chip-row'));
  assert.ok(!walkNodes(mainSection).some((n) => n.className === 'kw-related-panel'));
  const flat = walkNodes(blocks[0]);
  const at = (cls) => flat.findIndex((n) => n.className === cls);
  const meta = flat.find((n) => n.className === 'card-meta');
  assert.ok(meta && String(meta.textContent || '').indexOf('女優') !== -1);
  assert.ok(flat.indexOf(meta) < at('work-actions'), 'actress line before copy buttons');
  assert.ok(at('work-actions') < at('cover-wrap'), 'copy buttons before cover');
  assert.ok(at('cover-wrap') < at('stills-label'), 'cover before stills');
  assert.ok(at('stills-label') < at('kw-chip-row'), 'chips below the main-work block, not between actress and cover');
  const search = flat.find((n) => String(n.className || '').indexOf('kw-search') !== -1);
  assert.ok(search && flat.indexOf(search) > at('kw-chip-row'));
  assert.ok(flat.indexOf(search) > at('cover-wrap'), '搜尋 sits with the chips under the main work');
  const mainCard = walkNodes(blocks[0]).find((n) => String(n.className || '').indexOf('card') !== -1 && String(n.className || '').indexOf('work-carousel') === -1);
  assert.ok(mainCard);
  const mainCardClasses = (mainCard.children || []).map((c) => c.className);
  assert.ok(mainCardClasses.indexOf('kw-related-panel') === -1, mainCardClasses.join('|'));
  assert.ok(mainCardClasses.indexOf('kw-chip-row') === -1, mainCardClasses.join('|'));
  assert.ok(mainCardClasses.indexOf('work-actions') >= 0);
  assert.ok(mainCardClasses.indexOf('cover-wrap') > mainCardClasses.indexOf('work-actions'));
  const mainChips = walkNodes(blocks[0]).filter((n) => n.getAttribute && n.getAttribute('data-kw') && n.className === 'kw-chip');
  assert.strictEqual(mainChips.map((c) => c.getAttribute('data-kw')).join('・'), kws.join('・'));
  assert.ok(mainChips.every((c) => c.textContent.indexOf('（') !== -1));
  const mainSlides = walkNodes(blocks[0]).filter((n) => n.className === 'work-carousel-slide');
  mainSlides.slice(1).forEach((slide, i) => {
    const inside = walkNodes(slide).filter((n) => n.getAttribute && n.getAttribute('data-kw'));
    assert.strictEqual(inside.length, 0, 'related slide ' + i);
    assert.ok(!walkNodes(slide).some((n) => n.className === 'kw-related-panel'));
  });
  const otherChips = walkNodes(blocks[1]).filter((n) => n.getAttribute && n.getAttribute('data-kw'));
  assert.strictEqual(otherChips.length, 0, 'DANDY-893 must not render chips');
  assert.ok(!walkNodes(blocks[1]).some((n) => n.className === 'kw-related-panel'));

  H.paintHistoryDetail({
    id: 'dandy-no-kw-bucket',
    works: [
      {
        code: 'DANDYA-001',
        title: titleJa,
        title_zh: titleZh,
        actress: '大浦真奈美',
        line: 'main',
        theme_keywords: storedKws,
        keyword_queries: ['家庭教師'],
        related: [
          { code: 'SER-001', title: '同系列', line: 'theme', why: '片名相近', cover: cover },
          { code: 'MDBK-434', title: '別作品', line: 'actress', why: '同演員', cover: cover },
        ],
      },
    ],
  });
  const only = walkNodes(getEl('history-detail'));
  const hint = only.find((n) => n.className === 'work-carousel-hint');
  assert.ok(hint && hint.textContent.indexOf('片名') !== -1, hint && hint.textContent);
  assert.ok(hint && hint.textContent.indexOf('同演員') !== -1, hint && hint.textContent);
  assert.ok(hint && hint.textContent.indexOf('關鍵字') === -1, hint && hint.textContent);
  const stillChips = only.filter((n) => n.getAttribute && n.getAttribute('data-kw'));
  assert.strictEqual(stillChips.map((c) => c.getAttribute('data-kw')).join('・'), kws.join('・'));
}

// Title display keeps the full Japanese string, including an official internal
// ellipsis. Footer keywords do not split mid-token. Every gallery main
// (including multi slot 2) has its own chips. The night-bus card uses the
// stored NHDTC-254 catalog title, not the vision sentence that searched it.
{
  const ja =
    '息子からの母親不倫NTR告白 ママ不倫してるよ？可憐な妻が息子の家庭教師の絶倫チ●ポにナマでイカされて何度も何度も中出しに溺れて… 弥生みづき';
  const zh = '兒子的家教';
  const shown = H.formatDisplayTitle(ja, zh);
  assert.strictEqual(shown, ja + '（' + zh + '）');
  assert.ok(shown.indexOf(ja) === 0, 'do not cut the Japanese title before （中文）');
  assert.ok(shown.indexOf('…') > 0 && shown.indexOf('…') < shown.indexOf('（'));
  assert.strictEqual(H.formatDisplayTitle(ja, ''), ja);
  assert.ok(H.formatDisplayTitle(ja, '').indexOf('（）') === -1);
  const busAtoms = H.segmentDisplayText('關鍵字相關（夜行バス）', ['夜行バス']);
  assert.ok(busAtoms.some((s) => s.atom && s.text === '夜行バス'));
  assert.ok(!busAtoms.some((s) => s.text === '夜行バ' || s.text === 'バ' || s.text === 'ス'));
  const verbAtoms = H.segmentDisplayText('ナマでイカされて何度も', []);
  assert.ok(
    verbAtoms.some((s) => s.atom && s.text.indexOf('イカ') === 0 && s.text.indexOf('されて') !== -1),
    verbAtoms.map((s) => s.text).join('|')
  );
  assert.ok(!verbAtoms.some((s) => s.text === 'イ' || s.text === 'カ'));

  const tutorJa =
    '「今日も息子の家庭教師とセックスしています」2人きりになったら10秒で挿入 ? ! 息子がすぐ隣にいるのにイケメン家庭教師のチ〇ポを握る肉欲教育ママVOL.2';
  const busJa =
    '就寝中の夜行バスで指マンされた恐怖に目を開けられず寝たふりしながらイキまくる気弱女子5 増量中出しSP';
  const busZh =
    '五個膽小的女孩在夜間巴士上睡覺時被人猥褻，嚇得睜不開眼，卻在假裝睡覺的同時失控地達到高潮——超大噴射特輯';
  const cover = 'https://pics.dmm.co.jp/digital/video/1nhdtc00254/1nhdtc00254pl.jpg';
  const tutorKws = ['息子の家庭教師', '家庭教師', '息子'];
  const busKws = ['夜行バス', '指マン', 'SP', '中出し'];
  H.paintHistoryDetail({
    id: 'multi-two-mains',
    works: [
      {
        code: 'DANDYA-001',
        title: tutorJa,
        actress: '大浦真奈美',
        studio: 'DANDY',
        line: 'main',
        cover: cover,
        theme_keywords: tutorKws,
        related: [
          { code: 'SER-001', title: '同系列', line: 'theme', why: '片名相近', cover: cover },
          { code: 'KW-010', title: '家庭教師もの', line: 'keyword', why: '關鍵字×2', cover: cover },
        ],
      },
      {
        code: 'NHDTC-254',
        title: busJa,
        title_zh: busZh,
        actress: '中城葵',
        studio: 'ナチュラルハイ',
        line: 'multi',
        cover: cover,
        theme_keywords: busKws,
        visual_mismatch: true,
        visual_note: '未核對圖片（人物／衣服／姿勢與這張上傳圖不符）',
        related: [
          { code: 'NHDTC-235', title: '夜行バス逆NTR', line: 'theme', why: '片名相近', cover: cover },
          { code: 'BUS-002', title: '夜行バスで挿入', line: 'keyword', why: '關鍵字', cover: cover },
        ],
      },
      {
        code: 'DANDY-893',
        title: '候補だけ',
        line: 'candidate',
        cover: cover,
        theme_keywords: tutorKws,
        related: [{ code: 'SER-009', title: '系列', line: 'theme', why: '片名相近', cover: cover }],
      },
    ],
  });
  const blocks = walkNodes(getEl('history-detail')).filter((n) => n.className === 'work-carousel-block');
  assert.strictEqual(blocks.length, 3);
  function chipsOf(block) {
    return walkNodes(block)
      .filter((n) => n.className === 'kw-chip' && n.getAttribute && n.getAttribute('data-kw'))
      .map((n) => n.getAttribute('data-kw'));
  }
  assert.deepStrictEqual(chipsOf(blocks[0]), tutorKws);
  assert.deepStrictEqual(chipsOf(blocks[1]), busKws);
  assert.deepStrictEqual(chipsOf(blocks[2]), []);
  blocks.slice(0, 2).forEach((block, i) => {
    const kids = (block.children || []).map((c) => c.className);
    const sectionAt = kids.indexOf('work-main-section');
    const chipAt = kids.indexOf('kw-related-panel');
    assert.ok(sectionAt === 0 && chipAt > sectionAt, 'work ' + i + ' chips under the main block');
    const slides = walkNodes(block).filter((n) => n.className === 'work-carousel-slide');
    slides.slice(1).forEach((slide) => {
      assert.strictEqual(
        walkNodes(slide).filter((n) => n.className === 'kw-chip').length,
        0
      );
    });
  });
  const title0 = walkNodes(blocks[0]).find((n) => n.className === 'card-title');
  assert.strictEqual(title0 && title0.textContent, tutorJa);
  const title1 = walkNodes(blocks[1]).find((n) => n.className === 'card-title');
  assert.strictEqual(title1 && title1.textContent, busJa + '（' + busZh + '）');
  assert.ok(title1.textContent.indexOf('小悪魔') === -1);
  assert.ok(
    walkNodes(title1).some((n) => n.className === 'cjk-atom' && n.textContent === '夜行バス'),
    '夜行バス stays one atom in the title'
  );
  const hint1 = walkNodes(blocks[1]).find((n) => n.className === 'work-carousel-hint');
  assert.ok(hint1 && hint1.textContent.indexOf('夜行バス') !== -1, hint1 && hint1.textContent);
  assert.ok(
    walkNodes(hint1).some((n) => n.className === 'cjk-atom' && n.textContent === '夜行バス'),
    hint1 && hint1.textContent
  );
  assert.ok(!walkNodes(hint1).some((n) => n.className === 'cjk-atom' && n.textContent === '夜行バ'));
  const note = walkNodes(blocks[1]).find((n) => n.className === 'card-visual-note');
  assert.ok(note && note.textContent.indexOf('未核對圖片') !== -1, note && note.textContent);
  assert.ok(!walkNodes(blocks[0]).some((n) => n.className === 'card-visual-note'));
}

// Hit keywords sit beside the 關鍵字 badge; actress notes do not split the keyword block
{
  const cover = 'https://pics.dmm.co.jp/digital/video/snis00978/snis00978pl.jpg';
  H.paintHistoryDetail({
    id: 'order-paint',
    works: [
      {
        code: 'MIDA-616',
        title: '彼女の妹のノーブラ誘惑',
        cover: 'https://pics.dmm.co.jp/digital/video/mida00616/mida00616pl.jpg',
        line: 'main',
        theme_keywords: ['巨乳', 'ノーブラ'],
        related: [
          { code: 'SNIS-978', title: '系列', line: 'theme', why: '同系列', cover: cover },
          {
            code: 'MIDA-584',
            title: '義妹',
            line: 'theme',
            why: '同女優／同レーベル；義妹挑発アピールで主題線に近い',
            cover: cover,
          },
          {
            code: 'VENX-380',
            title: 'ノーブラ巨乳叔母',
            line: 'keyword',
            why: '關鍵字×2',
            matched_keywords: ['巨乳', 'ノーブラ'],
            cover: cover,
          },
          {
            code: 'ZZZA-1241',
            title: '別作品',
            line: 'keyword',
            why: '關鍵字×1',
            cover: cover,
          },
          { code: 'MIDA-652', title: '痴女', line: 'actress', why: '同女優', cover: cover },
        ],
      },
    ],
  });
  const slides = walkNodes(getEl('history-detail')).filter((n) => n.className === 'work-carousel-slide');
  const badges = slides.slice(1).map((slide) => {
    const html = walkNodes(slide).map((n) => n._html || '').join('\n');
    if (html.indexOf('同演員') !== -1) return '同演員';
    if (html.indexOf('關鍵字') !== -1) return '關鍵字';
    if (html.indexOf('片名相近') !== -1) return '片名相近';
    return html.slice(0, 80);
  });
  assert.deepStrictEqual(badges, ['片名相近', '關鍵字', '關鍵字', '同演員', '同演員']);
  const venxNodes = walkNodes(slides[2]);
  assert.ok(venxNodes.some((n) => n.className === 'card-hit-kw'));
  const venx = venxNodes.map((n) => n.textContent || '').join('\n');
  assert.ok(venx.indexOf('巨乳') !== -1 && venx.indexOf('ノーブラ') !== -1);
  const bare = walkNodes(slides[3]);
  assert.ok(!bare.some((n) => n.className === 'card-hit-kw'), 'do not invent hit keywords');
}

// Promote a related 品番: catalog jacket, same-code gloss, no parent Chinese leak
{
  const catalog = 'https://pics.dmm.co.jp/digital/video/ssis00123/ssis00123pl.jpg';
  const still = 'https://pics.dmm.co.jp/digital/video/ssis00123/ssis00123jp-1.jpg';
  const parent = { code: 'AAAA-001', title: '舊主', titleZh: '舊主中文', line: 'multi' };
  const prior = { code: 'SSIS-123', title: '相關', title_zh: '相關自帶', line: 'theme' };
  const jacket = H.promotedGalleryWork(
    {
      ok: true,
      code: 'SSIS-123',
      title: 'ジャケット',
      cid: 'ssis00123',
      cover: 'data:image/jpeg;base64,UPLOAD',
      user_preview: 'data:image/jpeg;base64,UPLOAD',
      stills: ['data:image/jpeg;base64,STILL', still],
      related_by_title: [
        {
          code: 'SSIS-200',
          title: '次',
          line: 'keyword',
          why: '關鍵字',
          cover: 'data:image/jpeg;base64,REL',
          cid: 'ssis00200',
        },
      ],
    },
    parent,
    prior
  );
  assert.ok(jacket, 'code identify becomes a gallery main');
  assert.strictEqual(jacket.code, 'SSIS-123');
  assert.strictEqual(jacket.line, 'multi');
  assert.strictEqual(jacket.cover, catalog);
  assert.ok(jacket.cover.indexOf('data:') === -1, jacket.cover);
  assert.strictEqual(jacket.userPreview, '');
  assert.deepStrictEqual(jacket.stills, [still]);
  assert.strictEqual(jacket.titleZh, '相關自帶', 'keep this 品番 gloss when identify omits Chinese');
  assert.ok(jacket.titleZh !== '舊主中文');
  assert.strictEqual(jacket.relatedByTitle[0].cover, 'https://pics.dmm.co.jp/digital/video/ssis00200/ssis00200pl.jpg');
  assert.ok(H.workShowsKeywordChips(jacket));

  const named = H.promotedGalleryWork(
    {
      ok: true,
      code: 'SSIS-123',
      title: 'ジャケット',
      title_zh: '目錄中文',
      cover: catalog,
    },
    parent,
    prior
  );
  assert.strictEqual(named.titleZh, '目錄中文');
  assert.notStrictEqual(named.titleZh, parent.titleZh);

  assert.strictEqual(H.reliablePromoteCode({ code: '', line: 'theme' }), '');
  assert.strictEqual(H.reliablePromoteCode({ code: '片名搜尋', line: 'theme' }), '');
  assert.strictEqual(H.reliablePromoteCode({ code: '未辨識', line: 'keyword' }), '');
  assert.strictEqual(H.reliablePromoteCode({ code: 'promo-150', line: 'actress' }), 'PROMO-150');
}

(async function () {
  const cover = 'https://pics.dmm.co.jp/digital/video/aaa00001/aaa00001pl.jpg';
  const still = 'https://pics.dmm.co.jp/digital/video/aaa00001/aaa00001jp-1.jpg';
  const work = { code: 'AAA-001', cover: cover, stills: [still] };
  const jpegBytes = new Uint8Array([0xff, 0xd8, 0xff, 0xd9]);

  // Selecting chips then 搜尋 re-searches only those keywords into a bottom row
  {
    const calls = [];
    const prevFetch = context.fetch;
    context.fetch = async (url, opts) => {
      calls.push({ url: String(url), body: opts && opts.body });
      return {
        ok: true,
        json: async () => ({
          ok: true,
          keywords: ['眼鏡', '地味'],
          min_hits: 2,
          related: [
            {
              code: 'PRED-001',
              title: '地味な眼鏡の会社員',
              title_zh: '土味眼鏡公司',
              line: 'keyword',
              why: '關鍵字×2',
              cover: 'https://pics.dmm.co.jp/digital/video/pred00001/pred00001pl.jpg',
              cid: 'pred00001',
              stills: [],
            },
            {
              code: 'JUFE-271',
              title: 'same as main',
              line: 'keyword',
              why: '關鍵字×2',
              cover: 'https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271pl.jpg',
            },
          ],
        }),
      };
    };
    H.paintHistoryDetail({
      id: 'kw-search',
      works: [
        {
          code: 'JUFE-271',
          title: '地味な眼鏡では隠し切れない美人OL',
          cover: 'https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271pl.jpg',
          stills: [],
          theme_keywords: ['眼鏡', '地味', 'OL'],
          related: related(1, 'keyword'),
          line: 'main',
        },
      ],
    });
    const nodes = walkNodes(getEl('history-detail'));
    const chips = nodes.filter((n) => n.getAttribute && n.getAttribute('data-kw'));
    chips[0].click();
    chips[1].click();
    assert.strictEqual(chips[0].getAttribute('aria-pressed'), 'true');
    assert.strictEqual(chips[1].getAttribute('aria-pressed'), 'true');
    assert.ok(chips[0].classList.contains('is-on'));
    const search = nodes.find((n) => String(n.className || '').indexOf('kw-search') !== -1);
    search.click();
    await new Promise((r) => setTimeout(r, 30));
    const posted = calls.find((c) => c.url.indexOf('/api/related-by-keywords') !== -1);
    assert.ok(posted, calls.map((c) => c.url).join(' | '));
    const body = JSON.parse(posted.body);
    assert.strictEqual(body.keywords.join('・'), '眼鏡・地味');
    assert.strictEqual(body.code, 'JUFE-271');
    const after = walkNodes(getEl('history-detail'));
    const researchLabel = after
      .filter((n) => n.className === 'kw-related-label')
      .map((n) => n.textContent)
      .find((t) => t.indexOf('關鍵字再搜') === 0);
    assert.strictEqual(
      researchLabel,
      '關鍵字再搜（' + ['眼鏡', '地味'].map(H.formatKeywordChip).join('・') + '）'
    );
    const slides = after.filter((n) => n.className === 'kw-research-slide');
    assert.strictEqual(slides.length, 1, 'source work is not repeated in the re-search row');
    const slideHtml = walkNodes(slides[0]).map((n) => n._html || n.textContent || '').join('\n');
    assert.ok(slideHtml.indexOf('PRED-001') !== -1, slideHtml.slice(0, 240));
    assert.ok(slideHtml.indexOf('土味眼鏡公司') !== -1, 'Chinese title stays on the re-search card');
    context.fetch = prevFetch;
  }

  // Re-search row shows real matches up to 10 and does not pad
  {
    async function paintResearch(n) {
      const calls = [];
      context.fetch = async (url, opts) => {
        calls.push({ url: String(url), body: opts && opts.body });
        return {
          ok: true,
          json: async () => ({
            ok: true,
            related: Array.from({ length: n }, (_, i) => ({
              code: 'PRED-' + String(i + 1).padStart(3, '0'),
              title: '地味な眼鏡 ' + (i + 1),
              line: 'keyword',
              why: '關鍵字×2',
              stills: [],
            })),
          }),
        };
      };
      H.paintHistoryDetail({
        id: 'kw-cap-' + n,
        works: [
          {
            code: 'JUFE-271',
            title: '地味な眼鏡では隠し切れない美人OL',
            cover: 'https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271pl.jpg',
            stills: [],
            theme_keywords: ['眼鏡', '地味'],
            related: related(1, 'keyword'),
            line: 'main',
          },
        ],
      });
      const nodes = walkNodes(getEl('history-detail'));
      const search = nodes.find((node) => String(node.className || '').indexOf('kw-search') !== -1);
      nodes.filter((node) => node.getAttribute && node.getAttribute('data-kw')).forEach((chip) => chip.click());
      search.click();
      await new Promise((r) => setTimeout(r, 30));
      const slides = walkNodes(getEl('history-detail')).filter((node) => node.className === 'kw-research-slide');
      return slides.length;
    }
    assert.strictEqual(await paintResearch(12), 10, 're-search row caps at 10');
    assert.strictEqual(await paintResearch(3), 3, 're-search row does not pad to 10');
  }

  // appendHistoryFromIdentify stores a thumb per uploaded file
  {
    canvasThumbN = 0;
    store.lfp_identify_history_v1 = JSON.stringify([]);
    const files = [
      new File([jpegBytes], 'a.jpg', { type: 'image/jpeg' }),
      new File([jpegBytes], 'b.jpg', { type: 'image/jpeg' }),
      new File([jpegBytes], 'c.jpg', { type: 'image/jpeg' }),
    ];
    await H.appendHistoryFromIdentify(
      { ok: true, code: 'DDD-001', title: 'Multi', stills: [] },
      files
    );
    const recs = H.loadHistory();
    assert.ok(recs.length >= 1);
    assert.strictEqual(
      H.historyUserShots(recs[0]).length,
      3,
      'session stores one thumb per uploaded file: ' + H.historyUserShots(recs[0]).length
    );
  }

  function mockCdnFetch(fetched) {
    context.fetch = async (url) => {
      fetched.push(String(url));
      return {
        ok: true,
        arrayBuffer: async () => jpegBytes.slice().buffer,
      };
    };
  }

  // Prefetch via /api/cdn-file then one multi-file share — no zip, no N downloads
  {
    const fetched = [];
    const shareCalls = [];
    const toasts = [];
    mockCdnFetch(fetched);
    const innerFetch = context.fetch;
    context.fetch = async (url) => {
      toasts.push(String(getEl('lfp-toast').textContent));
      return innerFetch(url);
    };
    anchorClicks = 0;
    context.navigator.share = async (data) => {
      shareCalls.push(data);
    };
    context.navigator.canShare = (data) => !!(data && data.files && data.files.length);
    context.navigator.userActivation = { isActive: true };

    const result = await H.downloadWorkMedia(work);
    assert.strictEqual(result.ok, true);
    assert.strictEqual(result.reason, 'shared');
    assert.strictEqual(shareCalls.length, 1, 'one share sheet for the whole set');
    assert.strictEqual(shareCalls[0].files.length, 2);
    assert.strictEqual(shareCalls[0].text, undefined);
    assert.strictEqual(shareCalls[0].url, undefined);
    shareCalls[0].files.forEach((f) => {
      const name = String(f.name || '');
      assert.ok(name.endsWith('.jpg'), name);
      assert.ok(!name.endsWith('.zip'), name);
      assert.strictEqual(f.type, 'image/jpeg');
    });
    const names = shareCalls[0].files.map((f) => String(f.name || ''));
    assert.ok(names.indexOf('AAA-001-cover.jpg') !== -1, 'cover jpeg must be in the share set: ' + names.join(','));
    assert.ok(names.indexOf('AAA-001-jp-01.jpg') !== -1, names.join(','));
    assert.strictEqual(anchorClicks, 0, 'must not fire sequential <a download> clicks');
    assert.ok(fetched.length >= 2);
    fetched.forEach((u) => {
      assert.ok(u.indexOf('/api/cdn-file?') !== -1, u);
      assert.ok(u.indexOf('work-zip') === -1, u);
      assert.ok(u.indexOf('.zip') === -1, u);
    });
    assert.ok(
      toasts.some((t) => /準備中（\d+\/2）/.test(t)),
      'progress toast while prefetching: ' + toasts.join(' | ')
    );
  }

  // Lost user activation → one tappable toast, then one share (not N dialogs)
  {
    const fetched = [];
    const shareCalls = [];
    mockCdnFetch(fetched);
    anchorClicks = 0;
    context.navigator.share = async (data) => {
      shareCalls.push(data);
    };
    context.navigator.canShare = (data) => !!(data && data.files && data.files.length);
    context.navigator.userActivation = { isActive: false };

    const result = await H.downloadWorkMedia(work);
    assert.strictEqual(result.reason, 'tap');
    assert.strictEqual(shareCalls.length, 0, 'share waits for the follow-up tap');
    const toast = getEl('lfp-toast');
    assert.ok(String(toast.textContent).indexOf('點一下') !== -1, toast.textContent);
    assert.ok(toast.classList.contains('is-action'));
    toast.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.strictEqual(shareCalls.length, 1);
    assert.strictEqual(shareCalls[0].files.length, 2);
    assert.strictEqual(anchorClicks, 0);
  }

  function namesOf(files) {
    return (files || []).map((f) => String(f.name || ''));
  }

  function isJacketUrl(u) {
    return /(pl|ps)\.jpg/i.test(u) || /mono\/movie/i.test(u);
  }

  {
    const urls = H.dmmCoverVariantUrls(
      'https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271pl.jpg'
    );
    assert.ok(urls[0].indexOf('jufe00271pl.jpg') !== -1);
    assert.ok(
      urls.some((u) => u.indexOf('jufe00271ps.jpg') !== -1),
      'pl failure should try the same-cid ps jacket: ' + urls.join(',')
    );
    assert.ok(
      urls.some((u) => /mono\/movie\/adult\/jufe00271\/jufe00271pl\.jpg/.test(u)),
      'digital pl also tries mono jacket: ' + urls.join(',')
    );
    assert.ok(
      urls.every((u) => !/j[ps]-\d+\.jpg/i.test(u)),
      'must not invent jp/js stills as cover: ' + urls.join(',')
    );
    assert.ok(
      urls.some((u) => u.indexOf('pics.dmm.com/digital/video/jufe00271/') !== -1),
      'also tries pics.dmm.com digital jacket: ' + urls.join(',')
    );
    const ready = H.shareReadyMessage({
      coverExpected: true,
      coverFailed: true,
      files: [{ name: 'x-jp-01.jpg' }, { name: 'x-jp-02.jpg' }],
      failed: 1,
      total: 3,
    });
    assert.ok(/封面無法下載/.test(ready), ready);
    assert.ok(/劇照 2 張/.test(ready), ready);
    assert.ok(/失敗 1/.test(ready), ready);
    assert.ok(/點一下儲存/.test(ready), ready);
    assert.ok(!/準備完成/.test(ready), 'must not imply total success: ' + ready);
  }

  // Cover fetch fails once, then succeeds on the same /api/cdn-file retry
  {
    const fetched = [];
    const shareCalls = [];
    let coverHits = 0;
    context.fetch = async (url) => {
      const u = String(url);
      fetched.push(u);
      if (u.indexOf('aaa00001pl.jpg') !== -1) {
        coverHits += 1;
        if (coverHits === 1) {
          return { ok: false, arrayBuffer: async () => new ArrayBuffer(0) };
        }
      }
      return { ok: true, arrayBuffer: async () => jpegBytes.slice().buffer };
    };
    context.navigator.share = async (data) => {
      shareCalls.push(data);
    };
    context.navigator.canShare = (data) => !!(data && data.files && data.files.length);
    context.navigator.userActivation = { isActive: true };

    const result = await H.downloadWorkMedia(work);
    assert.strictEqual(result.ok, true);
    assert.strictEqual(result.reason, 'shared');
    assert.strictEqual(coverHits, 2, 'cover retries once via /api/cdn-file');
    assert.strictEqual(shareCalls.length, 1);
    assert.strictEqual(shareCalls[0].files.length, 2);
    assert.ok(namesOf(shareCalls[0].files).indexOf('AAA-001-cover.jpg') !== -1);
    fetched.forEach((u) => assert.ok(u.indexOf('/api/cdn-file?') !== -1, u));
  }

  // Cover never arrives: still share stills (do not block the whole set)
  {
    const shareCalls = [];
    const fetched = [];
    const toasts = [];
    context.fetch = async (url) => {
      const u = String(url);
      fetched.push(u);
      toasts.push(String(getEl('lfp-toast').textContent));
      if (isJacketUrl(u)) {
        return { ok: false, arrayBuffer: async () => new ArrayBuffer(0) };
      }
      return { ok: true, arrayBuffer: async () => jpegBytes.slice().buffer };
    };
    context.navigator.share = async (data) => {
      shareCalls.push(data);
    };
    context.navigator.canShare = (data) => !!(data && data.files && data.files.length);
    context.navigator.userActivation = { isActive: true };

    const result = await H.downloadWorkMedia(work);
    assert.strictEqual(result.ok, true, 'stills still share when cover fails');
    assert.strictEqual(result.reason, 'shared');
    assert.strictEqual(shareCalls.length, 1);
    assert.strictEqual(shareCalls[0].files.length, 1);
    assert.ok(namesOf(shareCalls[0].files).indexOf('AAA-001-jp-01.jpg') !== -1);
    assert.ok(namesOf(shareCalls[0].files).indexOf('AAA-001-cover.jpg') === -1);
    assert.ok(
      fetched.some((u) => u.indexOf('aaa00001ps.jpg') !== -1),
      'cover fail tries ps via /api/cdn-file: ' + fetched.join(' | ')
    );
    // Stills run in parallel, so in-flight toasts may still be 1/2 before the
    // jacket variants finish. Ready copy (next test) names 封面無法下載.
    assert.ok(
      toasts.every((t) => !/準備完成/.test(t) || /封面無法下載/.test(t)),
      'must not toast bare 準備完成: ' + toasts.join(' | ')
    );
  }

  // Cover fail + follow-up tap: toast names stills, includes 失敗, then one share
  {
    const shareCalls = [];
    context.fetch = async (url) => {
      const u = String(url);
      if (isJacketUrl(u)) {
        return { ok: false, arrayBuffer: async () => new ArrayBuffer(0) };
      }
      return { ok: true, arrayBuffer: async () => jpegBytes.slice().buffer };
    };
    context.navigator.share = async (data) => {
      shareCalls.push(data);
    };
    context.navigator.canShare = (data) => !!(data && data.files && data.files.length);
    context.navigator.userActivation = { isActive: false };

    const result = await H.downloadWorkMedia(work);
    assert.strictEqual(result.reason, 'tap');
    assert.strictEqual(shareCalls.length, 0);
    const toast = String(getEl('lfp-toast').textContent);
    assert.ok(/封面無法下載/.test(toast), toast);
    assert.ok(/劇照 1 張/.test(toast), toast);
    assert.ok(/失敗/.test(toast), toast);
    assert.ok(/點一下儲存/.test(toast), toast);
    assert.ok(!/準備完成/.test(toast), toast);
    getEl('lfp-toast').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.strictEqual(shareCalls.length, 1);
    assert.strictEqual(shareCalls[0].files.length, 1);
    assert.ok(namesOf(shareCalls[0].files).indexOf('AAA-001-jp-01.jpg') !== -1);
  }

  // Zero images succeeded → abort entirely
  {
    const shareCalls = [];
    context.fetch = async () => ({ ok: false, arrayBuffer: async () => new ArrayBuffer(0) });
    context.navigator.share = async (data) => {
      shareCalls.push(data);
    };
    context.navigator.canShare = (data) => !!(data && data.files && data.files.length);
    context.navigator.userActivation = { isActive: true };

    const result = await H.downloadWorkMedia(work);
    assert.strictEqual(result.ok, false);
    assert.strictEqual(shareCalls.length, 0);
    assert.ok(/封面無法下載|失敗/.test(String(getEl('lfp-toast').textContent)));
  }

  // JUFE-271: primary pl fails, same-cid ps jacket succeeds as *-cover.jpg (last in list)
  {
    const jufeCover = 'https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271pl.jpg';
    const jufeStills = Array.from({ length: 6 }, (_, i) =>
      'https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271jp-' + (i + 1) + '.jpg'
    );
    const jufeWork = { code: 'JUFE-271', cover: jufeCover, stills: jufeStills };
    const fetched = [];
    const shareCalls = [];
    context.fetch = async (url) => {
      const u = String(url);
      fetched.push(u);
      if (u.indexOf('jufe00271pl.jpg') !== -1) {
        return { ok: false, arrayBuffer: async () => new ArrayBuffer(0) };
      }
      return { ok: true, arrayBuffer: async () => jpegBytes.slice().buffer };
    };
    context.navigator.share = async (data) => {
      shareCalls.push(data);
    };
    context.navigator.canShare = (data) => !!(data && data.files && data.files.length);
    context.navigator.userActivation = { isActive: true };

    const result = await H.downloadWorkMedia(jufeWork);
    assert.strictEqual(result.ok, true);
    assert.strictEqual(shareCalls.length, 1);
    assert.strictEqual(shareCalls[0].files.length, 7, '6 stills + ps jacket');
    const names = namesOf(shareCalls[0].files);
    assert.strictEqual(names[names.length - 1], 'JUFE-271-cover.jpg', names.join(','));
    assert.strictEqual(names.filter((n) => /-jp-\d+\.jpg$/.test(n)).length, 6);
    assert.ok(
      fetched.filter((u) => u.indexOf('jufe00271pl.jpg') !== -1).length >= 2,
      'pl retried via /api/cdn-file'
    );
    assert.ok(
      fetched.some((u) => u.indexOf('/api/cdn-file?') !== -1 && u.indexOf('jufe00271ps.jpg') !== -1),
      'ps jacket via /api/cdn-file: ' + fetched.join(' | ')
    );
  }

  // All cover CDN URLs fail: export the already-displayed card <img> as CODE-cover.jpg
  {
    const jufeCover = 'https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271pl.jpg';
    const jufeWork = {
      code: 'JUFE-271',
      cover: jufeCover,
      stills: ['https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271jp-1.jpg'],
    };
    const shareCalls = [];
    const fetched = [];
    context.fetch = async (url) => {
      const u = String(url);
      fetched.push(u);
      if (u.indexOf('blob:displayed-jufe-cover') !== -1) {
        const blob = new Blob([jpegBytes], { type: 'image/jpeg' });
        return { ok: true, arrayBuffer: async () => jpegBytes.slice().buffer, blob: async () => blob };
      }
      if (isJacketUrl(u)) {
        return { ok: false, arrayBuffer: async () => new ArrayBuffer(0) };
      }
      return { ok: true, arrayBuffer: async () => jpegBytes.slice().buffer };
    };
    context.navigator.share = async (data) => {
      shareCalls.push(data);
    };
    context.navigator.canShare = (data) => !!(data && data.files && data.files.length);
    context.navigator.userActivation = { isActive: true };

    const coverImg = {
      currentSrc: 'blob:displayed-jufe-cover',
      src: jufeCover,
      alt: 'JUFE-271 封面',
      naturalWidth: 800,
      naturalHeight: 538,
      classList: { contains: () => false },
    };
    const result = await H.downloadWorkMedia(jufeWork, { coverImg: coverImg });
    assert.strictEqual(result.ok, true);
    assert.strictEqual(shareCalls.length, 1);
    const names = namesOf(shareCalls[0].files);
    assert.ok(names.indexOf('JUFE-271-cover.jpg') !== -1, names.join(','));
    assert.ok(names.indexOf('JUFE-271-jp-01.jpg') !== -1, names.join(','));
    assert.strictEqual(names[names.length - 1], 'JUFE-271-cover.jpg');
    assert.ok(fetched.some((u) => u.indexOf('blob:displayed-jufe-cover') !== -1));
  }

  // Warmed same-origin blob on the displayed <img> is used when the proxy is down
  {
    const jufeCover = 'https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271pl.jpg';
    const jufeWork = {
      code: 'JUFE-271',
      cover: jufeCover,
      stills: ['https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271jp-1.jpg'],
    };
    const shareCalls = [];
    context.fetch = async (url) => {
      const u = String(url);
      if (isJacketUrl(u)) {
        return { ok: false, arrayBuffer: async () => new ArrayBuffer(0) };
      }
      return { ok: true, arrayBuffer: async () => jpegBytes.slice().buffer };
    };
    context.navigator.share = async (data) => {
      shareCalls.push(data);
    };
    context.navigator.canShare = (data) => !!(data && data.files && data.files.length);
    context.navigator.userActivation = { isActive: true };

    const coverImg = {
      currentSrc: jufeCover,
      src: jufeCover,
      alt: 'JUFE-271 封面',
      naturalWidth: 800,
      naturalHeight: 538,
      classList: { contains: () => false },
      _lfpCoverBlob: new Blob([jpegBytes], { type: 'image/jpeg' }),
    };
    const result = await H.downloadWorkMedia(jufeWork, { coverImg: coverImg });
    assert.strictEqual(result.ok, true);
    assert.strictEqual(shareCalls.length, 1);
    const names = namesOf(shareCalls[0].files);
    assert.ok(names.indexOf('JUFE-271-cover.jpg') !== -1, names.join(','));
    assert.strictEqual(names[names.length - 1], 'JUFE-271-cover.jpg');
  }

  // Cross-origin DMM <img> must NOT be treated as a cover (tainted canvas on iOS).
  // Stills still share — cover-fail must not abort the batch (PR #9).
  {
    const jufeCover = 'https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271pl.jpg';
    const jufeWork = {
      code: 'JUFE-271',
      cover: jufeCover,
      stills: ['https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271jp-1.jpg'],
    };
    const shareCalls = [];
    context.fetch = async (url) => {
      const u = String(url);
      if (isJacketUrl(u)) {
        return { ok: false, arrayBuffer: async () => new ArrayBuffer(0) };
      }
      return { ok: true, arrayBuffer: async () => jpegBytes.slice().buffer };
    };
    context.navigator.share = async (data) => {
      shareCalls.push(data);
    };
    context.navigator.canShare = (data) => !!(data && data.files && data.files.length);
    context.navigator.userActivation = { isActive: true };

    const coverImg = {
      currentSrc: jufeCover,
      src: jufeCover,
      alt: 'JUFE-271 封面',
      naturalWidth: 800,
      naturalHeight: 538,
      classList: { contains: () => false },
      decode: async () => {},
    };
    const result = await H.downloadWorkMedia(jufeWork, { coverImg: coverImg });
    assert.strictEqual(result.ok, true, 'stills still share when DMM canvas is tainted');
    assert.strictEqual(shareCalls.length, 1);
    const names = namesOf(shareCalls[0].files);
    assert.ok(names.indexOf('JUFE-271-cover.jpg') === -1, 'must not export tainted DMM canvas: ' + names.join(','));
    assert.ok(names.indexOf('JUFE-271-jp-01.jpg') !== -1, names.join(','));
    const toast = String(getEl('lfp-toast').textContent);
    assert.ok(/封面無法下載/.test(toast) || result.reason === 'shared', toast);
  }

  // Same-origin /api/cdn-file <img> can be exported (not a DMM tainted canvas)
  {
    const jufeCover = 'https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271pl.jpg';
    const proxySrc =
      '/api/cdn-file?url=' + encodeURIComponent(jufeCover);
    const jufeWork = {
      code: 'JUFE-271',
      cover: jufeCover,
      stills: ['https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271jp-1.jpg'],
    };
    const shareCalls = [];
    context.fetch = async (url) => {
      const u = String(url);
      if (u.indexOf(proxySrc) !== -1 || (u.indexOf('/api/cdn-file') !== -1 && u.indexOf('jufe00271pl.jpg') !== -1 && !isJacketUrl(u.replace(/^.*url=/, '')))) {
        const blob = new Blob([jpegBytes], { type: 'image/jpeg' });
        return { ok: true, arrayBuffer: async () => jpegBytes.slice().buffer, blob: async () => blob };
      }
      if (isJacketUrl(u) && u.indexOf('jufe00271jp-') === -1) {
        return { ok: false, arrayBuffer: async () => new ArrayBuffer(0) };
      }
      return { ok: true, arrayBuffer: async () => jpegBytes.slice().buffer };
    };
    context.navigator.share = async (data) => {
      shareCalls.push(data);
    };
    context.navigator.canShare = (data) => !!(data && data.files && data.files.length);
    context.navigator.userActivation = { isActive: true };

    const coverImg = {
      currentSrc: proxySrc,
      src: proxySrc,
      alt: 'JUFE-271 封面',
      naturalWidth: 800,
      naturalHeight: 538,
      classList: { contains: () => false },
    };
    const result = await H.downloadWorkMedia(jufeWork, { coverImg: coverImg });
    assert.strictEqual(result.ok, true);
    assert.strictEqual(shareCalls.length, 1);
    const names = namesOf(shareCalls[0].files);
    assert.ok(names.indexOf('JUFE-271-cover.jpg') !== -1, 'same-origin proxy img: ' + names.join(','));
    assert.ok(names.indexOf('JUFE-271-jp-01.jpg') !== -1, names.join(','));
  }

  // Stills start while the jacket is still in-flight (must not stay 0/11 until cover ends)
  {
    const jufeCover = 'https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271pl.jpg';
    const jufeWork = {
      code: 'JUFE-271',
      cover: jufeCover,
      stills: ['https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271jp-1.jpg'],
    };
    const events = [];
    const shareCalls = [];
    context.fetch = async (url) => {
      const u = String(url);
      const jacket = isJacketUrl(u);
      events.push('start:' + (jacket ? 'jacket' : 'still'));
      if (jacket) {
        await new Promise((resolve) => setTimeout(resolve, 40));
        events.push('end:jacket');
        return { ok: false, arrayBuffer: async () => new ArrayBuffer(0) };
      }
      events.push('end:still');
      return { ok: true, arrayBuffer: async () => jpegBytes.slice().buffer };
    };
    context.navigator.share = async (data) => {
      shareCalls.push(data);
    };
    context.navigator.canShare = (data) => !!(data && data.files && data.files.length);
    context.navigator.userActivation = { isActive: true };

    const result = await H.downloadWorkMedia(jufeWork);
    assert.strictEqual(result.ok, true);
    assert.strictEqual(shareCalls.length, 1);
    assert.ok(namesOf(shareCalls[0].files).indexOf('JUFE-271-jp-01.jpg') !== -1);
    const stillStart = events.indexOf('start:still');
    const jacketEnd = events.indexOf('end:jacket');
    assert.ok(stillStart !== -1 && jacketEnd !== -1, events.join(' | '));
    assert.ok(
      stillStart < jacketEnd,
      'stills must prefetch before cover variants finish: ' + events.join(' | ')
    );
  }

  // HAWA-367: 1 jacket + 10 jp stills all go into one share, cover named *-cover.jpg
  {
    const hawaCover = 'https://pics.dmm.co.jp/digital/video/1hawa00367/1hawa00367pl.jpg';
    const hawaStills = Array.from({ length: 10 }, (_, i) =>
      'https://pics.dmm.co.jp/digital/video/1hawa00367/1hawa00367jp-' + (i + 1) + '.jpg'
    );
    const hawaWork = { code: 'HAWA-367', cover: hawaCover, stills: hawaStills };
    const shareCalls = [];
    const toasts = [];
    context.fetch = async (url) => {
      toasts.push(String(getEl('lfp-toast').textContent));
      return { ok: true, arrayBuffer: async () => jpegBytes.slice().buffer };
    };
    context.navigator.share = async (data) => {
      shareCalls.push(data);
    };
    context.navigator.canShare = (data) => !!(data && data.files && data.files.length);
    context.navigator.userActivation = { isActive: true };

    const result = await H.downloadWorkMedia(hawaWork);
    assert.strictEqual(result.ok, true);
    assert.strictEqual(shareCalls.length, 1);
    assert.strictEqual(shareCalls[0].files.length, 11);
    const names = namesOf(shareCalls[0].files);
    assert.ok(names.indexOf('HAWA-367-cover.jpg') !== -1, names.join(','));
    assert.strictEqual(names.filter((n) => /-jp-\d+\.jpg$/.test(n)).length, 10);
    assert.ok(
      toasts.some((t) => /準備中（\d+\/11）/.test(t)),
      'progress denominator is 11: ' + toasts.join(' | ')
    );
  }

  // One still fails: share the rest (including cover); toast shows failed count
  {
    const shareCalls = [];
    context.fetch = async (url) => {
      const u = String(url);
      if (u.indexOf('aaa00001jp-1.jpg') !== -1) {
        return { ok: false, arrayBuffer: async () => new ArrayBuffer(0) };
      }
      return { ok: true, arrayBuffer: async () => jpegBytes.slice().buffer };
    };
    context.navigator.share = async (data) => {
      shareCalls.push(data);
    };
    context.navigator.canShare = (data) => !!(data && data.files && data.files.length);
    context.navigator.userActivation = { isActive: true };

    const result = await H.downloadWorkMedia(work);
    assert.strictEqual(result.ok, true);
    assert.strictEqual(shareCalls.length, 1);
    assert.strictEqual(shareCalls[0].files.length, 1, 'cover still shared if a still fails');
    assert.ok(
      namesOf(shareCalls[0].files).some((n) => String(n) === 'AAA-001-cover.jpg'),
      'cover jpeg remains: ' + namesOf(shareCalls[0].files).join(',')
    );
  }

  // No Web Share API: toast Safari limitation, still no zip / no N downloads
  {
    const fetched = [];
    mockCdnFetch(fetched);
    anchorClicks = 0;
    delete context.navigator.share;
    delete context.navigator.canShare;
    context.navigator.userActivation = { isActive: true };

    const result = await H.downloadWorkMedia(work);
    assert.strictEqual(result.ok, false);
    assert.ok(String(getEl('lfp-toast').textContent).indexOf('Safari') !== -1);
    assert.strictEqual(anchorClicks, 0);
    fetched.forEach((u) => {
      assert.ok(u.indexOf('/api/cdn-file?') !== -1, u);
      assert.ok(u.indexOf('work-zip') === -1, u);
    });
  }

  // Manual fix: only incomplete cards (no cover and/or no title)
  {
    assert.ok(H.workNeedsManualFix({ titleOnly: true, code: '片名搜尋', title: '', cover: '' }));
    assert.ok(H.workNeedsManualFix({ code: 'AAA-001', title: '', cover: '' }));
    assert.ok(
      H.workNeedsManualFix({
        code: 'AAA-001',
        title: '',
        cover: 'https://pics.dmm.co.jp/digital/video/aaa00001/aaa00001pl.jpg',
      }),
      'cover without a name still needs a title fix'
    );
    assert.ok(
      H.workNeedsManualFix({ code: 'AAA-001', title: '地味な眼鏡', cover: '' }),
      'named work without cover needs a cover fix'
    );
    assert.ok(
      !H.workNeedsManualFix({
        code: 'AAA-001',
        title: '地味な眼鏡',
        cover: 'https://pics.dmm.co.jp/digital/video/aaa00001/aaa00001pl.jpg',
      }),
      'complete cards stay uncluttered'
    );
    assert.ok(
      H.workHasUsableCover({ cover: 'data:image/jpeg;base64,xx', titleOnly: true })
    );
  }

  {
    const merged = H.mergeManualFixIntoWork(
      { code: '片名搜尋', title: '', titleOnly: true, line: 'main', stills: [] },
      {
        ok: true,
        code: 'JUFE-271',
        title: '地味な眼鏡では隠し切れない美人OL',
        cover: 'https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271pl.jpg',
        stills: ['https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271jp-1.jpg'],
      }
    );
    assert.strictEqual(merged.code, 'JUFE-271');
    assert.ok(!merged.titleOnly);
    assert.ok(merged.cover.indexOf('jufe00271pl') !== -1);
    assert.ok(merged.title.indexOf('眼鏡') !== -1);
    const local = H.mergeManualFixIntoWork(
      { code: 'BBB-001', title: '有名無圖', cover: '', stills: [] },
      { ok: false },
      'data:image/jpeg;base64,cover'
    );
    assert.strictEqual(local.cover, 'data:image/jpeg;base64,cover');
    assert.strictEqual(local.title, '有名無圖');
  }

  {
    H.saveHistory([
      {
        id: 'h-fix',
        code: '片名搜尋',
        title: '',
        cover: '',
        works: [{ code: '片名搜尋', title: '', cover: '', related: [] }],
      },
    ]);
    const next = {
      code: 'JUFE-271',
      title: '地味な眼鏡',
      titleZh: '土味眼鏡',
      cover: 'https://pics.dmm.co.jp/digital/video/jufe00271/jufe00271pl.jpg',
      stills: [],
      line: 'main',
    };
    H.persistManualWorkFix({ code: '片名搜尋' }, next, { historyId: 'h-fix', workIndex: 0 });
    const rec = H.loadHistory().find((x) => x.id === 'h-fix');
    assert.ok(rec);
    assert.strictEqual(rec.code, 'JUFE-271');
    assert.strictEqual(rec.works[0].code, 'JUFE-271');
    assert.strictEqual(rec.works[0].title, '地味な眼鏡');
    assert.ok(String(rec.works[0].cover).indexOf('jufe00271pl') !== -1);
  }

  assert.strictEqual(H.batchQueryKeepsFrames(5, true, false), true);
  assert.strictEqual(H.batchQueryKeepsFrames(5, false, true), true);
  assert.strictEqual(H.batchQueryKeepsFrames(5, false, false), false);
  assert.strictEqual(H.batchQueryKeepsFrames(1, true, true), false);

  // Related carousel covers are catalog jackets. The upload preview stays on the main card.
  {
    const upload = 'data:image/jpeg;base64,UPLOADBYTES';
    const gallery = H.galleryFromIdentify({
      ok: true,
      code: 'CAMP-100',
      title: '巨乳水泳部員の媚薬合宿',
      cover: 'https://pics.dmm.co.jp/digital/video/camp100/camp100pl.jpg',
      user_preview: upload,
      related_by_title: [
        {
          code: 'BODY-101',
          title: '巨乳だけの合宿',
          line: 'keyword',
          cid: 'body00101',
          cover: upload,
          stills: [upload, 'https://pics.dmm.co.jp/digital/video/body101/body101jp-1.jpg'],
          user_preview: upload,
        },
      ],
    });
    const main = gallery.items[0];
    assert.strictEqual(main.cover, 'https://pics.dmm.co.jp/digital/video/camp100/camp100pl.jpg');
    assert.notStrictEqual(main.cover, upload);
    assert.ok(main.stills.every((u) => /^https:\/\//.test(u) && u.indexOf('UPLOAD') === -1));
    const stolen = H.galleryFromIdentify({
      ok: true,
      code: 'CAMP-100',
      title: '巨乳水泳部員の媚薬合宿',
      cid: 'camp100',
      cover: upload,
      stills: [upload],
      user_preview: upload,
    });
    const stolenMain = stolen.items[0];
    assert.strictEqual(
      stolenMain.cover,
      'https://pics.dmm.co.jp/digital/video/camp100/camp100pl.jpg'
    );
    assert.notStrictEqual(stolenMain.cover, upload);
    assert.notStrictEqual(stolenMain.cover, stolenMain.userPreview);
    assert.ok(stolenMain.stills.length > 0);
    stolenMain.stills.forEach((u) => {
      assert.ok(/^https:\/\//.test(u), u);
      assert.ok(u.indexOf('UPLOAD') === -1, u);
    });
    const rel = main.relatedByTitle[0];
    assert.ok(/^https:\/\//.test(rel.cover), rel.cover);
    assert.ok(rel.cover.indexOf('body00101') !== -1, rel.cover);
    assert.strictEqual(rel.userPreview, '');
    assert.ok(rel.stills.length > 0);
    rel.stills.forEach((u) => {
      assert.ok(/^https:\/\//.test(u), u);
      assert.ok(u.indexOf('UPLOAD') === -1, u);
    });
    assert.ok(rel.stills.indexOf('https://pics.dmm.co.jp/digital/video/body101/body101jp-1.jpg') !== -1);
  }

  // Swim-camp chips render under the main block, not between the title and the cover.
  {
    const cover = 'https://pics.dmm.co.jp/digital/video/camp100/camp100pl.jpg';
    const kws = ['巨乳', '水泳部', '媚薬', '合宿'];
    H.paintHistoryDetail({
      id: 'swim-chips',
      works: [
        {
          code: 'CAMP-100',
          title: '巨乳水泳部員の媚薬合宿記録',
          line: 'main',
          cover: cover,
          theme_keywords: kws,
          related: [
            {
              code: 'BODY-M',
              title: '巨乳の媚薬',
              line: 'keyword',
              why: '關鍵字',
              cover: cover,
            },
          ],
        },
      ],
    });
    const block = walkNodes(getEl('history-detail')).find((n) => n.className === 'work-carousel-block');
    assert.ok(block);
    const kids = (block.children || []).map((c) => c.className);
    assert.ok(kids.indexOf('kw-related-panel') > kids.indexOf('work-main-section'), kids.join('|'));
    const flat = walkNodes(block);
    const at = (cls) => flat.findIndex((n) => n.className === cls);
    assert.ok(at('card-title') < at('cover-wrap'));
    assert.ok(at('cover-wrap') < at('kw-chip-row'));
    const chips = flat.filter((n) => n.className === 'kw-chip' && n.getAttribute && n.getAttribute('data-kw'));
    assert.strictEqual(chips.map((c) => c.getAttribute('data-kw')).join('・'), kws.join('・'));
    assert.ok(chips.every((c) => String(c.textContent).indexOf('（') !== -1), chips.map((c) => c.textContent).join('|'));
    const card = flat.find((n) => String(n.className || '').indexOf('card') === 0);
    assert.ok(card);
    assert.ok(!(card.children || []).some((c) => c.className === 'kw-chip-row'));
  }

  // Split ノーブラ / 誘惑 chips, catalog title parentheses, actress only with a real CN name.
  {
    const cover = 'https://pics.dmm.co.jp/digital/video/mida00616/mida00616pl.jpg';
    const ja = '彼女の妹のノーブラ誘惑に負け巨乳';
    const zh = '女友妹妹的誘惑';
    H.paintHistoryDetail({
      id: 'nobura-split',
      works: [
        {
          code: 'MIDA-616',
          title: ja,
          title_zh: zh,
          actress: '福田ゆあ',
          actress_zh: '福田由愛',
          studio: 'MOODYZ',
          line: 'main',
          cover: cover,
          theme_keywords: ['ノーブラ誘惑', '巨乳'],
          related: [
            {
              code: 'REL-001',
              title: '巨乳の合宿',
              title_zh: '巨乳集訓',
              actress: '福田ゆあ',
              actress_zh: '福田由愛',
              line: 'keyword',
              why: '關鍵字',
              cover: cover,
              matched_keywords: ['ノーブラ', '誘惑'],
            },
          ],
        },
      ],
    });
    const block = walkNodes(getEl('history-detail')).find((n) => n.className === 'work-carousel-block');
    const chips = walkNodes(block).filter((n) => n.className === 'kw-chip' && n.getAttribute && n.getAttribute('data-kw'));
    assert.deepStrictEqual(chips.map((c) => c.getAttribute('data-kw')), ['ノーブラ', '誘惑', '巨乳']);
    assert.strictEqual(chips[0].textContent, 'ノーブラ（無胸罩）');
    assert.strictEqual(chips[1].textContent, '誘惑（誘惑）');
    const titles = walkNodes(block).filter((n) => n.className === 'card-title').map((n) => n.textContent);
    assert.ok(titles[0].indexOf(ja) === 0, titles[0]);
    assert.ok(titles[0].indexOf('（' + zh + '）') !== -1, titles[0]);
    assert.ok(titles[1].indexOf('巨乳の合宿（巨乳集訓）') !== -1, titles[1]);
    const actresses = walkNodes(block).filter((n) => n.className === 'card-actress');
    assert.ok(actresses.length >= 2, actresses.map((n) => n.textContent).join('|'));
    assert.ok(actresses.every((n) => n.textContent.indexOf('福田ゆあ（福田由愛）') !== -1), actresses.map((n) => n.textContent).join('|'));
    const hits = walkNodes(block).filter((n) => n.className === 'card-hit-kw').map((n) => n.textContent);
    assert.ok(hits.indexOf('ノーブラ（無胸罩）') !== -1, hits.join('|'));
    assert.ok(hits.indexOf('誘惑（誘惑）') !== -1, hits.join('|'));
  }

  // Reopening a saved related list must not refetch it down from 14 to 9.
  {
    const fourteen = Array.from({ length: 14 }, (_, i) => ({
      code: 'KEEP-' + String(i + 1).padStart(3, '0'),
      title: 'Saved ' + (i + 1),
      title_zh: '',
      line: i < 5 ? 'theme' : i < 10 ? 'keyword' : 'actress',
      why: i < 5 ? '片名相近' : i < 10 ? '關鍵字' : '同女優',
      cover: 'https://pics.dmm.co.jp/digital/video/keep00' + (i + 1) + '/kpl.jpg',
    }));
    assert.ok(!H.relatedBucketsNeedFill(fourteen, { title: '巨乳水泳部員の媚薬合宿', actress: '誰か' }));
    H.saveHistory([
      {
        id: 'freeze-14',
        ok: true,
        code: 'CAMP-100',
        title: '巨乳水泳部員の媚薬合宿記録',
        title_zh: '合宿',
        works: [
          {
            code: 'CAMP-100',
            title: '巨乳水泳部員の媚薬合宿記録',
            title_zh: '合宿',
            theme_keywords: ['巨乳', '水泳部', '媚薬', '合宿'],
            related: fourteen,
            line: 'main',
          },
        ],
      },
    ]);
    context.fetch = async () => ({
      ok: true,
      json: async () => ({
        ok: true,
        related_by_title: related(3, 'keyword').concat(related(2, 'theme'), related(1, 'actress')),
      }),
    });
    H.openHistoryDetail('freeze-14');
    await new Promise((r) => setTimeout(r, 40));
    const rec = H.loadHistory().find((x) => x.id === 'freeze-14');
    assert.ok(rec && rec.works && rec.works[0]);
    assert.strictEqual((rec.works[0].related || []).length, 14, 'saved related count stays 14');
    assert.strictEqual(rec.works[0].related[0].code, 'KEEP-001');
    assert.strictEqual(rec.works[0].related[13].code, 'KEEP-014');
  }

  // SSE died on 目錄查詢 3/4. Polling the job must walk later slots and finish.
  {
    const FALSE_TIMEOUT = ['時間不夠', '尚未查完', '尚未鎖定'];
    const shots = [
      {
        status: 'running',
        progress: {
          step: 'search',
          status: 'active',
          detail: '搜尋第 3/4 張…',
          progress: 0.68,
          phase: '目錄查詢',
        },
      },
      {
        status: 'stalled',
        stale: true,
        progress: {
          step: 'search',
          status: 'active',
          detail: '搜尋第 3/4 張…',
          progress: 0.68,
          phase: '目錄查詢',
        },
      },
      {
        status: 'running',
        progress: {
          step: 'search',
          status: 'active',
          detail: '搜尋第 4/4 張…',
          progress: 0.74,
          phase: '目錄查詢',
        },
      },
      {
        status: 'running',
        progress: {
          step: 'cover',
          status: 'active',
          detail: '抓取封面與劇照',
          progress: 0.88,
          phase: '封面鎖定',
        },
      },
      {
        status: 'done',
        progress: {
          step: 'done',
          status: 'done',
          detail: '完成，列出 4 部',
          progress: 1,
        },
        result: {
          ok: true,
          code: 'MIDA-616',
          title: '作品1',
          results: ['MIDA-616', 'APGH-012', 'JUFE-271', 'SSIS-001'].map((code) => {
            const cid = code.toLowerCase().replace('-', '');
            return {
              ok: true,
              code,
              title: code,
              cover: 'https://pics.dmm.co.jp/digital/video/' + cid + '/' + cid + 'pl.jpg',
            };
          }),
        },
      },
    ];
    let n = 0;
    context.fetch = async (url) => {
      assert.ok(String(url).indexOf('/api/identify/jobs/job_batch') !== -1);
      const job = shots[Math.min(n, shots.length - 1)];
      n += 1;
      return { ok: true, json: async () => ({ ok: true, job }) };
    };
    H.showProgress();
    const seen = [];
    const data = await H.followIdentifyJob('job_batch', (evt) => {
      seen.push(String(evt.detail || ''));
      H.applyProgressEvent(evt);
    });
    assert.ok(seen.indexOf('搜尋第 3/4 張…') !== -1, seen.join('|'));
    assert.ok(seen.indexOf('搜尋第 4/4 張…') !== -1, seen.join('|'));
    assert.ok(seen.indexOf('搜尋第 3/4 張…') < seen.indexOf('搜尋第 4/4 張…'));
    assert.ok(seen.indexOf('抓取封面與劇照') > seen.indexOf('搜尋第 4/4 張…'));
    const steps = getEl('progress-steps').children;
    const search = steps.find((row) => row.dataset && row.dataset.step === 'search');
    const cover = steps.find((row) => row.dataset && row.dataset.step === 'cover');
    assert.ok(search && !search.dataset.phase, '目錄查詢 clears once the batch moves on');
    assert.ok(cover && cover.dataset.phase === '封面鎖定');
    assert.strictEqual(getEl('progress-detail').textContent, '完成，列出 4 部');
    assert.strictEqual(data.results.length, 4);
    data.results.forEach((row) => {
      assert.ok(String(row.cover).indexOf('https://pics.dmm.co.jp/') === 0);
      assert.ok(String(row.cover).indexOf('data:') !== 0);
    });
    const blob = JSON.stringify(data) + seen.join('');
    FALSE_TIMEOUT.forEach((phrase) => assert.ok(blob.indexOf(phrase) === -1, phrase));
    assert.strictEqual(H.resumePayloadFromJob({ status: 'stalled', progress: shots[0].progress }), null);
  }

  // A poll snapshot older than the SSE high-water mark must not rewind the bar.
  {
    const FALSE_TIMEOUT = ['時間不夠', '尚未查完', '尚未鎖定'];
    H.showProgress();
    H.bindIdentifyJob('job_rewind');
    H.applyProgressEvent({
      step: 'search',
      status: 'active',
      detail: '封面鎖定第 4/4 張…',
      progress: 0.8,
      phase: '封面鎖定',
    });
    const polls = [
      {
        status: 'running',
        progress: {
          step: 'search',
          status: 'active',
          detail: '搜尋第 3/4 張…',
          progress: 0.68,
          phase: '目錄查詢',
        },
      },
      {
        status: 'running',
        progress: {
          step: 'vision',
          status: 'active',
          detail: '第 2/4 張',
          progress: 0.3,
          phase: '辨識中',
        },
      },
      {
        status: 'done',
        progress: { step: 'done', status: 'done', detail: '完成，列出 4 部', progress: 1 },
        result: {
          ok: true,
          code: 'MIDA-616',
          title: '作品1',
          cover: 'https://pics.dmm.co.jp/digital/video/mida616/mida616pl.jpg',
          visual_lock: true,
          results: [
            {
              ok: true,
              code: 'MIDA-616',
              cover: 'https://pics.dmm.co.jp/digital/video/mida616/mida616pl.jpg',
            },
          ],
        },
      },
    ];
    let n = 0;
    const prevTimeout = context.setTimeout;
    context.setTimeout = (fn) => {
      fn();
      return 0;
    };
    context.fetch = async (url) => {
      assert.ok(String(url).indexOf('/api/identify/jobs/job_rewind') !== -1);
      const job = polls[Math.min(n, polls.length - 1)];
      n += 1;
      return { ok: true, json: async () => ({ ok: true, job }) };
    };
    const seen = [];
    let data;
    try {
      data = await H.followIdentifyJob('job_rewind', (evt) => {
        seen.push(String(evt.detail || ''));
        H.applyProgressEvent(evt);
      });
    } finally {
      context.setTimeout = prevTimeout;
    }
    assert.deepStrictEqual(seen, ['完成，列出 4 部']);
    assert.strictEqual(getEl('progress-detail').textContent, '完成，列出 4 部');
    assert.strictEqual(getEl('progress-pct').textContent, '100%');
    assert.ok(seen.join('').indexOf('搜尋第 3/4') === -1);
    assert.strictEqual(data.code, 'MIDA-616');
    assert.strictEqual(data.visual_lock, true);
    assert.ok(String(data.cover).indexOf('https://pics.dmm.co.jp/') === 0);
    assert.ok(String(data.results[0].cover).indexOf('data:') !== 0);
    const blob = JSON.stringify(data) + getEl('progress-detail').textContent + seen.join('');
    FALSE_TIMEOUT.forEach((phrase) => assert.ok(blob.indexOf(phrase) === -1, phrase));
  }

  // A 4-image job poll still in flight must not paint over a new 7-image run.
  {
    const FALSE_TIMEOUT = ['時間不夠', '尚未查完', '尚未鎖定'];
    H.beginIdentifyProgress(4);
    H.showProgress(
      [
        { id: 'receive', label: '接收圖片（4 張）' },
        { id: 'search', label: '搜尋作品資料' },
        { id: 'done', label: '完成，進入畫廊' },
      ],
      4
    );
    H.bindIdentifyJob('job_old_four');
    const prevTimeout = context.setTimeout;
    const prevFetch = context.fetch;
    let release = null;
    context.setTimeout = (fn) => {
      fn();
      return 0;
    };
    context.fetch = () =>
      new Promise((resolve) => {
        release = resolve;
      });
    const applied = [];
    const pending = H.followIdentifyJob('job_old_four', (evt) => {
      applied.push(String(evt.detail || ''));
      H.applyProgressEvent(evt);
    });
    pending.catch(() => {});
    await new Promise((r) => prevTimeout(r, 20));
    assert.strictEqual(typeof release, 'function');
    H.beginIdentifyProgress(7);
    H.showProgress(
      [
        { id: 'receive', label: '接收圖片（7 張）' },
        { id: 'search', label: '搜尋作品資料' },
        { id: 'done', label: '完成，進入畫廊' },
      ],
      7
    );
    H.bindIdentifyJob('job_seven_live');
    H.applyProgressEvent({
      step: 'search',
      status: 'active',
      detail: '搜尋第 2/7 張…',
      progress: 0.55 + 0.25 * (1 / 7),
      phase: '目錄查詢',
    });
    release({
      ok: true,
      json: async () => ({
        ok: true,
        job: {
          status: 'running',
          progress: {
            step: 'search',
            status: 'active',
            detail: '搜尋第 2/4 張…',
            progress: 0.55 + 0.25 * (1 / 4),
            phase: '目錄查詢',
          },
        },
      }),
    });
    let superseded = false;
    try {
      await pending;
    } catch (err) {
      superseded = !!(err && err.superseded);
    } finally {
      context.setTimeout = prevTimeout;
      context.fetch = prevFetch;
    }
    assert.strictEqual(superseded, true);
    assert.ok(applied.indexOf('搜尋第 2/4 張…') === -1, applied.join('|'));
    assert.strictEqual(getEl('progress-detail').textContent, '搜尋第 2/7 張…');
    assert.strictEqual(getEl('progress-pct').textContent, '59%');
    const blob = getEl('progress-detail').textContent + getEl('progress-pct').textContent;
    FALSE_TIMEOUT.forEach((phrase) => assert.ok(blob.indexOf(phrase) === -1, phrase));
  }

  // Skip a slot, keep the others' related, then fill that same slot.
  {
    const relatedA = [
      {
        code: 'REL-101',
        title: 'Alpha sibling',
        line: 'keyword',
        why: '關鍵字',
        cover: 'https://pics.dmm.co.jp/digital/video/rel00101/rel00101pl.jpg',
      },
    ];
    const relatedC = [
      {
        code: 'REL-303',
        title: 'Gamma sibling',
        line: 'actress',
        why: '同女優',
        cover: 'https://pics.dmm.co.jp/digital/video/rel00303/rel00303pl.jpg',
      },
    ];
    const preview = 'data:image/jpeg;base64,abc';
    const data = {
      ok: true,
      code: 'AAA-001',
      title: 'Alpha',
      cover: 'https://pics.dmm.co.jp/digital/video/aaa00001/aaa00001pl.jpg',
      results: [
        {
          ok: true,
          code: 'AAA-001',
          title: 'Alpha',
          cover: 'https://pics.dmm.co.jp/digital/video/aaa00001/aaa00001pl.jpg',
          from_image_index: 1,
          related_by_title: relatedA,
          line: 'main',
        },
        {
          ok: false,
          skipped: true,
          code: null,
          title: null,
          cover: null,
          from_image_index: 2,
          user_preview: preview,
          related_by_title: [],
        },
        {
          ok: true,
          code: 'CCC-003',
          title: 'Gamma',
          cover: 'https://pics.dmm.co.jp/digital/video/ccc00003/ccc00003pl.jpg',
          from_image_index: 3,
          related_by_title: relatedC,
          line: 'multi',
        },
      ],
    };
    const works = H.sessionWorksFromIdentify(data);
    assert.strictEqual(works.length, 3, 'skipped slot stays in the session');
    assert.strictEqual(works[1].skipped, true);
    assert.strictEqual(works[1].code, '');
    assert.strictEqual(works[1].title, '');
    assert.strictEqual(works[1].cover, '');
    assert.strictEqual(works[1].user_preview, preview);
    assert.strictEqual(works[0].related[0].code, 'REL-101');
    assert.strictEqual(works[2].related[0].code, 'REL-303');
    assert.notStrictEqual(works[1].title, 'Alpha', 'skipped slot must not inherit the main title');

    const gallery = H.galleryFromIdentify(data);
    assert.strictEqual(gallery.items.length, 3);
    assert.strictEqual(gallery.items[1].skipped, true);
    assert.strictEqual(gallery.items[1].code, '');
    assert.strictEqual(gallery.items[1].cover, '');
    assert.strictEqual(gallery.items[1].userPreview, preview);
    assert.ok(gallery.items[1].cover !== preview, 'upload preview is not the catalog cover');
    assert.strictEqual(gallery.items[0].relatedByTitle[0].code, 'REL-101');
    assert.strictEqual(gallery.items[2].relatedByTitle[0].code, 'REL-303');

    const single = {
      ok: true,
      code: 'BBB-002',
      title: 'Beta',
      cid: 'bbb00002',
      cover: 'https://pics.dmm.co.jp/digital/video/bbb00002/bbb00002pl.jpg',
      related_by_title: [
        {
          code: 'REL-202',
          title: 'Beta sibling',
          line: 'keyword',
          why: '關鍵字',
          cover: 'https://pics.dmm.co.jp/digital/video/rel00202/rel00202pl.jpg',
        },
      ],
    };
    const merged = H.mergeSlotRetryIntoIdentify(data, 2, single);
    assert.strictEqual(merged.results[0].related_by_title, relatedA);
    assert.strictEqual(merged.results[2].related_by_title, relatedC);
    assert.strictEqual(merged.results[1].code, 'BBB-002');
    assert.strictEqual(merged.results[1].skipped, false);
    assert.strictEqual(merged.results[1].from_image_index, 2);
    assert.ok(String(merged.results[1].cover).indexOf('https://') === 0);
    assert.notStrictEqual(merged.results[1].cover, preview);

    const filled = H.galleryFromIdentify(merged);
    assert.strictEqual(filled.items[1].skipped, false);
    assert.strictEqual(filled.items[1].code, 'BBB-002');
    assert.ok(filled.items[1].cover.indexOf('https://pics.dmm.co.jp/') === 0);
    assert.strictEqual(filled.items[0].relatedByTitle[0].code, 'REL-101');
    assert.strictEqual(filled.items[2].relatedByTitle[0].code, 'REL-303');
    assert.strictEqual(filled.items[1].relatedByTitle[0].code, 'REL-202');

    const nextWorks = H.sessionWorksFromIdentify(merged);
    H.saveHistory([{ id: 'h-skip', kind: 'session', works: works, code: 'AAA-001', title: 'Alpha' }]);
    assert.strictEqual(H.replaceHistorySessionWorks('h-skip', nextWorks), true);
    const saved = H.loadHistory().find((x) => x.id === 'h-skip');
    assert.strictEqual(saved.works.length, 3);
    assert.strictEqual(saved.works[1].code, 'BBB-002');
    assert.strictEqual(saved.works[1].skipped, false);
    assert.strictEqual(saved.works[0].related[0].code, 'REL-101');
    assert.strictEqual(saved.works[2].related[0].code, 'REL-303');
    assert.strictEqual(saved.works[0].code, 'AAA-001');

    const round = H.galleryFromIdentify(H.identifyPayloadFromHistory(saved));
    assert.strictEqual(round.items[1].code, 'BBB-002');
    assert.strictEqual(round.items[1].cover.indexOf('https://') === 0, true);

    const skipOn = H.skipControlState(
      { step: 'search', status: 'active', detail: '搜尋第 2/7 張…', image_index: 2, image_count: 7 },
      7,
      'job_x'
    );
    assert.strictEqual(skipOn.visible, true);
    assert.strictEqual(skipOn.index, 2);
    assert.strictEqual(skipOn.total, 7);
    const wrongDenom = H.skipControlState(
      { step: 'search', status: 'active', detail: '搜尋第 2/5 張…', image_count: 5 },
      7,
      'job_x'
    );
    assert.strictEqual(wrongDenom.visible, false, 'denominator must stay the original upload count');
    const falseTimeout = H.skipControlState(
      { step: 'search', status: 'active', detail: '第 2/7 張時間不夠' },
      7,
      'job_x'
    );
    assert.strictEqual(falseTimeout.visible, false);
    const visionOn = H.skipControlState(
      { step: 'vision', status: 'active', detail: '辨識第 4/7 張…', image_index: 4, image_count: 7 },
      7,
      'job_x'
    );
    assert.strictEqual(visionOn.visible, true);
    assert.strictEqual(visionOn.index, 4);
  }

  // 「以此為主」 on a related card: code identify replaces that slot only.
  {
    const coverA = 'https://pics.dmm.co.jp/digital/video/promo00100/promo00100pl.jpg';
    const coverB = 'https://pics.dmm.co.jp/digital/video/promo00200/promo00200pl.jpg';
    const coverNew = 'https://pics.dmm.co.jp/digital/video/promo00150/promo00150pl.jpg';
    const stillNew = 'https://pics.dmm.co.jp/digital/video/promo00150/promo00150jp-1.jpg';
    const shot = 'data:image/jpeg;base64,usershot';
    const session = {
      ok: true,
      code: 'PROMO-100',
      title: '第一張',
      title_zh: '舊主中文',
      cover: coverA,
      results: [
        {
          ok: true,
          code: 'PROMO-100',
          title: '第一張',
          title_zh: '舊主中文',
          cover: coverA,
          line: 'main',
          related_by_title: [
            {
              code: 'PROMO-110',
              title: '第一相關',
              title_zh: '第一相關中',
              line: 'theme',
              why: '片名相近',
              cover: coverA,
            },
          ],
        },
        {
          ok: true,
          code: 'PROMO-200',
          title: '第二張',
          title_zh: '第二中文',
          cover: coverB,
          line: 'multi',
          related_by_title: [
            {
              code: 'PROMO-150',
              title: '要升主',
              title_zh: '相關自帶',
              line: 'theme',
              why: '片名相近',
              cover: coverB,
            },
          ],
        },
      ],
    };
    const works = H.sessionWorksFromIdentify(session);
    assert.strictEqual(works.length, 2);
    const rec = {
      id: 'promo-session',
      ts: Date.now(),
      kind: 'session',
      code: works[0].code,
      title: works[0].title,
      title_zh: works[0].title_zh,
      cover: works[0].cover,
      related: works[0].related,
      userShots: [shot],
      works: works,
    };
    const saved = H.loadHistory();
    saved.unshift(rec);
    H.saveHistory(saved);
    if (typeof context.scrollTo !== 'function') context.scrollTo = function () {};
    H.renderGallery(H.galleryFromIdentify(session));

    function actionLabels(node) {
      return walkNodes(node)
        .filter((n) => n.tagName === 'BUTTON' && String(n.className || '').indexOf('work-action') !== -1)
        .map((n) => n.getAttribute('aria-label'));
    }
    const blocks = walkNodes(getEl('gallery-cards')).filter((n) => n.className === 'work-carousel-block');
    assert.strictEqual(blocks.length, 2, 'two upload slots stay vertical');
    const firstSlides = walkNodes(blocks[0]).filter((n) => n.className === 'work-carousel-slide');
    const secondSlides = walkNodes(blocks[1]).filter((n) => n.className === 'work-carousel-slide');
    assert.ok(actionLabels(firstSlides[0]).indexOf('以此為主') === -1, 'main card has no promote button');
    assert.deepStrictEqual(actionLabels(firstSlides[1]).slice(-2), ['下載封面與劇照', '以此為主']);
    assert.ok(actionLabels(secondSlides[0]).indexOf('以此為主') === -1);
    const relatedLabels = actionLabels(secondSlides[1]);
    assert.deepStrictEqual(relatedLabels.slice(-2), ['下載封面與劇照', '以此為主']);
    const promoteBtn = walkNodes(secondSlides[1]).find(
      (n) => n.getAttribute && n.getAttribute('aria-label') === '以此為主'
    );
    assert.ok(promoteBtn, 'related card shows 以此為主 beside download');

    const identifyPayload = {
      ok: true,
      code: 'PROMO-150',
      title: '要升主的完整包',
      title_zh: '延伸中文',
      actress: '女優甲',
      actress_zh: '女優甲中',
      cid: 'promo00150',
      cover: coverNew,
      stills: [stillNew, 'data:image/jpeg;base64,STILL'],
      user_preview: shot,
      theme_keywords: ['巨乳', '水泳部'],
      keyword_queries: ['巨乳'],
      related_by_title: [
        {
          code: 'PROMO-888',
          title: '延伸相關',
          title_zh: '延伸相關中',
          line: 'keyword',
          why: '關鍵字',
          cover: 'https://pics.dmm.co.jp/digital/video/promo00888/promo00888pl.jpg',
          matched_keywords: ['巨乳'],
        },
      ],
    };
    const forms = [];
    function RecForm() {
      this.pairs = [];
      forms.push(this);
    }
    RecForm.prototype.append = function (k, v) {
      this.pairs.push([String(k), v]);
    };
    const prevFetch = context.fetch;
    const prevForm = context.FormData;
    context.FormData = RecForm;
    const cover110 = 'https://pics.dmm.co.jp/digital/video/promo00110/promo00110pl.jpg';
    const slot0Payload = {
      ok: true,
      code: 'PROMO-110',
      title: '第一張改由相關延伸',
      title_zh: '第一延伸',
      cid: 'promo00110',
      cover: cover110,
      user_preview: shot,
      stills: [cover110],
      theme_keywords: ['巨乳'],
      related_by_title: [
        {
          code: 'PROMO-111',
          title: '新相關',
          title_zh: '新相關中',
          line: 'theme',
          why: '片名相近',
          cover: coverA,
        },
      ],
    };
    context.fetch = async (url, opts) => {
      const u = String(url || '');
      if (u.indexOf('/api/identify/stream') !== -1) {
        return { ok: false, status: 404, headers: { get: () => '' }, body: null, json: async () => ({}) };
      }
      if (u.indexOf('/api/identify') !== -1) {
        const body = opts && opts.body;
        const codePair = body && body.pairs && body.pairs.find((p) => p[0] === 'code');
        const asked = codePair && codePair[1];
        return {
          ok: true,
          status: 200,
          headers: { get: () => 'application/json' },
          json: async () => (asked === 'PROMO-110' ? slot0Payload : identifyPayload),
        };
      }
      return { ok: false, status: 404, headers: { get: () => '' }, json: async () => ({}) };
    };
    try {
      const missed = await H.promoteRelatedToMain(
        { code: '舌技が神', line: 'theme', title: '口號' },
        null,
        { surface: 'gallery', slotIndex: 1 }
      );
      assert.strictEqual(missed.ok, false);
      assert.strictEqual(missed.reason, 'nocode');
      assert.ok(getEl('lfp-toast').textContent.indexOf('沒有可用番號') !== -1);
      assert.strictEqual(forms.length, 0, 'no code means no identify request');

      promoteBtn.click();
      const result = await H.promoteTask();
      assert.strictEqual(result.ok, true, result && result.reason);
      assert.strictEqual(result.slotIndex, 1);
      assert.ok(forms.length >= 1, 'identify was called');
      forms.forEach((form) => {
        const keys = form.pairs.map((p) => p[0]);
        assert.ok(keys.indexOf('image') === -1, keys.join(','));
        assert.ok(keys.indexOf('images') === -1, keys.join(','));
        const codePair = form.pairs.find((p) => p[0] === 'code');
        assert.ok(codePair, 'identify by code');
        assert.strictEqual(codePair[1], 'PROMO-150');
      });

      const after = walkNodes(getEl('gallery-cards')).filter((n) => n.className === 'work-carousel-block');
      assert.strictEqual(after.length, 2, 'promoting one related does not drop the other upload');
      const codes0 = walkNodes(after[0]).filter((n) => n.className === 'card-code').map((n) => n.textContent);
      const codes1 = walkNodes(after[1]).filter((n) => n.className === 'card-code').map((n) => n.textContent);
      assert.strictEqual(codes0[0], 'PROMO-100');
      assert.strictEqual(codes0[1], 'PROMO-110');
      assert.strictEqual(codes1[0], 'PROMO-150');
      assert.ok(codes1.indexOf('PROMO-888') !== -1, 'new related bucket is on the promoted main');
      const promotedMainLabels = actionLabels(
        walkNodes(after[1]).filter((n) => n.className === 'work-carousel-slide')[0]
      );
      assert.ok(promotedMainLabels.indexOf('以此為主') === -1);
      const chipText = walkNodes(after[1]).map((n) => n.textContent || '').join('\n');
      assert.ok(chipText.indexOf('巨乳（巨乳）') !== -1, chipText);
      assert.ok(chipText.indexOf('水泳部（游泳社）') !== -1, chipText);
      assert.ok(chipText.indexOf('延伸中文') !== -1, chipText);

      const stored = H.loadHistory().find((x) => x.id === 'promo-session');
      assert.ok(stored);
      assert.strictEqual(stored.works.length, 2);
      assert.strictEqual(stored.works[0].code, 'PROMO-100');
      assert.strictEqual(stored.works[0].related[0].code, 'PROMO-110');
      assert.strictEqual(stored.code, 'PROMO-100', 'session head stays the first upload');
      assert.strictEqual(stored.cover, coverA);
      assert.strictEqual(stored.userShots.length, 1);
      assert.strictEqual(stored.userShots[0], shot);
      assert.strictEqual(stored.works[1].code, 'PROMO-150');
      assert.strictEqual(stored.works[1].title_zh, '延伸中文');
      assert.notStrictEqual(stored.works[1].title_zh, '第二中文');
      assert.strictEqual(stored.works[1].cover, coverNew);
      assert.ok(stored.works[1].cover.indexOf('data:') === -1);
      assert.ok(stored.works[1].stills.indexOf(stillNew) !== -1);
      assert.ok(stored.works[1].stills.every((u) => String(u).indexOf('data:') !== 0));
      assert.ok(stored.works[1].theme_keywords.indexOf('巨乳') !== -1);
      assert.ok(stored.works[1].theme_keywords.indexOf('水泳部') !== -1);
      assert.ok(stored.works[1].related.some((r) => r.code === 'PROMO-888'));
      const replay = H.sessionWorksFromIdentify(H.identifyPayloadFromHistory(stored));
      assert.strictEqual(replay.length, 2, 'promoted related stays nested, not a new vertical row');
      assert.ok(!replay.some((w) => w.code === 'PROMO-888'));

      const toast = getEl('lfp-toast').textContent;
      assert.ok(toast.indexOf('已以 PROMO-150 為主作品') !== -1, toast);
      assert.ok(toast.indexOf('時間不夠') === -1, toast);
      assert.ok(toast.indexOf('尚未查完') === -1, toast);
      assert.ok(toast.indexOf('尚未鎖定') === -1, toast);

      const firstRelatedBtn = walkNodes(after[0]).find(
        (n) => n.getAttribute && n.getAttribute('aria-label') === '以此為主'
      );
      assert.ok(firstRelatedBtn);
      firstRelatedBtn.click();
      const slot0 = await H.promoteTask();
      assert.strictEqual(slot0.ok, true);
      assert.strictEqual(slot0.slotIndex, 0);
      const head = H.loadHistory().find((x) => x.id === 'promo-session');
      assert.strictEqual(head.works.length, 2);
      assert.strictEqual(head.works[0].code, 'PROMO-110');
      assert.strictEqual(head.works[0].title_zh, '第一延伸');
      assert.notStrictEqual(head.works[0].title_zh, '舊主中文');
      assert.strictEqual(head.works[0].cover, cover110);
      assert.ok(head.works[0].cover.indexOf('data:') === -1);
      assert.strictEqual(head.works[1].code, 'PROMO-150');
      assert.strictEqual(head.code, 'PROMO-110');
      assert.strictEqual(head.cover, cover110);
      assert.strictEqual(head.userShots.length, 1);
      assert.strictEqual(head.userShots[0], shot);

      // History detail: same button replaces that record's slot and keeps screenshots.
      const histRec = {
        id: 'promo-history',
        ts: Date.now(),
        kind: 'session',
        code: works[0].code,
        title: works[0].title,
        title_zh: works[0].title_zh,
        cover: coverA,
        related: works[0].related,
        userShots: [shot],
        works: works.map((w) => Object.assign({}, w, { related: (w.related || []).slice() })),
      };
      const list = H.loadHistory();
      list.unshift(histRec);
      H.saveHistory(list);
      assert.strictEqual(H.openHistoryDetail('promo-history'), true);
      const detailBlocks = walkNodes(getEl('history-detail')).filter((n) => n.className === 'work-carousel-block');
      assert.strictEqual(detailBlocks.length, 2);
      const detailSlides = walkNodes(detailBlocks[1]).filter((n) => n.className === 'work-carousel-slide');
      const detailBtn = walkNodes(detailSlides[1]).find(
        (n) => n.getAttribute && n.getAttribute('aria-label') === '以此為主'
      );
      assert.ok(detailBtn);
      detailBtn.click();
      const histResult = await H.promoteTask();
      assert.strictEqual(histResult.ok, true);
      assert.strictEqual(histResult.slotIndex, 1);
      const histStored = H.loadHistory().find((x) => x.id === 'promo-history');
      assert.strictEqual(histStored.works[0].code, 'PROMO-100');
      assert.strictEqual(histStored.works[1].code, 'PROMO-150');
      assert.strictEqual(histStored.userShots.length, 1);
      assert.strictEqual(histStored.userShots[0], shot);
      assert.strictEqual(histStored.code, 'PROMO-100');
      const detailText = walkNodes(getEl('history-detail')).map((n) => n.textContent || '').join('\n');
      assert.ok(detailText.indexOf('你的截圖') !== -1);
      assert.ok(detailText.indexOf('PROMO-100') !== -1);
      assert.ok(detailText.indexOf('PROMO-150') !== -1);
      assert.ok(detailText.indexOf('時間不夠') === -1);
      assert.ok(detailText.indexOf('尚未查完') === -1);
      assert.ok(detailText.indexOf('尚未鎖定') === -1);
    } finally {
      context.fetch = prevFetch;
      context.FormData = prevForm;
    }
  }

  console.log('test_history_session.js: ok');
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
