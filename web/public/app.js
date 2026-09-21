/* NetPulse Local demo player.
 *
 * Plays back recordings produced by scripts/build_demo_data.py, which runs
 * the real Scorer over the real fault-injection corpus. Nothing here computes
 * a health score or decides a severity: it renders what the agent already
 * concluded, frame by frame.
 *
 * The one visual worth the effort is the timeline. It draws the moment the
 * agent first warned and the period when things were actually bad, so the gap
 * between them is a thing you can see rather than a number you are told.
 */

(function () {
  "use strict";

  var LAYERS = [
    ["wifi", "Wi-Fi"],
    ["os", "This device"],
    ["dns", "DNS"],
    ["gateway", "Your router"],
    ["path", "Path to internet"],
    ["remote_https", "Remote services"],
    ["vpn", "VPN"]
  ];

  var SIGNALS = [
    ["wifi_rssi_dbm", "Wi-Fi signal", " dBm", -70],
    ["wifi_tx_retry_rate", "Wi-Fi retries", "%", 15],
    ["gw_rtt_ms", "Router round trip", " ms", 25],
    ["gw_loss_rate", "Router loss", "%", 5],
    ["dns_p50_ms", "Name lookup", " ms", 120],
    ["dns_fail_rate", "Lookup failures", "%", 10],
    ["path_rtt_ms", "Path latency", " ms", 90],
    ["https_ttfb_ms", "Time to first byte", " ms", 300],
    ["https_tls_ms", "TLS handshake", " ms", 200],
    ["os_cpu_pct", "CPU", "%", 75],
    ["os_retrans_rate", "TCP retransmits", "%", 5]
  ];

  var SEVERITY = {
    info: { cls: "ok", word: "healthy" },
    watch: { cls: "watch", word: "watch" },
    risk: { cls: "risk", word: "elevated risk" },
    critical: { cls: "crit", word: "trouble now" }
  };

  var state = {
    index: null,
    scenario: null,
    frame: 0,
    playing: false,
    speed: 4,
    timer: null,
    cache: {}
  };

  function el(id) { return document.getElementById(id); }
  function text(id, value) { var n = el(id); if (n) n.textContent = value; }
  function pct(v) { return v === null || v === undefined ? "--" : Math.round(v * 100) + "%"; }

  function mmss(seconds) {
    var m = Math.floor(seconds / 60), s = Math.floor(seconds % 60);
    return (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s;
  }

  function minutes(seconds) {
    return (seconds / 60).toFixed(1).replace(/\.0$/, "") + " min";
  }

  /* ------------------------------------------------------------- loading */

  function loadIndex() {
    return fetch("/data/index.json")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        state.index = data;
        // Every headline figure comes from the recordings index, which takes
        // them from the evaluation harness. Hard-coding them in the markup
        // would let the page drift away from what the gate actually enforces.
        var s = data.stats || {};
        if (s.medianLeadLabel) text("statLead", s.medianLeadLabel);
        if (s.detectionRate !== undefined) text("statDetect", Math.round(s.detectionRate * 100) + "%");
        if (s.layerAccuracy !== undefined) text("statLayer", Math.round(s.layerAccuracy * 100) + "%");
        if (s.benignAlertsPerDay !== undefined) {
          text("statBenign", s.benignAlertsPerDay === 0 ? "0" : s.benignAlertsPerDay.toFixed(1));
        }
        buildPicker(data.scenarios);
        var first = data.scenarios.filter(function (s) { return !s.benign; })[0];
        return select((first || data.scenarios[0]).name);
      })
      .catch(function () {
        text("scenarioTitle", "Could not load the recordings");
        text("scenarioDescription", "Try reloading the page.");
      });
  }

  function buildPicker(scenarios) {
    var picker = document.querySelector(".picker");
    picker.innerHTML = "";
    scenarios.forEach(function (s) {
      var button = document.createElement("button");
      button.type = "button";
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", "false");
      button.className = s.benign ? "benign" : "fault";
      button.textContent = s.title;
      button.dataset.name = s.name;
      button.addEventListener("click", function () { select(s.name); });
      picker.appendChild(button);
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
          .then(function (data) { state.cache[name] = data; return data; });

    return loaded.then(function (data) {
      state.scenario = data;
      // Start just before the agent is warm. Watching a model warm up is not
      // interesting, and scoring during warm-up would not be honest anyway.
      state.frame = Math.max(0, (data.warmFrom || 0) - 4);
      text("scenarioTitle", data.title);
      text("scenarioDescription", data.description);
      el("scrub").max = String(data.frames.length - 1);
      drawChart();
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

    text("health", f.h === null ? "--" : Math.round(f.h));
    var sev = SEVERITY[f.sev] || SEVERITY.info;
    var chip = el("sevChip");
    chip.textContent = f.warm ? "learning" : sev.word;
    chip.className = "chip " + (f.warm ? "" : sev.cls);
    el("warmChip").hidden = !f.warm;

    text("r5", pct(f.r5));
    text("r15", pct(f.r15));
    text("cov", pct(f.cov));
    meter("r5Bar", f.r5);
    meter("r15Bar", f.r15);

    renderCard(f, data);
    renderLayers(f, data);
    renderSignals(f);
    movePlayhead();
    updateLeadBadge(data);
  }

  function meter(id, value) {
    var n = el(id);
    var v = Math.max(0, Math.min(1, value || 0));
    n.style.width = (v * 100) + "%";
    n.className = v >= 0.75 ? "crit" : v >= 0.5 ? "risk" : v >= 0.25 ? "watch" : "";
  }

  function renderCard(f, data) {
    var host = el("incidentCard");
    host.innerHTML = "";

    if (!f.card) {
      var p = document.createElement("p");
      p.className = "muted";
      p.textContent = f.warm
        ? "Still learning what normal looks like on this network."
        : "Nothing unusual. The agent has nothing to say, which is most of the time.";
      host.appendChild(p);
      return;
    }

    var h = document.createElement("h4");
    h.textContent = f.card.title;
    host.appendChild(h);

    var summary = document.createElement("p");
    summary.textContent = f.card.summary;
    host.appendChild(summary);

    var meta = document.createElement("p");
    meta.className = "meta";
    var right = data.expectedLayer && f.card.layer === data.expectedLayer;
    meta.textContent =
      "Likely " + (f.card.layerLabel || f.card.layer) +
      " · confidence " + Math.round((f.card.confidence || 0) * 100) + "%" +
      (data.expectedLayer ? (right ? " · correct layer" : " · expected " + data.expectedLayerLabel) : "");
    host.appendChild(meta);

    if (f.card.evidence && f.card.evidence.length) {
      var ev = document.createElement("ul");
      ev.className = "evidence";
      f.card.evidence.forEach(function (item) {
        var li = document.createElement("li");
        li.textContent = item.label + ": " + item.value + (item.unit ? " " + item.unit : "");
        li.title = item.comparison || "";
        ev.appendChild(li);
      });
      host.appendChild(ev);
    }

    if (f.card.remediation && f.card.remediation.length) {
      var head = document.createElement("p");
      head.className = "remedy-head";
      head.textContent = "What to try";
      host.appendChild(head);
      var ol = document.createElement("ol");
      ol.className = "remedy";
      f.card.remediation.forEach(function (t) {
        var li = document.createElement("li");
        li.textContent = t;
        ol.appendChild(li);
      });
      host.appendChild(ol);
    }
  }

  function renderLayers(f, data) {
    var list = el("layerList");
    list.innerHTML = "";
    var scores = f.layers || {};
    var top = null, topValue = 0;
    Object.keys(scores).forEach(function (k) {
      if (scores[k] > topValue) { topValue = scores[k]; top = k; }
    });

    LAYERS.forEach(function (entry) {
      var key = entry[0], label = entry[1];
      var value = Math.max(0, Math.min(1, scores[key] || 0));
      var li = document.createElement("li");
      if (key === top && topValue > 0.05) li.className = "lead";

      var name = document.createElement("span");
      name.className = "name";
      name.textContent = label;

      var bar = document.createElement("span");
      bar.className = "bar";
      var fill = document.createElement("i");
      fill.style.width = (value * 100) + "%";
      if (key === top && topValue > 0.05) fill.className = "hot";
      bar.appendChild(fill);

      var num = document.createElement("span");
      num.className = "num";
      num.textContent = value > 0.005 ? value.toFixed(2) : "–";

      li.appendChild(name);
      li.appendChild(bar);
      li.appendChild(num);
      list.appendChild(li);
    });
  }

  function renderSignals(f) {
    var list = el("signalList");
    var values = f.v || {};
    list.innerHTML = "";
    SIGNALS.forEach(function (entry) {
      var key = entry[0], label = entry[1], unit = entry[2], threshold = entry[3];
      if (values[key] === undefined) return;
      var value = values[key];
      var hot = key === "wifi_rssi_dbm" ? value <= threshold : value >= threshold;
      var li = document.createElement("li");
      var l = document.createElement("span");
      l.className = "label";
      l.textContent = label;
      var v = document.createElement("span");
      v.className = "value" + (hot ? " hot" : "");
      v.textContent = value + unit;
      li.appendChild(l);
      li.appendChild(v);
      list.appendChild(li);
    });
  }

  function updateLeadBadge(data) {
    var badge = el("leadBadge");
    if (data.leadSeconds && state.frame >= (data.alertIndex || 0)) {
      badge.hidden = false;
      badge.textContent = "warned " + minutes(data.leadSeconds) + " before it broke";
    } else if (data.benign) {
      badge.hidden = false;
      badge.textContent = "no alert raised";
    } else {
      badge.hidden = true;
    }
  }

  /* ---------------------------------------------------------------- chart */

  var NS = "http://www.w3.org/2000/svg";
  var W = 1000, H = 240, PAD = 10;

  function node(name, attrs) {
    var n = document.createElementNS(NS, name);
    Object.keys(attrs).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    return n;
  }

  function drawChart() {
    var svg = el("chart");
    var data = state.scenario;
    svg.innerHTML = "";
    if (!data || !data.frames.length) return;

    var frames = data.frames;
    var n = frames.length;
    var x = function (i) { return PAD + (i / (n - 1)) * (W - PAD * 2); };
    var y = function (h) { return PAD + (1 - (h || 0) / 100) * (H - PAD * 2); };

    // The period when things were actually bad.
    if (data.impactIndex !== null && data.impactIndex !== undefined) {
      svg.appendChild(node("rect", {
        x: x(data.impactIndex), y: PAD,
        width: Math.max(2, W - PAD - x(data.impactIndex)), height: H - PAD * 2,
        fill: "var(--crit)", opacity: "0.10"
      }));
      svg.appendChild(node("line", {
        x1: x(data.impactIndex), x2: x(data.impactIndex), y1: PAD, y2: H - PAD,
        stroke: "var(--crit)", "stroke-width": "1.5", "stroke-dasharray": "4 3", opacity: "0.8"
      }));
      svg.appendChild(label(x(data.impactIndex) + 7, PAD + 14, "it broke", "var(--crit)"));
    }

    // Where the agent first warned.
    if (data.alertIndex !== null && data.alertIndex !== undefined) {
      svg.appendChild(node("line", {
        x1: x(data.alertIndex), x2: x(data.alertIndex), y1: PAD, y2: H - PAD,
        stroke: "var(--watch)", "stroke-width": "2"
      }));
      svg.appendChild(label(x(data.alertIndex) - 6, PAD + 14, "warned", "var(--watch)", "end"));

      // The gap between the two, drawn as a measured span.
      if (data.impactIndex !== null && data.impactIndex !== undefined) {
        var mid = (x(data.alertIndex) + x(data.impactIndex)) / 2;
        var yArrow = H - PAD - 16;
        svg.appendChild(node("line", {
          x1: x(data.alertIndex), x2: x(data.impactIndex), y1: yArrow, y2: yArrow,
          stroke: "var(--ok)", "stroke-width": "1.5"
        }));
        [data.alertIndex, data.impactIndex].forEach(function (i) {
          svg.appendChild(node("line", {
            x1: x(i), x2: x(i), y1: yArrow - 4, y2: yArrow + 4,
            stroke: "var(--ok)", "stroke-width": "1.5"
          }));
        });
        svg.appendChild(label(mid, yArrow - 7, minutes(data.leadSeconds), "var(--ok)", "middle"));
      }
    }

    // Warm-up is drawn as explicitly not counted.
    if (data.warmFrom) {
      svg.appendChild(node("rect", {
        x: PAD, y: PAD, width: Math.max(0, x(data.warmFrom) - PAD), height: H - PAD * 2,
        fill: "var(--text-3)", opacity: "0.08"
      }));
      svg.appendChild(label(PAD + 6, H - PAD - 6, "warming up", "var(--text-3)"));
    }

    [25, 50, 75].forEach(function (level) {
      svg.appendChild(node("line", {
        x1: PAD, x2: W - PAD, y1: y(level), y2: y(level),
        stroke: "var(--text-3)", opacity: "0.15", "stroke-width": "1"
      }));
    });

    var d = frames.map(function (f, i) {
      return (i ? "L" : "M") + x(i).toFixed(1) + " " + y(f.h).toFixed(1);
    }).join(" ");

    svg.appendChild(node("path", {
      d: d + " L" + x(n - 1) + " " + (H - PAD) + " L" + x(0) + " " + (H - PAD) + " Z",
      fill: "var(--accent)", opacity: "0.10"
    }));
    svg.appendChild(node("path", {
      d: d, fill: "none", stroke: "var(--accent)", "stroke-width": "2",
      "vector-effect": "non-scaling-stroke", "stroke-linejoin": "round"
    }));

    var head = node("line", {
      x1: 0, x2: 0, y1: PAD, y2: H - PAD,
      stroke: "var(--text)", "stroke-width": "1.5", opacity: "0.75", id: "playhead"
    });
    svg.appendChild(head);
    var dot = node("circle", { cx: 0, cy: 0, r: 4, fill: "var(--accent)",
      stroke: "var(--surface)", "stroke-width": "2", id: "playdot" });
    svg.appendChild(dot);
  }

  function label(x, y, value, colour, anchor) {
    var t = node("text", {
      x: x, y: y, fill: colour, "font-size": "12", "font-weight": "600",
      "text-anchor": anchor || "start",
      "font-family": "system-ui, sans-serif"
    });
    t.textContent = value;
    return t;
  }

  function movePlayhead() {
    var data = state.scenario;
    if (!data) return;
    var n = data.frames.length;
    var head = document.getElementById("playhead");
    var dot = document.getElementById("playdot");
    if (!head || !dot) return;
    var px = PAD + (state.frame / (n - 1)) * (W - PAD * 2);
    var py = PAD + (1 - (data.frames[state.frame].h || 0) / 100) * (H - PAD * 2);
    head.setAttribute("x1", px); head.setAttribute("x2", px);
    dot.setAttribute("cx", px); dot.setAttribute("cy", py);
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
    el("playLabel").textContent = "Pause";
    el("playBtn").querySelector(".icon-play").classList.add("pause");
    el("playBtn").setAttribute("aria-label", "Pause");
    clearInterval(state.timer);
    // Each frame is 15 seconds of recorded time.
    state.timer = setInterval(tick, Math.max(40, 15000 / state.speed));
  }

  function pause() {
    state.playing = false;
    clearInterval(state.timer);
    var btn = el("playBtn");
    if (!btn) return;
    el("playLabel").textContent = "Play";
    btn.querySelector(".icon-play").classList.remove("pause");
    btn.setAttribute("aria-label", "Play");
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
        var target = document.querySelector(button.dataset.copy);
        if (!target || !navigator.clipboard) return;
        navigator.clipboard.writeText(target.textContent.trim()).then(function () {
          var original = button.textContent;
          button.textContent = "Copied";
          setTimeout(function () { button.textContent = original; }, 1400);
        });
      });
    });

    document.addEventListener("keydown", function (e) {
      if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
      if (e.code === "Space") { e.preventDefault(); state.playing ? pause() : play(); }
      if (e.code === "ArrowRight") { pause(); state.frame = Math.min(
        state.scenario.frames.length - 1, state.frame + 1); render(); }
      if (e.code === "ArrowLeft") { pause(); state.frame = Math.max(0, state.frame - 1); render(); }
    });

    window.addEventListener("resize", function () { drawChart(); render(); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    wire();
    loadIndex();
  });
})();
