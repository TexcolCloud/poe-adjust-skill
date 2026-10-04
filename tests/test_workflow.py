"""Offline behavioral tests, including the installed real calculation engine.

Run with TeleAgent's Python: python -m unittest discover -s tests -v
The user's XML and installed application files are never edited.
"""
import argparse
import base64
import copy
from datetime import date, datetime, timedelta, timezone
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "poe-adjust/scripts"))
import buildxml as bx
import engine
import facts
import poe
import routes

SAMPLE = '''<PathOfBuilding><Build level="86" className="Duelist" targetVersion="3_0" mainSocketGroup="1"/><Tree activeSpec="1"><Spec treeVersion="3_29" classId="4" ascendClassId="3" nodes="1,2"/></Tree><Skills activeSkillSet="2"><SkillSet id="1"><Skill enabled="true"><Gem skillId="Fireball" level="1" quality="0"/></Skill></SkillSet><SkillSet id="2"><Skill enabled="true" slot="Boots"><Gem skillId="ShieldCrush" gemId="stale" variantId="stale" level="20" quality="0"/></Skill></SkillSet></Skills><Items activeItemSet="1"><Item id="1">Rarity: NORMAL\nIron Greaves\nSockets: R-R-R-R</Item><ItemSet id="1"><Slot name="Boots" itemId="1"/></ItemSet></Items><Config/><Extension provider="fixture">preserve me</Extension></PathOfBuilding>'''


def args(**kwargs):
    return argparse.Namespace(pob=None, **kwargs)


