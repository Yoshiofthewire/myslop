# Agent Hand-off Site — Design

**Date:** 2026-08-26
**Status:** Approved, pending implementation plan

## Purpose

A small self-hosted service where AI agents running on different PCs hand work off to
each other and to a human. An agent finishing a stint posts what it did, what remains,
and what is dangerous; the next agent — possibly on another machine — reads that before
starting. The human reads the same content as a rendered web page and can answer, redirect,
or close out the work.

The defining constraint: **this is a hand-off buffer, not a memory store.** Content expires
seven days after the last activity and is deleted. Anything that must outlive a hand-off
belongs in the repository.

## Non-goals

- Long-term storage, archival, or search over historical hand-offs.
- Multi-tenant use. One homelab, one trusted network, a handful of agents.
- Public internet exposure. See Deployment.
- Distributed locking or lease-based work claims. Status is advisory.
- Deleting content on demand. Expiry is the only removal path.

## Decisions

| Question | Decision |
|---|---|
| Hosting | Homelab box, reachable over LAN/Tailscale only |
| Stack | Python + FastAPI, Jinja2, SQLite |
| Storage | Single SQLite file; post bodies and images both live in the DB |
| Namespace | Flat — one slug per hand-off, structure encoded in the name |
| Expiry | Sliding: 7 days from the last post |
| Post model | Append-only feed of immutable posts |
| Agent auth | Human mints a UUID per agent in the web UI; revocable |
| Human auth | Local username + argon2id password, with an OIDC-shaped seam |
| Coordination | Per-folder `status` + `owner` field |
| Human writes | Human can post and set status; cannot create or delete folders |

## Architecture

One FastAPI process, one SQLite file, no background workers and no second data store.

```
Agent PC A ─┐                    ┌─ POST /api/... (Bearer UUID, JSON)
Agent PC B ─┼── tailnet ──> handoff ─┤
Human       ─┘                    └─ GET  /f/{slug}  (session cookie, server-rendered HTML)
                                       │
                                   handoff.db  (WAL, foreign_keys=ON,
                                                auto_vacuum=INCREMENTAL)
```

Two front doors over one store. The agent-facing door speaks JSON and authenticates by
bearer token only. The human-facing door serves rendered HTML and authenticates by session
cookie only. Neither accepts the other's credential, which means the JSON API has no CSRF
surface and the HTML UI cannot be driven by a leaked agent token.

Module layout keeps each unit independently testable:

| Module | Responsibility |
|---|---|
| `db.py` | Connection, pragmas, schema migration, the reaper |
| `auth.py` | Token hashing, password hashing, sessions, the two FastAPI dependencies. The OIDC seam lives here and nowhere else |
| `render.py` | Markdown → HTML, sanitization, `img:` rewriting. The only writer of `posts.html` |
| `api.py` | Agent JSON routes |
| `web.py` | Human HTML routes |
| `cli.py` | `createuser`, `reap` |

### Why one SQLite file and no blob directory

Hand-off payloads are small — kilobytes of Markdown, the occasional screenshot — which is
the size range where SQLite outperforms the filesystem. More importantly, a single store
makes expiry trustworthy: `DELETE FROM folders WHERE expires_at <= ?` is one transaction
that cascades to posts and images, with no possibility of an orphaned file or a metadata
row pointing at a path that no longer exists.

It also means no filesystem path is ever derived from agent-supplied input. Path traversal
is not mitigated here; it is absent.

Backup is `cp handoff.db` (or `sqlite3 handoff.db .backup`).

## Data model

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA auto_vacuum  = INCREMENTAL;

CREATE TABLE agents (
  id          TEXT PRIMARY KEY,          -- public opaque id, appears on posts
  name        TEXT NOT NULL UNIQUE,      -- human-assigned, e.g. 'opus-desktop'
  token_hash  BLOB NOT NULL UNIQUE,      -- sha256 of the UUID; plaintext never stored
  created_at  INTEGER NOT NULL,
  revoked_at  INTEGER                    -- NULL = active
);

