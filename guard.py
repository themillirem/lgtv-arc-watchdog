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
import argparse   # parses the command-line flags (--target, --pair, etc.)
import asyncio     # the whole script is built on asyncio because the TV is
                    # talked to over a WebSocket, and every call to it
                    # (connect, request, disconnect) is a network round-trip
                    # that has to be awaited rather than blocking
import os           # only used here to build the default keys.sqlite path
import socket       # raw sd_notify(3) implementation, see _sd_notify() below
import sys          # only used for sys.exit() on Ctrl-C
from datetime import datetime, timezone   # timestamps for log lines

# bscpylgtv is the third-party library that speaks LG's webOS remote-control
# protocol (the same protocol the official LG ThinQ app uses) over a local
# WebSocket connection to the TV on ports 3000/3001.
from bscpylgtv import WebOsClient

# Directory this script lives in -- used so the default key-file path
# ("keys.sqlite next to the script") still works no matter what directory
# you happen to run guard.py from.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Read-only endpoints probed by --dump. Each is queried independently so one
# unsupported/failing endpoint doesn't stop the rest from being collected.
# These are all "ssap://" URIs -- LG's internal RPC namespace -- rather than
# convenience methods on WebOsClient, because bscpylgtv doesn't wrap every
# possible call; client.request() lets us hit any URI directly.
DUMP_REQUESTS = [
    ("audio/getStatus", None),                        # the *true* audio routing state (see get_true_sound_output below)
    ("com.webos.service.eim/getAllInputSockets", None),  # 404s on this TV's firmware -- kept for completeness/other models
    ("tv/getExternalInputList", None),                 # per-HDMI-port CEC/SIMPLINK device info (connected receiver, its power state, etc.)
]


def log(msg):
    # A tiny wrapper instead of calling print() directly everywhere, mostly
    # so flush=True is guaranteed in one place -- without it, output can sit
    # buffered and never reach `journalctl -f` promptly when running under
    # systemd.
    print(msg, flush=True)


