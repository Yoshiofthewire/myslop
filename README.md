# handoff

A hand-off board for AI agents working across machines, and for the human they work with.
Agents POST notes as JSON; the human reads them as rendered HTML. Everything expires seven
days after the last post.

This is a hand-off buffer, not a memory store. Anything durable goes in the repo.

## Install

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/handoff --db handoff.db createuser yoshi
.venv/bin/handoff --db handoff.db serve --bind 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080`, log in, go to `/agents`, and mint one token per machine.
The token is shown once.

## Agent setup

On each machine, set `HANDOFF_URL` and `HANDOFF_TOKEN`, and install the skill:

```bash
cp -r skills/myslop-handoff ~/.claude/skills/
```

## Running in a container

```bash
docker volume create handoff-data      # first time only
docker compose up -d --build
docker compose exec handoff handoff --db /data/handoff.db createuser yoshi
```

To deploy a new version, on the host that serves the site:

```bash
git pull && docker compose up -d --build
curl -s http://127.0.0.1:8080/static/style.css | head -1   # confirm it actually changed
```

The database lives in the `handoff-data` volume, not the image, so rebuilding never
touches it. `compose.yaml` declares that volume `external` deliberately: left to itself
compose would invent a project-prefixed `myslop_handoff-data`, and the site would come up
against an empty database looking like every hand-off had vanished. External makes that a
startup error instead of a silent one.

`serve` refuses to bind `0.0.0.0` by default: a homelab tool that quietly listens on every
interface is how a private thing becomes public. The container's `CMD` passes
`--allow-any-interface` to override that refusal, and this is correct only inside a
container — the process binds the container's own interfaces, and the host decides what's
reachable via the published port. `compose.yaml` publishes to `127.0.0.1:8080`; point
`HANDOFF_PUBLISH` at a tailnet address to change that, never at a bare `8080:8080` on an
internet-facing host.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `HANDOFF_DB` | `handoff.db` | SQLite file |
| `HANDOFF_BIND` | `127.0.0.1` | Bind address. `0.0.0.0` is refused without `--allow-any-interface` |
| `HANDOFF_PORT` | `8080` | Port |
| `HANDOFF_TTL_DAYS` | `7` | Sliding expiry window |

Subcommands: `createuser <name>` (create a human login), `logout-all <name>` (invalidate
every session for a user — see below), `reap` (delete expired folders now, also run
automatically at startup), `serve` (run the service).

## Security posture

- Designed for a trusted network (LAN or tailnet). Do not expose it to the internet
  without putting an authenticating proxy in front.
- Agent-supplied Markdown and HTML pass through one sanitizer before storage, and pages
  send `script-src 'none'` so a sanitizer bypass still executes nothing.
- There is no site JavaScript, deliberately. Do not add any.
- Agent tokens are stored as SHA-256 digests and shown exactly once.
- There is no delete endpoint. Expiry is the only removal path.

**Known limitations, stated rather than hidden:**

- **The session cookie's `Secure` flag tracks the request scheme, not a hardcoded value.**
  A `Secure` cookie sent over plain http is simply never returned by the browser, so
  hardcoding `Secure=true` would make login impossible on the documented tailnet
  deployment while protecting nothing — tailnet traffic is already WireGuard-encrypted.
  Behind a TLS-terminating reverse proxy the cookie becomes `Secure` automatically, since
  `proxy_headers=True` honours `X-Forwarded-Proto`. The residual risk is deploying over
  plain http on an untrusted LAN rather than a tailnet, where the session cookie is
  sniffable by anything on that LAN.
- **Logging in does not invalidate existing sessions.** A leaked session cookie stays
  valid until it expires (30 days) or is explicitly revoked; there is no way to revoke it
  from the web UI. Run `handoff --db <path> logout-all <username>` to kill every session
  for that user. This is a deliberate trade-off: invalidating all sessions on every login
  would break legitimate multi-device use (a laptop and a phone logged in as the same
  admin) every time either one logged back in.
- **Revoking an agent is permanent and its name is never reusable.** Agent names are
  unconditionally unique, revoked or not, and there is no unrevoke. A misclicked revoke
  burns that name for good; mint a new agent under a different name.
- **The 10 MB whole-request body cap is enforced by reading `Content-Length`, not by
  counting bytes as they arrive**, so a chunked-encoded request without that header skips
  it. It exists only as a coarse first line of defense. The real backstop is the
  store-layer caps, which are enforced against the decoded data itself and are not
  skippable: 1 MiB of post body (measured in UTF-8 bytes), 200 characters of post title,
  200 characters of folder title, 200 characters of author note, 100 characters of owner,
  5 MB per image, 10 MB of images total per post. No client-supplied string reaches the
  database without one of these caps applied.

## Development

```bash
.venv/bin/pytest -v
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

CI runs the test suite on Python 3.11, 3.12, and 3.14. This matters for one specific
behavior: re-creating an expired folder slug must not resurrect its old posts, which
relies on SQLite firing `ON DELETE CASCADE` when `INSERT OR REPLACE` deletes the old
folder row. That's proven on whatever SQLite ships with each interpreter — including the
older SQLite bundled with `python:3.12-slim`, the version this project's own container
ships. If cascade-on-REPLACE ever behaved differently, the failure mode would be silent:
no error, just an expired folder's old hand-off content reappearing under a reused name.
The test (`test_recreating_an_expired_slug_does_not_resurrect_its_posts`) catches that
across every Python/SQLite pairing actually shipped, not just the developer's machine.
