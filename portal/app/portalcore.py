"""Shared rules for the home portal.

The portal add-on and the publish skill both import this file, so a page that
passes the skill's check is a page the portal will serve. Standard library only.

A *page* is a folder with an index.html (a single-file page is just a folder
with one file). Its settings live in <meta> tags in index.html, like the work
portal:

    report-title     REQUIRED. Decides the slug: same title -> same page, replaced in place.
    report-desc      Card text on the home page.
    report-category  Groups cards.
    report-icon      Exactly one emoji.
    report-hidden    "true" keeps it off the home page; reachable at /r/<id>/ only.
    report-audience  Who may open it (enforced once the portal sits behind Cloudflare Access).
    report-author    Keeps the author's own access.
    report-sites     Outside sites the page may use: "images host, code host/path/, data host".
"""
import hashlib
import html as _html
import os
import re
import unicodedata

RULES_VERSION = '1'          # bump in both copies (add-on and content repo tools/) when the rules change

MAX_PAGE_BYTES = 100 * 1024 * 1024          # whole page folder
TEXT_EXT = {'.html', '.htm', '.js', '.mjs', '.css', '.json', '.svg', '.txt', '.md', '.xml', '.csv', '.geojson'}
SCAN_EXT = {'.html', '.htm', '.js', '.mjs', '.css', '.svg'}
SITE_USES = {'images', 'code', 'data'}

META_RE = re.compile(r'<meta\s+[^>]*name\s*=\s*["\'](report-[a-z]+)["\'][^>]*>', re.I)
CONTENT_RE = re.compile(r'content\s*=\s*("([^"]*)"|\'([^\']*)\')', re.I | re.S)


# ---------------------------------------------------------------- tags

def read_meta(html):
    """{tag-name: (value, line)} for every report-* meta tag, first one wins."""
    out = {}
    for m in META_RE.finditer(html):
        name = m.group(1).lower()
        c = CONTENT_RE.search(m.group(0))
        if not c or name in out:
            continue
        val = c.group(2) if c.group(2) is not None else c.group(3)
        out[name] = (_html.unescape(val).strip(), html.count('\n', 0, m.start()) + 1)
    return out


def set_meta(html, name, value):
    """Add or replace one report-* tag. Returns the new html."""
    tag = f'<meta name="{name}" content="{_html.escape(value, quote=True)}">'
    pat = re.compile(r'<meta\s+[^>]*name\s*=\s*["\']%s["\'][^>]*>' % re.escape(name), re.I)
    if pat.search(html):
        return pat.sub(lambda _: tag, html, count=1)
    m = re.search(r'<head[^>]*>', html, re.I)
    if m:
        return html[:m.end()] + '\n' + tag + html[m.end():]
    return tag + '\n' + html


def slugify(title):
    s = unicodedata.normalize('NFKD', title).encode('ascii', 'ignore').decode()
    s = re.sub(r'[^a-zA-Z0-9]+', '-', s).strip('-').lower()
    return s[:80].strip('-') or 'page'


def page_id(slug):
    """Stable unlisted id for /r/<id>/ links."""
    return hashlib.sha256(('portal:' + slug).encode()).hexdigest()[:12]


def is_single_emoji(s):
    s = s.strip()
    if not s or len(s) > 12 or any(ch.isspace() for ch in s):
        return False
    core = [ch for ch in s if ch not in '‍️︎' and not ('\U0001f3fb' <= ch <= '\U0001f3ff')]
    if not core or any(ch.isascii() for ch in core):
        return False
    pictos = [ch for ch in core if unicodedata.category(ch) == 'So' or '\U0001f1e6' <= ch <= '\U0001f1ff']
    if len(pictos) != len(core):
        return False
    # a ZWJ sequence counts as one; otherwise one pictograph (or a flag = two regional letters)
    if '‍' in s:
        return True
    if all('\U0001f1e6' <= ch <= '\U0001f1ff' for ch in core):
        return len(core) == 2
    return len(core) == 1


