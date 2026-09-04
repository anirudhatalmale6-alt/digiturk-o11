# Digiturk provider for O11 PRO

Adds Digiturk Play (digiturkplay.com, the "Yurt Dışı" international package) to O11
as a provider: channel list, live manifests, ~85 channels including beIN Sports.

The streams are **not** DRM encrypted, so no CDM / Widevine setup is needed.

## Files

| file | what it is |
|---|---|
| `Digiturk.py` | the provider script (this is the only required file) |
| `config_digiturk.json` | your login + options |
| `digiturk_cdn.py` | optional helper, **only** for a server that cannot reach the sports CDN |

## Install

1. Copy `Digiturk.py` and `config_digiturk.json` into your O11 `scripts` folder
   (next to the other provider scripts).

2. Install the one dependency:

       pip install curl_cffi

3. Put your Digiturk login into `config_digiturk.json`:

       {
         "username": "your@email.com",
         "password": "your password",
         "proxy": "",
         "use_cdn_proxy": false
       }

4. Test it before touching O11:

       python Digiturk.py action=login
       python Digiturk.py action=channels
       python Digiturk.py action=manifest id=trt-1

   `action=login` should print `success`, and `action=manifest` should print a
   link starting with `https://dt-live-int-...dtvcdns.com/...`.

5. In O11: add a provider, set its **Script** to `Digiturk`, then run
   **Update Linear** to import the channels.

## The two options that matter

### `proxy`

Where the script talks to digiturkplay.com from. Leave it empty (`""`) if the
machine is in a country Digiturk serves. Set it (e.g.
`"http://127.0.0.1:8888"`) if you need to go out through a tunnel.

Important: the CDN link Digiturk issues is **locked to the IP that asked for
it**. So whatever fetches the video afterwards must come from that same IP.

### `use_cdn_proxy`

Leave this **`true`**. The normal channels always get the original CDN link; the
setting only affects the sports (HLS) ones, and those need the helper on *any*
machine — see below.

### `cdn_proxy_upstream`

Where the helper fetches the CDN from. Empty (`""`) means straight out from this
machine — correct when this machine can reach the CDN itself. Set it to a tunnel
(e.g. `"http://127.0.0.1:8888"`) only on a server whose own IP the sports CDN
refuses.

## Why the sports channels need the helper (even with the original domain)

It is not about IP or geo blocking. O11 **does not resolve relative URLs inside
an HLS playlist**. Digiturk's sports playlists list their segments by bare
filename:

    beinsports01_int-audio_tur=128000-video=6000000-465765210.ts

O11 passes that straight to its HTTP client and fails with
`unsupported protocol scheme ""`. Digiturk also puts its access token in the URL
*path*, and the token contains a slash (`acl=/*`), which breaks the filename O11
derives when it saves a manifest.

The helper solves both: it serves the playlists and rewrites the segment lines
into absolute `https://…dtvcdns.com/…` URLs. **The video itself never goes
through the helper** — O11 downloads the segments straight from the CDN, so the
helper stays at ~1-3% CPU. Sending the video through it is what causes
`Slow: N` and freezing.

## Channels

* Most channels (TRT 1, ATV, Kanal D, Star TV, A Spor, beIN Movies …) are DASH.
* The sports feeds (beIN SPORTS 1 / 2 / 90+1 / MAX 2, TRT Spor) are HLS.
* `Show TV` is not part of this package and will always fail — that is normal.

## If a channel keeps restarting

O11 restarts a channel when the playlist jumps too far ahead ("too many new
fragments"). On the provider, these settings make it tolerant of a slow link:

    NbAnnouncedFragments   24
    PlaylistDuration       30
    StallDetectTimeout     90
    IgnoreOldestFragment   true
    NoWaitFullPlaylist     true
    MaxConcurrentScript    6

The script also pins each channel's link until its token is close to expiring
(`cache_digiturk/`). Without that, every manifest refresh asks Digiturk for a
new link, gets a different CDN server each time, and the channel restarts.

## Running the helper (needed for the sports channels)

Start it before the channels, and leave it running:

    python digiturk_cdn.py

On Windows it stays in the console window — keep that window open. On Linux it
goes to the background by itself (use `--foreground` if you'd rather watch it).
It listens on `127.0.0.1:9192`.

Proof it is needed: with the original link, O11 logs

    HTTP Get error [Get "beinsports01_int-audio_tur=128000-video=6000000-465765776.ts":
    unsupported protocol scheme ""]

because the playlist names its segments without an address and O11 will not
build one.

**After changing any of this, delete the channel in O11 and add it again.**
Editing is not enough — O11 stores the last address it was given and keeps
reusing it, so a channel can keep failing on a link you already replaced.
