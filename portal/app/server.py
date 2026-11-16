#!/usr/bin/env python3
"""Home portal: serves pages published to a GitHub content repo.

The publish skill commits pages to the content repo; this server keeps a clone of
it, refreshes on a timer (and on a GitHub webhook, once the portal is online), and
serves:

    /                     home page: one card per listed page
    /p/<slug>/...         a page and its files (404 for hidden pages)
    /r/<id>/...           the unlisted link, works for hidden pages too
    /lib/<name>/<ver>/... engines and libraries shared by pages
    /api/pages            JSON list of pages (portal only, not readable by pages)
    /api/health           sync state
    /hook                 GitHub push webhook (optional; needs webhook_secret)

A second listener (share_port, 8098) is for the public share host, which sits outside
Cloudflare Access. It serves nothing from the portal, only:

    /s/<token>/...        a share, until it expires (410 after)
    /lib/<name>/<ver>/... only engine versions that a live share uses

Content repo layout:

    pages/<slug>/index.html (+ any other files)
    lib/<name>/<version>/...
    shares/<id>/share.json + shares/<id>/site/...

Every page is served with a sandboxing Content-Security-Policy, so a page can only
load from the portal and from the outside sites it declared in report-sites.
Standard library only.
"""
import hashlib
import hmac
import html
import json
import mimetypes
import os
import posixpath
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portalcore as pc  # noqa: E402

mimetypes.add_type('text/javascript', '.mjs')
mimetypes.add_type('application/geo+json', '.geojson')
mimetypes.add_type('application/wasm', '.wasm')
mimetypes.add_type('font/woff2', '.woff2')


# ---------------------------------------------------------------- settings

def load_settings():
    opts = {}
    path = os.environ.get('PORTAL_OPTIONS', '/data/options.json')
    if os.path.exists(path):
        opts = json.load(open(path))
    env = lambda k, d=None: os.environ.get('PORTAL_' + k.upper(), opts.get(k, d))
    return {
        'repo': env('github_repo', ''),                  # "owner/name", or a local path/URL for testing
        'token': env('github_token', ''),
        'branch': env('branch', 'main'),
        'poll': int(env('poll_seconds', 30) or 30),
        'webhook_secret': env('webhook_secret', ''),
        'public_url': (env('public_url', '') or '').rstrip('/'),
        'title': env('portal_title', 'Home portal'),
        'data_dir': os.environ.get('PORTAL_DATA', '/data'),
        'port': int(os.environ.get('PORTAL_PORT', 8099)),
        'share_port': int(os.environ.get('PORTAL_SHARE_PORT', 8098)),
    }


S = load_settings()
CONTENT = os.path.join(S['data_dir'], 'content')
STATE = {'pages': {}, 'by_id': {}, 'head': None, 'synced_at': None, 'error': None, 'problems': {},
         'shares': {}, 'share_problems': {}}
LOCK = threading.Lock()


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


# ---------------------------------------------------------------- git sync

def remote_url():
    r = S['repo']
    if re.fullmatch(r'[\w.-]+/[\w.-]+', r):
        if S['token']:
            return f"https://x-access-token:{S['token']}@github.com/{r}.git"
        return f'https://github.com/{r}.git'
    return r


def git(*args, check=True):
    return subprocess.run(['git', '-C', CONTENT, *args], capture_output=True, text=True, check=check,
                          env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})


def sync():
    """Fetch the content repo; rebuild the index if it moved. Safe to call from any thread."""
    with LOCK:
        try:
            if not os.path.isdir(os.path.join(CONTENT, '.git')):
                os.makedirs(CONTENT, exist_ok=True)
                subprocess.run(['git', 'init', '-q', CONTENT], check=True)
                git('remote', 'add', 'origin', remote_url(), check=False)
            git('remote', 'set-url', 'origin', remote_url())
            r = git('fetch', '-q', '--depth', '200', 'origin', S['branch'], check=False)
            if r.returncode != 0:
                msg = (r.stderr or '').replace(S['token'] or '\0', '***').strip()
                if "couldn't find remote ref" in msg:
                    msg = f"branch '{S['branch']}' not found yet (empty repo?)"
                STATE['error'] = msg[-300:]
                return False
            head = git('rev-parse', 'FETCH_HEAD').stdout.strip()
            if head != STATE['head']:
                git('reset', '-q', '--hard', 'FETCH_HEAD')
                git('clean', '-qfdx')
                build_index(head)
            STATE['synced_at'] = time.time()
            STATE['error'] = None
            return True
        except Exception as e:  # keep serving the last good index
            STATE['error'] = str(e).replace(S['token'] or '\0', '***')[-300:]
            log('sync error:', STATE['error'])
            return False


