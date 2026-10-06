/**
 * Screen Wake Lock for an in-progress identify session.
 *
 * Hold while at least one identify session wants the screen awake.
 * The lock is acquired only when the document is visible. Browsers drop it
 * when the page hides; becoming visible again reacquires if a session is
 * still in progress. Unsupported APIs and denied requests resolve quietly.
 *
 * Loaded as a classic script (sets globals) and as a Node module for tests.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.createScreenWakeLock = api.createScreenWakeLock;
    root.createIdentifyWakeSession = api.createIdentifyWakeSession;
  }
})(typeof globalThis !== 'undefined' ? globalThis : typeof window !== 'undefined' ? window : this, function () {
  'use strict';

  function createScreenWakeLock(env) {
    env = env || {};
    var nav = env.navigator || null;
    var doc = env.document || null;
    var sentinel = null;
    var wanted = false;
    var ticket = 0;
    var pending = null;

    function isVisible() {
      if (!doc) return true;
      if (typeof doc.visibilityState === 'string') return doc.visibilityState === 'visible';
      if (typeof doc.hidden === 'boolean') return doc.hidden === false;
      return true;
    }

    function wakeApi() {
      if (!nav || !nav.wakeLock || typeof nav.wakeLock.request !== 'function') return null;
      return nav.wakeLock;
    }

    function isHeld() {
      return !!(sentinel && sentinel.released !== true);
    }

    function abandonLock(lock) {
      if (!lock || lock.released === true || typeof lock.release !== 'function') return;
      try {
        var p = lock.release();
        if (p && typeof p.catch === 'function') p.catch(function () {});
      } catch (_) {}
    }

    function dropSentinel() {
      var lock = sentinel;
      sentinel = null;
      ticket += 1;
      abandonLock(lock);
    }

    function finishAcquire(lock, myTicket) {
      if (myTicket !== ticket || !wanted || !isVisible() || !lock) {
        abandonLock(lock);
        if (!lock) return { ok: false, reason: 'denied' };
        if (myTicket !== ticket || !wanted) return { ok: false, reason: 'stale' };
        return { ok: false, reason: 'hidden' };
      }
      sentinel = lock;
      if (typeof lock.addEventListener === 'function') {
        lock.addEventListener('release', function () {
          if (sentinel === lock) sentinel = null;
        });
      }
      return { ok: true, reason: 'acquired' };
    }

    function shouldRetry(result) {
      return !!(
        result &&
        (result.reason === 'stale' || result.reason === 'hidden') &&
        wanted &&
        isVisible() &&
        !isHeld()
      );
    }

    function acquire() {
      var wake = wakeApi();
      if (!wanted) return Promise.resolve({ ok: false, reason: 'idle' });
      if (!isVisible()) return Promise.resolve({ ok: false, reason: 'hidden' });
      if (!wake) return Promise.resolve({ ok: false, reason: 'unsupported' });
      if (isHeld()) return Promise.resolve({ ok: true, reason: 'held' });
      if (pending) return pending;

      var myTicket = ticket;
      var request;
      try {
        // Call request() on this turn so a click that started identify
        // still counts as the user gesture browsers require.
        request = wake.request('screen');
      } catch (_) {
        return Promise.resolve({ ok: false, reason: 'denied' });
      }
      if (!request || typeof request.then !== 'function') {
        return Promise.resolve({ ok: false, reason: 'denied' });
      }

      var mine = null;
      mine = new Promise(function (resolve) {
        function finish(result) {
          if (pending === mine) pending = null;
          if (shouldRetry(result)) {
            resolve(acquire());
            return;
          }
          resolve(result);
        }
        request.then(
          function (lock) {
            finish(finishAcquire(lock, myTicket));
          },
          function () {
            finish({ ok: false, reason: 'denied' });
          }
        );
      });
      pending = mine;
      return mine;
    }

    function hold() {
      wanted = true;
      try {
        var p = acquire();
        if (!p || typeof p.then !== 'function') {
          return Promise.resolve(p || { ok: false, reason: 'denied' });
        }
        return p.then(
          function (result) {
            return result;
          },
          function () {
            return { ok: false, reason: 'denied' };
          }
        );
      } catch (_) {
        return Promise.resolve({ ok: false, reason: 'denied' });
      }
    }

    function release() {
      wanted = false;
      dropSentinel();
      return Promise.resolve({ ok: true, reason: 'released' });
    }

    function syncVisibility() {
      if (!wanted) return Promise.resolve({ ok: false, reason: 'idle' });
      if (!isVisible()) {
        dropSentinel();
        return Promise.resolve({ ok: false, reason: 'hidden' });
      }
      try {
        return hold();
      } catch (_) {
        return Promise.resolve({ ok: false, reason: 'denied' });
      }
    }

    if (doc && typeof doc.addEventListener === 'function') {
      doc.addEventListener('visibilitychange', function () {
        syncVisibility();
      });
    }

    return {
      hold: hold,
      release: release,
      syncVisibility: syncVisibility,
      isHeld: isHeld,
      isWanted: function () {
        return wanted;
      },
    };
  }

  /**
   * Refcount identify sessions onto one wake lock.
   * The first begin acquires; the last end releases.
   * endAll drops every session immediately (user reset / cancel).
   * A later end() for an id already cleared by endAll is a no-op.
   */
  function createIdentifyWakeSession(wake) {
    var ids = new Set();
    var seq = 0;

    function holdSafe() {
      try {
        var p = wake.hold();
        if (p && typeof p.catch === 'function') p.catch(function () {});
      } catch (_) {}
    }

    function releaseSafe() {
      try {
        var p = wake.release();
        if (p && typeof p.catch === 'function') p.catch(function () {});
      } catch (_) {}
    }

    return {
      begin: function () {
        var id = ++seq;
        var wasEmpty = ids.size === 0;
        ids.add(id);
        if (wasEmpty) holdSafe();
        return id;
      },
      end: function (id) {
        if (!ids.has(id)) return false;
        ids.delete(id);
        if (ids.size === 0) releaseSafe();
        return true;
      },
      endAll: function () {
        ids.clear();
        releaseSafe();
      },
      size: function () {
        return ids.size;
      },
    };
  }

  return {
    createScreenWakeLock: createScreenWakeLock,
    createIdentifyWakeSession: createIdentifyWakeSession,
  };
});
