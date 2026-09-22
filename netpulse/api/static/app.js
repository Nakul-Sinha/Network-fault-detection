/* NetPulse Local dashboard.
 *
 * The job of this page is to answer one question for someone who is not a
 * network engineer: is my connection about to let me down, and if so what do
 * I do. Everything else is real and available, but folded away, because a
 * person glancing at this before a call should not have to parse a layer
 * attribution table to learn that they are fine.
 *
 * Scores are translated into sentences here rather than printed as numbers.
 * "83/100" tells a user almost nothing; "your network looks healthy" tells
 * them what they came to find out.
 */

(function () {
  "use strict";

  var TOKEN = window.NETPULSE_TOKEN || "";
  var POLL_MS = 5000;
  var TREND_MS = POLL_MS * 6;
  var SPARK = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"];

  var LAYER_NAMES = {
    wifi: "Wi-Fi",
    os: "this device",
    dns: "DNS",
    gateway: "your router",
    path: "the path out",
    remote_https: "the sites you reach",
    vpn: "your VPN"
  };

  var state = { paused: false, open: false, lastSeverity: null };

  function el(id) { return document.getElementById(id); }
  function text(id, v) { var n = el(id); if (n) n.textContent = v; }

  function api(path, options) {
    options = options || {};
    options.headers = Object.assign({ "X-NetPulse-Token": TOKEN }, options.headers || {});
    if (options.body) options.headers["Content-Type"] = "application/json";
    return fetch(path, options).then(function (r) {
      if (!r.ok) throw new Error(r.status + " " + r.statusText);
      return r.json();
    });
  }

  /* ------------------------------------------------- the plain-English bit */

  /** Turn a score into the sentence a person actually wants. */
  function verdictFor(d) {
    if (d.paused) {
      return {
        cls: "", pulse: "", eyebrow: "paused",
        title: "Probing is paused",
        because: "The agent is not sending anything or measuring anything until you resume it."
      };
    }
    if (d.warming_up) {
      return {
        cls: "", pulse: "warn", eyebrow: "getting started",
        title: "Getting to know your network",
        because: "It needs about half an hour of quiet observation to learn what normal " +
                 "looks like here. Until then it will not guess."
      };
    }

    var incident = d.incident;
    var layer = LAYER_NAMES[d.primary_layer] || "something";

    if (d.severity === "critical") {
      return {
        cls: "bad", pulse: "bad", eyebrow: "right now",
        title: incident ? incident.title : "Something is wrong now",
        because: incident ? incident.summary
          : "Your connection is having real trouble, and " + layer + " looks like the cause."
      };
    }
    if (d.severity === "risk") {
      return {
        cls: "risk", pulse: "bad", eyebrow: "heads up",
        title: incident ? incident.title : "Trouble is likely soon",
        because: incident ? incident.summary
          : "Based on the last few minutes, " + layer + " is likely to cause problems shortly."
      };
    }
    if (d.severity === "watch") {
      return {
        cls: "watch", pulse: "warn", eyebrow: "keeping an eye out",
        title: "Something looks slightly off",
        because: incident ? incident.summary
          : "Nothing is broken. " + capitalise(layer) + " is drifting from its usual pattern, " +
            "so the agent is watching it more closely."
      };
    }
    return {
      cls: "ok", pulse: "live", eyebrow: "your network",
      title: "Your network looks healthy",
      because: nextFifteen(d)
    };
  }

  function nextFifteen(d) {
    var risk = d.risk_15m || 0;
    if (risk >= 0.25) {
      return "Nothing wrong right now, though there are early signs worth watching " +
             "over the next quarter hour.";
    }
    return "Nothing is wrong, and nothing looks like it is about to go wrong in the " +
           "next fifteen minutes.";
  }

  function capitalise(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

  /* --------------------------------------------------------------- render */

  function refresh() {
    return api("/api/health").then(function (d) {
      state.paused = !!d.paused;
      var v = verdictFor(d);

      el("verdict").className = "verdict " + v.cls;
      text("verdict", v.title);
      text("because", v.because);
      text("eyebrow", v.eyebrow);

      el("pulse").className = "pulse " + v.pulse;
      text("barState", d.paused ? "paused" : d.warming_up ? "learning" : "live");

      renderTodo(d);
      renderDetails(d);

      text("updated", d.updated_at
        ? "updated " + new Date(d.updated_at * 1000).toLocaleTimeString()
        : "");
      el("offstage").hidden = true;
      state.lastSeverity = d.severity;
    }).catch(function () {
      el("pulse").className = "pulse";
      text("barState", "not connected");
      el("offstage").hidden = false;
      text("offstage", "Lost contact with the agent. Is it still running in your terminal?");
    });
  }

  function renderTodo(d) {
    // Advice belongs to a problem happening now. An incident can still be
    // open while the score has already recovered, and showing "try this"
    // under "your network looks healthy" reads as a contradiction and
    // teaches people to ignore the panel.
    var current = d.severity === "watch" || d.severity === "risk" || d.severity === "critical";
    var steps = (current && d.incident && d.incident.remediation) || [];
    var host = el("todo");
    if (!steps.length || d.warming_up || d.paused) { host.hidden = true; return; }
    var list = el("todoList");
    list.innerHTML = "";
    steps.slice(0, 3).forEach(function (s) {
      var li = document.createElement("li");
      li.textContent = s;
      list.appendChild(li);
    });
    host.hidden = false;
  }

  function renderDetails(d) {
    text("dHealth", d.health === null || d.health === undefined ? "--" : d.health + " / 100");
    text("dRisk", pct(d.risk_5m) + "  /  " + pct(d.risk_15m));
    text("dCoverage", pct(d.coverage));
    text("pauseBtn", d.paused ? "resume probing" : "pause probing");
  }

  function pct(v) {
    return v === null || v === undefined ? "--" : Math.round(v * 100) + "%";
  }

  /* ---------------------------------------------------------- the trend */

  function loadTrend() {
    return api("/api/timeline?hours=1").then(function (d) {
      var points = d.points || [];
      var width = Math.max(24, Math.min(64, Math.floor(window.innerWidth / 13)));
      if (points.length < 2) {
        el("spark").innerHTML = '<span class="n">' + "▁".repeat(width) + "</span>";
        text("trendNote", "not enough history yet");
        return;
      }

      var out = "";
      for (var c = 0; c < width; c++) {
        var lo = Math.floor((c / width) * points.length);
        var hi = Math.max(lo + 1, Math.floor(((c + 1) / width) * points.length));
        var worst = 100;
        for (var i = lo; i < hi && i < points.length; i++) {
          if (points[i].health < worst) worst = points[i].health;
        }
        var idx = Math.max(0, Math.min(7, Math.round((worst / 100) * 7)));
        var cls = worst >= 70 ? "" : worst >= 35 ? "w" : "r";
        out += cls ? '<span class="' + cls + '">' + SPARK[idx] + "</span>" : SPARK[idx];
      }
      el("spark").innerHTML = out;

      var bad = points.filter(function (p) {
        return p.severity === "risk" || p.severity === "critical";
      }).length;
      text("trendNote", bad
        ? "last hour · " + bad + " reading" + (bad === 1 ? "" : "s") + " with elevated risk"
        : "last hour · steady");
    }).catch(function () { /* the status line already reports a lost agent */ });
  }

  function loadLayers() {
    return api("/api/status").then(function (d) {
      var scores = (d.score && d.score.layer_scores) || {};
      var lines = Object.keys(LAYER_NAMES).map(function (key) {
        var v = Math.max(0, Math.min(1, scores[key] || 0));
        var filled = Math.round(v * 22);
        var cls = v >= 0.5 ? "hot" : v >= 0.2 ? "mid" : "cool";
        return '<span class="name">' + padRight(LAYER_NAMES[key], 20) + "</span>" +
          '<span class="' + cls + '">' + "█".repeat(filled) + "</span>" +
          '<span class="cool">' + "█".repeat(22 - filled) + "</span>" +
          (v > 0.005 ? "  " + v.toFixed(2) : "");
      });
      el("dLayers").innerHTML = lines.join("\n");
    }).catch(function () {});
  }

  function padRight(s, n) { return s.length >= n ? s : s + " ".repeat(n - s.length); }

  function loadIncidents() {
    return api("/api/incidents?limit=5").then(function (d) {
      var items = Array.isArray(d) ? d : d.incidents || [];
      var host = el("dIncidents");
      if (!items.length) { host.innerHTML = '<p class="none">none yet</p>'; return; }
      host.innerHTML = "";
      items.forEach(function (i) {
        var div = document.createElement("div");
        div.className = "item";
        var when = new Date(i.started_at * 1000).toLocaleString();
        div.innerHTML = '<div>' + escapeHtml(i.title) + "</div>" +
          '<div class="when">' + escapeHtml(when) + (i.active ? " · still open" : "") + "</div>";
        host.appendChild(div);
      });
    }).catch(function () {});
  }

  function loadProbes() {
    return api("/api/probes?limit=12").then(function (d) {
      var items = Array.isArray(d) ? d : d.probes || [];
      var host = el("dProbes");
      if (!items.length) { host.innerHTML = '<p class="none">nothing sent yet</p>'; return; }
      host.innerHTML = "";
      items.forEach(function (p) {
        var div = document.createElement("div");
        div.className = "item";
        var ms = p.duration_ms === null || p.duration_ms === undefined
          ? "" : " · " + Math.round(p.duration_ms) + " ms";
        div.innerHTML =
          '<span class="' + (p.ok ? "ok" : "fail") + '">' + (p.ok ? "ok  " : "fail") + "</span> " +
          escapeHtml(p.collector) + " → " + escapeHtml(p.target) +
          '<div class="when">' + escapeHtml(new Date(p.ts * 1000).toLocaleTimeString()) +
          escapeHtml(ms) + "</div>";
        host.appendChild(div);
      });
    }).catch(function () {});
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  /* -------------------------------------------------------------- actions */

  function note(message) {
    text("actionNote", message);
    setTimeout(function () { text("actionNote", ""); }, 5000);
  }

  function wire() {
    el("detailsToggle").addEventListener("click", function () {
      state.open = !state.open;
      el("details").hidden = !state.open;
      this.setAttribute("aria-expanded", String(state.open));
      this.textContent = state.open ? "hide details" : "details";
      if (state.open) { loadLayers(); loadIncidents(); loadProbes(); }
    });

    document.querySelectorAll("[data-label]").forEach(function (b) {
      b.addEventListener("click", function () {
        api("/api/label", {
          method: "POST",
          body: JSON.stringify({ verdict: b.dataset.label, note: "", window_minutes: 15 })
        }).then(function () {
          note("Thanks. That feedback is stored locally and helps the agent improve.");
        }).catch(function () { note("Could not save that."); });
      });
    });

    el("pauseBtn").addEventListener("click", function () {
      api(state.paused ? "/api/resume" : "/api/pause", { method: "POST" })
        .then(function () { refresh(); })
        .catch(function () { note("Could not change that."); });
    });

    el("exportBtn").addEventListener("click", function () {
      note("Writing a report…");
      api("/api/export", { method: "POST", body: JSON.stringify({ hours: 24, include_logs: true }) })
        .then(function (d) { note("Saved to " + d.path); })
        .catch(function () { note("Could not write the report."); });
    });

    el("wipeBtn").addEventListener("click", function () {
      if (!window.confirm(
        "Delete every measurement this agent has stored on this machine?\n\n" +
        "This cannot be undone."
      )) return;
      api("/api/wipe", { method: "POST" })
        .then(function () { note("All stored data deleted."); refresh(); loadTrend(); })
        .catch(function () { note("Could not delete the data."); });
    });

    var resizeTimer;
    window.addEventListener("resize", function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(loadTrend, 200);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    wire();
    refresh();
    loadTrend();
    window.setInterval(refresh, POLL_MS);
    window.setInterval(loadTrend, TREND_MS);
    window.setInterval(function () {
      if (state.open) { loadLayers(); loadIncidents(); loadProbes(); }
    }, TREND_MS);
  });
})();
