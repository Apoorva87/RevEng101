/* eslint-disable no-unused-vars */
/**
 * Shared utilities for all session dashboards.
 * Namespace: window.DashUtils
 */
(function(){
  "use strict";

  // ── Formatting ──

  function compact(n){
    if(n==null) return "\u2014";
    n=Number(n);
    if(n>=1e9) return (n/1e9).toFixed(1)+"B";
    if(n>=1e6) return (n/1e6).toFixed(1)+"M";
    if(n>=1e3) return (n/1e3).toFixed(1)+"K";
    return String(n);
  }

  function relTime(ts){
    if(!ts) return "\u2014";
    var s=Math.floor(Date.now()/1000-ts);
    if(s<60) return "just now";
    if(s<3600) return Math.floor(s/60)+"m ago";
    if(s<86400) return Math.floor(s/3600)+"h ago";
    return Math.floor(s/86400)+"d ago";
  }

  function escHtml(s){
    if(!s) return "";
    return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
  }

  function formatStamp(ts){
    if(!ts) return "\u2014";
    return new Date(ts*1000).toLocaleString();
  }

  // ── State helpers ──

  function stateClass(s){
    if(!s) return "idle";
    var cat = s.state_category || "";
    var st = s.state || "";
    if(cat==="running") return "running";
    if(st==="rate_limited") return "blocked rate_limited";
    if(st==="error") return "blocked error";
    if(st==="inactive") return "inactive";
    return "idle";
  }

  function stateLabel(s){
    if(!s) return "unknown";
    return (s.state||"unknown").replace(/_/g," ");
  }

  // ── Clipboard / toast ──

  function copyText(text, btn){
    navigator.clipboard.writeText(text).then(function(){
      if(btn){
        var orig = btn.textContent;
        btn.textContent = "ok";
        setTimeout(function(){ btn.textContent = orig; }, 1200);
      }
      showToast("Copied!", "success");
    }).catch(function(){
      showToast("Copy failed", "error");
    });
  }

  var _toastTimer = null;
  function showToast(msg, type){
    var existing = document.querySelector(".toast");
    if(existing) existing.remove();
    var el = document.createElement("div");
    el.className = "toast" + (type ? " "+type : "");
    el.textContent = msg;
    document.body.appendChild(el);
    if(_toastTimer) clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function(){ el.remove(); }, 3000);
  }

  // ── Confirm dialog ──

  function confirmDialog(message, onConfirm){
    var overlay = document.createElement("div");
    overlay.className = "confirm-overlay";
    overlay.innerHTML =
      '<div class="confirm-box">' +
        '<div class="confirm-msg">' + escHtml(message) + '</div>' +
        '<div class="confirm-actions">' +
          '<button class="btn-cancel">Cancel</button>' +
          '<button class="btn-confirm">Confirm</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(overlay);

    function close(){ overlay.remove(); }
    overlay.querySelector(".btn-cancel").onclick = close;
    overlay.onclick = function(e){ if(e.target===overlay) close(); };
    overlay.querySelector(".btn-confirm").onclick = function(){
      close();
      onConfirm();
    };
    document.addEventListener("keydown", function handler(e){
      if(e.key==="Escape"){ close(); document.removeEventListener("keydown", handler); }
    });
  }

  // ── Delete helpers ──

  function deleteSession(baseUrl, sessionId, onDone){
    confirmDialog("Delete session " + sessionId.substring(0,12) + "...?", function(){
      fetch(baseUrl + "/" + encodeURIComponent(sessionId), {method:"DELETE"})
        .then(function(r){ return r.json(); })
        .then(function(d){
          if(d.error) showToast("Error: " + d.error, "error");
          else showToast("Deleted session", "success");
          if(onDone) onDone(d);
        })
        .catch(function(e){ showToast("Delete failed: " + e, "error"); });
    });
  }

  function deleteInactive(baseUrl, count, days, onDone){
    // Highlight rows first
    var rows = document.querySelectorAll("tr.inactive-row");
    rows.forEach(function(r){ r.classList.add("highlight-delete","flash-delete"); });
    setTimeout(function(){
      confirmDialog("Delete " + count + " session(s) inactive for " + days + "+ days?", function(){
        fetch(baseUrl + "?days=" + encodeURIComponent(days), {method:"DELETE"})
          .then(function(r){ return r.json(); })
          .then(function(d){
            if(d.error) showToast("Error: " + d.error, "error");
            else showToast("Deleted " + (d.deleted||0) + " session(s)", "success");
            if(onDone) onDone(d);
          })
          .catch(function(e){ showToast("Delete failed: " + e, "error"); });
      });
      rows.forEach(function(r){ r.classList.remove("highlight-delete","flash-delete"); });
    }, 1500);
  }

  // ── Column resize ──

  function initColumnResize(tableEl){
    if(!tableEl) return;
    var ths = tableEl.querySelectorAll("th");
    var storageKey = tableEl.id ? "colw-" + tableEl.id : null;
    var saved = {};
    if(storageKey){
      try{ saved = JSON.parse(sessionStorage.getItem(storageKey)||"{}"); }catch(e){}
    }

    ths.forEach(function(th){
      var key = th.dataset.key;
      if(key && saved[key]) th.style.width = saved[key] + "px";

      var handle = document.createElement("div");
      handle.className = "col-resize-handle";
      th.appendChild(handle);

      handle.addEventListener("mousedown", function(e){
        e.preventDefault();
        e.stopPropagation();
        handle.classList.add("active");
        var startX = e.clientX;
        var startW = th.offsetWidth;

        function onMove(ev){
          var w = Math.max(40, startW + ev.clientX - startX);
          th.style.width = w + "px";
        }
        function onUp(){
          handle.classList.remove("active");
          document.removeEventListener("mousemove", onMove);
          document.removeEventListener("mouseup", onUp);
          if(storageKey && key){
            saved[key] = th.offsetWidth;
            try{ sessionStorage.setItem(storageKey, JSON.stringify(saved)); }catch(e){}
          }
        }
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
      });
    });
  }

  // ── Timeline drag-select ──

  function initTimelineDragSelect(chartEl, onSelectionChange){
    if(!chartEl) return;
    var dragging = false;
    var startIdx = -1;
    var endIdx = -1;

    function bucketButtons(){ return Array.from(chartEl.querySelectorAll(".bucket-btn")); }

    function clearSelection(){
      bucketButtons().forEach(function(b){ b.classList.remove("drag-selected"); });
    }

    function applySelection(from, to){
      var lo = Math.min(from, to), hi = Math.max(from, to);
      var btns = bucketButtons();
      var selected = [];
      btns.forEach(function(b, i){
        if(i>=lo && i<=hi){
          b.classList.add("drag-selected");
          selected.push(b.dataset.bucketKey || String(i));
        } else {
          b.classList.remove("drag-selected");
        }
      });
      return selected;
    }

    chartEl.addEventListener("mousedown", function(e){
      var btn = e.target.closest(".bucket-btn");
      if(!btn) return;
      var btns = bucketButtons();
      startIdx = btns.indexOf(btn);
      if(startIdx<0) return;
      endIdx = startIdx;
      dragging = true;
      clearSelection();
      applySelection(startIdx, endIdx);
      e.preventDefault();
    });

    document.addEventListener("mousemove", function(e){
      if(!dragging) return;
      var btns = bucketButtons();
      // Find closest bucket under cursor
      var best = -1, bestDist = Infinity;
      btns.forEach(function(b, i){
        var r = b.getBoundingClientRect();
        var cx = r.left + r.width/2;
        var d = Math.abs(e.clientX - cx);
        if(d<bestDist){ bestDist=d; best=i; }
      });
      if(best>=0 && best!==endIdx){
        endIdx = best;
        applySelection(startIdx, endIdx);
      }
    });

    document.addEventListener("mouseup", function(){
      if(!dragging) return;
      dragging = false;
      var selected = applySelection(startIdx, endIdx);
      if(onSelectionChange) onSelectionChange(selected);
    });

    // Click outside clears
    document.addEventListener("click", function(e){
      if(!chartEl.contains(e.target)){
        clearSelection();
        if(onSelectionChange) onSelectionChange([]);
      }
    });

    // Escape clears
    document.addEventListener("keydown", function(e){
      if(e.key==="Escape"){
        clearSelection();
        if(onSelectionChange) onSelectionChange([]);
      }
    });
  }

  // ── Auto-refresh helpers ──

  function createRefreshController(opts){
    var intervalMs = opts.interval || 12000;
    var onTick = opts.onTick;
    var progressBarEl = opts.progressBar;
    var refreshBtnEl = opts.refreshBtn;
    var lastUpdateEl = opts.lastUpdateEl;
    var auto = true;
    var timer = null;
    var progressTimer = null;
    var progressStart = 0;

    function startProgress(){
      progressStart = Date.now();
      if(progressTimer) clearInterval(progressTimer);
      if(!progressBarEl) return;
      progressTimer = setInterval(function(){
        var elapsed = Date.now()-progressStart;
        var pct = Math.min(100, elapsed/intervalMs*100);
        progressBarEl.style.width = pct+"%";
      }, 50);
    }

    function schedule(){
      if(timer) clearTimeout(timer);
      if(!auto) return;
      startProgress();
      timer = setTimeout(async function(){
        await onTick();
        if(lastUpdateEl) lastUpdateEl.textContent = new Date().toLocaleTimeString();
        schedule();
      }, intervalMs);
    }

    function toggle(){
      auto = !auto;
      if(refreshBtnEl){
        refreshBtnEl.classList.toggle("on", auto);
        refreshBtnEl.textContent = auto ? "Auto" : "Paused";
      }
      if(auto){
        schedule();
      } else {
        if(timer) clearTimeout(timer);
        if(progressTimer) clearInterval(progressTimer);
        if(progressBarEl) progressBarEl.style.width = "0%";
      }
    }

    function stop(){
      auto = false;
      if(timer) clearTimeout(timer);
      if(progressTimer) clearInterval(progressTimer);
    }

    return { schedule: schedule, toggle: toggle, stop: stop };
  }

  // ── Sort helper ──

  function sortSessions(sessions, key, dir){
    return sessions.slice().sort(function(a,b){
      var va=a[key], vb=b[key];
      if(va==null) va=-Infinity;
      if(vb==null) vb=-Infinity;
      if(typeof va==="string") va=va.toLowerCase();
      if(typeof vb==="string") vb=vb.toLowerCase();
      var cmp=va<vb?-1:va>vb?1:0;
      return dir==="desc"?-cmp:cmp;
    });
  }

  // ── Project colors ──

  var PROJECT_COLORS = [
    "#37c7a7","#62a7ff","#ff7eb3","#ffb347","#a78bfa",
    "#3ddc84","#ff5555","#67e8f9","#fbbf24","#c084fc"
  ];

  function projectColor(idx){
    return PROJECT_COLORS[idx % PROJECT_COLORS.length];
  }

  // ── Zoom minimap ──

  /**
   * Renders and wires a minimap slider for timeline zoom.
   * @param {HTMLElement} containerEl  - The .zoom-minimap element
   * @param {Array} allBuckets        - Full array of bucket objects (day or hour)
   * @param {number} zStart           - Current zoom start index
   * @param {number} zEnd             - Current zoom end index (-1 = all)
   * @param {function} onChange        - Called with (newStart, newEnd) when user drags
   */
  function renderZoomMinimap(containerEl, allBuckets, zStart, zEnd, onChange){
    if(!containerEl || !allBuckets || !allBuckets.length){
      if(containerEl) containerEl.innerHTML = "";
      return;
    }
    var total = allBuckets.length;
    var s = zStart;
    var e = (zEnd < 0 || zEnd >= total) ? total - 1 : zEnd;

    // Build minimap bars
    var maxVal = Math.max(1, Math.max.apply(null, allBuckets.map(function(b){ return b.total; })));
    var barsHtml = '<div class="zoom-minimap-bars">';
    allBuckets.forEach(function(b){
      var pct = Math.max(4, b.total / maxVal * 100);
      barsHtml += '<div class="mini-bar" style="height:' + pct + '%"></div>';
    });
    barsHtml += '</div>';

    // Viewport window
    var leftPct = (s / total * 100);
    var widthPct = ((e - s + 1) / total * 100);
    var windowHtml = '<div class="zoom-window" id="zw" style="left:' + leftPct + '%;width:' + widthPct + '%">' +
      '<div class="zw-edge left"></div>' +
      '<div class="zw-edge right"></div>' +
      '</div>';

    containerEl.innerHTML = barsHtml + windowHtml;

    // Dim areas via CSS custom properties
    var dimRight = 100 - leftPct - widthPct;
    containerEl.style.setProperty("--dim-left", leftPct + "%");
    containerEl.style.setProperty("--dim-right", Math.max(0, dimRight) + "%");

    // ── Drag interactions ──
    var zw = containerEl.querySelector("#zw");
    var edgeLeft = zw.querySelector(".zw-edge.left");
    var edgeRight = zw.querySelector(".zw-edge.right");

    function pxToIdx(px){
      var rect = containerEl.getBoundingClientRect();
      var ratio = Math.max(0, Math.min(1, (px - rect.left) / rect.width));
      return Math.round(ratio * (total - 1));
    }

    function updateWindow(newS, newE){
      newS = Math.max(0, Math.min(total - 1, newS));
      newE = Math.max(newS, Math.min(total - 1, newE));
      if(newE - newS < 1 && total > 1){
        if(newE < total - 1) newE = newS + 1;
        else if(newS > 0) newS = newE - 1;
      }
      var lp = newS / total * 100;
      var wp = (newE - newS + 1) / total * 100;
      zw.style.left = lp + "%";
      zw.style.width = wp + "%";
      containerEl.style.setProperty("--dim-left", lp + "%");
      containerEl.style.setProperty("--dim-right", Math.max(0, 100 - lp - wp) + "%");
      return { s: newS, e: newE };
    }

    // Drag the window body (pan)
    zw.addEventListener("mousedown", function(ev){
      if(ev.target.classList.contains("zw-edge")) return;
      ev.preventDefault();
      var startX = ev.clientX;
      var origS = s, origE = e;
      var span = origE - origS;
      function onMove(mv){
        var dx = mv.clientX - startX;
        var rect = containerEl.getBoundingClientRect();
        var idxDelta = Math.round(dx / rect.width * total);
        var ns = origS + idxDelta;
        var ne = ns + span;
        if(ns < 0){ ns = 0; ne = span; }
        if(ne >= total){ ne = total - 1; ns = ne - span; }
        var r = updateWindow(ns, ne);
        s = r.s; e = r.e;
      }
      function onUp(){
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        onChange(s, e);
      }
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });

    // Drag left edge (resize left)
    edgeLeft.addEventListener("mousedown", function(ev){
      ev.preventDefault(); ev.stopPropagation();
      function onMove(mv){
        var ni = pxToIdx(mv.clientX);
        var r = updateWindow(ni, e);
        s = r.s; e = r.e;
      }
      function onUp(){
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        onChange(s, e);
      }
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });

    // Drag right edge (resize right)
    edgeRight.addEventListener("mousedown", function(ev){
      ev.preventDefault(); ev.stopPropagation();
      function onMove(mv){
        var ni = pxToIdx(mv.clientX);
        var r = updateWindow(s, ni);
        s = r.s; e = r.e;
      }
      function onUp(){
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        onChange(s, e);
      }
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });

    // Click on minimap background to jump viewport there
    containerEl.addEventListener("mousedown", function(ev){
      if(ev.target === containerEl || ev.target.classList.contains("mini-bar") || ev.target.classList.contains("zoom-minimap-bars")){
        var ci = pxToIdx(ev.clientX);
        var span = e - s;
        var ns = Math.round(ci - span / 2);
        var ne = ns + span;
        if(ns < 0){ ns = 0; ne = span; }
        if(ne >= total){ ne = total - 1; ns = ne - span; }
        var r = updateWindow(ns, ne);
        s = r.s; e = r.e;
        onChange(s, e);
      }
    });
  }

  // ── Export ──

  window.DashUtils = {
    compact: compact,
    relTime: relTime,
    escHtml: escHtml,
    formatStamp: formatStamp,
    stateClass: stateClass,
    stateLabel: stateLabel,
    copyText: copyText,
    showToast: showToast,
    confirmDialog: confirmDialog,
    deleteSession: deleteSession,
    deleteInactive: deleteInactive,
    initColumnResize: initColumnResize,
    initTimelineDragSelect: initTimelineDragSelect,
    createRefreshController: createRefreshController,
    sortSessions: sortSessions,
    projectColor: projectColor,
    PROJECT_COLORS: PROJECT_COLORS,
    renderZoomMinimap: renderZoomMinimap
  };
})();
