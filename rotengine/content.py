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
    "power": ("effects",),
    "creature": ("name", "stats", "body"),
    "tile_legend": ("tiles",),
    "skill": ("stat",),
}

_META_KEYS = {"copy-from", "extend", "delete", "relative", "abstract"}


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
                except ContentError as e:
                    errors.append(f"{where}: {e}")
                    continue
                for field in REQUIRED.get(type_, ()):
                    if field not in obj:
                        errors.append(f"{where}: missing required field '{field}'")
                errors.extend(f"{where}: {e}" for e in _check_refs(self, type_, obj))
                for hook, fx in obj.get("hooks", {}).items():
                    errors.extend(f"{where} hook {hook}: {e}" for e in effects.validate(fx))
                if type_ == "power":
                    errors.extend(f"{where}: {e}" for e in effects.validate(obj["effects"]))
                    if "ai_condition" in obj:
                        errors.extend(f"{where} ai_condition: {e}"
                                      for e in effects.validate_condition(obj["ai_condition"]))
        return errors


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


def _apply_relative(out: dict, rel: dict) -> None:
    for k, v in rel.items():
        if isinstance(v, dict):
            _apply_relative(out.setdefault(k, {}), v)
        else:
            out[k] = out.get(k, 0) + v


def _apply_extend(out: dict, ext: dict) -> None:
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
