#!/usr/bin/python3
"""
O11 provider script for Digiturk Play (digiturkplay.com - "Yurt Disi" international package).

Actions:
    action=login                  -> validate / refresh the session
    action=channels               -> JSON channel list for O11
    action=manifest id=<urlSlug>  -> JSON manifest descriptor for one channel

Notes:
  - digiturkplay.com sits behind a WAF that rejects plain python-requests TLS
    fingerprints, so every call goes through curl_cffi impersonating Chrome.
  - Login is POST /api/login (a Next.js route). It returns a wrapper JWT in the
    "token" cookie; every other API call is proxied through POST /api/service.
  - Streams are NOT DRM encrypted (no ContentProtection / no EXT-X-KEY), so
    UseCdm is False. They are Akamai token protected and the token is bound to
    the egress IP, which is why manifest+media must use the same proxy as this
    script (the North Macedonia tunnel on 127.0.0.1:8888).
"""
import json
import re
import sys
import os
import time
import base64

try:
    from curl_cffi import requests as cr
except ImportError:
    print("Error: curl_cffi is required (pip3 install curl_cffi)", file=sys.stderr)
    sys.exit(1)

config_file = "config_digiturk.json"
auth_file = "auth_digiturk.json"

BASE = "https://www.digiturkplay.com"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
IMPERSONATE = "chrome124"
PLAY_REQUEST_TYPE_CHANNEL = 2
CDN_PROXY_PORT = 9192
# reissue a channel url this long before its CDN token expires
CDN_URL_RENEW_MARGIN = 900

# Sports feeds delivered as HLS. O11 cannot consume their Akamai token urls
# directly (see the manifest action), so they are routed through the local CDN
# proxy and need the per-stream network override at import time.
HLS_SLUGS = {
    "bein-sports-international",
    "beIN-Sports-2",
    "bein_doksanartibir",
    "bein-sports-max-2",
    "trt-spor",
}


def parse_params(params, name, default=""):
    ret = default
    for p in params:
        if p.split("=")[0] == name and len(p.split("=")) >= 2:
            ret = p[len(p.split("=")[0])+1:]
    return ret


user = parse_params(sys.argv, 'user')
password = parse_params(sys.argv, 'password')
account = parse_params(sys.argv, 'account')
proxy = parse_params(sys.argv, 'proxy')
bind = parse_params(sys.argv, 'bind')
doh = parse_params(sys.argv, 'doh')
dns = parse_params(sys.argv, 'dns')
worker = parse_params(sys.argv, 'worker')
id = parse_params(sys.argv, 'id')
action = parse_params(sys.argv, 'action')

if account and not user:
    if ':' in account:
        parts = account.split(':', 1)
        user = parts[0]
        password = parts[1]
    else:
        user = account


def script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def load_config():
    try:
        with open(os.path.join(script_dir(), config_file), 'r') as f:
            return json.load(f)
    except Exception:
        return {}


config = load_config()
if not proxy:
    proxy = config.get('proxy', '')

# Route the sports (HLS) feeds through the local CDN helper. Needed only when
# this machine's own ip cannot fetch them (see the manifest action). On a local
# install in a country Digiturk serves, set "use_cdn_proxy": false and every
# channel is handed the original CDN link.
USE_CDN_PROXY = bool(config.get('use_cdn_proxy', True))
CDN_PROXY_PORT = int(config.get('cdn_proxy_port', CDN_PROXY_PORT))

proxies = {'http': proxy, 'https': proxy} if proxy else None


def new_session():
    s = cr.Session(impersonate=IMPERSONATE, timeout=30, verify=False)
    if proxies:
        s.proxies = proxies
    s.headers.update({'User-Agent': USER_AGENT})
    return s


session = new_session()


def api_headers(referer="/en/canli-tv"):
    return {
        'content-type': 'application/json',
        'accept': 'application/json',
        'origin': BASE,
        'referer': BASE + referer,
        'user-agent': USER_AGENT,
        'x-requested-with': 'XMLHttpRequest',
    }


def svc(payload, referer="/en/canli-tv"):
    """Call the site's same-origin API proxy."""
    r = session.post(BASE + "/api/service", headers=api_headers(referer),
                     data=json.dumps(payload))
    try:
        return r.json()
    except Exception:
        raise Exception(f"Non-JSON response ({r.status_code}): {r.text[:150]}")


def token_expiry(tok):
    """Read exp out of the wrapper JWT without verifying it."""
    try:
        p = tok.split('.')[1]
        p += '=' * (-len(p) % 4)
        return int(json.loads(base64.urlsafe_b64decode(p)).get('exp', 0))
    except Exception:
        return 0


def do_login(username, pwd):
    """POST /api/login -> sets the 'token' cookie on the session."""
    session.get(BASE + "/en", headers={'user-agent': USER_AGENT})
    r = session.post(
        BASE + "/api/login",
        headers={'content-type': 'application/json', 'accept': '*/*',
                 'origin': BASE, 'referer': BASE + '/en',
                 'user-agent': USER_AGENT},
        data=json.dumps({"loginEmailBody": {
            "username": username, "password": pwd, "passCode": ""}}))
    if r.status_code != 200:
        raise Exception(f"login HTTP {r.status_code}: {r.text[:150]}")
    body = {}
    try:
        body = r.json()
    except Exception:
        pass
    tok = session.cookies.get("token")
    if not tok:
        msg = (body.get('message') or {}).get('text') or json.dumps(body)[:150]
        raise Exception(f"login did not return a token cookie: {msg}")
    return tok


