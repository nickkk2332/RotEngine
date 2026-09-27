"""JSON content loading: the modding backbone.

Every file under a content directory holds a list of objects (or a single
object), each with a "type" and an "id". Later mods override earlier ones by
id. Objects can inherit with CDDA-style "copy-from" plus "extend", "delete"
and "relative" blocks, so a mod can write

    {"type": "creature", "id": "super_soldier", "copy-from": "soldier",
     "relative": {"stats": {"ST": 4}}, "extend": {"traits": ["tough_hide"]}}

without restating the whole soldier.
"""
from __future__ import annotations

import copy
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Fields each type must have after inheritance is resolved.
REQUIRED: dict[str, tuple[str, ...]] = {
    "material": ("glyph",),
    "damage_type": (),
    "body_plan": ("parts",),
    "item": ("name",),
    "trait": (),
    "status": (),
    "power": ("name", "effects"),
    "creature": ("name", "stats", "body"),
    "tile_legend": ("tiles",),
    "skill": ("stat",),
    "gas": (),
    "palette": ("tiles",),
    "prefab": ("rows",),
    "monster_group": ("entries",),
    "item_group": ("entries",),
    "dungeon": ("name", "floors"),
}

_META_KEYS = {"copy-from", "extend", "delete", "relative", "abstract"}
HOOKS = ("on_damaged", "on_second", "on_kill")


class ContentError(Exception):
    pass


