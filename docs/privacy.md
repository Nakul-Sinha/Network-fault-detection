# Privacy

What NetPulse Local stores, what it does not, and how to check for yourself
rather than taking this document's word for it.

---

## The short version

Everything the agent measures stays on your machine. There is no account, no
telemetry and no cloud service. The only traffic it originates is the probes
listed below, and you can see every one of them in the probe log.

It never captures packet contents. Not by default, not behind a setting.

---

## What is stored

One SQLite file, at:

| Platform | Location |
| --- | --- |
| Windows | `%LOCALAPPDATA%\NetPulse\netpulse.sqlite` |
| macOS | `~/Library/Application Support/NetPulse/data/netpulse.sqlite` |
| Linux | `~/.local/share/netpulse/netpulse.sqlite` |

It holds:

- **Samples**: timings, counters and rates. Round trip times, packet loss
  fractions, signal strength, retry rates, CPU and memory percentages.
- **Scores**: the health score and risk forecast over time.
- **Incidents**: what the agent concluded, the evidence, and what it
  suggested.
- **Labels**: your feedback about whether it was right.
- **Probe log**: every probe sent, its configured destination, and whether it
  answered.
- **Drift events**: moments when the agent decided your network had changed.

`netpulse doctor` prints the exact paths on your machine.

---

## What is not stored, and why it cannot be

The privacy guarantee here is **structural**, not a promise to be careful.

Every column in the sample table is a number. Open the database and look:

```bash
sqlite3 ~/.local/share/netpulse/netpulse.sqlite ".schema samples"
```

Every column is `REAL`. There is no column a hostname, a URL, a page title or
a packet byte could be written into. The agent could not store your browsing
history if it tried, because there is nowhere to put it. The test suite
asserts this against the live schema on every run, so it cannot drift.

Specifically absent:

- packet contents of any kind
- the sites you visit
- page or file contents
- the DNS names *your applications* look up (the agent measures only its own
  configured probe names)
- your network's name in plain text

---

## Network identity

The agent needs to notice when you move to a different network or your device
roams between access points, because both change what "normal" means.

It does this without storing the name. SSIDs and BSSIDs are hashed with an
HMAC using a 32 byte salt generated once on your machine and kept in the
state directory. The same network hashes the same way on this host and
differently on any other, so the stored value is useful for comparison and
useless to anyone who gets hold of it. The salt is never included in an
export.

This settles PRD open question 5 in favour of hashing over plain text.

---

## Probes the agent sends

It is an active tool: it measures by sending traffic. Here is all of it, at
its default settings.

| To | What | How often |
| --- | --- | --- |
| Your own router | four ICMP echoes | every 20 s |
| Your configured DNS resolver | three name lookups | every 45 s |
| `www.cloudflare.com`, `www.google.com` | one HTTPS request each, head of the response only | every 90 s |
| `detectportal.firefox.com` | one HTTP request | every 5 min |
| `1.1.1.1` | one traceroute, 12 hops | every 10 min |

That is roughly 4 KB a minute. Every target is configurable, and every layer
has an off switch:

```toml
[collectors]
https = false     # stop contacting third-party endpoints entirely
path = false      # stop tracerouting
```

`netpulse pause` stops all of it immediately, and the pause survives a
restart. The probe log in the UI shows exactly what was sent and where, so
you never have to take the table above on trust.

Rate limits are enforced in code, not merely implied by the cadence, so a
misconfigured interval cannot turn the agent into a traffic generator.

---

## What leaves your machine

Nothing, unless you export it.

- No account, no sign-in, no licence check.
- No telemetry, no crash reports, no usage analytics. The config keys exist
  and default to off; nothing is implemented behind them.
- No automatic updates. You update the agent yourself.

The probes above reach the hosts listed, which is inherent to measuring a
network. Turn off the `https`, `captive` and `path` collectors and the agent
speaks only to your own router and resolver.

---

## Exports

`netpulse export` writes a zip you can send to your provider or a support
thread. It contains what is already in the database, nothing newly collected,
scrubbed on the way out: addresses, MAC addresses, credential-shaped strings
and network names are removed from every free-text field and from the logs.

The archive leads with a README explaining what is inside. The export command
audits its own output and tells you whether it came out clean.

The timings themselves still say something about your network, though: how
many hops from your provider you are, roughly when you were using it, and how
it performed. The README says so, and it is worth a look through
`samples.jsonl` before you forward it anywhere.

---

## Deleting everything

```bash
netpulse wipe          # asks first
netpulse wipe --yes    # does not
```

or the button in the privacy centre of the web UI. It removes every sample,
score, incident, label, probe record and drift event, deletes the saved model
state, and vacuums the database so the space is actually returned.

To remove the agent completely, uninstall it and delete the three
directories `netpulse doctor` prints.

---

## Checking for yourself

You do not have to believe any of this.

```bash
# Every column is a number
sqlite3 <db> ".schema samples"

# Look at the raw rows
sqlite3 <db> "SELECT * FROM samples ORDER BY ts DESC LIMIT 5;"

# Every table in the file
sqlite3 <db> ".tables"

# Everything the agent has sent
netpulse status --json
sqlite3 <db> "SELECT ts, collector, target, ok FROM probe_log ORDER BY ts DESC LIMIT 40;"

# Watch it on the wire
sudo tcpdump -i any -n 'icmp or port 53 or port 443'
```

The source is Apache-2.0 and the probe code is about four hundred lines
across `netpulse/collectors/`. The parts worth reading first are
`features/schema.py`, which is the complete list of everything the agent can
record, and `privacy.py`, which is the hashing and redaction.

---

## The control API

The agent serves a web UI on `127.0.0.1:8787`.

- The bind address is validated to be loopback, so it cannot be moved onto a
  routable interface by editing a setting.
- Every API route needs a token from a file only your account can read.
- The `Host` header is checked on every request. A loopback service that
  trusts `Host` can be reached by a page on the open internet that resolves
  its own hostname to 127.0.0.1; this one refuses anything not addressed as
  loopback.
- The page gets its token by injection into the HTML the agent serves, so it
  never appears in a URL, a bookmark, a referrer or browser history, and a
  page from another origin cannot read it.
- There is no remote shell and no route that takes a filesystem path.

---

## Honest limits

- **One vantage point.** The agent sees your network from your machine. It
  can say the trouble appeared past your router; it cannot prove whose fault
  it is. The wording in incident cards reflects that, and PRD 12.3 is
  explicit that an export is observations rather than evidence.
- **Exports contain network metadata.** See above.
- **A shared machine is a shared database.** Another administrator on the
  same computer can read the file. It is protected by file permissions, not
  by encryption.
- **The salt is per install, not per network.** Someone with your database
  and your salt could confirm a guess about which networks you have used.
  Neither leaves your machine on its own.
