"""Command line: python -m rotengine {arena,list,validate} ..."""
from __future__ import annotations

import argparse
import sys

from . import arena
from .content import DATA_DIR, ContentError, load_content


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rotengine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("arena", help="run an arena scenario")
    a.add_argument("scenario", help="scenario id (data/scenarios) or path to a .json")
    a.add_argument("--seed", type=int, default=None)
    a.add_argument("--runs", type=int, default=1, help="run N seeded fights and print statistics")
    a.add_argument("--map", action="store_true", help="print the map before and after")
    a.add_argument("--mod", action="append", default=[], help="load an extra mod")

    sub.add_parser("list", help="list scenarios")
    v = sub.add_parser("validate", help="load and validate all content")
    v.add_argument("--mod", action="append", default=[])

    args = ap.parse_args(argv)
    try:
        if args.cmd == "list":
            for p in sorted((DATA_DIR / "scenarios").glob("*.json")):
                s = arena.load_scenario(p)
                print(f"{p.stem:<22} {s.get('name', '')} - {s.get('description', '')}")
        elif args.cmd == "validate":
            c = load_content(args.mod)
            counts = {t: len(c.ids(t)) for t in sorted(c._raw)}
            print("content OK:", ", ".join(f"{n} {t}" for t, n in counts.items()))
        else:
            scenario = arena.load_scenario(args.scenario)
            content = arena.content_for(scenario, args.mod)
            if args.runs > 1:
                print(arena.run_batch(scenario, content, args.runs, args.seed or 0))
            else:
                arena.run_once(scenario, content, args.seed, echo=print, show_map=args.map)
    except ContentError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
