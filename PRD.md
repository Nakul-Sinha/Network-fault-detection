# Product Requirements Document: Local Predictive Network Health Agent

**Codename:** NetPulse Local (working title; final name TBD)  
**Approach:** Hybrid Multi-Layer Predictive Health (HMPH)  
**Companion research:** [`Research.md`](./Research.md)  
**Status:** Greenfield — ready for engineering kickoff  
**Last updated:** 2026-08-03

---

## 1. Vision, Goals, and Non-Goals

### 1.1 Vision

A privacy-first desktop and server application that predicts user-visible network degradation (video freezes, game lag, SaaS stalls) **5–15 minutes ahead**, explains **which layer is failing** in plain language, and suggests concrete fixes—running entirely on the user’s laptop or server by default.

### 1.2 Goals

1. **Predict** elevated risk of QoE degradation with usable lead time (target median ≥ 5 minutes on dogfood corpus).  
2. **Fuse** Wi‑Fi + OS + DNS + traceroute + synthetic HTTPS (optional flow metadata) into one health model.  
3. **Explain** in non-expert language with layer attribution and remediation.  
4. **Stay local:** no PCAP/payloads by default; no mandatory cloud account for MVP.  
5. **Ship** as a real public product (installers for at least Windows + one of macOS/Linux in MVP; all three by v1).  
6. **Adapt** to concept drift in home/SMB environments without requiring labeled datasets from users.

### 1.3 Non-Goals

| Non-goal | Rationale |
| --- | --- |
| Compete with Datadog / ThousandEyes / Kentik / ExtraHop as enterprise NPM/DEM/NDR | Different buyer, data plane, and trust model |
| Full IDS/IPS or malware detection | Scope creep; Kitsune-like security AD is inspirational only |
| Custom hardware / NIC / Raspberry Pi appliance requirement | Software-only constraint |
| Mandatory deep packet inspection | Privacy and packaging complexity |
| Guaranteeing ISP-side root cause with legal certainty | Single vantage point is inherently limited; be honest in UX |
| Multi-tenant SaaS observability backend in MVP | Optional sync is post-MVP |

---

## 2. Target Users and Personas

### 2.1 Primary personas

**P1 — Home power user (“Alex”)**  
Works from home, games, joins daily video calls. Tired of guessing “router or ISP?” Wants a tray app, clear warnings before important calls, zero cloud upload of traffic.

**P2 — SMB / small office admin (“Sam”)**  
10–50 people, no NOC. Needs a server or always-on PC agent that flags shared Wi‑Fi vs WAN issues and produces screenshots/reports for the ISP or MSP.

**P3 — Privacy-conscious engineer (“Riley”)**  
Will only install if defaults are auditable, open-source core preferred, optional flows clearly gated, export/delete data easy.

### 2.2 Secondary

- MSP technicians using the agent as a lightweight customer-premises diagnostic (v2 packaging: portable report export).  
- Students / researchers reproducing HMPH baselines (open datasets + docs).

### 2.3 Anti-persona

Enterprise SREs shopping for fleet-wide NPM with BGP and PCAP—point them elsewhere; do not warp the roadmap to win that RFP.

---

## 3. Problem Statement and Success Metrics

### 3.1 Problem

Network problems felt by users are multi-layer and often **predicted too late**. Existing tools are either (a) enterprise/cloud-heavy, (b) single-layer (speedtest, ping), or (c) expert packet tools. Nobody in the consumer/SMB lane combines **cross-layer fusion**, **lookahead**, **local ML**, and **plain-language RCA** in one shippable agent.

### 3.2 Product success metrics

| Metric | MVP target | v1 target |
| --- | --- | --- |
| Install → first health score | < 5 minutes | < 3 minutes |
| Baseline warm-up before forecasts | ≤ 30 minutes | ≤ 15 minutes (transfer priors) |
| Forecast lead time (median, labeled bad events) | ≥ 5 min | ≥ 10 min |
| Precision of “elevated risk” alerts (dogfood) | ≥ 0.5 | ≥ 0.65 |
| User RCA agreement (“right layer?”) | ≥ 60% | ≥ 75% |
| Default noisy alerts / day | ≤ 2 | ≤ 1 |
| Idle CPU (laptop, AC power) | < 3% avg | < 2% |
| Idle RAM | < 250 MB | < 200 MB |
| Crash-free sessions (7-day) | ≥ 99% | ≥ 99.5% |
| Privacy: payloads stored by default | 0 | 0 |