def build_index(head):
    pages, by_id, problems = {}, {}, {}
    root = os.path.join(CONTENT, 'pages')
    for slug in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        folder = os.path.join(root, slug)
        if not os.path.isdir(folder) or slug.startswith('.'):
            continue
        res = pc.check_page(folder)
        if not res.get('title'):
            problems[slug] = res['problems']
            continue
        if res['problems']:
            problems[slug] = res['problems']       # still served; CSP blocks what it must
        info = {
            'slug': slug, 'id': pc.page_id(slug), 'title': res['title'],
            'desc': res['meta'].get('report-desc', ''), 'category': res['meta'].get('report-category', ''),
            'icon': res['meta'].get('report-icon', '') if pc.is_single_emoji(res['meta'].get('report-icon', '')) else '',
            'hidden': res['hidden'], 'audience': res['audience'], 'author': res['meta'].get('report-author'),
            'sites': res['sites'], 'files': len(res['files']), 'bytes': res['bytes'],
            'updated': last_change(f'pages/{slug}'),
        }
        pages[slug] = info
        by_id[info['id']] = slug
    shares, share_problems = build_share_index()
    STATE.update(pages=pages, by_id=by_id, head=head, problems=problems, shares=shares, share_problems=share_problems)
    log(f'index: {len(pages)} pages, {len(shares)} shares at {head[:8]}' +
        (f', {len(problems) + len(share_problems)} with problems' if problems or share_problems else ''))
    threading.Thread(target=report_status, args=(head, pages, problems), daemon=True).start()


def build_share_index():
    """{token: info} for every share; expiry is checked per request, not here."""
    shares, problems = {}, {}
    root = os.path.join(CONTENT, 'shares')
    for sid in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        folder = os.path.join(root, sid)
        if not os.path.isdir(folder) or sid.startswith('.'):
            continue
        info, problem = pc.read_share(folder)
        if not info:
            problems[sid] = problem
            continue
        if info['token'] in shares:
            problems[sid] = 'same token as another share; ignored'
            continue
        site = os.path.join(folder, 'site')
        res = pc.check_page(site, share=True)
        info.update(id=sid, sites=res.get('sites', []), libs=pc.lib_refs(site),
                    title=info['title'] or res.get('title') or sid)
        shares[info['token']] = info
    return shares, problems


def share_libs():
    """Engine versions that live (unexpired) shares use."""
    now = time.time()
    out = set()
    for s in list(STATE['shares'].values()):
        if s['expires'] > now:
            out |= s['libs']
    return out


def last_change(path):
    r = git('log', '-1', '--format=%ct', '--', path, check=False)
    try:
        return int(r.stdout.strip())
    except ValueError:
        return int(time.time())


def report_status(head, pages, problems):
    """Post a commit status per page to GitHub, so publishes show ✓/✗ on the commit."""
    if not (S['token'] and re.fullmatch(r'[\w.-]+/[\w.-]+', S['repo'])):
        return
    base = S['public_url'] or ''
    items = [(f'portal/{s}', 'failure', '; '.join(p['reason'] for p in problems[s])[:138], '')
             for s in problems]
    for s, p in pages.items():
        if s in problems:
            continue
        link = f"{base}/r/{p['id']}/" if p['hidden'] else f'{base}/p/{s}/'
        items.append((f'portal/{s}', 'success', f"live{' (hidden)' if p['hidden'] else ''}: {link}"[:138],
                      link if base else ''))
    for ctx, state, desc, url in items[:60]:
        body = {'state': state, 'context': ctx, 'description': desc}
        if url:
            body['target_url'] = url
        req = urllib.request.Request(
            f"https://api.github.com/repos/{S['repo']}/statuses/{head}", data=json.dumps(body).encode(),
            headers={'Authorization': f"Bearer {S['token']}", 'Accept': 'application/vnd.github+json',
                     'User-Agent': 'home-portal'}, method='POST')
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:
            log('status post skipped:', getattr(e, 'code', e))
            return