def intent_for(root):
    return {"confirmed": True, "goal": "离线测试：保留盾牌并验证两种改装流程", "league": "国服测试赛季", "mode": "both",
            "game_version": "3.29", "version_source": "https://poe.qq.com/webplat/info/news_version3/22557/22558/22601/m14525/202607/988692.shtml", "version_checked_at": "2026-10-04",
            "budget": {"amount": 20, "currency": "divine"}, "target_level": 86,
            "allow_main_skill_change": True, "allow_ascendancy_change": True, "keep_slots": ["Weapon 2"],
            "scenario": {"enemyLevel": 83}, "baseline_main_group": 1,
            "quest_passive_points": 23, "ascendancy_points": 8}


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.root = bx.parse(SAMPLE)

    def test_engine_metadata_accepts_new_releases_without_allowlist(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "manifest.xml").write_text('<PoBVersion><Version number="9.1.0"/></PoBVersion>', encoding="utf-8")
            (root / "GameVersions.lua").write_text('treeVersionList = {"3_29", "3_30_ruthless", "3_30"}', encoding="utf-8")
            (root / "changelog.txt").write_text('VERSION[9.1.0][2027/01/01]\nfixes\nVERSION[9.0.0]\n--- 3.30 - Example ---\n', encoding="utf-8")
            self.assertEqual(engine.metadata(root)["data_version"], "3.30")
            before = engine.fingerprint(root)
            (root / "Modules").mkdir()
            data = root / "Modules/Data.lua"
            data.write_text("value = 10", encoding="utf-8")
            self.assertNotEqual(before, engine.fingerprint(root))
            before = engine.fingerprint(root)
            stamp = data.stat()
            data.write_text("value = 11", encoding="utf-8")
            os.utime(data, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            self.assertNotEqual(before, engine.fingerprint(root))
            (root / "GameVersions.lua").write_text('treeVersionList = {"3_31"}', encoding="utf-8")
            self.assertIsNone(engine.metadata(root)["data_version"])
            with self.assertRaisesRegex(ValueError, "不能一致确认"):
                engine.require_data_version(engine.metadata(root))
            import shutil
            clone = root / "relocated"
            shutil.copytree(root, clone, ignore=shutil.ignore_patterns("relocated"))
            self.assertEqual(engine.fingerprint(root), engine.fingerprint(clone))

    def test_missing_native_contract_is_not_a_zero_value(self):
        with self.assertRaisesRegex(ValueError, "resource_rules"):
            engine.validate_contract({"stats": {"Life": 100}})

    def test_saved_snapshot_has_exact_content_digest_on_windows(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshot.xml"
            text = bx.serialise(self.root)
            poe.write_text(path, text)
            self.assertEqual(bx.digest(path.read_bytes()), bx.digest(text))

    def test_diagnosis_preserves_native_damage_and_distinguishes_resource_pools(self):
        result = {"main_skill_name": "Spectral Shield Throw", "stats": {"CombinedDPS": 33770, "HitChance": 56, "ManaUnreserved": -275}}
        summary = poe.diagnosis_summary(result)
        self.assertIn("主技能综合 DPS：33770", summary)
        self.assertNotIn("18911", summary)
        self.assertIn("不再乘命中率", summary)
        self.assertIn("不能据此断言所有技能都不能使用", summary)
        result["active_skills"] = [{"is_main": True, "minion": True}]
        result["stats"].update(MinionCombinedDPS=1234, MinionHitChance=95)
        summary = poe.diagnosis_summary(result)
        self.assertIn("召唤物单体综合 DPS：1234", summary)
        self.assertIn("召唤物命中率 %：95", summary)
        self.assertNotIn("33770", summary)

    def test_inspection_never_overwrites_input(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "原始角色.xml"
            path.write_text(SAMPLE, encoding="utf-8")
            original = path.read_bytes()
            with redirect_stderr(io.StringIO()):
                code = poe.main(["inspect", "--input", str(path), "--out", str(path)])
            self.assertEqual(code, 2)
            self.assertEqual(path.read_bytes(), original)

    def test_patch_uses_active_set_and_clears_stale_identifiers(self):
        plan = {"mode": "refine", "summary": "test", "operations": {"gems": [{"group": 1, "index": 1, "skill_id": "Fireball", "level": 3}]}}
        changed = bx.apply_plan(self.root, plan)
        gem = bx.groups(changed)[0].find("Gem")
        self.assertEqual(gem.get("skillId"), "Fireball")
        self.assertNotIn("gemId", gem.attrib)
        self.assertEqual(changed.find("Skills/SkillSet/Skill/Gem").get("level"), "1")
        self.assertEqual(bx.groups(self.root)[0].find("Gem").get("skillId"), "ShieldCrush")
        self.assertEqual(changed.find("Extension").text, "preserve me")

    def test_mastery_delta_preserves_others_and_full_replacement_cannot_drop_selections(self):
        bx.active_spec(self.root).set("masteryEffects", "{1,11},{2,22}")
        plan = {"mode": "refine", "summary": "只增加一个专精", "operations": {"tree": {"nodes_add": [3], "masteries_add": {"3": 33}}}}
        changed = bx.apply_plan(self.root, plan)
        self.assertEqual(bx.active_spec(changed).get("masteryEffects"), "{1,11},{2,22},{3,33}")
        changed = bx.apply_plan(changed, {**plan, "operations": {"tree": {"nodes_remove": [2], "masteries_add": {"1": 12}}}})
        self.assertEqual(bx.active_spec(changed).get("masteryEffects"), "{1,12},{3,33}")
        self.assertEqual(bx.active_spec(self.root).get("masteryEffects"), "{1,11},{2,22}")
        with self.assertRaisesRegex(ValueError, "完整替换.*其他专精"):
            bx.apply_plan(self.root, {**plan, "operations": {"tree": {"masteries": {"1": 12}}}})
        with self.assertRaisesRegex(ValueError, "未分配"):
            bx.apply_plan(self.root, {**plan, "operations": {"tree": {"masteries_add": {"3": 33}}}})
        changed = bx.apply_plan(self.root, {**plan, "operations": {"tree": {"masteries": {"1": 12, "2": 22}}}})
        self.assertEqual(bx.active_spec(changed).get("masteryEffects"), "{1,12},{2,22}")

    def test_rebuild_replaces_only_selected_skill_set(self):
        result = bx.apply_plan(self.root, {"mode": "rebuild", "summary": "switch", "operations": {"replace_skills": [{"slot": "Boots", "gems": [{"skill_id": "Fireball", "level": 10}]}]}})
        self.assertEqual(len(result.findall("Skills/SkillSet")), 2)
        self.assertEqual(bx.groups(result)[0].find("Gem").get("level"), "10")
        self.assertEqual(result.find("Skills/SkillSet/Skill/Gem").get("level"), "1")

    def test_rejects_entities_dangling_ids_and_wrong_active_set(self):
        for text in (SAMPLE.replace("<PathOfBuilding>", '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///secret">]><PathOfBuilding>'), SAMPLE.replace('itemId="1"', 'itemId="2"'), SAMPLE.replace('activeSkillSet="2"', 'activeSkillSet="9"')):
            with self.subTest(text=text[:80]), self.assertRaises(ValueError): bx.parse(text)

    def test_share_code_and_chinese_filename(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "角色 分享码.txt"
            path.write_text(base64.urlsafe_b64encode(zlib.compress(SAMPLE.encode())).decode().rstrip("="), encoding="utf-8")
            self.assertEqual(bx.read_document(path).tag, "PathOfBuilding")

    def test_scenario_clears_inherited_damage_conditions(self):
        bx.set_config(self.root, {"conditionOnslaught": True, "bandit": "Alira"})
        bx.set_config(self.root, {"enemyLevel": 83}, reset=True)
        names = {node.get("name") for node in bx.active_config(self.root)}
        self.assertEqual(names, {"enemyLevel", "bandit"})
        with self.assertRaises(ValueError): bx.set_config(self.root, {"customMods": "1000% more Damage"})

    def test_intent_requires_user_direction_and_valid_budget(self):
        intent = intent_for(self.root)
        intent["confirmed"] = False
        with self.assertRaises(ValueError): poe.validate_intent(intent, self.root)
        intent["confirmed"] = True
        intent["budget"]["amount"] = float("inf")
        with self.assertRaises(ValueError): poe.validate_intent(intent, self.root)

    def test_budget_keeps_unknown_and_foreign_prices_unverified(self):
        intent = {"budget": {"amount": 20, "currency": "divine"}, "league": "国服测试赛季"}
        result = poe.budget_summary({"shopping": [{"description": "盾", "status": "unknown"}]}, intent)
        self.assertEqual(result["status"], "unknown")
        with self.assertRaises(ValueError):
            poe.budget_summary({"shopping": [{"description": "盾", "status": "verified_cn", "low": 1, "high": 2, "currency": "divine", "source": "https://example.com", "checked_at": "2026-10-04", "league": "国际服"}]}, intent)

    def test_intent_does_not_assume_character_progress(self):
        for field in ("target_level", "baseline_main_group", "quest_passive_points", "ascendancy_points"):
            intent = intent_for(self.root)
            del intent[field]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                poe.validate_intent(intent, self.root)

    def test_329_socket_layout_ignores_colour_but_rejects_missing_links(self):
        result = {"equipped": [{"slot": "Boots", "id": 1}],
                  "gems": [{"group": 1, "index": i, "color": "G", "enabled": True, "support": i > 1} for i in range(1, 5)]}
        self.assertEqual(poe.socket_layout_issues(self.root, result), [])
        self.root.find("Items/Item").text = "Rarity: NORMAL\nIron Greaves\nSockets: W-W W-W"
        self.assertTrue(any("连线不匹配" in error for error in poe.socket_layout_issues(self.root, result)))
        result["gems"].append({"group": 1, "index": 5})
        self.assertTrue(any("数量不足" in error for error in poe.socket_layout_issues(self.root, result)))

    def test_shared_link_groups_cannot_hide_support_effects(self):
        changed = bx.apply_plan(self.root, {"mode": "rebuild", "summary": "two groups", "operations": {"replace_skills": [
            {"slot": "Boots", "gems": [{"skill_id": "ShieldCrush"}, {"skill_id": "SupportLifetap"}]},
            {"slot": "Boots", "gems": [{"skill_id": "Determination"}]}]}})
        result = {"equipped": [{"slot": "Boots", "id": 1}], "gems": [
            {"group": 1, "index": 1}, {"group": 1, "index": 2, "enabled": True, "support": True}, {"group": 2, "index": 1}]}
        self.assertTrue(any("共享物理连线" in error for error in poe.socket_layout_issues(changed, result)))
        bx.groups(changed)[0].set("enabled", "false")
        poe.release_inactive_sockets(changed)
        self.assertEqual(bx.groups(changed)[0].get("slot"), "")
        self.assertEqual(poe.socket_layout_issues(changed, result), [])

    def test_season_and_engine_version_mismatch_cannot_be_certified(self):
        intent = intent_for(self.root)
        for league in ("国服 S29", "国服S29", "第29赛季", "沙海幻境"):
            intent["league"] = league
            with self.assertRaisesRegex(ValueError, "S29.*对应 3.28"):
                poe.validate_intent(intent, self.root)
        with self.assertRaisesRegex(ValueError, "机制/宝石数据"):
            engine.validate_game_version("3.28", "历史赛季", "3.29")
        del intent["game_version"]
        with self.assertRaisesRegex(ValueError, "game_version"):
            poe.validate_intent(intent, self.root)
        intent["league"], intent["game_version"] = "国服 S30", "3.29"
        self.assertEqual(poe.validate_intent(intent, self.root)["game_version"], "3.29")
        intent["version_source"] = engine.VERSION_EVIDENCE["3.28"]["url"]
        with self.assertRaisesRegex(ValueError, "官方公告实际为 S29 / 3.28"):
            poe.validate_intent(intent, self.root)

    def test_tree_delta_preserves_unrelated_nodes_and_rejects_conflict(self):
        plan = {"mode": "refine", "summary": "delta", "operations": {"tree": {"nodes_add": [3, 4], "nodes_remove": [2]}}}
        result = bx.apply_plan(self.root, plan)
        self.assertEqual(bx.active_spec(result).get("nodes"), "1,3,4")
        self.assertEqual(bx.active_spec(self.root).get("nodes"), "1,2")
        for changes in ({"nodes": [1], "nodes_add": [3]}, {"nodes_remove": [9]}, {"nodes_add": [2], "nodes_remove": [2]}):
            plan["operations"]["tree"] = changes
            with self.assertRaises(ValueError): bx.apply_plan(self.root, plan)

    def test_recipe_rejects_text_injection_or_invalid_roll_before_engine(self):
        item = {"id": 20, "craft": {"base": "Iron Greaves", "ilvl": 86, "sockets": "W-W-W-W", "affixes": [{"id": "IncreasedLife8", "roll": 2}]}}
        with self.assertRaisesRegex(ValueError, "roll"):
            poe.materialize_items({"operations": {"items": [item]}}, None)
        item["craft"]["affixes"][0]["roll"] = 0.5
        item["craft"]["sockets"] = "W-W\n+1000 to maximum Life"
        with self.assertRaisesRegex(ValueError, "sockets"):
            poe.materialize_items({"operations": {"items": [item]}}, None)
        item["text"] = "arbitrary text"
        with self.assertRaisesRegex(ValueError, "混入 text"):
            poe.materialize_items({"operations": {"items": [item]}}, None)
        duplicate = {"id": 20, "text": "Rarity: RARE\nTarget\nIron Greaves"}
        with self.assertRaisesRegex(ValueError, "id 不能重复"):
            bx.apply_plan(self.root, {"mode": "refine", "summary": "test", "operations": {"items": [duplicate, duplicate]}})

    def test_readiness_blocks_forgotten_ascendancy_and_temporary_defence(self):
        result = {"level": 86, "points": 108, "ascendancy_points": 6,
                  "stats": {"GuardSkillActive": True}, "equipped": [{"slot": "Flask 1", "active": True}]}
        audit = poe.readiness(result, intent_for(self.root), {})
        self.assertTrue(any("2 个可用升华" in e for e in audit["errors"]))
        self.assertTrue(any("守护" in e for e in audit["errors"]))
        self.assertTrue(any("Flask 1" in e for e in audit["errors"]))
        result["stats"]["GuardSkillActive"] = False
        result["equipped"][0]["active"] = False
        result["ascendancy_points"] = 8
        self.assertEqual(poe.readiness(result, intent_for(self.root), {})["errors"], [])

    def test_burst_requires_time_limit_and_reports_recovery_downtime(self):
        result = {"level": 86, "points": 108, "ascendancy_points": 8, "equipped": [],
                  "stats": {"ManaUnreserved": 300, "ManaCost": 30, "ManaPerSecondCost": 60, "ManaRegenRecovery": 10}}
        intent = intent_for(self.root)
        self.assertTrue(poe.readiness(result, intent, {})["errors"])
        intent["resource_mode"] = "burst"
        self.assertTrue(poe.readiness(result, intent, {"burst_seconds": 7})["errors"])
        audit = poe.readiness(result, intent, {"burst_seconds": 4})
        self.assertEqual(audit["errors"], [])
        mana = audit["resources"][0]
        self.assertEqual(mana["maximum_burst_seconds"], 6)
        self.assertEqual(mana["recovery_seconds_at_regen_only"], 20)
        self.assertAlmostEqual(mana["duty_cycle"], 1 / 6)

    def test_gem_upgrade_and_duplicate_gems_are_not_free_owned_inventory(self):
        old = {"gems": [{"id": "SupportBrutality", "level": 19, "quality": 0}], "equipped": []}
        new = {"gems": [{"id": "SupportBrutality", "name": "Brutality", "level": 20, "quality": 0, "group_enabled": True}] * 2, "equipped": []}
        needs = poe.material_requirements(self.root, self.root, old, new)
        self.assertEqual(len(needs), 1)
        self.assertEqual(needs[0]["quantity"], 2)
        self.assertIn("19", needs[0]["description"])
        plan = {"shopping": [{**needs[0], "status": "owned"}]}
        with self.assertRaisesRegex(ValueError, "不能直接标 owned"):
            poe.budget_summary(plan, intent_for(self.root), needs)
        plan = {"shopping": [{"description": "原角色已有全部宝石", "status": "owned"}]}
        self.assertEqual(poe.budget_summary(plan, intent_for(self.root), needs)["status"], "unknown")

    def test_energy_shield_costs_recharge_conditions_and_self_degen(self):
        result = {"level": 86, "points": 108, "ascendancy_points": 8, "equipped": [],
                  "resource_rules": {"energy_shield_protects_mana": True},
                  "stats": {"ManaUnreserved": 0, "ManaCost": 20, "ESPerSecondCost": 100, "EnergyShield": 300,
                            "EnergyShieldRecharge": 120, "EnergyShieldRechargeDelay": 2, "EnergyShieldRechargeAppliesToEnergyShield": True}}
        intent = intent_for(self.root)
        audit = poe.readiness(result, intent, {})
        self.assertEqual(audit["errors"], [])
        self.assertEqual(audit["resources"][-1]["recharge"], 120)
        self.assertEqual(audit["resources"][-1]["net_cost"], -20)
        result["stats"]["sharedMindOverMatter"] = 40
        self.assertTrue(any("能量护盾" in error for error in poe.readiness(result, intent, {})["errors"]))
        intent["calculation_mode"] = "conditional"
        plan = {"config_evidence": {"energy_shield_recharge": "仅验证充能开启期间；伤害打断后停止满速使用，等待恢复。"}}
        self.assertEqual(poe.readiness(result, intent, plan)["errors"], [])
        result["stats"]["EnergyShield"] = 30
        self.assertTrue(any("启动延迟" in error for error in poe.readiness(result, intent, plan)["errors"]))
        result["resource_rules"]["energy_shield_protects_mana"] = False
        result["stats"] = {"Life": 1000, "LifeRegenRecovery": 100, "NetLifeRegen": -200, "TotalBuildDegen": 300}
        self.assertTrue(any("净秒回 -200.0" in error for error in poe.readiness(result, intent, {})["errors"]))
        result["stats"] = {"EnergyShield": 300, "ESCost": 80, "ESPerSecondCost": 100, "EnergyShieldRegenRecovery": 50}
        self.assertTrue(any("能量护盾" in error for error in poe.readiness(result, intent, {})["errors"]))

    def test_incomparable_conditions_do_not_emit_gain_percentage(self):
        before, after = {"stats": {"CombinedDPS": 10}}, {"stats": {"CombinedDPS": 30}}
        self.assertIsNone(poe.compare(before, after, False)["CombinedDPS"]["percent"])
        self.assertEqual(poe.compare(before, after)["CombinedDPS"]["percent"], 200)

    def test_automatic_trigger_costs_are_added_once_and_corpse_dependencies_explained(self):
        result = {"level": 86, "points": 108, "ascendancy_points": 8, "equipped": [],
                  "stats": {"ManaUnreserved": 100, "ManaCost": 10, "ManaPerSecondCost": 20, "ManaRegenRecovery": 30},
                  "active_skills": [{"id": "FixtureMain", "name": "Main", "is_main": True, "trigger": "Fixture", "costs": {"ManaPerSecondCost": 20, "SkillTriggerRate": 2}},
                                    {"id": "FixtureTrigger", "name": "Auto", "trigger": "Fixture", "costs": {"ManaCost": 30, "ManaPerSecondCost": 60, "SkillTriggerRate": 2}},
                                    {"id": "FixtureDisabled", "name": "Disabled", "disabled": True, "trigger": "Fixture", "costs": {"ManaPerSecondCost": 999, "SkillTriggerRate": 2}}]}
        intent = intent_for(self.root)
        audit = poe.readiness(result, intent, {})
        self.assertEqual(audit["resources"][0]["cost_per_second"], 80)
        self.assertEqual(audit["resources"][0]["automatic_cost_per_second"], 60)
        self.assertTrue(any("净缺口 50.0" in error for error in audit["errors"]))
        result["stats"]["ManaRegenRecovery"] = 100
        result["active_skills"][0]["generals_cry"] = True
        result["capabilities"] = {"native_corpse_sources": True}
        audit = poe.readiness(result, intent, {})
        self.assertTrue(any("config_evidence.generals_cry" in error for error in audit["errors"]))
        self.assertTrue(any("尸体生成" in error for error in audit["errors"]))
        source = {"id": "Desecrate", "name": "Desecrate", "creates_corpses": False, "costs": {"ManaCost": 20}}
        result["active_skills"].append(source)
        plan = {"config_evidence": {"generals_cry": "先手动亵渎备尸，再自动战吼；按实际尸体与冷却维持幻影。本人依靠回复，额外施放需预留魔力。"}}
        self.assertTrue(any("尸体生成" in error for error in poe.readiness(result, intent, plan)["errors"]))
        source.update(id="FixtureNewCorpseSource", name="Fixture Source", creates_corpses=True, disabled=True)
        self.assertTrue(any("尸体生成" in error for error in poe.readiness(result, intent, plan)["errors"]))
        source["disabled"] = False
        result["capabilities"]["native_corpse_sources"] = False
        self.assertTrue(any("接口无法读取" in error for error in poe.readiness(result, intent, plan)["errors"]))
        result["capabilities"]["native_corpse_sources"] = True
        self.assertEqual(poe.readiness(result, intent, plan)["errors"], [])
        result["gems"] = [{"id": "FixtureMain", "enabled": True, "group_enabled": True, "count": 20}]
        self.assertTrue(any("skill_counts" in error for error in poe.readiness(result, intent, plan)["errors"]))
        plan["config_evidence"]["skill_counts"] = "测试数量依据；实际路线需按持续时间、召唤速率与资源计算。"
        self.assertEqual(poe.readiness(result, intent, plan)["errors"], [])
        result["stats"].update({"ManaRegenRecovery": 0, "ManaLeechGainRate": 100})
        audit = poe.readiness(result, intent, plan)
        self.assertEqual(audit["resources"][0]["leech_on_hit"], 0)
        self.assertEqual(audit["resources"][0]["native_leech_on_hit"], 100)
        self.assertTrue(any("净缺口 80.0" in error for error in audit["errors"]))



def route_fixture():
    return {"name": "离线开荒路线测试", "confirmed": True, "goal": "验证分阶段推荐的限制", "league": "测试赛季", "class_id": 4,
            "game_version": "3.29",
            "ascendancy": "Champion", "trade_mode": "ssf", "allow_minions": False, "allow_mule": False, "budget": {"amount": 0, "currency": "divine"}, "version_status": "unverified",
            "sources": [{"id": "gem", "title": "测试来源", "url": "https://example.org/gem", "checked_at": "2026-10-04", "version": "测试数据"}],
            "gems": {"ShieldCrush": {"acquisition": "vendor", "source_id": "gem", "source_note": "仅验证文档结构，不作为实际领取资料。", "available_from_act": 1, "class_ids": list(range(7))}},
            "stages": [{"name": "测试阶段", "level": level, "act": act, "weapon_setup": "one_hand_shield",
                        "skill_groups": [{"name": "主技能", "slot": "Gloves", "use": "main", "gems": [{"skill_id": "ShieldCrush", "level": 1}]}],
                        "gear": ["盾牌"], "passives": ["生命"], "ascendancy_points": 0, "ascendancy_nodes": [], "resources": "核对消耗与魔力药剂",
                        "checks": ["属性需求满足"], "transition": ["下一阶段核心技能准备好"]} for level, act in ((1, 1), (12, 2), (68, 11))]}


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.route = route_fixture()
        self.gems = {"ShieldCrush": {"skill_id": "ShieldCrush", "name": "Shield Crush", "min_level": 1, "tags": {"grants_active_skill": True},
                                    "level_requirements": [{"gem_level": 1, "character_level": 1}, {"gem_level": 20, "character_level": 70}]}}

    def test_route_rejects_gem_level_unavailable_at_milestone(self):
        self.route["stages"][-1]["skill_groups"][0]["gems"][0]["level"] = 20
        with self.assertRaisesRegex(ValueError, "需要角色 70 级"):
            routes.validate(self.route, self.gems)

    def test_route_auto_levels_resolve_without_changing_input_and_report_all_errors(self):
        self.gems["ShieldCrush"]["level_requirements"] += [{"gem_level": 3, "character_level": 12}, {"gem_level": 18, "character_level": 68}]
        for stage in self.route["stages"]: stage["skill_groups"][0]["gems"][0]["level"] = "auto"
        original = copy.deepcopy(self.route)
        checked, _ = routes.validate(self.route, self.gems)
        self.assertEqual([stage["skill_groups"][0]["gems"][0]["level"] for stage in checked["stages"]], [1, 3, 18])
        self.assertEqual(self.route, original)
        for stage in self.route["stages"]: stage["skill_groups"][0]["gems"][0]["level"] = 70
        with self.assertRaises(ValueError) as caught: routes.validate(self.route, self.gems)
        for label in ("1 级阶段", "12 级阶段", "68 级阶段", "最高可练到宝石 18 级"):
            self.assertIn(label, str(caught.exception))
        self.route["stages"][0]["skill_groups"][0]["gems"][0]["level"] = "auto"
        self.gems["ShieldCrush"]["min_level"] = 2
        self.gems["ShieldCrush"]["level_requirements"][0]["character_level"] = 2
        with self.assertRaisesRegex(ValueError, "最早需要角色 2 级"):
            routes.validate(self.route, self.gems)

    def test_route_rejects_wrong_ascendancy_and_incompatible_support(self):
        classes = [{"id": 4, "ascendancies": [{"name": "Champion"}]}]
        self.route["ascendancy"] = "Elementalist"
        with self.assertRaisesRegex(ValueError, "升华不属于"):
            routes.validate(self.route, self.gems, "3.29", classes)
        self.route["ascendancy"] = "Champion"
        links = {"ShieldCrush": {"incompatible": ["SupportGreaterMultipleProjectiles"]}}
        with self.assertRaisesRegex(ValueError, "以下辅助不支持"):
            routes.validate(self.route, self.gems, "3.29", classes, links)
        links["ShieldCrush"] = {"incompatible": [], "errors": ["Fixture native query failure"], "valid": False}
        with self.assertRaisesRegex(ValueError, "原生连线查询无效"):
            routes.validate(self.route, self.gems, "3.29", classes, links)

    def test_route_enforces_recorded_acquisition_chapter_class_and_player_preferences(self):
        acquisition = self.route["gems"]["ShieldCrush"]
        acquisition["available_from_act"] = 3
        with self.assertRaisesRegex(ValueError, "早于已记录的第 3 章"):
            routes.validate(self.route, self.gems)
        acquisition["available_from_act"], acquisition["class_ids"] = 1, [3]
        with self.assertRaisesRegex(ValueError, "职业范围不含当前职业"):
            routes.validate(self.route, self.gems)
        acquisition["via_mule"] = True
        with self.assertRaisesRegex(ValueError, "未允许另建小号"):
            routes.validate(self.route, self.gems)
        self.route["allow_mule"] = True
        routes.validate(self.route, self.gems)
        self.gems["ShieldCrush"]["tags"]["minion"] = True
        with self.assertRaisesRegex(ValueError, "用户选择不使用召唤"):
            routes.validate(self.route, self.gems)
        self.route["allow_minions"] = True
        routes.validate(self.route, self.gems)

    def test_ssf_does_not_depend_on_trade_or_random_core_without_fallback(self):
        self.route["gems"]["ShieldCrush"]["acquisition"] = "trade"
        with self.assertRaisesRegex(ValueError, "不能依赖交易宝石"):
            routes.validate(self.route, self.gems)
        self.route["gems"]["ShieldCrush"]["acquisition"] = "drop"
        with self.assertRaisesRegex(ValueError, "fallback_skill_id"):
            routes.validate(self.route, self.gems)

    def test_route_keeps_unverified_version_and_does_not_invent_dps(self):
        checked, warnings = routes.validate(self.route, self.gems)
        self.assertTrue(any("国服赛季" in warning for warning in warnings))
        report = routes.render(checked, warnings, "2.67.2", "3_29")
        self.assertIn("尚未使用完整阶段装备", report)
        self.assertIn("宝石练级目标", report)
        self.route["version_status"] = "verified"
        with self.assertRaisesRegex(ValueError, "version_source"):
            routes.validate(self.route, self.gems)

    def test_five_link_has_an_obtainable_four_link_fallback(self):
        group = self.route["stages"][-1]["skill_groups"][0]
        group["gems"] *= 5
        with self.assertRaisesRegex(ValueError, "基础路线每组使用 1–4 连"):
            routes.validate(self.route, self.gems)

    def test_route_attributes_cover_utility_and_optional_links_without_summing(self):
        self.gems["ShieldCrush"]["level_requirements"][0].update(strength=33, dexterity=0, intelligence=0)
        for skill_id, active, attribute, amount in (("UtilityFixture", True, "intelligence", 80), ("SupportFixture", False, "dexterity", 111)):
            self.gems[skill_id] = {"skill_id": skill_id, "name": skill_id, "min_level": 1,
                                   "tags": {"grants_active_skill": active},
                                   "level_requirements": [{"gem_level": 1, "character_level": 1, attribute: amount}]}
            self.route["gems"][skill_id] = copy.deepcopy(self.route["gems"]["ShieldCrush"])
        stage = self.route["stages"][-1]
        stage["skill_groups"].append({"name": "辅助操作", "slot": "Boots", "use": "utility", "gems": [{"skill_id": "UtilityFixture", "level": 1}]})
        stage["skill_groups"][0]["upgrade_gems"] = [{"skill_id": "ShieldCrush", "level": 1}, {"skill_id": "SupportFixture", "level": 1}]
        checked, warnings = routes.validate(self.route, self.gems)
        self.assertEqual(checked["stages"][0]["attribute_requirements"]["gems"], {"strength": 33, "dexterity": 0, "intelligence": 0})
        self.assertEqual(checked["stages"][-1]["attribute_requirements"]["gems"], {"strength": 33, "dexterity": 0, "intelligence": 80})
        self.assertEqual(checked["stages"][-1]["attribute_requirements"]["upgrade_gems"], {"strength": 33, "dexterity": 111, "intelligence": 80})
        report = routes.render(checked, warnings, "2.67.2", "3_29")
        self.assertIn("可选连线升级后：力量 33、敏捷 111、智慧 80", report)
        self.assertIn("不代表角色已经满足", report)

    def test_route_physical_sockets_and_every_utility_link_are_checked(self):
        for index in range(5):
            skill_id = f"SupportFixture{index}"
            self.gems[skill_id] = {"skill_id": skill_id, "name": skill_id, "tags": {}, "level_requirements": [{"gem_level": 1, "character_level": 1}]}
            self.route["gems"][skill_id] = copy.deepcopy(self.route["gems"]["ShieldCrush"])
        group = self.route["stages"][-1]["skill_groups"][0]
        group["slot"] = "Body Armour"
        group["gems"] += [{"skill_id": f"SupportFixture{i}", "level": 1} for i in range(3)]
        group["upgrade_gems"] = copy.deepcopy(group["gems"]) + [{"skill_id": f"SupportFixture{i}", "level": 1} for i in (3, 4)]
        checked, _ = routes.validate(self.route, self.gems)
        self.assertEqual(len(checked["stages"][-1]["skill_groups"][0]["upgrade_gems"]), 6)
        other = copy.deepcopy(group)
        other["use"], other["slot"] = "utility", "Weapon 1"
        self.route["stages"][-1]["skill_groups"].append(other)
        with self.assertRaisesRegex(ValueError, "实际孔数"):
            routes.validate(self.route, self.gems)
        other["slot"] = "Body Armour"
        with self.assertRaisesRegex(ValueError, "总孔数超过容量"):
            routes.validate(self.route, self.gems)
        self.route["stages"][-1]["skill_groups"].pop()
        utility = {"name": "辅助操作", "use": "utility", "slot": "Boots", "gems": [{"skill_id": "ShieldCrush", "level": 1}, {"skill_id": "SupportFixture4", "level": 1}]}
        self.route["stages"][-1]["skill_groups"].append(utility)
        compatibility = {query: {"incompatible": []} for query in routes.link_queries(self.route)}
        compatibility["ShieldCrush,SupportFixture4"]["incompatible"] = ["SupportFixture4"]
        with self.assertRaisesRegex(ValueError, "以下辅助不支持"):
            routes.validate(self.route, self.gems, compatibility=compatibility)

    def test_research_dates_and_notes_are_not_fabricated_or_malformed(self):
        engine.validate_checked_at(date.today().isoformat())
        engine.validate_checked_at(datetime.now(timezone.utc).isoformat())
        for value in ((date.today() + timedelta(days=1)).isoformat(), (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()):
            with self.assertRaisesRegex(ValueError, "未来"):
                engine.validate_checked_at(value)
        with self.assertRaisesRegex(ValueError, "时区"):
            engine.validate_checked_at("2026-10-04T12:00:00")
        self.route["sources"][0]["checked_at"] = "2099-01-01"
        with self.assertRaisesRegex(ValueError, "未来"):
            routes.validate(self.route, self.gems)
        self.route["sources"][0]["checked_at"] = date.today().isoformat()
        self.route["notes"] = "应为数组"
        with self.assertRaisesRegex(ValueError, "数组"):
            routes.validate(self.route, self.gems)


@unittest.skipUnless(os.name == "nt", "需要 Windows 本机引擎")
class FactTests(unittest.TestCase):
    def setUp(self):
        self.raw = '''<title>Example - PoEDB</title><h5>Quest /2 </h5><table><tr><th>Act</th></tr>
        <tr><td>1</td><td>Opening quest</td><td><i title="Quest Reward"></i>Quest Reward</td><td>Marauder</td></tr>
        <tr><td>1</td><td>Opening quest</td><td><i title="Quest Vendor"></i>Nessa</td><td>Marauder · Templar</td></tr></table>'''
        self.general = "Siosa in Act 3 sells all unleveled skill gems that can be received as quest rewards for quests you've completed, across all classes. Lilly Roth in Act 6, 10 and Karui Shores sells all unleveled skill gems after completing the Act 6 quest Fallen from Grace."
        self.rows = facts.quest_rows(self.raw, "Example")

    def test_quest_and_vendor_rows_preserve_actual_class_scope(self):
        self.assertEqual(len(self.rows), 2)
        self.assertEqual(self.rows[0]["class_ids"], [1])
        self.assertEqual(self.rows[1]["class_ids"], [1, 5])
        self.assertEqual(self.rows[1]["npc"], "Nessa")
        self.assertEqual(facts.quest_rows(self.raw, "Different gem"), [])

    def test_all_classes_quest_rows_are_explicit_and_not_inferred_from_unknown_labels(self):
        raw = self.raw.replace("Marauder · Templar", "All Classes").replace("Marauder</td>", "All Classes</td>")
        rows = facts.quest_rows(raw, "Example")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["class_ids"] == list(range(7)) for row in rows))
        self.assertEqual(facts.quest_rows(raw.replace("All Classes", "Unknown class group"), "Example"), [])

    def test_universal_vendor_requires_both_live_rule_and_eligible_gem(self):
        rows = facts.universal_rows(self.rows, self.general)
        self.assertEqual([row["available_from_act"] for row in rows], [3, 6])
        self.assertEqual(facts.universal_rows([], self.general), [])
        self.assertEqual(facts.universal_rows(self.rows, "Unavailable page"), [])

    def test_acquisition_cannot_broaden_classes_or_invent_early_vendor(self):
        route = {"class_id": 1, "gems": {"Example": {"acquisition": "vendor", "available_from_act": 1, "class_ids": list(range(7))}}}
        proof = {"Example": {"status": "source_checked", "choices": self.rows, "sources": [{"url": "https://poedb.tw/us/Example"}]}}
        with self.assertRaisesRegex(ValueError, "扩大"): facts.reconcile_acquisitions(route, proof)
        route["gems"]["Example"]["class_ids"] = [1]
        route["class_id"] = 3
        with self.assertRaisesRegex(ValueError, "未对应唯一来源行"): facts.reconcile_acquisitions(route, proof)
        proof["Example"]["choices"] += facts.universal_rows(self.rows, self.general)
        route["gems"]["Example"].update(available_from_act=3, npc="Siosa", quest="A Fixture of Fate", source_note="outdated invented explanation")
        warnings, unresolved = facts.reconcile_acquisitions(route, proof)
        self.assertFalse(warnings or unresolved)
        self.assertNotIn("invented", route["gems"]["Example"]["source_note"])
        self.assertEqual(route["gems"]["Example"]["class_ids"], list(range(7)))

    def test_unknown_sources_stay_unknown(self):
        route = {"class_id": 1, "gems": {"Example": {"acquisition": "vendor", "available_from_act": 1}}}
        warnings, unresolved = facts.reconcile_acquisitions(route, {"Example": {"status": "unverified", "reason": "timeout"}})
        self.assertEqual(unresolved, ["Example"])
        self.assertIn("timeout", warnings[0])
        with patch.object(facts, "fetch", side_effect=OSError("offline")):
            result = facts.gem_facts({"Example": {"skill_id": "Example", "name": "Example", "min_level": 12,
                                                 "description": "Native fixture description", "tags": {"spell": True}}}, ".unused", "test")
        self.assertEqual(result["Example"]["status"], "unverified")
        self.assertEqual(result["Example"]["native"]["min_level"], 12)
        self.assertEqual(result["Example"]["native"]["description"], "Native fixture description")
        self.assertFalse(result["Example"]["choices"])

    def test_support_names_use_the_actual_source_page(self):
        record = {"html": self.raw.replace("Example -", "Example Support -"), "url": "https://poedb.tw/us/Example_Support", "checked_at": "2026-10-04", "sha256": "fixture"}
        with patch.object(facts, "fetch", return_value=record) as fetch:
            result = facts.gem_facts({"SupportExample": {"skill_id": "SupportExample", "name": "Example", "tags": {"support": True}}}, ".unused", "test")
        self.assertEqual(fetch.call_args.args[0], record["url"])
        self.assertEqual(result["SupportExample"]["status"], "source_checked")

    def test_source_league_marker_must_match_target_not_history_text(self):
        raw = '<a title="Example v3.30" class="league_bar_icon"></a>' + self.raw + '<p>Old 3.29 changes</p>'
        record = {"html": raw, "url": "https://poedb.tw/us/Example", "checked_at": "2026-10-04", "sha256": "fixture"}
        with patch.object(facts, "fetch", return_value=record):
            result = facts.gem_facts({"Example": {"skill_id": "Example", "name": "Example"}}, ".unused", "test", "3.29")
            self.assertEqual(result["Example"]["status"], "unverified")
            self.assertEqual(result["Example"]["source_game_version"], "3.30")
            result = facts.gem_facts({"Example": {"skill_id": "Example", "name": "Example"}}, ".unused", "test", "3.30")
            self.assertEqual(result["Example"]["status"], "source_checked")

    def test_version_evidence_excludes_related_news_and_requires_body(self):
        raw = '<nav>S31 3.30</nav><div id="newsDetail"><h2>S31 version</h2><div><p>在3.30.0版本中，测试挑战赛季开放。</p></div></div><footer>S30 3.29</footer>'
        record = {"html": raw, "url": "https://poe.qq.com/example", "checked_at": "2026-10-04", "sha256": "fixture"}
        with patch.object(facts, "fetch", return_value=record):
            result = facts.version_fact(record["url"], "3.30", "S31", ".unused", "test")
            self.assertEqual(result["status"], "source_observed")
            result = facts.version_fact(record["url"], "3.29", "S30", ".unused", "test")
            self.assertEqual(result["status"], "unverified")
            record["html"] = raw.replace("3.30.0", "3.20.0")
            self.assertEqual(facts.version_fact(record["url"], "3.30", "S31", ".unused", "test")["status"], "unverified")

    def test_cached_source_is_invalidated_by_context_and_content(self):
        with tempfile.TemporaryDirectory() as temp:
            import hashlib
            url = "https://poedb.tw/us/Example"
            text = "source"
            record = {"url": url, "html": text, "sha256": hashlib.sha256(text.encode()).hexdigest(), "checked_at": datetime.now(timezone.utc).isoformat(), "context": "old"}
            path = Path(temp) / (hashlib.sha256(url.encode()).hexdigest() + ".json")
            poe.write_json(path, record)
            with patch.object(facts, "urlopen", side_effect=TimeoutError("offline")) as fetch:
                self.assertEqual(facts.fetch(url, temp, "old"), record)
                self.assertEqual(fetch.call_count, 0)
                with self.assertRaises(TimeoutError): facts.fetch(url, temp, "updated-engine")
                record["html"] = "altered"
                poe.write_json(path, record)
                with self.assertRaises(TimeoutError): facts.fetch(url, temp, "old")

    def test_version_research_returns_short_body_evidence_without_native_calculation(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "version.json"
            url = engine.VERSION_EVIDENCE["3.29"]["url"]
            record = {"html": '<div id="newsDetail"><h2>S30 patch</h2><p>在3.29.0版本中，测试挑战赛季开放。</p><p>' + "other updates " * 20000 + '</p></div>',
                      "url": url, "checked_at": "2026-10-04", "sha256": "fixture"}
            info = {"data_version": "3.29", "engine_fingerprint": "fixture", "version_evidence": {"url": url}}
            with patch.object(engine, "resolve_root", return_value=Path(temp)), patch.object(engine, "check", return_value=info), \
                    patch.object(engine, "calculate", side_effect=AssertionError("version research needs no calculation")), patch.object(facts, "fetch", return_value=record):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = poe.main(["research", "--version", "--league", "国服S30", "--out", str(output)])
                self.assertEqual(code, 0)
                result = json.loads(stdout.getvalue())
                self.assertEqual(result["version_evidence"]["status"], "source_observed")
                self.assertIn("3.29.0", result["version_evidence"]["excerpt"])
                self.assertLess(len(stdout.getvalue()), 2400)
                self.assertNotIn("other updates", output.read_text(encoding="utf-8"))
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(poe.main(["research", "--version", "--league", "S29", "--out", str(output)]), 2)

    def test_new_season_research_accepts_new_source_and_preserves_missing_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "version.json"
            info = {"data_version": "3.30", "engine_fingerprint": "future", "version_evidence": None}
            command = ["research", "--version", "--league", "S31", "--out", str(output)]
            with patch.object(engine, "resolve_root", return_value=Path(temp)), patch.object(engine, "check", return_value=info):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(poe.main(command), 0)
                self.assertEqual(poe.read_json(output)["version_evidence"]["status"], "unverified")
                record = {"html": '<div id="newsDetail"><h2>S31 announcement</h2><p>在3.30.0版本中，新的挑战赛季开放。</p></div>',
                          "url": "https://poe.qq.com/new", "checked_at": "2026-10-04", "sha256": "fixture"}
                with patch.object(facts, "fetch", return_value=record), redirect_stdout(io.StringIO()):
                    self.assertEqual(poe.main(command + ["--source", record["url"]]), 0)
                self.assertEqual(poe.read_json(output)["version_evidence"]["status"], "source_observed")

    def test_trial_progress_uses_current_source_chapters_not_area_levels(self):
        raw = '<a class="league_bar_icon" title="Current v3.29"></a><table><tr><th>Name</th><th>Area</th><th>Level</th><th>Enchantment</th></tr>'
        for name, act, area in [("The Labyrinth", 3, 33), ("The Cruel Labyrinth", 7, 55), ("The Merciless Labyrinth", 10, 68)]:
            raw += f'<tr><td>{name}</td><td>Act {act} Trial Area</td><td>{area}</td><td>craft</td></tr>'
        raw += '</table><p>each reward you with two Ascendancy points</p>'
        record = {"html": raw, "url": "https://poedb.tw/us/The_Lords_Labyrinth", "checked_at": "2026-10-04", "sha256": "fixture"}
        with patch.object(facts, "fetch", return_value=record):
            proof = facts.trial_facts(".unused", "fixture", "3.29")
            self.assertEqual(proof["status"], "source_checked", proof)
            route = {"stages": [{"level": 42, "act": 6, "ascendancy_points": 4}]}
            with self.assertRaisesRegex(ValueError, "第 7 章"):
                facts.reconcile_trial_progress(route, proof)
            route["stages"][0]["act"] = 7
            self.assertEqual(facts.reconcile_trial_progress(route, proof), ([], False))
            self.assertEqual(route["stages"][0]["trial_requirements"][-1]["area_level"], 55)
            record["html"] = raw.replace("Act 7 Trial Area", "Act 8 Trial Area")
            changed = facts.trial_facts(".unused", "fixture", "3.29")
            with self.assertRaisesRegex(ValueError, "第 8 章"):
                facts.reconcile_trial_progress(route, changed)
            route["shared_trials"] = True
            self.assertEqual(facts.reconcile_trial_progress(route, changed), ([], False))
            self.assertEqual(facts.trial_facts(".unused", "fixture", "3.30")["status"], "unverified")
            self.assertTrue(facts.reconcile_trial_progress(route, {"status": "unverified"})[1])


class RealEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Offline engine tests use an explicit source-boundary stub, not live web.
        cls.fact_patch = patch.object(facts, "version_fact", return_value={"status": "source_observed", "scope": "offline source-boundary fixture"})
        cls.fact_patch.start()
        cls.addClassCleanup(cls.fact_patch.stop)
        try:
            cls.pob = engine.resolve_root(os.environ.get("POECHARM_PATH"))
            cls.sample = Path(os.environ["POE_TEST_XML"]) if os.environ.get("POE_TEST_XML") else cls.pob / "Builds/夷陵盾牌S29 原始配装.xml"
            if not cls.sample.is_file():
                raise ValueError("未找到验收用原始盾牌样本，请设置 POE_TEST_XML；不能随机使用其他构筑。")
        except ValueError as exc:
            raise unittest.SkipTest(str(exc))
        cls.original_hash = bx.digest(cls.sample.read_bytes())
        cls.settings_hash = bx.digest((cls.pob / "Settings.xml").read_bytes())
        cls.cache_hash = bx.digest((cls.pob / "Data/ModCache.lua").read_bytes())
        cls.temp = tempfile.TemporaryDirectory(prefix="poe-offline-")
        cls.run_dir = Path(cls.temp.name) / "中文 配装任务"
        poe.begin(args(invoked=True, input=str(cls.sample), run=str(cls.run_dir)))
        cls.original = bx.read_document(cls.sample)
        cls.intent = intent_for(cls.original)
        path = Path(cls.temp.name) / "intent.json"
        poe.write_json(path, cls.intent)
        poe.set_intent(args(run=str(cls.run_dir), file=str(path)))
        cls.refine = {"name": "离线局部优化", "mode": "refine", "summary": "关闭样本中额外保留的技能组并更换辅助。",
                      "operations": {"groups": [{"index": i, "enabled": False} for i in (3, 4, 6, 7, 8, 9, 10)],
                                     "gems": [{"group": 1, "index": 2, "skill_id": "SupportBrutality", "level": 19, "quality": 0},
                                              {"group": 1, "index": 4, "skill_id": "SupportManaLeech", "level": 20, "quality": 0},
                                              {"group": 2, "index": 3, "enable_global1": False}]},
                      "shopping": [{"description": "测试假定辅助宝石已备齐", "status": "owned"}],
                      "assumptions": ["仅离线行为验证，不作为正式配装推荐。"],
                      "migration_steps": ["先准备宝石，再调整开启的技能。"]}
        cls.rebuild = {"name": "离线完整换技能组", "mode": "rebuild", "summary": "以盾牌碾压完整替换原技能方案。",
                       "operations": {"replace_skills": [
                           {"slot": "Gloves", "include_in_full_dps": True, "gems": [
                               {"skill_id": "ShieldCrush", "level": 20}, {"skill_id": "SupportBrutality", "level": 19},
                               {"skill_id": "SupportLifetap", "level": 20}, {"skill_id": "SupportMeleePhysicalDamage", "level": 20}]},
                           {"slot": "Weapon 2", "gems": [{"skill_id": "Determination", "level": 19}]}], "main_group": 1},
                       "shopping": [{"description": "测试假定宝石已备齐", "status": "owned"}],
                       "assumptions": ["仅离线行为验证，不作为正式配装推荐。"],
                       "migration_steps": ["先准备全套技能宝石。", "核对盾牌与属性需求后一次切换技能。"]}
        for plan in (cls.refine, cls.rebuild):
            plan["operations"]["equip"] = [{"slot": slot.get("name"), "item_id": int(slot.get("itemId")), "active": False}
                                             for slot in bx.active_set(cls.original, "Items", "ItemSet", "activeItemSet").findall("Slot") if slot.get("name", "").startswith("Flask ")]
            plan["unused_points_reason"] = {"normal": "本夹具验证技能、物品与导出流程，保持输入天赋以隔离变量；不是完成配装推荐。",
                                             "ascendancy": "本夹具保持原升华，另有独立测试验证升华路线分配。"}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def evaluate_plan(self, plan, name):
        path = Path(self.temp.name) / (name + ".json")
        poe.write_json(path, plan)
        return poe.evaluate(args(run=str(self.run_dir), plan=str(path), name=name))

    def test_01_real_local_refine_export_and_source_preservation(self):
        result = self.evaluate_plan(self.refine, "refine")
        self.assertEqual(result["status"], "validated", result["errors"])
        self.assertTrue(result["roundtrip_verified"])
        self.assertTrue(any("negative" in warning for warning in result["baseline_warnings"]))
        self.assertNotEqual(result["comparison"]["CombinedDPS"]["delta"], 0)
        self.assertIsNone(result["comparison"]["CombinedDPS"]["percent"])
        output = poe.export(args(run=str(self.run_dir), name="refine"))
        for path in output["files"]: self.assertTrue(Path(path).is_file())
        self.assertIn("不提供同条件提升百分比", output["user_summary"])
        self.assertIn("不能承诺预算内买齐", output["user_summary"])
        evidence = poe.read_json(self.run_dir / "candidates/refine/evaluation.json")
        report = Path(output["files"][1]).read_text(encoding="utf-8")
        self.assertTrue(evidence["actual_changes"])
        self.assertIn("实际改动", report)
        self.assertEqual(evidence["validation"]["user_constraints"], "passed")
        with patch.object(facts, "version_fact", return_value={"status": "unverified", "reason": "offline"}):
            draft = poe.export(args(run=str(self.run_dir), name="refine"))
        self.assertEqual(draft["status"], "exported_draft")
        self.assertIn("待核实草案", draft["user_summary"])
        self.assertEqual(bx.digest(self.sample.read_bytes()), self.original_hash)
        self.assertEqual(bx.digest((self.pob / "Settings.xml").read_bytes()), self.settings_hash)
        self.assertEqual(bx.digest((self.pob / "Data/ModCache.lua").read_bytes()), self.cache_hash)

    def test_02_complete_rebuild_and_template_route(self):
        result = self.evaluate_plan(self.rebuild, "rebuild")
        self.assertEqual(result["status"], "validated", result["errors"])
        recorded = poe.read_json(self.run_dir / "candidates/rebuild/evaluation.json")
        self.assertEqual(recorded["result"]["main_skill"], "ShieldCrush")
        template = copy.deepcopy(self.rebuild)
        template["template_xml"] = str(self.run_dir / "candidates/rebuild/candidate.xml")
        template["operations"] = {"main_group": 1}
        template["sources"] = [{"title": "本地离线测试模板", "url": "https://github.com/PathOfBuildingCommunity/PathOfBuilding"}]
        result = self.evaluate_plan(template, "template")
        self.assertEqual(result["status"], "validated", result["errors"])

    def test_03_invalid_gem_is_not_silently_accepted(self):
        plan = copy.deepcopy(self.refine)
        plan["operations"]["gems"][0]["skill_id"] = "InventedPowerGem"
        result = self.evaluate_plan(plan, "bad-gem")
        self.assertEqual(result["status"], "invalid")
        with self.assertRaises(ValueError): poe.export(args(run=str(self.run_dir), name="bad-gem"))

    def test_04_dropped_tree_node_and_locked_item_are_rejected(self):
        plan = copy.deepcopy(self.refine)
        nodes = [int(n) for n in bx.active_spec(self.original).get("nodes").split(",")]
        plan["operations"]["tree"] = {"nodes": nodes + [99999999]}
        plan["operations"]["equip"] = [{"slot": "Weapon 2", "item_id": 10}]
        result = self.evaluate_plan(plan, "bad-tree")
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("天赋" in err for err in result["errors"]))
        self.assertTrue(any("保留" in err for err in result["errors"]))
        self.assertEqual(result["tree_diagnostics"], [{"id": 99999999, "found": False}])
        available = engine.calculate(self.pob, bx.serialise(self.original), catalog="nodes", query="Champion", limit=100)["catalog"]
        node = next(n for n in available if not n["allocated"] and n["type"] == "Notable" and n["points_to_allocate"] == 2)
        disconnected = copy.deepcopy(self.refine)
        disconnected["operations"]["tree"] = {"nodes": nodes + [node["id"]]}
        result = self.evaluate_plan(disconnected, "disconnected-tree")
        detail = next(row for row in result["tree_diagnostics"] if row["id"] == node["id"])
        self.assertTrue(detail["found"])
        self.assertTrue(detail["reachable"])
        self.assertEqual(detail["path"], node["path"])
        self.assertEqual(detail["points_to_allocate"], 2)
        self.assertEqual(disconnected["operations"]["tree"]["nodes"], nodes + [node["id"]])

    def test_05_modified_candidate_cannot_be_exported(self):
        result = self.evaluate_plan(self.refine, "tamper")
        self.assertEqual(result["status"], "validated", result["errors"])
        path = self.run_dir / "candidates/tamper/candidate.xml"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "修改"): poe.export(args(run=str(self.run_dir), name="tamper"))

    def test_06_new_intent_invalidates_existing_candidate(self):
        result = self.evaluate_plan(self.refine, "old-intent")
        self.assertEqual(result["status"], "validated", result["errors"])
        changed = copy.deepcopy(self.intent)
        changed["goal"] = "另一目标"
        path = Path(self.temp.name) / "changed-intent.json"
        poe.write_json(path, changed)
        poe.set_intent(args(run=str(self.run_dir), file=str(path)))
        with self.assertRaisesRegex(ValueError, "意图"): poe.export(args(run=str(self.run_dir), name="old-intent"))

    def test_22_updated_skill_refreshes_old_baseline_metadata(self):
        self.evaluate_plan(self.refine, "baseline-before-upgrade")
        path = self.run_dir / "baseline.json"
        baseline = poe.read_json(path)
        self.assertEqual(baseline["skill_version"], poe.VERSION)
        baseline.pop("skill_version")
        baseline.pop("active_skills")
        baseline["stats"]["CombinedDPS"] = -12345
        poe.write_json(path, baseline)
        result = self.evaluate_plan(self.rebuild, "baseline-after-upgrade")
        refreshed = poe.read_json(path)
        self.assertEqual(refreshed["skill_version"], poe.VERSION)
        self.assertTrue(refreshed["active_skills"])
        self.assertGreater(refreshed["stats"]["CombinedDPS"], 0)
        record = poe.read_json(self.run_dir / "candidates/baseline-after-upgrade/evaluation.json")
        self.assertTrue(any("主技能不同" in note for note in record["comparison_notes"]))
        self.assertIsNone(result["comparison"]["CombinedDPS"]["percent"])

    def test_07_catalog_route_can_be_allocated_without_losing_nodes(self):
        result = engine.calculate(self.pob, bx.serialise(self.original), catalog="nodes", query="life", limit=100)
        route = next(node for node in result["catalog"] if not node["allocated"] and node["reachable"]
                     and node["type"] in {"Normal", "Notable"} and 0 < node["points_to_allocate"] <= 3)
        original_nodes = {int(n) for n in bx.active_spec(self.original).get("nodes").split(",")}
        requested = original_nodes | set(route["path"])
        plan = copy.deepcopy(self.refine)
        plan["operations"]["tree"] = {"nodes": sorted(requested)}
        outcome = self.evaluate_plan(plan, "catalog-route")
        self.assertEqual(outcome["status"], "validated", outcome["errors"])
        saved = poe.read_json(self.run_dir / "candidates/catalog-route/evaluation.json")
        self.assertTrue(requested.issubset(set(saved["result"]["nodes"])))

    def test_08_item_skill_and_condition_mechanics_are_queryable(self):
        queried = engine.calculate(self.pob, queries=[{"kind": "skills", "query": "Embrace Madness", "limit": 5},
                                                    {"kind": "config", "query": "Glorious Madness", "limit": 10},
                                                    {"kind": "config", "query": "enemyIsBoss", "limit": 1}])["catalog_batch"]
        skills = queried[0]["entries"]
        self.assertTrue(any(s["id"] == "EmbraceMadness" and s["from_item"] and "Debuffs" in s["description"] for s in skills))
        options = queried[1]["entries"]
        self.assertTrue(any(s["var"] == "touchedDebuffsCount" and "recovery rate" in s["tooltip"] for s in options))
        boss = queried[2]["entries"][0]
        self.assertIn({"val": "Boss", "label": "Standard Boss"}, boss["options"])
        self.assertEqual(boss["default_value"], boss["options"][boss["defaultIndex"] - 1]["val"])
        plan = copy.deepcopy(self.refine)
        plan["operations"]["config"] = {"enemyIsBoss": "Standard Boss"}
        plan["config_evidence"] = {"enemyIsBoss": "验证显示标签不应冒充配置值。"}
        outcome = self.evaluate_plan(plan, "invalid-config-label")
        self.assertTrue(any("无效配置值" in error for error in outcome["errors"]), outcome["errors"])
        plan["operations"]["config"]["enemyIsBoss"] = "Boss"
        outcome = self.evaluate_plan(plan, "valid-config-value")
        self.assertEqual(outcome["status"], "validated", outcome["errors"])
        recorded = poe.read_json(self.run_dir / "candidates/valid-config-value/evaluation.json")
        self.assertEqual(recorded["result"]["config_values"]["enemyLevel"], self.intent["scenario"]["enemyLevel"])
        self.assertEqual(recorded["result"]["config_values"]["enemyIsBoss"], "Boss")
        self.assertEqual(recorded["result"]["config_labels"]["enemyIsBoss"], "Standard Boss")
        output = poe.export(args(run=str(self.run_dir), name="valid-config-value"))
        self.assertIn("敌人类型：Standard Boss（Boss）；敌人等级参数：83", output["user_summary"])
        report = Path(output["files"][1]).read_text(encoding="utf-8")
        self.assertIn("敌人类型：Standard Boss（Boss）；敌人等级参数：83", report)
        self.assertIn(recorded["xml_sha256"], report)
        self.assertIn(recorded["engine_fingerprint"], report)

    def test_09_resource_shortfall_and_local_sources_remain_visible(self):
        plan = copy.deepcopy(self.refine)
        plan["operations"]["gems"] = [{"group": 1, "index": 2, "skill_id": "SupportBrutality", "level": 20}]
        plan["sources"] = [{"title": "本机机制查询", "url": "catalog --kind skills", "version": "3.29"}]
        outcome = self.evaluate_plan(plan, "resource-check")
        self.assertEqual(outcome["status"], "invalid", outcome["errors"])
        self.assertTrue(any("每秒消耗魔力" in error for error in outcome["errors"]))
        with self.assertRaises(ValueError): poe.export(args(run=str(self.run_dir), name="resource-check"))
        plan["operations"] = copy.deepcopy(self.refine["operations"])
        self.evaluate_plan(plan, "resource-fixed")
        output = poe.export(args(run=str(self.run_dir), name="resource-fixed"))
        report = Path(output["files"][1]).read_text(encoding="utf-8")
        self.assertIn("本机机制查询", report)
        self.assertIn("命中吸取", report)
        self.assertIn("仅作诊断参考", report)

    def test_10_native_329_mismatched_colours_are_legal_and_quality_changes(self):
        plan = copy.deepcopy(self.refine)
        item_set = bx.active_set(self.original, "Items", "ItemSet", "activeItemSet")
        item_id = int(next(slot for slot in item_set.findall("Slot") if slot.get("name") == "Boots").get("itemId"))
        item = next(item for item in self.original.findall("Items/Item") if int(item.get("id")) == item_id)
        import re
        text = re.sub(r"^Sockets:.*$", "Sockets: W-W-W-W", bx.text_item(item), flags=re.M)
        plan["operations"]["items"] = [{"id": item_id, "text": text}]
        outcome = self.evaluate_plan(plan, "white-sockets")
        self.assertEqual(outcome["status"], "validated", outcome["errors"])
        saved = poe.read_json(self.run_dir / "candidates/white-sockets/evaluation.json")["result"]
        self.assertEqual(saved["data_version"], "3.29")
        self.assertEqual(saved["matching_socket_quality_bonus"], 10)
        for gem in saved["gems"]:
            if gem["slot"] == "Boots": self.assertEqual(gem["socket_quality_bonus"], 0)
        standard = engine.calculate(self.pob, bx.serialise(bx.apply_plan(poe.prepared(self.original, self.intent), self.refine)))
        self.assertTrue(any(gem["socket_quality_bonus"] == 10 for gem in standard["gems"] if gem["slot"] == "Boots"))
        determination = next(gem for gem in saved["gems"] if gem.get("id") == "Determination")
        self.assertEqual(determination["name_cn"], "坚定")

    def test_11_ascendancy_nodes_are_searchable_and_allocatable(self):
        entries = engine.calculate(self.pob, bx.serialise(self.original), catalog="nodes", query="Champion", limit=100)["catalog"]
        self.assertTrue(entries)
        entries = [node for node in entries if node["ascendancy_name"] == "Champion"]
        self.assertTrue(any(node["is_ascendancy_start"] for node in entries))
        route = next(node for node in entries if not node["allocated"] and node["reachable"]
                     and node["type"] == "Notable" and node["points_to_allocate"] == 2)
        original_nodes = {int(n) for n in bx.active_spec(self.original).get("nodes").split(",")}
        plan = copy.deepcopy(self.refine)
        plan["operations"]["tree"] = {"nodes": sorted(original_nodes | set(route["path"]))}
        outcome = self.evaluate_plan(plan, "ascendancy-route")
        self.assertEqual(outcome["status"], "validated", outcome["errors"])
        saved = poe.read_json(self.run_dir / "candidates/ascendancy-route/evaluation.json")
        self.assertEqual(saved["result"]["ascendancy_points"], 8)

    def test_12_native_batch_catalog_validates_starter_gem_levels(self):
        result = engine.calculate(self.pob, queries=[{"kind": "gems", "query": "id:ShieldCrush", "limit": 2},
                                                   {"kind": "gems", "query": "id:Sunder", "limit": 2},
                                                   {"kind": "gems", "query": "id:Determination", "limit": 2},
                                                   {"kind": "gems", "query": "id:Grace", "limit": 2},
                                                   {"kind": "gems", "query": "id:Discipline", "limit": 2}])
        self.assertEqual(len(result["catalog_batch"]), 5)
        gems = {entry["skill_id"]: entry for query in result["catalog_batch"] for entry in query["entries"]}
        self.assertEqual(set(gems), {"ShieldCrush", "Sunder", "Determination", "Grace", "Discipline"})
        self.assertEqual(gems["ShieldCrush"]["min_level"], 1)
        self.assertEqual(gems["Sunder"]["min_level"], 12)
        for skill_id, attribute in (("Determination", "strength"), ("Grace", "dexterity"), ("Discipline", "intelligence")):
            level_20 = next(row for row in gems[skill_id]["level_requirements"] if row["gem_level"] == 20)
            self.assertEqual(level_20["character_level"], 70)
            self.assertEqual(level_20[attribute], 155)
        route = route_fixture()
        checked, _ = routes.validate(route, gems)
        self.assertEqual(checked["stages"][0]["skill_groups"][0]["gems"][0]["name"], gems["ShieldCrush"]["display_name"])
        route["stages"][-1]["skill_groups"][0]["gems"][0]["level"] = "auto"
        checked, _ = routes.validate(route, gems)
        actual = checked["stages"][-1]["skill_groups"][0]["gems"][0]["level"]
        self.assertEqual(actual, max(row["gem_level"] for row in gems["ShieldCrush"]["level_requirements"] if row["character_level"] <= 68))
        route["gems"]["Sunder"] = copy.deepcopy(route["gems"]["ShieldCrush"])
        route["stages"][0]["skill_groups"][0]["gems"][0]["skill_id"] = "Sunder"
        with self.assertRaisesRegex(ValueError, "需要角色 12 级"):
            routes.validate(route, gems)

    def test_27_display_ascendancies_have_connected_native_nodes(self):
        classes = engine.calculate(self.pob)["classes"]
        names = [asc["name"] for cls in classes for asc in cls["ascendancies"]]
        result = engine.calculate(self.pob, queries=[{"kind": "nodes", "query": name, "limit": 100} for name in names]
                                  + [{"kind": "nodes", "query": "Deadeye", "limit": 1}])
        for name, query in zip(names, result["catalog_batch"]):
            with self.subTest(ascendancy=name):
                selected, paths = engine.ascendancy_paths(name, query["entries"])
                notables = [node for node in selected.values() if node["type"] == "Notable"]
                self.assertTrue(paths, name)
                self.assertTrue(notables, name)
                connected = [node for node in notables if node["linked"]]
                self.assertTrue(connected, name)
                self.assertTrue(all(node["id"] in paths for node in connected), name)
                self.assertTrue(all(node["no_tree_connection"] for node in notables if not node["linked"]), name)
                self.assertTrue(all(node["ascendancy_path_cost"] == len(paths[node["id"]]) for node in connected), name)
        truncated = result["catalog_batch"][-1]
        self.assertGreater(truncated["total"], len(truncated["entries"]))
        self.assertTrue(all("ascendancy_path" not in node for node in truncated["entries"]))

    def test_28_same_version_engine_change_refreshes_baseline_and_blocks_old_export(self):
        self.evaluate_plan(self.refine, "fingerprint-before")
        path = self.run_dir / "baseline.json"
        baseline = poe.read_json(path)
        baseline["engine_fingerprint"] = "different-content-same-version"
        baseline["stats"]["CombinedDPS"] = -12345
        poe.write_json(path, baseline)
        result = self.evaluate_plan(self.refine, "fingerprint-after")
        self.assertEqual(result["status"], "validated", result["errors"])
        refreshed = poe.read_json(path)
        current = engine.check(self.pob)
        self.assertEqual(refreshed["engine_fingerprint"], current["engine_fingerprint"])
        self.assertGreater(refreshed["stats"]["CombinedDPS"], 0)
        current["engine_fingerprint"] = "same-version-but-updated-files"
        with patch.object(engine, "check", return_value=current):
            with self.assertRaisesRegex(ValueError, "版本号相同"):
                poe.read_candidate(args(run=str(self.run_dir), name="fingerprint-after"))

    def test_29_excluded_guard_keeps_trigger_validation_and_resource_costs(self):
        plan = copy.deepcopy(self.rebuild)
        plan["operations"]["replace_skills"].append({"slot": "Helmet", "gems": [
            {"skill_id": "SupportCastWhenDamageTaken", "level": 1},
            {"skill_id": "ImmortalCall", "level": 1, "enable_global1": False}]})
        outcome = self.evaluate_plan(plan, "guard-off")
        self.assertFalse(any("未辅助任何启用技能" in error for error in outcome["errors"]), outcome["errors"])
        result = poe.read_json(self.run_dir / "candidates/guard-off/evaluation.json")["result"]
        self.assertFalse(result["stats"]["GuardSkillActive"])
        guard = next(skill for skill in result["active_skills"] if skill["id"] == "ImmortalCall")
        self.assertTrue(guard["benefit_excluded"])
        self.assertTrue(guard["trigger"])
        self.assertLessEqual(guard["trigger_target_required_level"], guard["trigger_level_limit"])
        mana = next(row for row in outcome["readiness"]["resources"] if row["resource"] == "魔力")
        self.assertGreater(mana["automatic_cost_per_second"], 0)
        saved_xml = (self.run_dir / "candidates/guard-off/candidate.xml").read_text(encoding="utf-8")
        saved = bx.parse(saved_xml)
        guard_gem = next(g for group in bx.groups(saved) for g in group.findall("Gem") if g.get("skillId") == "ImmortalCall")
        self.assertEqual(guard_gem.get("enableGlobal1"), "false")
        restored = engine.calculate(self.pob, saved_xml)
        self.assertFalse(restored["stats"]["GuardSkillActive"])
        self.assertEqual(restored["stats"]["CombinedDPS"], result["stats"]["CombinedDPS"])
        self.assertEqual(restored["excluded_guard_checks"], result["excluded_guard_checks"])
        restored["capabilities"]["excluded_guard_validation"] = False
        with self.assertRaisesRegex(ValueError, "不能识别守护技能"):
            engine.validate_contract(restored)
        plan["operations"]["replace_skills"][-1]["gems"][1]["level"] = 20
        incompatible = self.evaluate_plan(plan, "guard-trigger-level-invalid")
        self.assertTrue(any("触发辅助等级上限" in error for error in incompatible["errors"]), incompatible["errors"])

    def test_13_native_support_compatibility_and_route_class_data(self):
        query = "ShieldCrush,SupportMeleePhysicalDamage,SupportGreaterMultipleProjectiles"
        result = engine.calculate(self.pob, queries=[{"kind": "links", "query": query, "limit": 1},
                                                   {"kind": "classes", "query": "Duelist", "limit": 1},
                                                   {"kind": "classes", "query": "Templar", "limit": 1},
                                                   {"kind": "nodes", "query": "39790", "limit": 1},
                                                   {"kind": "links", "query": "Fireball,FreezingPulse", "limit": 1},
                                                   {"kind": "links", "query": "SupportMeleePhysicalDamage,NonexistentFixture", "limit": 1},
                                                   {"kind": "links", "query": "", "limit": 1},
                                                   {"kind": "gems", "query": "id:Fireball", "limit": 1}])
        links = result["catalog_batch"][0]["entries"][0]
        self.assertEqual(links["compatible"], ["SupportMeleePhysicalDamage"])
        self.assertEqual(links["incompatible"], ["SupportGreaterMultipleProjectiles"])
        self.assertFalse(links["valid"])
        invalid = [query["entries"][0] for query in result["catalog_batch"][4:7]]
        self.assertTrue(all(not entry["valid"] and entry["errors"] for entry in invalid))
        self.assertIn("Expected support gem: FreezingPulse", invalid[0]["errors"])
        self.assertEqual(len(invalid[1]["errors"]), 2)
        self.assertEqual(result["catalog_batch"][7]["entries"][0]["skill_id"], "Fireball")
        self.assertIn("Champion", [a["name"] for a in result["catalog_batch"][1]["entries"][0]["ascendancies"]])
        templar = result["catalog_batch"][2]["entries"][0]
        self.assertEqual(templar["name_cn"], "圣堂武僧")
        self.assertEqual(next(a for a in templar["ascendancies"] if a["name"] == "Inquisitor")["name_cn"], "判官")
        self.assertEqual(result["catalog_batch"][3]["entries"][0]["name_cn"], "至圣所")
        checked_route, warnings = routes.validate(route_fixture(), {"ShieldCrush": {"skill_id": "ShieldCrush", "name": "Shield Crush", "tags": {"grants_active_skill": True}, "level_requirements": [{"gem_level": 1, "character_level": 1}]}}, "3.29", result["classes"])
        self.assertIn("决斗者（Duelist） → 冠军（Champion）", routes.render(checked_route, warnings, "2.67.2", "3_29"))
        plan = copy.deepcopy(self.rebuild)
        plan["operations"]["replace_skills"][0]["gems"][1]["skill_id"] = "SupportGreaterMultipleProjectiles"
        outcome = self.evaluate_plan(plan, "incompatible-support")
        self.assertEqual(outcome["status"], "invalid")
        self.assertTrue(any("未辅助任何启用技能" in error for error in outcome["errors"]))

    def test_14_impossible_base_gem_level_and_quality_are_rejected(self):
        plan = copy.deepcopy(self.refine)
        plan["operations"]["gems"].append({"group": 1, "index": 3, "level": 30, "quality": 50})
        outcome = self.evaluate_plan(plan, "impossible-gem")
        self.assertEqual(outcome["status"], "invalid")
        self.assertTrue(any("自身等级/品质" in error for error in outcome["errors"]))

    def test_15_native_rare_recipe_and_invalid_modifier_constraints(self):
        recipe = {"base": "Two-Toned Boots (Armour/Energy Shield)", "ilvl": 86, "sockets": "W-W-W-W",
                  "affixes": [{"id": "IncreasedLife8"}, {"id": "MovementVelocity5"}, {"id": "ChaosResist6"}]}
        item_set = bx.active_set(self.original, "Items", "ItemSet", "activeItemSet")
        item_id = int(next(slot for slot in item_set.findall("Slot") if slot.get("name") == "Boots").get("itemId"))
        plan = copy.deepcopy(self.refine)
        plan["operations"]["items"] = [{"id": item_id, "craft": recipe}]
        outcome = self.evaluate_plan(plan, "crafted-boots")
        self.assertEqual(outcome["status"], "validated", outcome["errors"])
        recorded = poe.read_json(self.run_dir / "candidates/crafted-boots/evaluation.json")
        raw = recorded["crafted_items"][0]["text"]
        self.assertIn("+122 to maximum Life", raw)
        self.assertIn("+33% to Chaos Resistance", raw)
        self.assertIn("+10% to Fire and Lightning Resistances", raw)
        self.assertIn("ArmourBasePercentile: 0.5", raw)
        output = poe.export(args(run=str(self.run_dir), name="crafted-boots"))
        self.assertIn("词缀依据", Path(output["files"][1]).read_text(encoding="utf-8"))
        bad = copy.deepcopy(recipe)
        bad["affixes"].append({"id": "IncreasedLife7"})
        with self.assertRaisesRegex(ValueError, "Conflicting modifier group"):
            poe.materialize_items({"operations": {"items": [{"id": 20, "craft": bad}]}}, self.pob)
        bad = copy.deepcopy(recipe)
        bad["ilvl"] = 70
        with self.assertRaisesRegex(ValueError, "level is too low"):
            poe.materialize_items({"operations": {"items": [{"id": 20, "craft": bad}]}}, self.pob)
        bad = copy.deepcopy(recipe)
        bad["sockets"] = "W-W-W-W-W"
        with self.assertRaisesRegex(ValueError, "Socket count"):
            poe.materialize_items({"operations": {"items": [{"id": 20, "craft": bad}]}}, self.pob)
        entries = engine.calculate(self.pob, catalog="affixes", query=recipe["base"] + "|", limit=1000)["catalog"]
        unique_prefixes = {entry["group"]: entry["id"] for entry in entries if entry["type"] == "Prefix" and entry["required_item_level"] <= 86}
        self.assertGreaterEqual(len(unique_prefixes), 4)
        bad = copy.deepcopy(recipe)
        bad["affixes"] = [{"id": mod} for mod in list(unique_prefixes.values())[:4]]
        with self.assertRaisesRegex(ValueError, "Too many Prefix"):
            poe.materialize_items({"operations": {"items": [{"id": 20, "craft": bad}]}}, self.pob)

    def test_16_missing_equipment_is_rejected_even_without_socketed_skills(self):
        plan = copy.deepcopy(self.refine)
        plan["operations"]["items"] = [{"id": 100, "text": "Rarity: RARE\nInvented Belt\nNonexistent Test Base\nImplicits: 0\n+100 to maximum Life"}]
        plan["operations"]["equip"].append({"slot": "Belt", "item_id": 100})
        outcome = self.evaluate_plan(plan, "missing-belt")
        self.assertEqual(outcome["status"], "invalid")
        self.assertTrue(any("Belt 的装备 100 未被引擎装入" in error for error in outcome["errors"]), outcome["errors"])

    def test_17_gem_probe_matches_individual_engine_and_does_not_write_candidate(self):
        self.evaluate_plan(self.refine, "gem-probe")
        folder = self.run_dir / "candidates/gem-probe"
        snapshot = {path.name: bx.digest(path.read_bytes()) for path in folder.iterdir()}
        result = poe.probe(args(run=str(self.run_dir), name="gem-probe", group=1, index=2,
                                ids="SupportIronGrip,SupportBrutality,SupportSpellEcho", level=19, quality=0))
        self.assertEqual(result["status"], "screened")
        rows = result["alternatives"]
        self.assertAlmostEqual(rows[1]["dps_delta"], 0, places=6)
        self.assertTrue(any("未辅助" in error for error in rows[2]["errors"]), rows[2]["errors"])
        changed = bx.apply_plan(bx.read_document(folder / "candidate.xml"), {"mode": "refine", "summary": "comparison", "operations": {"gems": [{"group": 1, "index": 2, "skill_id": "SupportIronGrip", "level": 19, "quality": 0}]}})
        standalone = engine.calculate(self.pob, bx.serialise(changed))
        self.assertAlmostEqual(rows[0]["stats"]["CombinedDPS"], standalone["stats"]["CombinedDPS"], places=6)
        self.assertEqual(snapshot, {path.name: bx.digest(path.read_bytes()) for path in folder.iterdir()})

    def test_18_native_energy_shield_payment_and_righteous_fire_degen(self):
        unique = engine.calculate(self.pob, catalog="uniques", query="The Devouring Diadem", limit=1)["catalog"][0]["raw"]
        plan = copy.deepcopy(self.refine)
        plan["operations"]["items"] = [{"id": 100, "text": unique}]
        plan["operations"]["equip"].append({"slot": "Helmet", "item_id": 100})
        outcome = self.evaluate_plan(plan, "energy-shield-payment")
        result = poe.read_json(self.run_dir / "candidates/energy-shield-payment/evaluation.json")["result"]
        self.assertTrue(result["resource_rules"]["energy_shield_protects_mana"])
        self.assertGreater(result["stats"]["ESPerSecondCost"], 0)
        self.assertEqual(outcome["status"], "validated", outcome["errors"])
        self.assertTrue(any(row["resource"] == "能量护盾" for row in outcome["readiness"]["resources"]))
        plan = copy.deepcopy(self.rebuild)
        plan["operations"]["replace_skills"].append({"slot": "Helmet", "gems": [{"skill_id": "RighteousFire", "level": 19, "enable_global1": True}]})
        outcome = self.evaluate_plan(plan, "self-degen")
        result = poe.read_json(self.run_dir / "candidates/self-degen/evaluation.json")["result"]
        self.assertGreater(result["stats"].get("TotalBuildDegen", 0), 0)
        self.assertLess(result["stats"]["NetLifeRegen"], 0)
        self.assertEqual(outcome["status"], "invalid")
        self.assertTrue(any("净秒回" in error for error in outcome["errors"]), outcome["errors"])

    def test_19_native_route_counts_small_ascendancy_points_and_requires_respec(self):
        result = engine.calculate(self.pob, queries=[{"kind": "nodes", "query": "Inquisitor", "limit": 100},
                                                   {"kind": "gems", "query": "id:ShieldCrush", "limit": 1},
                                                   {"kind": "nodes", "query": "Deadeye", "limit": 100}])
        nodes = result["catalog_batch"][0]["entries"]
        gems = {entry["skill_id"]: entry for entry in result["catalog_batch"][1]["entries"]}
        route = route_fixture()
        route["class_id"], route["ascendancy"] = 5, "Inquisitor"
        stage = route["stages"][-1]
        stage["ascendancy_points"] = 8
        stage["ascendancy_nodes"] = [39790, 32816, 53884, 48214]
        checked, _ = routes.validate(route, gems, "3.29", result["classes"], nodes=nodes)
        self.assertEqual(checked["stages"][-1]["ascendancy_used"], 8)
        stage["ascendancy_nodes"].append(40059)
        with self.assertRaisesRegex(ValueError, "共需 10 点"):
            routes.validate(route, gems, nodes=nodes)
        stage["ascendancy_nodes"].remove(40059)
        route["stages"][1].update({"ascendancy_points": 2, "ascendancy_nodes": [40059]})
        with self.assertRaisesRegex(ValueError, "洗点与切换原因"):
            routes.validate(route, gems, nodes=nodes)
        stage["ascendancy_respec_reason"] = "先准备后悔石，退还忏悔之兆与连接小点，再分配廉简之途。"
        routes.validate(route, gems, nodes=nodes)

        # An implicit prerequisite remains allocated when its child is selected.
        nodes = result["catalog_batch"][2]["entries"]
        route = route_fixture()
        route["class_id"], route["ascendancy"] = 2, "Deadeye"
        route["stages"][1].update(level=33, act=4, ascendancy_points=2, ascendancy_nodes=[2872])
        route["stages"][-1].update(ascendancy_points=6, ascendancy_nodes=[24848, 26067])
        checked, _ = routes.validate(route, gems, "3.29", result["classes"], nodes=nodes)
        final = checked["stages"][-1]
        self.assertEqual(final["ascendancy_used"], 6)
        details = {node["id"]: node for node in final["ascendancy_details"]}
        self.assertEqual(set(details), {2872, 24848, 26067})
        self.assertTrue(details[2872]["required_by_path"])
        self.assertFalse(details[26067]["required_by_path"])
        self.assertIn("连接路径必经", routes.render(checked, [], result["engine_version"], result["tree_version"]))
        route["stages"][-1]["ascendancy_nodes"] = [24848]
        with self.assertRaisesRegex(ValueError, "洗点与切换原因"):
            routes.validate(route, gems, nodes=nodes)

    def test_20_native_general_cry_context_and_automatic_costs(self):
        plan = copy.deepcopy(self.rebuild)
        plan["operations"]["replace_skills"][0] = {"slot": "Gloves", "include_in_full_dps": True, "gems": [
            {"skill_id": "ShieldCrush", "level": 19}, {"skill_id": "GeneralsCry", "level": 19},
            {"skill_id": "Autoexertion", "level": 19}, {"skill_id": "SupportBrutality", "level": 19}]}
        outcome = self.evaluate_plan(plan, "gc-no-corpses")
        self.assertEqual(outcome["status"], "invalid")
        self.assertTrue(any("尸体生成" in error for error in outcome["errors"]), outcome["errors"])
        plan["operations"]["replace_skills"].append({"slot": "Helmet", "gems": [{"skill_id": "Desecrate", "level": 1}]})
        plan["config_evidence"] = {"generals_cry": "测试夹具：手动亵渎生成尸体，自动增助按冷却触发将军之吼；幻影攻击，玩家不依靠幻影吸取。"}
        outcome = self.evaluate_plan(plan, "gc-costs")
        result = poe.read_json(self.run_dir / "candidates/gc-costs/evaluation.json")["result"]
        cry = next(skill for skill in result["active_skills"] if skill["id"] == "GeneralsCry")
        main = next(skill for skill in result["active_skills"] if skill["is_main"])
        self.assertTrue(main["generals_cry"])
        self.assertTrue(result["capabilities"]["native_corpse_sources"])
        self.assertTrue(next(skill for skill in result["active_skills"] if skill["id"] == "Desecrate")["creates_corpses"])
        self.assertFalse(main["creates_corpses"])
        self.assertEqual(cry["trigger"], "Autoexertion")
        self.assertGreater(cry["costs"]["ManaPerSecondCost"], 0)
        mana = next(row for row in outcome["readiness"]["resources"] if row["resource"] == "魔力")
        self.assertAlmostEqual(mana["cost_per_second"], cry["costs"]["ManaPerSecondCost"] + result["stats"].get("ManaPerSecondCost", 0))
        self.assertTrue(result["damage_sources"])
        self.assertEqual(outcome["status"], "validated", outcome["errors"])
        exported = poe.export(args(run=str(self.run_dir), name="gc-costs"))
        report = Path(exported["files"][1]).read_text(encoding="utf-8")
        self.assertIn("原生 Full DPS 分项", report)
        self.assertIn("自动触发额外消耗", report)
        self.assertIn("19 级 / 0 品质", report)
        self.assertIn("原样发送 user_summary", exported["final_reply_policy"])
        self.assertIn("幻影吸取未计入玩家回复", exported["user_summary"])
        for resource in outcome["readiness"]["resources"]:
            self.assertEqual(resource["leech_on_hit"], 0)

    def test_21_minion_damage_is_separate_and_probe_uses_minion_output(self):
        plan = copy.deepcopy(self.rebuild)
        plan["operations"]["replace_skills"] = [{"slot": "Gloves", "include_in_full_dps": True, "gems": [
            {"skill_id": "SummonRagingSpirit", "level": 12}, {"skill_id": "SupportMinionDamage", "level": 19},
            {"skill_id": "SupportLifetap", "level": 19}]}]
        outcome = self.evaluate_plan(plan, "minion-context")
        self.assertEqual(outcome["status"], "validated", outcome["errors"])
        result = poe.read_json(self.run_dir / "candidates/minion-context/evaluation.json")["result"]
        self.assertEqual(result["stats"]["CombinedDPS"], 0)
        self.assertGreater(result["stats"]["MinionCombinedDPS"], 0)
        self.assertEqual(result["stats"]["MinionCombinedDPS"], result["minion"]["CombinedDPS"])
        self.assertTrue(any(row["name"] == "Summon Raging Spirit" for row in result["damage_sources"]))
        result = poe.probe(args(run=str(self.run_dir), name="minion-context", group=1, index=2,
                                ids="SupportMinionDamage,SupportMeleePhysicalDamage", level=19, quality=0))
        self.assertEqual(result["alternatives"][0]["dps_metric"], "MinionCombinedDPS")
        self.assertAlmostEqual(result["alternatives"][0]["dps_delta"], 0)
        self.assertNotEqual(result["alternatives"][1]["dps_delta"], 0)
        exported = poe.export(args(run=str(self.run_dir), name="minion-context"))
        self.assertIn("召唤物单体综合 DPS", exported["user_summary"])
        self.assertNotIn("主技能综合 DPS 0.0", exported["user_summary"])
        self.assertIn("召唤物命中率", exported["user_summary"])
        self.assertNotIn("角色技能命中率", exported["user_summary"])

    def test_23_large_catalog_output_is_saved_and_inputs_are_protected(self):
        query = Path(self.temp.name) / "queries.json"
        output = Path(self.temp.name) / "查询结果.json"
        poe.write_json(query, {"queries": [{"kind": "gems", "query": "", "limit": 40}, {"kind": "classes", "query": "Templar", "limit": 1}]})
        command = ["catalog", "--batch", str(query), "--pob", str(self.pob)]
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(poe.main(command), 2)
        self.assertIn("--out", error.getvalue())
        with redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(poe.main(command + ["--out", str(output)]), 0)
        response = json.loads(stdout.getvalue())
        self.assertEqual(response["catalog_file"], str(output))
        self.assertEqual([row["returned"] for row in response["queries"]], [40, 1])
        self.assertLess(len(stdout.getvalue()), 3000)
        self.assertGreater(len(output.read_text(encoding="utf-8")), 12000)
        data = poe.read_json(output)
        self.assertEqual(len(data["queries"][0]["entries"]), 40)
        digest = bx.digest(query.read_bytes())
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(poe.main(command + ["--out", str(query)]), 2)
        self.assertIn("不能覆盖", error.getvalue())
        self.assertEqual(bx.digest(query.read_bytes()), digest)

    def test_25_inspect_returns_safe_diagnosis_even_when_full_data_goes_to_file(self):
        path = Path(self.temp.name) / "diagnosis.json"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = poe.main(["inspect", "--input", str(self.sample), "--calculate", "--out", str(path)])
        self.assertEqual(code, 0)
        result = json.loads(stdout.getvalue())
        saved = poe.read_json(path)
        self.assertEqual(result["diagnosis_summary"], saved["diagnosis_summary"])
        self.assertIn(f"主技能综合 DPS：{saved['calculation']['stats']['CombinedDPS']:g}", result["diagnosis_summary"])
        self.assertIn("不再乘命中率", result["diagnosis_summary"])
        self.assertIn("原样", result["reply_policy"])
        self.assertLess(len(stdout.getvalue()), 3000)

    def test_26_native_partial_mastery_edit_preserves_allocated_masteries(self):
        import re
        masteries = {int(node): int(effect) for node, effect in re.findall(r"\{(\d+),(\d+)\}", bx.active_spec(self.original).get("masteryEffects", ""))}
        self.assertGreater(len(masteries), 1)
        node, effect = next(iter(masteries.items()))
        plan = copy.deepcopy(self.refine)
        plan["operations"]["tree"] = {"masteries_add": {str(node): effect}}
        outcome = self.evaluate_plan(plan, "mastery-delta")
        self.assertEqual(outcome["status"], "validated", outcome["errors"])
        result = poe.read_json(self.run_dir / "candidates/mastery-delta/evaluation.json")["result"]
        self.assertTrue(set(masteries) <= set(result["nodes"]))

    def test_24_catalog_failure_explains_character_free_queries(self):
        invalid = '<PathOfBuilding><Build className="Marauder" ascendClassName="Chieftain" level="1" targetVersion="3_0"/><Tree><Spec treeVersion="3_29" ascendancyName="Chieftain"><URL>https://www.pathofexile.com/passive-skill-tree/AAAABgAA</URL></Spec></Tree><Skills/><Items/></PathOfBuilding>'
        with self.assertRaises(engine.EngineError) as failure:
            engine.calculate(self.pob, invalid, catalog="nodes", query="Chieftain")
        self.assertIn("去掉 --input", str(failure.exception))
        self.assertNotIn("Press Enter/Escape", str(failure.exception))
        recovered = engine.calculate(self.pob, catalog="nodes", query="Chieftain", limit=40)
        nodes = recovered["catalog"]
        self.assertTrue(any(node["is_ascendancy_start"] for node in nodes))
        self.assertTrue(any(node["name"] == "Hinekora, Death's Fury" and node["type"] == "Notable" for node in nodes))
        # The text search also legitimately matches Ascendant's Chieftain node.
        # Route allocation selects the actual ascendancy by ascendancy_name.
        self.assertTrue(any(node["name"] == "Chieftain" and node["ascendancy_name"] == "Ascendant" for node in nodes))
        self.assertTrue(any(node["is_ascendancy_start"] and node["ascendancy_name"] == "Chieftain" for node in nodes))


if __name__ == "__main__": unittest.main()
