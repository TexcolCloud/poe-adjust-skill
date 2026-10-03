"""Offline behavioral tests, including the installed real calculation engine.

Run with TeleAgent's Python: python -m unittest discover -s tests -v
The user's XML and installed application files are never edited.
"""
import argparse
import base64
import copy
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "poe-adjust/scripts"))
import buildxml as bx
import engine
import poe
import routes

SAMPLE = '''<PathOfBuilding><Build level="86" className="Duelist" targetVersion="3_0" mainSocketGroup="1"/><Tree activeSpec="1"><Spec treeVersion="3_29" classId="4" ascendClassId="3" nodes="1,2"/></Tree><Skills activeSkillSet="2"><SkillSet id="1"><Skill enabled="true"><Gem skillId="Fireball" level="1" quality="0"/></Skill></SkillSet><SkillSet id="2"><Skill enabled="true" slot="Boots"><Gem skillId="ShieldCrush" gemId="stale" variantId="stale" level="20" quality="0"/></Skill></SkillSet></Skills><Items activeItemSet="1"><Item id="1">Rarity: NORMAL\nIron Greaves\nSockets: R-R-R-R</Item><ItemSet id="1"><Slot name="Boots" itemId="1"/></ItemSet></Items><Config/><Extension provider="fixture">preserve me</Extension></PathOfBuilding>'''


def args(**kwargs):
    return argparse.Namespace(pob=None, **kwargs)


