'use strict';

/**
 * History session shape: one query → one history row; related stays nested.
 * Loads app.js in a DOM stub so we exercise the real helpers.
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

function dummyEl() {
  return {
    classList: {
      toggle() {},
      add() {},
      remove() {},
      contains() {
        return false;
      },
    },
    addEventListener() {},
    getAttribute() {
      return '';
    },
    setAttribute() {},
    removeAttribute() {},
    click() {},
    style: {},
    innerHTML: '',
    textContent: '',
    value: '',
    hidden: false,
    disabled: false,
  };
}

const dummy = dummyEl();
const context = {
  window: {},
  document: {
    getElementById() {
      return dummyEl();
    },
    addEventListener() {},
    body: dummyEl(),
  },
  localStorage: {
    getItem() {
      return null;
    },
    setItem() {},
  },
  URL: {
    createObjectURL() {
      return 'blob:test';
    },
    revokeObjectURL() {},
  },
  FormData: class FormData {},
  fetch: async () => ({ json: async () => ({}) }),
  Image: class Image {},
  confirm: () => false,
  navigator: { userAgent: 'node-test' },
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

console.log('test_history_session.js: ok');
