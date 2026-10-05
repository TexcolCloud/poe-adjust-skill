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
import resources

SAMPLE = '''<PathOfBuilding><Build level="86" className="Duelist" targetVersion="3_0" mainSocketGroup="1"/><Tree activeSpec="1"><Spec treeVersion="3_29" classId="4" ascendClassId="3" nodes="1,2"/></Tree><Skills activeSkillSet="2"><SkillSet id="1"><Skill enabled="true"><Gem skillId="Fireball" level="1" quality="0"/></Skill></SkillSet><SkillSet id="2"><Skill enabled="true" slot="Boots"><Gem skillId="ShieldCrush" gemId="stale" variantId="stale" level="20" quality="0"/></Skill></SkillSet></Skills><Items activeItemSet="1"><Item id="1">Rarity: NORMAL\nIron Greaves\nSockets: R-R-R-R</Item><ItemSet id="1"><Slot name="Boots" itemId="1"/></ItemSet></Items><Config/><Extension provider="fixture">preserve me</Extension></PathOfBuilding>'''


def args(**kwargs):
    return argparse.Namespace(pob=None, **kwargs)


def intent_for(root):
    value={"schema_version":2,"confirmed":True,"goal":"离线测试：验证现有角色的改装计算","league":"国服测试赛季","mode":"both",
           "game_version":"3.29","budget":{"amount":20,"currency":"divine"},"target_level":86,
           "allow_main_skill_change":True,"allow_ascendancy_change":True,"scenario":{"enemyLevel":83},"baseline_main_group":1,
           "quest_passive_points":23,"normal_points_available":108,"ascendancy_points":8}
    slots={s.get("name") for s in bx.active_set(root,"Items","ItemSet","activeItemSet").findall("Slot")}
    value["keep_slots"]=[{"slot":"Weapon 2" if "Weapon 2" in slots else "Boots","preserve":"exact_spec"}]
    value["objectives"]=[{"objective_id":"fixture-goal","text":value["goal"],"evidence_refs":["user"]}]
    value["constraint_sources"]={"/"+k:"user" for k in value if k not in {"schema_version","confirmed","league","game_version"}}
    value["evidence"]=[{"evidence_id":"user","kind":"user_statement","subject":"intent","text":"离线夹具明确声明以上方向、预算、目标等级、进度和保留规格；不是真实玩家授权记录。"}]
    return value


def resource_fixture():
    stats={}
    for resource,cost in (("Mana","Mana"),("Life","Life"),("EnergyShield","ES")):
        stats.update({resource:1000,cost+"Cost":0,cost+"PerSecondCost":0,resource+"RegenRecovery":0,resource+"Degen":0,resource+"LeechGainRate":0})
    return {"stats":stats,"equipped":[],"active_skills":[{"id":"main","skill_ref":"main","is_main":True,"actor":"player","costs":{}}]}


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.root = bx.parse(SAMPLE)

    def test_saved_catalog_summary_points_to_actual_fields_and_entries(self):
        examples=[{"total":2,"entries":[{"id":1,"stats":["native effect"]},{"id":2,"future_field":False}]},
                  {"queries":[{"kind":"nodes","query":"current","total":2,"entries":[{"id":1,"stats":["native effect"]},{"id":2,"future_field":False}]},
                              {"kind":"gems","query":"missing","total":0,"entries":[]}]}]
        with tempfile.TemporaryDirectory() as folder:
            for index, example in enumerate(examples):
                with self.subTest(batch=index):
                    output=Path(folder)/f"catalog-{index}.json";stdout=io.StringIO()
                    with patch.object(poe,"catalog",return_value=example),redirect_stdout(stdout):
                        self.assertEqual(poe.main(["catalog","--kind","nodes","--out",str(output)]),0)
                    response=json.loads(stdout.getvalue());saved=poe.read_json(output)
                    self.assertEqual(response["entry_fields"]["nodes"],["future_field","id","stats"])
                    self.assertTrue(response["summary_complete"])
                    for row in response["queries"]:
                        self.assertEqual(len(facts.pointer(saved,row["entries_pointer"])),row["returned"])
                    if index:self.assertEqual(response["entry_fields"]["gems"],[])
                    self.assertEqual(response["runtime"]["worker_calls"],0)

    def test_large_saved_catalog_summary_stays_bounded_and_paginates_metadata(self):
        result={"queries":[{"kind":"nodes","query":f"id:{index}","total":1,"entries":[{"id":index,"stats":["native effect"]}]} for index in range(300)]}
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/"catalog.json";stdout=io.StringIO()
            with patch.object(poe,"catalog",return_value=result),redirect_stdout(stdout):
                self.assertEqual(poe.main(["catalog","--kind","nodes","--out",str(output)]),0)
            response=json.loads(stdout.getvalue())
            self.assertLessEqual(len(stdout.getvalue()),12000)
            self.assertEqual(response["queries_total"],300)
            self.assertFalse(response["summary_complete"])
            self.assertTrue(response["queries"])
            self.assertLess(len(response["queries"]),300)
            action=response["next_actions"][0]
            self.assertEqual(action["action"],"view")
            self.assertEqual(action["file"],str(output))
            self.assertEqual(action["pointer"],"/queries")
            self.assertEqual(action["offset"],len(response["queries"]))
            saved=poe.read_json(output)
            self.assertEqual(len(saved["queries"]),300)
            self.assertNotIn("entries",action["fields"])

    def test_boolean_text_and_nonfinite_group_values_do_not_change_xml(self):
        before=bx.serialise(self.root)
        operations=[{"groups":[{"index":1,"enabled":"false"}]},{"gems":[{"group":1,"index":1,"enabled":"false"}]},
                    {"groups":[{"index":1,"group_count":float("nan")}]},{"equip":[{"slot":"Boots","item_id":True}]},{"tree":{"nodes":[True]}}]
        for change in operations:
            with self.subTest(change=change),self.assertRaises(ValueError):
                bx.apply_plan(self.root,{"mode":"refine","summary":"typed contract","operations":change})
            self.assertEqual(bx.serialise(self.root),before)

    def test_engine_metadata_accepts_new_releases_without_allowlist(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "manifest.xml").write_text('<PoBVersion><Version number="9.1.0"/></PoBVersion>', encoding="utf-8")
            (root / "GameVersions.lua").write_text('treeVersionList = {"3_29", "3_30_ruthless", "3_30"}', encoding="utf-8")
            (root / "changelog.txt").write_text('VERSION[9.1.0][2027/01/01]\nfixes\nVERSION[9.0.0]\n--- 3.30 - Example ---\n', encoding="utf-8")
            info = engine.metadata(root)
            self.assertEqual(info["data_version"], "3.30")
            self.assertIsNone(info["version_source_hint"])
            self.assertNotIn("version_evidence", info)
            with patch.dict(engine.VERSION_SOURCE_HINTS, {"3.30": {"season": "historic-example", "url": "https://poe.qq.com/example"}}):
                hint = engine.metadata(root)["version_source_hint"]
                self.assertEqual(hint["status"], "unverified_hint")
                self.assertNotIn("checked_at", hint)
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
        result = {"main_skill_name": "Spectral Shield Throw", "points": 103, "ascendancy_points": 6, "stats": {"CombinedDPS": 33770, "HitChance": 56, "ManaUnreserved": -275}}
        summary = poe.diagnosis_summary(result)
        self.assertIn("主技能综合 DPS：33770", summary)
        self.assertNotIn("18911", summary)
        self.assertIn("不再乘命中率", summary)
        self.assertIn("不能据此断言所有技能都不能使用", summary)
        self.assertIn("普通天赋 103 点，升华 6 点（包含连接小点）", summary)
        self.assertIn("这不是剩余点数", summary)
        self.assertNotIn("次级升华", summary)
        result["secondary_ascendancy_points"] = 2
        result["active_skills"] = [{"is_main": True, "minion": True}]
        result["stats"].update(MinionCombinedDPS=1234, MinionHitChance=95)
        summary = poe.diagnosis_summary(result)
        self.assertIn("召唤物单体综合 DPS：1234", summary)
        self.assertIn("召唤物命中率 %：95", summary)
        self.assertNotIn("33770", summary)
        self.assertIn("次级升华已分配 2 点", summary)

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

    def test_scenario_preserves_new_native_fields_and_deletes_only_explicit_keys(self):
        bx.set_config(self.root,{"conditionOnslaught":True,"futureSeasonChoice":"keep","bandit":"Alira"})
        bx.set_config(self.root,{"enemyLevel":83,"conditionOnslaught":None})
        names={n.get("name") for n in bx.active_config(self.root)}
        self.assertEqual(names,{"enemyLevel","futureSeasonChoice","bandit"})
        with self.assertRaises(ValueError): bx.set_config(self.root,{"customMods":"1000% more Damage"})

    def test_intent_requires_user_direction_and_valid_budget(self):
        intent = intent_for(self.root)
        intent["confirmed"] = False
        with self.assertRaises(ValueError): poe.validate_intent(intent, self.root)
        intent["confirmed"] = True
        intent["budget"]["amount"] = float("inf")
        with self.assertRaises(ValueError): poe.validate_intent(intent, self.root)

    def test_budget_keeps_unknown_and_foreign_prices_unverified(self):
        intent={"budget":{"amount":20,"currency":"divine"},"league":"CN"}
        item={"kind":"item","item_id":5,"quantity":1}
        result=poe.budget_summary({"shopping":[item]},intent,[item])
        self.assertEqual(result["status"],"unknown")
        self.assertFalse(result["totals_complete"])
        with self.assertRaises(ValueError):poe.budget_summary({"shopping":[dict(item,status="verified_cn")]},intent)
        result=poe.budget_summary({"shopping":[dict(item,low=30,high=35,currency="divine")]},intent,[item])
        self.assertFalse(result["over_budget"])
        self.assertEqual(result["known_lower_bound"],0)

    def test_intent_does_not_assume_character_progress(self):
        for field in ("target_level","quest_passive_points","ascendancy_points","normal_points_available"):
            value=intent_for(self.root);del value[field]
            with self.subTest(field=field):self.assertIsNone(poe.validate_intent(value,self.root)[field])
        value=intent_for(self.root);value["constraint_sources"].pop("/budget")
        with self.assertRaisesRegex(ValueError,"/budget"):poe.validate_intent(value,self.root)

    def test_329_socket_layout_ignores_colour_but_rejects_missing_links(self):
        result = {"equipped": [{"slot": "Boots", "id": 1}],
                  "gems": [{"group": 1, "index": i, "color": "G", "enabled": True, "support": i > 1} for i in range(1, 5)]}
        self.assertEqual(poe.socket_layout_issues(self.root, result), [])
        self.root.find("Items/Item").text = "Rarity: NORMAL\nIron Greaves\nSockets: W-W W-W"
        self.assertTrue(any("连线不匹配" in error for error in poe.socket_layout_issues(self.root, result)))
        result["gems"].append({"group": 1, "index": 5})
        self.assertTrue(any("数量不足" in error for error in poe.socket_layout_issues(self.root, result)))

    def test_granted_skill_before_socketed_group_does_not_consume_a_socket(self):
        parent=bx.active_set(self.root,"Skills","SkillSet","activeSkillSet")
        ordinary=bx.groups(self.root)[0]
        granted=copy.deepcopy(ordinary);granted.set("source","Item:fixture:granted skill")
        parent.insert(0,granted)
        for _ in range(3): ordinary.append(copy.deepcopy(ordinary.find("Gem")))
        result={"equipped":[{"slot":"Boots","id":1}],"gems":[{"group":1,"index":1,"source":granted.get("source")}] + [{"group":2,"index":i,"enabled":True,"support":i>1} for i in range(1,5)]}
        self.assertEqual(poe.socket_layout_issues(self.root,result),[])

    def test_shared_link_groups_cannot_hide_support_effects(self):
        changed = bx.apply_plan(self.root, {"mode": "rebuild", "summary": "two groups", "operations": {"replace_skills": [
            {"slot": "Boots", "gems": [{"skill_id": "ShieldCrush"}, {"skill_id": "SupportLifetap"}]},
            {"slot": "Boots", "gems": [{"skill_id": "Determination"}]}]}})
        result = {"equipped": [{"slot": "Boots", "id": 1}], "gems": [
            {"group": 1, "index": 1}, {"group": 1, "index": 2, "enabled": True, "support": True}, {"group": 2, "index": 1}]}
        self.assertTrue(any("共享物理连线" in error for error in poe.socket_layout_issues(changed, result)))
        bx.groups(changed)[0].set("enabled", "false")
        self.assertEqual(bx.groups(changed)[0].get("slot"), "Boots")
        self.assertTrue(poe.socket_layout_issues(changed, result))
        bx.groups(changed)[0].set("slot", "")
        self.assertEqual(poe.socket_layout_issues(changed, result), [])

    def test_disabled_granted_group_keeps_native_source_identity(self):
        group = bx.groups(self.root)[0]
        group.set("source", "Item:1:Fixture")
        group.set("enabled", "false")
        self.assertEqual(group.get("slot"), "Boots")
        self.assertEqual(group.get("source"), "Item:1:Fixture")
        self.assertEqual(group.get("enabled"), "false")

    def test_season_names_do_not_replace_observed_version_evidence(self):
        value=intent_for(self.root)
        value["league"]="未来赛季"
        self.assertEqual(poe.validate_intent(value,self.root)["league"],"未来赛季")
        with self.assertRaisesRegex(ValueError,"机制/宝石数据"):engine.validate_game_version("3.28","历史赛季","3.29")
        del value["game_version"]
        self.assertNotIn("game_version",poe.validate_intent(value,self.root))
        self.assertEqual(facts.version_fact(None,None,"未知",Path.cwd(),"fixture")["status"],"unverified")

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

    def test_readiness_leaves_points_and_conditions_to_explicit_judgment(self):
        result={"points":103,"ascendancy_points":6,"stats":{"GuardSkillActive":True},"equipped":[{"slot":"Flask 1","active":True}]}
        audit=resources.assess(result,intent_for(self.root),{})
        self.assertFalse(audit["errors"])
        self.assertEqual(audit["unused_points"]["ascendancy"],2)
        self.assertIn("condition:guard",audit["pending"])
        self.assertIn("condition:Flask 1",audit["pending"])
        result["ascendancy_points"]=9
        self.assertTrue(resources.assess(result,intent_for(self.root),{})["errors"])

    def test_usage_checks_declared_window_and_recovery_without_mode_switch(self):
        result=resource_fixture()
        result["stats"].update(ManaUnreserved=300,ManaCost=30,ManaPerSecondCost=60,ManaRegenRecovery=10)
        audit=resources.assess(result,{},{});self.assertIn("resource:Mana:usage",audit["pending"])
        usage={"skill_ref":"main","active_seconds":4,"recovery_seconds":20,"claim_id":"use"}
        audit=resources.assess(result,{}, {"usage":[usage]})
        self.assertEqual(audit["resources"][0]["maximum_burst_seconds"],6)
        self.assertEqual(audit["resources"][0]["recovery_seconds_at_regen_only"],20)
        self.assertFalse(audit["errors"])
        usage["recovery_seconds"]=19
        self.assertTrue(resources.assess(result,{}, {"usage":[usage]})["errors"])
        usage.update(continuous=True)
        self.assertTrue(resources.assess(result,{}, {"usage":[usage]})["errors"])

    def test_gem_upgrade_and_duplicate_gems_are_not_free_owned_inventory(self):
        old={"gems":[{"id":"SupportBrutality","level":19,"quality":0,"slot":"Boots"}],"equipped":[]}
        new={"gems":[{"id":"SupportBrutality","name":"Brutality","level":20,"quality":0,"slot":"Boots","group_enabled":False}]*2,"equipped":[]}
        needs=poe.material_requirements(self.root,self.root,old,new)
        self.assertEqual(needs[0]["quantity"],2)
        item={k:v for k,v in needs[0].items() if k in {"kind","skill_id","level","quality","quantity"}}
        ref=facts.material_ref(item)
        evidence={"held":{"evidence_id":"held","kind":"user_statement","subject":ref,"status":"user_declared","context":{"quantity":1}}}
        inventory={**item,"quantity":1,"evidence_refs":["held"]}
        result=poe.budget_summary({"inventory":[inventory]},intent_for(self.root),needs,evidence)
        self.assertEqual(result["materials"][0]["remaining"],1)
        self.assertEqual(result["status"],"unknown")

    def test_energy_shield_costs_recharge_conditions_and_self_degen(self):
        result=resource_fixture();result["resource_rules"]={"energy_shield_protects_mana":True}
        result["stats"].update(ManaUnreserved=0,ManaCost=20,ESPerSecondCost=100,EnergyShield=300,EnergyShieldRecharge=120,EnergyShieldRechargeDelay=2)
        audit=resources.assess(result,{},{});self.assertEqual(audit["resources"][-1]["net_cost"],100)
        claims=[{"claim_id":"charge","status":"model_assessed","subjects":["resource:EnergyShield:recharge"]}]
        audit=resources.assess(result,{}, {},claims);self.assertEqual(audit["resources"][-1]["net_cost"],-20)
        result["stats"]["EnergyShield"]=30
        self.assertIn("resource:EnergyShield:startup",resources.assess(result,{}, {},claims)["pending"])
        result["stats"].update(LifeRegenRecovery=100,LifeDegen=300)
        life=resources.assess(result,{}, {})["resources"][1]
        self.assertEqual(life["regeneration"],-200)
        del result["stats"]["LifeDegen"]
        self.assertIsNone(resources.assess(result,{}, {})["resources"][1]["regeneration"])

    def test_incomparable_conditions_do_not_emit_gain_percentage(self):
        before, after = {"stats": {"CombinedDPS": 10}}, {"stats": {"CombinedDPS": 30}}
        self.assertIsNone(poe.compare(before, after, False)["CombinedDPS"]["percent"])
        self.assertEqual(poe.compare(before, after)["CombinedDPS"]["percent"], 200)

    def test_automatic_trigger_costs_are_added_once_and_corpse_dependencies_explained(self):
        result=resource_fixture();result["stats"].update(ManaCost=10,ManaPerSecondCost=20,ManaRegenRecovery=30)
        result["active_skills"].append({"id":"auto","skill_ref":"auto","trigger":"native","costs":{"ManaCost":30,"ManaPerSecondCost":60,"LifeCost":0,"LifePerSecondCost":0,"ESCost":0,"ESPerSecondCost":0,"SkillTriggerRate":2}})
        audit=resources.assess(result,{},{});self.assertEqual(audit["resources"][0]["cost_per_second"],80)
        self.assertEqual(audit["resources"][0]["net_cost"],50)
        result["active_skills"][0].update(generals_cry=True,actor="mirage")
        result["stats"]["ManaLeechGainRate"]=999
        audit=resources.assess(result,{},{});self.assertEqual(audit["resources"][0]["leech_on_hit"],0)
        self.assertIn("dependency:main",audit["pending"])
        claims=[{"claim_id":"corpses","status":"model_assessed","subjects":["dependency:main"]}]
        self.assertNotIn("dependency:main",resources.assess(result,{}, {},claims)["pending"])



