"""Interactive play: the sim pauses for the player, and the UI drives it.
UI tests run headlessly on tcod consoles (skipped if tcod isn't installed)."""
import pytest

from rotengine import actions, arena
from rotengine.sim import Sim
from rotengine.world import World


def open_arena(content, seed=0, w=14, h=9):
    world = World(content.all("material"), w, h, 1)
    for y in range(h):
        for x in range(w):
            world.set_floor((x, y, 0), "concrete")
    return Sim(content, world, seed)


# -- engine side: advance / player_act -------------------------------------------
def test_sim_pauses_for_the_player(content):
    sim = open_arena(content)
    me = sim.spawn("street_tough", "a", (2, 4, 0))
    sim.spawn("street_tough", "b", (12, 4, 0))
    me.controller = "player"
    assert sim.advance() == "player" and sim.awaiting is me
    assert sim.advance() == "player"  # asking again doesn't skip the turn
    t0 = sim.time
    sim.player_act(actions.step_dir(sim, me, 1, 0))
    assert me.pos == (3, 4, 0)
    assert sim.advance() == "player" and sim.time > t0


def test_player_tempo_means_more_turns(content):
    sim = open_arena(content)
    fast = sim.spawn("speedster", "a", (2, 4, 0))
    sim.spawn("street_tough", "b", (12, 4, 0))
    fast.controller = "player"
    sim.advance()
    t0 = sim.time
    sim.player_act(actions.wait(sim, fast, 1000))  # one second of *his* time
    sim.advance()
    assert sim.time - t0 == 125


def test_stunned_player_is_skipped(content):
    sim = open_arena(content)
    me = sim.spawn("street_tough", "a", (2, 4, 0))
    sim.spawn("street_tough", "b", (12, 4, 0))
    me.controller = "player"
    sim.advance()
    sim.apply_status(me, "stunned", 3000)
    sim.player_act(500)
    assert sim.advance() == "player"
    assert sim.time >= me.statuses.get("stunned", 0)  # only asked again once the stun wore off


def test_attack_plans_match_best(content):
    from rotengine import combat
    sim = open_arena(content)
    wick = sim.spawn("wick", "a", (2, 4, 0))
    thug = sim.spawn("thug", "b", (8, 4, 0))
    plans = combat.attack_plans(sim, wick, thug)
    best = combat.best_attack_plan(sim, wick, thug)
    assert max(p.value for p in plans) == pytest.approx(best.value)
    assert all(0 <= p.p_land <= 1 for p in plans)


# -- UI side (headless) ---------------------------------------------------------------
tcod = pytest.importorskip("tcod")


def screen_text(app) -> str:
    from rotengine.ui.theme import SCREEN_H, SCREEN_W
    con = tcod.console.Console(SCREEN_W, SCREEN_H, order="F")
    app.render(con)
    return str(con)


def new_app(scenario=None, who=None, seed=5):
    from rotengine.ui.app import App
    app = App()
    if scenario:
        app.start(scenario, who, seed)
    return app


def test_key_translation():
    import tcod.event as ev
    from rotengine.ui.keys import translate
    down = ev.KeyDown(scancode=ev.Scancode.UP, sym=ev.KeySym.UP, mod=ev.Modifier.NONE)
    assert translate(down) == "up"
    kp = ev.KeyDown(scancode=ev.Scancode.KP_7, sym=ev.KeySym.KP_7, mod=ev.Modifier.NONE)
    assert translate(kp) == "upleft"
    assert translate(ev.TextInput(text="F")) == "F"
    assert translate(ev.TextInput(text="7")) is None  # numpad digits come as KeyDown


def test_menus_lead_into_a_fight():
    from rotengine.ui.game import GameScreen
    app = new_app()
    assert "Play a scenario" in screen_text(app)
    app.handle_key("a")
    assert "Choose a scenario" in screen_text(app)
    names = [label for label, _ in app.screen.items]
    app.handle_key(chr(ord("a") + names.index("John Wick vs thugs")))
    assert "who do you play?" in screen_text(app)
    app.handle_key("a")  # John Wick is listed first
    assert isinstance(app.screen, GameScreen)
    assert "John Wick" in screen_text(app)


