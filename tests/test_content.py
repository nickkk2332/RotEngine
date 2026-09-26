import pytest

from rotengine.content import Content, ContentError, load_content


def test_core_loads_clean(content):
    assert "hulk" in content.ids("creature")
    assert "human" not in content.ids("creature")  # abstract templates are hidden


def test_copy_from_inherits(content):
    tough = content.get("creature", "street_tough")
    assert tough["stats"]["ST"] == 10 and tough["body"] == "humanoid"
    assert content.get("item", "tactical_pistol")["attacks"] == content.get("item", "pistol")["attacks"]


def test_relative_extend_delete():
    c = Content()
    c.add({"type": "trait", "id": "a"})
    c.add({"type": "creature", "id": "base", "name": "b", "body": "x",
           "stats": {"ST": 10}, "traits": ["a", "b"], "skills": {"guns": 10}})
    c.add({"type": "creature", "id": "child", "copy-from": "base",
           "relative": {"stats": {"ST": 5}, "skills": {"guns": -2}},
           "extend": {"traits": ["c"]}, "delete": {"traits": ["a"]}})
    child = c.get("creature", "child")
    assert child["stats"]["ST"] == 15
    assert child["skills"]["guns"] == 8
    assert child["traits"] == ["b", "c"]
    assert c.get("creature", "base")["stats"]["ST"] == 10  # parent untouched


def test_copy_from_cycle_is_an_error():
    c = Content()
    c.add({"type": "trait", "id": "a", "copy-from": "b"})
    c.add({"type": "trait", "id": "b", "copy-from": "a"})
    with pytest.raises(ContentError, match="cycle"):
        c.get("trait", "a")


def test_validation_catches_typos(content):
    c = Content()
    c.add({"type": "power", "id": "oops", "effects": [{"damgae": {"amount": 3}}]})
    c.add({"type": "creature", "id": "x", "name": "x", "stats": {}, "body": "nope"})
    errors = c.validate()
    assert any("unknown effect 'damgae'" in e for e in errors)
    assert any("unknown body_plan 'nope'" in e for e in errors)


def test_mod_adds_and_inherits(modded_content):
    sup = modded_content.get("creature", "super_soldier")
    base = modded_content.get("creature", "soldier")
    assert sup["stats"]["ST"] == base["stats"]["ST"] + 6
    assert sup["traits"] == base["traits"] + ["high_pain_threshold"]
    assert modded_content.has("power", "adrenal_surge")


def test_unknown_mod():
    with pytest.raises(ContentError, match="unknown mod"):
        load_content(["does_not_exist"])
