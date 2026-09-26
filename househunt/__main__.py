import argparse
import logging
import sys

from .config import load_config
from .pipeline import run
from .report import write_csv, write_html
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
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO, format="%(levelname)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = load_config(args.config)
    html_path = cfg.output_dir / "listings.html"

    if args.command == "run":
        results = run(cfg)
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        csv_path = cfg.output_dir / "listings.csv"
        write_csv(results, cfg.destinations, csv_path)
        write_html(results, cfg.destinations, html_path)
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
