import argparse
import logging
import os
import sys

from .config import load_config
from .pipeline import run
from .progress import Cancelled, Progress, TerminalProgress
from .report import build_payload, write_csv, write_html
from .serve import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="househunt", description="Uusimaa homes ranked by travel time")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="Fetch listings, compute travel times, write reports")
    p_run.add_argument("-c", "--config", default="config.yaml")
    p_run.add_argument("-v", "--verbose", action="store_true")
    p_run.add_argument("--serve", action="store_true", help="Serve the report on localhost when done")
    p_serve = sub.add_parser("serve", help="Serve the latest report on localhost")
    p_serve.add_argument("-c", "--config", default="config.yaml")
    for p in (p_run, p_serve):
        p.add_argument("--port", type=int, default=8765)
        p.add_argument("--no-browser", action="store_true")
    p_web = sub.add_parser("web", help="Run the web app")
    p_web.add_argument("--host", default=os.environ.get("HOUSEHUNT_HOST", "127.0.0.1"))
    p_web.add_argument("--port", type=int, default=int(os.environ.get("HOUSEHUNT_PORT", "8000")))
    p_web.add_argument("--data-dir", default=os.environ.get("HOUSEHUNT_DATA_DIR", "data"))
    p_web.add_argument("--no-auth", action="store_true", help="Disable the password (only allowed on 127.0.0.1)")
    p_web.add_argument("--import-config", metavar="YAML", help="Create a search profile from a YAML config and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO, format="%(levelname)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.command == "web":
        from .web.app import main as web_main

        return web_main(args)

    cfg = load_config(args.config)
    html_path = cfg.output_dir / "listings.html"

    if args.command == "run":
        try:
            results = run(cfg, Progress(TerminalProgress()))
        except (KeyboardInterrupt, Cancelled):
            print("\nInterrupted. Finished routes are cached; the next run continues from there.", file=sys.stderr)
            return 130
        payload = build_payload(results, cfg.destinations, cfg.map)
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        csv_path = cfg.output_dir / "listings.csv"
        write_csv(payload, csv_path)
        write_html(payload, html_path)
        print(f"{len(results)} listings -> {csv_path}, {html_path}")
        if not args.serve:
            return 0

    if not html_path.exists():
        print(f"No report at {html_path}; run `python -m househunt run` first", file=sys.stderr)
        return 1
    serve(cfg.output_dir, args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
