# Research Knowledge Base: Local Predictive Network Health

**Product working title:** Local Predictive Network Health Agent  
**Novel approach:** Hybrid Multi-Layer Predictive Health (HMPH)  
**Scope:** Software-only (laptop / server); privacy-local by default; shippable public product  
**Last updated:** 2026-08-03

---

## 1. Executive Summary and Problem Framing

User-visible network degradation—video freezes, game lag spikes, SaaS page stalls—rarely begins as a clean “link down” event. It usually emerges from interacting failures across layers: Wi‑Fi airtime and retries, OS socket/buffer pressure, DNS latency and NXDOMAIN storms, last-mile or ISP path congestion visible in traceroute/RTT, and application-layer HTTPS timing. Enterprise observability (Datadog Network Monitoring, ThousandEyes, Kentik, ExtraHop) is optimized for fleets, deep packet/flow visibility, and NOC workflows. Home users, freelancers, and small offices lack a **local-first** tool that (1) fuses those layers without shipping PCAP or payloads to the cloud, (2) **forecasts** degradation 5–15 minutes ahead rather than alerting after the call drops, and (3) explains the failing layer in plain language.

This knowledge base synthesizes verified academic work and industry practice into a buildable research foundation for a **Local Predictive Network Health Agent**. The proposed technical thesis is **Hybrid Multi-Layer Predictive Health (HMPH)**: cross-layer fusion (Wi‑Fi + OS + DNS + traceroute + synthetic HTTPS, with optional flow metadata), online learning under concept drift, explainable root-cause analysis (RCA) for non-experts, privacy-local ML, and hybrid classical statistics + unsupervised models + a lightweight predictive head aimed at QoS extreme events—not just post-breach anomaly alerts.

**Compete against:** consumer “is the internet down?” checkers, opaque ISP apps, and generic host metric dashboards.  
**Do not compete against:** enterprise NPM / DEM / NDR platforms on their own turf (fleet scale, PCAP, BGP, WAN fabrics).

---

## 2. Field Landscape

### 2.1 Academia

Research clusters that matter for this product:

| Cluster | What it contributes | Gaps for a consumer/SMB local agent |
| --- | --- | --- |
| **Data-center / ISP fault localization** | Active probing meshes, tomography, boolean/dependency RCA (Pingmesh, 007, NetBouncer, Sherlock, NetMedic) | Assumes controlled fabric, privileged telemetry, operator personas |
| **Online / unsupervised network AD** | Streaming autoencoders and flow encodings (Kitsune, ENCODE, Putina & Rossi) | Often security-oriented (intrusion) rather than QoE lag forecasting |
| **Service degradation from flows** | Intra- and inter-flow early degradation signals (Bicski & Pekar) | Needs flow export; privacy and privilege on end hosts vary |
| **Time-series AD & prediction** | Transformers / reconstruction / forecasting (TranAD, Anomaly Transformer, Donut, Outage-Watch, LEAD, TUBO) | Usually single-domain metrics; weak cross-layer consumer UX |
| **Concept drift** | Cellular LEAF; label-free drift assessment | Home Wi‑Fi / SMB path regimes change constantly; few end-host products apply this |
| **RCA / causal / graph methods** | NetCause, GraphIDS, UniGAD, PyRCA ecosystem | Heavy graphs or security graphs; need simplification for non-experts |
| **AIOps surveys** | Taxonomies for failure mgmt, TSAD in ops | Bridge from cloud AIOps to local desktop agent is under-served |

### 2.2 Industry products (competitive context)

| Product / class | Strength | Why HMPH is different |
| --- | --- | --- |
| **Datadog Network Monitoring** | Cloud NPM, process ↔ socket correlation | Cloud-first, enterprise pricing, not consumer RCA |
| **ThousandEyes** | Internet / WAN path & app DEM | Agent fleets for enterprises; not privacy-local home ML |
| **Kentik** | Flow / BGP analytics at scale | Operator tooling; not laptop QoE lookahead |
| **ExtraHop** | NDR / wire data | Appliance / PCAP-centric; security posture |
| **Elastic ML / Prometheus+Grafana / Netdata ML** | Metric AD on host/infra | Rarely fuses Wi‑Fi↔DNS↔path↔HTTPS with plain-language RCA |
| **ntopng, Zeek, Suricata** | Deep traffic visibility | Powerful but expert-facing; payload/flow heavy by nature |
| **NALA & early local-first tools** | Local network awareness directionally aligned | Opportunity: stronger prediction + multi-layer fusion + shippable UX |

