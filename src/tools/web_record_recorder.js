// Page-side recorder for web_record_dom_tool.
//
// Modeled on Playwright's codegen recorder (packages/injected/src/recorder/
// recorder.ts — JsonRecordActionTool) and its selector engine
// (packages/injected/src/selectorGenerator.ts + roleUtils.ts).
//
// Persistently installed in browser_tool's Chrome context (add_init_script) +
// a binding (expose_binding "__pw_rec"). It is INERT unless
// window.__pw_recording === true, which the backend flips on per recording
// (so normal agent browsing incurs zero cost and records nothing). The backend
// calls window.__pw_mount() to show the finish button when recording starts.
//
// Filters on `event.isTrusted` (like codegen) — Playwright's own CDP-driven
// actions also produce trusted events, so this captures the AGENT's programmatic
// actions as well as human input.
//
// Role inference + accessible-name precedence ported from roleUtils.ts (common
// cases). textContent (not innerText) to avoid reflow. Selector priority follows
// selectorGenerator.ts (testid=1 > role+name=100 > placeholder=120 > label=140
// > text=180 > CSS>=500).
(function () {
  if (window.__pw_rec_installed) return;
  window.__pw_rec_installed = true;
  window.__rec__ = [];
  window.__pw_page_id = window.__pw_page_id || (Date.now().toString(36) + Math.random().toString(36).slice(2));
  // Inert by default; backend flips this on/off per recording.
  window.__pw_recording = window.__pw_recording || false;

  function flush(done) {
    if (!done) return;  // only the finish button ends recording
    // Try the binding (bonus if the context supports it) but ALWAYS set __pw_done
    // so the Python side can poll it via a plain page.evaluate — robust on
    // connect_over_cdp contexts where expose_binding may not fire.
    try { window.__pw_rec({ pageId: window.__pw_page_id, actions: window.__rec__, done: true }); } catch (e) {}
    window.__pw_recording = false;
    window.__pw_done = true;
    var b = document.querySelector("[data-pw-rec=\"1\"]"); if (b) b.remove();
  }
  function rec(a) {
    var arr = window.__rec__;
    if (a.type === "fill" && arr.length) {
      var l = arr[arr.length - 1];
      if (l.type === "fill" && l.selector === a.selector) { l.value = a.value; return; }
    }
    arr.push(a);
  }
  function norm(s) { return String(s == null ? "" : s).replace(/\s+/g, " ").trim(); }

  // ---- ARIA role inference (ported from roleUtils.ts, common cases) ----
  function explicitRole(el) {
    var r = (el.getAttribute("role") || "").split(/\s+/);
    for (var i = 0; i < r.length; i++) { if (r[i]) return r[i]; }
    return null;
  }
  function inputRole(el) {
    var t = (el.type || "").toLowerCase();
    if (t === "button" || t === "submit" || t === "reset" || t === "image" || t === "file") return "button";
    if (t === "checkbox") return "checkbox";
    if (t === "radio") return "radio";
    if (t === "number") return "spinbutton";
    if (t === "range") return "slider";
    if (t === "hidden") return null;
    if (t === "search") return "searchbox";
    return "textbox";
  }
  var NAME_FROM_CONTENT = { button: 1, link: 1, checkbox: 1, radio: 1, heading: 1, menuitem: 1, option: 1, tab: 1, switch: 1, treeitem: 1, cell: 1, row: 1 };
  function roleOf(el) {
    if (!el || el.nodeType !== 1) return null;
    var er = explicitRole(el); if (er) return er;
    var tag = el.tagName;
    switch (tag) {
      case "A": return el.hasAttribute("href") ? "link" : null;
      case "AREA": return el.hasAttribute("href") ? "link" : null;
      case "BUTTON": return "button";
      case "INPUT": return inputRole(el);
      case "TEXTAREA": return "textbox";
      case "SELECT": return (el.multiple || el.size > 1) ? "listbox" : "combobox";
      case "IMG": return "img";
      case "H1": case "H2": case "H3": case "H4": case "H5": case "H6": return "heading";
      case "UL": case "OL": return "list";
      case "LI": return "listitem";
      case "NAV": return "navigation";
      case "TABLE": return "table";
      case "OPTION": return "option";
      case "DIALOG": return "dialog";
      case "FIELDSET": return "group";
      case "DETAILS": return "group";
      case "FORM": return (el.getAttribute("aria-label") || el.getAttribute("aria-labelledby")) ? "form" : null;
      case "SECTION": return (el.getAttribute("aria-label") || el.getAttribute("aria-labelledby")) ? "region" : null;
    }
    return null;
  }

  // ---- accessible name (ported precedence from roleUtils.ts, common cases) ----
  function visibleText(el) { return norm(el.textContent || ""); }
  function refsText(el, attr) {
    var ref = el.getAttribute(attr); if (!ref) return "";
    var out = [];
    ref.split(/\s+/).forEach(function (id) { if (!id) return; var t; try { t = document.getElementById(id); } catch (e) {} if (t) out.push(visibleText(t)); });
    return norm(out.join(" "));
  }
  function labelsText(el) { try { var ls = el.labels; if (!ls || !ls.length) return ""; var out = []; for (var i = 0; i < ls.length; i++) out.push(visibleText(ls[i])); return norm(out.join(" ")); } catch (e) { return ""; } }
  function accName(el) {
    if (!el || el.nodeType !== 1) return "";
    var lb = refsText(el, "aria-labelledby"); if (lb) return lb;
    var al = norm(el.getAttribute("aria-label")); if (al) return al;
    var lt = labelsText(el); if (lt) return lt;
    if (el.tagName === "INPUT") {
      var t = (el.type || "").toLowerCase();
      if (t === "submit") return norm(el.value) || "Submit";
      if (t === "reset") return norm(el.value) || "Reset";
      if (t === "button") return norm(el.value);
      if (t === "image") return norm(el.getAttribute("alt")) || norm(el.getAttribute("title")) || "Submit";
    }
    if (el.tagName === "IMG") return norm(el.getAttribute("alt"));
    if (el.tagName === "BUTTON" || el.tagName === "SUMMARY") return visibleText(el);
    var role = roleOf(el);
    if (role && NAME_FROM_CONTENT[role]) { var t2 = visibleText(el); if (t2) return t2; }
    return norm(el.getAttribute("title"));
  }

  // ---- selector candidate generation (codegen priority + approximate uniqueness) ----
  function uniq(sel) { try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; } }
  function jsq(v) { return JSON.stringify(String(v == null ? "" : v)); }
  function cssAttr(tag, attr, v) { return tag + "[" + attr + "=\"" + String(v).replace(/["\\]/g, "\\$&") + "\"]"; }
  function cssFallback(el) {
    if (el.id) { var s = "#" + CSS.escape(el.id); if (uniq(s)) return s; }
    var tag = el.tagName.toLowerCase();
    var attrs = ["data-testid", "data-test-id", "data-test", "name", "placeholder", "type", "aria-label"];
    for (var i = 0; i < attrs.length; i++) { var v = el.getAttribute(attrs[i]); if (v != null && v !== "") { var s2 = cssAttr(tag, attrs[i], v); if (uniq(s2)) return s2; } }
    var parts = []; var cur = el;
    for (var j = 0; j < 8 && cur && cur.nodeType === 1; j++) {
      var part = cur.tagName.toLowerCase();
      if (cur.id) { parts.unshift("#" + CSS.escape(cur.id)); break; }
      var sibs = []; for (var c = cur.parentNode.firstElementChild; c; c = c.nextElementSibling) if (c.tagName === cur.tagName) sibs.push(c);
      if (sibs.length > 1) part += ":nth-of-type(" + (sibs.indexOf(cur) + 1) + ")";
      parts.unshift(part); cur = cur.parentNode;
      if (uniq(parts.join(" > "))) return parts.join(" > ");
    }
    var cand = parts.join(" > "); return uniq(cand) ? cand : null;
  }
  var UNIQ_CAP = 50000;
  function countRoleName(role, name) {
    var els = document.querySelectorAll("*");
    if (els.length > UNIQ_CAP) return 1;
    var n = 0;
    for (var i = 0; i < els.length; i++) { if (roleOf(els[i]) === role && accName(els[i]) === name) { if (++n > 1) return n; } }
    return n;
  }
  function countLeafText(text) {
    var els = document.querySelectorAll("*");
    if (els.length > UNIQ_CAP) return 1;
    var n = 0;
    for (var i = 0; i < els.length; i++) { var e = els[i]; if (e.children.length === 0 && visibleText(e) === text) { if (++n > 1) return n; } }
    return n;
  }
  function gen(el) {
    var role = roleOf(el), name = accName(el), text = visibleText(el);
    var css = cssFallback(el);
    var kind = "css", sel = css;
    var tid = el.getAttribute("data-testid") || el.getAttribute("data-test-id") || el.getAttribute("data-test");
    if (tid && uniq("[data-testid=\"" + String(tid).replace(/["\\]/g, "\\$&") + "\"]")) { kind = "testid"; sel = "get_by_test_id(" + jsq(tid) + ")"; }
    else if (role && name && countRoleName(role, name) === 1) { kind = "role"; sel = "get_by_role(" + jsq(role) + ", name=" + jsq(name) + ", exact=True)"; }
    else if ((role === "textbox" || role === "searchbox" || role === "combobox") && el.getAttribute("placeholder") && uniq(cssAttr(el.tagName.toLowerCase(), "placeholder", el.getAttribute("placeholder")))) { kind = "placeholder"; sel = "get_by_placeholder(" + jsq(el.getAttribute("placeholder")) + ", exact=True)"; }
    else if ((el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") && labelsText(el)) { kind = "label"; sel = "get_by_label(" + jsq(labelsText(el)) + ", exact=True)"; }
    else if (text && text.length <= 80 && countLeafText(text) === 1) { kind = "text"; sel = "get_by_text(" + jsq(text) + ", exact=True)"; }
    return { kind: kind, selector: sel, css: css, role: role, name: name, text: text };
  }

  function isOurUI(el) { return el && el.closest && el.closest('[data-pw-rec="1"]'); }
  function asCheckbox(el) { if (!el || el.tagName !== "INPUT") return null; var t = (el.type || "").toLowerCase(); return (t === "checkbox" || t === "radio") ? el : null; }
  function editable(el) { if (!el) return false; if (el.tagName === "TEXTAREA" || el.tagName === "INPUT") return true; return !!el.isContentEditable; }
  function shouldPress(e) {
    var key = e.key; if (typeof key !== "string") return null;
    var t = e.target;
    if (key === "Enter" && (t.tagName === "TEXTAREA" || t.isContentEditable)) return null;
    if (key === "Backspace" || key === "Delete" || key === "AltGraph") return null;
    if (key === "Shift" || key === "Control" || key === "Meta" || key === "Alt" || key === "Process") return null;
    var hasMod = e.ctrlKey || e.altKey || e.metaKey;
    if (key.length === 1 && !hasMod) return editable(t) ? null : key;
    return key;
  }
  function action(type, el, extra) { var g = gen(el); return Object.assign({ type: type, kind: g.kind, selector: g.selector, css: g.css, role: g.role, name: g.name, text: g.text }, extra || {}); }

  var lastUrl = location.href;
  if (window.__pw_recording) rec({ type: "goto", url: lastUrl });
  function maybeNav() { var u = location.href; if (u !== lastUrl) { lastUrl = u; rec({ type: "goto", url: u }); } }

  // All listeners bail out immediately when not recording (zero cost for normal browsing).
  document.addEventListener("click", function (e) {
    if (!window.__pw_recording) return;
    if (!e.isTrusted) return; var t = e.target; if (!t || isOurUI(t)) return;
    var cb = asCheckbox(t);
    if (cb) { rec(action(cb.checked ? "check" : "uncheck", t)); return; }
    rec(action("click", t)); maybeNav();
  }, true);
  document.addEventListener("input", function (e) {
    if (!window.__pw_recording) return;
    var t = e.target; if (!t || isOurUI(t)) return; var tag = t.tagName;
    if (tag === "SELECT") { rec(action("select", t, { value: t.value })); return; }
    if (tag === "INPUT" && asCheckbox(t)) return;
    if (tag === "INPUT" && (t.type || "").toLowerCase() === "file") return;
    if (tag === "INPUT" || tag === "TEXTAREA" || t.isContentEditable) { rec(action("fill", t, { value: t.isContentEditable ? t.innerText : t.value })); }
  }, true);
  document.addEventListener("keydown", function (e) {
    if (!window.__pw_recording) return;
    if (!e.isTrusted) return; var t = e.target; if (!t || isOurUI(t)) return;
    var key = shouldPress(e); if (!key) return;
    if (key === " ") key = "Space";
    rec(action("press", t, { key: key }));
  }, true);

  window.addEventListener("hashchange", maybeNav);
  window.addEventListener("popstate", maybeNav);
  setInterval(function () { if (window.__pw_recording) flush(false); }, 2000);
  window.addEventListener("pagehide", function () { if (window.__pw_recording) flush(true); });

  // Finish button — only while recording. !important styles so page CSS can't
  // hide/move it; pointer-events:auto so real mouse clicks register. Re-mounted
  // every second in case the SPA wipes body children. We never consume events.
  function mount() {
    if (!window.__pw_recording) return;
    if (!document.body || document.querySelector("[data-pw-rec=\"1\"]")) return;
    var b = document.createElement("div");
    b.setAttribute("data-pw-rec", "1");
    b.textContent = "\u23F9 \u5B8C\u6210\u5F55\u5236";
    b.style.cssText = "position:fixed !important;z-index:2147483647 !important;right:12px !important;bottom:12px !important;background:#2563eb !important;color:#fff !important;padding:10px 16px !important;border-radius:8px !important;font:14px sans-serif !important;cursor:pointer !important;box-shadow:0 2px 8px rgba(0,0,0,.3) !important;pointer-events:auto !important;";
    b.addEventListener("click", function () { flush(true); });
    document.body.appendChild(b);
  }
  window.__pw_mount = mount;  // backend calls this after flipping the flag on
  if (document.body) mount(); else document.addEventListener("DOMContentLoaded", mount);
  setInterval(function () { if (window.__pw_recording && document.body && !document.querySelector("[data-pw-rec=\"1\"]")) mount(); }, 1000);
})();
