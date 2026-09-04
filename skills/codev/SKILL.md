---
name: codev
description: Build a website and publish it to Codev, a static host that gives every site a public URL. Use when the user asks to build, make or create a website, page, landing page, portfolio, demo, prototype or single-page app, or to deploy, publish, host, ship, put online or share one; also to update, list, rename, protect, roll back, duplicate or delete a site already on Codev.
---

# Codev

Codev hosts static output: HTML, CSS, JavaScript, images, fonts and single-page apps.
It does not run server code, so an app that needs a backend must call one hosted elsewhere.
Every publish creates an immutable version and serves it at `https://<slug>.<sites domain>`.

The API lives at `https://api.co.dev`; set `CODEV_API_URL` to point at another Codev API.
This skill's folder contains `publish.py`, a dependency-free Python 3.10+ script that does the whole publish protocol: hash the files, ask for upload URLs, upload only what is new, finalize.

## Build the site

Choose the simplest form that satisfies the request.

- Pages, landing pages, portfolios and documents: write plain HTML, CSS and JavaScript into one folder with `index.html` at its root, use relative links, and skip a build step.
- Applications with routing or a framework: use Vite (`npm create vite@latest`, then `npm run build`, and publish `dist/`).
- Next.js: set `output: "export"` in the config, build, and publish `out/`.

Before publishing, confirm that `index.html` sits at the top level of the folder, that every asset path resolves from there, and that nothing secret is in the folder, because everything published is public.
When you can, serve the folder locally (`python3 -m http.server 8000` inside it) and check the pages before publishing.

## Publish

This skill's folder is the base directory the harness reported when it loaded this file.
Run the script from there:

```
python3 "<skill folder>/publish.py" ./dist --spa
```

If no base directory was reported, look for `publish.py` under `~/.agents/skills/codev`, `~/.claude/skills/codev`, `~/.cursor/skills/codev` or `~/.codex/skills/codev`.
Pass `--spa` when the app uses client-side routing, so unknown paths serve `index.html`.

The script prints `site_url=`, `site_id=`, `version_id=` and `preview_url=`.
Keep `site_id` for later updates.
Without a key the site is anonymous: it also prints `claim_url=` and `expires_at=`, and it expires in 24 hours unless the user opens the claim link while signed in.

## Update an existing site

Use `--site <site id>` to publish a new version instead of a new site.
Pass `--base-version <version id>` with the version you built on; if someone else published in between, the script exits with code 4 and prints `current_version_id=`.
Reload that version's state, rebuild if needed, and retry with the new base.
Files that were already uploaded for an earlier version are skipped automatically.

## Keys and pairing

A key makes sites permanent and lets the same agent update them later.
Read it from `CODEV_API_KEY` in the environment, or pass `--api-key`.
When there is no key and the user wants a permanent site, run the pairing flow:

```
python3 "<skill folder>/publish.py" ./dist --pair --name "Claude Code"
```

The script prints a short code and a URL.
Ask the user to open the URL, sign in and approve the code, then wait; the script polls and continues on its own.
It prints `api_key=` once at the end.
Tell the user to store that value as `CODEV_API_KEY`; never write it into a file yourself.
Keys can also be created on the dashboard's API keys page.

## Manage sites with the API

Everything after the first publish is a REST call with the key as a bearer token:

```
API="${CODEV_API_URL:-https://api.co.dev}"
AUTH="Authorization: Bearer $CODEV_API_KEY"
```

Errors come back as `{"detail": {"code": "...", "message": "..."}}`; the codes to branch on are `version_conflict`, `slug_taken`, `upload_incomplete` and `storage_unconfigured`.

| Task | Call |
| --- | --- |
| List the user's sites | `curl -sS "$API/v1/sites" -H "$AUTH"` |
| Show one site | `curl -sS "$API/v1/sites/<site_id>" -H "$AUTH"` |
| Rename or toggle SPA mode | `curl -sS -X PATCH "$API/v1/sites/<site_id>" -H "$AUTH" -H 'Content-Type: application/json' -d '{"display_name": "Docs", "spa_mode": true}'` |
| Delete a site | `curl -sS -X DELETE "$API/v1/sites/<site_id>" -H "$AUTH"` (204; the hostname then answers 410 and the slug stays taken) |
| Duplicate a site | `curl -sS -X POST "$API/v1/sites/<site_id>/duplicate" -H "$AUTH" -H 'Content-Type: application/json' -d '{"slug": "docs-copy", "display_name": "Docs copy"}'` |
| List versions | `curl -sS "$API/v1/sites/<site_id>/versions" -H "$AUTH"` |
| Roll back to a version | `curl -sS -X POST "$API/v1/sites/<site_id>/versions/<version_id>/restore" -H "$AUTH"` |
| Set a password | `curl -sS -X PUT "$API/v1/sites/<site_id>/access" -H "$AUTH" -H 'Content-Type: application/json' -d '{"mode": "password", "password": "open-sesame"}'` |
| Restrict to emails | the same call with `{"mode": "restricted", "allowed_emails": ["a@example.com"]}`; `{"mode": "public"}` opens it again |
| New version by hand | `POST "$API/v1/sites/<site_id>/versions"` with the same body as `/v1/publishes`, then upload and finalize |

A key cannot claim a site, create or revoke keys, or approve a pairing; those need the signed-in user on the dashboard.

## Report back to the user

- Always give the `site_url`.
- For an anonymous site, also give the claim link and say when it expires.
- After an access change, say what a visitor will now see.
- After an update, say the new version is live, and keep the `site_id` in the conversation for the next change.
- Never paste an API key into a file, a commit, or a message the user did not ask for.

## Limits

- Up to 25 MB per file, 5000 files and 500 MB per version.
- A folder named `.codev/` is reserved and cannot be published.
- Anonymous sites cannot choose a slug and expire after 24 hours.
- Slugs are lowercase letters, digits and single hyphens, and signed-in publishes only.

## Options

```
publish.py <dir> [--site ID] [--slug SLUG] [--name NAME] [--spa]
                 [--base-version ID] [--base-url URL] [--api-key KEY]
                 [--pair] [--concurrency N] [--json]
```

`--json` prints one JSON object instead of `key=value` lines.
`--base-url` overrides `CODEV_API_URL`.

## Exit codes

0 ok, 1 unexpected error, 2 usage or folder problem, 3 not authorized, 4 version conflict, 5 upload failed, 6 finalize failed, 7 pairing expired or denied.

## Without Python

The protocol is three calls with a bearer token (`Authorization: Bearer <key>`).
`POST https://api.co.dev/v1/publishes` with `{"spa_mode": bool, "files": [{"path", "size", "content_type", "sha256"}]}` returns `uploads` (PUT each file's bytes to `put_url` with exactly the returned `headers`), `skipped`, and `finalize_url`.
`POST https://api.co.dev<finalize_url>` publishes the version and returns `site_url`.