def test_every_scenario_and_mode_renders():
    for path in sorted((arena.DATA_DIR / "scenarios").glob("*.json")):
        app = new_app(path.stem, None)
        app.handle_key("a")
        g = app.screen
        for keys in (["?"], ["x", "right", "esc"], ["[", "]"], ["f", "enter"], ["esc"], ["p"]):
            for k in keys:
                app.handle_key(k)
            screen_text(app)
            g.mode = "play"
        assert g.player.name in screen_text(app)


def test_attack_menu_executes_an_attack():
    app = new_app("wick_vs_thugs", "wick")
    g = app.screen
    t0 = g.sim.time
    app.handle_key("f")
    app.handle_key("enter")
    assert g.mode == "attack" and "to hit" in screen_text(app)
    best = g.menu.plan()
    assert best is not None
    app.handle_key("enter")
    assert g.mode in ("play", "over")
    assert g.sim.time > t0
    assert any(line.split("] ", 1)[1].startswith("John Wick") for line in g.sim.lines)


def test_a_whole_fight_to_the_aftermath():
    app = new_app("wick_vs_thugs", "wick", seed=5)
    g = app.screen
    for _ in range(300):
        if g.mode == "over":
            break
        w = g.player.wielded
        app.handle_key("r" if w is not None and w.ammo == 0 else "F" if g._visible_enemies() else ".")
    assert g.mode == "over"
    assert "Fight over" in screen_text(app)
    app.handle_key("a")
    assert g.mode == "summary" and "120s later" in screen_text(app)
    app.handle_key("q")
    assert "Play a scenario" in screen_text(app)


def test_quick_attack_closes_distance_for_melee():
    app = new_app("speedster_vs_squad", "speedster", seed=3)
    g = app.screen
    start = g.player.pos
    app.handle_key("F")
    assert g.player.pos != start or g.player.target is None or g.mode == "over"


def test_powers_menu_uses_a_power():
    app = new_app("hulk_vs_squad", "hulk", seed=1)
    g = app.screen
    app.handle_key("p")
    assert "thunderclap" in screen_text(app)
    stamina = g.player.stamina
    app.handle_key("a")  # thunderclap, whoever is near
    assert g.player.stamina < stamina or "fizzles" in "".join(g.sim.lines)


def test_custom_fight_starts():
    from rotengine.ui.game import GameScreen
    from rotengine.ui.menus import CustomFight
    app = new_app()
    app.handle_key("b")
    assert isinstance(app.screen, CustomFight)
    app.handle_key("down")    # 'you'
    app.handle_key("right")
    app.handle_key("enter")
    assert isinstance(app.screen, GameScreen)
    assert app.screen.player.controller == "player"


def test_quit_to_menu():
    app = new_app("street_fight", None)
    app.handle_key("a")
    app.handle_key("esc")
    assert "Leave this fight?" in screen_text(app)
    app.handle_key("y")
    assert "Play a scenario" in screen_text(app)


def test_stealth_controls():
    app = new_app("stealth_compound", "operative", seed=1)
    g = app.screen
    p = g.player
    assert "unseen" in screen_text(app)
    app.handle_key("s")
    assert p.sneaking and "sneaking" in screen_text(app)
    app.handle_key("w")
    assert p.wielded.id == "suppressed_pistol"
    app.handle_key("v")
    assert g.show_cones
    screen_text(app)


def _grab_by(app, g, what):
    """G, then the letter for that grip in the grab menu."""
    app.handle_key("G")
    assert g.mode == "grab"
    options = [o[0] for o in g._grab_options()]
    app.handle_key(chr(ord("a") + options.index(what)))


