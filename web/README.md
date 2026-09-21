# Project site

The public page for NetPulse Local: what it does, an interactive replay of
the agent catching real failures, and how to install it.

## It is a replay, not a mock

Every number the demo shows was produced by `netpulse.ml.scorer.Scorer`
observing the fault-injection corpus, which is the same object the installed
agent runs. Regenerate after any change to the model ladder:

```bash
python scripts/build_demo_data.py
```

That writes one JSON file per scenario into `public/data/`, plus an index.

A hand-written demo would drift from the product within a week and would
quietly be making claims the code does not support.

## Why the agent itself is not deployed here

It measures the network from the machine it runs on. Deployed to a serverless
platform it would measure a datacenter, not your Wi-Fi, and it also needs
long-running background threads, SQLite persistence and the platform `ping`
and `traceroute` binaries, none of which exist there. The point of the
product is that nothing leaves your machine, so the agent belongs on your
machine.

## Local preview

```bash
cd web/public && python -m http.server 4321
```

## Deploy

```bash
cd web && vercel --prod
```

Static output, no build step and no framework. `vercel.json` sets the
security headers and caches the recordings.
