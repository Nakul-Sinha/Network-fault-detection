/* NetPulse Local UI.
 *
 * Plain modules, no framework and no build step. The agent ships as one
 * Python package and adding a JavaScript toolchain to it would buy nothing
 * here: the whole surface is a handful of polled views.
 *
 * Every value rendered comes from the local agent over loopback. The token
 * is injected into the page by the server, so it never appears in a URL.
 */

(function () {
  "use strict";

  var TOKEN = window.NETPULSE_TOKEN || "";
  var POLL_MS = 5000;
  var SEVERITIES = ["info", "watch", "risk", "critical"];
  var LAYER_LABELS = {
    wifi: "Wi-Fi",
    os: "This device",
    dns: "DNS",
    gateway: "Your router",
    path: "Path to internet",
    remote_https: "Remote services",
    vpn: "VPN"
  };

  var state = { paused: false, learning: true, timelineHours: 24, lastIncidentId: null };

  function api(path, options) {
    options = options || {};
    var headers = { "X-NetPulse-Token": TOKEN };
    if (options.body) headers["Content-Type"] = "application/json";
    return fetch(path, {
      method: options.method || "GET",
      headers: headers,
      body: options.body ? JSON.stringify(options.body) : undefined
    }).then(function (response) {
      if (!response.ok) throw new Error(path + " returned " + response.status);
      return response.json();
    });
  }

  function el(id) { return document.getElementById(id); }

  function setText(id, text) {
    var node = el(id);
    if (node) node.textContent = text;
  }

  function severityClass(severity) {
    if (severity === "info") return "healthy";
    return SEVERITIES.indexOf(severity) >= 0 ? severity : "";
  }

  function severityLabel(severity, warming) {
    if (warming) return "learning";
    switch (severity) {
      case "info": return "healthy";
      case "watch": return "watch";
      case "risk": return "elevated risk";
      case "critical": return "trouble now";
      default: return "starting";
    }
  }

  function percent(value) {
    if (value === null || value === undefined) return "--";
    return Math.round(value * 100) + "%";
  }

  function timeLabel(ts) {
    if (!ts) return "";
    return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function dateTimeLabel(ts) {
    if (!ts) return "";
    return new Date(ts * 1000).toLocaleString();
  }

  /* ----------------------------------------------------------- rendering */

  function renderHealth(data) {
    var severity = data.severity || "info";
    var cls = severityClass(severity);

    setText("healthValue", data.health === null || data.health === undefined
      ? "--" : Math.round(data.health));
    var chip = el("severityChip");
    chip.textContent = severityLabel(severity, data.warming_up);
    chip.className = "chip " + cls;
    el("warmupChip").hidden = !data.warming_up;

    el("statusDot").className = "dot " + cls;

    setText("risk5", percent(data.risk_5m));
    setText("risk15", percent(data.risk_15m));
    setText("coverage", percent(data.coverage));
    bar("risk5Bar", data.risk_5m);
    bar("risk15Bar", data.risk_15m);

    setText("updated", data.updated_at
      ? "updated " + timeLabel(data.updated_at)
      : "waiting for the first reading");

    state.paused = !!data.paused;
    var pauseBtn = el("pauseBtn");
    pauseBtn.textContent = state.paused ? "Resume probing" : "Pause probing";
    pauseBtn.setAttribute("aria-pressed", String(state.paused));

    renderIncidentCard(data.incident, data);
  }

  function bar(id, value) {
    var node = el(id);
    if (!node) return;
    var pct = Math.max(0, Math.min(1, value || 0));
    node.style.width = (pct * 100) + "%";
    node.className = pct >= 0.75 ? "critical" : pct >= 0.5 ? "risk" : pct >= 0.25 ? "watch" : "";
  }

  function renderIncidentCard(incident, health) {
    var card = el("incidentCard");
    if (!incident) {
      var line = health.warming_up
        ? "Still learning what normal looks like on this network. Scores get sharper as it goes."
        : "Nothing unusual. The agent is watching " + percent(health.coverage) +
          " of the signals it knows about.";
      card.innerHTML = "";
      var p = document.createElement("p");
      p.className = "muted";
      p.textContent = line;
      card.appendChild(p);
      return;
    }

    card.innerHTML = "";
    var title = document.createElement("h3");
    title.textContent = incident.title;
    card.appendChild(title);

    var summary = document.createElement("p");
    summary.textContent = incident.summary;
    card.appendChild(summary);

    var meta = document.createElement("p");
    meta.className = "muted";
    meta.textContent = "Likely " + (LAYER_LABELS[incident.primary_layer] || incident.primary_layer) +
      " · confidence " + Math.round((incident.confidence || 0) * 100) + "%" +
      " · started " + timeLabel(incident.started_at);
    card.appendChild(meta);

    if (incident.evidence && incident.evidence.length) {
      var evidence = document.createElement("ul");
      evidence.className = "evidence";
      incident.evidence.slice(0, 4).forEach(function (item) {
        var li = document.createElement("li");
        li.textContent = item.label + ": " + item.value + (item.unit ? " " + item.unit : "");
        li.title = item.comparison || "";
        evidence.appendChild(li);
      });
      card.appendChild(evidence);
    }

    if (incident.remediation && incident.remediation.length) {
      var heading = document.createElement("p");
      heading.className = "muted";
      heading.textContent = "What to try";
      card.appendChild(heading);
      var list = document.createElement("ol");
      incident.remediation.forEach(function (text) {
        var li = document.createElement("li");
        li.textContent = text;
        list.appendChild(li);
      });
      card.appendChild(list);
    }
    state.lastIncidentId = incident.id;
  }

  function renderTimeline(data) {
    var svg = el("chart");
    var points = data.points || [];
    svg.innerHTML = "";
    if (points.length < 2) {
      setText("chartSummary", "Not enough history yet for this window.");
      return;
    }

    var width = 800, height = 180, pad = 6;
    var first = points[0].ts, last = points[points.length - 1].ts;
    var span = Math.max(1, last - first);
    var x = function (ts) { return pad + ((ts - first) / span) * (width - pad * 2); };
    var y = function (health) { return pad + (1 - health / 100) * (height - pad * 2); };

    (data.incidents || []).forEach(function (incident) {
      var start = x(Math.max(first, incident.started_at));
      var end = x(Math.min(last, incident.ended_at || last));
      var rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("x", String(start));
      rect.setAttribute("y", String(pad));
      rect.setAttribute("width", String(Math.max(2, end - start)));
      rect.setAttribute("height", String(height - pad * 2));
      rect.setAttribute("fill", "currentColor");
      rect.setAttribute("opacity", "0.1");
      svg.appendChild(rect);
    });

    [25, 50, 75].forEach(function (level) {
      var line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", String(pad));
      line.setAttribute("x2", String(width - pad));
      line.setAttribute("y1", String(y(level)));
      line.setAttribute("y2", String(y(level)));
      line.setAttribute("stroke", "currentColor");
      line.setAttribute("opacity", "0.12");
      svg.appendChild(line);
    });

    var d = points.map(function (point, index) {
      return (index === 0 ? "M" : "L") + x(point.ts).toFixed(1) + " " + y(point.health).toFixed(1);
    }).join(" ");

    var area = document.createElementNS("http://www.w3.org/2000/svg", "path");
    area.setAttribute("d", d + " L" + x(last).toFixed(1) + " " + (height - pad) +
      " L" + x(first).toFixed(1) + " " + (height - pad) + " Z");
    area.setAttribute("fill", "currentColor");
    area.setAttribute("opacity", "0.08");
    svg.appendChild(area);

    var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "2");
    path.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(path);

    var lowest = points.reduce(function (best, point) {
      return point.health < best.health ? point : best;
    }, points[0]);
    setText("chartSummary",
      points.length + " readings from " + timeLabel(first) + " to " + timeLabel(last) +
      " · lowest " + Math.round(lowest.health) + " at " + timeLabel(lowest.ts) +
      " · " + (data.incidents || []).length + " incidents");
  }

  function renderLayers(status) {
    var list = el("layerList");
    var scores = (status.score && status.score.layer_scores) || {};
    var caps = {};
    (status.capabilities || []).forEach(function (item) { caps[item.layer] = item; });

    list.innerHTML = "";
    Object.keys(LAYER_LABELS).forEach(function (layer) {
      var li = document.createElement("li");
      var name = document.createElement("span");
      name.textContent = LAYER_LABELS[layer];
      var wrap = document.createElement("div");
      wrap.className = "bar";
      var fill = document.createElement("i");
      var value = Math.max(0, Math.min(1, scores[layer] || 0));
      fill.style.width = (value * 100) + "%";
      wrap.appendChild(fill);
      var note = document.createElement("span");

      var capability = caps[layer];
      if (capability && !capability.enabled) {
        note.textContent = "off";
        li.className = "off";
      } else if (capability && capability.capability && !capability.capability.supported) {
        note.textContent = "unavailable";
        note.title = capability.capability.reason || "";
        li.className = "off";
      } else {
        note.textContent = value > 0.01 ? value.toFixed(2) : "normal";
      }
      li.appendChild(name);
      li.appendChild(wrap);
      li.appendChild(note);
      list.appendChild(li);
    });
  }

  function renderIncidents(incidents) {
    var list = el("incidentList");
    list.innerHTML = "";
    if (!incidents.length) {
      var empty = document.createElement("li");
      empty.className = "muted";
      empty.textContent = "None yet.";
      list.appendChild(empty);
      return;
    }
    incidents.slice(0, 12).forEach(function (incident) {
      var li = document.createElement("li");
      li.className = incident.severity;
      var title = document.createElement("strong");
      title.textContent = incident.title;
      var when = document.createElement("div");
      when.className = "when";
      when.textContent = dateTimeLabel(incident.started_at) +
        (incident.ended_at ? " – " + timeLabel(incident.ended_at) : " · ongoing") +
        " · " + (LAYER_LABELS[incident.primary_layer] || incident.primary_layer) +
        (incident.user_label ? " · you said: " + incident.user_label : "");
      li.appendChild(title);
      li.appendChild(when);
      list.appendChild(li);
    });
  }

  function renderProbes(probes) {
    var body = el("probeBody");
    body.innerHTML = "";
    probes.slice(0, 200).forEach(function (probe) {
      var tr = document.createElement("tr");
      [timeLabel(probe.ts), probe.collector, probe.target,
       probe.ok ? "ok" : (probe.detail || "failed"),
       probe.duration_ms === null || probe.duration_ms === undefined
         ? "" : Math.round(probe.duration_ms)
      ].forEach(function (value, index) {
        var td = document.createElement("td");
        td.textContent = String(value);
        if (index === 3 && !probe.ok) td.className = "fail";
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
  }

  function renderPrivacy(status, config) {
    var list = el("privacyFacts");
    list.innerHTML = "";
    var store = status.store || {};
    var facts = [
      "Database: " + (store.path || "local file") +
        " (" + Math.round((store.size_bytes || 0) / 1024) + " KB)",
      (store.samples || 0) + " observations kept, oldest " +
        (store.oldest_sample ? dateTimeLabel(store.oldest_sample) : "none"),
      "Raw data kept for " + config.retention.raw_days + " days, then summarised",
      "Network names stored as salted hashes: " + (config.privacy.hash_ssid ? "yes" : "no"),
      "Packet contents stored: never",
      "Product analytics: " + (config.privacy.product_analytics ? "on" : "off"),
      "Probes sent in the last hour: " + (store.probes_last_hour || 0)
    ];
    facts.forEach(function (text) {
      var li = document.createElement("li");
      li.textContent = text;
      list.appendChild(li);
    });

    state.learning = status.learning !== false;
    var btn = el("learningBtn");
    btn.textContent = state.learning ? "Pause learning" : "Resume learning";
    btn.setAttribute("aria-pressed", String(!state.learning));
    setText("versionLabel", "NetPulse Local " + (status.version || ""));
  }

  function renderCapabilities(status) {
    var list = el("capList");
    list.innerHTML = "";
    (status.capabilities || []).forEach(function (item) {
      var li = document.createElement("li");
      var supported = item.capability && item.capability.supported;
      li.className = supported ? "" : "no";
      li.textContent = item.name + " · " +
        (item.enabled ? (supported ? "available" : "unavailable") : "switched off") +
        (item.capability && item.capability.reason ? " · " + item.capability.reason : "") +
        " · every " + Math.round(item.interval_s) + "s";
      list.appendChild(li);
    });
  }

  /* -------------------------------------------------------------- actions */

  function toggleSection(buttonId, targetId) {
    var button = el(buttonId);
    var target = el(targetId);
    if (!button || !target) return;
    button.addEventListener("click", function () {
      var open = target.hasAttribute("hidden");
      if (open) target.removeAttribute("hidden"); else target.setAttribute("hidden", "");
      button.setAttribute("aria-expanded", String(open));
      button.textContent = open ? "Hide" : "Show";
    });
  }

  function wireActions() {
    el("pauseBtn").addEventListener("click", function () {
      api(state.paused ? "/api/resume" : "/api/pause", { method: "POST" }).then(refresh);
    });

    document.querySelectorAll("[data-verdict]").forEach(function (button) {
      button.addEventListener("click", function () {
        api("/api/label", {
          method: "POST",
          body: { verdict: button.dataset.verdict, note: "", window_minutes: 15 }
        }).then(function () {
          setText("feedbackResult", "Thanks, that is recorded.");
        }).catch(function (error) {
          setText("feedbackResult", "Could not record that: " + error.message);
        });
      });
    });

    el("exportBtn").addEventListener("click", function () {
      setText("privacyResult", "Building bundle...");
      api("/api/export", { method: "POST", body: { hours: 24, include_logs: true } })
        .then(function (result) {
          setText("privacyResult", "Saved to " + result.path +
            " (" + Math.round(result.bytes / 1024) + " KB)");
        })
        .catch(function (error) { setText("privacyResult", "Export failed: " + error.message); });
    });

    el("learningBtn").addEventListener("click", function () {
      api("/api/learning", { method: "POST", body: { enabled: !state.learning } }).then(refresh);
    });

    el("wipeBtn").addEventListener("click", function () {
      if (!window.confirm("Delete every observation stored on this machine? This cannot be undone.")) {
        return;
      }
      api("/api/wipe", { method: "POST" }).then(function () {
        setText("privacyResult", "All stored data deleted.");
        refresh();
      });
    });

    el("rangeSelect").addEventListener("change", function (event) {
      state.timelineHours = Number(event.target.value);
      loadTimeline();
    });

    toggleSection("probesToggle", "probeTableWrap");
    toggleSection("capToggle", "capList");
  }

  /* ------------------------------------------------------------- polling */

  function loadTimeline() {
    return api("/api/timeline?hours=" + state.timelineHours).then(renderTimeline);
  }

  function refresh() {
    return Promise.all([
      api("/api/health").then(renderHealth),
      api("/api/status"),
      api("/api/config"),
      api("/api/incidents?limit=20").then(renderIncidents),
      api("/api/probes?limit=200").then(renderProbes)
    ]).then(function (results) {
      var status = results[1];
      var config = results[2];
      renderLayers(status);
      renderPrivacy(status, config);
      renderCapabilities(status);
    }).catch(function (error) {
      setText("updated", "agent unreachable");
      console.error(error);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    wireActions();
    refresh();
    loadTimeline();
    window.setInterval(refresh, POLL_MS);
    window.setInterval(loadTimeline, POLL_MS * 6);
  });
})();