### 3.3 Business / shipping metrics (public product)

- Public GitHub releases with signed installers.  
- Week-4 dogfood: ≥ 20 external users OR ≥ 5 SMB pilots.  
- NPS or “would recommend” ≥ 30 at v1 (directional).  
- Clear freemium funnel: core free; Pro unlocks multi-device, longer retention, report export (see §14).

---

## 4. Competitive Positioning and Novelty

### 4.1 Positioning statement

> For home and SMB users who need trustworthy answers about lag **before** the meeting fails, NetPulse Local is a **privacy-first predictive network health agent** that fuses Wi‑Fi, DNS, path, and application probes on your machine. Unlike enterprise DEM/NPM suites, it runs locally, forecasts degradation minutes ahead, and explains the failing layer in plain language.

### 4.2 Competitive frame

| vs | We win by… | We lose when… |
| --- | --- | --- |
| Speedtest / Downdetector | Continuous + predictive + layered RCA | User only wants a one-off bandwidth number |
| Router vendor apps | Cross-path + multi-app synthetics; OS vantage | User needs AP radio management UI |
| Prometheus/Grafana/Netdata | Opinionated QoE forecasts + consumer UX | User wants arbitrary metric DIY |
| ThousandEyes / Datadog | Price, privacy, local install, consumer copy | User needs global BGP/mesh fleet |
| Zeek/ntopng | Simplicity + no PCAP default | User needs forensic packet evidence |

### 4.3 Novelty (product claim)

**HMPH** — Hybrid Multi-Layer Predictive Health — as defined in `Research.md` §6: composition of cross-layer fusion, online/drift-aware learning, extreme-event forecasting, privacy-local ML, and non-expert RCA. Novelty is **shipped composition for end hosts**, not a claim of inventing autoencoders or traceroute.

---

## 5. Product Requirements

### 5.1 Functional requirements

| ID | Requirement | Priority |
| --- | --- | --- |
| F1 | Installable agent with system tray / menu bar presence | MVP |
| F2 | Continuous collectors: Wi‑Fi (when available), OS net stats, DNS timing, gateway ping, rate-limited traceroute, synthetic HTTPS | MVP |
| F3 | Local feature store with retention controls (default 7–14 days raw features) | MVP |
| F4 | Health score 0–100 updated at least every 60s | MVP |
| F5 | Risk forecast for 5 / 15 minute horizons with traffic-light bands | MVP |
| F6 | Plain-language incident cards: what / which layer / why we think so / what to try | MVP |
| F7 | OS notifications for elevated risk (user-configurable quiet hours) | MVP |
| F8 | Manual “Mark this as bad/good” labeling control | MVP |
| F9 | Pause probing / pause learning | MVP |
| F10 | Export diagnostic bundle (JSON/ZIP: features summary, not payloads) | MVP |
| F11 | Per-layer enable/disable | MVP |
| F12 | Optional flow metadata collector (explicit opt-in) | Should (v1) |
| F13 | Multi-profile targets (Work VPN vs Home) auto-detected | Should (v1) |
| F14 | Historical timeline UI with incident markers | Should (v1) |
| F15 | SMB report PDF/HTML for ISP escalation | v2 |
| F16 | Optional encrypted multi-device sync | v2 |
| F17 | Headless server mode + simple web UI on localhost | Should (MVP server SKU) |
| F18 | Plugin allowlist of synthetic HTTPS targets | MVP |

### 5.2 Non-functional requirements

| ID | Requirement | Notes |
| --- | --- | --- |
| N1 | **Privacy-local default** | No account; no telemetry upload unless user opts into anonymous product analytics (separate from network data) |
| N2 | **No payloads / no PCAP by default** | Document clearly; flow opt-in stores metadata only |
| N3 | **Cross-platform** | Windows 10/11 MVP; macOS 13+ and Linux (amd64/arm64) by v1 |
| N4 | **Performance budgets** | See §3.2; battery-aware backoff on laptops |
| N5 | **Probe ethics / safety** | Default targets: user’s gateway, well-known public resolvers user already uses, and 2–3 allowlisted HTTPS endpoints (configurable). Max probe rates enforced in code |
| N6 | **Permissions honesty** | Installer explains Wi‑Fi location/local network permissions (macOS), admin for some OS counters (Windows), capabilities for ICMP |
| N7 | **Security** | Autoupdate signatures; least privilege; localhost API bound to loopback; no remote shell |
| N8 | **Resilience** | Collector crashes must not kill UI; watchdog restart |
| N9 | **Accessibility** | Keyboard navigable UI; sufficient contrast; screen-reader labels on primary views |
| N10 | **Offline** | Full core loop works offline (except synthetic HTTPS to Internet—degrade to LAN/DNS/Wi‑Fi-only mode) |

