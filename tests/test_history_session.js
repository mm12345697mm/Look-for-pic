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
  assert.ok(H.workNeedsTitleZh({ code: 'AAA-001', title: 'x', title_zh: '' }));
  assert.ok(!H.workNeedsTitleZh({ code: 'AAA-001', title: 'x', title_zh: '中文' }));
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
    'ノーブラ誘惑・巨乳・彼女の妹・彼女・妹'
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
    '關鍵字相關（中出し）'
  );
}

// Keyword chips on a card that already has a 關鍵字 related section
{
  assert.strictEqual(
    H.formatKeywordListLabel('關鍵字相關', ['眼鏡', '地味', 'OL']),
    '關鍵字相關（眼鏡・地味・OL）'
  );
  assert.strictEqual(
    H.formatKeywordListLabel('關鍵字再搜', ['眼鏡', '地味']),
    '關鍵字再搜（眼鏡・地味）'
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
  assert.ok(labels.indexOf('關鍵字相關（眼鏡・地味・OL）') !== -1, labels.join('|'));
  const chips = nodes.filter((n) => n.getAttribute && n.getAttribute('data-kw'));
  assert.strictEqual(chips.map((c) => c.textContent).join('・'), '眼鏡・地味・OL');
  const hint = nodes.find((n) => n.className === 'work-carousel-hint');
  assert.ok(
    hint && hint.textContent.indexOf('關鍵字相關（眼鏡・地味・OL）') !== -1,
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
  assert.strictEqual(mainChips.map((c) => c.textContent).join('・'), kws.join('・'));
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
  assert.strictEqual(stillChips.map((c) => c.textContent).join('・'), kws.join('・'));
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
      .map((n) => n.textContent);
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
    assert.strictEqual(researchLabel, '關鍵字再搜（眼鏡・地味）');
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

  console.log('test_history_session.js: ok');
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
