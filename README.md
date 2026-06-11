# lgtv-arc-watchdog

**Stop your LG TV from switching sound output back to TV Speaker.**

A tiny Python watchdog that polls your LG webOS TV over the network and flips
the sound output back to **HDMI ARC** (or eARC, optical, a soundbar — your
choice) whenever the TV reverts it to the built-in speakers. No Home
Assistant, no Docker, no soldering — one script, one systemd service.

## The problem

If you've ever searched any of these, you're in the right place:

- *LG TV sound output keeps changing to TV Speaker on power on*
- *LG OLED won't stay on HDMI ARC / eARC after turning off*
- *LG C5 / C4 / C3 / C2 / G5 / G4 / B4 sound output resets every time*
- *LG TV CEC / SIMPLINK handshake fails with AV receiver (Onkyo, Denon,
  Yamaha, Marantz, Pioneer, Sony, Anthem...)*
- *LG TV forgets soundbar / optical output setting*

Many LG webOS TVs lose the CEC (SIMPLINK) handshake with an older receiver or
soundbar on some power-ons and silently fall back to `tv_speaker`. There is no
setting on the TV to pin the output. Grabbing the remote and digging through
Settings → Sound → Sound Out every time gets old fast.

## The fix

LG webOS TVs expose a local network control API (WebSocket on ports
3000/3001). This watchdog uses [bscpylgtv](https://github.com/chros73/bscpylgtv)
to check the TV's current sound output every 15 seconds and correct it when
it's wrong:

- TV unreachable / off / mid-boot → silently retry next loop
- Output already correct → do nothing, log nothing
- Output is `tv_speaker` → set it to `external_arc` and log one line

It runs happily on anything always-on: a Raspberry Pi, a Proxmox LXC, a NAS,
an old laptop.

## Install

```sh
sudo mkdir -p /opt/tv-audio-guard && cd /opt/tv-audio-guard
sudo python3 -m venv venv
sudo venv/bin/pip install bscpylgtv
sudo curl -fsSLO https://raw.githubusercontent.com/fungiblemoose/lgtv-arc-watchdog/main/guard.py
```

Give your TV a DHCP reservation so its IP never changes.

### 1. Pair with the TV (one time)

Turn the TV fully on (actual picture, not screensaver), then:

```sh
sudo venv/bin/python3 guard.py <TV_IP> --pair
```

A permission popup appears **on the TV** — accept it with the remote within
~30 seconds. The client key is stored in `keys.sqlite` next to the script, so
the service never needs a popup again.

No popup? Make sure the TV is fully awake and that **Settings → General →
Devices → External Devices → Mobile Device Control** (naming varies by year)
allows network control / LG ThinQ connections.

### 2. Run it as a service

```sh
sudo curl -fsSL -o /etc/systemd/system/tv-audio-guard.service \
  https://raw.githubusercontent.com/fungiblemoose/lgtv-arc-watchdog/main/tv-audio-guard.service
# edit the TV IP in the unit file, then:
sudo systemctl daemon-reload
sudo systemctl enable --now tv-audio-guard
journalctl -u tv-audio-guard -f
```

Test it: flip Sound Out to TV Speaker with the remote. Within ~20 seconds the
journal shows:

```
Corrected sound output: tv_speaker -> external_arc
```

## Not using ARC?

`--target` accepts any output your TV supports — common values are
`external_arc`, `external_optical`, `external_speaker`, `bt_soundbar`,
`lineout`, `headphone`. If the TV rejects the target, the watchdog logs the
TV's own list of valid `soundOutput` values and stops, so you can pick the
right one. There's also `--wrong` (what to correct away from), `--interval`,
and `--key-file`.

## How it behaves

- Logs **only** on pairing and on corrections — journald stays clean
- Swallows every connection error quietly (TV off is normal, not an error)
- `Restart=always` + the internal loop means it survives reboots, TV firmware
  updates, and network blips

## Credits

All the heavy lifting is done by
[bscpylgtv](https://github.com/chros73/bscpylgtv). This repo is just the
~100-line watchdog and the systemd glue around it.
