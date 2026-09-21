# Installing NetPulse Local

Nothing here needs administrator rights. If something asks for elevation,
that is a bug.

---

## Quickest look

```bash
pip install netpulse-local
netpulse doctor     # what can this machine measure?
netpulse check      # one full round, then exit
netpulse run        # the agent, with the web UI on http://127.0.0.1:8787/
```

`doctor` is worth running first. It prints your default route, whether a VPN
is active, and which of the seven collectors can run here. A layer that is
unavailable stays unavailable, and the agent tells you why rather than
quietly reporting it as healthy.

---

## Requirements

- Python 3.11 or newer, or one of the standalone binaries below.
- No administrator rights.
- On Linux, `iputils-ping` and `traceroute` if you want those two layers.
  Without them the agent runs with five layers instead of seven.

| Platform | Gateway | Wi-Fi | Path |
| --- | --- | --- | --- |
| Windows 10/11 | `ping` | `netsh wlan` | `tracert` |
| macOS 13+ | `ping` | `airport`, then `system_profiler` | `traceroute` |
| Linux | `ping` | `iw`, then `/proc/net/wireless` | `traceroute` or `tracepath` |

macOS may ask for Local Network permission the first time the agent probes,
and for Location permission before it will reveal the Wi-Fi network name.
Both are optional. Denying them hides the Wi-Fi layer, and the agent says so.

---

## Standalone binary

No Python needed. Download from the
[releases page](https://github.com/Nakul-Sinha/Network-fault-detection/releases),
check it against `SHA256SUMS`, and run it.

```bash
# Linux and macOS
chmod +x netpulse-linux-x86_64
./netpulse-linux-x86_64 doctor
```

```powershell
# Windows
.\netpulse-windows-x86_64.exe doctor
```

macOS will refuse an unsigned binary on first run. Right-click, Open, and
confirm, or clear the quarantine flag:

```bash
xattr -d com.apple.quarantine ./netpulse-macos-arm64
```

Code signing and notarisation are a v1 item in PRD 14.3 and are not done yet.
Until they are, verify the checksum.

---

## Running it all the time

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\install-task.ps1
```

Registers a logon scheduled task that runs as you. Not a Windows service, and
that is deliberate: a service runs as SYSTEM in session 0, where there is no
Wi-Fi association to read, no session to send a toast to, and a different
view of the network from the one your applications have.

Remove it with `-Uninstall`, or `-Uninstall -Purge` to delete stored data too.

### Linux

```bash
./packaging/systemd/install.sh
loginctl enable-linger "$USER"   # keep it running while logged out
```

A user service, not a system one: the agent measures the network as your
applications see it, and needs no privilege. The unit is sandboxed to its own
directories and niced out of the way.

```bash
systemctl --user status netpulse
journalctl --user -u netpulse -f
./packaging/systemd/uninstall.sh
```

### macOS

```bash
cp packaging/macos/sh.raincloud.netpulse.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/sh.raincloud.netpulse.plist
```

### Container, for a server

```bash
cd packaging/docker
docker compose up -d
```

Two things to know. The compose file uses host networking on purpose: on the
default bridge the agent measures Docker's gateway rather than your router,
so every incident card would talk about the wrong thing. And the API binds
loopback inside the container, so if you publish the port, publish it to
`127.0.0.1:8787` and never `0.0.0.0`.

```bash
docker compose exec netpulse netpulse status
docker compose logs -f
```

---

## Configuration

A commented config file is written on first run:

| Platform | Location |
| --- | --- |
| Windows | `%APPDATA%\NetPulse\config.toml` |
| macOS | `~/Library/Application Support/NetPulse/config.toml` |
| Linux | `~/.config/netpulse/config.toml` |

```bash
netpulse config          # what is in effect right now
netpulse config --init   # write the starter file
```

Every setting also has an environment variable, which is what the container
uses: `NETPULSE_API__PORT=9000`, `NETPULSE_RETENTION__RAW_DAYS=14`, and so
on. `NETPULSE_HOME` puts config, data and state under one directory, which is
useful for a portable install.

A few changes people make first:

```toml
[collectors]
https = false          # stop contacting third-party endpoints
path = false           # stop tracerouting

[notifications]
quiet_hours_start = 23
quiet_hours_end = 8
min_severity = "critical"   # only interrupt me when it is already bad

[retention]
raw_days = 14
```

An invalid setting fails at startup with a sentence explaining what is wrong.
A probe target that points at loopback, a link-local address or an unusual
port is rejected, and so is an API host that is not loopback.

---

## First run

The agent needs about 30 minutes to learn what your network normally looks
like. Before that it still shows a health score and says plainly that it is
still learning. Forecasts sharpen as the baselines settle, and the seasonal
part keeps improving over the first few days as it sees each hour of the day.

---

## Uninstalling

```bash
netpulse wipe --yes      # delete every stored observation
pip uninstall netpulse-local
```

Then remove the three directories `netpulse doctor` prints. The platform
install scripts take `--purge` and `-Purge` to do all of this for you.

---

## When something does not work

**"No readings yet"** means no agent is running. Start one with
`netpulse run`.

**A layer shows as unavailable.** `netpulse doctor` says why. Usually a
missing binary on Linux, or a denied permission on macOS. The rest keep
working.

**The web UI will not load.** Check the agent is running and that the port is
free: `netpulse status --json`. The UI is at `http://127.0.0.1:8787/` and is
only reachable from this machine by design.

**It says my network is bad and I disagree.** Tell it:
`netpulse label ok "seemed fine to me"`. Labels are stored against the
incident and are what a future model gets trained on.

**It is too noisy.** Raise `min_severity` to `critical`, lengthen
`cooldown_minutes`, or widen quiet hours. If it is alerting on something
genuinely benign, an exported bundle plus the label is the useful bug report.

**Something is wrong and you want to send it somewhere.**

```bash
netpulse export --hours 24
```

The zip explains itself, contains no packet contents, and is scrubbed of
addresses and identifiers. Read `README.txt` inside before sharing it.
