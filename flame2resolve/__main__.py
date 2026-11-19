"""Run the browser app or inspect and export archives from a terminal."""

import argparse
from getpass import getpass
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

from . import archives, auth, flame

DEFAULT_STATE = Path(".flame2resolve")


def initialize_auth(path):
    if path.exists() or path.is_symlink():
        raise ValueError("This verifier already exists. Choose a new path to rotate the password.")
    password = getpass("Development password (12–512 characters): ")
    if not 12 <= len(password) <= 512:
        raise ValueError("Enter a development password between 12 and 512 characters.")
    if password != getpass("Repeat password: "):
        raise ValueError("Passwords did not match.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as file:
        file.write(auth.make_verifier(password))
    print(f"Created password verifier at {path}")


def serve(args):
    from .server import Application

    if not 1 <= args.port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    origin = args.origin or f"http://127.0.0.1:{args.port}"
    url = urlsplit(origin)
    if (url.scheme not in ("http", "https") or not url.hostname or url.username or
        url.query or url.fragment or url.path not in ("", "/")):
        raise ValueError("Use a browser origin such as https://flame2resolve.example.org.")
    if url.scheme == "http" and url.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Use HTTPS for a non-local browser origin.")
    verifier = (args.verifier or args.state / "password-verifier").read_text(encoding="ascii")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with Application("127.0.0.1", args.port, args.state / "uploads", verifier, origin) as app:
        logging.info("Flame2Resolve is ready at %s", origin)
        try:
            app.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            app.done.set()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-auth", help="Create a local password verifier")
    init.add_argument("--verifier", type=Path, default=DEFAULT_STATE / "password-verifier")

    web = commands.add_parser("serve", help="Start the password-gated browser app")
    web.add_argument("--state", type=Path, default=DEFAULT_STATE)
    web.add_argument("--verifier", type=Path)
    web.add_argument("--port", type=int, default=8427, metavar="PORT")
    web.add_argument("--origin", default=os.environ.get("FLAME2RESOLVE_ORIGIN"), help="Exact browser origin when using an HTTPS reverse proxy")

    inspect = commands.add_parser("inspect", help="List candidate timelines and source base paths as JSON")
    inspect.add_argument("header", type=Path, help="Path to the native Flame archive header")

    export = commands.add_parser("export", help="Write one DRT and print its review as JSON")
    export.add_argument("header", type=Path)
    export.add_argument("--timeline", required=True, help="Sequence ID returned by inspect")
    export.add_argument("--output", required=True, type=Path)
    export.add_argument("--scale", choices=archives.SCALE.keys(), default="preserve")
    export.add_argument("--map", action="append", nargs=2, metavar=("SOURCE", "TARGET"), default=[])

    audit = commands.add_parser("audit-timewarp", help="Print decoded raw Timewarp setups as JSON")
    audit.add_argument("header", type=Path)

    args = parser.parse_args()
    try:
        if args.command == "init-auth":
            initialize_auth(args.verifier)
        elif args.command == "serve":
            serve(args)
        elif args.command == "audit-timewarp":
            data = flame.archive_metadata(args.header)
            print(json.dumps([{"offset": hex(at), **flame.timewarp_summary(state)}
                              for at, state in flame.timewarp_payloads(data)], indent=2))
        elif args.command == "inspect":
            entries, plans = archives.inspect(args.header)
            print(json.dumps({"timelines": entries, "bases": archives.source_bases(plans.values())}, indent=2))
        else:
            from . import conversion

            if args.output.suffix.lower() != ".drt":
                raise ValueError("Export to a new path ending in .drt.")
            _, plans = archives.inspect(args.header)
            if args.timeline not in plans:
                raise ValueError("Select a recoverable timeline ID from inspect.")
            rules = archives.mappings([{"source": source, "target": target} for source, target in args.map])
            binary, report = conversion.convert(plans[args.timeline], args.scale, rules, bool(args.map))
            with args.output.open("xb") as file:
                file.write(binary)
            print(json.dumps(report, indent=2))
    except archives.ARCHIVE_ERRORS + (RuntimeError,) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