def test_chokehold_from_the_ui():
    from rotengine.ui.game import GameScreen
    scenario = {"id": "yard", "name": "Yard", "start_alert": False, "ambient_light": 0.08,
                "levels": [["," * 30] * 5],
                "teams": {"you": [{"creature": "operative", "at": [4, 2, 0]}],
                          "them": [{"creature": "sentry", "at": [5, 2, 0], "facing": [1, 0]},
                                   {"creature": "sentry", "at": [28, 4, 0], "facing": [1, 0],
                                    "name": "far sentry"}]}}
    app = new_app()
    content = app.content_for(scenario)
    sim = arena.build(scenario, content, seed=2)
    app.screen = g = GameScreen(app, scenario, content, sim, sim.creatures[0], 2)
    p, guard = g.player, sim.creatures[1]
    for _ in range(30):  # grab the neck; G chokes; if he wriggles free, grab him again
        if not guard.conscious:
            break
        if p.grappling is None:
            if g.mode != "play":
                app.handle_key("esc")
            _grab_by(app, g, "neck")
        else:
            assert g.mode == "grapple" and "Holding sentry by the neck" in screen_text(app)
            app.handle_key("G")
    assert not guard.conscious and not guard.dead
    app.handle_key("esc")
    assert p.grappling is guard and "holding sentry's neck" in screen_text(app)
    app.handle_key("L")
    assert p.grappling is None


def test_log_only_shows_what_you_witness():
    app = new_app("stealth_compound", "operative", seed=1)
    g = app.screen
    sim = g.sim
    far = next(c for c in sim.creatures if c.name == "tower sentry")
    hidden = next((x, y, 0) for y in range(sim.world.height) for x in range(sim.world.width)
                  if (x, y, 0) not in g.visible)
    with sim.focus(hidden):
        sim.log("a secret thing happens far away")
    sim.log("You hear something.", private_to=g.player.uid)
    sim.log("A thing only someone else hears.", private_to=far.uid)
    text = screen_text(app)
    assert "You hear something." in text
    assert "secret thing" not in text and "someone else" not in text


def _custom_game(scenario, seed=1):
    from rotengine.ui.game import GameScreen
    app = new_app()
    content = app.content_for(scenario)
    sim = arena.build(scenario, content, seed=seed)
    app.screen = GameScreen(app, scenario, content, sim, sim.creatures[0], seed)
    return app, app.screen


def test_throw_from_the_ui():
    scenario = {"id": "range", "name": "Range", "levels": [["." * 30] * 7],
                "teams": {"you": [{"creature": "swat", "at": [2, 3, 0]}],
                          "them": [{"creature": "thug", "at": [14, 3, 0]}]}}
    app, g = _custom_game(scenario)
    n = len(g.player.carried)
    app.handle_key("t")
    assert g.mode == "throwmenu" and "Throw what?" in screen_text(app)
    app.handle_key("a")  # frag grenade
    assert g.mode == "aim" and g.cursor == g.sim.creatures[1].pos
    assert "to land on target" in screen_text(app)
    app.handle_key("enter")
    assert len(g.player.carried) == n - 1
    for _ in range(6):
        if g.mode == "over":
            break
        app.handle_key(".")
    assert any("goes off" in line for line in g.sim.lines)


def test_direction_commands_and_hazards_render():
    scenario = {"id": "house", "name": "House", "levels": [["WWWWWWWWWW", "W________W", "W____+___W", "WWWWWWWWWW"]],
                "teams": {"you": [{"creature": "hulk", "at": [4, 2, 0]}],
                          "them": [{"creature": "thug", "at": [8, 1, 0]}]}}
    app, g = _custom_game(scenario)
    app.handle_key("right")  # walk into the door: it opens
    assert g.sim.world.passable((5, 2, 0))
    app.handle_key("c")
    assert g.mode == "dir"
    app.handle_key("right")
    assert not g.sim.world.passable((5, 2, 0))
    app.handle_key("B")
    app.handle_key("down")  # the Hulk smashes through the wooden wall below him
    assert g.sim.world.passable((4, 3, 0)) or "giving way" in g.sim.lines[-1]
    g.sim.fields.ignite((2, 1, 0), 8)
    g.sim.fields.add_gas("smoke", (3, 2, 0), 80)
    g._update_fov()
    screen_text(app)


