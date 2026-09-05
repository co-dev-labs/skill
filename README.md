# Codev skill

Codev publishes static sites and single-page apps to a public URL, for people and for AI agents.
This skill teaches Claude Code, Cursor and Codex to build a site, publish it, and manage it afterwards.
New websites use React + Vite with TypeScript by default, including landing pages and portfolios; Codev stores their editable source privately alongside versioned production builds.
A new computer can download the original project by its site URL, edit it, and publish a new version.

## Install

```
npx skills add co-dev-labs/skill --skill codev -g
```

Then ask your agent to build or publish a website.

## Connect once

Approve one clear connection covering all your current and future sites, including editable source.
The agent remembers the connection in your operating system credential store and resumes automatically after approval.
Future edits reuse it across projects and chats without another browser visit.
You can revoke access in Connected agents.
The normal connection permits creating, editing, publishing, and restoring sites; it excludes deletion, ownership changes, and access settings.
An explicit anonymous test publish still expires in 24 hours and prints a save link.
For automation, an existing `CODEV_API_KEY` remains supported.

## What is inside

- `skills/codev/SKILL.md` is the instructions the agent follows.
- `skills/codev/publish.py` is a standard library Python 3.10+ script that runs the publish protocol, and it works by hand too.
- `skills/codev/project.py` and its companion Python modules save private React/Vite source revisions, reopen projects by domain, build saved snapshots, and restore previous iterations without Git.
- `skills/codev/auth.py` shares secure connections between both clients; its isolated native-store helper uses Python keyring.

Source projects need a server with source storage enabled and a key explicitly authorized for `sources:read` and `sources:write`.
Run a project command with `--connect` to request those permissions once when needed.
Older saved connections request a source-access grant once; explicit API keys keep their scopes and must be replaced if insufficient.
`publish.py` rejects detected React/Vite output without `--output-only`, which is reserved for intentional uploads without source.
Opening a domain downloads source into a fresh directory; it never executes project scripts or overwrites an existing workspace.

## Configuration

The skill talks to `https://api.co.dev`.
Set `CODEV_API_URL` to point it at another Codev API, and `CODEV_API_KEY` to publish under your account.

This repository is generated from the Codev source and pushed here by CI on every change, so edits made here are overwritten.
