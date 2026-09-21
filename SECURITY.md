# Security

## Reporting a vulnerability

Please report privately through
[GitHub Security Advisories](https://github.com/Nakul-Sinha/Network-fault-detection/security/advisories/new)
rather than opening a public issue.

Include what you did, what happened, and what you expected. A diagnostic
bundle helps if the issue involves the agent's behaviour, but read it first:
it describes your own network.

This is a student project, not a funded one. There is no bounty and no
guaranteed response time, but reports will be taken seriously and credited
unless you prefer otherwise.

## What is in scope

The things this project claims, and would be genuinely wrong about:

- **Anything that gets data off the machine.** The agent is supposed to send
  nothing but its configured probes. A path that leaks measurements, network
  names or the install salt is the most serious class of bug here.
- **The control API.** It binds loopback, requires a token from an
  owner-only file, and checks the `Host` header against loopback on every
  request. A way to reach it from another origin, another host, or without
  the token is in scope.
- **Probe target validation.** Probe URLs are checked against a scheme, port
  and address allowlist so the config cannot be turned into a scanning or
  SSRF primitive. A bypass is in scope.
- **The export.** Bundles are scrubbed of addresses, MAC addresses,
  credential-shaped strings and network names. Anything sensitive that
  survives into an archive is in scope.
- **The storage guarantee.** Every column in the sample table is numeric, so
  there is nowhere to write free text. A path that stores a hostname, a URL
  or a payload byte is in scope.
- **Privilege.** Nothing here should need administrator rights or escalate.

## What is out of scope

- The agent reading your local network state. That is what it is for.
- Traffic to the configured probe targets. It is documented in
  `docs/privacy.md`, rate-limited in code, logged in the probe ledger, and
  switchable off per layer.
- Another administrator on the same machine reading the database. It is
  protected by file permissions, not encryption, and `docs/privacy.md` says
  so.
- Denial of service against the loopback API by something already running as
  your user on your machine.
- Missing code signing on release binaries. It is a known gap, tracked in
  ADR 0001, and releases publish SHA256 checksums in the meantime.

## What the project already does

Worth knowing before you look, so you can skip what is already covered:

- Loopback-only bind, validated in config rather than trusted.
- Token required on every API route, compared in constant time, from a file
  written with owner-only permissions.
- The token reaches the UI by injection into the served page, so it is never
  in a URL, a bookmark, a referrer or browser history.
- `Host` header checked on every request, which is the DNS rebinding
  defence that loopback binding alone does not provide.
- A content security policy confining the page to its own origin.
- No route accepts a filesystem path; the only write is an export inside the
  agent's own directory.
- Subprocess calls use argv lists, never a shell string.
- Probe rates are enforced by token buckets in code, independent of the
  configured cadence.
- Dependencies are audited in CI on every pull request.

Each of these has a test. If you find one that does not hold, that is exactly
the report worth sending.
