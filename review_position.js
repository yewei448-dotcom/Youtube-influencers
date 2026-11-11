/* Streamlit 1.50 .dvn-scroller verified in bundled index.CFMf5_ez.js.
 * In-memory coordinates only. No storage, fetch, navigation, focus or data edits.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.WorkbenchReviewPosition = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const REGISTRY = "__workbenchReviewPositionsV1";
  const VERSION = "3";
  const MAX_WAIT = 2600;

  function visible(w, node) {
    if (!node || node.isConnected === false) return false;
    if (node.getClientRects && !node.getClientRects().length) return false;
    for (let item = node; item; item = item.parentElement) {
      if (item.hidden || item.getAttribute && item.getAttribute("aria-hidden") === "true") return false;
      const style = w.getComputedStyle ? w.getComputedStyle(item) : {};
      if (style.display === "none" || style.visibility === "hidden") return false;
    }
    return true;
  }

  function install(w, config) {
    try {
      if (!w || !w.document || !config || !/^[A-Za-z0-9_-]{1,80}$/.test(config.prefix)) return null;
      const doc = w.document;
      let registry = w[REGISTRY];
      if (!registry) registry = w[REGISTRY] = {items: new Map(), active: null};
      let existing = registry.items.get(config.prefix);
      if (existing && existing.version !== VERSION) { existing.dispose(); existing = null; }
      if (existing) { existing.update(config); return existing; }
      const controller = create(w, doc, registry, config);
      registry.items.set(config.prefix, controller);
      controller.update(config);
      return controller;
    } catch (_) { return null; }
  }

  function create(w, doc, registry, first) {
    const prefix = first.prefix;
    let config = null, wrapper = null, grid = null, saved = null, frozen = null;
    let dialog = false, dialogSequence = 0, attemptedToken = null;
    let pending = null, raf = null, checkRaf = null, missingTimer = null;
    let applying = false, disposed = false, restores = 0;
    let anchorUntil = 0, restoredGrid = null;
    let beforeAction = null, beforeActionUntil = 0;
    const history = [];
    const listeners = [];
    const now = () => w.performance && w.performance.now ? w.performance.now() : Date.now();
    const frame = fn => w.requestAnimationFrame(fn);
    const cancelFrame = id => { if (id !== null) w.cancelAnimationFrame(id); };
    const pageNodes = () => [
      ["main", doc.querySelector('[data-testid="stMain"]')],
      ["view", doc.querySelector('[data-testid="stAppViewContainer"]')],
      ["document", doc.scrollingElement]
    ].filter((pair, index, list) => pair[1] && list.findIndex(other => other[1] === pair[1]) === index);
    const dialogOpen = () => Array.from(doc.querySelectorAll('[role="dialog"]')).some(node => visible(w, node));
    const findWrapper = () => doc.getElementsByClassName(config.marker)[0] || null;
    const findGrid = node => {
      const grids = node ? node.querySelectorAll(".dvn-scroller") : [];
      return grids.length === 1 ? grids[0] : null;
    };
    const active = () => visible(w, wrapper) && registry.active === prefix;
    function trace(event) {
      history.push(event); if (history.length > 12) history.shift();
      if (!wrapper || !wrapper.setAttribute) return;
      for (const [name, value] of Object.entries({ready: VERSION, state: event, revision: config.revision,
        saved: saved ? saved.grid.top : "", frozen: frozen ? frozen.grid.top : "",
        grid: grid ? grid.scrollTop : "", restores, trace: history.join("|")})) {
        wrapper.setAttribute("data-review-position-" + name, String(value));
      }
    }
    function claimIfAvailable() {
      if (!visible(w, wrapper)) return;
      const other = registry.items.get(registry.active);
      if (!other || !other.isVisible()) registry.active = prefix;
    }
    function capture() {
      if (!grid || !visible(w, wrapper)) return saved;
      return {grid: {top: grid.scrollTop || 0, left: grid.scrollLeft || 0},
        page: pageNodes().map(([name, node]) => ({name, top: node.scrollTop || 0, left: node.scrollLeft || 0}))};
    }
    function cancelRestore(reason) {
      cancelFrame(raf); raf = null; pending = null; applying = false;
      if (reason) trace(reason);
    }
    function stopMissingTimer() {
      if (missingTimer !== null) { w.clearTimeout(missingTimer); missingTimer = null; }
    }
    function refresh() {
      wrapper = findWrapper();
      if (!wrapper) {
        cancelRestore();
        if (missingTimer === null) missingTimer = w.setTimeout(() => {
          missingTimer = null;
          if (!findWrapper()) controller.dispose();
        }, 2200);
        return;
      }
      stopMissingTimer();
      const oldGrid = grid;
      grid = findGrid(wrapper);
      if (!visible(w, wrapper)) {
        cancelRestore("hidden"); frozen = null; anchorUntil = 0;
        if (registry.active === prefix) registry.active = null;
        return;
      }
      claimIfAvailable();
      const opened = dialogOpen();
      if (opened && !dialog && active()) {
        frozen = beforeAction && now() < beforeActionUntil ? beforeAction : saved || capture();
        beforeAction = null;
        dialogSequence += 1;
        anchorUntil = now() + MAX_WAIT;
        trace("dialog-open");
      } else if (!opened && dialog && active()) {
        requestRestore(frozen || saved);
      }
      dialog = opened;
      if (grid && grid !== oldGrid && frozen && active() && now() < anchorUntil && !opened && !pending) {
        requestRestore(frozen, true);
      }
    }
    function requestRestore(position, force) {
      if (!position || !active()) return;
      const token = config.revision + ":" + dialogSequence;
      if (token === attemptedToken && !force) return;
      attemptedToken = token;
      if (pending) { pending.position = position; trace("merge"); return; }
      if (!force) anchorUntil = now() + MAX_WAIT;
      pending = {position, deadline: anchorUntil, size: "", stable: 0, target: null, quietSince: null};
      trace("queued");
      raf = frame(restoreFrame);
    }
    const clamp = (value, maximum) => Math.max(0, Math.min(Number(value) || 0, Math.max(0, maximum || 0)));
    function restoreFrame() {
      raf = null;
      if (!pending || disposed) return;
      wrapper = findWrapper();
      claimIfAvailable();
      if (!active() || now() > pending.deadline) { cancelRestore("timeout-or-inactive"); return; }
      const target = findGrid(wrapper);
      if (target && !dialogOpen()) {
        const size = [target.scrollHeight, target.clientHeight, target.scrollWidth, target.clientWidth,
          ...pageNodes().map(([, node]) => [node.scrollHeight, node.clientHeight, node.scrollWidth, node.clientWidth].join(","))].join("|");
        if (pending.target !== target) { pending.target = target; pending.stable = 0; pending.quietSince = null; trace("target-ready"); }
        pending.stable = pending.size === size ? pending.stable + 1 : 0;
        pending.size = size;
        if (pending.stable >= 2) {
          const position = pending.position;
          applying = true;
          const top = clamp(position.grid.top, target.scrollHeight - target.clientHeight);
          const left = clamp(position.grid.left, target.scrollWidth - target.clientWidth);
          const moved = Math.abs(target.scrollTop - top) > 1 || Math.abs(target.scrollLeft - left) > 1;
          target.scrollTop = top; target.scrollLeft = left;
          for (const [name, node] of pageNodes()) {
            const value = position.page.find(item => item.name === name);
            if (!value) continue;
            node.scrollTop = clamp(value.top, node.scrollHeight - node.clientHeight);
            node.scrollLeft = clamp(value.left, node.scrollWidth - node.clientWidth);
          }
          if (restoredGrid !== target || moved) { restores += 1; restoredGrid = target; pending.quietSince = now(); trace("applied"); }
          if (pending.quietSince === null) pending.quietSince = now();
          grid = target;
          // Keep the action anchor: Streamlit can close the dialog first and
          // replace the editor later. Observe a quiet interval, not one frame.
          if (now() - pending.quietSince >= 240) {
            saved = capture(); pending = null; applying = false; trace("settled");
            return;
          }
        }
      }
      raf = frame(restoreFrame);
    }
    function onScroll(event) {
      if (disposed || applying || pending || dialogOpen() || !visible(w, wrapper)) return;
      if (frozen && now() < anchorUntil) return;
      const currentGrid = findGrid(wrapper);
      // A replacement grid's reset-to-zero is not a user scroll. Keep the
      // previous snapshot until revision/dialog restoration is scheduled.
      if (currentGrid !== grid) return;
      if (event.target === grid) registry.active = prefix;
      if (!active()) return;
      if (event.target === grid || pageNodes().some(([, node]) => event.target === node) || event.target === doc || event.target === w) { saved = capture(); trace("captured"); }
    }
    function onIntent(event) {
      if (disposed || dialogOpen()) return;
      if (event.type === "keydown" && !["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "PageDown", "PageUp", "Home", "End", " ", "Tab", "Enter", "F2"].includes(event.key)) return;
      if (pending) cancelRestore("user-cancel");
      frozen = null; anchorUntil = 0;
      if (event.type === "wheel" || event.type === "touchmove" || event.type === "keydown" && !["Enter", "F2"].includes(event.key)) beforeAction = null;
      const inside = wrapper && wrapper.contains(event.target);
      if (inside && visible(w, wrapper)) {
        grid = findGrid(wrapper); registry.active = prefix; saved = capture();
        if (event.type === "pointerdown" || event.type === "keydown" && ["Enter", "F2"].includes(event.key)) {
          beforeAction = saved; beforeActionUntil = now() + 4000;
        }
        trace("user-anchor");
      }
    }
    function scheduleCheck() {
      if (disposed || checkRaf !== null) return;
      checkRaf = frame(() => { checkRaf = null; refresh(); });
    }
    for (const [type, handler] of [["scroll", onScroll], ["wheel", onIntent], ["touchmove", onIntent],
      ["pointerdown", onIntent], ["keydown", onIntent]]) {
      w.addEventListener(type, handler, true); listeners.push([type, handler]);
    }
    const observer = w.MutationObserver ? new w.MutationObserver(scheduleCheck) : null;
    if (observer && doc.body) observer.observe(doc.body, {subtree: true, childList: true, attributes: true,
      attributeFilter: ["hidden", "aria-hidden", "style"]});
    const controller = {
      version: VERSION,
      isVisible: () => visible(w, wrapper),
      update(next) {
        if (disposed) return;
        const previous = config;
        config = {prefix, marker: next.marker, scope: String(next.scope), revision: String(next.revision)};
        if (!previous || previous.scope !== config.scope) {
          cancelRestore(); frozen = null; saved = null; attemptedToken = null; dialog = false; anchorUntil = 0; beforeAction = null;
          refresh(); saved = capture(); trace("scope-ready");
          return;
        }
        // Do not capture the freshly rebuilt zero positions on an epoch change.
        const position = frozen || saved;
        refresh();
        if (previous.revision !== config.revision) { trace("revision"); requestRestore(position); }
      },
      inspect: () => ({scope: config.scope, revision: config.revision, pending: !!pending,
        restores, disposed, saved: saved ? JSON.parse(JSON.stringify(saved)) : null}),
      dispose() {
        if (disposed) return;
        disposed = true; cancelRestore(); cancelFrame(checkRaf); checkRaf = null; stopMissingTimer();
        for (const [type, handler] of listeners) w.removeEventListener(type, handler, true);
        if (observer) observer.disconnect();
        if (registry.items.get(prefix) === controller) registry.items.delete(prefix);
        if (registry.active === prefix) registry.active = null;
      }
    };
    return controller;
  }
  return {install, version: VERSION};
});