def route_fixture():
    return {"schema_version":2,"name":"离线路线","confirmed":True,"goal":"验证阶段结构","objectives":[{"objective_id":"fixture-goal","text":"验证阶段结构","evidence_refs":["user"]}],"league":"测试赛季","class_id":4,"game_version":"3.29","ascendancy":"Champion","scope":"所列阶段","trade_mode":"ssf",
            "constraint_sources":{"/goal":"user","/trade_mode":"user"},"gems":{"ShieldCrush":{}},
            "stages":[{"stage_id":"stage-"+str(i),"name":"测试阶段","level":level,"act":act,"entry_conditions":[],"bases":{"Gloves":"Iron Gauntlets"},
                       "skill_groups":[{"name":"输出","slot":"Gloves","use":"按需使用","gems":[{"instance_id":"one","skill_id":"ShieldCrush","level":1,"quality":0}]}],
                       "ascendancy_points":0,"ascendancy_nodes":[],"required_items":[]} for i,(level,act) in enumerate(((1,1),(12,2),(68,11)))]}


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.route=route_fixture()
        self.gems={"ShieldCrush":{"skill_id":"ShieldCrush","name":"Shield Crush","tags":{"grants_active_skill":True},"level_requirements":[{"gem_level":1,"character_level":1,"naturally_trainable":True,"strength":10,"dexterity":0,"intelligence":0,"base_resources":{"manaCost":6}},{"gem_level":20,"character_level":70,"naturally_trainable":True}]}}
        self.classes=[{"id":4,"name":"Duelist","ascendancies":[{"id":3,"name":"Champion"}]}]
        self.evidence={"user":{"kind":"user_statement","status":"user_declared","subject":"intent"}}
        self.bases={"Iron Gauntlets":{"name":"Iron Gauntlets","sockets":4,"valid_slots":["Gloves"]}}
        self.slots=[{"name":"Gloves"}]
    def check(self):
        return routes.validate(self.route,self.gems,"3.29",self.classes,bases=self.bases,slots=self.slots,evidence=self.evidence)
    def test_same_level_and_current_character_start(self):
        self.route["stages"]=self.route["stages"][1:];self.route["stages"][1]["level"]=12
        checked,_=self.check();self.assertEqual([s["level"] for s in checked["stages"]],[12,12])
    def test_unavailable_level_and_explicit_auto(self):
        gem=self.route["stages"][0]["skill_groups"][0]["gems"][0];gem["level"]=20
        with self.assertRaisesRegex(ValueError,"使用等级"):self.check()
        gem["level"]="auto";checked,_=self.check()
        self.assertEqual(checked["stages"][0]["skill_groups"][0]["gems"][0]["level"],1);self.assertEqual(gem["level"],"auto")
    def test_unknown_level_is_not_illegal_or_zero(self):
        self.route["stages"][0]["skill_groups"][0]["gems"][0]["level"]=31
        checked,_=self.check();self.assertIn("native:gem_level:one",checked["pending"])
        self.assertIsNone(checked["stages"][0]["attribute_requirements"]["strength"])
    def test_same_id_requires_distinct_instances(self):
        group=self.route["stages"][0]["skill_groups"][0];group["gems"].append(copy.deepcopy(group["gems"][0]))
        with self.assertRaisesRegex(ValueError,"两个位置"):self.check()
        group["gems"][1]["instance_id"]="two";checked,_=self.check();self.assertIn("acquisition:two",checked["pending"])
    def test_disabled_group_still_occupies_sockets(self):
        group=self.route["stages"][0]["skill_groups"][0];group["enabled"]=False
        group["gems"]=[dict(group["gems"][0],instance_id=str(i)) for i in range(5)]
        with self.assertRaisesRegex(ValueError,"孔位"):self.check()
    def test_slots_and_capacity_come_from_native_base(self):
        self.bases["Iron Gauntlets"]["valid_slots"]=["Future Slot"]
        with self.assertRaisesRegex(ValueError,"无法放入"):self.check()
        self.bases["Iron Gauntlets"].pop("valid_slots");checked,_=self.check();self.assertIn("native:base_slot:Iron Gauntlets",checked["pending"])
    def test_new_native_class_and_quality_do_not_hit_old_limits(self):
        self.route["class_id"]=17;self.classes[0]["id"]=17
        self.route["stages"][0]["skill_groups"][0]["gems"][0]["quality"]=60
        checked,_=self.check();self.assertEqual(checked["class_id"],17)
    def test_wrong_ascendancy(self):
        self.route["ascendancy"]="Another"
        with self.assertRaisesRegex(ValueError,"升华"):self.check()
    def test_ssf_trade_constraint_and_conditional_drop(self):
        need={"kind":"gem","instance_id":"one","skill_id":"ShieldCrush","level":1,"quality":0,"quantity":1,"acquisition":"trade"}
        self.route["stages"][0]["required_items"]=[need]
        with self.assertRaisesRegex(ValueError,"SSF"):self.check()
        need["acquisition"]="drop";checked,_=self.check();self.assertNotIn("fallback_skill_id",str(checked))
    def test_resources_and_attributes_do_not_pretend_full_build_calculation(self):
        checked,warnings=self.check();stage=checked["stages"][0]
        self.assertEqual(stage["attribute_requirements"]["strength"],10)
        self.assertEqual(stage["skill_groups"][0]["gems"][0]["base_resources"],{"manaCost":6})
        self.assertIn("未提供完整阶段快照",routes.render(checked,warnings,"future","3_future"))
    def test_instance_cannot_change_gem_id_silently(self):
        self.gems["Other"]={**self.gems["ShieldCrush"],"skill_id":"Other","name":"Other"};self.route["gems"]["Other"]={}
        self.route["stages"][1]["skill_groups"][0]["gems"][0]["skill_id"]="Other"
        with self.assertRaisesRegex(ValueError,"新的 instance_id"):self.check()
    def test_legacy_strategy_and_future_timestamp_rejected(self):
        self.route["version_status"]="verified"
        with self.assertRaisesRegex(ValueError,"旧字段"):self.check()
        with self.assertRaises(ValueError):engine.validate_checked_at((date.today()+timedelta(days=1)).isoformat())


    def test_key_transition_requires_actual_snapshot(self):
        self.route["stages"][0]["key_transition"]=True
        checked,_=self.check();self.assertIn("snapshot:"+self.route["stages"][0]["stage_id"],checked["pending"])
    def test_reused_instance_is_not_acquired_twice(self):
        need={"kind":"gem","instance_id":"one","skill_id":"ShieldCrush","level":1,"quality":0,"quantity":1,"acquisition":"owned"}
        self.route["stages"][0]["required_items"]=[need]
        self.route["stages"][1]["required_items"]=[copy.deepcopy(need)]
        with self.assertRaisesRegex(ValueError,"首次取得"):self.check()

    def test_acquired_and_used_levels_are_separate_training_states(self):
        need={"kind":"gem","instance_id":"one","skill_id":"ShieldCrush","level":1,"quality":0,"quantity":1,"acquisition":"owned"}
        self.route["stages"]=self.route["stages"][-1:]
        stage=self.route["stages"][0];stage["required_items"]=[need];stage["level"]=70
        stage["skill_groups"][0]["gems"][0]["level"]="auto"
        checked,_=self.check();transition=checked["stages"][0]["instance_transitions"][0]
        self.assertEqual((transition["acquired"]["level"],transition["used"]["level"]),(1,20))
        self.assertEqual(transition["kind"],"natural_training")
        self.assertIn(transition["required_subject"],checked["pending"])
        claims=[{"claim_id":"training","subjects":[transition["required_subject"]],"status":"model_assessed"}]
        checked,_=routes.validate(self.route,self.gems,"3.29",self.classes,bases=self.bases,slots=self.slots,evidence=self.evidence,claims=claims)
        self.assertNotIn(transition["required_subject"],checked["pending"])
        self.assertIn("不证明已发生",checked["stages"][0]["instance_transitions"][0]["scope"])
        self.assertEqual(stage["skill_groups"][0]["gems"][0]["level"],"auto")

    def test_acquisition_can_precede_first_use_without_matching_same_stage_group(self):
        self.gems["Spare"]={**self.gems["ShieldCrush"],"skill_id":"Spare","name":"Spare"}
        self.route["stages"][0]["required_items"]=[{"kind":"gem","instance_id":"spare","skill_id":"Spare","level":1,"quality":0,"quantity":1,"acquisition":"owned"}]
        self.route["stages"][1]["skill_groups"][0]["gems"][0].update(instance_id="spare",skill_id="Spare")
        self.route["stages"][2]["skill_groups"][0]["gems"][0].update(instance_id="spare",skill_id="Spare")
        checked,_=self.check()
        self.assertNotIn("acquisition:spare",checked["pending"])
        self.assertEqual(checked["stages"][1]["instance_transitions"][0]["acquired"]["stage_id"],"stage-0")

    def test_quality_changes_downgrades_and_untrainable_levels_need_transformation(self):
        original=copy.deepcopy(self.route)
        for old_level,used_level,quality,trainable in ((20,1,0,True),(1,1,20,True),(1,20,0,False)):
            with self.subTest(old=old_level,used=used_level,quality=quality,trainable=trainable):
                self.route=copy.deepcopy(original);self.route["stages"]=self.route["stages"][-1:];stage=self.route["stages"][0];stage["level"]=70
                stage["required_items"]=[{"kind":"gem","instance_id":"one","skill_id":"ShieldCrush","level":old_level,"quality":0,"quantity":1,"acquisition":"owned"}]
                stage["skill_groups"][0]["gems"][0].update(level=used_level,quality=quality)
                self.gems["ShieldCrush"]["level_requirements"][-1]["naturally_trainable"]=trainable
                checked,_=self.check();transition=checked["stages"][0]["instance_transitions"][0]
                self.assertEqual(transition["kind"],"transformation");self.assertIn(transition["required_subject"],checked["pending"])

    def test_instance_mismatch_returns_observed_expected_used_actual_and_location(self):
        self.route["stages"][0]["required_items"]=[{"kind":"gem","instance_id":"one","skill_id":"Other","level":1,"quality":0,"quantity":1,"acquisition":"owned"}]
        with self.assertRaises(bx.ContractError) as caught:self.check()
        diagnostic=caught.exception.diagnostic
        self.assertEqual(diagnostic["location"],"/stages/0/skill_groups/0/gems/0/skill_id")
        self.assertEqual((diagnostic["expected"],diagnostic["actual"]),("Other","ShieldCrush"))

    def test_route_cheap_shape_errors_have_paths_before_any_catalog(self):
        for field,value in (("bases",[]),("level",True),("stage_id","ambiguous#ref"),("snapshot",{"operations":{"tree":[]}})):
            route=copy.deepcopy(self.route);route["stages"][0][field]=value
            with self.subTest(field=field),self.assertRaises(bx.ContractError) as caught:routes.preflight(route)
            self.assertTrue(caught.exception.diagnostic["location"].startswith("/stages/0/"))

FACT_CLASSES=[{"id":i,"name":name} for i,name in enumerate(("Scion","Marauder","Ranger","Witch","Duelist","Templar","Shadow"))]


class FactTests(unittest.TestCase):
    def setUp(self):
        self.raw = '''<title>Example - PoEDB</title><h5>Quest /2 </h5><table><tr><th>Act</th></tr>
        <tr><td>1</td><td>Opening quest</td><td><i title="Quest Reward"></i>Quest Reward</td><td>Marauder</td></tr>
        <tr><td>1</td><td>Opening quest</td><td><i title="Quest Vendor"></i>Nessa</td><td>Marauder · Templar</td></tr></table>'''
        self.general = "Siosa in Act 3 sells all unleveled skill gems that can be received as quest rewards for quests you've completed, across all classes. Lilly Roth in Act 6, 10 and Karui Shores sells all unleveled skill gems after completing the Act 6 quest Fallen from Grace."
        self.rows = facts.quest_rows(self.raw, "Example", FACT_CLASSES)

    def test_quest_and_vendor_rows_preserve_actual_class_scope(self):
        self.assertEqual(len(self.rows), 2)
        self.assertEqual(self.rows[0]["class_ids"], [1])
        self.assertEqual(self.rows[1]["class_ids"], [1, 5])
        self.assertEqual(self.rows[1]["npc"], "Nessa")
        self.assertEqual(facts.quest_rows(self.raw, "Different gem"), [])

    def test_all_classes_quest_rows_are_explicit_and_not_inferred_from_unknown_labels(self):
        raw = self.raw.replace("Marauder · Templar", "All Classes").replace("Marauder</td>", "All Classes</td>")
        rows = facts.quest_rows(raw, "Example", FACT_CLASSES)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["class_ids"] == list(range(7)) for row in rows))
        self.assertEqual(facts.quest_rows(raw.replace("All Classes", "Unknown class group"), "Example"), [])

    def test_universal_vendor_does_not_invent_unlock_rows(self):
        self.assertEqual(facts.universal_rows(self.rows,self.general,FACT_CLASSES),[])
        row={"acquisition":"vendor","available_from_act":3,"npc":"Siosa","quest":"Observed quest","class_ids":[4]}
        expanded=facts.universal_rows([row],self.general,FACT_CLASSES)
        self.assertEqual(expanded[0]["quest"],"Observed quest");self.assertEqual(expanded[0]["class_ids"],list(range(7)))

    def test_acquisition_cannot_broaden_classes_or_invent_early_vendor(self):
        need={"kind":"gem","skill_id":"Example","instance_id":"one","level":1,"quality":0,"quantity":1,"acquisition":"vendor","available_from_act":1,"npc":"Nessa","quest":"Opening quest","class_ids":list(range(7))}
        route={"class_id":1,"stages":[{"stage_id":"start","required_items":[need]}]}
        proof={"Example":{"status":"source_checked","choices":self.rows,"sources":[]}}
        with self.assertRaisesRegex(ValueError,"扩大"):facts.reconcile_acquisitions(route,proof)
        need["class_ids"]=[1];route["class_id"]=3
        with self.assertRaisesRegex(ValueError,"唯一获取行"):facts.reconcile_acquisitions(route,proof)
        route["class_id"]=1
        _,unresolved=facts.reconcile_acquisitions(route,proof)
        self.assertEqual(need["evidence_status"],"source_observed");self.assertIn("acquisition:one",unresolved)

    def test_unknown_sources_stay_unknown(self):
        need={"kind":"gem","skill_id":"Example","instance_id":"one","level":1,"quality":0,"quantity":1,"acquisition":"vendor"}
        route={"class_id":1,"stages":[{"stage_id":"start","required_items":[need]}]}
        _,unresolved=facts.reconcile_acquisitions(route,{"Example":{"status":"unverified","reason":"timeout"}})
        self.assertEqual(unresolved,["acquisition:one"])
        with patch.object(facts,"fetch",side_effect=OSError("offline")):
            proof=facts.gem_facts({"Example":{"skill_id":"Example","name":"Example","level_requirements":[{"gem_level":1,"base_resources":{"manaReservationPercent":23}}]}},".unused","test")
        self.assertEqual(proof["Example"]["status"],"unverified")
        self.assertEqual(proof["Example"]["native"]["level_requirements"][0]["base_resources"]["manaReservationPercent"],23)

    def test_support_names_use_the_actual_source_page(self):
        record = {"html": self.raw.replace("Example -", "Example Support -"), "url": "https://poedb.tw/us/Example_Support", "checked_at": "2026-10-04", "sha256": "fixture"}
        with patch.object(facts, "fetch", return_value=record) as fetch:
            result = facts.gem_facts({"SupportExample": {"skill_id": "SupportExample", "name": "Example", "tags": {"support": True}}}, ".unused", "test", classes=FACT_CLASSES)
        self.assertEqual(fetch.call_args.args[0], record["url"])
        self.assertEqual(result["SupportExample"]["status"], "source_checked")

    def test_source_league_marker_must_match_target_not_history_text(self):
        raw = '<a title="Example v3.30" class="league_bar_icon"></a>' + self.raw + '<p>Old 3.29 changes</p>'
        record = {"html": raw, "url": "https://poedb.tw/us/Example", "checked_at": "2026-10-04", "sha256": "fixture"}
        with patch.object(facts, "fetch", return_value=record):
            result = facts.gem_facts({"Example": {"skill_id": "Example", "name": "Example"}}, ".unused", "test", "3.29")
            self.assertEqual(result["Example"]["status"], "unverified")
            self.assertEqual(result["Example"]["source_game_version"], "3.30")
            result = facts.gem_facts({"Example": {"skill_id": "Example", "name": "Example"}}, ".unused", "test", "3.30", FACT_CLASSES)
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
            key=json.dumps({"url":url,"context":"old"},sort_keys=True,ensure_ascii=False,allow_nan=False)
            path = Path(temp) / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")
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
            url = engine.VERSION_SOURCE_HINTS["3.29"]["url"]
            record = {"html": '<div id="newsDetail"><h2>S30 patch</h2><p>在3.29.0版本中，测试挑战赛季开放。</p><p>' + "other updates " * 20000 + '</p></div>',
                      "url": url, "checked_at": "2026-10-04", "sha256": "fixture"}
            info = {"data_version": "3.29", "engine_fingerprint": "fixture", "version_source_hint": {"url": url, "status": "unverified_hint"}}
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
                    self.assertEqual(poe.main(["research", "--version", "--league", "S29", "--out", str(output)]), 0)
                self.assertEqual(poe.read_json(output)["version_evidence"]["status"], "unverified")

    def test_new_season_research_accepts_new_source_and_preserves_missing_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "version.json"
            info = {"data_version": "3.30", "engine_fingerprint": "future", "version_source_hint": None}
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

    def test_trial_rows_do_not_infer_completion_or_point_rewards(self):
        raw='<a class="league_bar_icon" title="Current v3.29"></a><table><tr><th>Area</th><th>Level</th></tr><tr><td>New Labyrinth</td><td>Act 12 New Trial</td><td>90</td><td>reward</td></tr></table>'
        record={"html":raw,"url":"https://poedb.tw/us/The_Lords_Labyrinth","checked_at":"2026-10-04","sha256":"fixture"}
        with patch.object(facts,"fetch",return_value=record):proof=facts.trial_facts(".unused","fixture","3.29")
        self.assertEqual(proof["status"],"source_checked");self.assertEqual(proof["labyrinths"][0]["observed_acts"],[12]);self.assertNotIn("points",proof["labyrinths"][0])
        route={"stages":[{"stage_id":"s","ascendancy_used":4,"level":100}]}
        self.assertTrue(facts.reconcile_trial_progress(route,proof)[1])
        route["claims"]=[{"claim_id":"p","subjects":["progress:s"],"status":"model_assessed"}]
        self.assertFalse(facts.reconcile_trial_progress(route,proof)[1])


class NativeBoundaryTests(unittest.TestCase):
    def test_missing_cost_is_only_zero_with_observed_no_cost_flag(self):
        result=resource_fixture();del result["stats"]["ManaCost"];del result["stats"]["ManaPerSecondCost"]
        claims=[{"claim_id":"unrelated","subjects":["resource:Mana"],"status":"model_assessed"}]
        audit=resources.assess(result,{}, {},claims)
        self.assertIn("native:resource:Mana",audit["pending"]);self.assertIsNone(audit["resources"][0]["cost_per_second"])
        result["active_skills"][0]["no_cost"]=True
        audit=resources.assess(result,{},{});self.assertEqual(audit["resources"][0]["cost_per_second"],0)
        self.assertIsNone(audit["resources"][0]["native_cost_per_second"])
    def test_uncovered_native_cost_is_preserved_as_gap(self):
        result=resource_fixture();result["stats"]["FutureResourceCost"]=12
        audit=resources.assess(result,{},{});self.assertIn("capability:resource_cost:main:FutureResourceCost",audit["pending"])
        self.assertEqual(audit["additional_native_costs"][0]["value"],12)
    def test_manual_secondary_skill_without_frequency_cannot_be_declared_free(self):
        result=resource_fixture();result["active_skills"].append({"id":"movement","skill_ref":"movement","costs":{"ManaCost":20}})
        claims=[{"claim_id":"reason","status":"model_assessed","subjects":["skill:movement"]}]
        self.assertIn("usage:movement",resources.assess(result,{}, {},claims)["pending"])