class Content:
    def __init__(self) -> None:
        self._raw: dict[str, dict[str, dict]] = defaultdict(dict)
        self._source: dict[tuple[str, str], str] = {}
        self._resolved: dict[str, dict[str, dict]] = defaultdict(dict)
        self.loaded_mods: list[str] = []

    # -- loading -----------------------------------------------------------
    def load_path(self, path: Path, mod: str = "core") -> None:
        files = sorted(path.rglob("*.json")) if path.is_dir() else [path]
        for f in files:
            if f.name == "modinfo.json":
                continue
            try:
                data = json.loads(f.read_text())
            except json.JSONDecodeError as e:
                raise ContentError(f"{f}: invalid JSON: {e}") from e
            for obj in data if isinstance(data, list) else [data]:
                self.add(obj, f"{mod}:{f.name}")

    def add(self, obj: dict, source: str = "<code>") -> None:
        if not isinstance(obj, dict) or "type" not in obj or "id" not in obj:
            raise ContentError(f"{source}: every object needs a 'type' and an 'id': {obj!r:.80}")
        type_, id_ = obj["type"], obj["id"]
        self._raw[type_][id_] = obj
        self._source[(type_, id_)] = source
        self._resolved.clear()

    # -- access ------------------------------------------------------------
    def get(self, type_: str, id_: str) -> dict:
        if id_ not in self._resolved[type_]:
            if id_ not in self._raw.get(type_, {}):
                raise ContentError(f"unknown {type_} '{id_}'")
            self._resolved[type_][id_] = self._resolve(type_, id_, ())
        return self._resolved[type_][id_]

    def has(self, type_: str, id_: str) -> bool:
        return id_ in self._raw.get(type_, {})

    def types(self) -> list[str]:
        return sorted(self._raw)

    def ids(self, type_: str) -> list[str]:
        return [i for i, o in self._raw.get(type_, {}).items() if not o.get("abstract")]

    def all(self, type_: str) -> dict[str, dict]:
        return {i: self.get(type_, i) for i in self.ids(type_)}

    def source(self, type_: str, id_: str) -> str:
        return self._source.get((type_, id_), "?")

    # -- inheritance -------------------------------------------------------
    def _resolve(self, type_: str, id_: str, chain: tuple[str, ...]) -> dict:
        if id_ in chain:
            raise ContentError(f"copy-from cycle: {' -> '.join(chain + (id_,))}")
        raw = self._raw[type_].get(id_)
        if raw is None:
            raise ContentError(f"{type_} '{chain[-1]}' copies from unknown '{id_}'")
        if "copy-from" not in raw:
            return {k: copy.deepcopy(v) for k, v in raw.items() if k not in _META_KEYS}
        out = self._resolve(type_, raw["copy-from"], chain + (id_,))
        out.update({k: copy.deepcopy(v) for k, v in raw.items() if k not in _META_KEYS})
        _apply_relative(out, raw.get("relative", {}))
        _apply_extend(out, raw.get("extend", {}))
        _apply_delete(out, raw.get("delete", {}))
        return out

    # -- validation --------------------------------------------------------
    def validate(self) -> list[str]:
        from . import effects  # local import: effects depends on content types only

        errors: list[str] = []
        for type_, objs in self._raw.items():
            for id_, raw in objs.items():
                if raw.get("abstract"):
                    continue
                where = f"{self.source(type_, id_)} {type_} '{id_}'"
                try:
                    obj = self.get(type_, id_)
                    errors.extend(f"{where}: {e}" for e in self._validate_one(type_, obj, effects))
                except ContentError as e:
                    errors.append(f"{where}: {e}")
                except Exception as e:  # malformed data must be a report, never a crash
                    errors.append(f"{where}: malformed ({type(e).__name__}: {e})")
        return errors

    def _validate_one(self, type_: str, obj: dict, effects) -> Iterable[str]:
        missing = [f for f in REQUIRED.get(type_, ()) if f not in obj]
        for field in missing:
            yield f"missing required field '{field}'"
        yield from _check_refs(self, type_, obj)
        hooks = obj.get("hooks", {})
        if not isinstance(hooks, dict):
            yield "'hooks' must be an object of {hook_name: [effects]}"
        else:
            for hook, fx in hooks.items():
                if hook not in HOOKS:
                    yield f"unknown hook '{hook}' (known: {', '.join(HOOKS)})"
                yield from (f"hook {hook}: {e}" for e in effects.validate(fx, content=self))
        if type_ == "power" and "effects" in obj:
            yield from effects.validate(obj["effects"], content=self)
            if "ai_condition" in obj:
                yield from (f"ai_condition: {e}" for e in effects.validate_condition(obj["ai_condition"]))
        if type_ == "creature" and not missing:
            stats = obj["stats"]
            if not isinstance(stats, dict) or any(not isinstance(v, (int, float)) for v in stats.values()):
                yield "stats must be an object of numbers"
            elif stats.get("ST", 10) + obj.get("hp_bonus", 0) < 1:
                yield "ST (+ hp_bonus) must be at least 1: HP = ST"
        if type_ in ("palette", "prefab", "monster_group", "item_group", "dungeon", "item") and not missing:
            yield from _validate_dungeon_content(self, type_, obj, effects)
        if type_ == "body_plan" and not missing:
            ids = {p.get("id") for p in obj["parts"]}
            for p in obj["parts"]:
                if p.get("parent") and p["parent"] not in ids:
                    yield f"part '{p.get('id')}' has unknown parent '{p['parent']}'"
                by_type = p.get("by_type", {})
                if not isinstance(by_type, dict):
                    yield f"part '{p.get('id')}': by_type must be an object of {{damage_type: overrides}}"
                    continue
                for dt, over in by_type.items():
                    if not self.has("damage_type", dt):
                        yield f"part '{p.get('id')}': by_type names unknown damage_type '{dt}'"
                for spec in [p, *(o for o in by_type.values() if isinstance(o, dict))]:
                    for field, ref_type in (("sever_item", "item"), ("fracture_status", "status"),
                                            ("destroy_status", "status")):
                        if spec.get(field) is not None and not self.has(ref_type, spec[field]):
                            yield f"part '{p.get('id')}': {field} references unknown {ref_type} '{spec[field]}'"


