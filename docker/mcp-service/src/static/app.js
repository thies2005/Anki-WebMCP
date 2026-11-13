(function () {
  "use strict";

  function fallbackCopy(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.left = "-9999px";
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (_err) {
      ok = false;
    }
    document.body.removeChild(area);
    return ok;
  }

  function showCopied(btn) {
    var statusId = btn.getAttribute("data-copy-status");
    var status = statusId ? document.getElementById(statusId) : null;
    if (status) {
      status.textContent = "Copied!";
      window.setTimeout(function () {
        if (status.textContent === "Copied!") {
          status.textContent = "";
        }
      }, 2000);
    } else {
      var orig = btn.textContent;
      btn.textContent = "Copied!";
      window.setTimeout(function () {
        btn.textContent = orig;
      }, 2000);
    }
  }

  document.querySelectorAll("[data-copy-target]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var targetId = btn.getAttribute("data-copy-target");
      var el = targetId ? document.getElementById(targetId) : null;
      if (!el) return;
      var text = el.textContent || "";
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(
          function () { showCopied(btn); },
          function () { if (fallbackCopy(text)) showCopied(btn); }
        );
      } else if (fallbackCopy(text)) {
        showCopied(btn);
      }
    });
  });

  // Auto-populate endpoint-aware MCP snippets with current origin.
  var origin = window.location.origin;
  document.querySelectorAll("[data-origin-template]").forEach(function (el) {
    var tpl = el.getAttribute("data-origin-template") || "";
    el.textContent = tpl.replaceAll("__ORIGIN__", origin);
  });

  // Native <dialog> confirmation for high-impact POST actions.
  var dialog = document.getElementById("confirm-dialog");
  if (dialog && typeof dialog.showModal === "function") {
    var dlgTitle = document.getElementById("confirm-dialog-title");
    var dlgMsg = document.getElementById("confirm-dialog-message");
    var dlgOk = document.getElementById("confirm-dialog-ok");
    var dlgCancel = document.getElementById("confirm-dialog-cancel");
    var pendingForm = null;

    document.querySelectorAll("form[data-confirm-message]").forEach(function (form) {
      form.addEventListener("submit", function (ev) {
        if (form.dataset.confirmed === "1") {
          return;
        }
        ev.preventDefault();
        pendingForm = form;
        if (dlgTitle) dlgTitle.textContent = form.getAttribute("data-confirm-title") || "Confirm action";
        if (dlgMsg) dlgMsg.textContent = form.getAttribute("data-confirm-message") || "";
        if (dlgOk) dlgOk.textContent = form.getAttribute("data-confirm-cta") || "Confirm";
        dialog.showModal();
      });
    });

    if (dlgCancel) {
      dlgCancel.addEventListener("click", function () {
        pendingForm = null;
        dialog.close();
      });
    }
    if (dlgOk) {
      dlgOk.addEventListener("click", function () {
        var form = pendingForm;
        pendingForm = null;
        dialog.close();
        if (form) {
          form.dataset.confirmed = "1";
          form.submit();
        }
      });
    }
  }

  // Deck tree filter and expand/collapse controls.
  var deckFilter = document.getElementById("deck-filter");
  var deckNodes = Array.prototype.slice.call(document.querySelectorAll(".decknode"));
  if (deckFilter && deckNodes.length > 0) {
    deckFilter.addEventListener("input", function () {
      var q = deckFilter.value.trim().toLowerCase();
      if (!q) {
        deckNodes.forEach(function (node) {
          node.classList.remove("hidden");
        });
        return;
      }
      // Reveal nodes matching query OR having matching descendants.
      deckNodes.forEach(function (node) {
        var nameEl = node.querySelector(":scope > summary .deckname");
        var text = nameEl ? nameEl.textContent.toLowerCase() : "";
        var matchSelf = text.indexOf(q) !== -1;
        node.dataset.matchSelf = matchSelf ? "1" : "0";
        node.classList.add("hidden");
      });
      deckNodes.forEach(function (node) {
        if (node.dataset.matchSelf === "1") {
          node.classList.remove("hidden");
          var cur = node.parentElement;
          while (cur) {
            if (cur.classList && cur.classList.contains("decknode")) {
              cur.classList.remove("hidden");
              cur.open = true;
            }
            cur = cur.parentElement;
          }
        }
      });
    });
  }

  var expandAllBtn = document.getElementById("deck-expand-all");
  var collapseAllBtn = document.getElementById("deck-collapse-all");
  if (expandAllBtn) {
    expandAllBtn.addEventListener("click", function () {
      deckNodes.forEach(function (node) {
        if (!node.classList.contains("leaf")) node.open = true;
      });
    });
  }
  if (collapseAllBtn) {
    collapseAllBtn.addEventListener("click", function () {
      deckNodes.forEach(function (node) {
        node.open = false;
      });
    });
  }

  // Self-service password confirmation check (progressive enhancement).
  var pwNew = document.getElementById("pw-new");
  var pwConfirm = document.getElementById("pw-confirm");
  if (pwNew && pwConfirm) {
    var syncPwValidity = function () {
      if (pwConfirm.value && pwConfirm.value !== pwNew.value) {
        pwConfirm.setCustomValidity("Passwords do not match.");
      } else {
        pwConfirm.setCustomValidity("");
      }
    };
    pwNew.addEventListener("input", syncPwValidity);
    pwConfirm.addEventListener("input", syncPwValidity);
  }

  // Tool rules preset buttons and interactive tool toggles synced to #patterns.
  var patternsBox = document.getElementById("patterns");
  if (patternsBox) {
    var readLines = function () {
      return patternsBox.value
        .split(/\r?\n/)
        .map(function (s) { return s.trim(); })
        .filter(Boolean);
    };
    var writeLines = function (lines) {
      var seen = {};
      var uniq = [];
      lines.forEach(function (item) {
        if (!seen[item]) {
          seen[item] = true;
          uniq.push(item);
        }
      });
      patternsBox.value = uniq.join("\n");
      syncToggleButtons();
    };
    var toggleButtons = Array.prototype.slice.call(document.querySelectorAll("[data-tool-toggle]"));
    var syncToggleButtons = function () {
      var active = new Set(readLines());
      toggleButtons.forEach(function (btn) {
        var name = btn.getAttribute("data-tool-toggle");
        var isOff = active.has(name);
        btn.setAttribute("aria-pressed", isOff ? "true" : "false");
        btn.textContent = isOff ? "Unstage enable" : "Stage disable";
      });
    };

    toggleButtons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var name = btn.getAttribute("data-tool-toggle");
        var lines = readLines();
        if (lines.indexOf(name) !== -1) {
          writeLines(lines.filter(function (x) { return x !== name; }));
        } else {
          lines.push(name);
          writeLines(lines);
        }
      });
    });

    document.querySelectorAll("[data-rule-preset]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var mode = btn.getAttribute("data-rule-preset");
        if (mode === "clear") {
          writeLines([]);
        } else if (mode === "destructive") {
          writeLines(readLines().concat(["delete_*", "restore_note_type_to_stock", "change_note_type"]));
        } else if (mode === "no-sync") {
          writeLines(readLines().concat(["sync_collection"]));
        }
      });
    });

    patternsBox.addEventListener("input", syncToggleButtons);
    syncToggleButtons();
  }
})();
