"""Remember review scroll coordinates only; no business or credential I/O."""

import hashlib
import json
from pathlib import Path
import re

import streamlit.components.v1 as components


def _script():
    return Path(__file__).with_suffix(".js").read_text(encoding="utf-8")


def render_review_position(prefix, scope, revision):
    """Render after the editor in ``st.container(key='review_position_'+prefix)``.

    The container must contain just one result editor (plus its controls).
    ``scope`` identifies the original batch/context, never the filtered rows.
    Only its hash, the UI revision and scroll coordinates enter the browser
    registry. This helper performs no network requests or persistent writes.
    """
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", prefix):
        return
    try:
        scope_text = scope if isinstance(scope, str) else json.dumps(scope, ensure_ascii=False, sort_keys=True)
        payload = {"prefix": prefix, "scope": hashlib.sha256(scope_text.encode("utf-8")).hexdigest(),
                   "revision": str(revision), "marker": "st-key-review_position_" + prefix}
        config = json.dumps(payload, ensure_ascii=True).replace("<", "\\u003c")
        # The runtime must be created in the parent JS realm. A controller
        # defined in an old srcdoc iframe can retain data while all its queued
        # callbacks stop after Streamlit replaces that iframe.
        source = json.dumps(_script(), ensure_ascii=True).replace("<", "\\u003c")
        bootstrap = """
(function () {
  try {
    var frame = window.frameElement;
    if (frame) frame.setAttribute('data-review-script-ready', '3');
    var parent = window.parent;
    if (!parent.WorkbenchReviewPosition || parent.WorkbenchReviewPosition.version !== '3') {
      var script = parent.document.createElement('script');
      script.setAttribute('data-review-position-runtime', '3');
      script.textContent = SOURCE;
      (parent.document.head || parent.document.body).appendChild(script);
    }
    if (parent.WorkbenchReviewPosition && parent.WorkbenchReviewPosition.version === '3') {
      parent.WorkbenchReviewPosition.install(parent, CONFIG);
      if (frame) frame.setAttribute('data-review-parent-ready', '3');
    }
  } catch (_) {}
})();
""".replace("SOURCE", source).replace("CONFIG", config)
        components.html("<script>" + bootstrap + "</script>", height=1, width=1, scrolling=False)
    except Exception:
        # Scroll retention must never block the workbench or tag persistence.
        return