def parse_sites(value):
    """'images a.com, code b.com/lib/1.0/' -> ([{use, host, path}], [problems])."""
    sites, problems = [], []
    for raw in [p.strip() for p in value.replace('\n', ' ').split(',') if p.strip()]:
        parts = raw.split()
        if len(parts) != 2 or parts[0].lower() not in SITE_USES:
            problems.append(f'"{raw}": each entry is "<images|code|data> <address>"')
            continue
        use, addr = parts[0].lower(), parts[1]
        addr = re.sub(r'^https://', '', addr)
        if addr.startswith('http://'):
            problems.append(f'"{raw}": outside sites must be https')
            continue
        host, _, path = addr.partition('/')
        if '*' in host or not re.fullmatch(r'[a-z0-9.-]+(:\d+)?', host, re.I) or '.' not in host:
            problems.append(f'"{raw}": give one exact host, no wildcards')
            continue
        if use == 'code' and path and not path.endswith('/'):
            problems.append(f'"{raw}": pin a code library to its folder, ending in /')
            continue
        sites.append({'use': use, 'host': host.lower(), 'path': ('/' + path) if path else ''})
    return sites, problems


# ---------------------------------------------------------------- outside loads

# Things that make the browser load from somewhere. Plain <a href> links are not loads.
LOAD_PATTERNS = [
    re.compile(r'<(?:script|img|iframe|audio|video|source|track|embed|input)\b[^>]*?\ssrc\s*=\s*["\']([^"\']+)', re.I),
    re.compile(r'<link\b[^>]*?\shref\s*=\s*["\']([^"\']+)', re.I),
    re.compile(r'<object\b[^>]*?\sdata\s*=\s*["\']([^"\']+)', re.I),
    re.compile(r'url\(\s*["\']?([^"\')\s]+)', re.I),
    re.compile(r'@import\s+["\']([^"\']+)', re.I),
    re.compile(r'\bfetch\(\s*[`"\']([^`"\']+)', re.I),
    re.compile(r'\bimport\s*(?:[\w*{}\s,]+from\s*)?["\']([^"\']+)["\']', re.I),
    re.compile(r'\bimport\(\s*["\']([^"\']+)', re.I),
    re.compile(r'new\s+(?:Shared)?Worker\(\s*["\']([^"\']+)', re.I),
    re.compile(r'\bsrcset\s*=\s*["\']([^"\']+)', re.I),
]


def _use_for(pattern_index, ref_context):
    if pattern_index in (5,):            # fetch
        return 'data'
    if pattern_index in (3,) or re.search(r'\.(png|jpe?g|gif|webp|avif|svg|ico)(\?|$)', ref_context, re.I):
        return 'images'
    return 'code'


def site_allows(sites, url, use):
    m = re.match(r'https://([^/]+)(/.*)?$', url)
    if not m:
        return False
    host, path = m.group(1).lower(), m.group(2) or '/'
    for s in sites:
        if s['host'] != host:
            continue
        if s['use'] != use and not (use == 'images' and s['use'] == 'data'):
            continue
        if s['path'] and not path.startswith(s['path']):
            continue
        return True
    return False


def scan_text(text, relpath, sites, is_html):
    """Problems for outside loads and root-absolute paths in one text file."""
    problems = []
    for i, pat in enumerate(LOAD_PATTERNS):
        if i in (0, 1, 2) and not is_html and not relpath.endswith('.svg'):
            continue
        for m in pat.finditer(text):
            refs = [r.strip().split(' ')[0] for r in m.group(1).split(',')] if i == 9 else [m.group(1).strip()]
            for ref in refs:
                line = text.count('\n', 0, m.start()) + 1
                low = ref.lower()
                if i == 1 and re.search(r'rel\s*=\s*["\'](?:canonical|alternate|author|license|help|me|next|prev|search)["\']', m.group(0), re.I):
                    continue
                if low.startswith(('data:', 'blob:', '#', 'about:', 'javascript:', 'mailto:', 'tel:')) or '${' in ref:
                    continue
                if low.startswith('//'):
                    ref, low = 'https:' + ref, 'https:' + low
                if low.startswith(('http://', 'https://')):
                    use = _use_for(i, ref)
                    if not site_allows(sites, ref, use):
                        problems.append({'file': relpath, 'line': line, 'text': ref[:160],
                                         'reason': f'loads from outside ({use}); inline it, add it to the page, '
                                                   f'or declare it in report-sites'})
                elif ref.startswith('/') and not ref.startswith(('/lib/', '/data/')):
                    problems.append({'file': relpath, 'line': line, 'text': ref[:160],
                                     'reason': 'root path; use a relative path (or /lib/ for engines)'})
    return problems


# ---------------------------------------------------------------- whole page