### 5.3 Permissions matrix (MVP)

| Platform | Needed | Fallback if denied |
| --- | --- | --- |
| Windows | Normal user; optional elevation for some ETW/flow counters | HTTPS+DNS+ping still work |
| macOS | Local Network; Location **only if** required for Wi‑Fi SSID APIs—minimize ask | Hide Wi‑Fi layer; explain in UI |
| Linux | Capabilities for ICMP or use UDP traceroute; nl80211 for Wi‑Fi | Same layered degradation |

---

## 6. System Architecture

### 6.1 Logical components

1. **Agent core** — process supervisor, config, scheduling, IPC, autostart.  
2. **Collectors** — Wi‑Fi, OS, DNS, path, HTTPS synthetic, optional flows.  
3. **Feature store** — SQLite for relational/config + embedded TSDB (e.g. `duckdb` or `sqlite` time-series tables / `redb`) for dense samples.  
4. **ML stack (L0–L4)** — see §8.  
5. **UX shell** — Tauri (or equivalent) desktop UI + tray.  
6. **Notification bridge** — OS native notifications.  
7. **Optional cloud** (v2) — account, encrypted sync, license for Pro—never required for scoring.

### 6.2 ML layers (L0–L4)

| Layer | Role | Example techniques (from research) |
| --- | --- | --- |
| **L0** | Hard rules / safety | Gateway unreachable → immediate red; probe budget breaker |
| **L1** | Classical stats | EWMA, CUSUM, seasonal baselines (Donut-like seasonality) |
| **L2** | Online unsupervised | Small AE ensemble (Kitsune-inspired), streaming scores |
| **L3** | Predictive head | Lightweight classifier/regressor: P(degrade in 5/15m); extreme-event-aware loss (Outage-Watch-inspired) |
| **L4** | RCA / UX | Layer contribution + NetMedic-like probabilities → templates |

### 6.3 Data plane constraints

- Sample Wi‑Fi/OS every 10–30s.  
- DNS synthetic every 30–60s.  
- HTTPS synthetic every 60–120s (jittered).  
- Traceroute every 5–15 min, or on suspicion.  
- Persist features at 10–60s resolution; downsample after 48h.

### 6.4 Trust boundaries

```
[UI] ←IPC→ [Agent core] ←→ [Collectors]
                 ↓
          [Feature DB encrypted at rest optional]
                 ↓
          [ML sidecar / in-process ONNX]
                 ↓
          [Notifications]

Internet: only explicit synthetic probes + optional autoupdate.
```

---

## 7. Tech Stack Recommendation

### 7.1 Recommended stack (justified)

| Piece | Choice | Why |
| --- | --- | --- |
| Agent core | **Rust** | Safe concurrency, small footprint, solid cross-compile, good for long-running tray agents |
| Collectors | Rust crates + thin platform FFI | One binary where possible |
| ML training / research | **Python** (PyTorch) in repo `/ml` | Fast iteration; matches academic baselines (TranAD, etc.) |
| ML inference (shipped) | **ONNX Runtime** inside agent | Avoid shipping full Python to every user in MVP if possible; Python sidecar acceptable for alpha |
| UI | **Tauri 2 + TypeScript + Vite** | Native webview, small installers vs Electron |
| Local DB | **SQLite** (+ SQL migrations) | Universal, auditable, easy export |
| Packaging | `cargo` + Tauri bundler; WiX/NSIS (Win), `.dmg`/`.app` (Mac), `.deb`/AppImage (Linux) | Real public ship path |
| CI | GitHub Actions matrix Win/Mac/Linux | Signed release artifacts |

**Alternative considered:** Go agent + Python sidecar always-on. Rejected as default because dual-runtime packaging and RAM budget are harder for a tray app; keep Python for training and optional power-user “research mode.”

**Unified approach (if team is Python-only):** ship PyInstaller/briefcase agent for alpha only; migrate hot path to Rust/ONNX before public v1.

### 7.2 Repository layout (suggested)

```
/apps/desktop          # Tauri UI
/crates/agent          # Rust agent core
/crates/collectors     # platform collectors
/crates/features       # feature schemas
/crates/infer          # ONNX wrappers
/ml                    # training, notebooks, eval
/docs                  # Research.md, PRD.md (root copies OK)
/packaging             # scripts
/tests                 # e2e, probe mocks
```

