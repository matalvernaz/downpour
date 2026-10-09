"""Entry point: the GUI, the command line, or the browser's native-messaging host."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    # A browser starting us as its native-messaging host cannot pass flags,
    # so its calling convention has to be recognised before anything else.
    try:
        from downpour.browser import native_host
    except ImportError:
        native_host = None
    if native_host is not None and native_host.is_native_host_invocation(argv):
        return native_host.main(argv)

    from downpour.cli.main import main as cli_main

    return cli_main(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
