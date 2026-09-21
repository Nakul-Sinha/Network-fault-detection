# Running it

Day to day operation, tuning, and what to do when the agent is wrong.

---

## Commands

```bash
netpulse run            # the agent, with the web UI
netpulse run --headless # server mode, no browser prompt
netpulse run --no-api   # agent only, no UI

netpulse status         # current health, from a running agent or the store
netpulse check          # one full round, then exit
netpulse doctor         # what this machine can measure
netpulse incidents      # what it has concluded recently

netpulse pause          # stop probing
netpulse resume
netpulse learning off   # keep scoring, stop updating baselines

netpulse label bad "call dropped"
netpulse export --hours 24
netpulse wipe

netpulse eval           # replay the corpus and report the metrics
netpulse replay samples.jsonl
netpulse train
```

`--json` works on any of them, before or after the subcommand.

---

## The numbers on screen

**Health, 0 to 100.** How the network looks right now, with the forecast
pulling it down early rather than defining it. Smoothed, so the number you
are watching does not flicker.

**Risk in 5 and 15 minutes.** The probability that user-visible degradation
begins within that window. These are calibrated: when the agent says 30
percent, it should be right about 30 percent of the time. `docs/evaluation.md`
has the reliability figures.

**Coverage.** How much of the feature set is currently measurable. 100
percent means every collector is contributing. A wired desktop with no Wi-Fi
radio will sit around 80 percent, which is normal.

**Severity.** `info` is healthy, `watch` is something moving, `risk` is where
notifications start, and `critical` means it is already bad.

---

## Warm-up

About 30 minutes before forecasts are meaningful. Before that the agent still
shows a score and says plainly that it is learning.

Seasonal baselines keep improving over the first few days, as each hour of
the day is seen for the first time. Until an hour is known, the agent widens
its uncertainty rather than guessing, which is why it is quiet on the first
evening rather than noisy.

Baselines are saved every 15 minutes and restored on start, so a restart does
not re-enter warm-up.

---

## Tuning

### Too noisy

```toml
[notifications]
min_severity = "critical"   # only interrupt when it is already bad
cooldown_minutes = 60
quiet_hours_start = 22
quiet_hours_end = 8
```

Or raise the thresholds themselves:

```toml
[ml]
risk_bands = { watch = 0.35, risk = 0.65, critical = 0.85 }
```

If it is alerting on something genuinely fine, the useful thing is a label
plus an export. That pair is what a future model gets trained on.

### Too quiet

Lower `min_severity` to `watch`, or lower the bands. Turning on `focus_mode`
before an important call is the intended lever, rather than permanently
lowering the thresholds.

### Too much traffic

```toml
[collectors]
https = false      # stop contacting third-party endpoints
path = false       # stop tracerouting

[probes]
gateway_interval_s = 60
dns_interval_s = 120
https_interval_s = 300
path_interval_s = 1800
```

An interval that would exceed the hard budget is rejected at startup with an
explanation, rather than silently clamped.

### On a laptop

Battery backoff is on by default: below 30 percent charge on battery, active
probes stretch by 2x. Passive collectors are unaffected, since reading a
counter costs nothing.

```toml
[power]
battery_backoff = true
battery_interval_multiplier = 3.0
battery_threshold_pct = 50
```

---

## When the agent is wrong

Tell it, then export.

```bash
netpulse label bad "Zoom froze but it said everything was fine"
netpulse export --hours 2 -o wrong-call.zip
```

The label is stored against whatever incident was open, or against the last
15 minutes if none was. The export replays:

```bash
unzip -p wrong-call.zip samples.jsonl > samples.jsonl
netpulse replay samples.jsonl
```

That runs your actual capture back through the same scoring path the agent
uses, so you can see exactly what it concluded and when. It is also the right
attachment for a bug report, because someone else can replay it.

---

## Reading the probe log

Every packet the agent originates is recorded. The web UI has it under Probe
log, and it is in the database:

```sql
SELECT datetime(ts,'unixepoch','localtime') AS at, collector, target, ok, duration_ms
FROM probe_log ORDER BY ts DESC LIMIT 40;
```

This is how you check the claims in `docs/privacy.md` rather than taking them
on trust.

---

## Disk

Raw samples for 48 hours, then five-minute roll-ups for 90 days. At default
cadences that is roughly 10 to 20 MB per month.

```toml
[retention]
raw_days = 7
downsample_after_hours = 48
rollup_days = 90
```

Retention runs every five minutes. `netpulse wipe` clears everything and
vacuums, so the space is actually returned.

---

## Running headless

```bash
netpulse run --headless
```

Notifications fall back to the log, which is the only sensible backend on a
box nobody is sitting in front of. Everything else is the same: the web UI is
still on loopback, reachable over an SSH tunnel:

```bash
ssh -L 8787:127.0.0.1:8787 user@server
```

Never move the API off loopback. It is not designed for it, and the config
will refuse.

---

## Monitoring the monitor

```bash
netpulse status --json | jq '{health, severity, risk_15m, coverage, paused}'
```

Exit code is 0 whenever the agent answers, so use the payload rather than the
status code for alerting. `netpulse eval --gate` exits non-zero when a PRD
target regresses, which is what CI keys on.

Logs are in the state directory, rotating at 2 MB with three kept.
`journalctl --user -u netpulse -f` on Linux, `docker compose logs -f` for the
container.

---

## Upgrading

```bash
pip install --upgrade netpulse-local
```

The database migrates itself on start. A database written by a newer version
is refused rather than silently downgraded, so a rollback tells you what
happened instead of corrupting data.

Model files carry their feature layout and are rejected if it does not match
the running code, which is why an upgrade cannot silently apply weights
trained against a different feature set.

---

## Things that will look like bugs and are not

**Health drops on a network that feels fine.** Usually a genuine change the
agent noticed first: a slower resolver, a rerouted path. Check the incident
card, and label it if you disagree.

**A layer says unavailable.** `netpulse doctor` says why. Usually a missing
binary on Linux or a denied permission on macOS.

**Wi-Fi shows nothing on a laptop with Wi-Fi.** You are on ethernet. The
agent deliberately ignores an associated radio when the active route is
wired, because otherwise it would blame Wi-Fi for a cable problem.

**Path measurements stop updating.** Traceroute runs every 10 minutes and has
its own hourly budget. It is pulled forward when severity rises.

**The first evening is quieter than expected.** By design. The agent widens
its uncertainty for an hour it has not seen.
