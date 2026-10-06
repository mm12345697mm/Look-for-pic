'use strict';

/**
 * Screen Wake Lock helper: acquire, release, and reacquire with a mocked API.
 * Also loads the real app.js once to check identify holds the lock only while
 * that session is in progress and the page is visible.
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const { createScreenWakeLock, createIdentifyWakeSession } = require('../wake_lock.js');

function makeSentinel() {
  let released = false;
  const listeners = [];
  return {
    get released() {
      return released;
    },
    addEventListener(type, fn) {
      if (type === 'release') listeners.push(fn);
    },
    release() {
      if (!released) {
        released = true;
        listeners.slice().forEach((fn) => fn());
      }
      return Promise.resolve();
    },
  };
}

function makeDoc(state) {
  const listeners = {};
  const doc = {
    visibilityState: state || 'visible',
    hidden: (state || 'visible') !== 'visible',
    addEventListener(type, fn) {
      (listeners[type] = listeners[type] || []).push(fn);
    },
    emit(type) {
      (listeners[type] || []).forEach((fn) => fn());
    },
    setVisibility(next) {
      this.visibilityState = next;
      this.hidden = next !== 'visible';
      this.emit('visibilitychange');
    },
  };
  return doc;
}

function makeNav(request) {
  const calls = [];
  const nav = {
    calls,
    wakeLock: {
      request(type) {
        calls.push(type);
        return request ? request(type) : Promise.resolve(makeSentinel());
      },
    },
  };
  return nav;
}

function lockWith(doc, nav) {
  return createScreenWakeLock({
    document: doc || makeDoc('visible'),
    navigator: nav || makeNav(),
  });
}

(async function () {
  // Acquire while visible.
  {
    const nav = makeNav();
    const lock = lockWith(makeDoc('visible'), nav);
    const result = await lock.hold();
    assert.strictEqual(result.ok, true);
    assert.strictEqual(result.reason, 'acquired');
    assert.deepStrictEqual(nav.calls, ['screen']);
    assert.strictEqual(lock.isHeld(), true);
    assert.strictEqual(lock.isWanted(), true);
    const again = await lock.hold();
    assert.strictEqual(again.reason, 'held');
    assert.strictEqual(nav.calls.length, 1, 'a second hold does not request again');
  }

  // request() runs on the caller turn, before the promise settles.
  {
    let called = false;
    const nav = makeNav(() => {
      called = true;
      return Promise.resolve(makeSentinel());
    });
    const pending = lockWith(null, nav).hold();
    assert.strictEqual(called, true);
    await pending;
  }

  // Release drops the sentinel.
  {
    const nav = makeNav();
    const lock = lockWith(null, nav);
    await lock.hold();
    const released = await lock.release();
    assert.strictEqual(released.reason, 'released');
    assert.strictEqual(lock.isHeld(), false);
    assert.strictEqual(lock.isWanted(), false);
    await lock.release();
    assert.strictEqual(lock.isHeld(), false);
  }

  // Hidden page does not request. Showing it again does, if still wanted.
  {
    const doc = makeDoc('hidden');
    const nav = makeNav();
    const lock = lockWith(doc, nav);
    const hidden = await lock.hold();
    assert.strictEqual(hidden.reason, 'hidden');
    assert.strictEqual(nav.calls.length, 0);
    assert.strictEqual(lock.isWanted(), true);
    doc.setVisibility('visible');
    await new Promise((r) => setTimeout(r, 0));
    assert.strictEqual(nav.calls.length, 1);
    assert.strictEqual(lock.isHeld(), true);
  }

  // Browser drops the lock on hide. Visible again reacquires while wanted.
  {
    const doc = makeDoc('visible');
    const nav = makeNav();
    const lock = lockWith(doc, nav);
    await lock.hold();
    assert.strictEqual(lock.isHeld(), true);
    doc.setVisibility('hidden');
    assert.strictEqual(lock.isHeld(), false);
    assert.strictEqual(lock.isWanted(), true, 'identify is still in progress');
    doc.setVisibility('visible');
    await new Promise((r) => setTimeout(r, 0));
    assert.strictEqual(nav.calls.length, 2);
    assert.strictEqual(lock.isHeld(), true);
    await lock.release();
    doc.setVisibility('hidden');
    doc.setVisibility('visible');
    await new Promise((r) => setTimeout(r, 0));
    assert.strictEqual(nav.calls.length, 2, 'no reacquire after the session ended');
    assert.strictEqual(lock.isWanted(), false);
  }

  // Hide while the request is in flight, then show before it resolves.
  {
    let resolveFirst;
    let n = 0;
    const nav = makeNav(() => {
      n += 1;
      if (n === 1) {
        return new Promise((resolve) => {
          resolveFirst = resolve;
        });
      }
      return Promise.resolve(makeSentinel());
    });
    const doc = makeDoc('visible');
    const lock = lockWith(doc, nav);
    const pending = lock.hold();
    doc.setVisibility('hidden');
    const first = makeSentinel();
    resolveFirst(first);
    doc.setVisibility('visible');
    const again = await pending;
    assert.strictEqual(first.released, true, 'the lock granted while hidden is dropped');
    assert.strictEqual(again.ok, true);
    assert.strictEqual(lock.isHeld(), true);
    assert.ok(nav.calls.length >= 2);
  }

  // Release while the request is in flight does not leave the screen locked.
  {
    let resolveReq;
    const nav = makeNav(
      () =>
        new Promise((resolve) => {
          resolveReq = resolve;
        })
    );
    const lock = lockWith(null, nav);
    const pending = lock.hold();
    await lock.release();
    const sentinel = makeSentinel();
    resolveReq(sentinel);
    const result = await pending;
    assert.strictEqual(result.reason, 'stale');
    assert.strictEqual(sentinel.released, true);
    assert.strictEqual(lock.isHeld(), false);
    assert.strictEqual(lock.isWanted(), false);
  }

  // Unsupported and denied are quiet no-ops.
  {
    const doc = makeDoc('visible');
    const unsupported = createScreenWakeLock({ document: doc, navigator: {} });
    const missing = await unsupported.hold();
    assert.strictEqual(missing.ok, false);
    assert.strictEqual(missing.reason, 'unsupported');
    assert.strictEqual(unsupported.isWanted(), true);

    const throwing = makeNav(() => {
      throw new Error('NotAllowedError');
    });
    const deniedSync = lockWith(doc, throwing);
    const syncResult = await deniedSync.hold();
    assert.strictEqual(syncResult.ok, false);
    assert.strictEqual(syncResult.reason, 'denied');

    const rejecting = makeNav(() => Promise.reject(new Error('NotAllowedError')));
    const denied = lockWith(doc, rejecting);
    const deniedResult = await denied.hold();
    assert.strictEqual(deniedResult.ok, false);
    assert.strictEqual(deniedResult.reason, 'denied');
    assert.strictEqual(denied.isHeld(), false);
  }

  // Overlapping identify sessions share one lock. Reset clears all of them.
  {
    const nav = makeNav();
    const lock = lockWith(null, nav);
    const session = createIdentifyWakeSession(lock);
    const a = session.begin();
    const b = session.begin();
    await new Promise((r) => setTimeout(r, 0));
    assert.strictEqual(nav.calls.length, 1);
    assert.strictEqual(lock.isHeld(), true);
    assert.strictEqual(session.size(), 2);
    assert.strictEqual(session.end(a), true);
    assert.strictEqual(session.size(), 1);
    assert.strictEqual(lock.isHeld(), true, 'the other session still wants the screen');
    assert.strictEqual(session.end(b), true);
    assert.strictEqual(session.size(), 0);
    assert.strictEqual(lock.isHeld(), false);
    assert.strictEqual(lock.isWanted(), false);
    assert.strictEqual(session.end(a), false, 'ending an already finished id is a no-op');

    const doc = makeDoc('visible');
    const nav2 = makeNav();
    const lock2 = createScreenWakeLock({ document: doc, navigator: nav2 });
    const session2 = createIdentifyWakeSession(lock2);
    const c = session2.begin();
    const d = session2.begin();
    await new Promise((r) => setTimeout(r, 0));
    assert.strictEqual(lock2.isHeld(), true);
    session2.endAll();
    assert.strictEqual(session2.size(), 0);
    assert.strictEqual(lock2.isHeld(), false);
    assert.strictEqual(lock2.isWanted(), false);
    assert.strictEqual(session2.end(c), false);
    assert.strictEqual(session2.end(d), false);
    doc.setVisibility('hidden');
    doc.setVisibility('visible');
    await new Promise((r) => setTimeout(r, 0));
    assert.strictEqual(nav2.calls.length, 1, 'endAll stops reacquire');
  }

  await testAppHook();

  console.log('test_wake_lock.js: ok');
})().catch((err) => {
  console.error(err);
  process.exit(1);
});

function unref(id) {
  if (id && typeof id.unref === 'function') id.unref();
  return id;
}

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
      if (!child) return child;
      this.children.push(child);
      child.parentNode = this;
      return child;
    },
    insertBefore(child, ref) {
      const kids = this.children || [];
      const i = ref ? kids.indexOf(ref) : -1;
      if (i < 0) kids.push(child);
      else kids.splice(i, 0, child);
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
      this._attrs[k] = String(v);
    },
    getAttribute(k) {
      return this._attrs[k] || '';
    },
    removeAttribute(k) {
      delete this._attrs[k];
    },
    focus() {},
    click(ev) {
      const e = ev || { preventDefault() {}, stopPropagation() {} };
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

function loadApp(fetchImpl) {
  const byId = {};
  function getEl(id) {
    if (!byId[id]) byId[id] = makeEl('div', id);
    return byId[id];
  }
  const docListeners = {};
  const requests = [];
  const sentinels = [];
  function makePageSentinel() {
    let released = false;
    const listeners = [];
    const sentinel = {
      get released() {
        return released;
      },
      addEventListener(type, fn) {
        if (type === 'release') listeners.push(fn);
      },
      release() {
        if (!released) {
          released = true;
          listeners.slice().forEach((fn) => fn());
        }
        return Promise.resolve();
      },
    };
    sentinels.push(sentinel);
    return sentinel;
  }
  const document = {
    visibilityState: 'visible',
    hidden: false,
    getElementById(id) {
      return getEl(id);
    },
    createElement(tag) {
      return makeEl(tag);
    },
    addEventListener(type, fn) {
      (docListeners[type] = docListeners[type] || []).push(fn);
    },
    body: makeEl('body'),
    setVisibility(next) {
      this.visibilityState = next;
      this.hidden = next !== 'visible';
      (docListeners.visibilitychange || []).forEach((fn) => fn());
    },
  };
  const context = {
    window: {},
    document,
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
    FormData: class FormData {
      append() {}
    },
    Blob,
    File,
    fetch: fetchImpl,
    Image: class Image {
      set src(_v) {}
    },
    FileReader: class FileReader {
      readAsDataURL() {}
    },
    confirm: () => false,
    navigator: {
      userAgent: 'node-test',
      wakeLock: {
        request(type) {
          requests.push(type);
          return Promise.resolve(makePageSentinel());
        },
      },
    },
    console,
    scrollTo() {},
    scrollY: 0,
    pageYOffset: 0,
    setTimeout: (fn, ms) => unref(setTimeout(fn, ms)),
    clearTimeout,
    setInterval: (fn, ms) => unref(setInterval(fn, ms)),
    clearInterval,
    requestAnimationFrame: (fn) => unref(setTimeout(fn, 0)),
    TextEncoder,
    TextDecoder,
  };
  context.window = context;
  context.globalThis = context;
  context.self = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'wake_lock.js'), 'utf8'), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8'), context);
  assert.strictEqual(typeof context.createScreenWakeLock, 'function');
  return { context, requests, sentinels, document, getEl };
}

async function waitFor(fn, label) {
  const start = Date.now();
  while (Date.now() - start < 2000) {
    if (fn()) return;
    await new Promise((r) => setTimeout(r, 10));
  }
  throw new Error('timeout: ' + label);
}

async function testAppHook() {
  // Identify in progress holds the lock, hide releases, show reacquires, reset ends it.
  {
    const page = loadApp(async () => ({
      ok: true,
      status: 200,
      headers: { get: () => 'text/event-stream' },
      body: {
        getReader() {
          return {
            read() {
              return new Promise(() => {});
            },
            cancel() {
              return Promise.resolve();
            },
          };
        },
      },
      json: async () => ({}),
    }));
    page.context.__lfpAddFiles([{ name: 'shot.jpg', type: 'image/jpeg' }]);
    page.context.__lfpStartPending();
    assert.deepStrictEqual(page.requests, ['screen']);
    await waitFor(() => page.sentinels.length === 1 && page.sentinels[0].released === false, 'acquired');
    page.document.setVisibility('hidden');
    assert.strictEqual(page.sentinels[0].released, true);
    page.document.setVisibility('visible');
    await waitFor(() => page.requests.length === 2 && page.sentinels[1].released === false, 'reacquired');
    page.getEl('btn-reset').click();
    await waitFor(() => page.sentinels[1].released === true, 'reset releases');
    const n = page.requests.length;
    page.document.setVisibility('hidden');
    page.document.setVisibility('visible');
    await new Promise((r) => setTimeout(r, 20));
    assert.strictEqual(page.requests.length, n, 'reset ends the session, so visible does not reacquire');
  }

  // A finished identify releases the lock on its own.
  {
    const payload = {
      ok: true,
      code: 'MIDA-616',
      title: '示範',
      cover: 'https://pics.dmm.co.jp/digital/video/mida00616/mida00616pl.jpg',
      stills: [],
      related_by_title: [],
    };
    const chunk = new TextEncoder().encode(
      'data: ' + JSON.stringify({ type: 'result', data: payload, status: 200 }) + '\n\n'
    );
    const page = loadApp(async () => ({
      ok: true,
      status: 200,
      headers: { get: () => 'text/event-stream' },
      body: {
        getReader() {
          let sent = false;
          return {
            read() {
              if (sent) return Promise.resolve({ done: true, value: undefined });
              sent = true;
              return Promise.resolve({ done: false, value: chunk });
            },
            cancel() {
              return Promise.resolve();
            },
          };
        },
      },
      json: async () => payload,
    }));
    page.context.__lfpAddFiles([{ name: 'shot.jpg', type: 'image/jpeg' }]);
    page.context.__lfpStartPending();
    await waitFor(
      () => page.sentinels.length === 1 && page.sentinels[0].released === true,
      'finished identify releases'
    );
    const n = page.requests.length;
    page.document.setVisibility('hidden');
    page.document.setVisibility('visible');
    await new Promise((r) => setTimeout(r, 30));
    assert.strictEqual(page.requests.length, n);
  }
}