def poller():
    while True:
        sync()
        time.sleep(max(5, S['poll']))


# ---------------------------------------------------------------- pages

def esc(s):
    return html.escape(str(s or ''), quote=True)


TILE_COLORS = ['#c0563b', '#3a6ea5', '#5b8a3a', '#8a5bb0', '#b08a2e', '#2e8a86', '#a0445e', '#5a6470']


def home_page():
    pages = [p for p in STATE['pages'].values() if not p['hidden']]
    groups = {}
    for p in sorted(pages, key=lambda p: -p['updated']):
        groups.setdefault(p['category'] or 'Pages', []).append(p)
    order = sorted(groups, key=lambda c: (c == 'Pages', c.lower()))
    cards = []
    for cat in order:
        items = []
        for p in groups[cat]:
            if p['icon']:
                badge = f'<span class="ic">{esc(p["icon"])}</span>'
            else:
                col = TILE_COLORS[int(hashlib.md5(p['slug'].encode()).hexdigest(), 16) % len(TILE_COLORS)]
                badge = f'<span class="ic tile" style="background:{col}">{esc(p["title"][:1].upper())}</span>'
            when = time.strftime('%b %-d, %Y', time.localtime(p['updated']))
            items.append(f'<a class="card" href="/p/{esc(p["slug"])}/">{badge}<span class="tx">'
                         f'<b>{esc(p["title"])}</b><span>{esc(p["desc"])}</span><i>Updated {when}</i></span></a>')
        cards.append(f'<section><h2>{esc(cat)}</h2><div class="grid">{"".join(items)}</div></section>')
    if not cards:
        cards = ['<p class="empty">Nothing published yet. Ask Claude to publish a page.</p>']
    hidden = sum(1 for p in STATE['pages'].values() if p['hidden'])
    foot = f'{len(pages)} pages' + (f' · {hidden} hidden' if hidden else '')
    if STATE['error']:
        foot += f' · <span class="err">sync problem: {esc(STATE["error"])}</span>'
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(S["title"])}</title>
<style>
:root{{--bg:#f6f3ee;--card:#fff;--ink:#1d1b18;--mut:#6f6a62;--line:#e4dfd6;--acc:#c0563b}}
@media(prefers-color-scheme:dark){{:root{{--bg:#151412;--card:#1f1d1a;--ink:#efeae2;--mut:#9a948a;--line:#2e2b27}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}}
header{{padding:28px 24px 8px;max-width:1100px;margin:auto}}h1{{margin:0;font-size:1.5rem;letter-spacing:-.01em}}
main{{max-width:1100px;margin:auto;padding:0 24px 40px}}h2{{font-size:.75rem;letter-spacing:.12em;text-transform:uppercase;color:var(--mut);margin:28px 0 10px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}}
.card{{display:flex;gap:14px;padding:14px;background:var(--card);border:1px solid var(--line);border-radius:12px;color:inherit;text-decoration:none;transition:border-color .15s,transform .1s}}
.card:hover{{border-color:var(--acc)}}.card:active{{transform:scale(.99)}}
.ic{{flex:0 0 44px;height:44px;border-radius:10px;display:grid;place-items:center;font-size:26px;background:var(--bg)}}
.tile{{color:#fff;font-weight:700;font-size:20px}}.tx{{display:flex;flex-direction:column;gap:3px;min-width:0}}
.tx span{{color:var(--mut);font-size:.88rem;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.tx i{{color:var(--mut);font-size:.75rem;font-style:normal}}footer{{max-width:1100px;margin:auto;padding:0 24px 30px;color:var(--mut);font-size:.8rem}}
.err{{color:#c0392b}}.empty{{color:var(--mut)}}
</style></head><body><header><h1>{esc(S["title"])}</h1></header><main>{"".join(cards)}</main>
<footer>{foot}</footer></body></html>'''


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = 'HomePortal/1'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        if os.environ.get('PORTAL_QUIET'):
            return
        log(self.address_string(), fmt % args)

    def origin(self):
        host = self.headers.get('X-Forwarded-Host') or self.headers.get('Host') or 'localhost'
        proto = self.headers.get('X-Forwarded-Proto') or ('https' if self.headers.get('Cf-Visitor') else 'http')
        return f'{proto}://{host}'

    def send(self, code, body, ctype='text/html; charset=utf-8', headers=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def not_found(self, msg='Not found'):
        self.send(404, f'<!DOCTYPE html><title>Not found</title><p style="font-family:system-ui;padding:2em">{esc(msg)}. '
                       f'<a href="/">Back to the portal</a></p>')

    def viewer(self):
        """Signed-in email from Cloudflare Access, when the portal sits behind it."""
        return (self.headers.get('Cf-Access-Authenticated-User-Email') or '').lower() or None

    def may_view(self, page):
        aud = page.get('audience')
        if not aud or any(a.lower() == 'everyone' for a in aud):
            return True
        who = self.viewer()
        if who is None:
            return True            # no sign-in layer yet: home network only
        allowed = {a.lower() for a in aud} | ({page['author'].lower()} if page.get('author') else set())
        return who in allowed

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self._get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            import traceback
            log('ERROR serving', self.path, '\n' + traceback.format_exc())
            try:
                self.send(500, '<p style="font-family:system-ui;padding:2em">The portal hit an error; '
                               'the add-on log has the details.</p>')
            except Exception:
                pass

    def _get(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == '/' or path == '/index.html':
            return self.send(200, home_page(), headers={'Cache-Control': 'no-store'})
        if path == '/api/health':
            live = sum(1 for s in list(STATE['shares'].values()) if s['expires'] > time.time())
            return self.send(200, json.dumps({k: STATE[k] for k in ('head', 'synced_at', 'error')} |
                                             {'pages': len(STATE['pages']), 'shares_live': live,
                                              'share_problems': STATE['share_problems'],
                                              'rules': pc.RULES_VERSION}), 'application/json')
        if path == '/api/pages':
            return self.send(200, json.dumps(sorted(STATE['pages'].values(), key=lambda p: p['slug']), indent=1),
                             'application/json', {'Cache-Control': 'no-store'})
        if path == '/api/problems':
            return self.send(200, json.dumps(STATE['problems'], indent=1), 'application/json')
        m = re.match(r'^/(p|r)/([^/]+)(/.*)?$', path)
        if m:
            kind, key, rest = m.group(1), m.group(2), m.group(3)
            slug = key if kind == 'p' else STATE['by_id'].get(key)
            page = STATE['pages'].get(slug or '')
            if not page or (kind == 'p' and page['hidden']):
                return self.not_found('No page here')
            if rest is None:
                return self.send(301, '', headers={'Location': f'/{kind}/{key}/'})
            if not self.may_view(page):
                return self.send(403, '<p style="font-family:system-ui;padding:2em">This page is shared with '
                                      'specific people, and you are not one of them.</p>')
            return self.static(os.path.join(CONTENT, 'pages', slug), rest, page)
        m = re.match(r'^/lib/(.+)$', path)
        if m:
            return self.static(os.path.join(CONTENT, 'lib'), '/' + m.group(1), None, immutable=True)
        if path == '/favicon.ico':
            return self.send(204, b'')
        self.not_found()

    def static(self, root, rel, page, immutable=False, extra=None):
        rel = posixpath.normpath(urllib.parse.unquote(rel))
        if rel.endswith('/') or rel in ('/', '.'):
            rel = rel.rstrip('/') + '/index.html'
        full = os.path.realpath(os.path.join(root, rel.lstrip('/')))
        if not full.startswith(os.path.realpath(root) + os.sep) or not os.path.isfile(full):
            return self.not_found()
        if any(part.startswith('.') for part in rel.split('/')):
            return self.not_found()
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype in ('application/json', 'application/geo+json', 'image/svg+xml'):
            ctype += '; charset=utf-8'
        data = open(full, 'rb').read()
        etag = '"' + hashlib.sha1(data).hexdigest()[:16] + '"'
        if self.headers.get('If-None-Match') == etag:
            self.send_response(304)
            self.send_header('ETag', etag)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        headers = {
            'ETag': etag,
            # pages run in an opaque origin, so their own module scripts and fetches are cross-origin
            'Access-Control-Allow-Origin': '*',
            'Cache-Control': 'public, max-age=31536000, immutable' if immutable else 'no-cache',
            'Cross-Origin-Resource-Policy': 'cross-origin',
        }
        if page is not None or full.endswith(('.html', '.htm', '.svg')):
            headers['Content-Security-Policy'] = pc.csp_for(page['sites'] if page else [], self.origin())
        headers.update(extra or {})
        self.send(200, data, ctype, headers)

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != '/hook':
            return self.not_found()
        body = self.rfile.read(min(int(self.headers.get('Content-Length') or 0), 5_000_000))
        secret = S['webhook_secret']
        sig = self.headers.get('X-Hub-Signature-256', '')
        good = secret and hmac.compare_digest(sig, 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest())
        if not good:
            return self.send(401, '{"error":"bad signature"}', 'application/json')
        threading.Thread(target=sync, daemon=True).start()
        self.send(202, '{"ok":true}', 'application/json')


SHARE_HEADERS = {
    'Cache-Control': 'private, no-store',          # nothing kept by Cloudflare or browsers past expiry
    'X-Robots-Tag': 'noindex, nofollow, noarchive',
}


def share_message(title, text):
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title></head>'
            f'<body style="font:16px/1.5 system-ui,sans-serif;max-width:32em;margin:15vh auto;padding:0 1.5em;color:#444">'
            f'<h1 style="font-size:1.3em;color:#222">{esc(title)}</h1><p>{esc(text)}</p></body></html>')


class ShareHandler(Handler):
    """The public share host. Knows nothing about the portal: no home page, no API, no hook."""
    server_version = 'Share/1'

    def not_found(self, msg='Not found'):
        self.send(404, share_message('Not found', 'There is nothing at this address.'),
                  headers=SHARE_HEADERS)

    def _get(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == '/robots.txt':
            return self.send(200, 'User-agent: *\nDisallow: /\n', 'text/plain; charset=utf-8')
        if path == '/favicon.ico':
            return self.send(204, b'')
        m = re.match(r'^/s/([^/]+)(/.*)?$', path)
        if m:
            token, rest = m.group(1), m.group(2)
            share = STATE['shares'].get(token) if pc.SHARE_TOKEN_RE.fullmatch(token) else None
            if not share:
                return self.not_found()
            if share['expires'] <= time.time():
                return self.send(410, share_message('This link has expired',
                                                    'Ask the person who sent it for a new one.'),
                                 headers=SHARE_HEADERS)
            if rest is None:
                return self.send(301, '', headers={'Location': f'/s/{token}/', **SHARE_HEADERS})
            return self.static(os.path.join(CONTENT, 'shares', share['id'], 'site'), rest, share,
                               extra=SHARE_HEADERS)
        clean = posixpath.normpath(urllib.parse.unquote(path))
        m = re.match(r'^/lib/([a-z0-9][a-z0-9-]*)/([^/]+)(/.+)$', clean)
        if m and (m.group(1), m.group(2)) in share_libs():
            return self.static(os.path.join(CONTENT, 'lib'), '/' + m.group(1) + '/' + m.group(2) + m.group(3),
                               None, extra={'Cache-Control': 'public, max-age=3600'})
        self.not_found()

    def do_POST(self):
        self.not_found()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def get_request(self):
        sock, addr = super().get_request()
        log('connection from', addr[0])
        return sock, addr

    def handle_error(self, request, client_address):
        import traceback
        log('ERROR on connection from', client_address[0], '\n' + traceback.format_exc())


def self_test():
    time.sleep(2)
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:{S['port']}/api/health", timeout=5)
        log('self-test: portal answers inside the add-on (HTTP %d)' % r.status)
    except Exception as e:
        log('self-test FAILED inside the add-on:', repr(e))


def main():
    log('settings:', {k: ('***' if k in ('token', 'webhook_secret') and v else v) for k, v in S.items()})
    if not S['repo']:
        log('No github_repo set. Open the add-on Configuration tab and fill it in.')
    log(f"portal starting on :{S['port']} for {S['repo'] or '(no repo)'} ({S['branch']}), polling every {S['poll']}s")
    threading.Thread(target=poller, daemon=True).start()
    srv = Server(('0.0.0.0', S['port']), Handler)
    if S['share_port'] and S['share_port'] != S['port']:
        share_srv = Server(('0.0.0.0', S['share_port']), ShareHandler)
        threading.Thread(target=share_srv.serve_forever, daemon=True).start()
        log(f"share links listening on :{S['share_port']} (only /s/<token>/ and the engines live shares use)")
    threading.Thread(target=self_test, daemon=True).start()
    log('listening; open http://<home-assistant-ip>:%d/' % S['port'])
    srv.serve_forever()


if __name__ == '__main__':
    main()
