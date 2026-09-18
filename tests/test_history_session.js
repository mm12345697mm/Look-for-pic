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
const context = {
  window: {},
  document: {
    getElementById(id) {
      return getEl(id);
    },
    createElement(tag) {
      return makeEl(tag);
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

console.log('test_history_session.js: ok');
