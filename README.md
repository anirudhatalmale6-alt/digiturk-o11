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

* `false` — every channel gets the **original** CDN link
  (`https://dt-live-int-bytpls.dtvcdns.com/...`). Use this whenever this machine
  can play the sports channels itself. This is the setup you want at home.
* `true` — the sports/HLS channels are routed through the local helper
  (`digiturk_cdn.py`, port 9192). Only needed on a machine whose own IP is
  refused by the sports CDN.

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

## Only if you need the helper (`use_cdn_proxy: true`)

Run it once, in the background, before starting channels:

    python digiturk_cdn.py

It forwards playlists through the tunnel and rewrites them so the video segments
still come straight from the CDN. It listens on `127.0.0.1:9192`.

In O11, those sports channels then need, per stream:
Manifest network = none (no proxy), Media network = proxy (your tunnel).