def test_hold_menu_shows_moves_and_odds():
    from rotengine.ui.game import GameScreen
    scenario = {"id": "yard", "name": "Yard", "start_alert": False,
                "levels": [["," * 20] * 5],
                "teams": {"you": [{"creature": "hulk", "at": [4, 2, 0]}],
                          "them": [{"creature": "sentry", "at": [5, 2, 0], "facing": [1, 0]},
                                   {"creature": "sentry", "at": [18, 4, 0], "name": "far sentry"}]}}
    app = new_app()
    content = app.content_for(scenario)
    sim = arena.build(scenario, content, seed=1)
    app.screen = g = GameScreen(app, scenario, content, sim, sim.creatures[0], 1)
    p, guard = g.player, sim.creatures[1]
    app.handle_key("G")
    text = screen_text(app)
    assert "Grab sentry by..." in text and "right arm" in text and "their 9mm pistol" in text
    app.handle_key("esc")
    for _ in range(10):
        if p.grappling is guard:
            break
        if g.mode != "play":
            app.handle_key("esc")
        _grab_by(app, g, "r_arm")
    assert p.grappling is guard and g.mode == "grapple"
    text = screen_text(app)
    assert "Holding sentry by the right arm" in text
    assert "tear off the right arm" in text and "comes right off" in text
    assert "choke" not in text  # you're holding an arm, not the neck
    options = [o[0] for o in g._grapple_options()]
    app.handle_key(chr(ord("a") + options.index("tear off the right arm")))
    assert guard.body.part("r_arm").destroyed
    assert p.wielded is not None and "right arm" in p.wielded.name
    assert p.grappling is None and g.mode == "play"


# -- roguelike mode ------------------------------------------------------------------
def run_app(tmp_path, seed=11):
    """Main menu -> new run -> the operative, saving to a temp dir."""
    app = new_app()
    app.save_path = tmp_path / "run.sav"
    app.handle_key("c")
    assert "who goes down?" in screen_text(app)
    app.handle_key("a")  # the operative
    return app, app.screen


def test_a_run_from_the_menu(tmp_path):
    from rotengine.creature import Item
    app, g = run_app(tmp_path)
    run = g.run
    assert run is not None and app.save_path.exists()
    text = screen_text(app)
    assert "Floor 1/6" in text and run.plan.name in text
    # inventory, medicine, character sheet
    g.player.carried.append(Item(run.content.get("item", "bandage")))
    app.handle_key("i")
    assert "Inventory" in screen_text(app) and "field dressing" in screen_text(app)
    app.handle_key("esc")
    app.handle_key("a")
    assert "Use what?" in screen_text(app) and "not bleeding" in screen_text(app)
    app.handle_key("esc")
    app.handle_key("@")
    text = screen_text(app)
    assert "Skills" in text and "stealth" in text and "Wounds" in text
    app.handle_key("x")
    assert g.mode == "play"
    # take the stairs: a new floor, and an autosave
    g.player.pos = (*run.plan.exit, 0)
    app.handle_key(">")
    assert run.depth == 2 and g.sim is run.sim and "Floor 2/6" in screen_text(app)
    # save and quit, then continue
    app.handle_key("esc")
    app.handle_key("y")
    assert "Continue your run" in screen_text(app)
    app.handle_key("d")
    assert app.screen.run is not None and app.screen.run.depth == 2
    assert app.screen.run.player.name == run.player.name


def test_resting_heals_and_dying_ends_the_run(tmp_path):
    app, g = run_app(tmp_path, seed=12)
    p = g.player
    for c in g.sim.creatures:  # nobody around to interrupt
        if c is not p:
            c.dead = True
    p.body.hp = p.max_hp / 2
    t0 = g.sim.time
    app.handle_key("R")
    assert p.hp > p.max_hp / 2 and g.sim.time > t0 and "Rested" in g.notice
    from rotengine import combat
    combat.kill(g.sim, p, "test")
    g.status = g.sim.advance()
    g._on_turn()
    assert g.mode == "over" and "Dead" in screen_text(app)
    assert not app.save_path.exists()  # permadeath
    app.handle_key("enter")
    assert "Continue your run" not in screen_text(app)


