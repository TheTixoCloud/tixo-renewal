/* TixoCloud Control Center -- live bandwidth widget (v1.10.0).
 * Zero dependencies, vanilla JS. Polls the existing, already-authenticated /api/live
 * endpoint (a single cheap /sys/class/net read server-side -- no new backend process, no
 * extra load on the hypervisor). Every rate shown here is computed CLIENT-SIDE from the
 * delta between two consecutive raw cumulative kernel-counter reads; the server never
 * computes or stores a "live rate" itself. A bounded ring buffer (MAX_POINTS) holds only
 * samples this browser has actually observed since the widget was created -- never
 * fabricated or interpolated history, and capped so memory/DOM cost can never grow
 * unbounded even if the tab is left open for hours.
 *
 * One widget instance can be mounted on multiple pages (Overview, Network Traffic) by
 * giving each container a unique id and calling TixoLiveBandwidth.mount(id).
 */
(function (global) {
  "use strict";

  var POLL_MS = 4000; // within the requested ~3-5s refresh window
  var MAX_POINTS = 300; // bounded buffer (300 * 4s = 20 minutes worst case)

  function fmtBitsRate(bytesPerSec, unit) {
    var bits = bytesPerSec * 8;
    if (unit === "mbps") return (bits / 1e6).toFixed(2) + " Mbps";
    if (unit === "gbps") return (bits / 1e9).toFixed(2) + " Gbps";
    if (bits >= 1e9) return (bits / 1e9).toFixed(2) + " Gbps";
    if (bits >= 1e3) return (bits / 1e6).toFixed(2) + " Mbps";
    return bits.toFixed(0) + " bps";
  }

  function fmtBytesRate(bytesPerSec, unit) {
    if (unit === "MBps") return (bytesPerSec / 1e6).toFixed(2) + " MB/s";
    if (unit === "GBps") return (bytesPerSec / 1e9).toFixed(2) + " GB/s";
    if (bytesPerSec >= 1e9) return (bytesPerSec / 1e9).toFixed(2) + " GB/s";
    if (bytesPerSec >= 1e3) return (bytesPerSec / 1e6).toFixed(2) + " MB/s";
    return bytesPerSec.toFixed(0) + " B/s";
  }

  function fmtRate(bytesPerSec, unit) {
    if (bytesPerSec == null || isNaN(bytesPerSec)) return "--";
    if (unit === "mbps" || unit === "gbps") return fmtBitsRate(bytesPerSec, unit);
    if (unit === "MBps" || unit === "GBps") return fmtBytesRate(bytesPerSec, unit);
    // auto: bit-based (networking convention) is the default
    return fmtBitsRate(bytesPerSec, "auto");
  }

  function svgNS(tag) {
    return document.createElementNS("http://www.w3.org/2000/svg", tag);
  }

  function mount(containerId) {
    var root = document.getElementById(containerId);
    if (!root) return;

    var labels = {
      download: root.getAttribute("data-l-download") || "Download",
      upload: root.getAttribute("data-l-upload") || "Upload",
      combined: root.getAttribute("data-l-combined") || "Combined",
      pause: root.getAttribute("data-l-pause") || "Pause",
      resume: root.getAttribute("data-l-resume") || "Resume",
      live: root.getAttribute("data-l-live") || "Live",
      stale: root.getAttribute("data-l-stale") || "Stale",
      unavailable: root.getAttribute("data-l-unavailable") || "Unavailable",
      collecting: root.getAttribute("data-l-collecting") || "Collecting live data...",
      interface: root.getAttribute("data-l-interface") || "Interface",
    };

    var valuesEl = root.querySelector(".live-bw-values");
    var svgEl = root.querySelector(".live-bw-svg");
    var statusEl = root.querySelector(".live-bw-status");
    var unitSel = root.querySelector(".live-bw-unit");
    var windowSel = root.querySelector(".live-bw-window");
    var pauseBtn = root.querySelector(".live-bw-pause");
    var ifaceEl = root.querySelector(".live-bw-iface");
    var cumulativeEl = root.querySelector(".live-bw-cumulative");

    var buffer = []; // {t: epoch_ms, rx: bytes/s, tx: bytes/s}
    var prevReading = null; // {rx, tx, t}
    var paused = false;
    var lastRates = { rx: null, tx: null };
    var timer = null;

    function setStatus(kind, text) {
      statusEl.textContent = text;
      statusEl.className = "live-bw-status muted " +
        (kind === "live" ? "live-bw-ok" : kind === "stale" ? "live-bw-warn" : "live-bw-bad");
    }

    function updateValues() {
      var unit = unitSel.value;
      var combined = (lastRates.rx != null && lastRates.tx != null)
        ? lastRates.rx + lastRates.tx : null;
      valuesEl.innerHTML =
        '<div class="kpi"><div class="kpi-label">' + labels.download + '</div>' +
        '<div class="kpi-value">' + fmtRate(lastRates.rx, unit) + "</div></div>" +
        '<div class="kpi"><div class="kpi-label">' + labels.upload + '</div>' +
        '<div class="kpi-value">' + fmtRate(lastRates.tx, unit) + "</div></div>" +
        '<div class="kpi kpi-info"><div class="kpi-label">' + labels.combined + '</div>' +
        '<div class="kpi-value">' + fmtRate(combined, unit) + "</div></div>";
    }

    function updateCumulative(d) {
      if (!cumulativeEl) return;
      cumulativeEl.innerHTML =
        '<div class="kpi"><div class="kpi-label">' + labels.download + '</div>' +
        '<div class="kpi-value">' + (d.download || "--") + "</div></div>" +
        '<div class="kpi"><div class="kpi-label">' + labels.upload + '</div>' +
        '<div class="kpi-value">' + (d.upload || "--") + "</div></div>" +
        '<div class="kpi kpi-info"><div class="kpi-label">' + labels.combined + '</div>' +
        '<div class="kpi-value">' + (d.combined || "--") + "</div></div>";
    }

    function render() {
      var windowSec = parseInt(windowSel.value, 10) || 300;
      var cutoff = Date.now() - windowSec * 1000;
      var pts = buffer.filter(function (p) { return p.t >= cutoff; });
      drawChart(pts, windowSec);
    }

    function drawChart(pts, windowSec) {
      while (svgEl.firstChild) svgEl.removeChild(svgEl.firstChild);
      var width = 760, height = 200, padL = 50, padR = 10, padT = 10, padB = 24;
      var plotW = width - padL - padR, plotH = height - padT - padB;
      svgEl.setAttribute("viewBox", "0 0 " + width + " " + height);

      if (pts.length < 2) {
        var t = svgNS("text");
        t.setAttribute("x", width / 2);
        t.setAttribute("y", height / 2);
        t.setAttribute("text-anchor", "middle");
        t.setAttribute("fill", "var(--muted)");
        t.setAttribute("font-size", "13");
        t.textContent = labels.collecting;
        svgEl.appendChild(t);
        return;
      }

      var maxVal = 0;
      for (var i = 0; i < pts.length; i++) {
        maxVal = Math.max(maxVal, pts[i].rx, pts[i].tx);
      }
      maxVal = maxVal || 1;

      var tMin = pts[0].t, tMax = pts[pts.length - 1].t;
      var tSpan = Math.max(tMax - tMin, 1);

      function xOf(t) { return padL + ((t - tMin) / tSpan) * plotW; }
      function yOf(v) { return padT + plotH - (v / maxVal) * plotH; }

      // gridlines + y labels (0, 50%, 100% of max, shown as bit-rate per current unit)
      [0, 0.5, 1].forEach(function (frac) {
        var y = padT + plotH * (1 - frac);
        var line = svgNS("line");
        line.setAttribute("x1", padL); line.setAttribute("x2", width - padR);
        line.setAttribute("y1", y); line.setAttribute("y2", y);
        line.setAttribute("stroke", "var(--border)");
        svgEl.appendChild(line);
        var lbl = svgNS("text");
        lbl.setAttribute("x", padL - 6); lbl.setAttribute("y", y + 3);
        lbl.setAttribute("text-anchor", "end"); lbl.setAttribute("font-size", "9.5");
        lbl.setAttribute("fill", "var(--muted)");
        lbl.textContent = fmtRate(maxVal * frac, unitSel.value);
        svgEl.appendChild(lbl);
      });

      function pathFor(key, color) {
        var d = "";
        for (var j = 0; j < pts.length; j++) {
          var x = xOf(pts[j].t), y = yOf(pts[j][key]);
          d += (j === 0 ? "M" : "L") + x.toFixed(1) + "," + y.toFixed(1) + " ";
        }
        var path = svgNS("path");
        path.setAttribute("d", d.trim());
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", color);
        path.setAttribute("stroke-width", "2");
        path.setAttribute("stroke-linejoin", "round");
        svgEl.appendChild(path);
      }
      pathFor("rx", "#3b82f6");
      pathFor("tx", "#ef4444");

      // combined as a dashed line
      var dCombined = "";
      for (var k = 0; k < pts.length; k++) {
        var xc = xOf(pts[k].t), yc = yOf(pts[k].rx + pts[k].tx);
        dCombined += (k === 0 ? "M" : "L") + xc.toFixed(1) + "," + yc.toFixed(1) + " ";
      }
      var combinedPath = svgNS("path");
      combinedPath.setAttribute("d", dCombined.trim());
      combinedPath.setAttribute("fill", "none");
      combinedPath.setAttribute("stroke", "var(--ok)");
      combinedPath.setAttribute("stroke-width", "1.5");
      combinedPath.setAttribute("stroke-dasharray", "4,3");
      svgEl.appendChild(combinedPath);

      // time axis labels (honest real timestamps, not synthetic ticks)
      [0, 0.5, 1].forEach(function (frac) {
        var t = tMin + tSpan * frac;
        var x = xOf(t);
        var lbl = svgNS("text");
        lbl.setAttribute("x", x); lbl.setAttribute("y", height - 6);
        lbl.setAttribute("text-anchor", frac === 0 ? "start" : frac === 1 ? "end" : "middle");
        lbl.setAttribute("font-size", "9.5");
        lbl.setAttribute("fill", "var(--muted)");
        var dt = new Date(t);
        lbl.textContent = dt.toTimeString().slice(0, 8);
        svgEl.appendChild(lbl);
      });
    }

    function poll() {
      if (paused) { schedule(); return; }
      fetch("/api/live", { credentials: "same-origin" })
        .then(function (r) {
          if (!r.ok) throw new Error("http " + r.status);
          return r.json();
        })
        .then(function (d) {
          if (!d.configured) {
            setStatus("bad", labels.unavailable + " (not configured)");
            schedule();
            return;
          }
          if (ifaceEl) ifaceEl.textContent = labels.interface + ": " + d.interface;
          if (!d.ok) {
            setStatus("bad", labels.unavailable);
            schedule();
            return;
          }
          updateCumulative(d);
          var now = new Date(d.read_at).getTime();
          if (prevReading && d.rx_bytes >= prevReading.rx && d.tx_bytes >= prevReading.tx) {
            var dt = (now - prevReading.t) / 1000;
            if (dt > 0.5) {
              var rxRate = (d.rx_bytes - prevReading.rx) / dt;
              var txRate = (d.tx_bytes - prevReading.tx) / dt;
              lastRates = { rx: rxRate, tx: txRate };
              buffer.push({ t: now, rx: rxRate, tx: txRate });
              if (buffer.length > MAX_POINTS) buffer.shift();
              updateValues();
              render();
              setStatus("live", labels.live);
            }
          } else if (prevReading) {
            // Counter reset/reboot/rollover -- never fabricate a rate across the gap.
            setStatus("stale", labels.stale + " (counter reset detected)");
          } else {
            setStatus("stale", labels.collecting);
          }
          prevReading = { rx: d.rx_bytes, tx: d.tx_bytes, t: now };
          schedule();
        })
        .catch(function (e) {
          setStatus("bad", labels.unavailable);
          schedule();
        });
    }

    function schedule() {
      if (timer) clearTimeout(timer);
      timer = setTimeout(poll, POLL_MS);
    }

    pauseBtn.addEventListener("click", function () {
      paused = !paused;
      pauseBtn.textContent = paused ? labels.resume : labels.pause;
    });
    unitSel.addEventListener("change", function () { updateValues(); render(); });
    windowSel.addEventListener("change", render);

    setStatus("stale", labels.collecting);
    poll();
  }

  global.TixoLiveBandwidth = { mount: mount };

  // Exposed ONLY for the Node-based unit tests (tests/test_live_bandwidth_js.py) --
  // `module` never exists in a browser, so this is a pure no-op there.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { fmtRate: fmtRate, fmtBitsRate: fmtBitsRate, fmtBytesRate: fmtBytesRate };
  }
})(typeof window !== "undefined" ? window : globalThis);