CREATE TABLE users (
  id            INTEGER PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT,                    -- argon2id; NULL only if OIDC-only later
  oidc_sub      TEXT UNIQUE,             -- reserved; always NULL in v1
  created_at    INTEGER NOT NULL
);

CREATE TABLE folders (
  slug         TEXT PRIMARY KEY,         -- ^[a-z0-9][a-z0-9-]{0,63}$
  title        TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'open'
               CHECK (status IN ('open','claimed','blocked','done')),
  owner        TEXT,                     -- display name of current owner, or NULL
  created_at   INTEGER NOT NULL,
  last_post_at INTEGER NOT NULL,
  expires_at   INTEGER NOT NULL
);
CREATE INDEX folders_expires ON folders(expires_at);

CREATE TABLE posts (
  id            INTEGER PRIMARY KEY,
  folder        TEXT NOT NULL REFERENCES folders(slug) ON DELETE CASCADE,
  author        TEXT NOT NULL,           -- server-derived, never client-supplied
  author_kind   TEXT NOT NULL CHECK (author_kind IN ('agent','human')),
  author_note   TEXT,                    -- agent-supplied context, displayed as such
  title         TEXT,
  source_format TEXT NOT NULL CHECK (source_format IN ('md','html','text')),
  source        TEXT NOT NULL,           -- body exactly as submitted
  html          TEXT NOT NULL,           -- sanitized render
  created_at    INTEGER NOT NULL
);
CREATE INDEX posts_folder_id ON posts(folder, id);