class EvidenceTests(unittest.TestCase):
    def test_typed_claim_checks_reject_false_assertions_and_keep_unknowns(self):
        ref="native:candidate#/stats/Future";claim={"claim_id":"c","text":"{{fact:"+ref+"}}","subjects":["goal"],"checks":[{"reference":ref,"op":"ge","value":10}]}
        for value,status in ((11,"supported"),(9,"refuted"),(None,"unknown"),(True,"unknown"),("11","unknown")):
            with self.subTest(value=value):
                row=facts.assess_claims([claim],{},{"candidate":{"stats":{"Future":value}}})[0]
                self.assertEqual(row["check_status"],status)
                self.assertEqual(row["status"],"model_assessed" if status=="supported" else "unresolved")
                self.assertEqual(row["checks"][0]["actual"],value)
        for op,expected in (("gte",10),("eq",float("inf")),("eq",[])):
            with self.subTest(op=op,expected=expected),self.assertRaises(ValueError):facts.check_value(None,op,expected)
        self.assertEqual(facts.check_value(True,"eq",1),"unknown")

    def test_current_display_definitions_control_unit_scale_and_minion_actor(self):
        result={"stats":{"Future":.5,"Unlabelled":100},"minion":{"Future":.2},"stat_metadata":{"Future":[{"label":"Future ratio","format":".1f%%","display_multiplier":100,"actor":"player"}],"MinionFuture":[{"label":"Other per hit","format":".1f","actor":"minion"}]},"config_values":{"enemyIsBoss":"None"},"engine_fingerprint":"v-next"}
        row=facts.resolve_reference("native:candidate#/stats/Future",{},{"candidate":result})
        self.assertTrue(row["formatted_fact"].endswith("50%"));self.assertEqual(row["context"]["config_values"],result["config_values"])
        row=facts.resolve_reference("native:candidate#/minion/Future",{},{"candidate":result})
        self.assertEqual((row["meaning"]["unit"],row["meaning"]["actor"]),("/hit","minion"))
        result["stat_metadata"]["Future"].append({"label":"Other display","format":".1f%%","display_multiplier":1})
        row=facts.resolve_reference("native:candidate#/stats/Future",{},{"candidate":result})
        self.assertEqual(row["meaning"]["unit_status"],"unknown");self.assertIn("0.5",row["formatted_fact"])
        self.assertEqual(facts.resolve_reference("native:candidate#/stats/Unlabelled",{},{"candidate":result})["meaning"]["unit_status"],"unknown")

    def test_fact_text_keeps_qualified_object_and_scalar_value_distinct(self):
        result={"catalog":[{"id":99,"name":"Same","qualified_name":"Future class / Other branch / Same (#99)","owner_class_id":17,"stats":["actual native effect"]}]}
        ref="native:catalog#/catalog/0/stats";claim={"claim_id":"c","text":"{{fact:"+ref+"}}","subjects":["goal"]}
        row=facts.assess_claims([claim],{},{"catalog":result})[0]
        self.assertEqual(row["status"],"model_assessed");self.assertIn("Other branch",row["rendered_text"])
        self.assertEqual(row["observations"][0]["meaning"]["object_identity"]["owner_class_id"],17)
        claim["text"]="{{value:"+ref+"}}"
        self.assertEqual(facts.assess_claims([claim],{},{"catalog":result})[0]["status"],"unresolved")

    def test_missing_measurement_cannot_pass_even_with_matching_review_token(self):
        ref="native:candidate#/stats/X";claim={"claim_id":"c","text":"at least 10","subjects":["goal"],"checks":[{"reference":ref,"op":"ge","value":10}]}
        checked=facts.assess_claims([claim],{},{"candidate":{"stats":{}}})
        packet,_=facts.final_review({}, {},checked,{}, {})
        _,pending=facts.final_review({}, {},checked,{}, {"review":{"token":packet["token"],"findings":"Reviewed current missing values."}})
        self.assertFalse(pending);self.assertFalse(facts.has_claim(checked,"goal"));self.assertEqual(checked[0]["check_status"],"unknown")

    def test_removed_native_value_clears_old_rendering_in_reused_registry(self):
        ref="native:candidate#/stats/Future";registry={}
        claim={"claim_id":"c","text":"{{fact:"+ref+"}}","subjects":["goal"],"checks":[{"reference":ref,"op":"eq","value":99}]}
        self.assertEqual(facts.assess_claims([claim],registry,{"candidate":{"stats":{"Future":99}}})[0]["check_status"],"supported")
        row=facts.assess_claims([claim],registry,{"candidate":{"stats":{}}})[0]
        self.assertEqual(row["check_status"],"unknown");self.assertNotIn(ref,registry)
        self.assertNotIn("= 99",row["rendered_text"]);self.assertIn("未取得",row["rendered_text"])

    def test_bad_operator_cannot_hide_behind_missing_entry_value(self):
        with self.assertRaises(ValueError):facts.entry_checks([{"check_id":"x","locator":"/missing","op":"gte","value":1,"claim_id":"c"}],{},[],"stage:s")

    def test_budget_tri_state_and_publication_follow_actual_bound_and_completeness(self):
        need={"kind":"gem","skill_id":"Example","level":1,"quality":0,"quantity":1}
        intent={"budget":{"amount":3,"currency":"divine"}}
        unknown=poe.budget_summary({},intent,[need],{},[])
        self.assertIsNone(unknown["within_limit"])
        within=poe.budget_summary({"inventory":[{**need,"evidence_refs":["held"]}]},intent,[need],{"held":{"kind":"user_statement","status":"user_declared","subject":facts.material_ref(need),"context":{"quantity":1}}},[])
        self.assertTrue(within["within_limit"])
        for value,expected in ((unknown,"unknown"),(within,"supported"),(dict(unknown,within_limit=False),"refuted")):
            with self.subTest(expected=expected):
                packet=poe.publication("draft",[],["budget"],value,{})
                self.assertEqual(packet["statements"][1]["status"],expected)
                self.assertIn("草案",packet["facts_summary"])
        ref="native:budget#/within_limit"
        checked=facts.assess_claims([{"claim_id":"b","text":"within","subjects":["goal"],"checks":[{"reference":ref,"op":"eq","value":True}]}],{},{"budget":unknown})[0]
        self.assertEqual(checked["check_status"],"unknown")
    def test_observed_artifact_is_not_verified_gameplay_and_forged_status_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);(folder/"source.txt").write_text("Actual visible price 2 divine",encoding="utf-8")
            declaration={"evidence_id":"price","kind":"artifact","subject":"item:1","path":"source.txt","excerpt":"price 2 divine"}
            registry=facts.load_evidence([declaration],folder,{})
            self.assertEqual(registry["price"]["status"],"artifact_observed")
            self.assertEqual(registry["price"]["origin"],"agent_supplied_artifact")
            with self.assertRaises(ValueError):facts.load_evidence([dict(declaration,status="verified")],folder,{})
            declaration["excerpt"]="invented price";self.assertEqual(facts.load_evidence([declaration],folder,{})["price"]["status"],"unresolved")
    def test_native_pointer_must_resolve_in_current_calculation(self):
        claims=[{"claim_id":"life","text":"Native life value","subjects":["goal"],"evidence_refs":["native:candidate#/stats/Life"]}]
        evidence={};native={"candidate":{"stats":{"Life":123},"engine_fingerprint":"new","xml":"<fixture/>"}}
        checked=facts.assess_claims(claims,evidence,native)
        self.assertEqual(checked[0]["status"],"model_assessed");self.assertEqual(evidence[claims[0]["evidence_refs"][0]]["value"],123)
        native["candidate"]["stats"]={}
        self.assertEqual(facts.assess_claims(claims,{},native)[0]["status"],"unresolved")
    def test_claim_uncertainty_cannot_be_cleared_by_nonempty_reference(self):
        claims=[{"claim_id":"c","text":"uncertain","subjects":["goal"],"evidence_refs":["user"],"uncertainties":["not tested"]}]
        self.assertEqual(facts.assess_claims(claims,{"user":{"status":"user_declared"}})[0]["status"],"unresolved")
    def test_quote_requires_matching_realm_subject_specs_and_claim_reference(self):
        need={"kind":"item","item_id":1,"spec_sha256":"spec","quantity":2}
        quote={**need,"low":6,"high":8,"currency":"divine","evidence_refs":["p"]}
        evidence={"p":{"evidence_id":"p","kind":"artifact","subject":"item:1","status":"artifact_observed","checked_at":"2026-10-04","context":{"realm":"cn","league":"test","currency":"divine","quantity":2,"low":6,"high":8,"spec_sha256":"spec"}}}
        claims=[{"claim_id":"q","status":"model_assessed","subjects":["quote:item:1"],"evidence_refs":["p"]}]
        intent={"league":"test","budget":{"amount":10,"currency":"divine"}}
        result=poe.budget_summary({"shopping":[quote]},intent,[need],evidence,claims)
        self.assertEqual((result["low"],result["high"]),(6,8))
        self.assertEqual(result["status"],"quote_assessed")
        for key,value in (("realm","international"),("quantity",1),("spec_sha256","other")):
            changed=copy.deepcopy(evidence);changed["p"]["context"][key]=value
            with self.subTest(key=key):self.assertNotEqual(poe.budget_summary({"shopping":[quote]},intent,[need],changed,claims)["status"],"quote_assessed")
        claims[0]["evidence_refs"]=["unrelated"]
        self.assertNotEqual(poe.budget_summary({"shopping":[quote]},intent,[need],evidence,claims)["status"],"quote_assessed")
    def test_known_required_lower_bound_and_missing_lines_are_separate(self):
        need={"kind":"gem","skill_id":"Example","level":1,"quality":0,"quantity":2};ref=facts.material_ref(need)
        row={**need,"low":4,"high":6,"currency":"divine","evidence_refs":["p"]}
        proof={"p":{"evidence_id":"p","kind":"web","subject":ref,"status":"source_observed","checked_at":"2026-10-04","context":{"realm":"cn","league":"test","currency":"divine","quantity":2,"low":4,"high":6}}}
        claims=[{"claim_id":"q","status":"model_assessed","subjects":["quote:"+ref],"evidence_refs":["p"]}]
        intent={"league":"test","budget":{"amount":3,"currency":"divine"}}
        result=poe.budget_summary({"shopping":[row]},intent,[need,{"kind":"service","service_id":"respec","quantity":1}],proof,claims)
        self.assertEqual(result["status"],"over_budget");self.assertEqual(result["known_lower_bound"],4);self.assertFalse(result["totals_complete"])
    def test_inventory_evidence_cannot_be_repeated_to_multiply_quantity(self):
        row={"kind":"gem","skill_id":"Example","level":1,"quality":0,"quantity":2,"evidence_refs":["held","held"]}
        with self.assertRaisesRegex(ValueError,"不重复"):poe.budget_summary({"inventory":[row]}, {})
    def test_metric_registry_is_not_a_fixed_whitelist(self):
        root=bx.parse(SAMPLE);intent=intent_for(root);intent["minimum_stats"]={"FutureMetric":1.5};intent["constraint_sources"]["/minimum_stats"]="user"
        self.assertEqual(poe.validate_intent(intent,root)["minimum_stats"],{"FutureMetric":1.5})
    def test_missing_capability_does_not_block_unrelated_catalog(self):
        engine.validate_contract({"catalog":[{"id":"future"}],"capabilities":{}},catalog_only=True)
        with self.assertRaises(ValueError):engine.validate_contract({"catalog":[{"id":"future"}]})


class ReadOnlyDataTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/"Portable"
        for name in ("Data","Modules","Classes","TreeData/3_90","TreeData/3_91","Builds"):
            (self.root/name).mkdir(parents=True)
        (self.root/"manifest.xml").write_text('<Manifest><Version number="99.0"/></Manifest>',encoding="utf-8")
        (self.root/"GameVersions.lua").write_text('treeVersionList = {"3_90", "3_91"}',encoding="utf-8")
        (self.root/"changelog.txt").write_text('VERSION[99.0]\n--- 3.91 Test',encoding="utf-8")
        (self.root/"lua51.dll").write_bytes(b"fixture only")
        (self.root/"Modules/Build.lua").write_text('return {}',encoding="utf-8")
        (self.root/"Data/Skills.lua").write_text('return {\n  name = "Test skill",\n  cost = 12,\n  note = "Test description",\n}',encoding="utf-8")
        (self.root/"Data/seed.zip.part0").write_bytes(b"binary fixture")
        for name in ("Settings.xml","Builds/private.lua"):
            (self.root/name).write_text('private Test',encoding="utf-8")
        for version in ("3_90","3_91"):
            (self.root/"TreeData"/version/"tree.lua").write_text('Test '+version,encoding="utf-8")
        self.translations=self.base/"Data/Translate/zh-rCN";self.translations.mkdir(parents=True)
        (self.translations/"tree_sd.csv").write_text('Test description,测试描述\n',encoding="utf-8")

    def test_public_sources_use_virtual_names_instead_of_installation_paths(self):
        listing=engine.query_sources(self.root,limit=200)
        self.assertTrue(listing["entries"])
        self.assertTrue(all("path" not in row for row in listing["entries"]))
        receipt=engine.read_source(self.root,"engine/Data/Skills.lua",2,2)
        self.assertNotIn("path",receipt)
        self.assertEqual(receipt["source"],"engine/Data/Skills.lua")
        self.assertNotIn(str(self.root),json.dumps(listing))
        self.assertNotIn(str(self.root),json.dumps(receipt))

    def test_saved_data_read_provides_view_action_for_workspace_receipt(self):
        output=self.base/"receipt.json";stdout=io.StringIO()
        with patch.object(engine,"resolve_root",return_value=self.root),redirect_stdout(stdout):
            self.assertEqual(poe.main(["data","--mode","read","--path","engine/Data/Skills.lua","--line","2","--limit","2","--out",str(output)]),0)
        response=json.loads(stdout.getvalue());action=response["next_actions"][0]
        self.assertEqual(action["action"],"view")
        self.assertEqual(action["file"],str(output))
        self.assertEqual(action["pointer"],"")
        self.assertIn("text",action["fields"])
        self.assertIn("evidence_template",action["fields"])
        self.assertTrue(response["read_only"])
        result=poe.view_json(args(file=str(output),pointer=action["pointer"],fields=",".join(action["fields"]),keys=False,query="",offset=0,limit=20))
        self.assertIn('cost = 12',result["value"]["text"])
        self.assertEqual(result["missing_fields"],[])
        self.assertEqual(response["runtime"]["worker_calls"],0)

    def test_inventory_search_paging_and_current_tree_discovery(self):
        listing=engine.query_sources(self.root,limit=200)
        names={r["source"] for r in listing["entries"]}
        self.assertIn("engine/Data/seed.zip.part0",names)
        self.assertNotIn("engine/Settings.xml",names);self.assertNotIn("engine/Builds/private.lua",names)
        self.assertIn("engine/TreeData/3_91/tree.lua",names);self.assertNotIn("engine/TreeData/3_90/tree.lua",names)
        result=engine.query_sources(self.root,mode="search",path="engine/Data",query="Test",limit=1)
        self.assertEqual((result["total"],result["next_offset"]),(2,1))
        other=engine.query_sources(self.root,mode="search",path="engine/Data",query="Test",limit=1,offset=1)
        self.assertEqual(other["entries"][0]["line"],4);self.assertIsNone(other["next_offset"])
        self.assertEqual(result["skipped"][0]["reason"],"archive")
        historical=engine.query_sources(self.root,path="engine/TreeData/3_90")
        self.assertEqual(historical["total"],1)
        (self.root/"TreeData/3_92").mkdir();(self.root/"TreeData/3_92/tree.lua").write_text("new",encoding="utf-8")
        (self.root/"GameVersions.lua").write_text('treeVersionList = {"3_90", "3_91", "3_92"}',encoding="utf-8")
        self.assertEqual(engine.query_sources(self.root)["current_tree_prefix"],"engine/TreeData/3_92")

    def test_local_receipt_is_bound_to_bytes_and_does_not_write_installation(self):
        before={str(p):bx.digest(p.read_bytes()) for p in self.base.rglob("*") if p.is_file()}
        receipt=engine.read_source(self.root,"engine/Data/Skills.lua",2,2)
        self.assertEqual([r["line"] for r in receipt["lines"]],[2,3])
        declaration={**receipt["evidence_template"],"evidence_id":"cost","subject":"fixture"}
        evidence=facts.load_evidence([declaration],self.base,{"engine_fingerprint":"fixture"},root=self.root)
        self.assertEqual(evidence["cost"]["status"],"local_data_observed")
        self.assertEqual(before,{str(p):bx.digest(p.read_bytes()) for p in self.base.rglob("*") if p.is_file()})
        (self.root/"Data/Skills.lua").write_text('return {cost=99}',encoding="utf-8")
        changed=facts.load_evidence([declaration],self.base,{},root=self.root)
        self.assertEqual(changed["cost"]["status"],"unresolved")
        self.assertNotIn("text",changed["cost"])

    def test_read_rejects_private_paths_traversal_archives_and_outside_links(self):
        for source in ("engine/Settings.xml","engine/Builds/private.lua","../Settings.xml","engine/Data/../../Settings.xml",str(self.root/"Settings.xml"),"engine/Data/seed.zip.part0"):
            with self.subTest(source=source),self.assertRaises(ValueError): engine.read_source(self.root,source)
        for source in ("engine/Data/Skills.lua",):
            with self.assertRaises(ValueError): engine.read_source(self.root,source,0,10)
            with self.assertRaises(ValueError): engine.read_source(self.root,source,1,201)
        link=self.root/"Data/link.lua"
        try: link.symlink_to(self.root/"Builds/private.lua")
        except OSError: self.skipTest("Windows symbolic-link creation unavailable")
        with self.assertRaises(ValueError): engine.read_source(self.root,"engine/Data/link.lua")

    def test_cli_output_cannot_write_to_application_or_translations(self):
        engine.resolve_root(str(self.root))
        for path in (self.root/"Data/out.json",self.translations/"out.json"):
            with self.assertRaises(ValueError): poe.write_json(path,{"bad":"write"})
            self.assertFalse(path.exists())
        output=self.base/"workspace/result.json"
        with redirect_stdout(io.StringIO()):
            code=poe.main(["data","--pob",str(self.root),"--mode","read","--path","engine/Data/Skills.lua","--out",str(output)])
        self.assertEqual(code,0);self.assertTrue(poe.read_json(output)["read_only"])
        with patch.object(engine,"PROTECTED_ROOTS",set()),self.assertRaises(ValueError):
            poe.write_json(self.root/"not-registered.json",{})
        with patch.object(facts,"urlopen") as network,self.assertRaises(ValueError):
            facts.fetch("https://poedb.tw/us/Gem",self.root/"Data/.poe-evidence","fixture")
        network.assert_not_called();self.assertFalse((self.root/"Data/.poe-evidence").exists())

    def test_fingerprint_covers_translated_descriptions_and_binary_tables(self):
        original=engine.fingerprint(self.root)
        (self.translations/"tree_sd.csv").write_text("Test,更新\n",encoding="utf-8")
        changed=engine.fingerprint(self.root);self.assertNotEqual(original,changed)
        (self.root/"Data/seed.zip.part0").write_bytes(b"changed binary")
        self.assertNotEqual(changed,engine.fingerprint(self.root))


