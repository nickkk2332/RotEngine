from rotengine.body import Body


def body(content, hp=10):
    return Body(content.get("body_plan", "humanoid"), hp)


def test_skull_multiplier(content):
    b = body(content)
    inj = b.wound("skull", 6, 2, content.get("damage_type", "pierce"))
    assert inj.injury == 16  # (6 - 2) x4
    assert b.hp == -6
    assert inj.newly_destroyed  # 1.6 x HP through the skull: brain destroyed


def test_dr_stops_damage(content):
    b = body(content)
    inj = b.wound("torso", 10, 25, content.get("damage_type", "pierce"))
    assert not inj.penetrated and b.hp == 10


def test_limb_crippling_caps_hp_loss(content):
    b = body(content, hp=10)
    inj = b.wound("r_arm", 30, 0, content.get("damage_type", "pierce"))
    assert inj.newly_crippled
    assert b.hp == 10 - 6  # only (HP/2)+1 counts against HP for a limb
    assert not inj.newly_destroyed  # piercing does not sever


def test_cutting_severs_and_takes_children(content):
    b = body(content, hp=10)
    inj = b.wound("r_arm", 10, 0, content.get("damage_type", "cut"))
    assert inj.newly_destroyed
    assert b.part("r_hand").destroyed and "right hand" in inj.lost
    assert b.bleed_rate > 1.0  # arterial


def test_crippled_arm_disables_hand(content):
    b = body(content, hp=10)
    b.wound("r_arm", 6, 0, content.get("damage_type", "pierce"))
    assert not b.part("r_hand").crippled
    assert not b.is_functional("r_hand")
    assert [p.id for p in b.functional_with("grasp")] == ["l_hand"]


def test_blunt_force_fractures_instead_of_bleeding(content):
    b = body(content, hp=10)
    inj = b.wound("torso", 6, 0, content.get("damage_type", "crush"))
    assert inj.newly_fractured and b.part("torso").fractured
    assert inj.internal  # broken ribs, bruised organs
    cut = body(content, hp=10)
    cut_inj = cut.wound("torso", 6, 0, content.get("damage_type", "cut"))
    assert not cut_inj.newly_fractured
    assert cut.bleed_rate > b.bleed_rate * 5


def test_organs_can_be_destroyed_by_any_damage(content):
    b = body(content, hp=10)
    inj = b.wound("vitals", 5, 0, content.get("damage_type", "pierce"))  # x3 = 15
    assert inj.newly_destroyed and inj.internal
    assert inj.part.data["destroy_status"] == "cardiac_arrest"


def test_hit_locations_follow_weights(content):
    import random
    b = body(content)
    rng = random.Random(0)
    hits = [b.roll_location(rng) for _ in range(3000)]
    assert hits.count("torso") > hits.count("skull") * 5
    assert "vitals" in hits