def check_page(folder):
    """Validate a page folder. Returns a dict:
       {ok, slug, title, meta:{...}, sites:[...], problems:[...], bytes, files}"""
    problems, files, total = [], [], 0
    index = os.path.join(folder, 'index.html')
    if not os.path.isfile(index):
        return {'ok': False, 'problems': [{'file': 'index.html', 'line': 0, 'text': '',
                                            'reason': 'a page needs an index.html at its top level'}]}
    for root, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for n in names:
            if n.startswith('.'):
                continue
            p = os.path.join(root, n)
            if os.path.islink(p):
                problems.append({'file': os.path.relpath(p, folder), 'line': 0, 'text': '', 'reason': 'links are not allowed'})
                continue
            total += os.path.getsize(p)
            files.append(os.path.relpath(p, folder).replace(os.sep, '/'))
    html = open(index, encoding='utf-8', errors='replace').read()
    meta = read_meta(html)

    def bad(tag, reason):
        v, line = meta.get(tag, ('', 0))
        problems.append({'file': 'index.html', 'line': line, 'text': f'{tag}="{v}"', 'reason': reason})

    title = meta.get('report-title', ('', 0))[0]
    if not title:
        problems.append({'file': 'index.html', 'line': 0, 'text': '', 'reason': 'missing <meta name="report-title">'})
    if 'report-icon' in meta and not is_single_emoji(meta['report-icon'][0]):
        bad('report-icon', 'must be exactly one emoji (or leave the tag out)')
    if 'report-hidden' in meta and meta['report-hidden'][0].lower() not in ('true', 'false'):
        bad('report-hidden', 'must be "true" or "false"')
    audience = None
    if 'report-audience' in meta:
        audience = [a.strip() for a in meta['report-audience'][0].split(',') if a.strip()]
        if not audience:
            bad('report-audience', 'is empty; name people or groups, or remove the tag')
        for a in audience:
            if a.lower() != 'everyone' and '@' not in a and not re.fullmatch(r'[A-Za-z][\w .-]{0,40}', a):
                bad('report-audience', f'"{a}" is not an email, a group name, or "everyone"')
    if 'report-author' in meta and '@' not in meta['report-author'][0]:
        bad('report-author', 'must be an email')
    sites = []
    if 'report-sites' in meta:
        sites, sp = parse_sites(meta['report-sites'][0])
        for s in sp:
            bad('report-sites', s)
    for rel in files:
        ext = os.path.splitext(rel)[1].lower()
        if ext in SCAN_EXT:
            text = open(os.path.join(folder, rel), encoding='utf-8', errors='replace').read()
            problems += scan_text(text, rel, sites, ext in ('.html', '.htm'))
    if total > MAX_PAGE_BYTES:
        problems.append({'file': '', 'line': 0, 'text': f'{total:,} bytes',
                         'reason': f'page is over the {MAX_PAGE_BYTES // 1048576} MB limit; move big data to /data or the database'})
    return {
        'ok': not problems,
        'slug': slugify(title) if title else None,
        'title': title,
        'meta': {k: v for k, (v, _) in meta.items()},
        'hidden': meta.get('report-hidden', ('false', 0))[0].lower() == 'true',
        'audience': audience,
        'sites': sites,
        'problems': problems,
        'bytes': total,
        'files': sorted(files),
    }


def csp_for(sites, self_origin):
    """Content-Security-Policy for a page. The sandbox directive gives the page an opaque
    origin, so it can never read the portal's cookies or call its private endpoints."""
    def hosts(*uses):
        out = []
        for s in sites:
            if s['use'] in uses:
                out.append('https://' + s['host'] + (s['path'] if s['use'] == 'code' else ''))
        return ' '.join(sorted(set(out)))
    base = f"{self_origin} 'self'"
    return '; '.join([
        'sandbox allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals '
        'allow-downloads allow-pointer-lock allow-presentation',
        f"default-src {base} data: blob:",
        f"script-src {base} 'unsafe-inline' 'unsafe-eval' blob: {hosts('code')}".rstrip(),
        f"style-src {base} 'unsafe-inline' {hosts('code')}".rstrip(),
        f"font-src {base} data: {hosts('code')}".rstrip(),
        f"img-src {base} data: blob: {hosts('images', 'data', 'code')}".rstrip(),
        f"media-src {base} data: blob:",
        f"connect-src {base} data: blob: {hosts('data')}".rstrip(),
        f"worker-src {base} blob:",
        "frame-src 'none'", "object-src 'none'", "base-uri 'none'", "form-action 'none'",
    ])