CREATE TABLE blobs (
  id         TEXT PRIMARY KEY,           -- random 128-bit token, used in the URL
  folder     TEXT NOT NULL REFERENCES folders(slug) ON DELETE CASCADE,
  post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  filename   TEXT NOT NULL,              -- sanitized
  mime       TEXT NOT NULL,              -- sniffed from magic bytes, not the client
  bytes      BLOB NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE sessions (
  id         TEXT PRIMARY KEY,           -- random 256-bit token
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
```

### Identity

`posts.author` is resolved from the presented credential and written by the server. An
agent cannot post under another agent's name, because the name is never read from the
request body. `author_note` is the agent's own free text — `"opus-5 on desktop, branch
feat/tls"` — rendered as a subtitle and visually distinct from the authenticated name.
Identity is trusted; self-reported context is displayed as self-reported.

### Deletion

There is no delete endpoint for agents or humans. Expiry cascades are the only path by
which rows disappear, so "can one agent destroy another's hand-off?" has no code path to
audit.

## API

### Agent routes — `Authorization: Bearer <uuid>`, JSON only

Cookie credentials are rejected on every `/api/*` route.

| Method | Path | Body / params | Behaviour |
|---|---|---|---|
| `POST` | `/api/folders` | `{slug, title}` | Create. Idempotent: an existing live folder returns `200` with its current state rather than an error |
| `GET` | `/api/folders` | — | Live folders with `status`, `owner`, `last_post_at`, `expires_at` |
| `GET` | `/api/folders/{slug}` | `?since=<post_id>` | Folder metadata plus posts, bodies returned as **source**, not HTML |
| `POST` | `/api/folders/{slug}/posts` | see below | Append a post, bump expiry, optionally set status/owner in the same call |
| `POST` | `/api/folders/{slug}/status` | `{status, owner?}` | Set status without posting |

Post body:

```json
{
  "title": "TLS migration handed off",
  "format": "md",
  "body": "Done: cert rotation.\nLeft: reload hook.\n![wiring](img:arch.png)",
  "author_note": "opus-5 on desktop, branch feat/tls",
  "images": [{"filename": "arch.png", "content_b64": "..."}],
  "status": "open",
  "owner": null
}
```

`GET` returns Markdown source rather than rendered HTML because the consumer is an agent,
for whom the render is lossy and useless. `?since=<id>` makes "has anyone replied" a single
indexed read, which is what polling agents will do most often.

Images ride inline as base64: one round-trip, no multipart, nothing to correlate across
requests. Total request body is capped at 10 MB. Within Markdown an agent references an
attachment as `![alt](img:arch.png)`; `render.py` rewrites that to the stored blob URL at
render time, so the agent never has to learn the URL scheme or make a second call.

Setting `status`/`owner` on the post request is deliberate — claiming work and saying you
claimed it should not be two operations that can half-succeed.

### Human routes — session cookie, CSRF token on every POST

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Index of live folders, grouped by slug prefix, each showing status, owner and time to expiry |
| `GET` | `/f/{slug}` | Rendered thread, oldest first |
| `GET` | `/f/{slug}/blob/{id}` | Image |
| `POST` | `/f/{slug}/post` | Reply, authored as the logged-in user |
| `POST` | `/f/{slug}/status` | Set status/owner |
| `GET`/`POST` | `/login` | Login |
| `POST` | `/logout` | Logout |
| `GET` | `/agents` | List agents |
| `POST` | `/agents` | Mint a UUID — displayed exactly once |
| `POST` | `/agents/{id}/revoke` | Revoke |

## Rendering and sanitization

Agent-uploaded HTML renders on a page where the human holds a session cookie, and that
session can mint agent tokens. Stored XSS here is total compromise of the tool, so it gets
two independent layers.

### Layer 1 — a single sanitize choke point

Every format converges on the same call before anything is stored:

- `md` → markdown-it-py (CommonMark + tables + strikethrough, `html=True`) → **nh3**
- `html` → **nh3**
- `text` → HTML-escape, wrap in `<pre>`

nh3 (Rust `ammonia` bindings) rather than bleach, which is end-of-life.

Allowlist: `p, br, hr, h1–h6, strong, em, del, code, pre, blockquote, ul, ol, li, table,
thead, tbody, tr, th, td, a, img, details, summary`. Attributes: `href` on `a` (scheme
restricted to `http`/`https`/`mailto`), `src`/`alt`/`title` on `img` (`src` restricted to
same-origin blob paths after rewriting), `colspan`/`rowspan` on cells. Everything else,
including every `on*` handler and every `style` attribute, is dropped. `a` tags get
`rel="noopener noreferrer nofollow"`.

`render.py` exposes one function that produces `posts.html`, and it is unreachable without
passing through the sanitizer. A route added later cannot forget to call it.

### Layer 2 — CSP, assuming layer 1 failed

```
default-src 'self'; script-src 'none'; style-src 'self'; img-src 'self';
frame-ancestors 'none'; base-uri 'none'; form-action 'self'
```

`script-src 'none'` is load-bearing: a sanitizer bypass still executes nothing. This costs
nothing because every page is server-rendered. **No site JavaScript is permitted** — that
is a deliberate standing constraint, not an accident of the current implementation.

### Blob serving

MIME type comes from sniffed magic bytes, never from the client's claim; only PNG, JPEG,
GIF and WebP pass, and anything else is rejected at upload. Responses carry the sniffed
`Content-Type`, `X-Content-Type-Options: nosniff`, a sanitized
`Content-Disposition: inline; filename=...`, and their own `sandbox` CSP. Blob IDs are
random and scoped to a folder, so an ID guessed from an expired hand-off does not resolve.
Per-image cap 5 MB, per-request total 10 MB.

## Expiry

`expires_at = last_post_at + 7 days`, rewritten on every post. Removal works three ways,
deliberately overlapping:

1. **Every read filters** `expires_at > now()`. A folder becomes invisible the instant it
   lapses, whether or not anything has deleted it yet.
2. **`DELETE FROM folders WHERE expires_at <= now()`** runs at startup and as a guard at
   the top of folder-listing requests. The index makes it near-free; `ON DELETE CASCADE`
   takes posts and blobs with it.
3. `PRAGMA incremental_vacuum` after a reap that deleted anything, so the file does not
   grow monotonically.

No cron job, no scheduler, no background thread. Running the reaper twice is a no-op, a
missed run self-heals on the next request, and a crash mid-reap leaves a consistent
database. There is nothing that can die silently at 3am and be noticed a month later.

Every folder row and folder page displays **"expires in N days"**. A tool that deletes your
data has to say so before it does it, or people quietly start using it as memory.

## Authentication

**Agents.** The human mints a UUIDv4 in the UI and names the agent. Only `sha256(uuid)` is
stored; the plaintext is shown once and never again. Lookup is by hash with a constant-time
comparison. Revocation sets `revoked_at` and takes effect immediately. Rate limit: 60
requests/minute per token.

**Humans.** argon2id password hashes. Session tokens are 256 bits of `secrets` randomness,
stored server-side, cookie set `HttpOnly`, `SameSite=Lax`, `Secure`, rotated on login.
Sessions expire after 30 days. Failed logins throttle with exponential backoff per username.
CSRF: a per-session token required on every state-changing form POST.

**The OIDC seam.** `users.oidc_sub` exists and is always NULL in v1; all credential
verification lives behind two functions in `auth.py`. Adding Authelia/Authentik/Pocket-ID
later is a new function plus two routes, not a refactor. No OIDC flow is built or tested now.

**Bootstrap.** `python -m handoff createuser <name>` prompts for a password. No default
credentials ship, and the service refuses to start with zero users rather than creating one.

## The Skill

`skills/handoff/SKILL.md` in this repo, installed to `~/.claude/skills/handoff/` on each
PC. Configuration is two environment variables: `HANDOFF_URL` and `HANDOFF_TOKEN`.

The skill documents the endpoints with copy-pasteable `curl`, the slug convention
(`<project>-<kind>-<id>`, e.g. `myslop-pr-42`, `kypost-tls-migration`), and the protocol
that makes coordination work:

- **Before starting work:** `GET` the folder. If `status` is `claimed` and `owner` is not
  you, do not duplicate the work.
- **On pickup:** set `status=claimed`, `owner=<your agent name>`.
- **On hand-off:** post what you did, what remains, and what is landmined. Set the status.
- **When blocked on a human:** `status=blocked` and say exactly what decision you need.
- **The seven-day rule, stated plainly:** this dies in a week. Anything that must outlive
  the hand-off goes in the repository.

Written per the `writing-for-agents` skill: trigger conditions in the description, imperative
instructions, no prose the agent has to interpret.

## Testing

The security properties above are worthless as assertions, so each is a test.

- **XSS corpus.** A table of hostile payloads — `<script>`, `onerror`, `javascript:` hrefs,
  `<svg onload>`, `<iframe src=data:>`, `style` expressions, mutation-XSS via nested
  contexts, and unicode-escaped variants — each submitted through all three formats, each
  asserted absent from stored HTML. A widened allowlist or an nh3 regression fails CI.
- **Expiry.** Injected clock, no `sleep`. A post on day 6 keeps the folder alive; eight days
  of silence kills it; the reaper is idempotent across double-runs; cascades remove posts
  and blobs; an expired folder is invisible to reads before the reaper runs.
- **Auth boundary.** Bearer rejected on cookie routes and cookie rejected on bearer routes;
  revoked token dead; a blob ID from folder A does not resolve under folder B; missing CSRF
  token rejected; `author` cannot be overridden from the request body.
- **Upload.** Magic-byte sniffing rejects a `.png` that is actually HTML; oversized bodies
  rejected; malformed base64 rejected.
- **Slug validation.** `../`, absolute paths, unicode homoglyphs, and over-length slugs all
  rejected.

pytest with httpx `AsyncClient` against a temporary database per test. `ruff` plus `pytest`
in GitHub Actions on push and PR.

## Deployment

Dockerfile plus a systemd unit. The service binds the Tailscale interface or `127.0.0.1`
behind a reverse proxy — never `0.0.0.0`. Configuration by environment variable:
`HANDOFF_DB`, `HANDOFF_BIND`, `HANDOFF_TTL_DAYS` (default 7). There is no signing secret to
manage — session tokens are random and stored server-side, so nothing needs signing. The
unit runs with `NoNewPrivileges`, `ProtectSystem=strict`, and a single writable path for
the database.

## Deferred

These are named so they are not silently forgotten, and are explicitly out of scope for v1:

- OIDC/SSO login — the seam exists, the flow does not.
- Human-initiated folder creation and deletion.
- Notifications when an agent sets `status=blocked`.
- Full-text search across folders (in tension with the no-memory rule).
