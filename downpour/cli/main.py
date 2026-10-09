"""Command line.

    downpour                      open Downpour (or bring the running one forward)
    downpour URL [URL ...]        hand links to the running Downpour
    downpour --tray               start in the notification area
    downpour get URL [options]    download in this console, no window

``get`` keeps its own small database, so running the same command again
after an interruption resumes instead of starting over. Progress is printed
as whole lines a few seconds apart, which a screen reader can follow.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from downpour import APP_NAME, __version__


def main(argv: list[str]) -> int:
    if argv and argv[0] == "get":
        return _get(argv[1:])
    if argv and argv[0] in ("-V", "--version", "version"):
        print(f"{APP_NAME} {__version__}")
        return 0
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    tray = "--tray" in argv
    urls = [a for a in argv if a.lower().startswith(("http://", "https://"))]
    unknown = [a for a in argv if a not in urls and a != "--tray"]
    if unknown:
        print(f"Unknown argument: {unknown[0]}. Try: downpour --help", file=sys.stderr)
        return 2
    return _gui(urls, tray)


def _gui(urls: list[str], tray: bool) -> int:
    from downpour.core import ipc

    message = {"type": "add", "items": [{"url": u} for u in urls], "pick": len(urls) > 1} if urls else {"type": "show"}
    if not (tray and not urls):
        try:
            ipc.request(message)
            return 0  # the running copy has it
        except ipc.NotRunning:
            pass
    try:
        from downpour.gui.app import run
    except ImportError as e:
        print(f"The Downpour window needs wxPython: {e}", file=sys.stderr)
        return 1
    return run(urls=urls, tray=tray)


def _get(argv: list[str]) -> int:
    from downpour.core import settings as settings_mod
    from downpour.core.engine import Engine
    from downpour.core.models import Download, Status
    from downpour.core.paths import data_dir
    from downpour.core.store import Store
    from downpour.core.units import format_duration, format_size, format_speed, parse_size

    parser = argparse.ArgumentParser(prog="downpour get", description="Download one file in this console.")
    parser.add_argument("url")
    parser.add_argument("-d", "--dir", help="folder to save into (default: your Downloads folder)")
    parser.add_argument("-o", "--output", help="file name to save as")
    parser.add_argument("-c", "--connections", type=int, help="connections to use (1 to 32)")
    parser.add_argument("-l", "--limit", help="speed limit, for example 500K or 2M")
    parser.add_argument("--checksum", help="expected checksum, for example sha256:abc123...")
    parser.add_argument("--referrer", default="")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the result")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    settings = settings_mod.load()
    settings.use_category_folders = False
    settings.max_active = 1
    limit = 0
    if args.limit:
        limit = parse_size(args.limit) or 0
        if not limit:
            print(f"Could not read the speed limit {args.limit!r}", file=sys.stderr)
            return 2
    folder = str(Path(args.dir or settings.download_dir).resolve())

    engine = Engine(Store(data_dir() / "cli.db"), settings)
    # Earlier interrupted runs are remembered for resuming, not restarted here.
    engine.pause([d.id for d in engine.downloads() if d.status == Status.QUEUED])

    dl = next(
        (d for d in engine.downloads() if d.url == args.url and d.directory == folder and d.status != Status.COMPLETED),
        None,
    )
    if dl is None:
        dl = Download(
            url=args.url,
            directory=folder,
            filename=args.output or "",
            filename_fixed=bool(args.output),
            connections=args.connections or 0,
            speed_limit=limit,
            checksum=args.checksum or "",
            referrer=args.referrer,
        )
        try:
            engine.add(dl, start=True, force=True)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
    else:
        if not args.quiet:
            print(f"Resuming {dl.filename} ({format_size(dl.downloaded)} already downloaded)")
        engine.start([dl.id])

    interrupted = threading.Event()

    def on_interrupt(signum, frame):
        interrupted.set()
        engine.pause([dl.id])

    signal.signal(signal.SIGINT, on_interrupt)
    started = time.monotonic()
    last_line = started
    try:
        while True:
            time.sleep(0.2)
            if dl.status in (Status.COMPLETED, Status.FAILED, Status.PAUSED) and not engine.is_running(dl.id):
                break
            now = time.monotonic()
            if not args.quiet and dl.status == Status.DOWNLOADING and now - last_line >= 3:
                last_line = now
                parts = []
                if dl.size:
                    parts.append(f"{dl.downloaded * 100 // dl.size}% of {format_size(dl.size)}")
                else:
                    parts.append(format_size(dl.downloaded))
                if dl.speed:
                    parts.append(format_speed(dl.speed))
                if dl.eta is not None:
                    parts.append(f"{format_duration(dl.eta)} left")
                conns = dl.active_connections
                parts.append(f"{conns} connection" + ("" if conns == 1 else "s"))
                print(f"{dl.filename}: " + ", ".join(parts), flush=True)
    finally:
        engine.shutdown()

    elapsed = time.monotonic() - started
    if dl.status == Status.COMPLETED:
        print(f"Saved {dl.path} ({format_size(dl.size)} in {format_duration(elapsed)})")
        return 0
    if interrupted.is_set() or dl.status == Status.PAUSED:
        print(f"Stopped. Run the same command again to resume {dl.filename}.")
        return 130
    print(f"Failed: {dl.error}", file=sys.stderr)
    return 1
