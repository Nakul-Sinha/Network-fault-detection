NetPulse Local predicts network trouble minutes before you feel it and
explains which layer is failing, running entirely on your own machine.

**[See it catch a failure](https://netpulse-local.vercel.app)** before you
install anything.

## Install

```bash
pip install netpulse-local
netpulse doctor     # what can this machine measure?
netpulse run        # the agent, plus a web UI on 127.0.0.1:8787
netpulse demo       # replay a recorded fault against the live agent
```

The binaries below need no Python. The container image is at
`ghcr.io/Nakul-Sinha/Network-fault-detection`.

Verify a download against `SHA256SUMS` before running it. The binaries are
not code-signed yet, which is a known gap tracked in
[ADR 0001](https://github.com/Nakul-Sinha/Network-fault-detection/blob/main/docs/adr/0001-python-reference-implementation.md).

## What it does

Seven collectors across Wi-Fi, the device, VPN, the router, DNS, the path out
and remote HTTPS, feeding a five stage model ladder: hard rules with
suppression, seasonal statistics, online autoencoders, a calibrated forecast,
and attribution that dampens inheritance before it ranks anything.

Against the fault-injection corpus it detects every injected fault with a
6.2 minute median warning, names the right layer every time, and raises no
alerts across eight hours of healthy traffic. CI blocks a merge when any of
those regress. The numbers and their limits are in
[docs/evaluation.md](https://github.com/Nakul-Sinha/Network-fault-detection/blob/main/docs/evaluation.md).

## Privacy

Nothing it measures leaves your machine. There is no account, no telemetry
and no cloud. The guarantee is structural rather than a policy: every column
in the sample table is numeric, so there is nowhere a hostname or a packet
byte could be written, and a test asserts that against the live schema.

[docs/privacy.md](https://github.com/Nakul-Sinha/Network-fault-detection/blob/main/docs/privacy.md)
shows how to check it yourself rather than taking that on trust.
