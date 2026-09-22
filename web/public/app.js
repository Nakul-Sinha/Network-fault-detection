/* NetPulse Local demo player.
 *
 * Plays back recordings produced by scripts/build_demo_data.py, which runs the
 * real Scorer over the real fault-injection corpus. Nothing here computes a
 * health score or decides a severity: it renders what the agent already
 * concluded, frame by frame.
 *
 * Everything is drawn as terminal output, including the chart, which is built
 * from block characters rather than SVG. That is not only a style choice: a
 * monospace grid makes the gap between the moment the agent warned and the
 * moment things broke something you can literally count across the line.
 */

(function () {
  "use strict";

  var LAYERS = [
    ["wifi", "wifi"],
    ["os", "device"],
    ["dns", "dns"],
    ["gateway", "router"],
    ["path", "path"],
    ["remote_https", "remote"],
    ["vpn", "vpn"]
  ];

  var SIGNALS = [
    ["wifi_rssi_dbm", "wifi signal", "dBm", -70, true],
    ["wifi_tx_retry_rate", "wifi retries", "%", 15],
    ["gw_rtt_ms", "router rtt", "ms", 25],
    ["gw_loss_rate", "router loss", "%", 5],
    ["dns_p50_ms", "dns lookup", "ms", 120],
    ["dns_fail_rate", "dns failures", "%", 10],
    ["path_rtt_ms", "path rtt", "ms", 90],
    ["https_ttfb_ms", "https ttfb", "ms", 300],
    ["https_tls_ms", "tls handshake", "ms", 200],
    ["os_cpu_pct", "cpu", "%", 75],
    ["os_retrans_rate", "tcp retransmits", "%", 5]
  ];

  var SEVERITY = {
    info: { cls: "g", word: "healthy" },
    watch: { cls: "a", word: "watch" },
    risk: { cls: "a", word: "elevated risk" },
    critical: { cls: "r", word: "trouble now" }
  };

  // Eighth-blocks, for the sparkline.
  var BLOCKS = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"];

  var state = {
    scenario: null,
    frame: 0,
    playing: false,
    speed: 4,
    timer: null,
    cache: {},
    width: 96
  };

  function el(id) { return document.getElementById(id); }
  function text(id, v) { var n = el(id); if (n) n.textContent = v; }
  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function pad(s, n) { s = String(s); return s.length >= n ? s : s + " ".repeat(n - s.length); }
  function padStart(s, n) { s = String(s); return s.length >= n ? s : " ".repeat(n - s.length) + s; }
  function pct(v) { return v === null || v === undefined ? "--" : Math.round(v * 100) + "%"; }

  function mmss(sec) {
    var m = Math.floor(sec / 60), s = Math.floor(sec % 60);
    return (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s;
  }

  /* ------------------------------------------------------------- loading */

  function loadIndex() {
    return fetch("/data/index.json")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        // Every headline figure comes from the index, which takes them from the
        // evaluation harness. Hard-coding them in the markup would let the page
        // drift away from what the gate actually enforces.
        var s = data.stats || {};
        if (s.medianLeadLabel) text("statLead", s.medianLeadLabel);
        if (s.detectionRate !== undefined) text("statDetect", Math.round(s.detectionRate * 100) + "%");
        if (s.layerAccuracy !== undefined) text("statLayer", Math.round(s.layerAccuracy * 100) + "%");
        if (s.benignAlertsPerDay !== undefined) {
          text("statBenign", s.benignAlertsPerDay === 0 ? "0" : s.benignAlertsPerDay.toFixed(1));
        }
        buildPicker(data.scenarios);
        var first = data.scenarios.filter(function (x) { return !x.benign; })[0];
        return select((first || data.scenarios[0]).name);
      })
      .catch(function () {
        text("scenarioDescription", "Could not load the recordings. Try reloading.");
      });
  }

  function buildPicker(scenarios) {
    var picker = document.querySelector(".picker");
    picker.innerHTML = "";
    scenarios.forEach(function (s) {
      var b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", "false");
      b.textContent = s.name;
      b.dataset.name = s.name;
      b.title = s.title;
      b.addEventListener("click", function () { select(s.name); });
      picker.appendChild(b);
    });
  }

  function select(name) {
    document.querySelectorAll(".picker button").forEach(function (b) {
      b.setAttribute("aria-selected", String(b.dataset.name === name));
    });
    pause();

    var loaded = state.cache[name]
      ? Promise.resolve(state.cache[name])
      : fetch("/data/" + name + ".json").then(function (r) { return r.json(); })
          .then(function (d) { state.cache[name] = d; return d; });

    return loaded.then(function (data) {
      state.scenario = data;
      // Start just before the agent is warm. Watching a model warm up is not
      // interesting, and scoring during warm-up would not be honest anyway.
      state.frame = Math.max(0, (data.warmFrom || 0) - 4);
      text("demoCmd", "netpulse demo " + data.name);
      text("scenarioDescription", data.description);
      el("scrub").max = String(data.frames.length - 1);
      render();
    });
  }

  /* ------------------------------------------------------------ rendering */

  function render() {
    var data = state.scenario;
    if (!data) return;
    var f = data.frames[state.frame];
    if (!f) return;

    el("scrub").value = String(state.frame);
    text("clock", mmss(f.t));

    renderReadout(f, data);
    renderChart(data);
    renderLayers(f);
    renderCard(f, data);
    renderLeadFlag(data);
  }

  function bar(value, width) {
    var filled = Math.round(Math.max(0, Math.min(1, value)) * width);
    return "█".repeat(filled) + "░".repeat(width - filled);
  }

  function riskClass(v) { return v >= 0.75 ? "r" : v >= 0.5 ? "a" : v >= 0.25 ? "a" : "g"; }

  function renderReadout(f, data) {
    var sev = SEVERITY[f.sev] || SEVERITY.info;
    var health = f.h === null ? 0 : f.h;
    var hClass = health >= 70 ? "g" : health >= 35 ? "a" : "r";
    var w = state.width > 70 ? 28 : 18;

    var lines = [];
    lines.push(
      "  " + span("k", pad("health", 18)) +
      span(hClass, padStart(Math.round(health), 3) + "/100 ") +
      span(hClass, bar(health / 100, w)) + "  " +
      span(f.warm ? "dim" : sev.cls, f.warm ? "learning baselines" : sev.word)
    );
    lines.push(
      "  " + span("k", pad("risk in 5 min", 18)) +
      span(riskClass(f.r5 || 0), padStart(pct(f.r5), 3) + "      ") +
      span(riskClass(f.r5 || 0), bar(f.r5 || 0, w))
    );
    lines.push(
      "  " + span("k", pad("risk in 15 min", 18)) +
      span(riskClass(f.r15 || 0), padStart(pct(f.r15), 3) + "      ") +
      span(riskClass(f.r15 || 0), bar(f.r15 || 0, w))
    );
    lines.push(
      "  " + span("k", pad("signals covered", 18)) +
      span("c", padStart(pct(f.cov), 3)) +
      span("dim", "      t+" + mmss(f.t) + " of " +
        mmss(data.frames[data.frames.length - 1].t))
    );
    lines.push("");

    // Live signal values, two per row where the terminal is wide enough.
    var vals = f.v || {};
    var cells = [];
    SIGNALS.forEach(function (s) {
      var key = s[0], label = s[1], unit = s[2], threshold = s[3], lowerIsWorse = s[4];
      if (vals[key] === undefined) return;
      var v = vals[key];
      var hot = lowerIsWorse ? v <= threshold : v >= threshold;
      cells.push(
        span("k", pad(label, 17)) + span(hot ? "r" : "v", padStart(v + unit, 9))
      );
    });
    var perRow = state.width > 74 ? 2 : 1;
    for (var i = 0; i < cells.length; i += perRow) {
      lines.push("  " + cells.slice(i, i + perRow).join("     "));
    }

    el("readout").innerHTML = lines.join("\n");
  }

  function span(cls, s) { return '<span class="' + cls + '">' + esc(s) + "</span>"; }

  /* ----------------------------------------------------------- the chart */

  function renderChart(data) {
    var frames = data.frames;
    var cols = Math.max(40, Math.min(state.width, 110));
    var rows = 12;

    // Downsample the recording onto the character grid, keeping the worst
    // health in each column so a brief collapse is never averaged away.
    var buckets = [];
    for (var c = 0; c < cols; c++) {
      var lo = Math.floor((c / cols) * frames.length);
      var hi = Math.max(lo + 1, Math.floor(((c + 1) / cols) * frames.length));
      var worst = 100;
      for (var i = lo; i < hi && i < frames.length; i++) {
        if (frames[i].h !== null && frames[i].h < worst) worst = frames[i].h;
      }
      buckets.push(worst);
    }

    var toCol = function (frameIndex) {
      return Math.max(0, Math.min(cols - 1, Math.floor((frameIndex / frames.length) * cols)));
    };
    var warnCol = data.alertIndex !== null && data.alertIndex !== undefined ? toCol(data.alertIndex) : -1;
    var brokeCol = data.impactIndex !== null && data.impactIndex !== undefined ? toCol(data.impactIndex) : -1;
    var warmCol = data.warmFrom ? toCol(data.warmFrom) : -1;
    var headCol = toCol(state.frame);

    var out = [];
    for (var r = rows - 1; r >= 0; r--) {
      var floor = (r / rows) * 100;
      var ceil = ((r + 1) / rows) * 100;
      var label = r === rows - 1 ? "100 " : r === 0 ? "  0 " : r === Math.floor(rows / 2) ? " 50 " : "    ";
      var line = span("axis", label) + span("axis", "│");
      for (var c2 = 0; c2 < cols; c2++) {
        var h = buckets[c2];
        var ch = " ";
        // Colour by the health value at that column, never by which region it
        // falls in. Shading the bars red across the impact window painted the
        // recovery at the end of every recording as a failure.
        var cls = h >= 70 ? "" : h >= 35 ? "warned" : "broke";

        if (h >= ceil) {
          ch = "█";
        } else if (h > floor) {
          var eighth = Math.floor(((h - floor) / (ceil - floor)) * 8) - 1;
          ch = BLOCKS[Math.max(0, Math.min(7, eighth))];
        } else {
          // Nothing drawn here. Only the warm-up run is shaded: an earlier
          // version shaded everything from the moment things broke to the end
          // of the recording, which painted the recovery red and made a
          // successful recovery look like a continuing failure.
          cls = "";
          if (warmCol > 0 && c2 < warmCol) { ch = "·"; cls = "warm"; }
        }

        // Two markers drawn over everything, because the distance between
        // them is the whole point of this chart.
        if (c2 === warnCol) { ch = ch === " " ? "│" : ch; cls = "warned"; }
        else if (c2 === brokeCol) { ch = ch === " " ? "│" : ch; cls = "broke"; }

        line += cls ? span(cls, ch) : esc(ch);
      }
      out.push(line);
    }

    out.push(span("axis", "    └" + "─".repeat(cols)));

    // Marker row: where the agent warned, where it broke, and the gap between.
    var marks = new Array(cols).fill(" ");
    if (warnCol >= 0) marks[warnCol] = "▲";
    if (brokeCol >= 0) marks[brokeCol] = "▲";
    if (headCol >= 0) marks[headCol] = marks[headCol] === " " ? "^" : marks[headCol];
    var markLine = "     ";
    for (var c3 = 0; c3 < cols; c3++) {
      var m = marks[c3];
      if (m === "▲" && c3 === warnCol) markLine += span("warned", m);
      else if (m === "▲") markLine += span("broke", m);
      else if (m === "^") markLine += span("axis", m);
      else markLine += " ";
    }
    out.push(markLine);

    if (warnCol >= 0 && brokeCol > warnCol) {
      var gap = brokeCol - warnCol;
      var label2 = data.leadSeconds ? (data.leadSeconds / 60).toFixed(1).replace(/\.0$/, "") + " min early" : "";
      var line2 = "     " + " ".repeat(warnCol) + span("warned", "└");
      if (gap > label2.length + 2) {
        var left = Math.floor((gap - label2.length - 1) / 2);
        line2 += span("warned", "─".repeat(left) + " " + label2 + " " +
          "─".repeat(Math.max(0, gap - left - label2.length - 3)) + "┘");
      } else {
        line2 += span("warned", "─".repeat(Math.max(0, gap - 1)) + "┘ " + label2);
      }
      out.push(line2);
    } else if (data.benign) {
      out.push("     " + span("axis", "no alert raised across the whole recording"));
    }

    el("chart").innerHTML = out.join("\n");
  }

  /* ---------------------------------------------------------- attribution */

  function renderLayers(f) {
    var scores = f.layers || {};
    var top = null, topValue = 0;
    Object.keys(scores).forEach(function (k) {
      if (scores[k] > topValue) { topValue = scores[k]; top = k; }
    });

    var w = state.width > 70 ? 34 : 20;
    var lines = LAYERS.map(function (entry) {
      var key = entry[0], label = entry[1];
      var v = Math.max(0, Math.min(1, scores[key] || 0));
      var isTop = key === top && topValue > 0.05;
      var cls = isTop ? "r" : v > 0.3 ? "a" : v > 0.01 ? "g" : "dim";
      return "  " + span(isTop ? "v" : "k", pad(label, 10)) +
        span(cls, bar(v, w)) + "  " +
        span(cls, v > 0.005 ? v.toFixed(2) : "  · ") +
        (isTop ? span("r", "  ← primary") : "");
    });
    el("layers").innerHTML = lines.join("\n");
  }

  /* ----------------------------------------------------------- the card */

  function renderCard(f, data) {
    var host = el("incidentCard");
    host.innerHTML = "";

    if (!f.card) {
      var p = document.createElement("p");
      p.className = "out";
      p.textContent = f.warm
        ? "Still learning what normal looks like on this network."
        : "Nothing unusual. The agent has nothing to say, which is most of the time and is the hardest case to get right.";
      host.appendChild(p);
      return;
    }

    var wrap = document.createElement("div");
    wrap.className = "verdict " + (f.sev === "critical" ? "alert" : "warn");

    var h = document.createElement("h4");
    h.textContent = "! " + f.card.title;
    wrap.appendChild(h);

    var summary = document.createElement("p");
    summary.textContent = f.card.summary;
    wrap.appendChild(summary);

    var meta = document.createElement("p");
    meta.className = "meta";
    var right = data.expectedLayer && f.card.layer === data.expectedLayer;
    meta.textContent =
      "layer=" + (f.card.layer || "?") +
      "  confidence=" + Math.round((f.card.confidence || 0) * 100) + "%" +
      (data.expectedLayer ? (right ? "  [correct layer]" : "  [expected " + data.expectedLayer + "]") : "");
    wrap.appendChild(meta);

    if (f.card.remediation && f.card.remediation.length) {
      var ol = document.createElement("ol");
      f.card.remediation.forEach(function (t) {
        var li = document.createElement("li");
        li.textContent = t;
        ol.appendChild(li);
      });
      wrap.appendChild(ol);
    }
    host.appendChild(wrap);
  }

  function renderLeadFlag(data) {
    var flag = el("leadFlag");
    if (data.leadSeconds && state.frame >= (data.alertIndex || 0)) {
      flag.hidden = false;
      flag.textContent = "warned " +
        (data.leadSeconds / 60).toFixed(1).replace(/\.0$/, "") + " min early";
    } else {
      flag.hidden = true;
    }
  }

  /* -------------------------------------------------------------- playback */

  function tick() {
    if (!state.scenario) return;
    if (state.frame >= state.scenario.frames.length - 1) { pause(); return; }
    state.frame += 1;
    render();
  }

  function play() {
    if (!state.scenario) return;
    if (state.frame >= state.scenario.frames.length - 1) {
      state.frame = Math.max(0, (state.scenario.warmFrom || 0) - 4);
    }
    state.playing = true;
    text("playLabel", "⏸ pause");
    el("playBtn").setAttribute("aria-label", "Pause");
    clearInterval(state.timer);
    // Each frame is 15 seconds of recorded time.
    state.timer = setInterval(tick, Math.max(40, 15000 / state.speed));
  }

  function pause() {
    state.playing = false;
    clearInterval(state.timer);
    if (!el("playBtn")) return;
    text("playLabel", "▶ play");
    el("playBtn").setAttribute("aria-label", "Play");
  }

  function measure() {
    // How many monospace characters fit across the panel, so the chart is
    // drawn to the real terminal width rather than a guess.
    var probe = document.createElement("span");
    probe.style.cssText = "position:absolute;visibility:hidden;white-space:pre;font:13px var(--mono)";
    probe.textContent = "0".repeat(100);
    document.body.appendChild(probe);
    var charWidth = probe.getBoundingClientRect().width / 100;
    document.body.removeChild(probe);
    var panel = document.querySelector(".chart-wrap");
    var available = panel ? panel.getBoundingClientRect().width : 800;
    state.width = Math.max(40, Math.floor(available / Math.max(6, charWidth)) - 6);
  }

  function wire() {
    el("playBtn").addEventListener("click", function () {
      state.playing ? pause() : play();
    });
    el("scrub").addEventListener("input", function (e) {
      pause();
      state.frame = Number(e.target.value);
      render();
    });
    el("speed").addEventListener("change", function (e) {
      state.speed = Number(e.target.value);
      if (state.playing) play();
    });

    document.querySelectorAll("[data-copy]").forEach(function (button) {
      button.addEventListener("click", function () {
        if (!navigator.clipboard) return;
        navigator.clipboard.writeText(button.dataset.copy).then(function () {
          var original = button.textContent;
          button.textContent = "copied";
          setTimeout(function () { button.textContent = original; }, 1400);
        });
      });
    });

    document.addEventListener("keydown", function (e) {
      if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
      if (!state.scenario) return;
      if (e.code === "Space") { e.preventDefault(); state.playing ? pause() : play(); }
      if (e.code === "ArrowRight") {
        pause();
        state.frame = Math.min(state.scenario.frames.length - 1, state.frame + 1);
        render();
      }
      if (e.code === "ArrowLeft") { pause(); state.frame = Math.max(0, state.frame - 1); render(); }
    });

    var resizeTimer;
    window.addEventListener("resize", function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () { measure(); render(); }, 120);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    wire();
    measure();
    loadIndex();
  });
})();
