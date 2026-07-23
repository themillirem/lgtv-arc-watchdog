#!/usr/bin/env python3
"""LG TV sound output watchdog.

Forces an LG webOS TV's sound output back to a target (e.g. HDMI ARC) whenever
the TV reverts it to the built-in speakers — a common symptom of a flaky CEC
handshake with an AV receiver or soundbar on power-on.

Usage:
    guard.py <tv-ip> --pair             # one-shot: trigger + verify TV pairing
    guard.py <tv-ip> --dump             # one-shot: read-only diagnostic dump
    guard.py <tv-ip> --capture          # watchdog loop + auto-dump burst on
                                         # each power-on edge (for catching
                                         # intermittent failures unattended)
    guard.py <tv-ip> --bounce           # watchdog loop + force sound output
                                         # away from --target and back on each
                                         # power-on edge (fixes the "reports
                                         # ARC but plays through TV speakers"
                                         # desync that reported state can't
                                         # detect)
    guard.py <tv-ip>                    # run the watchdog loop (for systemd)
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

from bscpylgtv import WebOsClient

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Read-only endpoints probed by --dump / --capture. Each is queried
# independently so one unsupported/failing endpoint doesn't stop the rest
# from being collected.
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

    Never calls change_sound_output. Intended to be run once against a
    healthy TV and once against a TV stuck in the "reports ARC but plays
    through TV speakers" state, so the two dumps can be diffed by hand to
    find a field that reliably distinguishes them.
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


async def capture_burst(args):
    """Fire a series of full diagnostic snapshots at increasing delays after
    a power-on edge, to catch how (or whether) CEC/audio state converges.
    Each snapshot uses its own connection; a failed snapshot doesn't cancel
    the rest of the burst. Never calls change_sound_output.
    """
    log("CAPTURE %s burst starting (delays=%s)" % (_now(), args.capture_delays))
    prev_delay = 0
    for d in args.capture_delays:
        gap = d - prev_delay
        if gap > 0:
            await asyncio.sleep(gap)
        prev_delay = d
        client = await make_client(args)
        try:
            await asyncio.wait_for(client.connect(), timeout=30)
        except Exception as e:
            log("CAPTURE %s T+%ss: could not connect: %r" % (_now(), d, e))
            continue
        try:
            await _dump_snapshot(client, label_prefix="T+%ss " % d)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
    log("CAPTURE %s burst complete" % _now())


async def bounce_after_edge(args):
    """One-shot: after a settle delay following a power-on edge, force sound
    output away from --target and back, mirroring the fix Scott performs by
    hand with the remote.

    Live A/B testing on 2026-07-22 (one --dump while the TV was confirmed
    stuck reporting external_arc but still playing through the panel
    speakers, one immediately after the manual toggle fixed it) found the two
    dumps identical in every field — get_power_state, get_sound_output,
    audio/getStatus, both SIMPLINK cecPower values, HDMI signal flags. None
    of the probed endpoints can distinguish stuck from healthy audio routing,
    so detect-and-correct isn't possible here; this blind bounce is the
    fix, not a stopgap.
    """
    if args.bounce_delay > 0:
        await asyncio.sleep(args.bounce_delay)
    client = await make_client(args)
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
    except Exception as e:
        log("BOUNCE %s could not connect: %r" % (_now(), e))
        return
    try:
        await client.change_sound_output(args.wrong)
        await asyncio.sleep(args.bounce_gap)
        await client.change_sound_output(args.target)
        log("BOUNCE %s forced %s -> %s -> %s" % (_now(), args.target, args.wrong, args.target))
    except Exception as e:
        log("BOUNCE %s ERROR: %r" % (_now(), e))
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def check_once(args, state):
    client = await make_client(args)
    newly_pairing = client.client_key is None
    try:
        await client.connect()
        if newly_pairing:
            log("Paired with TV; client key stored in %s" % args.key_file)

        if args.capture or args.bounce:
            await _check_power_on_edge(client, args, state)

        out = await client.get_sound_output()
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


async def _check_power_on_edge(client, args, state):
    """Detect a transition into the 'Active' power state and, on the rising
    edge, kick off a background capture burst and/or bounce (whichever of
    --capture/--bounce is enabled). Does not touch state["set_disabled"] or
    the sound-output correction path in check_once.
    """
    try:
        ps = await client.get_power_state()
        ps_state = ps.get("state") if isinstance(ps, dict) else ps
    except Exception:
        return

    if args.log_state:
        log("STATE %s power_state=%r" % (_now(), ps_state))

    have_prior = "last_power_state" in state
    was_active = state.get("last_power_state") == "Active"
    is_active = ps_state == "Active"

    # Only treat this as an edge once we have a prior observation — the very
    # first poll after (re)starting the watchdog just establishes a baseline,
    # even if the TV happens to already be Active, so a service restart
    # doesn't masquerade as a power-on event.
    if have_prior and is_active and not was_active:
        log("%s power-on edge detected (%r -> %r)" %
            (_now(), state.get("last_power_state"), ps_state))

        if args.capture:
            task = state.get("capture_task")
            if task is None or task.done():
                state["capture_task"] = asyncio.create_task(capture_burst(args))
            else:
                log("CAPTURE %s burst already in progress; skipping new one" % _now())

        if args.bounce:
            task = state.get("bounce_task")
            if task is None or task.done():
                state["bounce_task"] = asyncio.create_task(bounce_after_edge(args))
            else:
                log("BOUNCE %s already in progress; skipping new one" % _now())

    state["last_power_state"] = ps_state


async def watch(args):
    state = {"set_disabled": False}
    while True:
        try:
            await check_once(args, state)
        except Exception:
            # TV off, mid-boot, unreachable: stay quiet, retry next loop.
            pass
        await asyncio.sleep(args.interval)


def _parse_delays(s):
    try:
        delays = sorted(set(int(x) for x in s.split(",") if x.strip() != ""))
    except ValueError:
        raise argparse.ArgumentTypeError("--capture-delays must be a comma-separated list of integers")
    if not delays:
        raise argparse.ArgumentTypeError("--capture-delays must contain at least one value")
    return delays


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
    p.add_argument("--capture", action="store_true",
                   help="in watch mode, also fire a burst of read-only "
                        "diagnostic snapshots on each power-on edge "
                        "(standby/off -> Active), for catching intermittent "
                        "failures without having to reproduce them by hand")
    p.add_argument("--capture-delays", type=_parse_delays, default=_parse_delays("0,10,30,60,120"),
                   help="comma-separated seconds after a power-on edge to "
                        "snapshot (default: 0,10,30,60,120)")
    p.add_argument("--bounce", action="store_true",
                   help="in watch mode, on each power-on edge (standby/off "
                        "-> Active), force sound output away from --target "
                        "to --wrong and back after a settle delay. Fixes the "
                        "'reports ARC but plays through TV speakers' desync, "
                        "which reported state can't detect (see --bounce-delay "
                        "/ --bounce-gap)")
    p.add_argument("--bounce-delay", type=int, default=60,
                   help="seconds to wait after a power-on edge before "
                        "bouncing (default: 60 — an unvalidated starting "
                        "guess for how long the CEC handshake race takes to "
                        "settle; tune based on observed results)")
    p.add_argument("--bounce-gap", type=int, default=2,
                   help="seconds to hold --wrong before returning to "
                        "--target during a bounce (default: 2)")
    p.add_argument("--log-state", action="store_true",
                   help="with --capture/--bounce, log the raw power_state on "
                        "every poll (not just on detected edges) — a "
                        "temporary diagnostic aid for confirming what state "
                        "transitions the TV actually reports overnight")
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