def _now():
    # UTC, second-precision ISO timestamp used to prefix every log line so
    # journalctl output (which has its own timestamp column too) is
    # unambiguous about timezone when logs get copy-pasted out of context.
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sd_notify(message):
    """Speak the sd_notify(3) protocol directly over the NOTIFY_SOCKET unix
    datagram socket systemd sets in the environment for Type=notify units.

    Reimplemented by hand instead of adding the separate `sdnotify` pip
    dependency, since the whole protocol is "write one line to one socket".
    A no-op when NOTIFY_SOCKET isn't set -- e.g. running guard.py by hand
    for --pair/--dump, or under an older unit file that hasn't been updated
    to Type=notify yet -- so this is always safe to call unconditionally.

    Used as a backstop, not the primary fix: on 2026-07-27 this service sat
    completely frozen for ~11 hours (a hung TV handshake with no timeout
    anywhere in the call chain -- see check_once() below) while `systemctl
    status` still happily reported it "active (running)" the whole time,
    because the process itself never crashed, it just deadlocked. The real
    fix is bounding every poll with timeouts so that can't happen again; this
    heartbeat exists so that if some *other*, not-yet-imagined hang manages
    to slip past those bounds, systemd's WatchdogSec= will still notice the
    missed heartbeats and force-restart the unit rather than trusting the
    process's own liveness forever.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr[0] == "@":
        addr = "\0" + addr[1:]  # abstract-namespace socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(message.encode())
    except OSError:
        pass  # a notify failure is never worth taking the watchdog down for


async def make_client(args):
    # Builds (but does not connect) a WebOsClient. ping_interval=None turns
    # off bscpylgtv's own keepalive pings -- we don't need them because we
    # open a brand-new short-lived connection on every poll instead of
    # holding one connection open for the process's whole lifetime.
    # states=[] means "don't auto-subscribe to any push-update channels";
    # we only ever do direct request/response calls, never subscriptions.
    return await WebOsClient.create(
        args.ip, ping_interval=None, states=[], key_file_path=args.key_file
    )


async def pair(args):
    # One-shot interactive pairing flow: guard.py <tv-ip> --pair
    #
    # The TV requires an explicit on-screen permission popup the *first*
    # time an unrecognized client connects. bscpylgtv handles sending the
    # pairing request; the human has to physically accept it on the TV with
    # the remote. Once accepted, the TV hands back a long-lived "client key"
    # which bscpylgtv writes into keys.sqlite -- after that, connect() just
    # presents the stored key and no popup is needed again.
    client = await make_client(args)
    if client.client_key is not None:
        # client_key is loaded straight from keys.sqlite by WebOsClient.create()
        # before we've even connected -- so we already know before dialing in
        # whether this is a first-time pairing or a routine reconnect.
        log("Already paired (key found in %s)." % args.key_file)
    else:
        log("Not yet paired. ACCEPT THE POPUP ON THE TV within ~30 seconds...")
    # Generous 90s timeout here specifically because a human has to notice
    # the on-screen popup and press a button on the remote -- far longer
    # than any of the other timeouts in this file, which are all pure
    # machine-to-machine calls.
    await asyncio.wait_for(client.connect(), timeout=90)
    out = await client.get_sound_output()
    log("Connected. Key stored in %s" % args.key_file)
    log("Current sound output: %r" % out)
    await client.disconnect()


async def _dump_call(client, label, coro):
    # Runs a single awaitable and logs whatever it returns -- or logs the
    # exception instead of letting it propagate. This is what lets --dump
    # probe several endpoints in a row and still print results for the ones
    # that succeeded even if one of them (e.g. the eim/getAllInputSockets
    # 404) fails outright.
    try:
        result = await coro
    except Exception as e:
        log("DUMP %s %s: ERROR %r" % (_now(), label, e))
        return
    log("DUMP %s %s: %r" % (_now(), label, result))


async def _dump_snapshot(client, label_prefix=""):
    """Run every read-only probe against an already-connected client."""
    # get_power_state/get_sound_output are convenience wrappers bscpylgtv
    # provides on top of the same request() mechanism used for DUMP_REQUESTS
    # below -- functionally no different, just nicer names for common calls.
    await _dump_call(client, label_prefix + "get_power_state", client.get_power_state())
    await _dump_call(client, label_prefix + "get_sound_output", client.get_sound_output())
    for uri, payload in DUMP_REQUESTS:
        await _dump_call(client, label_prefix + uri, client.request(uri, payload))


async def dump(args):
    """Read-only diagnostic dump: connect, print raw state, disconnect, exit.

    Never calls change_sound_output.
    """
    # This whole function only ever reads state -- useful for manually
    # inspecting exactly what the TV is reporting at a given moment (e.g.
    # while the "reports ARC but plays through TV speakers" bug is actively
    # happening), without any risk of the diagnostic run itself changing
    # anything.
    client = await make_client(args)
    try:
        await asyncio.wait_for(client.connect(), timeout=90)
    except Exception as e:
        log("DUMP %s ERROR: could not connect: %r" % (_now(), e))
        return

    try:
        await _dump_snapshot(client)
    finally:
        # Best-effort disconnect regardless of whether the dump succeeded --
        # we don't want a dump run to leave a dangling connection open.
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
    # Raw shape of a successful audio/getStatus response looks like:
    #   {
    #     "returnValue": True,
    #     "volumeStatus": {
    #       "soundOutput": "external_arc",   <- the field we actually want
    #       "volume": 57,
    #       "externalDeviceControl": True,
    #       ... several other fields ...
    #     },
    #     "callerId": "...",
    #     "mute": False,
    #     "volume": 57,
    #   }
    # soundOutput is nested one level down inside "volumeStatus" -- NOT at
    # the top level of the payload. (An earlier version of this function
    # read status.get("soundOutput") directly on the top-level dict, which
    # silently always returned None -- dict.get() doesn't raise on a missing
    # key, so there was no exception to notice, just a correction check that
    # could never fire. Confirmed live against a real TV before concluding
    # this was actually working.)
    status = await client.request("audio/getStatus", None)
    if not isinstance(status, dict):
        # Defensive: request() is documented to return a dict on success or
        # raise on failure, but we don't want a surprising response shape to
        # blow up the whole watch loop -- better to report "unknown" (None)
        # and let the caller just skip correcting this cycle.
        return None
    volume_status = status.get("volumeStatus")
    return volume_status.get("soundOutput") if isinstance(volume_status, dict) else None


async def check_once(args, state):
    # One full poll cycle: open a fresh connection, check the true sound
    # output, correct it if needed, close the connection. Called repeatedly
    # by watch() below, every args.interval seconds.
    #
    # A brand-new WebOsClient/connection is created every single call rather
    # than reusing one long-lived connection across the whole process
    # lifetime. That trades a little overhead (a fresh TCP + WebSocket
    # handshake each poll) for simplicity and resilience: if the TV reboots,
    # goes to standby, or drops the connection for any reason, there's no
    # stale connection object to detect and recover from -- the next poll
    # just tries fresh.
    client = await make_client(args)
    newly_pairing = client.client_key is None
    try:
        # bscpylgtv's connect() only bounds the *initial* WebSocket dial
        # (internally: up to 9 retries, ~2s timeout each, so ~20s worst
        # case) -- the handshake steps after that (send hello/registration,
        # await the TV's response) have no timeout at all anywhere in the
        # library. Caught live on 2026-07-27: the TV accepted the raw
        # connection but then never answered registration, so
        # `await client.connect()` hung forever with no exception ever
        # raised -- the watch() loop below never got control back, so it
        # never logged again, never slept, never retried, for ~11 hours,
        # until the service was manually restarted.
        #
        # Wrapping the call here bounds every poll cycle *and* -- just as
        # important -- still lets the `finally` block below run on timeout,
        # so `client.disconnect()` gets a chance to cancel the now-useless
        # connect_task instead of leaking it forever. Each such leaked
        # attempt is also what was driving the process's memory from a
        # normal ~20MB baseline up past 600MB before it finally froze solid.
        await asyncio.wait_for(client.connect(), timeout=args.connect_timeout)
        if newly_pairing:
            # This should only ever fire once, the very first time the
            # watchdog runs against a never-before-paired TV -- after that,
            # client_key is loaded from keys.sqlite on every subsequent call.
            log("Paired with TV; client key stored in %s" % args.key_file)

        out = await get_true_sound_output(client)
        if out == args.wrong and not state["set_disabled"]:
            # out == args.wrong (default "tv_speaker") specifically, not
            # "out != args.target" -- so if you've manually switched to some
            # other legitimate output (headphones, a different soundbar
            # mode, whatever), the watchdog leaves it alone. It only fights
            # back against the one specific bad value the TV falls back to
            # on its own.
            try:
                await client.change_sound_output(args.target)
                log("Corrected sound output: %s -> %s" % (args.wrong, args.target))
            except Exception as e:
                # If the TV rejects the target, report its valid options and
                # stop attempting changes until the service is restarted.
                #
                # This is a one-way latch: once change_sound_output fails
                # once, state["set_disabled"] stays True for the rest of
                # this process's life (state is a plain dict shared across
                # every check_once() call from the same watch() loop, so
                # this persists across polls, not just within one call).
                # The intent is to avoid hammering the TV with a change call
                # every 15 seconds forever if --target is simply wrong for
                # this TV model -- better to fail loud once, log the TV's
                # own list of valid values, and require a human to fix the
                # flag and restart the service.
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
        # Always try to close the connection cleanly, whether the poll
        # succeeded, found nothing to correct, or hit the error path above.
        # Swallowing the exception here specifically guards against
        # disconnect() itself failing (e.g. the connection was already lost)
        # -- that's not interesting enough to log on every poll.
        try:
            await client.disconnect()
        except Exception:
            pass


async def watch(args):
    # The main loop, used whenever guard.py is run without --pair or --dump
    # (i.e. the systemd service mode). state is a single dict created once
    # here and threaded through every check_once() call so
    # state["set_disabled"] can persist across polls.
    state = {"set_disabled": False}
    _sd_notify("READY=1")
    while True:
        try:
            # Second, outer bound around the *whole* poll cycle, on top of
            # check_once()'s own --connect-timeout around just the connect()
            # call. Belt-and-suspenders: if some other call in the chain
            # (request(), change_sound_output(), disconnect() itself) were
            # ever to hang the way connect() did on 2026-07-27, this is what
            # stops it from freezing the loop forever instead of the
            # --connect-timeout fix above.
            await asyncio.wait_for(check_once(args, state), timeout=args.poll_timeout)
        except asyncio.TimeoutError:
            # Distinct from the silent catch-all below on purpose: hitting
            # *this* timeout means something hung well past what
            # --connect-timeout already accounts for -- worth a loud log
            # line since it's the exact failure class that silently froze
            # the service for ~11 hours before anyone noticed.
            log("WARNING: poll cycle exceeded --poll-timeout (%ss); abandoning it and continuing." % args.poll_timeout)
        except Exception:
            # TV off, mid-boot, unreachable: stay quiet, retry next loop.
            #
            # This is deliberately a silent, blanket catch-all: the TV being
            # powered off or still booting is the *normal*, expected state
            # for large parts of the day, and logging a connection-refused
            # error every 15 seconds during those stretches would just be
            # noise that trains you to ignore the log. Genuine problems
            # (like a bad --target value) still get surfaced loudly via the
            # ERROR/set_disabled path inside check_once() above -- this
            # outer catch only ever swallows the "couldn't even connect"
            # class of failure.
            pass
        # Heartbeat for systemd's WatchdogSec= (see _sd_notify's docstring
        # for why this exists alongside, not instead of, the timeouts
        # above). Sent every cycle regardless of outcome -- success, a
        # normal quiet failure, or an abandoned-via-timeout poll are all
        # equally "the loop is still alive and making progress".
        _sd_notify("WATCHDOG=1")
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
    p.add_argument("--connect-timeout", type=int, default=30,
                   help="max seconds to wait for client.connect() per poll "
                        "(default: 30 -- comfortably above bscpylgtv's own "
                        "~20s worst-case internal retry loop, so this only "
                        "fires for a genuinely hung handshake, not a slow-"
                        "but-normal one)")
    p.add_argument("--poll-timeout", type=int, default=60,
                   help="max seconds for one whole poll cycle before it's "
                        "abandoned (default: 60 -- --connect-timeout plus "
                        "margin for the request()/change_sound_output()/"
                        "disconnect() calls that follow it)")
    p.add_argument("--key-file", default=os.path.join(SCRIPT_DIR, "keys.sqlite"),
                   help="pairing key storage path (default: keys.sqlite next to script)")
    p.add_argument("--pair", action="store_true",
                   help="one-shot interactive pairing, then exit")
    p.add_argument("--dump", action="store_true",
                   help="one-shot read-only diagnostic dump, then exit")
    args = p.parse_args()

    try:
        # Exactly one of these three modes runs per invocation -- --pair and
        # --dump are both one-shot (connect, do one thing, exit); leaving
        # both flags off is what systemd actually runs day to day, looping
        # forever via watch().
        if args.pair:
            asyncio.run(pair(args))
        elif args.dump:
            asyncio.run(dump(args))
        else:
            asyncio.run(watch(args))
    except KeyboardInterrupt:
        # Exit code 130 is the conventional "terminated by Ctrl-C" status
        # (128 + SIGINT's signal number 2) -- mostly cosmetic here since
        # this only matters when running interactively rather than under
        # systemd, but it's the correct/expected code for shells and scripts
        # that check $? after killing a foreground process.
        sys.exit(130)


if __name__ == "__main__":
    main()