### 7.3 Third-party services (MVP)

- None required.  
- Autoupdate: GitHub Releases or self-hosted static bucket.  
- Crash reports: opt-in only (Sentry-like), scrubbed.

---

## 8. Data Model and ML Pipeline

### 8.1 Core entities

**`samples`** — timestamped feature vectors  
Fields (illustrative): `ts`, `wifi_rssi`, `wifi_tx_retries`, `dns_p50_ms`, `dns_fail_rate`, `gw_rtt_ms`, `path_hop_p95`, `https_tcp_ms`, `https_tls_ms`, `https_ttfb_ms`, `os_retrans`, `vpn_active`, `ssid_hash`, …

**`scores`** — `ts`, `health_0_100`, `risk_5m`, `risk_15m`, `l2_anomaly`, `layer_probs` JSON  

**`incidents`** — `id`, `start`, `end`, `primary_layer`, `summary`, `remediation_ids`, `user_label`  

**`config`** — probe targets, budgets, quiet hours, layer toggles, retention  

**`labels`** — user feedback rows joined to time windows  

### 8.2 Feature engineering

- Rolling windows: 1m / 5m / 15m means, stds, slopes, z-scores.  
- Cross-layer ratios: e.g. `https_ttfb` rising while `gw_rtt` stable → not LAN.  
- Categorical: VPN on/off, wifi vs ethernet, hour-of-day (for seasonality).  
- **Never store DNS query names in plain text by default**—hash or retain only timing aggregates (configurable advanced mode may keep domain allowlist timings only).

### 8.3 Training pipeline

1. **Offline:** train L3 on labeled windows from dogfood + fault injection; export ONNX.  
2. **On-device L1/L2:** fit baselines during first 30 minutes; continual update with learning-rate caps.  
3. **Drift:** daily/hourly PSI on key features; if drift, refresh L1 seasonality and reset L2 AE; L3 uses last shipped weights until next app update (v1 may allow local fine-tune).  
4. **Eval gates in CI:** replay synthetic fault scenarios; block release if precision/lead-time regress beyond threshold.

### 8.4 RCA algorithm (MVP — deterministic + scores)

```
inputs: layer feature deltas, L2 channel contributions, L0 rules
score each layer ∈ {wifi, os, dns, path, remote_https, vpn}
primary = argmax; secondary = next if within 0.15
select template(primary, secondary, severity)
attach 1–3 remediation actions from catalog
```

Remediation catalog examples: move closer to AP; toggle 5 GHz; flush DNS; disable VPN test; reboot gateway; try wired; contact ISP with export bundle.

---

## 9. Roadmap: MVP → v1 → v2

### 9.1 Timeline overview (calendar estimates for a 3–4 person team)

| Phase | Duration | Outcome |
| --- | --- | --- |
| **M0 — Foundations** | 3 weeks | Agent skeleton, collectors stubbed, SQLite, Tauri shell, fake scores |
| **MVP** | +7 weeks (≈ week 10) | Predictive health on Win + Linux headless; dogfoodable |
| **v1** | +8 weeks (≈ week 18) | macOS parity, opt-in flows, polish, public launch |
| **v2** | +10–14 weeks | SMB reports, sync, stronger L3, MSP export |

### 9.2 Milestones and deliverables

**M0 (weeks 0–3)**  
- Repo scaffolding, CI, signed-dev builds  
- Collector interfaces + mock data replay  
- UI: health score placeholder, settings  

**MVP (weeks 4–10)**  
- Real collectors on Windows; Linux server mode  
- L0–L2 live; L3 v0 model (even logistic regression on engineered features is OK if calibrated)  
- RCA templates (≥ 12 scenarios)  
- Notifications, labeling, export  
- Internal dogfood + eval harness  

**v1 (weeks 11–18)**  
- macOS collectors + notarization path  
- Drift monitors + quieter alerting  
- Optional flow metadata  
- Website + docs + public OSS core  
- Paid Pro flag (license key) even if features slim  

**v2**  
- Multi-device, PDF ISP reports, advanced forecast models (TranAD-class distilled), localhost web UI themes for SMB rack PCs  

---

## 10. Detailed MVP Scope

### 10.1 Must have

