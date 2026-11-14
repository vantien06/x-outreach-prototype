// Small presentation helpers. The app works without this file; nothing here talks to the backend.
(function () {
  "use strict";

  // Stable avatar colour from the handle.
  function paintAvatars(root) {
    root.querySelectorAll("[data-avatar]").forEach(function (el) {
      var s = el.getAttribute("data-avatar") || "", h = 0;
      for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
      el.style.setProperty("--h", h);
    });
  }

  // "8 selected" next to the Draft button on a campaign page.
  function updateSelection() {
    var out = document.querySelector("[data-sel-count]");
    if (!out) return;
    var boxes = document.querySelectorAll('.cand-form input[name="candidate_id"]');
    var n = 0;
    boxes.forEach(function (b) { if (b.checked) n++; });
    out.textContent = boxes.length ? n + " of " + boxes.length + " selected" : "";
  }

  function updateCount(area) {
    var out = document.querySelector('[data-count-for="' + area.id + '"]');
    if (!out) return;
    var max = area.maxLength > 0 ? area.maxLength : 0;
    out.textContent = area.value.length + (max ? " / " + max : "");
    out.classList.toggle("near", max > 0 && area.value.length > max * 0.9);
  }

  // Timestamps arrive as UTC text; show them in the viewer's own time zone.
  function localTimes(root) {
    var today = new Date().toDateString();
    root.querySelectorAll("time[data-ts]").forEach(function (el) {
      var d = new Date(el.getAttribute("data-ts"));
      if (isNaN(d)) return;
      var time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      el.textContent = d.toDateString() === today ? "today " + time
        : d.toLocaleDateString([], { day: "numeric", month: "short" }) + ", " + time;
      el.title = d.toLocaleString();
    });
  }

  function init(root) {
    paintAvatars(root);
    localTimes(root);
    root.querySelectorAll("textarea[data-count]").forEach(updateCount);
    updateSelection();
  }

  document.addEventListener("input", function (e) {
    if (e.target.matches && e.target.matches("textarea[data-count]")) updateCount(e.target);
  });
  document.addEventListener("change", function (e) {
    if (e.target.name === "candidate_id") updateSelection();
  });

  // Hovering a statement in a draft highlights the evidence it is based on.
  function link(e, on) {
    var li = e.target.closest && e.target.closest("[data-claim]");
    if (!li) return;
    var card = li.closest(".card");
    var ev = card && card.querySelector('[data-evidence="' + li.getAttribute("data-claim") + '"]');
    if (ev) ev.classList.toggle("is-linked", on);
  }
  document.addEventListener("mouseover", function (e) { link(e, true); });
  document.addEventListener("mouseout", function (e) { link(e, false); });

  // The dashboard refreshes every few seconds. Skip the swap when nothing changed
  // (keeps focus and text selection), and keep each row opened or closed as the
  // viewer left it when it did.
  var lastDash = null, rowState = {};
  document.addEventListener("htmx:beforeSwap", function (e) {
    var t = e.detail.target;
    if (!t || t.id !== "dash") return;
    var body = e.detail.xhr ? e.detail.xhr.responseText : null;
    if (body !== null && body === lastDash) { e.detail.shouldSwap = false; return; }
    lastDash = body;
    t.querySelectorAll("details[data-key]").forEach(function (d) { rowState[d.getAttribute("data-key")] = d.open; });
  });
  document.addEventListener("htmx:afterSwap", function (e) {
    var t = e.detail.target;
    if (t && t.id === "dash") {
      t.querySelectorAll("details[data-key]").forEach(function (d) {
        var k = d.getAttribute("data-key");
        if (k in rowState) d.open = rowState[k];
      });
    }
    init(document);
  });

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { init(document); });
  else init(document);
})();
