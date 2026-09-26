import argparse
import logging
import sys

from .config import load_config
from .pipeline import run
from .report import write_csv, write_html


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="househunt", description="Uusimaa homes ranked by travel time")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="Fetch listings, compute travel times, write reports")
    p_run.add_argument("-c", "--config", default="config.yaml")
    p_run.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = load_config(args.config)
    results = run(cfg)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path, html_path = cfg.output_dir / "listings.csv", cfg.output_dir / "listings.html"
    write_csv(results, cfg.destinations, csv_path)
    write_html(results, cfg.destinations, html_path)
    print(f"{len(results)} listings -> {csv_path}, {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