def intent_for(root):
    return {"confirmed": True, "goal": "离线测试：保留盾牌并验证两种改装流程", "league": "国服测试赛季", "mode": "both",
            "budget": {"amount": 20, "currency": "divine"}, "target_level": 86,
            "allow_main_skill_change": True, "allow_ascendancy_change": True, "keep_slots": ["Weapon 2"],
            "scenario": {"enemyLevel": 83}, "baseline_main_group": 1,
            "quest_passive_points": 23, "ascendancy_points": 8}


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.root = bx.parse(SAMPLE)

    def test_saved_snapshot_has_exact_content_digest_on_windows(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshot.xml"
            text = bx.serialise(self.root)
            poe.write_text(path, text)
            self.assertEqual(bx.digest(path.read_bytes()), bx.digest(text))

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

    def test_socket_colours_links_and_white_sockets(self):
        self.assertFalse(poe.socket_groups_fit(["RGGG"], ["RRGG"]))
        self.assertTrue(poe.socket_groups_fit(["RGGG"], ["RWGG"]))
        self.assertFalse(poe.socket_groups_fit(["RRR", "RRR"], ["RRRR", "RR"]))
        self.assertTrue(poe.socket_groups_fit(["RGB", "R"], ["RR", "RGB"]))
        self.assertTrue(poe.socket_groups_fit(["W", "RR"], ["BRR"]))

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

    def test_incomparable_conditions_do_not_emit_gain_percentage(self):
        before, after = {"stats": {"CombinedDPS": 10}}, {"stats": {"CombinedDPS": 30}}
        self.assertIsNone(poe.compare(before, after, False)["CombinedDPS"]["percent"])
        self.assertEqual(poe.compare(before, after)["CombinedDPS"]["percent"], 200)


def route_fixture():
    return {"name": "离线开荒路线测试", "confirmed": True, "goal": "验证分阶段推荐的限制", "league": "测试赛季", "class_id": 4,
            "ascendancy": "Champion", "trade_mode": "ssf", "budget": {"amount": 0, "currency": "divine"}, "version_status": "unverified",
            "sources": [{"id": "gem", "title": "测试来源", "url": "https://example.org/gem", "checked_at": "2026-10-04", "version": "测试数据"}],
            "stages": [{"name": "测试阶段", "level": level, "main_skill_id": "ShieldCrush", "links": 1,
                        "skills": [{"skill_id": "ShieldCrush", "level": 1, "role": "main", "acquisition": "vendor", "source_id": "gem", "source_note": "仅验证文档结构，不作为实际领取资料。"}],
                        "gear": ["盾牌"], "passives": ["生命"], "ascendancy": "按迷宫进度分配", "resources": "核对消耗与魔力药剂",
                        "checks": ["属性需求满足"], "transition": ["下一阶段核心技能准备好"]} for level in (1, 12, 68)]}


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.route = route_fixture()
        self.gems = {"ShieldCrush": {"skill_id": "ShieldCrush", "name": "Shield Crush", "min_level": 1, "tags": {"grants_active_skill": True},
                                    "level_requirements": [{"gem_level": 1, "character_level": 1}, {"gem_level": 20, "character_level": 70}]}}

    def test_route_rejects_gem_level_unavailable_at_milestone(self):
        self.route["stages"][-1]["skills"][0]["level"] = 20
        with self.assertRaisesRegex(ValueError, "需要角色 70 级"):
            routes.validate(self.route, self.gems)

    def test_ssf_does_not_depend_on_trade_or_random_core_without_fallback(self):
        self.route["stages"][0]["skills"][0]["acquisition"] = "trade"
        with self.assertRaisesRegex(ValueError, "不能依赖交易宝石"):
            routes.validate(self.route, self.gems)
        self.route["stages"][0]["skills"][0]["acquisition"] = "drop"
        with self.assertRaisesRegex(ValueError, "fallback_skill_id"):
            routes.validate(self.route, self.gems)

    def test_route_keeps_unverified_version_and_does_not_invent_dps(self):
        checked, warnings = routes.validate(self.route, self.gems)
        self.assertTrue(any("国服赛季" in warning for warning in warnings))
        report = routes.render(checked, warnings, "2.67.2", "3_29")
        self.assertIn("尚未使用完整阶段装备", report)
        self.assertIn("目标等级", report)
        self.route["version_status"] = "verified"
        with self.assertRaisesRegex(ValueError, "version_source"):
            routes.validate(self.route, self.gems)

    def test_five_link_has_an_obtainable_four_link_fallback(self):
        self.route["stages"][-1]["links"] = 5
        with self.assertRaisesRegex(ValueError, "四连替代"):
            routes.validate(self.route, self.gems)


@unittest.skipUnless(os.name == "nt", "需要 Windows 本机引擎")
class RealEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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
        skills = engine.calculate(self.pob, catalog="skills", query="Embrace Madness", limit=5)["catalog"]
        self.assertTrue(any(s["id"] == "EmbraceMadness" and s["from_item"] and "Debuffs" in s["description"] for s in skills))
        options = engine.calculate(self.pob, catalog="config", query="Glorious Madness", limit=10)["catalog"]
        self.assertTrue(any(s["var"] == "touchedDebuffsCount" and "recovery rate" in s["tooltip"] for s in options))

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

    def test_10_recoloured_support_requires_matching_equipment(self):
        plan = copy.deepcopy(self.refine)
        plan["operations"]["gems"] = [{"group": 1, "index": 1, "skill_id": "SupportManaLeech", "level": 20}]
        outcome = self.evaluate_plan(plan, "wrong-colour")
        self.assertEqual(outcome["status"], "invalid")
        self.assertTrue(any("Boots 插槽颜色" in error for error in outcome["errors"]))
        with self.assertRaises(ValueError): poe.export(args(run=str(self.run_dir), name="wrong-colour"))

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
                                                   {"kind": "gems", "query": "id:Sunder", "limit": 2}])
        self.assertEqual(len(result["catalog_batch"]), 2)
        gems = {entry["skill_id"]: entry for query in result["catalog_batch"] for entry in query["entries"]}
        self.assertEqual(set(gems), {"ShieldCrush", "Sunder"})
        self.assertEqual(gems["ShieldCrush"]["min_level"], 1)
        self.assertEqual(gems["Sunder"]["min_level"], 12)
        route = route_fixture()
        checked, _ = routes.validate(route, gems)
        self.assertEqual(checked["stages"][0]["skills"][0]["minimum_character_level"], 1)
        route["stages"][0]["main_skill_id"] = "Sunder"
        route["stages"][0]["skills"][0]["skill_id"] = "Sunder"
        with self.assertRaisesRegex(ValueError, "需要角色 12 级"):
            routes.validate(route, gems)


if __name__ == "__main__": unittest.main()