def _check_refs(content: Content, type_: str, obj: dict) -> Iterable[str]:
    def need(ref_type: str, ref: Any) -> Iterable[str]:
        if ref is not None and not content.has(ref_type, ref):
            yield f"references unknown {ref_type} '{ref}'"

    if type_ == "creature":
        yield from need("body_plan", obj.get("body"))
        for t in obj.get("traits", []):
            yield from need("trait", t)
        for p in obj.get("powers", []):
            yield from need("power", p)
        eq = obj.get("equipment", {})
        yield from need("item", eq.get("wield"))
        for w in eq.get("wear", []):
            yield from need("item", w)
    for atk in obj.get("attacks", []) + obj.get("natural_attacks", []):
        yield from need("damage_type", atk.get("damage", {}).get("type"))


def _validate_tile(content: Content, char: str, t: Any) -> Iterable[str]:
    if not isinstance(t, dict):
        yield f"tile {char!r} must be an object"
        return
    for key in ("fill", "floor"):
        if key in t and not content.has("material", t[key]):
            yield f"tile {char!r}: unknown material '{t[key]}'"
    for key, ref in (("monster", "monster_group"), ("item", "item_group")):
        if t.get(key) not in (None, "level") and not content.has(ref, t[key]):
            yield f"tile {char!r}: unknown {ref} '{t[key]}'"


def _validate_dungeon_content(content: Content, type_: str, obj: dict, effects) -> Iterable[str]:
    """Roguelike content: palettes, prefabs, spawn groups, dungeons, and
    items with a "use" block (medicine)."""
    if type_ == "palette":
        for ch, t in obj["tiles"].items():
            yield from _validate_tile(content, ch, t)
    elif type_ == "prefab":
        rows = obj["rows"]
        if not rows or not all(isinstance(r, str) for r in rows):
            yield "rows must be a non-empty list of strings"
            return
        if len({len(r) for r in rows}) != 1:
            yield "rows must all be the same width"
        legend = dict(content.get("tile_legend", "default")["tiles"]) if content.has("tile_legend", "default") else {}
        for pid in obj.get("palettes", []):
            if not content.has("palette", pid):
                yield f"unknown palette '{pid}'"
            else:
                legend.update(content.get("palette", pid)["tiles"])
        for ch, t in obj.get("legend", {}).items():
            yield from _validate_tile(content, ch, t)
        legend.update(obj.get("legend", {}))
        missing = sorted({ch for r in rows for ch in r if ch not in legend})
        if missing:
            yield f"characters with no palette entry: {''.join(missing)!r}"
        h, w = len(rows), max(len(r) for r in rows)
        slots = [(x, y) for y, r in enumerate(rows) for x, ch in enumerate(r) if legend.get(ch, {}).get("door_slot")]
        if not slots:
            yield "needs at least one door slot (a palette entry with \"door_slot\") in its outer wall"
        for x, y in slots:
            edge = x in (0, w - 1) or y in (0, h - 1)
            corner = x in (0, w - 1) and y in (0, h - 1)
            if not edge or corner:
                yield f"door slot at {(x, y)} must be on the outer wall and not a corner"
    elif type_ in ("monster_group", "item_group"):
        key, ref = ("creature", "creature") if type_ == "monster_group" else ("item", "item")
        for e in obj["entries"]:
            if not isinstance(e, dict) or key not in e:
                yield f"each entry needs a '{key}'"
            elif not content.has(ref, e[key]):
                yield f"entry references unknown {ref} '{e[key]}'"
    elif type_ == "dungeon":
        prefab_tags = {t for p in content.all("prefab").values() for t in p.get("tags", [])}
        for i, f in enumerate(obj["floors"]):
            where = f"floors[{i}]"
            if not isinstance(f.get("depth"), list) or len(f["depth"]) != 2:
                yield f"{where}: needs \"depth\": [first, last]"
            for tag in f.get("prefab_tags", []):
                if tag not in prefab_tags:
                    yield f"{where}: no prefab has tag '{tag}'"
            for key, ref in (("monsters", "monster_group"), ("items", "item_group")):
                g = f.get(key, {}).get("group")
                if g is not None and not content.has(ref, g):
                    yield f"{where}.{key}: unknown {ref} '{g}'"
            boss = f.get("boss")
            if boss:
                if not content.has("creature", boss.get("creature")):
                    yield f"{where}.boss: unknown creature '{boss.get('creature')}'"
                if boss.get("prefab_tag") and boss["prefab_tag"] not in prefab_tags:
                    yield f"{where}.boss: no prefab has tag '{boss['prefab_tag']}'"
            for key in ("rock", "corridor", "door", "doorway"):
                if key in f:
                    yield from (f"{where}.{e}" for e in _validate_tile(content, key, f[key]))
            if "exit_floor" in f and not content.has("material", f["exit_floor"]):
                yield f"{where}: unknown exit_floor material '{f['exit_floor']}'"
    elif type_ == "item" and "use" in obj:
        use = obj["use"]
        if not isinstance(use, dict) or "effects" not in use:
            yield "use needs \"effects\""
            return
        yield from (f"use.effects: {e}" for e in effects.validate(use["effects"], content=content))
        if "fail_effects" in use:
            yield from (f"use.fail_effects: {e}" for e in effects.validate(use["fail_effects"], content=content))
        if "needs" in use:
            yield from (f"use.needs: {e}" for e in effects.validate_condition(use["needs"]))
        if "skill" in use and not content.has("skill", use["skill"]):
            yield f"use.skill: unknown skill '{use['skill']}'"


