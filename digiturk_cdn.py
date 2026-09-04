#!/usr/bin/python3
"""
Digiturk HLS CDN proxy (isolated, port 9192).

Why it exists: Digiturk's beIN/sports channels are Akamai HLS whose master
playlist 302-redirects and whose tokens are bound to the tunnel egress IP.
O11 follows those redirects on a DIRECT connection (bypassing the provider
proxy), so the CDN 403s. This proxy forces EVERY hop - master, redirect,
variant playlist, segment - through the North Macedonia tunnel, and rewrites
HLS playlists so child URLs keep coming back here.

  http://127.0.0.1:9192/r/HOSTNAME/PATH?query  ->  https://HOSTNAME/PATH?query  (via tunnel)

Completely separate from cdn_serial_proxy.py (9191, used by the live channels).
"""
import http.server
import urllib.request
import urllib.parse
import ssl
import threading
import time
import sys
import os
import signal
import re

PORT = 9192
TUNNEL = "http://127.0.0.1:8888"
LOCK = threading.Semaphore(16)

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def build_opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({'http': TUNNEL, 'https': TUNNEL}),
        urllib.request.HTTPSHandler(context=ssl_ctx))


def is_playlist(url):
    return ".m3u8" in url.split("?")[0]


# --- master-playlist cache -------------------------------------------------
# Every fetch of a master playlist comes back with a NEWLY minted hdntl token,
# so the variant urls inside it change on each poll. O11 re-polls the master via
# the provider script, sees a different variant url, cannot line the timeline up
# with what it already has, and reports "too many new fragments" before
# restarting the channel. Serving the same rewritten master for a short window
# keeps one variant url alive so the live playlist simply advances.
# Only MASTER playlists are cached - the media playlist must stay live.
MASTER_TTL = 60
_master_cache = {}
_master_lock = threading.Lock()


def master_cache_get(key):
    with _master_lock:
        hit = _master_cache.get(key)
        if hit and (time.time() - hit[0]) < MASTER_TTL:
            return hit[1], hit[2]
    return None, None


def master_cache_put(key, data, ct):
    with _master_lock:
        _master_cache[key] = (time.time(), data, ct)
        if len(_master_cache) > 128:
            for k in sorted(_master_cache, key=lambda k: _master_cache[k][0])[:64]:
                _master_cache.pop(k, None)


def rewrite_hls(text, proxy_host_base, cdn_dir):
    """Playlists keep coming back through this proxy; MEDIA segments are pointed
    straight at the CDN.

    Only the playlists actually need rewriting - they are small text files, and
    O11 cannot fetch them directly because Digiturk's token sits in the path and
    contains a slash. The segments are the bandwidth, and pushing multi-Mbps
    video through this single Python process made channels buffer and freeze
    ("Slow: N" in the panel). O11 fetches the segments itself, over its own
    media transport (the tunnel), which is what it is good at.
    """
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            out.append(line)
            continue
        if s.startswith("#"):
            if 'URI="' in s:
                def repl(m):
                    uri = m.group(1)
                    # keys/maps are small: keep them on the proxy like playlists
                    return 'URI="' + to_proxy(uri, proxy_host_base, cdn_dir) + '"'
                s = re.sub(r'URI="([^"]*)"', repl, s)
            out.append(s)
        elif is_playlist(s):
            out.append(to_proxy(s, proxy_host_base, cdn_dir))
        else:
            out.append(to_origin(s, proxy_host_base, cdn_dir))
    return "\n".join(out)


def to_origin(url, host, cdn_dir):
    """Absolute url on the real CDN - no proxy hop."""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return "https://%s%s" % (host, url)
    return "https://%s%s%s" % (host, cdn_dir, url)


def to_proxy(url, proxy_host_base, cdn_dir):
    """Map a CDN url (absolute or relative) to a local /r/HOST/PATH url."""
    if url.startswith("http://"):
        return "http://127.0.0.1:%d/r/%s" % (PORT, url[7:])
    if url.startswith("https://"):
        return "http://127.0.0.1:%d/r/%s" % (PORT, url[8:])
    if url.startswith("/"):
        # absolute path on the same host
        host = proxy_host_base
        return "http://127.0.0.1:%d/r/%s%s" % (PORT, host, url)
    # relative to the current playlist directory
    return proxy_host_base_join(proxy_host_base, cdn_dir, url)