def save_auth(tok):
    with open(os.path.join(script_dir(), auth_file), 'w') as f:
        json.dump({'token': tok, 'expires': token_expiry(tok)}, f, indent=2)


def session_is_valid():
    try:
        j = svc({"path": "/api/v1/content/live-tv-summary"})
        return bool((j or {}).get('data', {}).get('items'))
    except Exception:
        return False


def get_auth(force=False):
    """Reuse the cached token when possible, otherwise log in again.

    The token is valid for ~90 days, so we do NOT spend a round trip validating
    it on every call - with 85 channels that dominated the runtime. Callers
    retry via reauth() if a request actually comes back unauthenticated.
    """
    path = os.path.join(script_dir(), auth_file)
    if not force:
        try:
            with open(path, 'r') as f:
                auth = json.load(f)
            tok = auth.get('token', '')
            exp = auth.get('expires', 0)
            if tok and (not exp or time.time() < exp - 3600):
                session.cookies.set("token", tok, domain="www.digiturkplay.com")
                return tok
            print("Cached session expired, re-authenticating", file=sys.stderr)
        except Exception as e:
            print(f"No cached session ({e}), logging in", file=sys.stderr)

    username = user or config.get('username', '')
    pwd = password or config.get('password', '')
    if not username or not pwd:
        print("Error: no credentials (use account=user:pass or config_digiturk.json)",
              file=sys.stderr)
        sys.exit(1)

    # a rejected cookie must not be sent along with the login request
    globals()['session'] = new_session()
    tok = do_login(username, pwd)
    save_auth(tok)
    return tok


def get_channels():
    """Flatten live-tv-summary into a list of channel dicts."""
    j = svc({"path": "/api/v1/content/live-tv-summary"})
    if looks_unauthenticated(j) or not (j.get('data') or {}).get('items'):
        reauth()
        j = svc({"path": "/api/v1/content/live-tv-summary"})
    out, seen = [], set()
    for group in (j.get('data') or {}).get('items') or []:
        for ch in ((group.get('data') or {}).get('items')) or []:
            slug = (ch.get('urlSlug') or '').strip()
            name = (ch.get('name') or '').strip()
            if not slug or not name or slug in seen:
                continue
            seen.add(slug)
            out.append({
                'slug': slug,
                'name': name,
                'no': ch.get('channelNo') or 0,
                'logo': ch.get('logo') or '',
                'genre': ch.get('genre') or '',
                'contentId': ch.get('channelContentId') or '',
            })
    return out


AUTH_ERRORS = ("LOGIN_INV_SES_KEY", "UNAUTHORIZED", "TOKEN", "SESSION")


def looks_unauthenticated(j):
    code = ((j or {}).get('message') or {}).get('code') or ''
    return any(e in str(code).upper() for e in AUTH_ERRORS)


def reauth():
    """Force a fresh login (cached cookie was rejected)."""
    globals()['session'] = new_session()
    tok = do_login(user or config.get('username', ''),
                   password or config.get('password', ''))
    save_auth(tok)
    return tok


def cache_path(slug):
    d = os.path.join(script_dir(), "cache_digiturk")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return os.path.join(d, slug.replace('/', '_') + ".json")


def url_expiry(url):
    """Read exp=... out of the CDN token so we know how long the url stays valid."""
    m = re.search(r"[~?&]exp=(\d+)", url)
    return int(m.group(1)) if m else 0


def cached_play_url(slug):
    """Return a still-valid previously issued url for this channel, if any.

    O11 re-runs this script every time it refreshes a live manifest. Digiturk
    hands out a DIFFERENT CDN host (fastly / akamaized / ercdn) and a fresh
    token on each play call, and switching host mid-stream moves the live edge,
    which O11 reports as "too many new fragments" before restarting the channel.
    Pinning the url until its token is nearly expired keeps one edge for the
    life of the token, and also spares the account a play call per refresh.
    """
    try:
        with open(cache_path(slug), 'r') as f:
            c = json.load(f)
        url, exp = c.get('url', ''), c.get('exp', 0)
        if url and exp and time.time() < exp - CDN_URL_RENEW_MARGIN:
            return url
    except Exception:
        pass
    return ''


def store_play_url(slug, url):
    exp = url_expiry(url)
    if not exp:
        return
    try:
        with open(cache_path(slug), 'w') as f:
            json.dump({'url': url, 'exp': exp}, f)
    except Exception:
        pass