def _apply_relative(out: dict, rel: dict, path: str = "relative") -> None:
    if not isinstance(rel, dict):
        raise ContentError(f"{path} must be an object")
    for k, v in rel.items():
        if isinstance(v, dict):
            if not isinstance(out.setdefault(k, {}), dict):
                raise ContentError(f"{path}.{k}: can't apply an object to {out[k]!r}")
            _apply_relative(out[k], v, f"{path}.{k}")
        elif isinstance(v, (int, float)) and isinstance(out.get(k, 0), (int, float)):
            out[k] = out.get(k, 0) + v
        else:
            raise ContentError(f"{path}.{k}: only numbers can be relative (got {v!r} on {out.get(k)!r})")


def _apply_extend(out: dict, ext: dict) -> None:
    if not isinstance(ext, dict):
        raise ContentError("extend must be an object")
    for k, v in ext.items():
        if isinstance(v, list):
            out[k] = list(out.get(k, [])) + v
        elif isinstance(v, dict):
            out.setdefault(k, {}).update(v)
        else:
            raise ContentError(f"extend.{k} must be a list or object")


def _apply_delete(out: dict, dele: dict) -> None:
    for k, v in dele.items():
        cur = out.get(k)
        if isinstance(cur, list):
            out[k] = [x for x in cur if x not in v]
        elif isinstance(cur, dict):
            for key in v:
                cur.pop(key, None)


def _mod_order(mod_dirs: dict[str, Path], wanted: list[str]) -> list[str]:
    order: list[str] = []
    visiting: set[str] = set()

    def visit(mod: str) -> None:
        if mod in order:
            return
        if mod in visiting:
            raise ContentError(f"mod dependency cycle at '{mod}'")
        if mod not in mod_dirs:
            raise ContentError(f"unknown mod '{mod}'")
        visiting.add(mod)
        info = json.loads((mod_dirs[mod] / "modinfo.json").read_text())
        for dep in info.get("dependencies", []):
            if dep != "core":
                visit(dep)
        order.append(mod)

    for m in wanted:
        visit(m)
    return order


def load_content(mods: Iterable[str] = (), data_dir: Path = DATA_DIR) -> Content:
    """Load core content plus the named mods (and their dependencies)."""
    content = Content()
    content.load_path(data_dir / "core", "core")
    mod_dirs = {p.name: p for p in (data_dir / "mods").iterdir()
                if (p / "modinfo.json").exists()} if (data_dir / "mods").exists() else {}
    for mod in _mod_order(mod_dirs, list(mods)):
        content.load_path(mod_dirs[mod], mod)
        content.loaded_mods.append(mod)
    errors = content.validate()
    if errors:
        raise ContentError("content errors:\n  " + "\n  ".join(errors))
    return content