class FinalReviewTests(unittest.TestCase):
    def test_semantics_uses_native_save_defaults_without_dropping_all_false_values(self):
        common={"class":"fixture","ascendancy":"fixture","level":1,"tree_version":"future","nodes":[],"main_skill":"x","main_group":1,"gems":[],"equipped":[]}
        explicit={**common,"config_values":{"default_false":False,"default_true":False},"config_saved_values":{"default_true":False}}
        omitted={**common,"config_values":{"default_true":False},"config_saved_values":{"default_true":False}}
        self.assertEqual(poe.semantics(explicit),poe.semantics(omitted))
        changed={**common,"config_values":{},"config_saved_values":{}}
        self.assertNotEqual(poe.semantics(explicit),poe.semantics(changed))

    def test_review_binds_facts_conditions_prose_and_source_content(self):
        spec={"summary":"A conditional recommendation","assessment":{"scope":"fixture"}}
        claims=[{"claim_id":"c","text":"Main skill only","subjects":["goal"],"evidence_refs":["native:candidate#/stats/Life"]}]
        evidence={"source":{"status":"artifact_observed","digest":"one","recorded_at":"earlier"}}
        native={"candidate":{"stats":{"Life":100},"xml_sha256":"a","engine_fingerprint":"v"}}
        assessed=facts.assess_claims(claims,evidence,native)
        packet,pending=facts.final_review(spec,evidence,assessed,native,spec["assessment"])
        self.assertIn("final_review:missing",pending)
        spec["assessment"]["review"]={"token":packet["token"],"findings":"Checked object, numbers, causality, alternatives, conditions and scope.","unresolved":[]}
        evidence["source"]["recorded_at"]="later"
        checked,pending=facts.final_review(spec,evidence,assessed,native,spec["assessment"])
        self.assertFalse(pending);self.assertEqual(checked["status"],"model_reviewed")
        for object_,key,value in ((native["candidate"]["stats"],"Life",101),(native["candidate"],"xml_sha256","b"),(evidence["source"],"digest","two"),(spec,"summary","different conclusion"),(claims[0],"text","unreviewed claim")):
            prior=object_[key];object_[key]=value
            current=facts.assess_claims(claims,evidence,native)
            _,pending=facts.final_review(spec,evidence,current,native,spec["assessment"])
            self.assertIn("final_review:stale",pending)
            object_[key]=prior
        self.assertEqual(assessed[0]["observations"][0]["value"],100)

    def test_review_unresolved_findings_stay_visible(self):
        packet,_=facts.final_review({}, {}, [], {}, {})
        review={"review":{"token":packet["token"],"findings":"Temporal coverage unknown.","unresolved":["coverage"]}}
        checked,pending=facts.final_review({}, {}, [], {}, review)
        self.assertEqual(checked["status"],"unresolved");self.assertIn("final_review:coverage",pending)

    def test_resource_prose_cannot_replace_numeric_usage_or_startup(self):
        result=resource_fixture();result["stats"].update(ManaCost=20,ManaPerSecondCost=60,ManaRegenRecovery=10)
        claim={"claim_id":"text","status":"model_assessed","subjects":["resource:Mana:usage"],"text":"Drink a potion or cast slowly"}
        self.assertIn("resource:Mana:usage",resources.assess(result,{}, {},[claim])["pending"])
        result["stats"].update(EnergyShield=10,ESCost=1,ESPerSecondCost=50,EnergyShieldRecharge=60,EnergyShieldRechargeDelay=2)
        claim["subjects"]=["resource:EnergyShield:recharge","resource:EnergyShield:startup"]
        self.assertIn("resource:EnergyShield:startup",resources.assess(result,{}, {},[claim])["pending"])

    def test_paced_cost_cannot_keep_full_speed_leech(self):
        result=resource_fixture();result["stats"].update(ManaCost=20,ManaPerSecondCost=60,ManaLeechGainRate=50)
        result["active_skills"][0]["costs"]={"Speed":3,"ManaCost":20,"LifeCost":0,"ESCost":0}
        audit=resources.assess(result,{}, {"usage":[{"skill_ref":"main","casts_per_second":1}]})
        self.assertIn("capability:resource:Mana:paced_leech",audit["pending"])
        self.assertIsNone(audit["resources"][0]["sustainable_while_hitting"])

    def test_flask_prose_cannot_certify_continuous_coverage(self):
        result=resource_fixture();result["equipped"]=[{"slot":"Flask 1","id":1,"active":True,"flask_local":{"duration":5,"chargesMax":30,"chargesUsed":10}}]
        claim={"claim_id":"flask","status":"model_assessed","subjects":["condition:Flask 1"]}
        audit=resources.assess(result,{}, {"usage":[{"skill_ref":"main","continuous":True}]},[claim])
        self.assertIn("capability:flask_coverage:Flask 1",audit["pending"])
        self.assertEqual(audit["conditions"]["flask_facts"][0]["flask_local"]["chargesUsed"],10)

    def test_large_evaluation_terminal_summary_points_to_complete_workspace_record(self):
        output=io.StringIO()
        long={"status":"draft","candidate":"fixture","pending":["unknown:"+str(i)+"x"*80 for i in range(200)],"readiness":{"details":"x"*20000}}
        with patch.object(poe,"evaluate",return_value=long),redirect_stdout(output):
            code=poe.main(["evaluate","--run",str(ROOT/".work/fixture"),"--name","test","--plan","fixture.json"])
        response=json.loads(output.getvalue())
        self.assertEqual(code,0);self.assertEqual(response["pending_total"],200)
        self.assertEqual(len(response["pending"]),12);self.assertLess(len(output.getvalue()),12000)
        self.assertTrue(response["details_file"].endswith("evaluation.json"))

    def test_stage_targets_use_actual_values_and_preserve_unmet_conditions(self):
        claim={"claim_id":"ready","status":"model_assessed","subjects":["stage:a"]}
        check={"check_id":"chosen-target","locator":"/stats/CustomNewMetric","op":"ge","value":50,"claim_id":"ready"}
        rows,pending=facts.entry_checks([check],{"stats":{"CustomNewMetric":49}},[claim],"stage:a")
        self.assertEqual(rows[0]["status"],"unmet");self.assertTrue(pending)
        rows,pending=facts.entry_checks([check],{"stats":{"CustomNewMetric":50}},[claim],"stage:a")
        self.assertEqual(rows[0]["status"],"met");self.assertFalse(pending)
        self.assertEqual(facts.entry_checks([check],{},[claim],"stage:a")[0][0]["status"],"unknown")
        self.assertTrue(facts.entry_checks([check],{"stats":{"CustomNewMetric":50}},[claim],"stage:b")[1])


