# Home Portal

Serves the pages Claude publishes to your GitHub content repo (`Meerdog/portal-content`).
Claude commits a page; this add-on pulls the repo every few seconds and serves it.
Nothing on Home Assistant changes when you publish, update, unpublish or restore.

## Setup (once)

1. **GitHub token.** github.com → Settings → Developer settings → Personal access tokens →
   **Fine-grained tokens** → Generate new token.
   - Repository access: **Only select repositories** → `portal-content`
   - Permissions → Repository permissions: **Contents: Read-only**,
     **Commit statuses: Read and write** (optional; shows ✓/✗ on each publish commit)
   - Expiration: up to a year; put a reminder in your calendar.
2. Paste it into this add-on's **Configuration** tab as `github_token`. Leave the rest as is.
3. **Start** the add-on and turn on **Start on boot**, **Watchdog** and **Auto update**.
4. Open the web UI (port 8099). You should see the portal's home page.

## Addresses

| Address | What |
|---|---|
| `/` | Home page: one card per listed page |
| `/p/<slug>/` | A page (not for hidden pages) |
| `/r/<id>/` | A page's unlisted link; the only way into a hidden page |
| `/lib/<name>/<version>/` | Shared engines and libraries |
| `/api/health` | Sync state, for checking |

On the separate share port (8098), and nothing else:

| Address | What |
|---|---|
| `/s/<token>/` | A share link, until it expires; after that it says the link has expired |
| `/lib/<name>/<version>/` | Only the engine versions a live share uses |

## Options

- `github_repo` — the content repo, `owner/name`.
- `github_token` — see above.
- `branch` — default `main`.
- `poll_seconds` — how often to check for new commits (default 30).
- `portal_title` — the home page heading.
- `public_url` — later, your outside address (for example `https://portal.example.com`), used in links.
- `webhook_secret` — later, with a GitHub webhook to `<public_url>/hook`, pages go live instantly.

## Share links

Share links are for things you want to send to someone without them signing in. Each one
has a long random link and an end date (7 days unless you ask for another, at most 90).
They're served on their own port, so the public share address can never reach the portal.

Setup (once), in the **Cloudflared** add-on's Configuration (⋮ → Edit in YAML), add a second host:

```yaml
additional_hosts:
  - hostname: portal.tango.trading
    service: http://c19af449-home-portal:8099
  - hostname: share.tango.trading
    service: http://c19af449-home-portal:8098
```

Restart Cloudflared. Do **not** add `share.tango.trading` to Cloudflare Access; if your Access
application covers `*.tango.trading`, add a separate application for `share.tango.trading`
with a **Bypass – Everyone** policy. Leave port 8098 empty under Network; the tunnel reaches it
inside Home Assistant.

## Safety

Every page is served with a security policy that runs it in a sealed-off origin and only
lets it load from the portal and from outside sites it declares in `report-sites`.
`report-audience` is enforced once the portal sits behind Cloudflare Access; on the home
network alone, anyone on your Wi-Fi can open any page. A share link is open to anyone
who has the link until it expires or is revoked, and is never listed or indexed.
