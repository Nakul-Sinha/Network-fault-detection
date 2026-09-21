# ADR 0002: Loopback API, with the token injected into the page

**Status:** accepted
**Date:** 2026-09-21
**Relates to:** PRD N7, PRD 12.2

## Context

The agent serves a control API and a web UI on localhost. PRD N7 requires the
API bound to loopback with a random token in a user-only file, and no remote
shell.

That leaves two questions the PRD does not settle, and both have wrong
answers that look fine.

**How does the page get the token?** A query string is the obvious answer and
the worst one: the token lands in browser history, in bookmarks, and in the
referrer header of any link the user follows. Prompting for a paste is safe
but hostile for something someone opens daily.

**Is loopback binding enough?** No. A page on the open internet can point its
own hostname at 127.0.0.1 and then talk to a local service from the user's
browser. DNS rebinding defeats binding alone, and a local agent with a
delete-everything endpoint is worth attacking.

## Decision

Six controls, each closing a specific hole.

1. **The bind address is validated in config.** `api.host` must parse as a
   loopback literal. The agent cannot be moved onto a routable interface by
   editing a setting, only by editing the code.

2. **Every API route requires the token**, compared in constant time, read
   from a file written with owner-only permissions.

3. **The token is injected into the HTML the agent itself serves.** The page
   receives it as `window.NETPULSE_TOKEN` at render time. It never appears in
   a URL, a bookmark, a referrer or browser history. A page from another
   origin cannot read the response that carries it, because the browser will
   not hand a cross-origin document body to script.

4. **The Host header is checked against loopback on every request.** This is
   the rebinding defence. A request that did not address the agent as
   loopback is refused with 421, whatever the socket says.

5. **A content security policy** confines the page to its own origin for
   scripts, styles, images and connections, forbids framing, and pins the
   base URI.

6. **No route takes a filesystem path.** The only write is an export, and its
   destination is always constructed inside the agent's own data directory.

## Consequences

The UI is only usable from the machine running the agent. Remote access means
an SSH tunnel, which is the correct amount of friction for a tool with a
delete-everything button.

`unsafe-inline` remains in the policy, for the token injection and a small
amount of inline styling. A nonce would be tighter and is worth doing when
the UI grows enough to justify the plumbing.

The token does not rotate. Deleting the token file and restarting generates a
new one. Rotation matters more once there is a second client.

Each control is covered by a test: the Host check, the token requirement, the
absence of the placeholder in the served page, the presence of the policy
header, and the export path staying inside the data directory.
