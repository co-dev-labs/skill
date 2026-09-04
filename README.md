# Codev skill

Codev publishes static sites and single-page apps to a public URL, for people and for AI agents.
This skill teaches Claude Code, Cursor and Codex to build a site, publish it, and manage it afterwards.

## Install

```
npx skills add co-dev-labs/skill --skill codev -g
```

Then ask your agent to build or publish a website.

## Keys

The first publish without a key creates a site that expires in 24 hours and prints a claim link.
For permanent sites the skill can pair with your account: it shows a short code, you approve it on the dashboard, and the agent receives its own key.
You can also create a key on the dashboard at https://co.dev and set it as `CODEV_API_KEY`.

## What is inside

- `skills/codev/SKILL.md` is the instructions the agent follows.
- `skills/codev/publish.py` is a standard library Python 3.10+ script that runs the publish protocol, and it works by hand too.

## Configuration

The skill talks to `https://api.co.dev`.
Set `CODEV_API_URL` to point it at another Codev API, and `CODEV_API_KEY` to publish under your account.

This repository is generated from the Codev source and pushed here by CI on every change, so edits made here are overwritten.
