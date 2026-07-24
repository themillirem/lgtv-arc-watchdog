#!/usr/bin/env python3
"""LG TV sound output watchdog.

Forces an LG webOS TV's sound output back to a target (e.g. HDMI ARC) whenever
the TV reverts it to the built-in speakers — a common symptom of a flaky CEC
handshake with an AV receiver or soundbar on power-on.

Usage:
    guard.py <tv-ip> --pair             # one-shot: trigger + verify TV pairing
    guard.py <tv-ip> --dump             # one-shot: read-only diagnostic dump
    guard.py <tv-ip>                    # run the watchdog loop (for systemd)
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

from bscpylgtv import WebOsClient

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Read-only endpoints probed by --dump. Each is queried independently so one
# unsupported/failing endpoint doesn't stop the rest from being collected.
DUMP_REQUESTS = [
    ("audio/getStatus", None),
    ("com.webos.service.eim/getAllInputSockets", None),
    ("tv/getExternalInputList", None),
]


def log(msg):
    print(msg, flush=True)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def make_client(args):
    return await WebOsClient.create(
        args.ip, ping_interval=None, states=[], key_file_path=args.key_file
    )


async def pair(args):
    client = await make_client(args)
    if client.client_key is not None:
        log("Already paired (key found in %s)." % args.key_file)
    else:
        log("Not yet paired. ACCEPT THE POPUP ON THE TV within ~30 seconds...")
    await asyncio.wait_for(client.connect(), timeout=90)
    out = await client.get_sound_output()
    log("Connected. Key stored in %s" % args.key_file)
    log("Current sound output: %r" % out)
    await client.disconnect()


async def _dump_call(client, label, coro):
    try:
        result = await coro
    except Exception as e:
        log("DUMP %s %s: ERROR %r" % (_now(), label, e))
        return
    log("DUMP %s %s: %r" % (_now(), label, result))


async def _dump_snapshot(client, label_prefix=""):
    """Run every read-only probe against an already-connected client."""
    await _dump_call(client, label_prefix + "get_power_state", client.get_power_state())
    await _dump_call(client, label_prefix + "get_sound_output", client.get_sound_output())
    for uri, payload in DUMP_REQUESTS:
        await _dump_call(client, label_prefix + uri, client.request(uri, payload))


async def dump(args):
    """Read-only diagnostic dump: connect, print raw state, disconnect, exit.

    Never calls change_sound_output.
    """
    client = await make_client(args)
    try:
        await asyncio.wait_for(client.connect(), timeout=90)
    except Exception as e:
        log("DUMP %s ERROR: could not connect: %r" % (_now(), e))
        return

    try:
        await _dump_snapshot(client)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def get_true_sound_output(client):
    """The plain get_sound_output() call can report the intended value even
    while the TV has silently reverted to a different one (a flaky CEC
    handshake with the AV receiver on power-on can leave the TV reporting
    'external_arc' while still routing audio through the panel speakers).
    audio/getStatus reflects the true routing, so it's used as the single
    source of truth here instead of get_sound_output().
    """
    status = await client.request("audio/getStatus", None)
    if not isinstance(status, dict):
        return None
    volume_status = status.get("volumeStatus")
    return volume_status.get("soundOutput") if isinstance(volume_status, dict) else None


async def check_once(args, state):
    client = await make_client(args)
    newly_pairing = client.client_key is None
    try:
        await client.connect()
        if newly_pairing:
            log("Paired with TV; client key stored in %s" % args.key_file)

        out = await get_true_sound_output(client)
        if out == args.wrong and not state["set_disabled"]:
            try:
                await client.change_sound_output(args.target)
                log("Corrected sound output: %s -> %s" % (args.wrong, args.target))
            except Exception as e:
                # If the TV rejects the target, report its valid options and
                # stop attempting changes until the service is restarted.
                state["set_disabled"] = True
                log("ERROR: change_sound_output(%r) failed: %r" % (args.target, e))
                try:
                    opts = await client.request(
                        "settings/getSystemSettingValues",
                        {"category": "sound", "key": "soundOutput"},
                    )
                    log("TV-reported valid soundOutput values: %r" % (opts,))
                except Exception as e2:
                    log("Could not query valid soundOutput values: %r" % (e2,))
                log("No further changes will be attempted until service restart.")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def watch(args):
    state = {"set_disabled": False}
    while True:
        try:
            await check_once(args, state)
        except Exception:
            # TV off, mid-boot, unreachable: stay quiet, retry next loop.
            pass
        await asyncio.sleep(args.interval)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("ip", help="TV IP address (give it a DHCP reservation)")
    p.add_argument("--target", default="external_arc",
                   help="desired sound output (default: external_arc)")
    p.add_argument("--wrong", default="tv_speaker",
                   help="output to correct away from (default: tv_speaker)")
    p.add_argument("--interval", type=int, default=15,
                   help="seconds between checks (default: 15)")
    p.add_argument("--key-file", default=os.path.join(SCRIPT_DIR, "keys.sqlite"),
                   help="pairing key storage path (default: keys.sqlite next to script)")
    p.add_argument("--pair", action="store_true",
                   help="one-shot interactive pairing, then exit")
    p.add_argument("--dump", action="store_true",
                   help="one-shot read-only diagnostic dump, then exit")
    args = p.parse_args()

    try:
        if args.pair:
            asyncio.run(pair(args))
        elif args.dump:
            asyncio.run(dump(args))
        else:
            asyncio.run(watch(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