def test_letters_work_without_text_input_events():
    """SDL3 only sends TextInput once asked; if it never comes, letters are
    read off KeyDown. Once TextInput shows up, it's trusted and the first key
    isn't counted twice."""
    import tcod.event as ev
    from rotengine.ui.keys import KeyReader

    def down(ch, shift=False):
        return ev.KeyDown(scancode=ev.Scancode.A, sym=ev.KeySym(ord(ch)),
                          mod=ev.Modifier.SHIFT if shift else ev.Modifier.NONE)
    r = KeyReader()
    assert r.read(down("c")) == "c"
    assert r.read(down("g", shift=True)) == "G"
    assert r.read(down("/", shift=True)) == "?"
    assert r.read(down(".", shift=True)) == ">"
    assert r.read(ev.KeyDown(scancode=ev.Scancode.UP, sym=ev.KeySym.UP, mod=ev.Modifier.NONE)) == "up"
    # the first real TextInput duplicates the KeyDown just handled: dropped
    assert r.read(down("a")) == "a"
    assert r.read(ev.TextInput(text="a")) is None
    # from now on letters come from TextInput only
    assert r.read(down("b")) is None
    assert r.read(ev.TextInput(text="b")) == "b"


def test_threats_from_above_and_the_message_log():
    app = new_app("hulk_vs_squad", "the Hulk", seed=3)
    g = app.screen
    for k in "..":
        app.handle_key(k)
    text = screen_text(app)
    assert "is aiming at you" in text and "above" in text
    app.handle_key("M")
    assert g.mode == "messages" and "Message log" in screen_text(app) and "takes aim" in screen_text(app)
    app.handle_key("up")
    app.handle_key("esc")
    assert g.mode == "play"


def test_facing_arrows_show_where_enemies_look():
    from rotengine.ui.game import GameScreen
    scenario = {"id": "yard", "name": "Yard", "start_alert": False, "levels": [["," * 12] * 5],
                "teams": {"you": [{"creature": "operative", "at": [2, 2, 0]}],
                          "them": [{"creature": "sentry", "at": [6, 2, 0], "facing": [1, 0]}]}}
    app = new_app()
    content = app.content_for(scenario)
    sim = arena.build(scenario, content, seed=1)
    app.screen = g = GameScreen(app, scenario, content, sim, sim.creatures[0], 1)
    row = [line for line in screen_text(app).splitlines() if "@" in line and "→" in line]
    assert row and row[0].index("→") == row[0].index("g") + 1  # the sentry (g) looks east, away from you


def test_a_bullet_in_the_air_is_drawn():
    from rotengine import combat, flight, perception
    from rotengine.ui.game import GameScreen
    scenario = {"id": "range", "name": "Range", "levels": [["," * 30] * 5],
                "teams": {"you": [{"creature": "operative", "at": [2, 2, 0]}],
                          "them": [{"creature": "soldier", "at": [25, 2, 0]}]}}
    app = new_app()
    content = app.content_for(scenario)
    sim = arena.build(scenario, content, seed=1)
    app.screen = g = GameScreen(app, scenario, content, sim, sim.creatures[0], 1)
    shooter, p = sim.creatures[1], g.player
    perception.make_all_aware(sim)
    plan = next(x for x in combat.attack_plans(sim, shooter, p, allow_aim=False)
                if x.attack["kind"] == "ranged")
    before = screen_text(app).count("*")
    combat.resolve_attack(sim, shooter, p, plan)
    sim.time += 10  # a few tiles out of the barrel
    assert any(f.kind == "tracer" for f in flight.active(sim))
    assert screen_text(app).count("*") > before
