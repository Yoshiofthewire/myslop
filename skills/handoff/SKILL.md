---
name: handoff
description: Use when handing off work to another agent or machine, when picking up work someone else started, when you need a decision from a human before continuing, or when your session is ending and someone else will continue this task. Posts and reads hand-off notes on the shared handoff server via its JSON API.
---

# Hand-off

A shared board for passing work between agents on different machines, and to the human.

**Everything posted here is deleted 7 days after the folder's last post. This is not
memory and not a scratchpad.** Anything that must survive — decisions, code, design
docs — belongs in the repository, not here. Write here only what the next reader needs
to pick up the work.

## Setup

Two environment variables, set on this machine:

- `HANDOFF_URL` — e.g. `http://handoff.tailnet:8080`
- `HANDOFF_TOKEN` — the UUID the human minted for this machine, at `$HANDOFF_URL/agents`

If `HANDOFF_TOKEN` is unset, tell the human to mint one. Do not invent a token and do not
proceed without one.

There is no `/api/docs` — Swagger UI is disabled on this server. The raw schema is at
`$HANDOFF_URL/openapi.json` if you need it; this document is otherwise the only reference.

## Folders

One folder per hand-off. The slug is lowercase letters, digits, and hyphens only, must
start with a letter or digit, 1–64 characters:

- `myslop-pr-42` — work on a pull request
- `kypost-tls-migration` — a named piece of project work
- `llamamail-build-break` — an incident

Name it after the thing being handed off, prefixed with the project. Reuse an existing
folder for a continuing hand-off; do not create `-v2`.

Your own agent name (shown as the post author) was fixed when the human minted your
token, and follows the same shape: lowercase letters, digits, hyphens, 1–64 characters.
You cannot change it and cannot set a different author in a post — the server reads it
from your token.

## Protocol

Follow this order.

**1. Check status before touching anything.**

```bash
curl -s -H "Authorization: Bearer $HANDOFF_TOKEN" "$HANDOFF_URL/api/folders/myslop-pr-42"
```

Read `status` and `owner` in the response, then read the posts. If `status` is `claimed`
and `owner` is not you, someone else has this — do not duplicate the work. If the folder
doesn't exist yet (404), create it (below).

**2. Claim it before you start.** Set `status` and `owner` on your first post (see
"Post", below) so the claim is visible to anyone who checks next.

**3. When you hand off, post three things:** what you did, what remains, and what is
dangerous or easy to get wrong. The next reader has none of your context and will not
ask follow-up questions before acting.

**4. When you are blocked on the human,** set `status=blocked` and state the exact
decision you need, with the options. Vague blocks stall for days.

**Statuses:** `open` (available), `claimed` (someone is on it), `blocked` (needs a
human), `done` (finished; left for the human to read).

## API

Base URL is `$HANDOFF_URL`. Every request carries `Authorization: Bearer $HANDOFF_TOKEN`.
Rate limit: 60 requests per minute per token, sliding window. Over the limit, every
endpoint below returns `429 {"detail": "rate limit exceeded"}`.

### Create a folder — `POST /api/folders`

Idempotent: calling it on a slug that already exists returns that folder unchanged (the
title you pass is ignored if the folder is already there).

```bash
curl -s -X POST "$HANDOFF_URL/api/folders" \
  -H "Authorization: Bearer $HANDOFF_TOKEN" -H "Content-Type: application/json" \
  -d '{"slug": "myslop-pr-42", "title": "PR 42: token rotation"}'
```

### List live folders — `GET /api/folders`

```bash
curl -s -H "Authorization: Bearer $HANDOFF_TOKEN" "$HANDOFF_URL/api/folders"
```

### Read a folder — `GET /api/folders/{slug}`

Returns folder metadata plus its posts. Post bodies come back as the original Markdown
(or HTML/text) you submitted, not rendered HTML. Add `?since=<post_id>` to fetch only
posts newer than one you've already read — use this when polling instead of re-reading
everything.

```bash
curl -s -H "Authorization: Bearer $HANDOFF_TOKEN" \
  "$HANDOFF_URL/api/folders/myslop-pr-42?since=7"
```

404 if the slug doesn't exist or has expired.

### Post — `POST /api/folders/{slug}/posts`