class WorkflowInterfaceTests(unittest.TestCase):
    def test_probe_preserves_native_diagnostic_and_maps_variant_source(self):
        with tempfile.TemporaryDirectory() as folder:
            run=Path(folder);poe.write_text(run/'original.xml',SAMPLE)
            variant_file=run/'variants.json';poe.write_json(variant_file,{'variants':[{'id':'custom','operations':{}}]})
            record=(run,{'intent':{}},{'plan':{'mode':'refine'}},SAMPLE,run)
            cases=[('edit_input','blocked'),('inspect_capability','unsupported'),('inspect_error','failed')]
            for action,status in cases:
                diagnostic={'code':'native_observation','action':action,'location':'/operations/items/7/craft/affixes/2','expected':20,'actual':1}
                with self.subTest(status=status),patch.object(poe,'read_candidate',return_value=record),patch.object(engine,'calculate',return_value={'active_skills':[]}),patch.object(poe,'materialize_items',side_effect=engine.EngineError('observed failure',diagnostic=diagnostic)):
                    result=poe.probe(args(run=str(run),name='fixture',variants=str(variant_file)))['alternatives'][0]
                    self.assertEqual(result['diagnostic']['location'],'/variants/0/operations/items/7/craft/affixes/2')
                    self.assertEqual(result['diagnostic']['expected'],20)
                    self.assertEqual(result['diagnostic']['input_file'],str(variant_file.resolve()))
                    self.assertEqual(result['status'],status)

    def test_route_native_diagnostics_distinguish_input_capability_and_failure(self):
        stage={'stage_id':'dynamic','level':12,'ascendancy_allocated_nodes':[],'skill_groups':[],
               'snapshot':{'operations':{}}}
        route={'class_id':1,'stages':[stage]}
        native={'xml':SAMPLE,'engine_fingerprint':'fixture','classes':[{'id':1,'ascendancies':[]}]}
        cases=[({'code':'native_invalid_input','action':'edit_input','location':'/operations/items/7/craft/affixes/2','expected':20,'actual':1},'blocked'),
               ({'code':'native_capability_unavailable','action':'inspect_capability'},'unsupported'),
               (None,'failed')]
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'route.html'
            poe.write_json(output.with_suffix('.state.json'),{})
            for diagnostic,status in cases:
                with self.subTest(status=status),patch.object(poe,'materialize_items',side_effect=engine.EngineError('observed failure',diagnostic=diagnostic)):
                    result=poe.route_snapshots(route,native,Path(folder),Path(folder),output)['dynamic']
                    self.assertEqual(result['status'],status)
                    self.assertNotIn('stats',result)
                    if status=='blocked':
                        self.assertEqual(result['diagnostic']['location'],'/stages/0/snapshot/operations/items/7/craft/affixes/2')
                        self.assertEqual(result['diagnostic']['expected'],20)

    def test_delivery_actions_prioritize_actual_dependencies_over_review(self):
        outcome={'status':'blocked','errors':['specific input conflict'],'pending':['objective:custom','stage:any:material:item:9','final_review:missing']}
        actions=poe.delivery_actions(outcome,'source.json','result.json','review.json','/checked_route')
        self.assertEqual([row['action'] for row in actions],['repair_input','assess_dependencies','review_current_result'])
        self.assertEqual(actions[1]['categories'],{'material':1,'objective':1})
        self.assertEqual(actions[1]['pointer'],'/checked_route/pending')
        self.assertEqual(poe.delivery_actions({'status':'scoped_result','errors':[],'pending':[]},'a','b','c'),[])

    def test_calculation_facts_exposes_current_keys_without_inventing_values(self):
        result={'stats':{'SeasonSpecificObservation':0,'SeasonSpecificFlag':False,'Life':1}}
        observed=poe.calculation_facts(result)
        self.assertEqual(observed['available_stat_keys'],sorted(result['stats']))
        self.assertNotIn('SeasonSpecificObservation',observed['stats'])
        referenced=poe.calculation_facts(result,[{'evidence_refs':['native:candidate#/stats/SeasonSpecificObservation','native:candidate#/stats/SeasonSpecificFlag']}])
        self.assertEqual(referenced['stats'],result['stats'])

    def test_view_discovers_current_metric_keys_and_filters_without_recalculation(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'current.json'
            poe.write_json(path,{'stats':{'NewSeasonMechanic':0,'NewSeasonEnabled':False,'OtherMetric':4},'pending':['objective:custom','capability:new']})
            before=bx.digest(path.read_bytes())
            with patch.object(engine,'calculate',side_effect=AssertionError('no recalculation')):
                for pointer,options,expected in [('/stats',['--keys','--query','newseason'],['NewSeasonEnabled','NewSeasonMechanic']),('/pending',['--query','objective:'],['objective:custom'])]:
                    stdout=io.StringIO()
                    with redirect_stdout(stdout):code=poe.main(['view','--file',str(path),'--pointer',pointer,*options])
                    self.assertEqual(code,0);self.assertEqual(json.loads(stdout.getvalue())['value'],expected)
            self.assertEqual(bx.digest(path.read_bytes()),before)

    def test_native_recipe_error_is_structured_and_remapped_to_source_item(self):
        diagnostic={'code':'native_invalid_input','action':'edit_input','location':'/crafts/0/affixes/0','expected':{'minimum_item_level':20},'actual':{'item_level':1}}
        error=engine.EngineError('native rule failed',diagnostic=diagnostic)
        plan={'operations':{'items':[{'id':1,'text':'unchanged'},{'id':2,'craft':{'base':'current base','ilvl':1,'sockets':'R','affixes':[{'id':'current-id'}]}}]}}
        with patch.object(engine,'calculate',side_effect=error),self.assertRaises(engine.EngineError) as caught:
            poe.materialize_items(plan,Path('unused'))
        self.assertEqual(caught.exception.diagnostic['location'],'/operations/items/1/craft/affixes/0')
        self.assertEqual(caught.exception.diagnostic['expected'],diagnostic['expected'])

    def test_empty_skill_group_is_valid_array_and_missing_gems_has_location(self):
        plan={'mode':'refine','summary':'interface placeholder','operations':{'replace_skills':[{'slot':'Boots','gems':[{'skill_id':'ShieldCrush'}]},{'enabled':False,'slot':'','gems':[]}]}}
        changed=bx.apply_plan(bx.parse(SAMPLE),plan)
        self.assertEqual(len(bx.groups(changed)),2);self.assertFalse(bx.groups(changed)[1].findall('Gem'))
        del plan['operations']['replace_skills'][1]['gems']
        with self.assertRaises(bx.ContractError) as caught:bx.apply_plan(bx.parse(SAMPLE),plan)
        self.assertEqual(caught.exception.diagnostic['location'],'/operations/replace_skills/1/gems')

    def test_web_cache_namespaces_contexts_and_preserves_previous_observation(self):
        from email.message import Message
        class Response:
            url='https://poedb.tw/us/fixture'
            headers=Message()
            def __init__(self,body):self.body=body
            def read(self,size):return self.body[:size]
            def __enter__(self):return self
            def __exit__(self,*args):pass
        with tempfile.TemporaryDirectory() as folder,patch.object(facts,'urlopen',side_effect=[Response(b'<p>first source</p>'),Response(b'<p>other context</p>')]) as network:
            a=facts.fetch(Response.url,folder,{'engine':'current','league':'user'})
            facts.fetch(Response.url,folder,'current')
            again=facts.fetch(Response.url,folder,{'league':'user','engine':'current'})
            self.assertEqual(network.call_count,2)
            self.assertEqual(a,again)
            self.assertEqual(len(list(Path(folder).glob('*.json'))),2)

    def test_view_large_saved_json_reads_only_requested_rows_fields_without_engine(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'catalog.json'
            poe.write_json(path,{'entries':[{'id':str(i),'cost':0,'active':False,'large_levels':'x'*20000} for i in range(5)]})
            original=bx.digest(path.read_bytes());stdout=io.StringIO()
            with patch.object(engine,'calculate',side_effect=AssertionError('view is read only')),redirect_stdout(stdout):
                code=poe.main(['view','--file',str(path),'--pointer','/entries','--fields','id,cost,active','--limit','2','--offset','1'])
            response=json.loads(stdout.getvalue())
            self.assertEqual(code,0);self.assertEqual(response['total'],5);self.assertEqual(response['next_offset'],3)
            self.assertEqual(response['value'][0],{'id':'1','cost':0,'active':False})
            self.assertLess(len(stdout.getvalue()),2000);self.assertEqual(bx.digest(path.read_bytes()),original)

    def test_view_missing_pointer_and_oversized_selection_do_not_return_fabricated_value(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'evidence.json';poe.write_json(path,{'large':'x'*20000})
            for pointer in ('/missing','/large'):
                stderr=io.StringIO()
                with redirect_stderr(stderr):code=poe.main(['view','--file',str(path),'--pointer',pointer])
                response=json.loads(stderr.getvalue());self.assertEqual(code,2);self.assertFalse(response['ok'])
                self.assertNotIn('value',response)

    def test_objective_id_cannot_replace_subject_and_diagnosis_names_field(self):
        objectives=[{'objective_id':'custom','text':'User chosen goal','evidence_refs':['user']}]
        claims=[{'claim_id':'objective:custom','subjects':['goal'],'status':'model_assessed'}]
        rows,pending=facts.objective_coverage(objectives,{'user':{'kind':'user_statement','status':'user_declared'}},claims)
        self.assertIn('objective:custom',pending)
        self.assertEqual(rows[0]['required_subject'],'objective:custom')
        self.assertEqual(rows[0]['diagnostics'][0]['location'],'/claims')
        self.assertIn('subjects',rows[0]['diagnostics'][0]['message'])

    def test_unresolved_source_diagnosis_keeps_claim_field_and_fetch_reason(self):
        evidence={'source':{'kind':'web','status':'unresolved','reason':'HTTP Error 404'}}
        rows=facts.assess_claims([{'claim_id':'a','text':'Conditional decision','subjects':['stage:custom'],'evidence_refs':['source']}],evidence)
        diagnostic=rows[0]['diagnostics'][0]
        self.assertEqual(diagnostic['location'],'/claims/0/evidence_refs')
        self.assertEqual(diagnostic['reference'],'source')
        self.assertIn('404',diagnostic['message'])

    def test_delivery_brief_counts_all_gaps_including_late_objective_and_stage(self):
        pending=['config:'+str(i) for i in range(30)]+['objective:later','stage:custom:material:item:1','capability:new_native_feature']
        brief=facts.delivery_brief('draft',[],pending)
        self.assertEqual(brief['pending_total'],33)
        self.assertEqual(brief['unresolved_categories']['objective'],1)
        self.assertEqual(brief['unresolved_categories']['material'],1)
        self.assertEqual(brief['unresolved_categories']['capability'],1)
        self.assertIn('草案',brief['label'])

    def test_shared_equipment_changes_detect_same_id_new_spec_and_report_point_budgets(self):
        original=bx.parse(SAMPLE);changed=copy.deepcopy(original)
        changed.find('Items/Item').text+='\n+10 to maximum Life'
        before={'nodes':[1,2],'gems':[],'equipped':[{'slot':'Boots','id':1,'base':'Iron Greaves','name':'Boots'}],'points':2,'ascendancy_points':0}
        after={**copy.deepcopy(before),'nodes':[1,2,3,4],'points':3,'ascendancy_points':1}
        changes=poe.equipment_changes(original,changed,before,after)
        self.assertEqual(changes['changed_slots'],['Boots'])
        self.assertFalse(changes['replaced_items'])
        summary=poe.actual_changes(before,after,changes)
        self.assertTrue(any('Boots' in row for row in summary))
        self.assertTrue(any('普通天赋' in row and '2' in row and '3' in row for row in summary))
        self.assertTrue(any('升华' in row and '0' in row and '1' in row for row in summary))

    def test_snapshot_key_error_returns_exact_field_and_allowed_keys(self):
        with self.assertRaises(bx.ContractError) as caught:
            bx.require_keys({'index':1,'enabled':True},{'index','group_count'},'/stages/2/snapshot/operations/groups/0')
        diagnostic=caught.exception.diagnostic
        self.assertEqual(diagnostic['location'],'/stages/2/snapshot/operations/groups/0')
        self.assertEqual(diagnostic['actual'],['enabled'])
        self.assertEqual(diagnostic['expected'],['group_count','index'])

    def test_research_returns_source_templates_using_observed_row_not_guessed_url(self):
        raw='<title>Example Support - PoEDB</title><h5>Quest /1</h5><table><tr><td>7</td><td>Observed quest</td><td><i title="Quest Vendor"></i>Observed NPC</td><td>Witch</td></tr></table>'
        record={'html':raw,'url':'https://poedb.tw/us/Example_Support','checked_at':'2026-10-04','sha256':'fixture'}
        with patch.object(facts,'fetch',return_value=record):
            result=facts.gem_facts({'SupportExample':{'skill_id':'SupportExample','name':'Example','tags':{'support':True}}},'.unused','fixture',classes=FACT_CLASSES)
            template=result['SupportExample']['choices'][0]['evidence_templates'][0]
            self.assertEqual(template['url'],record['url'])
            self.assertIn(template['excerpt'],facts.text_of(raw))
            declared={**template,'evidence_id':'observed','subject':'gem:SupportExample:1:0'}
            resolved=facts.load_evidence([declared],ROOT/'.work','fixture')
        self.assertEqual(resolved['observed']['status'],'source_observed')

    def test_objective_coverage_tracks_each_user_requirement(self):
        objectives=[{'objective_id':'recover','text':'停手恢复','evidence_refs':['user']},
                    {'objective_id':'easy','text':'减少操作','evidence_refs':['user']}]
        evidence={'user':{'kind':'user_statement','status':'user_declared'}}
        claims=[{'claim_id':'recover','subjects':['objective:recover'],'status':'model_assessed','evidence_refs':['native:candidate#/stats/LifeRegenRecovery'],'conditions':[],'uncertainties':[]}]
        rows,pending=facts.objective_coverage(objectives,evidence,claims)
        self.assertEqual(rows[0]['status'],'model_assessed')
        self.assertEqual(rows[1]['status'],'unresolved')
        self.assertIn('objective:easy',pending)

    def test_objective_statement_cannot_be_invented_by_model(self):
        rows,pending=facts.objective_coverage([{'objective_id':'new','text':'New preference','evidence_refs':['web']}],
            {'web':{'kind':'web','status':'source_observed'}},[])
        self.assertEqual(rows[0]['basis_status'],'unresolved')
        self.assertTrue(pending)

    def test_inline_native_values_follow_current_calculation(self):
        raw=[{'claim_id':'life','text':'生命 {{value:native:candidate#/stats/Life}}。','subjects':['goal'],'evidence_refs':['native:candidate#/stats/Life']}]
        native={'candidate':{'stats':{'Life':1234}}}
        observed=facts.assess_claims(raw,{},native)
        self.assertEqual(observed[0]['rendered_text'],'生命 1234。')
        native['candidate']['stats']['Life']=4321
        self.assertEqual(facts.assess_claims(raw,{},native)[0]['rendered_text'],'生命 4321。')

    def test_inline_value_without_real_reference_stays_unresolved(self):
        raw=[{'claim_id':'bad','text':'生命 {{value:native:candidate#/stats/Missing}}。','subjects':['goal'],'evidence_refs':['native:candidate#/stats/Life']}]
        observed=facts.assess_claims(raw,{}, {'candidate':{'stats':{'Life':100}}})
        self.assertEqual(observed[0]['status'],'unresolved')

    def test_cli_error_has_field_and_source_file(self):
        error=bx.ContractError('Frequency invalid',location='/usage/1/casts_per_second',expected=1,actual=100)
        output=io.StringIO()
        with patch.object(poe,'evaluate',side_effect=error),redirect_stderr(output):
            code=poe.main(['evaluate','--run','fixture','--name','test','--plan','input-plan.json'])
        result=json.loads(output.getvalue())
        self.assertEqual(code,2)
        self.assertEqual(result['diagnostic']['location'],'/usage/1/casts_per_second')
        self.assertTrue(result['diagnostic']['input_file'].endswith('input-plan.json'))

    def test_status_detects_candidate_tampering_without_calculation(self):
        with tempfile.TemporaryDirectory() as folder:
            run=Path(folder);xml=bx.serialise(bx.parse(SAMPLE));poe.write_text(run/'original.xml',xml)
            plan=run/'source-plan.json';poe.write_json(plan,{'operations':{}})
            candidate=run/'candidates/test';poe.write_text(candidate/'candidate.xml',xml);poe.write_text(candidate/'requested.xml',xml)
            value={'schema_version':2,'skill_version':poe.VERSION,'intent_hash':None,'original_sha256':bx.digest(xml),'xml_sha256':bx.digest(xml),'requested_sha256':bx.digest(xml),
                   'source_plan':str(plan),'source_plan_sha256':bx.digest(plan.read_bytes()),'engine_fingerprint':'fixture','status':'draft','pending':['budget'],'errors':[]}
            poe.write_json(candidate/'evaluation.json',value)
            poe.write_json(run/'run.json',{'schema_version':2,'invoked':True,'phase':'evaluated','original_sha256':bx.digest(xml),'pob_path':str(run),
                                          'candidates':{'test':{'evaluation_sha256':bx.digest((candidate/'evaluation.json').read_bytes())}}})
            with patch.object(engine,'resolve_root',return_value=run),patch.object(engine,'check',return_value={'engine_fingerprint':'fixture'}),patch.object(engine,'calculate',side_effect=AssertionError('status must not calculate')):
                first=poe.workflow_status(args(run=str(run)))
                self.assertTrue(first['candidate_records'][0]['valid'])
                self.assertEqual(first['candidate_records'][0]['plan_file'],str(plan))
                poe.write_text(candidate/'candidate.xml',xml+' ')
                second=poe.workflow_status(args(run=str(run)))
                self.assertFalse(second['candidate_records'][0]['valid'])
                self.assertTrue(any(a['action']=='reevaluate' for a in second['next_actions']))
                poe.write_text(candidate/'candidate.xml',xml)
                poe.write_json(plan,{'operations':{'main_group':2}})
                self.assertFalse(poe.workflow_status(args(run=str(run)))['candidate_records'][0]['valid'])
                with self.assertRaisesRegex(ValueError,'计划'):
                    poe.read_candidate(args(run=str(run),name='test'))

    def test_route_status_requires_current_input_output_and_engine(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'route.json';output=root/'route.html';state=root/'route.state.json'
            poe.write_json(source,{'goal':'fixture'});poe.write_text(output,'fixture report')
            poe.write_json(state,{'input':str(source),'input_sha256':bx.digest(source.read_bytes()),'output':str(output),'phase':'exported','file_exported':True,
                                 'stages':{'a':{'status':'calculated'},'b':{'status':'unsupported'}},
                                 'engine_fingerprint':'fixture','files':{str(output):bx.digest(output.read_bytes())}})
            with patch.object(engine,'resolve_root',return_value=root),patch.object(engine,'fingerprint',return_value='fixture'):
                self.assertTrue(poe.workflow_status(args(route_state=str(state)))['file_exported'])
                self.assertEqual(poe.workflow_status(args(route_state=str(state)))['completed_stages'],['a'])
                poe.write_json(source,{'goal':'changed'})
                result=poe.workflow_status(args(route_state=str(state)))
                self.assertFalse(result['file_exported'])
                self.assertEqual(result['next_actions'][0]['action'],'resume_route')


class ResourceRegressionTests(unittest.TestCase):
    def secondary(self, result, **costs):
        result['active_skills'].append({'id':'secondary','skill_ref':'secondary','actor':'player',
            'costs':{'ManaCost':10,'LifeCost':0,'ESCost':0,'ManaPerSecondCost':10,'ESPerSecondCost':0,'Speed':1,**costs}})
        return {'claim_id':'secondary','status':'model_assessed','subjects':['skill:secondary']}

    def test_secondary_energy_shield_payment_uses_native_rule(self):
        result=resource_fixture()
        result['resource_rules']={'energy_shield_protects_mana':True}
        result['stats'].update(Mana=0,EnergyShield=100,EnergyShieldRegenRecovery=100)
        claim=self.secondary(result,ManaCost=20,ManaPerSecondCost=0,ESPerSecondCost=40,Speed=2)
        audit=resources.assess(result,{}, {'usage':[{'skill_ref':'secondary','casts_per_second':1,'claim_id':'secondary'}]},[claim])
        self.assertFalse(audit['errors'])
        mana,_,shield=audit['resources']
        self.assertEqual(mana['manual_cost_per_second'],0)
        self.assertEqual(shield['manual_cost_per_second'],20)
        self.assertEqual(shield['single_cost'],20)

    def test_secondary_continuous_use_cannot_borrow_main_window(self):
        result=resource_fixture();result['stats'].update(Mana=50,ManaRegenRecovery=5)
        claim=self.secondary(result)
        usage=[{'skill_ref':'main','active_seconds':1,'recovery_seconds':1},
               {'skill_ref':'secondary','casts_per_second':1,'continuous':True,'claim_id':'secondary'}]
        audit=resources.assess(result,{}, {'usage':usage},[claim])
        self.assertTrue(audit['errors'] or any('timing' in gap for gap in audit['pending']))

    def test_secondary_frequency_cannot_exceed_native_rate(self):
        result=resource_fixture();result['stats']['ManaRegenRecovery']=1000
        claim=self.secondary(result)
        audit=resources.assess(result,{}, {'usage':[{'skill_ref':'secondary','casts_per_second':100,'claim_id':'secondary'}]},[claim])
        self.assertTrue(audit['errors'])

    def test_secondary_per_second_cost_is_not_silently_free(self):
        result=resource_fixture()
        self.secondary(result,ManaCost=0,ManaPerSecondCost=30)
        audit=resources.assess(result,{}, {})
        self.assertTrue(any('secondary' in gap for gap in audit['pending']))

    def test_different_active_windows_preserve_timing_gap(self):
        result=resource_fixture();result['stats'].update(ManaCost=10,ManaPerSecondCost=10,ManaRegenRecovery=5)
        claim=self.secondary(result)
        usage=[{'skill_ref':'main','active_seconds':1,'recovery_seconds':3},
               {'skill_ref':'secondary','casts_per_second':1,'active_seconds':20,'recovery_seconds':0,'claim_id':'secondary'}]
        audit=resources.assess(result,{}, {'usage':usage},[claim])
        self.assertTrue(audit['errors'] or any('timing' in gap for gap in audit['pending']))


class TimelineTests(unittest.TestCase):
    def fixture(self):
        result=resource_fixture();result["resource_rules"]={"energy_shield_protects_mana":False}
        for index,cost in enumerate((20,10)):
            ref="main" if index==0 else "move"
            costs={"ManaCost":cost,"ManaPerSecondCost":cost*2,"LifeCost":0,"LifePerSecondCost":0,"ESCost":0,"ESPerSecondCost":0,"Speed":2}
            skill={"skill_ref":ref,"is_main":index==0,"actor":"player","costs":costs}
            if index==0:result["active_skills"][0]=skill;result["stats"].update(costs)
            else:result["active_skills"].append(skill)
        result["stats"].update(Mana=50,ManaRegenRecovery=20)
        timeline={"duration_seconds":3,"events":[{"skill_ref":"main","at":0},{"skill_ref":"move","at":.5},{"skill_ref":"main","at":1}]}
        return result,timeline

    def test_shared_events_are_paid_on_one_clock_and_order_invariant(self):
        result,timeline=self.fixture();audit=resources.timeline_assessment(result,timeline)
        self.assertEqual(audit["status"],"supported");self.assertTrue(audit["resources"][0]["repeatable"])
        self.assertEqual([r["after"] for r in audit["resources"][0]["trace"]],[30,30,20])
        timeline["events"].reverse()
        reordered=resources.timeline_assessment(result,timeline)
        self.assertEqual([r["after"] for r in reordered["resources"][0]["trace"]],[30,30,20])

    def test_timeline_claim_links_do_not_require_duplicate_frequency_or_window(self):
        result,timeline=self.fixture()
        claims=[{"claim_id":ref,"subjects":["skill:"+ref],"status":"model_assessed"} for ref in ("main","move")]
        usage=[{"skill_ref":ref,"claim_id":ref} for ref in ("main","move")]
        audit=resources.assess(result,{}, {"timeline":timeline,"usage":usage},claims)
        self.assertEqual(audit["timeline"]["status"],"supported")
        self.assertFalse(audit["errors"]);self.assertFalse(audit["pending"])

    def test_fractional_positive_life_is_not_dead_and_negative_fees_are_unknown(self):
        result,timeline=self.fixture();result["stats"].update(Life=1,LifeDegen=.1)
        row=resources.timeline_assessment(result,timeline)["resources"][1]
        self.assertEqual(row["status"],"supported");self.assertFalse(row["repeatable"])
        result,timeline=self.fixture();result["active_skills"][0]["costs"].update(ManaCost=-1,ManaPerSecondCost=-2)
        self.assertEqual(resources.timeline_assessment(result,timeline)["resources"][0]["status"],"unknown")

    def test_positive_average_cannot_hide_initial_combined_payment_failure(self):
        result,timeline=self.fixture();result["stats"]["Mana"]=25
        timeline["events"][1]["at"]=0
        audit=resources.timeline_assessment(result,timeline)
        self.assertEqual(audit["status"],"refuted")
        self.assertGreater(20*3,sum(r["payment"] for r in audit["resources"][0]["trace"]))
        self.assertFalse(audit["resources"][0]["trace"][1]["affordable"])

    def test_first_window_possible_does_not_prove_infinite_repetition(self):
        result,timeline=self.fixture();result["stats"]["ManaRegenRecovery"]=1
        audit=resources.timeline_assessment(result,timeline)
        self.assertEqual(audit["resources"][0]["status"],"supported")
        self.assertFalse(audit["resources"][0]["repeatable"]);self.assertEqual(audit["status"],"unknown")
        packet=poe.publication("draft",[],audit["pending"],{"status":"unknown"},{},readiness={"timeline":audit})
        self.assertEqual(packet["statements"][2]["status"],"unknown")

    def test_missing_payment_rule_or_non_linear_or_automatic_cost_stays_unknown(self):
        original,timeline=self.fixture()
        for mutation in ("rule","linear","automatic","payment"):
            result=copy.deepcopy(original)
            if mutation=="rule":result["resource_rules"]={}
            elif mutation=="linear":result["active_skills"][0]["costs"]["ManaPerSecondCost"]=99
            elif mutation=="payment":del result["active_skills"][0]["costs"]["ManaCost"]
            else:result["active_skills"].append({"skill_ref":"automatic","trigger":True,"costs":{"ManaPerSecondCost":2}})
            with self.subTest(mutation=mutation):self.assertEqual(resources.timeline_assessment(result,timeline)["resources"][0]["status"],"unknown")

    def test_secondary_payment_with_native_energy_shield_rule_and_three_way_boundaries(self):
        for shield_covers_mana in (False,True):
            for pool in (15,50):
                for regen in (0,20):
                    result,timeline=self.fixture();result["resource_rules"]["energy_shield_protects_mana"]=shield_covers_mana
                    for skill in result["active_skills"]:
                        if shield_covers_mana:skill["costs"]["ESPerSecondCost"]=skill["costs"]["ManaPerSecondCost"];skill["costs"]["ManaPerSecondCost"]=0
                    resource="EnergyShield" if shield_covers_mana else "Mana"
                    result["stats"][resource]=pool;result["stats"][resource+"RegenRecovery"]=regen
                    with self.subTest(eb=shield_covers_mana,pool=pool,regen=regen):
                        audit=resources.timeline_assessment(result,timeline);row=next(r for r in audit["resources"] if r["resource_id"]==resource)
                        self.assertEqual(row["trace"][0]["payment"],20)
                        self.assertEqual(row["status"],"refuted" if pool<20 else "supported")
                        self.assertEqual(row["repeatable"],pool>=20 and regen==20)

    def test_frequency_checks_wraparound_and_does_not_change_damage(self):
        result,timeline=self.fixture();result["stats"]["CombinedDPS"]=1234
        timeline["events"]=[{"skill_ref":"main","at":0},{"skill_ref":"move","at":1},{"skill_ref":"main","at":2.9}]
        self.assertTrue(resources.timeline_assessment(result,timeline)["errors"])
        self.assertEqual(result["stats"]["CombinedDPS"],1234)
        timeline["events"][2]["at"]=.1
        audit=resources.timeline_assessment(result,timeline)
        self.assertEqual(audit["diagnostics"][0]["location"],"/timeline/events/2/at")

    def test_unknown_future_cost_or_regeneration_cannot_be_certified_by_timeline(self):
        result,timeline=self.fixture();del result["stats"]["ManaRegenRecovery"]
        self.assertEqual(resources.timeline_assessment(result,timeline)["resources"][0]["status"],"unknown")
        result,timeline=self.fixture();result["active_skills"][0]["costs"]["FutureResourceCost"]=8
        audit=resources.assess(result,{}, {"timeline":timeline})
        self.assertTrue(any("FutureResourceCost" in p for p in audit["pending"]))
        with self.assertRaises(bx.ContractError):resources.assess(result,{}, {"timeline":timeline,"usage":[{"skill_ref":"main","casts_per_second":1}]})


class EditAndRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.folder=Path(self.temp.name)
        self.source=self.folder/"route.json";self.request=self.folder/"change.json"
        self.root_patch=patch.object(engine,"resolve_root",return_value=self.folder);self.root_patch.start();self.addCleanup(self.root_patch.stop)
        self.engine_patch=patch.object(engine,"fingerprint",return_value="fixture");self.engine_patch.start();self.addCleanup(self.engine_patch.stop)

    def edit(self, change, revision=None, kind="route"):
        poe.write_json(self.request,change)
        return poe.edit_document(args(kind=kind,file=str(self.source),changes=str(self.request),if_sha256=revision,invoked=True))

    def test_save_direction_then_increment_stage_with_generated_handles_and_retry(self):
        first=self.edit({"set":{"name":"Provisional direction","goal":"user direction"}})
        self.assertEqual(first["status"],"saved_draft");self.assertNotIn("stages",poe.read_json(self.source))
        request={"set":{"stages":[{"name":"Early","skill_groups":[{"name":"main","slot":"","use":"manual","gems":[{"skill_id":"FutureSkill","level":1}]}]}]}}
        second=self.edit(request,first["revision"]);saved=poe.read_json(self.source);gem=saved["stages"][0]["skill_groups"][0]["gems"][0]
        self.assertTrue(saved["stages"][0]["stage_id"]);self.assertTrue(gem["instance_id"])
        self.assertEqual(second["stage_instances"][0]["instances"][0]["instance_id"],gem["instance_id"])
        self.assertTrue(self.edit(request,first["revision"])["replayed"])
        self.assertEqual(poe.read_json(self.source),saved)
        self.assertEqual(routes.gem_ids(saved),["FutureSkill"])

    def test_record_patch_retains_other_stages_null_is_data_and_remove_is_explicit(self):
        first=self.edit({"set":{"stages":[{"stage_id":"early","name":"Early"},{"stage_id":"later","name":"Later"}],"allow_mule":False}})
        second=self.edit({"set":{"stages":[{"stage_id":"early","level":None}]}},first["revision"])
        value=poe.read_json(self.source);self.assertEqual(len(value["stages"]),2);self.assertIsNone(value["stages"][0]["level"])
        self.edit({"set":{},"remove":["/allow_mule"]},second["revision"])
        self.assertNotIn("allow_mule",poe.read_json(self.source));self.assertIsNone(poe.read_json(self.source)["stages"][0]["level"])

    def test_revision_conflict_invalid_shape_and_ambiguous_instance_do_not_mutate(self):
        first=self.edit({"set":{"name":"original"}});before=self.source.read_bytes()
        cases=[({"set":{"name":"stale"}},"stale"),({"set":{"stages":[{"stage_id":"s","snapshot":{"operations":{"level":9}}}]}},first["revision"]),
               ({"set":{"stages":[{"stage_id":"s","skill_groups":[{"gems":[{"skill_id":"Same","level":1},{"skill_id":"Same","level":1}]}],"required_items":[{"kind":"gem","skill_id":"Same","level":1,"quality":0,"quantity":1}]}]}},first["revision"])]
        for change,revision in cases:
            with self.subTest(change=change),self.assertRaises(bx.ContractError):self.edit(change,revision)
            self.assertEqual(self.source.read_bytes(),before)

    def test_atomic_replace_failure_keeps_previous_file_and_cleans_temporary(self):
        poe.write_text(self.source,"old complete file")
        with patch.object(Path,"replace",side_effect=OSError("disk unavailable")),self.assertRaises(OSError):poe.write_text(self.source,"new partial file")
        self.assertEqual(self.source.read_text(),"old complete file");self.assertFalse(list(self.folder.glob("*.tmp")))

    def test_nonfinite_and_duplicate_json_rejected_before_native_calls(self):
        for text in ('{"x":NaN}','{"x":1e309}','{"x":1,"x":2}','{"nested":{"x":1,"x":2}}'):
            self.source.write_text(text,encoding="utf-8")
            with self.subTest(text=text),self.assertRaises(bx.ContractError):poe.read_json(self.source)

    def test_route_preparation_failure_persists_exact_repair_and_never_publishes(self):
        output=self.folder/"route.html"
        poe.write_json(self.source,{"schema_version":2,"confirmed":True,"stages":[{"stage_id":"s","snapshot":{"operations":{"tree":[]}}}]})
        with patch.object(poe,"native_queries") as worker,self.assertRaises(bx.ContractError):poe.route_command(args(invoked=True,file=str(self.source),out=str(output)))
        worker.assert_not_called();state=poe.read_json(output.with_suffix(".state.json"))
        self.assertEqual(state["phase"],"failed");self.assertFalse(state["file_exported"])
        self.assertEqual(state["failure"]["diagnostic"]["location"],"/stages/0/snapshot/operations/tree")
        status=poe.workflow_status(args(route_state=str(output.with_suffix(".state.json"))))
        self.assertEqual(status["next_actions"][0]["action"],"edit_input")

    def test_failed_resume_keeps_matching_progress_but_changed_input_invalidates_it(self):
        output=self.folder/"route.html";state_file=output.with_suffix(".state.json")
        poe.write_json(self.source,{"schema_version":2})
        previous={"input":str(self.source.resolve()),"input_sha256":bx.digest(self.source.read_bytes()),"engine_fingerprint":"fixture","stages":{"done":{"status":"calculated","native_calculated":True}}}
        poe.write_json(state_file,previous)
        with patch.object(poe,"calculate_route",side_effect=OSError("interrupted")),self.assertRaises(OSError):poe.route_command(args(invoked=True,file=str(self.source),out=str(output)))
        self.assertIn("done",poe.read_json(state_file)["stages"])
        poe.write_json(self.source,{"schema_version":2,"name":"changed"})
        with patch.object(poe,"calculate_route",side_effect=OSError("interrupted")),self.assertRaises(OSError):poe.route_command(args(invoked=True,file=str(self.source),out=str(output)))
        self.assertFalse(poe.workflow_status(args(route_state=str(state_file)))["completed_stages"])

    def test_failed_export_cli_invalidates_receipt_preserving_prior_artifact(self):
        run=self.folder/"run";run.mkdir();output=run/"output"/"valid";output.mkdir(parents=True)
        xml=output/"配装草案.xml";poe.write_text(xml,"prior valid artifact")
        poe.write_json(output/"delivery.json",{"file_exported":True,"files":{"配装草案.xml":bx.digest(xml.read_bytes())}})
        state={"phase":"exported","candidates":{"valid":{"status":"draft"}}};stderr=io.StringIO()
        poe.write_json(run/"run.json",state)
        with patch.object(poe,"read_run",return_value=(run,state)),patch.object(poe,"export",side_effect=OSError("render failed")),redirect_stderr(stderr):
            self.assertEqual(poe.main(["export","--run",str(run),"--name","valid"]),2)
        saved=poe.read_json(run/"run.json");receipt=poe.read_json(output/"delivery.json")
        self.assertEqual(saved["phase"],"failed");self.assertFalse(receipt["file_exported"])
        self.assertEqual(xml.read_text(),"prior valid artifact");self.assertEqual(saved["last_operation"]["candidate"],"valid")

    def test_edit_receipt_write_failure_exposes_committed_revision_for_recovery(self):
        first=self.edit({"set":{"name":"before"}});write=poe.write_json
        def fail_receipt(path,value):
            if Path(path)==self.source.with_suffix(".edit.json"):raise OSError("receipt disk failure")
            write(path,value)
        with patch.object(poe,"write_json",side_effect=fail_receipt),self.assertRaises(OSError):self.edit({"set":{"name":"after"}},first["revision"])
        view=poe.view_json(args(file=str(self.source),pointer="",fields="name",keys=False,query="",limit=10,offset=0))
        self.assertEqual(view["value"]["name"],"after");self.assertNotEqual(view["revision"],first["revision"])
        self.edit({"set":{"name":"recovered"}},view["revision"])
        self.assertEqual(poe.read_json(self.source)["name"],"recovered")

    def test_large_edit_reply_points_to_saved_full_ids_instead_of_tool_truncation(self):
        changes={"set":{"stages":[{"stage_id":f"stage-{i}","skill_groups":[{"gems":[{"skill_id":"Future","level":1}]}]} for i in range(200)]}}
        poe.write_json(self.request,changes);stdout=io.StringIO()
        with redirect_stdout(stdout):code=poe.main(["edit","--invoked","--kind","route","--file",str(self.source),"--changes",str(self.request)])
        self.assertEqual(code,0);reply=json.loads(stdout.getvalue());self.assertLess(len(stdout.getvalue()),12000)
        saved=poe.read_json(reply["receipt_file"]);self.assertEqual(len(saved["stage_instances"]),200)
        self.assertEqual(reply["next_actions"][1]["pointer"],"/stage_instances")


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
        cls.native = engine.calculate(cls.pob, catalog="bases", query="id:Iron Gauntlets", limit=1)
        cls.intent = intent_for(cls.original)
        path = Path(cls.temp.name) / "intent.json"
        poe.write_json(path, cls.intent)
        poe.set_intent(args(run=str(cls.run_dir), file=str(path)))
        cls.refine = {"schema_version":2,"name": "离线局部优化", "mode": "refine", "summary": "关闭样本中额外保留的技能组并更换辅助。",
                      "operations": {"groups": [{"index": i, "enabled": False, **({"slot":""} if not bx.groups(cls.original)[i-1].get("source") else {})} for i in (3, 4, 6, 7, 8, 9, 10)],
                                     "gems": [{"group": 1, "index": 2, "skill_id": "SupportBrutality", "level": 19, "quality": 0},
                                              {"group": 1, "index": 4, "skill_id": "SupportManaLeech", "level": 20, "quality": 0},
                                              {"group": 2, "index": 3, "enable_global1": False}]},
                      "shopping": [],
                      "assumptions": ["仅离线行为验证，不作为正式配装推荐。"],
                      "migration_steps": ["先准备宝石，再调整开启的技能。"]}
        cls.rebuild = {"schema_version":2,"name": "离线完整换技能组", "mode": "rebuild", "summary": "以盾牌碾压完整替换原技能方案。",
                       "operations": {"replace_skills": [
                           {"slot": "Gloves", "include_in_full_dps": True, "gems": [
                               {"skill_id": "ShieldCrush", "level": 20}, {"skill_id": "SupportBrutality", "level": 19},
                               {"skill_id": "SupportLifetap", "level": 20}, {"skill_id": "SupportMeleePhysicalDamage", "level": 20}]},
                           {"slot": "Weapon 2", "gems": [{"skill_id": "Determination", "level": 19}]}], "main_group": 1},
                       "shopping": [],
                       "assumptions": ["仅离线行为验证，不作为正式配装推荐。"],
                       "migration_steps": ["先准备全套技能宝石。", "核对盾牌与属性需求后一次切换技能。"]}
        for plan in (cls.refine, cls.rebuild):
            plan["operations"]["equip"] = [{"slot": slot.get("name"), "item_id": int(slot.get("itemId")), "active": False}
                                             for slot in bx.active_set(cls.original, "Items", "ItemSet", "activeItemSet").findall("Slot") if slot.get("name", "").startswith("Flask ")]


    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def validate_route(self,route,gems,data_version=None,classes=None,compatibility=None,nodes=None,**kwargs):
        evidence={"user":{"kind":"user_statement","status":"user_declared","subject":"intent"}}
        return routes.validate(route,gems,data_version or self.native["data_version"],classes or self.native["classes"],compatibility,nodes,
                               bases={b["name"]:b for b in self.native["catalog"]},slots=self.native["slots"],evidence=evidence)

    def evaluate_plan(self, plan, name):
        path = Path(self.temp.name) / (name + ".json")
        poe.write_json(path, plan)
        return poe.evaluate(args(run=str(self.run_dir), plan=str(path), name=name))

    def test_01_real_local_refine_export_and_source_preservation(self):
        result = self.evaluate_plan(self.refine, "refine")
        self.assertEqual(result["status"], "draft", result["errors"])
        self.assertTrue(result["roundtrip_verified"])
        self.assertTrue(any("negative" in warning for warning in result["baseline_warnings"]))
        self.assertNotEqual(result["comparison"]["CombinedDPS"]["delta"], 0)
        self.assertIsNone(result["comparison"]["CombinedDPS"]["percent"])
        output = poe.export(args(run=str(self.run_dir), name="refine"))
        for path in output["files"]: self.assertTrue(Path(path).is_file())
        final=poe.read_json(self.run_dir/"output/refine/final-assessment.json")
        self.assertTrue(final["comparison_notes"])
        self.assertIsNone(final["comparison"]["CombinedDPS"]["percent"])
        self.assertEqual(output["publication"]["statements"][1]["status"],"unknown")
        evidence = poe.read_json(self.run_dir / "candidates/refine/evaluation.json")
        granted = [gem for gem in evidence["result"]["gems"] if gem.get("source")]
        self.assertTrue(granted)
        self.assertTrue(all(not gem["group_enabled"] for gem in granted), granted)
        saved = bx.read_document(Path(output["files"][0]))
        self.assertTrue(all(group.get("enabled") == "false" and group.get("slot")
                            for group in bx.groups(saved) if group.get("source")))
        drifted = copy.deepcopy(evidence["result"])
        next(gem for gem in drifted["gems"] if gem.get("source"))["group_enabled"] = True
        errors, _, _ = poe.mechanical_issues(saved, drifted, poe.read_json(self.run_dir / "run.json")["intent"])
        self.assertTrue(any("授予技能状态" in error for error in errors), errors)
        report = Path(output["files"][1]).read_text(encoding="utf-8")
        self.assertTrue(evidence["actual_changes"])
        self.assertIn("实际改动", report)
        self.assertEqual(evidence["validation"]["user_constraints"], "passed")
        with patch.object(facts, "version_fact", return_value={"status": "unverified", "reason": "offline"}):
            draft = poe.export(args(run=str(self.run_dir), name="refine"))
        self.assertEqual(draft["status"], "draft")
        self.assertIn("待核实草案", draft["facts_summary"])
        self.assertEqual(bx.digest(self.sample.read_bytes()), self.original_hash)
        self.assertEqual(bx.digest((self.pob / "Settings.xml").read_bytes()), self.settings_hash)
        self.assertEqual(bx.digest((self.pob / "Data/ModCache.lua").read_bytes()), self.cache_hash)

    def test_02_complete_rebuild_and_template_route(self):
        result = self.evaluate_plan(self.rebuild, "rebuild")
        self.assertEqual(result["status"], "draft", result["errors"])
        recorded = poe.read_json(self.run_dir / "candidates/rebuild/evaluation.json")
        self.assertEqual(recorded["result"]["main_skill"], "ShieldCrush")
        template = copy.deepcopy(self.rebuild)
        template["template_xml"] = str(self.run_dir / "candidates/rebuild/candidate.xml")
        template["operations"] = {"main_group": 1}
        template["sources"] = [{"title": "本地离线测试模板", "url": "https://github.com/PathOfBuildingCommunity/PathOfBuilding"}]
        result = self.evaluate_plan(template, "template")
        self.assertEqual(result["status"], "draft", result["errors"])

    def test_03_invalid_gem_is_not_silently_accepted(self):
        plan = copy.deepcopy(self.refine)
        plan["operations"]["gems"][0]["skill_id"] = "InventedPowerGem"
        result = self.evaluate_plan(plan, "bad-gem")
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(poe.export(args(run=str(self.run_dir), name="bad-gem"))["file_exported"])

    def test_04_dropped_tree_node_and_locked_item_are_rejected(self):
        plan = copy.deepcopy(self.refine)
        nodes = [int(n) for n in bx.active_spec(self.original).get("nodes").split(",")]
        plan["operations"]["tree"] = {"nodes": nodes + [99999999]}
        plan["operations"]["equip"] = [{"slot": "Weapon 2", "item_id": 10}]
        result = self.evaluate_plan(plan, "bad-tree")
        self.assertEqual(result["status"], "blocked")
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
        self.assertEqual(result["status"], "draft", result["errors"])
        path = self.run_dir / "candidates/tamper/candidate.xml"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "修改"): poe.export(args(run=str(self.run_dir), name="tamper"))

    def test_06_new_intent_invalidates_existing_candidate(self):
        result = self.evaluate_plan(self.refine, "old-intent")
        self.assertEqual(result["status"], "draft", result["errors"])
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
        self.assertTrue(any("主技能口径" in note for note in record["comparison_notes"]))
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
        self.assertEqual(outcome["status"], "draft", outcome["errors"])
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
        plan["claims"] = []
        outcome = self.evaluate_plan(plan, "invalid-config-label")
        self.assertTrue(any("无效配置值" in error for error in outcome["errors"]), outcome["errors"])
        plan["operations"]["config"]["enemyIsBoss"] = "Boss"
        outcome = self.evaluate_plan(plan, "valid-config-value")
        self.assertEqual(outcome["status"], "draft", outcome["errors"])
        recorded = poe.read_json(self.run_dir / "candidates/valid-config-value/evaluation.json")
        self.assertEqual(recorded["result"]["config_values"]["enemyLevel"], self.intent["scenario"]["enemyLevel"])
        self.assertEqual(recorded["result"]["config_values"]["enemyIsBoss"], "Boss")
        self.assertEqual(recorded["result"]["config_labels"]["enemyIsBoss"], "Standard Boss")
        output = poe.export(args(run=str(self.run_dir), name="valid-config-value"))
        self.assertIn("敌人类型：Standard Boss（Boss）；敌人等级参数：83", output["facts_summary"])
        report = Path(output["files"][1]).read_text(encoding="utf-8")
        self.assertIn("敌人类型：Standard Boss（Boss）；敌人等级参数：83", report)
        self.assertIn(recorded["xml_sha256"], report)
        self.assertIn(recorded["engine_fingerprint"], report)

    def test_31_granted_passives_distinguish_existing_and_new_tree_nodes(self):
        original = engine.calculate(self.pob, bx.serialise(self.original), catalog="nodes", query="life", limit=200)
        notables = [node for node in original["catalog"] if node["type"] == "Notable" and not node["ascendancy_name"]]
        allocated = next(node for node in notables if node["allocated"])
        unallocated = next(node for node in notables if not node["allocated"])
        amulet_id = next(item["id"] for item in original["equipped"] if item["slot"] == "Amulet")
        documents = []
        for node in (allocated, unallocated):
            document = copy.deepcopy(self.original)
            item = document.find(f"Items/Item[@id='{amulet_id}']")
            self.assertNotIn("Allocates ", item.text)
            item.text += "\nAllocates " + node["name"] + "\n"
            documents.append(document)
        results = engine.calculate(self.pob, xml_batch=[bx.serialise(doc) for doc in documents])["calculations"]
        for result, node, on_tree in zip(results, (allocated, unallocated), (True, False)):
            evidence = next(entry for entry in result["granted_passives"] if entry["id"] == node["id"])
            self.assertEqual(evidence["allocated_on_tree"], on_tree)
            self.assertEqual(evidence["name"], node["name"])
            self.assertEqual(result["points"], original["points"])
        self.assertEqual(results[0]["stats"], original["stats"])
        intent = poe.read_json(self.run_dir / "run.json")["intent"]
        _, warnings, _ = poe.mechanical_issues(documents[0], results[0], intent)
        self.assertTrue(any(str(allocated["id"]) in warning and "不能算两份" in warning for warning in warnings))
        _, warnings, _ = poe.mechanical_issues(documents[1], results[1], intent)
        self.assertFalse(any("不能算两份" in warning for warning in warnings))

    def test_32_native_anoint_recipes_keep_quantities_and_local_translations(self):
        import re
        result = engine.calculate(self.pob, queries=[{"kind": "nodes", "query": "id:" + str(node), "limit": 1}
                                                  for node in (55485, 2225, 27604)])
        native_tree = (self.pob / "TreeData" / result["tree_version"] / "tree.lua").read_text(encoding="utf-8-sig")
        for row in result["catalog_batch"][:2]:
            node = row["entries"][0]
            stanza = re.search(r"\[" + str(node["id"]) + r"\]\s*=\s*\{.*?\[\"recipe\"\]\s*=\s*\{([^}]+)\}", native_tree, re.S)
            self.assertIsNotNone(stanza)
            self.assertEqual([part["id"] for part in node["anoint_recipe"]], re.findall(r'"([^\"]+)"', stanza[1]))
            self.assertTrue(all(part.get("name_cn") and part.get("display_name") for part in node["anoint_recipe"]))
        oils = result["catalog_batch"][1]["entries"][0]["anoint_recipe"]
        self.assertGreater(len(oils), len({part["id"] for part in oils}))
        self.assertNotIn("anoint_recipe", result["catalog_batch"][2]["entries"][0])

    def test_09_resource_shortfall_stays_visible_without_forcing_one_play_style(self):
        plan=copy.deepcopy(self.refine)
        plan["operations"]["gems"]=[{"group":1,"index":2,"skill_id":"SupportBrutality","level":20}]
        outcome=self.evaluate_plan(plan,"resource-check")
        mana=outcome["readiness"]["resources"][0]
        self.assertGreater(mana["net_cost"],0)
        self.assertIn("resource:Mana:usage",outcome["pending"])
        record=poe.read_json(self.run_dir/"candidates/resource-check/evaluation.json")
        main=next(s for s in record["result"]["active_skills"] if s["is_main"])
        plan["usage"]=[{"skill_ref":main["skill_ref"],"continuous":True,"claim_id":"missing"}]
        continuous=self.evaluate_plan(plan,"resource-continuous")
        self.assertEqual(continuous["status"],"blocked")
        self.assertFalse(poe.export(args(run=str(self.run_dir),name="resource-continuous"))["file_exported"])

    def test_10_native_329_mismatched_colours_are_legal_and_quality_changes(self):
        plan = copy.deepcopy(self.refine)
        item_set = bx.active_set(self.original, "Items", "ItemSet", "activeItemSet")
        item_id = int(next(slot for slot in item_set.findall("Slot") if slot.get("name") == "Boots").get("itemId"))
        item = next(item for item in self.original.findall("Items/Item") if int(item.get("id")) == item_id)
        import re
        text = re.sub(r"^Sockets:.*$", "Sockets: W-W-W-W", bx.text_item(item), flags=re.M)
        plan["operations"]["items"] = [{"id": item_id, "text": text}]
        outcome = self.evaluate_plan(plan, "white-sockets")
        self.assertEqual(outcome["status"], "draft", outcome["errors"])
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
        self.assertEqual(outcome["status"], "draft", outcome["errors"])
        saved = poe.read_json(self.run_dir / "candidates/ascendancy-route/evaluation.json")
        self.assertEqual(saved["result"]["ascendancy_points"], 8)

    def test_12_native_batch_catalog_validates_starter_gem_levels(self):
        result = engine.calculate(self.pob, queries=[{"kind": "gems", "query": "id:ShieldCrush", "limit": 2},
                                                   {"kind": "gems", "query": "id:Sunder", "limit": 2},
                                                   {"kind": "gems", "query": "id:Determination", "limit": 2},
                                                   {"kind": "gems", "query": "id:Grace", "limit": 2},
                                                   {"kind": "gems", "query": "id:Discipline", "limit": 2},
                                                   {"kind": "gems", "query": "id:Hatred", "limit": 2},
                                                   {"kind": "gems", "query": "id:Clarity", "limit": 2},
                                                   {"kind": "gems", "query": "id:SupportMinionDamage", "limit": 2}])
        self.assertEqual(len(result["catalog_batch"]), 8)
        gems = {entry["skill_id"]: entry for query in result["catalog_batch"] for entry in query["entries"]}
        self.assertEqual(set(gems), {"ShieldCrush", "Sunder", "Determination", "Grace", "Discipline", "Hatred", "Clarity", "SupportMinionDamage"})
        self.assertEqual(gems["ShieldCrush"]["min_level"], 1)
        self.assertEqual(gems["Sunder"]["min_level"], 12)
        for skill_id, amount in (("Discipline", 35), ("Hatred", 50)):
            for row in gems[skill_id]["level_requirements"]:
                self.assertEqual(row["base_resources"]["manaReservationPercent"], amount)
        clarity = gems["Clarity"]["level_requirements"]
        self.assertLess(clarity[0]["base_resources"]["manaReservationFlat"], clarity[-1]["base_resources"]["manaReservationFlat"])
        self.assertGreater(gems["ShieldCrush"]["level_requirements"][0]["base_resources"]["cost"]["Mana"], 0)
        self.assertGreater(gems["SupportMinionDamage"]["level_requirements"][0]["base_resources"]["manaMultiplier"], 0)
        for skill_id, attribute in (("Determination", "strength"), ("Grace", "dexterity"), ("Discipline", "intelligence")):
            level_20 = next(row for row in gems[skill_id]["level_requirements"] if row["gem_level"] == 20)
            self.assertEqual(level_20["character_level"], 70)
            self.assertEqual(level_20[attribute], 155)
        route = route_fixture()
        checked, _ = self.validate_route(route, gems)
        self.assertEqual(checked["stages"][0]["skill_groups"][0]["gems"][0]["name"], gems["ShieldCrush"]["display_name"])
        route["stages"][-1]["skill_groups"][0]["gems"][0]["level"] = "auto"
        checked, _ = self.validate_route(route, gems)
        actual = checked["stages"][-1]["skill_groups"][0]["gems"][0]["level"]
        self.assertEqual(actual, max(row["gem_level"] for row in gems["ShieldCrush"]["level_requirements"] if row["character_level"] <= 68 and row.get("naturally_trainable")))
        route["gems"]["Sunder"] = copy.deepcopy(route["gems"]["ShieldCrush"])
        route["stages"][0]["skill_groups"][0]["gems"][0]["skill_id"] = "Sunder"
        with self.assertRaisesRegex(ValueError, "使用等级"):
            self.validate_route(route, gems)

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

    def test_30_native_node_paths_require_explicit_character_context(self):
        generic = engine.calculate(self.pob, catalog="nodes", query="Enduring Bond", limit=5)
        self.assertEqual(generic["tree_context"], {"basis": "unspecified"})
        target = next(node for node in generic["catalog"] if node["type"] == "Notable")
        for key in ("path", "allocated", "reachable", "points_to_allocate"):
            self.assertNotIn(key, target)
        classes = {entry["name"]: entry["id"] for entry in generic["classes"]}
        paths = []
        for name in ("Witch", "Marauder"):
            result = engine.calculate(self.pob, catalog="nodes", query="id:" + str(target["id"]), limit=1, class_id=classes[name])
            context = result["tree_context"]
            self.assertEqual(context["basis"], "class_start")
            self.assertEqual(context["class_name"], name)
            self.assertEqual(context["class_id"], classes[name])
            self.assertEqual(result["nodes"], [context["start_node_id"]])
            node = result["catalog"][0]
            self.assertFalse(node["allocated"])
            self.assertTrue(node["reachable"])
            self.assertEqual(node["points_to_allocate"], len(node["path"]))
            paths.append(node["path"])
        self.assertNotEqual(paths[0], paths[1])
        with self.assertRaisesRegex(ValueError, "Unknown class ID"):
            engine.calculate(self.pob, catalog="nodes", query="Enduring Bond", class_id=max(classes.values()) + 100)
        with self.assertRaisesRegex(ValueError, "不能覆盖"):
            engine.calculate(self.pob, bx.serialise(self.original), class_id=classes["Witch"])
        current = engine.calculate(self.pob, bx.serialise(self.original), catalog="nodes", query="Enduring Bond", limit=5)
        self.assertEqual(current["tree_context"]["basis"], "input_xml")
        self.assertEqual(current["tree_context"]["class_name"], "Duelist")
        self.assertIn("path", current["catalog"][0])

    def test_28_same_version_engine_change_refreshes_baseline_and_blocks_old_export(self):
        self.evaluate_plan(self.refine, "fingerprint-before")
        path = self.run_dir / "baseline.json"
        baseline = poe.read_json(path)
        baseline["engine_fingerprint"] = "different-content-same-version"
        baseline["stats"]["CombinedDPS"] = -12345
        poe.write_json(path, baseline)
        result = self.evaluate_plan(self.refine, "fingerprint-after")
        self.assertEqual(result["status"], "draft", result["errors"])
        refreshed = poe.read_json(path)
        current = engine.check(self.pob)
        self.assertEqual(refreshed["engine_fingerprint"], current["engine_fingerprint"])
        self.assertGreater(refreshed["stats"]["CombinedDPS"], 0)
        current["engine_fingerprint"] = "same-version-but-updated-files"
        with patch.object(engine, "check", return_value=current):
            with self.assertRaisesRegex(ValueError, "适配器变化"):
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
        engine.validate_contract(restored)
        self.assertIn("capability:excluded_guard_validation", poe.mechanical_issues(saved,restored,self.intent)[2])
        plan["operations"]["replace_skills"][-1]["gems"][1]["level"] = 20
        incompatible = self.evaluate_plan(plan, "guard-trigger-level-invalid")
        self.assertTrue(any("触发辅助原生上限" in error for error in incompatible["errors"]), incompatible["errors"])

    def test_13_native_multiple_actives_and_unused_supports_are_diagnostics(self):
        result=engine.calculate(self.pob,queries=[{"kind":"links","query":"Fireball,FreezingPulse,SupportAddedLightningDamage","limit":1},
            {"kind":"links","query":"ShieldCrush,SupportGreaterMultipleProjectiles","limit":1},{"kind":"links","query":"NonexistentFixture","limit":1},
            {"kind":"bases","query":"id:Stone Axe","limit":1},{"kind":"bases","query":"id:Fishing Rod","limit":1},{"kind":"classes","query":"Templar","limit":1}])
        multi,unused,unknown,axe,rod,classes=[q["entries"][0] for q in result["catalog_batch"]]
        self.assertTrue(multi["valid"]);self.assertEqual([r["active_index"] for r in multi["relations"] if r["applied"]],[1,2])
        self.assertTrue(unused["valid"]);self.assertEqual(unused["incompatible"],["SupportGreaterMultipleProjectiles"])
        self.assertFalse(unknown["valid"]);self.assertTrue(unknown["errors"])
        self.assertEqual((axe["sockets"],rod["sockets"]),(6,4));self.assertIn("Weapon 1",axe["valid_slots"])
        self.assertEqual(classes["name_cn"],"圣堂武僧")
        for key,invalid in (("points",None),("ascendancy_points",True),("secondary_ascendancy_points",-1)):
            with self.assertRaisesRegex(ValueError,"已分配点数"):engine.validate_contract({**result,key:invalid})
        plan=copy.deepcopy(self.rebuild);plan["operations"]["replace_skills"][0]["gems"][1]["skill_id"]="SupportGreaterMultipleProjectiles"
        outcome=self.evaluate_plan(plan,"unused-support")
        self.assertFalse(any("未辅助" in e for e in outcome["errors"]));self.assertTrue(any("辅助" in w for w in outcome["warnings"]))

    def test_14_gem_quality_has_no_copied_global_ceiling(self):
        plan=copy.deepcopy(self.refine);plan["operations"]["gems"].append({"group":1,"index":3,"level":1,"quality":50})
        outcome=self.evaluate_plan(plan,"quality-evidence")
        record=poe.read_json(self.run_dir/"candidates/quality-evidence/evaluation.json")
        gem=next(g for g in record["result"]["gems"] if g["group"]==1 and g["index"]==3)
        self.assertEqual(gem["quality"],50)
        self.assertTrue(any("material:gem:" in p for p in outcome["pending"]))
        self.assertFalse(any("品质上限" in e for e in outcome["errors"]))

    def test_15_native_rare_recipe_and_invalid_modifier_constraints(self):
        recipe = {"base": "Two-Toned Boots (Armour/Energy Shield)", "ilvl": 86, "sockets": "W-W-W-W",
                  "affixes": [{"id": "IncreasedLife8"}, {"id": "MovementVelocity5"}, {"id": "ChaosResist6"}]}
        item_set = bx.active_set(self.original, "Items", "ItemSet", "activeItemSet")
        item_id = int(next(slot for slot in item_set.findall("Slot") if slot.get("name") == "Boots").get("itemId"))
        plan = copy.deepcopy(self.refine)
        plan["operations"]["items"] = [{"id": item_id, "craft": recipe}]
        outcome = self.evaluate_plan(plan, "crafted-boots")
        self.assertEqual(outcome["status"], "draft", outcome["errors"])
        recorded = poe.read_json(self.run_dir / "candidates/crafted-boots/evaluation.json")
        raw = recorded["crafted_items"][0]["text"]
        self.assertIn("+122 to maximum Life", raw)
        self.assertIn("+33% to Chaos Resistance", raw)
        self.assertIn("+10% to Fire and Lightning Resistances", raw)
        self.assertIn("ArmourBasePercentile: 0.5", raw)
        output = poe.export(args(run=str(self.run_dir), name="crafted-boots"))
        self.assertIn("crafted_items", Path(output["files"][1]).read_text(encoding="utf-8"))
        bad = copy.deepcopy(recipe)
        bad["affixes"].append({"id": "IncreasedLife7"})
        with self.assertRaisesRegex(ValueError, "Conflicting modifier group"):
            poe.materialize_items({"operations": {"items": [{"id": 20, "craft": bad}]}}, self.pob)
        bad = copy.deepcopy(recipe)
        bad["ilvl"] = 70
        with self.assertRaisesRegex(engine.EngineError, "level is too low") as failure:
            poe.materialize_items({"operations": {"items": [{"id": 20, "craft": bad}]}}, self.pob)
        diagnostic=failure.exception.diagnostic
        self.assertEqual(diagnostic["code"],"native_invalid_input")
        self.assertEqual(diagnostic["action"],"edit_input")
        self.assertTrue(diagnostic["location"].startswith("/operations/items/0/craft/affixes/"))
        self.assertGreater(diagnostic["expected"]["minimum_item_level"],bad["ilvl"])
        self.assertEqual(diagnostic["actual"]["item_level"],bad["ilvl"])
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

    def test_40_empty_recipe_and_empty_group_roundtrip_follow_native_format(self):
        plan={'mode':'refine','summary':'native interface verification only','operations':{
            'items':[{'id':201,'craft':{'base':'Iron Greaves','ilvl':1,'sockets':'W','affixes':[]}}],
            'equip':[{'slot':'Boots','item_id':201}],
            'replace_skills':[{'slot':'Boots','gems':[{'skill_id':'ShieldCrush','level':1}]},{'slot':'','enabled':False,'gems':[]}],
            'main_group':1}}
        resolved,audit=poe.materialize_items(plan,self.pob)
        self.assertEqual(audit[0]['rarity'],'NORMAL')
        self.assertTrue(audit[0]['text'].startswith('Rarity: NORMAL\nIron Greaves\n'))
        self.assertEqual(audit[0]['affixes'],[])
        self.assertNotIn('Prefix: None',audit[0]['text'])
        document=bx.apply_plan(self.original,resolved,native=self.native)
        calculated=engine.calculate(self.pob,bx.serialise(document))
        saved=poe.roundtrip(self.pob,calculated,document)
        self.assertFalse(bx.groups(bx.parse(saved))[1].findall('Gem'))

    def test_16_missing_equipment_is_rejected_even_without_socketed_skills(self):
        plan = copy.deepcopy(self.refine)
        plan["operations"]["items"] = [{"id": 100, "text": "Rarity: RARE\nInvented Belt\nNonexistent Test Base\nImplicits: 0\n+100 to maximum Life"}]
        plan["operations"]["equip"].append({"slot": "Belt", "item_id": 100})
        outcome = self.evaluate_plan(plan, "missing-belt")
        self.assertEqual(outcome["status"], "blocked")
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
        self.assertTrue(any("未辅助" in error for error in rows[2]["warnings"]), rows[2]["warnings"])
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
        self.assertEqual(outcome["status"], "draft", outcome["errors"])
        self.assertTrue(any(row["resource"] == "能量护盾" for row in outcome["readiness"]["resources"]))
        plan = copy.deepcopy(self.rebuild)
        plan["operations"]["replace_skills"].append({"slot": "Helmet", "gems": [{"skill_id": "RighteousFire", "level": 19, "enable_global1": True}]})
        outcome = self.evaluate_plan(plan, "self-degen")
        result = poe.read_json(self.run_dir / "candidates/self-degen/evaluation.json")["result"]
        self.assertGreater(result["stats"].get("TotalBuildDegen", 0), 0)
        self.assertLess(result["stats"]["NetLifeRegen"], 0)
        self.assertEqual(outcome["status"], "draft")
        self.assertLess(outcome["readiness"]["resources"][1]["regeneration"], 0)
        self.assertIn("resource:Life:usage", outcome["pending"])

    def test_19_native_route_counts_connections_and_records_respec_dependency(self):
        result=engine.calculate(self.pob,queries=[{"kind":"nodes","query":"Inquisitor","limit":100},{"kind":"gems","query":"id:ShieldCrush","limit":1}])
        nodes=result["catalog_batch"][0]["entries"];gems={g["skill_id"]:g for g in result["catalog_batch"][1]["entries"]}
        route=route_fixture();route.update(class_id=5,ascendancy="Inquisitor")
        stage=route["stages"][-1];stage.update(ascendancy_points=8,ascendancy_nodes=[39790,32816,53884,48214])
        checked,_=self.validate_route(route,gems,nodes=nodes);self.assertEqual(checked["stages"][-1]["ascendancy_used"],8)
        stage["ascendancy_nodes"].append(40059)
        with self.assertRaisesRegex(ValueError,"超过"):self.validate_route(route,gems,nodes=nodes)
        stage["ascendancy_nodes"].remove(40059);route["stages"][1].update(ascendancy_points=2,ascendancy_nodes=[40059])
        checked,_=self.validate_route(route,gems,nodes=nodes);self.assertIn("respec:stage-2",checked["pending"])
        self.assertIn("progress:stage-2",checked["pending"])

    def test_20_native_general_cry_context_and_automatic_costs(self):
        plan = copy.deepcopy(self.rebuild)
        plan["operations"]["replace_skills"][0] = {"slot": "Gloves", "include_in_full_dps": True, "gems": [
            {"skill_id": "ShieldCrush", "level": 19}, {"skill_id": "GeneralsCry", "level": 19},
            {"skill_id": "Autoexertion", "level": 19}, {"skill_id": "SupportBrutality", "level": 19}]}
        outcome = self.evaluate_plan(plan, "gc-no-corpses")
        self.assertEqual(outcome["status"], "draft")
        self.assertTrue(any(p.startswith("dependency:") for p in outcome["pending"]))
        plan["operations"]["replace_skills"].append({"slot": "Helmet", "gems": [{"skill_id": "Desecrate", "level": 1}]})
        plan["claims"] = []
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
        self.assertTrue(main["no_cost"])
        self.assertEqual(mana["main_cost_scope"],"not_applicable_by_native_flag")
        self.assertAlmostEqual(mana["cost_per_second"],cry["costs"]["ManaPerSecondCost"])
        self.assertTrue(any(p.startswith("usage:") for p in outcome["pending"]))
        self.assertTrue(result["damage_sources"])
        self.assertEqual(outcome["status"], "draft", outcome["errors"])
        exported = poe.export(args(run=str(self.run_dir), name="gc-costs"))
        report = Path(exported["files"][1]).read_text(encoding="utf-8")
        self.assertIn("原生 Full DPS 分项", report)
        self.assertIn("automatic_cost_per_second", report)
        self.assertIn("19/0", report)
        self.assertIn("claims", exported["reply_policy"])
        self.assertTrue(any(s["kind"]=="damage_subject" and s["actor"]=="mirage" for s in exported["publication"]["statements"]))
        for resource in outcome["readiness"]["resources"]:
            self.assertEqual(resource["leech_on_hit"], 0)

    def test_21_minion_damage_is_separate_and_probe_uses_minion_output(self):
        plan = copy.deepcopy(self.rebuild)
        plan["operations"]["replace_skills"] = [{"slot": "Gloves", "include_in_full_dps": True, "gems": [
            {"skill_id": "SummonRagingSpirit", "level": 12}, {"skill_id": "SupportMinionDamage", "level": 19},
            {"skill_id": "SupportLifetap", "level": 19}]}]
        outcome = self.evaluate_plan(plan, "minion-context")
        self.assertEqual(outcome["status"], "draft", outcome["errors"])
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
        observed=next(m for m in exported["publication"]["measurements"] if m["metric"]=="MinionCombinedDPS")
        self.assertEqual(observed["meaning"]["actor"],"minion")
        self.assertGreater(observed["value"],0)
        self.assertNotIn("主技能综合 DPS 0.0", exported["facts_summary"])
        self.assertNotIn("角色技能命中率", exported["facts_summary"])

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
        self.assertTrue(response["summary_complete"])
        self.assertEqual(response["entry_fields"]["gems"],sorted({key for row in data["queries"][0]["entries"] for key in row}))
        for row in response["queries"]:
            self.assertEqual(len(facts.pointer(data,row["entries_pointer"])),row["returned"])
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
        self.assertIn(f"普通天赋 {saved['calculation']['points']} 点，升华 {saved['calculation']['ascendancy_points']} 点", result["diagnosis_summary"])
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
        self.assertEqual(outcome["status"], "draft", outcome["errors"])
        result = poe.read_json(self.run_dir / "candidates/mastery-delta/evaluation.json")["result"]
        self.assertTrue(set(masteries) <= set(result["nodes"]))

    def test_24_catalog_failure_explains_character_free_queries(self):
        invalid = '<PathOfBuilding><Build className="Marauder" ascendClassName="Chieftain" level="1" targetVersion="3_0"/><Tree><Spec treeVersion="3_29" ascendancyName="Chieftain"><URL>https://www.pathofexile.com/passive-skill-tree/AAAABgAA</URL></Spec></Tree><Skills/><Items/></PathOfBuilding>'
        with self.assertRaises(engine.EngineError) as failure:
            engine.calculate(self.pob, invalid, catalog="nodes", query="Chieftain")
        self.assertIn("去掉 --input", str(failure.exception))
        self.assertNotIn("Press Enter/Escape", str(failure.exception))
        self.assertNotIn("Press Enter/Escape", failure.exception.diagnostic["message"])
        recovered = engine.calculate(self.pob, catalog="nodes", query="Chieftain", limit=40)
        nodes = recovered["catalog"]
        self.assertTrue(any(node["is_ascendancy_start"] for node in nodes))
        self.assertTrue(any(node["name"] == "Hinekora, Death's Fury" and node["type"] == "Notable" for node in nodes))
        # The text search also legitimately matches Ascendant's Chieftain node.
        # Route allocation selects the actual ascendancy by ascendancy_name.
        self.assertTrue(any(node["name"] == "Chieftain" and node["ascendancy_name"] == "Ascendant" for node in nodes))
        self.assertTrue(any(node["is_ascendancy_start"] and node["ascendancy_name"] == "Chieftain" for node in nodes))

    def test_33_calcs_breakdown_is_scoped_bound_and_does_not_change_xml(self):
        xml=bx.serialise(self.original)
        baseline=engine.calculate(self.pob,xml)
        minion=next(s for s in baseline["active_skills"] if s["minion"])
        result=engine.calculate(self.pob,xml,breakdowns=[{"metric":"Life"},{"metric":"UnavailableMetric"},{"metric":"Life","skill_ref":minion["skill_ref"],"actor":"minion"}])
        life,missing,summon=result["breakdowns"]
        self.assertEqual(life["status"],"observed");self.assertEqual(life["value"],baseline["stats"]["Life"])
        self.assertTrue(life["matches_main_value"]);self.assertEqual(life["context"]["xml_sha256"],bx.digest(xml))
        self.assertEqual(missing["status"],"unsupported");self.assertNotIn("value",missing)
        self.assertEqual(summon["selected_skill_id"],minion["id"]);self.assertEqual(summon["actor"],"minion")
        self.assertGreater(summon["value"],0)
        if "detail" not in summon: self.assertEqual(summon["status"],"unsupported");self.assertIn("No native breakdown",summon["reason"])
        self.assertEqual(poe.semantics(result),poe.semantics(baseline));self.assertEqual(result["stats"],baseline["stats"])
        before,after=bx.summary(bx.parse(baseline["xml"])),bx.summary(bx.parse(result["xml"]))
        for value in (before,after):
            for key in ("slots","jewels","items","config"): value[key]=sorted(value[key],key=lambda row:json.dumps(row,sort_keys=True))
        self.assertEqual(before,after)

    def test_34_cache_uses_contents_and_final_export_forces_fresh_worker(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(engine,"CACHE_DIRECTORY",Path(temp)):
            first=engine.calculate(self.pob,catalog="classes")
            second=engine.calculate(self.pob,catalog="classes")
            self.assertFalse(first["runtime"]["cache_hit"]);self.assertTrue(second["runtime"]["cache_hit"])
            cache=next(Path(temp).glob("*.json"));data=poe.read_json(cache);data["result"]["classes"]=[];poe.write_json(cache,data)
            third=engine.calculate(self.pob,catalog="classes")
            self.assertFalse(third["runtime"]["cache_hit"]);self.assertTrue(third["classes"])
            fresh=engine.calculate(self.pob,catalog="classes",cache=False)
            self.assertFalse(fresh["runtime"]["cache_hit"])
            changed=engine.calculate(self.pob,catalog="classes",query="Duelist")
            self.assertNotEqual(changed["runtime"]["cache_key"],first["runtime"]["cache_key"])
        self.evaluate_plan(self.refine,"fresh-export")
        with patch.object(engine,"calculate",wraps=engine.calculate) as calculation:
            poe.export(args(run=str(self.run_dir),name="fresh-export"))
        self.assertFalse(calculation.call_args_list[0].kwargs["cache"])

    def test_35_equipment_tree_and_combined_probe_match_full_calculation(self):
        self.evaluate_plan(self.refine,"combined-probe")
        candidate=self.run_dir/"candidates/combined-probe/candidate.xml"
        original_hash=bx.digest(candidate.read_bytes());document=bx.read_document(candidate)
        native=engine.calculate(self.pob,bx.serialise(document),catalog="nodes",query="life",limit=100)
        node=next(n for n in native["catalog"] if not n["allocated"] and n.get("reachable") and not n.get("ascendancy_name") and n.get("path"))
        item={"id":1001,"text":"Rarity: NORMAL\nLeather Belt"}
        variants=[{"id":"item","operations":{"items":[item],"equip":[{"slot":"Belt","item_id":1001}]}},
                  {"id":"tree","operations":{"tree":{"nodes_add":node["path"]}}}]
        variants.append({"id":"combined","operations":{**variants[0]["operations"],**variants[1]["operations"]}})
        path=Path(self.temp.name)/"variants.json";poe.write_json(path,{"variants":variants})
        result=poe.probe(args(run=str(self.run_dir),name="combined-probe",variants=str(path)))
        self.assertEqual(len(result["alternatives"]),3)
        for variant,row in zip(variants,result["alternatives"]):
            changed=bx.apply_plan(document,{"mode":"refine","summary":"independent comparison","operations":variant["operations"]},native=native)
            measured=engine.calculate(self.pob,bx.serialise(changed),cache=False)
            self.assertEqual(row["xml_sha256"],bx.digest(bx.serialise(changed)))
            self.assertEqual(row["stats"]["Life"],measured["stats"]["Life"])
            self.assertAlmostEqual(row["stats"]["CombinedDPS"],measured["stats"]["CombinedDPS"],places=6)
        self.assertEqual(bx.digest(candidate.read_bytes()),original_hash)
        screen=poe.probe(args(run=str(self.run_dir),name="combined-probe",node_ids=str(node["id"])))
        self.assertEqual(screen["alternatives"][0]["status"],"screened")
        self.assertIn("excludes path cost",screen["alternatives"][0]["scope"])

    def test_36_route_snapshots_bind_native_claims_and_leave_missing_stage_unknown(self):
        route=route_fixture();route.update(class_id=3,ascendancy=None,gems={"Fireball":{}},stages=[])
        text="Rarity: NORMAL\nWool Gloves\nSockets: B"
        held_gem={"kind":"gem","instance_id":"fire","skill_id":"Fireball","level":1,"quality":0,"quantity":1,"acquisition":"owned","evidence_refs":["held-gem"]}
        held_item={"kind":"item","item_id":201,"spec_sha256":bx.digest(bx.normal_item(text)),"quantity":1,"acquisition":"owned","evidence_refs":["held-item"]}
        route["evidence"]=[{"evidence_id":"user","kind":"user_statement","subject":"intent","text":"Offline fixture only"},
            {"evidence_id":"held-gem","kind":"user_statement","subject":facts.material_ref(held_gem),"text":"Fixture: held Fireball 1/0 x1","context":{"quantity":1,"instance_id":"fire"}},
            {"evidence_id":"held-item","kind":"user_statement","subject":"item:201","text":"Fixture: held exact gloves","context":{"quantity":1,"spec_sha256":held_item["spec_sha256"]}}]
        route["claims"]=[{"claim_id":"stage","text":"Conditional snapshot, not a gameplay recommendation","subjects":["stage:a","snapshot:a","goal"],"evidence_refs":["native:stage:a#/stats/Life"]}]
        stage={"stage_id":"a","name":"Fixture phase","level":12,"bases":{"Gloves":"Wool Gloves"},"key_transition":True,
               "skill_groups":[{"name":"Fireball","slot":"Gloves","use":"fixture","gems":[{"instance_id":"fire","skill_id":"Fireball","level":1,"quality":0}]}],
               "required_items":[held_gem,held_item],"snapshot":{"assumptions":["stage"],"operations":{"items":[{"id":201,"text":text}],"equip":[{"slot":"Gloves","item_id":201}],"main_group":1},"metrics":["Life","ManaCost"]}}
        route["stages"]=[stage,{**copy.deepcopy(stage),"stage_id":"b","level":None,"required_items":[]}]
        path=Path(self.temp.name)/"route.json";output=Path(self.temp.name)/"route.html";poe.write_json(path,route)
        with patch.object(facts,"gem_facts",return_value={"Fireball":{"status":"unverified"}}):
            response=poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        checked=poe.read_json(output.with_suffix(".evidence.json"))["checked_route"]
        first,second=checked["stages"]
        self.assertTrue(first["calculation"]["roundtrip_verified"])
        self.assertEqual(first["calculation"]["stats"]["Life"],checked["evidence"]["native:stage:a#/stats/Life"]["value"])
        self.assertEqual(checked["evidence"]["native:stage:a#/stats/Life"]["context"]["xml_sha256"],first["calculation"]["xml_sha256"])
        self.assertTrue(Path(first["calculation"]["xml_path"]).exists())
        self.assertNotIn("stats",second["calculation"]);self.assertEqual(second["calculation"]["status"],"not_calculated")
        self.assertIn("native:snapshot:b",response["pending"])
        self.assertEqual(checked["budget_assessment"]["status"],"user_declared")
        self.assertEqual(checked["budget_assessment"]["materials"][0]["quantity"],1)
        self.assertEqual(first["calculation"]["material_specs"][0]["spec_sha256"],held_item["spec_sha256"])
        stage["entry_checks"]=[{"check_id":"conditional-life","locator":"/stats/Life","op":"ge","value":first["calculation"]["stats"]["Life"]+1,"claim_id":"stage"}]
        route["assessment"]={"goal_claim_ids":["stage"],"scope":"Fixture only"}
        poe.write_json(path,route)
        with patch.object(facts,"gem_facts",return_value={}),patch.object(poe,"roundtrip",side_effect=ValueError("must reuse this verified stage")):
            response=poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        changed=poe.read_json(output.with_suffix(".evidence.json"))["checked_route"]
        self.assertTrue(changed["stages"][0]["calculation"]["roundtrip_verified"])
        self.assertEqual(changed["stages"][0]["calculation"]["entry_checks"][0]["status"],"unmet")
        route["assessment"]["review"]={"token":response["review_token"],"findings":"Fixture condition remains explicitly unmet.","unresolved":[]}
        poe.write_json(path,route)
        with patch.object(facts,"gem_facts",return_value={}):
            response=poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        reviewed=poe.read_json(output.with_suffix(".evidence.json"))["checked_route"]
        self.assertEqual(reviewed["final_review"]["status"],"model_reviewed")
        self.assertTrue(any("entry_check:" in p for p in response["pending"]))
        self.assertTrue(poe.read_json(output.with_suffix(".state.json"))["file_exported"])
        self.assertEqual(poe.workflow_status(args(route_state=str(output.with_suffix('.state.json'))))['completed_stages'],['a'])
        snapshot=Path(first["calculation"]["xml_path"]);snapshot.write_text(snapshot.read_text(encoding="utf-8")+"\n",encoding="utf-8")
        with patch.object(facts,"gem_facts",return_value={}),patch.object(poe,"roundtrip",wraps=poe.roundtrip) as replay:
            poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        self.assertEqual(replay.call_count,1)

    def test_37_unparsed_nonflask_remains_unknown_even_when_native_active_false(self):
        result=engine.calculate(self.pob,bx.serialise(self.original))
        item=next(i for i in result["equipped"] if not i["slot"].startswith("Flask "))
        item.update(active=False,unparsed=["Future unsupported modifier"])
        _,_,pending=poe.mechanical_issues(self.original,result,{})
        self.assertIn("item:"+str(item["id"])+":unparsed",pending)

    def test_38_route_surfaces_stage_errors_and_never_labels_blocked_snapshot_ready(self):
        route=route_fixture();route.update(class_id=3,ascendancy=None,gems={'Fireball':{}})
        route['evidence']=[{'evidence_id':'user','kind':'user_statement','subject':'intent','text':'Offline fixture only'}]
        route['stages']=[{'stage_id':'custom','name':'Diagnostic phase','level':12,'bases':{'Gloves':'Wool Gloves'},
            'skill_groups':[{'name':'Fireball','slot':'Gloves','use':'fixture','gems':[{'instance_id':'one','skill_id':'Fireball','level':1,'quality':0}]}],
            'snapshot':{'operations':{'items':[{'id':201,'text':'Rarity: NORMAL\nUnrecognized fixture base'}],'equip':[{'slot':'Gloves','item_id':201}],'main_group':1},'metrics':['Life']}}]
        path=Path(self.temp.name)/'diagnostic-route.json';output=path.with_suffix('.html');poe.write_json(path,route)
        with patch.object(facts,'gem_facts',return_value={}):response=poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        self.assertEqual(response['status'],'blocked')
        self.assertTrue(response['errors'])
        self.assertEqual(response['stage_summary'][0]['status'],'blocked')
        state=poe.read_json(output.with_suffix('.state.json'))
        self.assertEqual(state['stages']['custom']['status'],'blocked')
        self.assertTrue(state['stages']['custom']['native_calculated'])
        html=output.read_text(encoding='utf8')
        self.assertIn('已知错误',html)
        self.assertIn(response['errors'][0].split('：',1)[-1],html)

    def test_38_native_default_omission_is_equivalent_but_real_changes_fail(self):
        document=copy.deepcopy(self.original)
        bx.set_config(document,{"conditionEnemyRareOrUnique":False})
        result=engine.calculate(self.pob,bx.serialise(document))
        self.assertIs(result["config_defaults"]["conditionEnemyRareOrUnique"],False)
        saved=poe.roundtrip(self.pob,result,document)
        self.assertNotIn("conditionEnemyRareOrUnique",result["config_values"])
        changed=engine.calculate(self.pob,saved)
        changed["config_saved_values"]["conditionEnemyRareOrUnique"]=True
        with patch.object(engine,"calculate",return_value=changed),self.assertRaisesRegex(ValueError,"配置"):
            poe.roundtrip(self.pob,result,document)

    def test_41_current_native_fact_units_and_node_owners_are_not_name_aliases(self):
        result=engine.calculate(self.pob,bx.serialise(self.original))
        for key,unit in (("LifeLeechGainRate","/s"),("LifeLeechGainPerHit","/hit")):
            with self.subTest(key=key):
                self.assertEqual(facts.reference_meaning(result,"/stats/"+key)["unit"],unit)
        row=facts.resolve_reference("native:candidate#/stats/Life",{},{"candidate":result})
        self.assertEqual(row["context"]["xml_sha256"],bx.digest(bx.serialise(self.original)))
        row=facts.resolve_reference("native:candidate#/stats/ManaCancellableReservation",{},{"candidate":result})
        self.assertEqual(row["meaning"]["unit_status"],"unknown")
        native=engine.calculate(self.pob,catalog="nodes",query="Juggernaut",limit=100,class_id=1)
        entries=[e for e in native["catalog"] if e.get("owner_class_id") is not None]
        self.assertGreater(len({e["owner_class_id"] for e in entries}),1)
        for entry in entries:
            self.assertIn(str(entry["id"]),entry["qualified_name"])
            self.assertTrue(entry["owner_class_name"])

    def test_42_export_publication_uses_final_values_and_preserves_unknown_budget(self):
        plan=copy.deepcopy(self.refine)
        plan["claims"]=[{"claim_id":"facts","text":"{{fact:native:candidate#/stats/Life}}","subjects":["goal"],"checks":[{"reference":"native:candidate#/stats/Life","op":"ge","value":0}]}]
        first=self.evaluate_plan(plan,"publication")
        exported=poe.export(args(run=str(self.run_dir),name="publication"))
        final=poe.read_json(self.run_dir/"output/publication/final-assessment.json")
        self.assertEqual(exported["publication"],final["publication"])
        self.assertEqual(exported["publication"]["statements"][1]["status"],"unknown")
        self.assertEqual(final["claims"][0]["checks"][0]["status"],"supported")
        report=(self.run_dir/"output/publication/配装说明.html").read_text(encoding="utf-8")
        for measurement in exported["publication"]["measurements"]:
            self.assertIn(measurement["text"],report)

    def test_39_final_review_and_optional_url_survive_export_and_failed_render_is_not_success(self):
        plan=copy.deepcopy(self.refine)
        source=Path(self.temp.name)/"local-evidence.txt";source.write_text("Offline local record",encoding="utf-8")
        plan["evidence"]=[{"evidence_id":"artifact","kind":"artifact","subject":"fixture","path":source.name,"excerpt":"Offline local record"}]
        plan["claims"]=[{"claim_id":"review","text":"Fixture conclusion; remaining gaps are retained.","subjects":["goal"],"evidence_refs":["artifact","native:candidate#/stats/Life"]}]
        plan["assessment"]={"goal_claim_ids":["review"],"scope":"Fixture, not a player build"}
        first=self.evaluate_plan(plan,"reviewed")
        plan["assessment"]["review"]={"token":first["review_token"],"findings":"Checked current facts, object, conditions and limited conclusion.","unresolved":[]}
        second=self.evaluate_plan(plan,"reviewed")
        self.assertEqual(first["review_token"],second["review_token"])
        self.assertNotIn("final_review:stale",second["pending"])
        result=poe.export(args(run=str(self.run_dir),name="reviewed"))
        final=poe.read_json(self.run_dir/"output/reviewed/final-assessment.json")
        self.assertEqual(final["review_packet"]["status"],"model_reviewed")
        self.assertIsNone(final["evidence"]["artifact"]["url"])
        self.assertTrue(result["file_exported"])
        manifest=poe.read_json(self.run_dir/"output/reviewed/delivery.json")
        for name,digest in manifest["files"].items(): self.assertEqual(bx.digest((self.run_dir/"output/reviewed"/name).read_bytes()),digest)
        with patch.object(poe,"report_html",side_effect=OSError("fixture render failure")),self.assertRaises(OSError):
            poe.export(args(run=str(self.run_dir),name="reviewed"))
        self.assertEqual(poe.read_json(self.run_dir/"run.json")["phase"],"exporting")
        self.assertFalse(poe.read_json(self.run_dir/"output/reviewed/delivery.json")["file_exported"])
        self.assertTrue(poe.export(args(run=str(self.run_dir),name="reviewed"))["file_exported"])
        plan["summary"]="Unreviewed changed explanation"
        self.assertIn("final_review:stale",self.evaluate_plan(plan,"reviewed")["pending"])



if __name__ == "__main__": unittest.main()
