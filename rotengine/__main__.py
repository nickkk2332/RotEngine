"""Command line: python -m rotengine {play,run,arena,mapgen,list,validate} ..."""
from __future__ import annotations

import argparse
import random
import signal
import sys

from . import arena
from .content import DATA_DIR, ContentError, load_content
from .mapgen import MapgenError
from .roguelike import SaveError


def main(argv: list[str] | None = None) -> int:
    if hasattr(signal, "SIGPIPE"):  # `rotengine arena ... | head` shouldn't traceback
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    ap = argparse.ArgumentParser(prog="rotengine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("arena", help="run an arena scenario")
    a.add_argument("scenario", help="scenario id (data/scenarios) or path to a .json")
    a.add_argument("--seed", type=int, default=None,
                   help="replay a specific fight (a random seed is printed otherwise)")
    a.add_argument("--runs", type=int, default=1, help="run N seeded fights and print statistics")
    a.add_argument("--map", action="store_true", help="print the map before and after")
    a.add_argument("--summary", action="store_true", help="skip the blow-by-blow log")
    a.add_argument("--aftermath", type=float, default=None,
                   help="seconds to keep simulating after the fight (default: scenario's, 120)")
    a.add_argument("--mod", action="append", default=[], help="load an extra mod")

    pl = sub.add_parser("play", help="play in a window (needs: pip install tcod)")
    pl.add_argument("scenario", nargs="?", help="jump straight into a scenario")
    pl.add_argument("--as", dest="play_as", help="who to play in that scenario (e.g. 'John Wick')")
    pl.add_argument("--seed", type=int, default=None)
    pl.add_argument("--font", default="mono", choices=("mono", "mono-large", "square12", "square16"),
                    help="mono (default) reads best; square fonts give square map tiles")
    pl.add_argument("--mod", action="append", default=[], help="load an extra mod")

    r = sub.add_parser("run", help="roguelike mode in a window: start a new run, or continue yours")
    r.add_argument("--as", dest="play_as", default=None,
                   help="who goes down (a creature id, e.g. operative, wick, hulk)")
    r.add_argument("--seed", type=int, default=None)
    r.add_argument("--continue", dest="resume", action="store_true", help="continue your saved run")
    r.add_argument("--font", default="mono", choices=("mono", "mono-large", "square12", "square16"))
    r.add_argument("--mod", action="append", default=[], help="load an extra mod")

    m = sub.add_parser("mapgen", help="print a generated dungeon floor as ASCII")
    m.add_argument("--dungeon", default="black_site")
    m.add_argument("--depth", type=int, default=1)
    m.add_argument("--seed", type=int, default=None)
    m.add_argument("--mod", action="append", default=[])

    sub.add_parser("list", help="list scenarios")
    v = sub.add_parser("validate", help="load and validate all content")
    v.add_argument("--mod", action="append", default=[])

    args = ap.parse_args(argv)
    try:
        if args.cmd == "play":
            try:
                from .ui.app import main as play
            except ImportError:
                print("The windowed game needs python-tcod: pip install tcod", file=sys.stderr)
                return 1
            play(args.scenario, args.play_as, args.seed, args.font, args.mod)
        elif args.cmd == "run":
            try:
                from .ui.app import main as play
            except ImportError:
                print("The windowed game needs python-tcod: pip install tcod", file=sys.stderr)
                return 1
            play(None, None, args.seed, args.font, args.mod,
                 run=("continue" if args.resume else args.play_as or "operative"))
        elif args.cmd == "mapgen":
            from . import mapgen
            content = load_content(args.mod)
            seed = args.seed if args.seed is not None else random.randrange(1_000_000)
            plan = mapgen.generate(content, args.dungeon, args.depth, seed)
            print(f"{plan.name} (depth {plan.depth}, seed {seed}): {len(plan.rooms)} rooms, "
                  f"{len(plan.spawns)} people, {len(plan.items)} items. @ start, > exit, M someone")
            print(plan.ascii(content))
        elif args.cmd == "list":
            for p in sorted((DATA_DIR / "scenarios").glob("*.json")):
                s = arena.load_scenario(p)
                print(f"{p.stem:<22} {s.get('name', '')} - {s.get('description', '')}")
        elif args.cmd == "validate":
            c = load_content(args.mod)
            print("content OK:", ", ".join(f"{len(c.ids(t))} {t}" for t in c.types()))
        else:
            scenario = arena.load_scenario(args.scenario)
            content = arena.content_for(scenario, args.mod)
            if args.runs > 1:
                print(arena.run_batch(scenario, content, args.runs, args.seed or 0, args.aftermath))
            else:
                seed = args.seed if args.seed is not None else random.randrange(1_000_000)
                print(f"{scenario.get('name', scenario['id'])} - seed {seed}")
                arena.run_once(scenario, content, seed, echo=print, show_map=args.map,
                               aftermath=args.aftermath, play_by_play=not args.summary)
    except (ContentError, arena.ScenarioError, MapgenError, SaveError) as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