- Windows tray app + Linux headless agent with localhost UI **or** CLI status.  
- Collectors: OS net basics, DNS timing, gateway RTT, traceroute (best effort), HTTPS synthetics to allowlist, Wi‑Fi where APIs allow.  
- Health score + 5/15m risk.  
- ≥ 12 plain-language RCA templates covering Wi‑Fi, DNS, gateway, ISP path, remote service, VPN.  
- Local DB + retention slider.  
- Pause, quiet hours, export bundle.  
- Unit tests for features; integration test with recorded fixtures.  
- Privacy policy / in-app privacy center (short).  

### 10.2 Should have

- Battery-aware probe backoff.  
- Ethernet vs Wi‑Fi detection in RCA.  
- Auto-detect captive portal (block false ISP outages).  
- Simple onboarding wizard (3 screens).  

### 10.3 Won’t have (MVP)

- Cloud accounts, mobile apps, BGP, PCAP UI, auto-remediation that changes system DNS/VPN without consent, Kubernetes cluster monitoring, LLM cloud explanations.

---

## 11. UX Requirements

### 11.1 Primary surfaces

1. **Tray flyout** — health score, next-15m risk chip, one-line status.  
2. **Home** — large score, forecast strip, “What’s going on” card.  
3. **Timeline** — last 24h (MVP can be simplified sparkline).  
4. **Incident detail** — layer diagram (simple 5-block), confidence, remediations (buttons that deep-link to OS settings where possible).  
5. **Settings** — layers, targets, privacy, retention, notifications.  
6. **Privacy center** — what we collect; delete all data; export.

### 11.2 Copy principles

- No jargon without expansion (“DNS (the internet’s phone book)”).  
- Prefer “likely” / “confidence” over false certainty.  
- Remediations are imperative and short (max 3).  
- Never blame ISP without path evidence.

### 11.3 Visual

- Functional tool UI is fine (this is a utility, not a marketing landing page).  
- Use clear state colors: healthy / watch / risk / offline.  
- Avoid alarmist red for yellow risk.

### 11.4 Notification rules

- Cooldown ≥ 30 minutes per primary layer unless severity jumps.  
- Quiet hours default 22:00–07:00 local (user editable).  
- “Call starting soon” mode (user sets calendar-less manual boost sensitivity) — Should.

---

## 12. Security, Privacy, and Legal

### 12.1 Privacy

- Default: all network features stay on disk locally.  
- Domain names: aggregated/hashed by default.  
- No payload bytes.  
- Opt-in product analytics separate from network feature DB (build flags).  
- One-click wipe.

### 12.2 Security

- Loopback-only control API with random token in user-only file.  
- Signed updates.  
- Sandbox UI; agent validates probe URL allowlist (block `file://`, link-local abuse, huge ports scans).  
- Dependency scanning in CI.

### 12.3 Legal / compliance posture

- Publish Privacy Policy + open-source licenses.  
- Synthetic probes: document outbound destinations; allow enterprise allowlist mode.  
- Export bundles may contain network environment metadata—warn before share.  
- GDPR-style data minimization even if not EU-only: retention limits, deletion, no sale of network data.  
- Do not claim “ISP fault” as legal proof.

---

## 13. Testing, Evaluation, and Labeling

### 13.1 Test levels

| Level | What |
| --- | --- |
| Unit | Feature math, RCA selection, rate limiter |
| Fixture integration | Replay pcap-free JSONL collector fixtures → scores |
| System | Installer smoke on CI VMs |
| Fault injection lab | tc/netem (Linux), clumsy/toxiproxy-like HTTPS delays, DNS blackhole containers |
| Dogfood | Weekly precision/lead-time review |

### 13.2 Labeling strategy

1. In-app **Bad / OK / Unsure** with optional note.  
2. Auto weak labels: Zoom/Teams “poor network” OS signals when available; game RTP jitter if exposed—best effort.  
3. Lab ground truth from injected faults.  
4. Never block MVP on perfect labels—ship L1/L2 value first; improve L3 with accumulating labels.

### 13.3 Eval dashboards (internal)

- Lead time CDF, precision/recall by layer, alert rate, drift events, collector coverage % by OS.

---

## 14. Go-to-Market and Shipping Plan

### 14.1 Distribution model

**Open-source core + freemium binary**

| Tier | Includes |
| --- | --- |
| **Community (free)** | Local agent, core collectors, health + forecast, RCA templates, 7-day history |
| **Pro** | 90-day history, PDF/HTML reports, multi-profile, priority probe packs, optional sync (v2), email support |
| **OSS core** | Agent collectors + feature schemas + L0/L1 + eval harness under Apache-2.0 or MIT; model weights may be dual-license |