**Positioning takeaway:** Ship a **local predictive health** product with path/DNS/Wi‑Fi fusion, 5–15 minute lookahead, and consumer-grade RCA—not another enterprise observability clone.

---

## 3. Curated Reference Library (18 Core Papers)

The following **18** works are the primary design references. Each entry includes citation metadata, a concise summary, and **product relevance** for HMPH.

### R1 — Kitsune (online unsupervised ensemble)

**Mirsky, Y., Doitshman, T., Elovici, Y., & Shabtai, A.** *Kitsune: An Ensemble of Autoencoders for Online Network Intrusion Detection.* NDSS 2018.  
arXiv: [https://arxiv.org/abs/1802.09089](https://arxiv.org/abs/1802.09089) · Code: [https://github.com/ymirsky/Kitsune-py](https://github.com/ymirsky/Kitsune-py)

Online ensemble of autoencoders that learns per-feature-bundle normality from packet/flow-derived features without labeled attacks. Demonstrates practical streaming unsupervised AD on commodity hardware.

**Product relevance:** Blueprint for **L2 unsupervised** online scoring on local feature streams without cloud labels. Adapt the *ensemble-of-small-models* idea to multi-layer *health* features (not intrusion PCAP), keeping models tiny and updatable.

### R2 — ENCODE (NetFlow representation learning)

**Cao, X., et al.** *ENCODE: Encoding NetFlow for Network Anomaly Detection.* 2022.  
arXiv: [https://arxiv.org/abs/2207.03890](https://arxiv.org/abs/2207.03890) · Code: [https://github.com/tudelft-cda-lab/ENCODE](https://github.com/tudelft-cda-lab/ENCODE)

Learned encodings of NetFlow that improve downstream anomaly detection versus naive tabular features.

**Product relevance:** If/when optional flow metadata is enabled, use compact encodings rather than raw high-cardinality flow tables; keep default mode **flow-optional** for privacy.

### R3 — Early Detection of Network Service Degradation (intra-flow)

**Bicski, B., & Pekar, A.** *Early Detection of Network Service Degradation: An Intra-Flow Approach.* CNSM 2024.  
arXiv: [https://arxiv.org/abs/2407.06637](https://arxiv.org/abs/2407.06637)

Shows that intra-flow dynamics can surface service degradation earlier than waiting for session failure.

**Product relevance:** Motivates **lookahead features** from short synthetic sessions and optional flowlets—core to the 5–15 minute prediction thesis (degradation precursors before user complaint).

### R4 — Inter-Flow Service Degradation Detection

**Bicski, B., & Pekar, A.** *Inter-Flow Service Degradation Detection.* 2025.  
arXiv: [https://arxiv.org/abs/2509.11140](https://arxiv.org/abs/2509.11140)

Extends degradation detection across related flows, capturing correlated multi-session symptoms.

**Product relevance:** Supports **cross-app correlation** in the agent (e.g., Zoom + browser + game all rising together ⇒ path/Wi‑Fi/DNS shared cause vs single-app bug).

### R5 — TranAD

**Tuli, S., Casale, G., & Jennings, N. R.** *TranAD: Deep Transformer Networks for Anomaly Detection in Multivariate Time Series Data.* PVLDB 2022.  
arXiv: [https://arxiv.org/abs/2201.07284](https://arxiv.org/abs/2201.07284) · Code: [https://github.com/imperial-qore/TranAD](https://github.com/imperial-qore/TranAD)

Transformer-based multivariate TSAD with adversarial training focus; strong baseline for multi-metric streams.

**Product relevance:** Candidate architecture for **L3 multivariate** scoring over fused health channels; prefer distilled/small variants for laptop CPU.

### R6 — Anomaly Transformer

**Xu, J., Wu, H., Wang, J., & Long, M.** *Anomaly Transformer: Time Series Anomaly Detection with Association Discrepancy.* ICLR 2022.  
arXiv: [https://arxiv.org/abs/2110.02642](https://arxiv.org/abs/2110.02642)

Uses association discrepancy between adjacent time points as an anomaly criterion—interpretability-friendly relative to pure reconstruction error.

**Product relevance:** Association patterns can feed **explainability** (“which channels stopped associating”) into plain-language RCA templates.

### R7 — Outage-Watch (extreme-event early prediction)

**Outage-Watch** (ESEC/FSE 2023). *Early outage prediction with extreme-event regularization.*  
arXiv: [https://arxiv.org/abs/2309.17340](https://arxiv.org/abs/2309.17340)

Emphasizes predicting rare/extreme outage-like events earlier via specialized regularization—not only detecting average anomalies.

**Product relevance:** Direct support for **predictive QoS extreme-event forecasting** (the product’s differentiator vs threshold alerts). Training/loss design for rare lag spikes.

### R8 — LEAF (concept drift in cellular networks)

**LEAF** (CoNEXT / PACMNET 2023) — concept drift in cellular performance learning.  
PDF: [https://fbronzino.com/assets/pdf/conext23.pdf](https://fbronzino.com/assets/pdf/conext23.pdf) · Related arXiv PDF: [https://export.arxiv.org/pdf/2109.03011v4.pdf](https://export.arxiv.org/pdf/2109.03011v4.pdf)

Shows that mobile/cellular models degrade under distribution shift and that drift-aware methods matter in live networks.

**Product relevance:** Home/SMB regimes (new AP, ISP peering change, evening congestion) **will drift**; HMPH must include drift detection and online adaptation (see also R15).

### R9 — Donut (unsupervised KPI anomaly detection)

**Xu, H., et al.** *Unsupervised Anomaly Detection via Variational Auto-Encoder for Seasonal KPIs in Web Applications.* WWW 2018.  
PDF: [https://netman.aiops.org/wp-content/uploads/2018/03/www2018.pdf](https://netman.aiops.org/wp-content/uploads/2018/03/www2018.pdf) · Code: [https://github.com/NetManAIOps/donut](https://github.com/NetManAIOps/donut)

VAE-based unsupervised AD for seasonal web KPIs; influential AIOps KPI detector.

**Product relevance:** Pattern for **seasonal baselines** on latency/loss/DNS time-of-day curves without labels.

### R10 — DeepLog

**Du, M., Li, F., Zheng, G., & Srikumar, V.** *DeepLog: Anomaly Detection and Diagnosis from System Logs.* CCS 2017.  
PDF: [https://users.cs.utah.edu/~lifeifei/papers/deeplog.pdf](https://users.cs.utah.edu/~lifeifei/papers/deeplog.pdf)

LSTM-based log key sequence modeling for detection and diagnosis.

**Product relevance:** Optional **OS/event log** channel (Wi‑Fi roam failures, DHCP renewals) as a sparse diagnostic stream feeding RCA—not primary path for MVP.

### R11 — Pingmesh

**Guo, C., et al.** *Pingmesh: A Large-Scale System for Data Center Network Latency Measurement and Analysis.* SIGCOMM 2015.  
PDF: [https://www.microsoft.com/en-us/research/wp-content/uploads/2016/11/pingmesh_sigcomm2015.pdf](https://www.microsoft.com/en-us/research/wp-content/uploads/2016/11/pingmesh_sigcomm2015.pdf)

DC-wide active probing mesh that made continuous latency measurement an operational primitive.

**Product relevance:** Philosophy of **always-on lightweight active probes**—downscoped to home: few synthetic HTTPS/DNS/ICMP targets with careful rate limits, not a full mesh.

### R12 — 007

**Arzani, B., et al.** *007: Democratically Finding the Cause of Packet Drops.* NSDI 2018.  
PDF: [https://www.usenix.org/system/files/conference/nsdi18/nsdi18-arzani.pdf](https://www.usenix.org/system/files/conference/nsdi18/nsdi18-arzani.pdf)

End-host assisted localization of packet drops in DC networks.

**Product relevance:** Reinforces **end-host vantage point** for localization; HMPH uses the user’s machine as the democratic observer of path + local stack.

### R13 — NetBouncer

**Tan, C., et al.** *NetBouncer: Active Device and Link Failure Localization in Data Center Networks.* NSDI 2019.  
PDF: [https://www.usenix.org/system/files/nsdi19-tan.pdf](https://www.usenix.org/system/files/nsdi19-tan.pdf)

Active probing for device/link failure localization with careful probe design.

**Product relevance:** Design cues for **which hops/targets** to probe and how to interpret inconsistent RTT/loss without flooding the network.

### R14 — PreFix

**Zhang, S., et al.** *PreFix: Switch Failure Prediction in Datacenter Networks.* SIGMETRICS 2018.  
PDF: [https://netman.aiops.org/wp-content/uploads/2018/05/zhangsl-SIGMETERICS-2018.pdf](https://netman.aiops.org/wp-content/uploads/2018/05/zhangsl-SIGMETERICS-2018.pdf)

Predictive models for switch failures from telemetry—prediction before hard failure.

**Product relevance:** Academic precedent for **fault prediction** (not only detection); transfer the *predict-then-explain* mindset to user-visible QoS, not switch hardware.

### R15 — Label-Free Concept Drift Assessment

**Label-Free Concept Drift Assessment.** 2025.  
arXiv: [https://arxiv.org/abs/2508.00042](https://arxiv.org/abs/2508.00042)

Methods to assess drift without ground-truth labels—critical when users will not label every lag spike.

**Product relevance:** Operationalize **drift monitors** on feature distributions and model residuals to trigger re-baselining without a labeling farm.

### R16 — Sherlock

**Bahl, P., et al.** *Towards Highly Reliable Enterprise Network Services via Inference of Multi-level Dependencies.* SIGCOMM 2007.  
PDF: [https://www.microsoft.com/en-us/research/uploads/prod/2016/02/sherlock_sigcomm_07.pdf](https://www.microsoft.com/en-us/research/uploads/prod/2016/02/sherlock_sigcomm_07.pdf)

Multi-level dependency inference for enterprise service reliability.

**Product relevance:** Conceptual ancestor of **layered dependency graphs** (app → DNS → gateway → ISP → destination). HMPH needs a *tiny*, user-facing dependency story, not full enterprise inference.

### R17 — NetMedic

**Kandula, S., et al.** *Detailed Diagnosis in Enterprise Networks Using Bayesian Inference.* SIGCOMM 2009.  
PDF: [http://ccr.sigcomm.org/online/files/p243.pdf](http://ccr.sigcomm.org/online/files/p243.pdf)

Bayesian diagnosis across enterprise network components.

**Product relevance:** Template for **probabilistic RCA** (“likely Wi‑Fi 62%, DNS 21%, path 11%”) rendered as plain language for non-experts.

### R18 — Streaming telemetry anomaly detection

**Putina, A., & Rossi, D.** *Online Anomaly Detection Leveraging Stream-Based Clustering and Real-Time Telemetry.* IEEE TNSM 2020.  
PDF: [https://nonsns.github.io/paper/rossi20tnsm-b.pdf](https://nonsns.github.io/paper/rossi20tnsm-b.pdf) · Code: [https://github.com/anrputina/ods-anomalydetection](https://github.com/anrputina/ods-anomalydetection) · Dataset: [https://github.com/cisco-ie/telemetry](https://github.com/cisco-ie/telemetry)

Streaming clustering AD on real-time network telemetry with public code/data hooks.

**Product relevance:** Practical **streaming AD** patterns and a telemetry evaluation substrate; validates that online methods can run continuously.

---

## 4. Surveys and Secondary Sources (Tracked Separately)

These are **not** counted in the core 18 but inform taxonomy, evaluation, and roadmap.

### 4.1 Surveys

| Work | Link | Use |
| --- | --- | --- |
| Boutaba et al., *A comprehensive survey on machine learning for networking*, JISA 2018 | [Springer](https://link.springer.com/article/10.1186/s13174-018-0087-2) | Broad ML-for-networking map |
| Murphy et al., fault prediction survey, TNSM 2024 (TechRxiv) | [https://doi.org/10.36227/techrxiv.18857759](https://doi.org/10.36227/techrxiv.18857759) | Fault *prediction* taxonomy |
| AIOps for cloud (survey) | arXiv: [2304.04661](https://arxiv.org/abs/2304.04661) | Ops framing |
| AIOps + LLM failure management | arXiv: [2406.11213](https://arxiv.org/abs/2406.11213) | Future NL explanation layer |
| TSAD for AIOps | arXiv: [2308.00393](https://arxiv.org/abs/2308.00393) | Metric AD landscape |
| Deep learning for TSAD | arXiv: [2211.05244](https://arxiv.org/abs/2211.05244) | Model family survey |

### 4.2 Additional research (watchlist; not core MVP dependencies)

| Work | Link | Note |
| --- | --- | --- |
| TUBO (Yuan et al.; ICDCS 2025 short / 2026 preprint) | arXiv: [2602.11759](https://arxiv.org/abs/2602.11759) | Watch for forecasting techniques |
| LEAD (Sun et al., 2026) | arXiv: [2601.21437](https://arxiv.org/abs/2601.21437) | Watchlist TSAD/prediction |
| NetCause (Chraim et al., 2026) | arXiv: [2606.13543](https://arxiv.org/abs/2606.13543) | RCA methods to evaluate post-MVP |
| DeepSIP | arXiv: [2003.10643](https://arxiv.org/abs/2003.10643) | Multimodal failure impact ideas |
| GraphIDS (NeurIPS 2025) | [PDF](https://papers.nips.cc/paper_files/paper/2025/file/9ddb13ae9150f99298065d889f951014-Paper-Conference.pdf) | Graph security AD—borrow cautiously |
| UniGAD (NeurIPS 2024) | [PDF](https://proceedings.neurips.cc/paper_files/paper/2024/file/f57de20ab7bb1540bcac55266ebb5401-Paper-Conference.pdf) | Unified graph AD—post-v1 research |

### 4.3 Industry / OSS products (secondary)

Datadog NM, ThousandEyes, Kentik, ExtraHop, Elastic ML, Prometheus/Grafana, Netdata ML, ntopng, Zeek/Suricata, NALA and similar local-first experiments—see §2.2.

**OSS RCA toolkit:** [PyRCA](https://github.com/salesforce/pyrca) (Salesforce)—useful for experimenting with causal/RCA baselines offline before shipping simplified production rules + scores.

---

## 5. Technical Knowledge Synthesis

### 5.1 Telemetry layers (software-only)

| Layer | Example signals (no custom hardware) | Collection notes |
| --- | --- | --- |
| **Wi‑Fi** | RSSI, noise, TX rate, retry %, channel, roam events | OS APIs (nl80211/Windows WLAN/CoreWLAN); permission prompts |
| **OS / host** | Interface errors, retransmits (where exposed), DNS cache stats, CPU/net softirq pressure, VPN state | Cross-platform abstraction required |
| **DNS** | Resolve latency, SERVFAIL/NXDOMAIN rates, DoH vs system resolver | Active + passive (without payloads) |
| **Path** | ICMP/UDP traceroute hop RTT, instability, gateway reachability | Rate-limited; interpret missing hops carefully |
| **Synthetic HTTPS** | TCP connect, TLS handshake, TTFB, throughput micro-probe to allowlisted URLs | Primary *user-visible* proxy metric |
| **Optional flows** | eBPF/NFLOG/Windows ETW metadata: 5-tuple volume, retrans, RTT est. | Opt-in; never store payloads by default |

**Design rule:** Prefer **features and aggregates** over packet capture. Default install must be explainable to a privacy-conscious user in one sentence: *“We measure timings and Wi‑Fi stats on your machine; we don’t upload your traffic.”*

### 5.2 Anomaly detection (detection ≠ prediction)

- **Classical:** EWMA/CUSUM, seasonal STL + residual thresholds, Isolation Forest on windowed vectors—cheap L0/L1 baselines.  
- **Unsupervised deep/streaming:** Kitsune-style ensembles, Donut-like VAE for seasonal KPIs, TranAD/Anomaly Transformer for multivariate fusion (heavier).  
- **Streaming clustering:** Putina & Rossi-style online methods for telemetry.  

HMPH should **not** ship a single giant model. Use a **ladder**: rules → stats → small unsupervised → predictive head.

### 5.3 Prediction and extreme events

Outage-Watch and PreFix argue for training objectives that care about **rare, high-impact** events. Product target: **P(degradation in next 5–15 minutes | recent multi-layer features)** with calibrated risk bands (green/yellow/orange/red), not binary “anomaly yes/no.”

Intra-/inter-flow degradation work (R3–R4) justifies using short-horizon precursors (rising handshake time, DNS variance, Wi‑Fi retries) as predictive features even when mean throughput still looks fine.

### 5.4 RCA and explanation

Sherlock/NetMedic establish multi-level dependency + probabilistic diagnosis. For consumers:

1. Maintain a **small layered causal sketch**: Application QoE ← HTTPS synthetic ← {DNS, Path, Wi‑Fi, OS/VPN}.  
2. Attribute risk deltas to layers (contribution scores).  
3. Map to **templates**: “Your Wi‑Fi retries jumped; video calls may stutter in ~10 minutes. Try moving closer to the AP or switching bands.”  
4. Reserve LLM rephrasing (AIOps+LLM survey) for v2; MVP templates must work offline.

### 5.5 Concept drift

LEAF + label-free drift assessment imply:

- Track feature moments / PSI / residual error rates.  
- Freeze vs adapt policies (adapt unsupervised baselines quickly; adapt predictive head cautiously).  
- Detect “new normal” after ISP maintenance or AP upgrade to avoid alert fatigue.

---

## 6. Proposed Novel Approach: HMPH

### 6.1 Definition

**Hybrid Multi-Layer Predictive Health (HMPH)** is a local inference architecture that:

1. **Fuses** Wi‑Fi, OS, DNS, traceroute, and synthetic HTTPS features on a shared timeline (optional flow metadata).  
2. Runs **hybrid models**: classical stats (L0–L1) + online unsupervised (L2) + lightweight predictive head (L3) + explanation/RCA (L4).  
3. Optimizes for **user-visible QoS extremes** 5–15 minutes ahead.  
4. Keeps **learning and data on-device** by default.  
5. Emits **plain-language RCA** with remediation suggestions.

### 6.2 Novelty relative to preexisting approaches

| Claim | Not merely… | Because… |
| --- | --- | --- |
| Cross-layer consumer fusion | Host CPU graphs *or* traceroute-only tools | Joint model + shared RCA over Wi‑Fi↔DNS↔path↔HTTPS |
| Predictive QoE lookahead | Post-hoc “high latency” alerts | Explicit 5–15 min extreme-event forecasting objective |
| Privacy-local ML product | Cloud DEM / PCAP NDR | No payloads; optional flows; local TSDB |
| Drift-aware home/SMB agent | Static thresholds | Online baselines + label-free drift hooks |
| Non-expert RCA | Expert dashboards | Probabilistic layer attribution → templates |

**Honest boundary:** Individual ingredients exist in literature (Kitsune online AE, Pingmesh probing, NetMedic RCA, Outage-Watch extremes, LEAF drift). Novelty is the **productized composition** for software-only end hosts with predictive consumer UX—not a new universal theorem of networking.

### 6.3 Gaps HMPH fills

1. Academic DC/ISP systems assume infrastructure control users do not have.  
2. Security AD (Kitsune et al.) optimizes intrusion, not Zoom/game QoE.  
3. Enterprise DEM is cloud-centric and expensive.  
4. Open metric stacks lack opinionated multi-layer fusion + lookahead + RCA for laypeople.  
5. Few systems treat **concept drift** as a first-class home-network concern.

---

## 7. Architecture Recommendations (Software-Only)

```
+-------------------------------------------------------------------+
|  UI (Tauri / desktop shell)                                       |
|  Health score · Forecast · Plain-language RCA · History           |
+-------------------------------+-----------------------------------+
                                | local IPC
+-------------------------------v-----------------------------------+
|  Agent core (Rust or Go)                                          |
|  Scheduler · permissions · probe rate limits · config             |
+----+-------------+--------------+--------------+------------------+
     |             |              |              |
     v             v              v              v
 Wi-Fi/OS      DNS/Path     Synthetic HTTPS   Optional flow
 collectors    collectors   probes            metadata
     |             |              |              |
     +-------------+------+-------+--------------+
                          |
                          v
               Feature store (SQLite + local TSDB)
                          |
           +--------------+--------------+
           v              v              v
         L0-L1          L2 online      L3 predictive
         rules/stats    unsupervised   head (small)
           +--------------+--------------+
                          |
                          v
                     L4 RCA / templates
                          |
                     Local alerts / OS notifications
          (optional encrypted sync — post-MVP, user opt-in)
```

**Principles**

- **Collectors are kill-switchable** per layer.  
- **Probe budget:** e.g., ≤ N HTTPS micro-probes/minute; traceroute on slower cadence or on suspicion.  
- **ML sidecar:** Python (PyTorch/ONNX Runtime) for research velocity **or** ONNX in-agent for simpler packaging—see PRD for stack choice.  
- **No custom hardware:** laptop/server OS APIs only.  
- **Offline-first:** full MVP works without accounts.

---

## 8. Datasets, OSS, and Evaluation Strategy

### 8.1 Public datasets (for offline R&D—not perfect QoE labels)

| Dataset | Role | Caution |
| --- | --- | --- |
| [MAWI](https://mawi.wide.ad.jp/) | Traffic traces | Not home Wi‑Fi QoE |
| CIC-IDS2017, UNSW-NB15, CTU-13 | Security AD baselines | Wrong label ontology for lag prediction |
| [Loghub](https://github.com/logpai/loghub) | Log AD experiments | Sparse for network QoE |
| [Cisco IE telemetry](https://github.com/cisco-ie/telemetry) | Streaming telemetry AD (with Putina & Rossi) | Device telemetry ≠ consumer laptop |
| Abilene / GÉANT | Topology / traffic matrices | Research only |
| AssureMOSS (where available) | Security/ops research | Check licenses |

**Implication:** Public datasets validate *components* (TSAD, streaming AD). **Product metrics require a self-collected labeled corpus:** user “this was bad” buttons, synthetic fault injection (traffic shape, DNS delay, Wi‑Fi attenuate via OS airplane toggles in lab), and post-hoc session quality proxies.

### 8.2 OSS to leverage

| Project | Link | Use |
| --- | --- | --- |
| Kitsune-py | https://github.com/ymirsky/Kitsune-py | Online AE reference |
| ENCODE | https://github.com/tudelft-cda-lab/ENCODE | Flow encoding experiments |
| TranAD | https://github.com/imperial-qore/TranAD | Multivariate TSAD baseline |
| Donut | https://github.com/NetManAIOps/donut | Seasonal KPI VAE |
| ODS AD (Putina) | https://github.com/anrputina/ods-anomalydetection | Streaming AD |
| PyRCA | https://github.com/salesforce/pyrca | Offline RCA baselines |

### 8.3 Evaluation strategy (product-grade)

**Offline**

- Component AUROC/F1 on injected faults for each layer.  
- Forecast metrics: lead time distribution, precision@k for “will degrade in 15m,” extreme-event recall (Outage-Watch-inspired).  
- Calibration of risk scores (reliability diagrams).

**Online / dogfood**

- False alert rate per day (target for MVP: tunable; aim < 2 noisy alerts/day default).  
- User agreement rate on RCA layer (“Was it Wi‑Fi?” yes/no).  
- Time-to-useful-insight after install (< 30 minutes of baselines).

**Privacy eval**

- Confirm default DB contains no payloads; document retention TTLs; export/delete UX.

---

## 9. Risks, Open Problems, and Novelty Claims

### 9.1 Risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Weak labels for “lag” | Models optimize wrong proxy | In-app labeling + synthetic faults; start with HTTPS/DNS proxies |
| Probe load / IT policy | Users blame the agent | Strict budgets; pause on battery; enterprise allowlists later |
| OS API fragmentation | Feature gaps (esp. Wi‑Fi on OS versions) | Capability matrix; degrade gracefully |
| Alert fatigue | Uninstall | Drift adaptation + cooldown + “quiet hours” |
| Overclaiming novelty | Credibility | Claim composition + product thesis, cite priors |
| Security AD confusion | Scope creep to NDR | Explicit non-goal: not an IDS |

### 9.2 Open problems

1. Causal identification of Wi‑Fi vs ISP vs remote origin from one vantage point remains partially ambiguous (traceroute helps but is imperfect).  
2. Transfer across homes (apartment RF vs rural) without cloud sharing of raw data.  
3. Calibrated multi-horizon forecasts under abrupt VPN/SSID changes.  
4. Ethical active probing toward third-party endpoints (allowlist, robots/ToS, minimize).

### 9.3 Novelty claims (safe wording for README/pitch)

> We productize **hybrid multi-layer predictive health** on the end host: fusing Wi‑Fi, OS, DNS, path, and synthetic application probes with online, drift-aware models to forecast user-visible degradation minutes ahead and explain the failing layer—without sending your traffic to the cloud.

Avoid claiming “first neural network for networks” or “solves RCA uniquely”; the literature already covers pieces.

---

## 10. Full Bibliography with URLs

### 10.1 Core curated set (R1–R18)

1. Mirsky et al., Kitsune, NDSS 2018 — https://arxiv.org/abs/1802.09089 — https://github.com/ymirsky/Kitsune-py  
2. Cao et al., ENCODE, 2022 — https://arxiv.org/abs/2207.03890 — https://github.com/tudelft-cda-lab/ENCODE  
3. Bicski & Pekar, Intra-Flow Service Degradation, CNSM 2024 — https://arxiv.org/abs/2407.06637  
4. Bicski & Pekar, Inter-Flow Service Degradation, 2025 — https://arxiv.org/abs/2509.11140  
5. Tuli et al., TranAD, PVLDB 2022 — https://arxiv.org/abs/2201.07284 — https://github.com/imperial-qore/TranAD  
6. Xu et al., Anomaly Transformer, ICLR 2022 — https://arxiv.org/abs/2110.02642  
7. Outage-Watch, ESEC/FSE 2023 — https://arxiv.org/abs/2309.17340  
8. LEAF, CoNEXT/PACMNET 2023 — https://fbronzino.com/assets/pdf/conext23.pdf — related https://export.arxiv.org/pdf/2109.03011v4.pdf  
9. Xu et al., Donut, WWW 2018 — https://netman.aiops.org/wp-content/uploads/2018/03/www2018.pdf — https://github.com/NetManAIOps/donut  
10. Du et al., DeepLog, CCS 2017 — https://users.cs.utah.edu/~lifeifei/papers/deeplog.pdf  
11. Guo et al., Pingmesh, SIGCOMM 2015 — https://www.microsoft.com/en-us/research/wp-content/uploads/2016/11/pingmesh_sigcomm2015.pdf  
12. Arzani et al., 007, NSDI 2018 — https://www.usenix.org/system/files/conference/nsdi18/nsdi18-arzani.pdf  
13. Tan et al., NetBouncer, NSDI 2019 — https://www.usenix.org/system/files/nsdi19-tan.pdf  
14. Zhang et al., PreFix, SIGMETRICS 2018 — https://netman.aiops.org/wp-content/uploads/2018/05/zhangsl-SIGMETERICS-2018.pdf  
15. Label-Free Concept Drift Assessment, 2025 — https://arxiv.org/abs/2508.00042  
16. Bahl et al., Sherlock, SIGCOMM 2007 — https://www.microsoft.com/en-us/research/uploads/prod/2016/02/sherlock_sigcomm_07.pdf  
17. Kandula et al., NetMedic, SIGCOMM 2009 — http://ccr.sigcomm.org/online/files/p243.pdf  
18. Putina & Rossi, TNSM 2020 — https://nonsns.github.io/paper/rossi20tnsm-b.pdf — https://github.com/anrputina/ods-anomalydetection — https://github.com/cisco-ie/telemetry  

### 10.2 Surveys

19. Boutaba et al., JISA 2018 — https://link.springer.com/article/10.1186/s13174-018-0087-2  
20. Murphy et al., TechRxiv / TNSM 2024 fault prediction survey — https://doi.org/10.36227/techrxiv.18857759  
21. AIOps cloud survey — https://arxiv.org/abs/2304.04661  
22. AIOps LLM failure management — https://arxiv.org/abs/2406.11213  
23. TSAD AIOps — https://arxiv.org/abs/2308.00393  
24. DL TSAD survey — https://arxiv.org/abs/2211.05244  

### 10.3 Watchlist

25. TUBO — https://arxiv.org/abs/2602.11759  
26. LEAD — https://arxiv.org/abs/2601.21437  
27. NetCause — https://arxiv.org/abs/2606.13543  
28. DeepSIP — https://arxiv.org/abs/2003.10643  
29. GraphIDS NeurIPS 2025 — https://papers.nips.cc/paper_files/paper/2025/file/9ddb13ae9150f99298065d889f951014-Paper-Conference.pdf  
30. UniGAD NeurIPS 2024 — https://proceedings.neurips.cc/paper_files/paper/2024/file/f57de20ab7bb1540bcac55266ebb5401-Paper-Conference.pdf  

### 10.4 OSS / datasets (selected)

31. PyRCA — https://github.com/salesforce/pyrca  
32. MAWI — https://mawi.wide.ad.jp/  
33. Loghub — https://github.com/logpai/loghub  

---

*This document is the research backbone for `PRD.md`. Prefer citing the curated set (R1–R18) in design docs and RFCs; promote watchlist items only after reproduction on local telemetry.*
