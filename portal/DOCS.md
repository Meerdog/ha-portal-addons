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

## Options

- `github_repo` — the content repo, `owner/name`.
- `github_token` — see above.
- `branch` — default `main`.
- `poll_seconds` — how often to check for new commits (default 30).
- `portal_title` — the home page heading.
- `public_url` — later, your outside address (for example `https://portal.example.com`), used in links.
- `webhook_secret` — later, with a GitHub webhook to `<public_url>/hook`, pages go live instantly.

## Safety

Every page is served with a security policy that runs it in a sealed-off origin and only
lets it load from the portal and from outside sites it declares in `report-sites`.
`report-audience` is enforced once the portal sits behind Cloudflare Access; on the home
network alone, anyone on your Wi-Fi can open any page.