### 14.2 Launch sequence

1. Private dogfood (team + friends).  
2. Public beta on GitHub Discussions.  
3. Show HN / Reddit r/selfhosted / r/networking (honest privacy framing).  
4. SMB pilots via simple landing page.  
5. v1 launch blog: “Predict lag 15 minutes early—on your laptop, offline.”

### 14.3 Packaging checklist (public ship)

- [ ] Code-signed Windows installer  
- [ ] macOS notarization (v1)  
- [ ] Linux packages + systemd unit  
- [ ] SBOM + license attribution  
- [ ] Crash opt-in  
- [ ] Uninstall cleans data with prompt  

---

## 15. Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Models cry wolf | Strict cooldowns; default conservative thresholds; user sensitivity slider |
| macOS permission hell | Excellent denied-state UX; ethernet-first messaging |
| Users hate active probes | Transparent probe log; budgets; pause |
| Attribution ambiguity | Soft language; show evidence chips (charts) |
| Scope creep to NDR | Enforce non-goals in sprint reviews |
| Small team bandwidth | MVP L3 can be linear/GBDT ONNX—not TranAD day one |
| Liability for “ISP is at fault” | UX disclaimers; export as “observations” |

---

## 16. Open Questions

1. Final product name / trademark?  
2. Apache-2.0 vs GPL for OSS core (copyleft may scare Pro dual-license)?  
3. Is Python sidecar acceptable for public beta, or ONNX-only from day one?  
4. Default HTTPS allowlist endpoints (own status endpoints vs public static assets)—legal review?  
5. Do we store SSID in plain text or HMAC with per-install salt only?  
6. Server SKU: separate binary or same with `NETPULSE_HEADLESS=1`?  
7. Pricing point for Pro ($4/mo vs $40 lifetime)?  
8. Will we accept community model weight contributions without privacy review pipeline?

---

## 17. Appendix: Research Papers → Product Features

| Paper (see Research.md) | Product feature mapping |
| --- | --- |
| Kitsune (R1) | L2 online unsupervised ensemble on local features |
| ENCODE (R2) | v1 optional flow metadata encoding |
| Intra-flow degradation (R3) | Short-horizon precursors for 5–15m forecast features |
| Inter-flow degradation (R4) | Cross-app correlation in incident grouping |
| TranAD (R5) | v1/v2 multivariate model candidate (distill to ONNX) |
| Anomaly Transformer (R6) | Explainability cues / research baseline |
| Outage-Watch (R7) | Extreme-event loss / rare lag spike emphasis in L3 |
| LEAF (R8) | Drift-aware adaptation policy |
| Donut (R9) | Seasonal KPI baselines for DNS/HTTPS latency |
| DeepLog (R10) | Optional OS log channel post-MVP |
| Pingmesh (R11) | Philosophy of continuous lightweight active probing |
| 007 (R12) | End-host vantage for localization narrative |
| NetBouncer (R13) | Probe design / interpretation caution |
| PreFix (R14) | Prediction-before-failure product framing |
| Label-free drift (R15) | Drift monitors without user labels |
| Sherlock (R16) | Layered dependency sketch in UX |
| NetMedic (R17) | Probabilistic layer diagnosis → copy |
| Putina & Rossi (R18) | Streaming telemetry AD patterns + eval ideas |
| PyRCA (OSS) | Offline RCA experimentation |
| AIOps+LLM survey | v2 natural-language rewrite of templates (optional, local or cloud opt-in) |

### Appendix B — MVP acceptance checklist (engineering)

- [ ] Fresh Windows install shows score < 5 minutes  
- [ ] With DNS blackhole injection, primary layer = DNS within 2 minutes  
- [ ] With Wi‑Fi RSSI drop fixture, RCA mentions Wi‑Fi  
- [ ] With netem delay on HTTPS path only, layer ≠ Wi‑Fi  
- [ ] Disk after 1 hour contains no packet payloads  
- [ ] Pause stops all probes (verified by probe counter)  
- [ ] Export ZIP opens and redacts secrets (tokens)  
- [ ] Idle CPU budget held under load of default probes  

---

## Document history

| Version | Date | Notes |
| --- | --- | --- |
| 0.1 | 2026-08-03 | Initial PRD from research synthesis; greenfield scaffold |

*Engineering may create ADRs under `/docs/adr` for stack deviations; update this PRD when milestones slip more than 2 weeks or when MVP must/won’t lists change.*