def get_play(slug, allow_cache=True):
    if allow_cache:
        cached = cached_play_url(slug)
        if cached:
            print(f"reusing pinned url for {slug}", file=sys.stderr)
            return cached, {'streamFormatType': '3' if cached.split('?')[0].endswith('.m3u8')
                            else '1', 'cached': True}
    payload = {"path": "/api/v1/play/play", "method": "POST",
               "body": {"playRequestType": PLAY_REQUEST_TYPE_CHANNEL,
                        "channelUrlSlug": slug}}
    ref = f"/en/izle/canli-tv/{slug}"
    j = svc(payload, referer=ref)
    if looks_unauthenticated(j):
        print("session rejected, re-authenticating", file=sys.stderr)
        reauth()
        j = svc(payload, referer=ref)
    data = (j or {}).get('data') or {}
    url = data.get('playUrl') or ''
    if not url:
        msg = ((j or {}).get('message') or {}).get('code') or ''
        errs = (j or {}).get('errors') or []
        if not msg and errs:
            msg = errs[0].get('description', '')
        raise Exception(f"no playUrl for '{slug}': {msg or json.dumps(j)[:120]}")
    store_play_url(slug, url)
    return url, data


def do_action():
    if action == "login":
        get_auth()
        print("success")
        sys.exit(0)

    elif action == "channels":
        get_auth()
        channels = get_channels()
        if not channels:
            print("Error: no channels returned", file=sys.stderr)
            sys.exit(1)

        output = {'Channels': []}
        for ch in channels:
            slug = ch['slug']
            # O11 rejects '_' in a stream id, so the panel id is sanitised while
            # the script keeps being called with the real Digiturk slug.
            sid = slug.replace('_', '-')
            entry = {
                'Name': ch['name'],
                'Id': sid,
                'EpgId': sid,
                'LogoUrl': ch['logo'],
                'Category': ch['genre'],
                'SortId': ch['no'],
                'Mode': 'live',
                'SessionManifest': True,
                'ScriptParams': f"id={slug}",
                'ManifestScript': f"Digiturk id={slug}",
                'UseCdm': False,
                'Video': 'best',
                'OnDemand': True,
                'SpeedUp': True,
            }
            if USE_CDN_PROXY and slug in HLS_SLUGS:
                # served from the local CDN proxy -> must NOT go through the tunnel
                entry.update({
                    'NetworkOverride': True,
                    'ManifestNetwork': 'none', 'ManifestProxy': '',
                    'MediaNetwork': 'none', 'MediaProxy': '',
                })
            output['Channels'].append(entry)
        print(json.dumps(output, indent=2, ensure_ascii=False))
        sys.exit(0)

    elif action == "manifest":
        if not id:
            print("Error: missing channel id (id=<urlSlug>)", file=sys.stderr)
            sys.exit(1)
        get_auth()
        try:
            url, data = get_play(id)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        print(f"drmType={data.get('streamDrmType')} format={data.get('streamFormatType')} "
              f"cdn={data.get('cdnProvider')}", file=sys.stderr)

        # DASH (the large majority of channels): hand O11 the ORIGINAL CDN url,
        # same as the Magenta MK provider. The tunnel is applied at the network
        # layer (provider Network=proxy + ScriptProxy), not by rewriting the url.
        #
        # HLS (the beIN/sports feeds) cannot be given the original url: Digiturk
        # serves them with the Akamai session token as a PATH segment that
        # contains "/" (acl=/*). O11 derives a local manifest filename from that
        # path and the write fails -
        #   open /opt/o11/manifests/digiturk/hdntl=iat=...~acl=/*~id=... : no such file
        # - so the channel never starts. Creating the directory does not help; the
        # slash inside the token is the problem. Routing just those feeds through
        # the local CDN proxy rewrites the playlist into urls O11 can handle.
        # This is a workaround for an O11 limitation, not a preference.
        #
        # On an installation whose OWN ip can reach the sports CDN (a machine in
        # a country Digiturk serves), none of that applies: the token is minted
        # for that ip, so the original url just works. Set
        #   "use_cdn_proxy": false
        # in config_digiturk.json and every channel gets the original link.
        is_hls = (str(data.get('streamFormatType')) == '3'
                  or url.split('?')[0].endswith('.m3u8'))
        out_url = url
        if is_hls and USE_CDN_PROXY:
            if url.startswith("https://"):
                out_url = f"http://127.0.0.1:{CDN_PROXY_PORT}/r/" + url[len("https://"):]
            elif url.startswith("http://"):
                out_url = f"http://127.0.0.1:{CDN_PROXY_PORT}/r/" + url[len("http://"):]

        output = {
            "Cdn": [{"Name": "default", "ManifestUrl": out_url}],
            "ManifestUrl": out_url,
            "ExtraUrlParam": "",
            "Headers": {
                "Manifest": {"User-Agent": USER_AGENT},
                "Media": {"User-Agent": USER_AGENT},
            },
        }
        print(json.dumps(output))
        sys.exit(0)

    else:
        print(f"Unsupported action: {action}", file=sys.stderr)
        print("Supported: login, channels, manifest", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    if not action:
        print("Error: no action specified", file=sys.stderr)
        print("Usage: Digiturk.py action=channels", file=sys.stderr)
        print("       Digiturk.py action=manifest id=trt-1", file=sys.stderr)
        sys.exit(1)
    do_action()
