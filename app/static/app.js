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

  function init(root) {
    paintAvatars(root);
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
  // (keeps focus and text selection), and keep opened messages open when it did.
  var lastDash = null, openKeys = [];
  document.addEventListener("htmx:beforeSwap", function (e) {
    var t = e.detail.target;
    if (!t || t.id !== "dash") return;
    var body = e.detail.xhr ? e.detail.xhr.responseText : null;
    if (body !== null && body === lastDash) { e.detail.shouldSwap = false; return; }
    lastDash = body;
    openKeys = [];
    t.querySelectorAll("details[open][data-key]").forEach(function (d) { openKeys.push(d.getAttribute("data-key")); });
  });
  document.addEventListener("htmx:afterSwap", function (e) {
    var t = e.detail.target;
    if (t && t.id === "dash") {
      openKeys.forEach(function (k) {
        var d = t.querySelector('details[data-key="' + k + '"]');
        if (d) d.open = true;
      });
    }
    init(document);
  });

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { init(document); });
  else init(document);
})();
