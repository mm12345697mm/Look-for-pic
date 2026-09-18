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
      return this._text;
    },
    set textContent(v) {
      this._text = String(v);
      this._html = String(v);
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
  Image: class Image {},
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

(async function () {
  const cover = 'https://pics.dmm.co.jp/digital/video/aaa00001/aaa00001pl.jpg';
  const still = 'https://pics.dmm.co.jp/digital/video/aaa00001/aaa00001jp-1.jpg';
  const work = { code: 'AAA-001', cover: cover, stills: [still] };
  const jpegBytes = new Uint8Array([0xff, 0xd8, 0xff, 0xd9]);

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
    const ready = H.shareReadyMessage({
      coverExpected: true,
      coverFailed: true,
      files: [{ name: 'x-jp-01.jpg' }, { name: 'x-jp-02.jpg' }],
      failed: 1,
      total: 3,
    });
    assert.ok(/封面失敗/.test(ready), ready);
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
    assert.ok(
      toasts.some((t) => /封面失敗/.test(t)),
      'progress mentions cover fail: ' + toasts.join(' | ')
    );
    assert.ok(
      toasts.every((t) => !/準備完成/.test(t) || /封面失敗/.test(t)),
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
    assert.ok(/封面失敗/.test(toast), toast);
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
    assert.ok(/失敗/.test(String(getEl('lfp-toast').textContent)));
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

  console.log('test_history_session.js: ok');
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