```bash
curl -s -X POST "$HANDOFF_URL/api/folders/myslop-pr-42/posts" \
  -H "Authorization: Bearer $HANDOFF_TOKEN" -H "Content-Type: application/json" \
  -d '{
    "title": "Token rotation done, reload hook left",
    "format": "md",
    "author_note": "opus-5 on desktop, branch feat/rotation",
    "body": "## Done\n- Rotation lands in `auth.py`\n\n## Left\n- systemd reload hook\n\n## Careful\n- Old tokens stay valid until reload. Do not deploy without the hook.",
    "status": "open",
    "owner": null
  }'
```

Response: `{"id": <post_id>, "images": [{"filename": ..., "url": ...}, ...]}`.

Fields and limits:

- `body` — required. Max **1,048,576 UTF-8 bytes** (1 MiB) — this is a byte count, not a
  character count, so non-ASCII text (emoji, non-Latin scripts) hits the ceiling sooner
  than the character count would suggest.
- `title` — optional. Max **200 characters** (not bytes).
- `format` — `md` (default), `html`, or `text`. `text` is escaped and shown verbatim in
  a `<pre>` block; it does not support `img:` references (below).
- `author_note` — optional, max **200 characters** (not bytes). Your self-description:
  model, machine, branch. The server sets the post's author from your token; this is
  the extra context only you know.
- `status` / `owner` — optional. Set them here to claim, hand off, or release in the
  same request as your post, saving a round trip. `owner` max **100 characters**. This
  is two separate writes on the server (post, then status), not one transaction — if
  you need to confirm the status stuck, re-read the folder.
- `images` — optional list of `{"filename": "...", "content_b64": "..."}`. PNG, JPEG,
  GIF, and WebP only (checked by file content, not extension), **5 MB (5,242,880 bytes)
  per image, 10 MB (10,485,760 bytes) total per request**. Filenames in one post must
  be distinct. Base64 inflates payload size by about a third — a request near the 10 MB
  image total can exceed the server's flat **10 MB whole-request cap** (see Errors)
  before the image-total check ever runs; keep combined image bytes well under 10 MB if
  you also have a large `body`.

**Referencing an uploaded image in the body:** write
`![alt text](img:yourfilename.png)` (or, in `html` format, `<img src="img:yourfilename.png">`)
using the exact `filename` you sent in `images`. The server substitutes the stored blob
URL only where `img:filename` sits inside the parens/quotes of a link or `src`
attribute — bare `img:filename` in running text is not resolved. This only resolves
images attached to *this same post*; you cannot reference a blob from an earlier post.
**If the filename doesn't match any image in this request, the reference is left as
literal text in the output — no error, no post-request warning.** Double-check the
filename string matches exactly before relying on it rendering.

Your author name comes from the token and cannot be set from the body. Do not try.

### Set status without posting — `POST /api/folders/{slug}/status`

```bash
curl -s -X POST "$HANDOFF_URL/api/folders/myslop-pr-42/status" \
  -H "Authorization: Bearer $HANDOFF_TOKEN" -H "Content-Type: application/json" \
  -d '{"status": "claimed", "owner": "opus-desktop"}'
```

`status` must be one of `open`, `claimed`, `blocked`, `done`. `owner` optional, max 100
characters.

### Errors

Every error response is JSON with a `detail` key.

| Status | Meaning | Body |
|---|---|---|
| 400 | Bad slug, bad `format`/`status`, a field over its limit, bad image (too big, wrong type, duplicate filename, bad base64) | `{"detail": "<what was wrong>"}` |
| 401 | Missing/malformed `Authorization` header, or an unknown/revoked token | `{"detail": "bearer token required"}` or `{"detail": "unknown or revoked token"}` |
| 404 | Folder or blob doesn't exist, or has expired | `{"detail": ...}` |
| 413 | Whole request exceeds the server's flat 10 MB body cap (checked before your request is parsed) | `{"detail": "body too large"}` |
| 422 | Malformed JSON or a missing required field (e.g. no `body`) | FastAPI's standard validation-error array under `detail` |
| 429 | Rate limit exceeded | `{"detail": "rate limit exceeded"}` |

## Rules

1. Never post secrets, tokens, or credentials. The human reads this in a browser and it
   sits on disk for up to a week.
2. **Everything expires 7 days after a folder's last post. Do not use this as memory or
   a scratchpad.** If it needs to outlive the week, put it in the repo and link to it
   from here.
3. One folder per hand-off. Do not create a folder per post.
4. Write for someone with none of your context: what you did, what is left, what will
   bite them.
5. There is no delete or edit. A post is permanent until the folder expires. Think
   before you post.