def proxy_host_base_join(host, cdn_dir, rel):
    return "http://127.0.0.1:%d/r/%s%s%s" % (PORT, host, cdn_dir, rel)


class Proxy(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if not self.path.startswith('/r/'):
            self.send_error(404, "use /r/HOST/PATH")
            return
        rest = self.path[3:]
        slash = rest.find('/')
        if slash < 0:
            self.send_error(400, "missing path")
            return
        host = rest[:slash]
        path = rest[slash:]
        real = "https://" + host + path

        # a cached master playlist keeps the variant url stable between polls
        cached, cct = master_cache_get(real.split("?")[0])
        if cached is not None:
            self.send_response(200)
            self.send_header('Content-Type', cct)
            self.send_header('Content-Length', str(len(cached)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(cached)
            return

        req = urllib.request.Request(real, headers={"User-Agent": UA})
        LOCK.acquire()
        try:
            resp = build_opener().open(req, timeout=30)
            data = resp.read()
            ct = resp.headers.get('Content-Type', 'application/octet-stream')
            final = resp.geturl()  # after any redirects (still via tunnel)

            bare = path.split("?")[0]
            if bare.endswith('.m3u8') or 'mpegurl' in ct.lower():
                # rewrite against the FINAL url's host + directory
                fu = urllib.parse.urlsplit(final)
                fhost = fu.netloc
                fdir = fu.path.rsplit("/", 1)[0] + "/"
                text = data.decode('utf-8', errors='replace')
                data = rewrite_hls(text, fhost, fdir).encode('utf-8')
                ct = 'application/vnd.apple.mpegurl'
                # a MASTER lists other playlists; a media playlist lists segments
                is_master = any(is_playlist(l.strip()) for l in text.split("\n")
                                if l.strip() and not l.startswith("#"))
                if is_master:
                    master_cache_put(real.split("?")[0], data, ct)

            elif bare.endswith('.mpd') or 'dash+xml' in ct.lower():
                # DASH: the CDN 302-redirects to a path carrying an hdntl token
                # prefix, and segments resolve relative to THAT. Since we hide the
                # redirect from O11, pin an absolute BaseURL pointing back at this
                # proxy with the final (post-redirect) directory baked in.
                fu = urllib.parse.urlsplit(final)
                fdir = fu.path.rsplit("/", 1)[0] + "/"
                text = data.decode('utf-8', errors='replace')
                m = re.search(r"<BaseURL>([^<]*)</BaseURL>", text)
                rel = (m.group(1) if m else "")
                if rel.startswith("http://") or rel.startswith("https://"):
                    absolute = to_proxy(rel, fu.netloc, fdir)
                else:
                    absolute = "http://127.0.0.1:%d/r/%s%s%s" % (PORT, fu.netloc, fdir, rel)
                if m:
                    text = text.replace(m.group(0), "<BaseURL>%s</BaseURL>" % absolute, 1)
                else:
                    text = re.sub(r"(<Period\b[^>]*>)",
                                  r"\1<BaseURL>%s</BaseURL>" % absolute, text, count=1)
                data = text.encode('utf-8')
                ct = 'application/dash+xml'

            self.send_response(200)
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header('Content-Length', '0')
            self.send_header('Connection', 'close')
            self.end_headers()
        except Exception as e:
            self.send_response(502)
            self.send_header('Content-Length', '0')
            self.send_header('Connection', 'close')
            self.end_headers()
            sys.stderr.write("proxy err: %s\n" % str(e))
            sys.stderr.flush()
        finally:
            LOCK.release()

    def log_message(self, *a):
        pass


class ThreadedServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def run():
    srv = ThreadedServer(('127.0.0.1', PORT), Proxy)
    print("digiturk CDN proxy on 127.0.0.1:%d" % PORT)
    sys.stdout.flush()
    srv.serve_forever()


if __name__ == '__main__':
    try:
        with open('/tmp/digiturk_cdn.pid') as f:
            os.kill(int(f.read().strip()), signal.SIGTERM)
        import time
        time.sleep(1)
    except Exception:
        pass
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)
    sys.stdin = open(os.devnull, 'r')
    sys.stdout = open('/tmp/digiturk_cdn.log', 'a')
    sys.stderr = sys.stdout
    with open('/tmp/digiturk_cdn.pid', 'w') as f:
        f.write(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    run()
