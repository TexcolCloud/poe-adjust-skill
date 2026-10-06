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


def intent_for(root,*,game_version="3.29",normal_points=108,ascendancy_points=8):
    value={"schema_version":2,"confirmed":True,"goal":"离线测试：验证现有角色的改装计算","league":"国服测试赛季","mode":"both",
           "game_version":game_version,"budget":{"amount":20,"currency":"divine"},"target_level":int(root.find('Build').get('level')),
           "allow_main_skill_change":True,"allow_ascendancy_change":True,"scenario":{"enemyLevel":83},"baseline_main_group":1,
           "normal_points_available":normal_points,"ascendancy_points":ascendancy_points}
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
            self.assertIsNone(engine.metadata(root)['data_version'])
            (root/'TreeData/3_30').mkdir(parents=True)
            (root/'TreeData/3_30/tree.lua').write_text('return {}',encoding='utf8')
            (root/'TreeData/3_50_unregistered').mkdir()
            (root/'TreeData/3_50_unregistered/data.json').write_text('{}',encoding='utf8')
            info = engine.metadata(root)
            self.assertEqual(info["data_version"], "3.30")
            self.assertIsNone(info["version_source_hint"])
            self.assertNotIn("version_evidence", info)
            self.assertEqual(info['data_layout']['tree_locations']['3_30'],['TreeData/3_30/tree.lua'])
            self.assertEqual(info['data_layout']['missing_declared_trees'],['3_29','3_30_ruthless'])
            self.assertEqual(info['data_layout']['unregistered_tree_directories'],['3_50_unregistered'])
            self.assertEqual(info['latest_standard_tree'],'3_30')
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
            value=intent_for(self.root);value.pop(field,None)
            with self.subTest(field=field):self.assertIsNone(poe.validate_intent(value,self.root)[field])
        value=intent_for(self.root);value["constraint_sources"].pop("/budget")
        with self.assertRaisesRegex(ValueError,"/budget"):poe.validate_intent(value,self.root)

    def test_socket_layout_checks_actual_occupancy_and_links(self):
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



def route_fixture(game_version="3.29"):
    return {"schema_version":2,"name":"离线路线","confirmed":True,"goal":"验证阶段结构","objectives":[{"objective_id":"fixture-goal","text":"验证阶段结构","evidence_refs":["user"]}],"league":"测试赛季","class_id":4,"game_version":game_version,"ascendancy":"Champion","scope":"所列阶段","trade_mode":"ssf",
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
        self.equipment={stage["stage_id"]:{"bases":copy.deepcopy(stage["bases"]),"checks":{"Gloves":{"base":"Iron Gauntlets","valid_for_slot":True}},"context":{"basis":"declared_base_layout"}} for stage in self.route["stages"]}
    def check(self):
        return routes.validate(self.route,self.gems,"3.29",self.classes,bases=self.bases,slots=self.slots,equipment=self.equipment,evidence=self.evidence)

    def test_goal_reference_diagnostics_distinguish_missing_subject_and_unreviewed_claims(self):
        claims=[{"claim_id":"overall","status":"model_assessed","subjects":["goal"]},
                {"claim_id":"detail","status":"model_assessed","subjects":["objective:direction"]},
                {"claim_id":"unreviewed","status":"unresolved","subjects":["goal"]}]
        self.route["assessment"]={"goal_claim_ids":["absent","detail","unreviewed"],"scope":"Current declared direction"}
        def validate():
            return routes.validate(self.route,self.gems,"3.29",self.classes,bases=self.bases,slots=self.slots,
                                   equipment=self.equipment,evidence=self.evidence,claims=claims)[0]
        checked=validate()
        self.assertIn("goal_assessment",checked["pending"])
        self.assertEqual([d["location"] for d in checked["diagnostics"]],
                         ["/assessment/goal_claim_ids/0","/assessment/goal_claim_ids/1","/assessment/goal_claim_ids/2"])
        self.assertEqual([d["code"] for d in checked["diagnostics"]],
                         ["goal_claim_missing","goal_claim_subject_missing","goal_claim_unresolved"])
        self.assertTrue(all(d["available_goal_claim_ids"]==["overall"] for d in checked["diagnostics"]))
        self.route["assessment"]["goal_claim_ids"]=["overall"]
        checked=validate();self.assertNotIn("goal_assessment",checked["pending"]);self.assertEqual(checked["diagnostics"],[])
        self.route["assessment"]["scope"]="  "
        self.assertEqual(validate()["diagnostics"][0]["location"],"/assessment/scope")

    def test_mapping_types_rejected_at_real_route_entry_before_queries(self):
        for value in ({"evidence_id":"user"},["user"],None,False,12):
            with self.subTest(value=value),tempfile.TemporaryDirectory() as folder:
                route=copy.deepcopy(self.route);route["constraint_sources"]["/goal"]=value
                source=Path(folder)/"route.json";poe.write_json(source,route);before=source.read_bytes()
                with patch.object(poe,"native_queries") as native,self.assertRaises(bx.ContractError) as error:
                    poe.calculate_route(args(invoked=True,file=str(source),out=str(Path(folder)/"route.html")))
                native.assert_not_called();self.assertEqual(source.read_bytes(),before)
                self.assertEqual(error.exception.diagnostic["location"],"/constraint_sources/~1goal")

    def test_invalid_scalar_and_schedule_shapes_fail_before_route_queries(self):
        cases=[({"trade_mode":{}},"/trade_mode"),({"allow_mule":0},"/allow_mule"),
               ({"class_id":False},"/class_id"),({"budget":{"amount":True,"currency":"currency"}},"/budget/amount"),
               ({"budget":{"amount":1,"currency":[]}},"/budget/currency")]
        for change,location in cases:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as folder:
                route={**copy.deepcopy(self.route),**change}
                source=Path(folder)/"route.json";poe.write_json(source,route)
                with patch.object(poe,"native_queries") as native,self.assertRaises(bx.ContractError) as error:
                    poe.calculate_route(args(invoked=True,file=str(source),out=str(Path(folder)/"route.html")))
                native.assert_not_called();self.assertEqual(error.exception.diagnostic["location"],location)
        for field,value,location in [("usage",[{"skill_ref":[]}],"/usage/0/skill_ref"),
                                      ("timeline",{"duration_seconds":2,"events":[{"skill_ref":"observed","at":2}]},"/timeline/events/0/at")]:
            with self.subTest(field=field),tempfile.TemporaryDirectory() as folder:
                route=copy.deepcopy(self.route);route["stages"][0]["snapshot"]={field:value}
                source=Path(folder)/"route.json";poe.write_json(source,route)
                with patch.object(poe,"native_queries") as native,self.assertRaises(bx.ContractError) as error:
                    poe.calculate_route(args(invoked=True,file=str(source),out=str(Path(folder)/"route.html")))
                native.assert_not_called();self.assertEqual(error.exception.diagnostic["location"],"/stages/0/snapshot"+location)

    def test_unknown_version_and_unascended_state_remain_valid_shapes(self):
        route={**copy.deepcopy(self.route),"game_version":None,"ascendancy":None}
        self.assertIs(routes.preflight(route),route)
        for field in ("game_version","ascendancy"):
            with self.subTest(field=field),self.assertRaises(bx.ContractError) as error:
                routes.preflight({**route,field:{}})
            self.assertEqual(error.exception.diagnostic["location"],"/"+field)
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
        checked,_=self.check();self.assertNotIn("native:equipment:stage-0:Gloves",checked["pending"])
        self.equipment["stage-0"]["checks"]["Gloves"]["valid_for_slot"]=False
        with self.assertRaisesRegex(ValueError,"不相符"):self.check()
        self.equipment.clear();checked,_=self.check();self.assertIn("native:equipment:stage-0:Gloves",checked["pending"])

    def test_slot_check_from_other_layout_is_unknown_and_does_not_certify(self):
        self.equipment["stage-0"]["bases"]={"Gloves":"Different Base"}
        self.equipment["stage-0"]["checks"]["Gloves"]["valid_for_slot"]=False
        checked,_=self.check();self.assertIn("native:equipment:stage-0:Gloves",checked["pending"])

    def test_explicit_empty_base_is_not_an_unknown_catalog_name_or_socket_host(self):
        self.route["stages"][0]["bases"]["Gloves"]=None
        self.assertEqual(routes.base_names({"stages":[self.route["stages"][0]]}),[])
        with self.assertRaisesRegex(ValueError,"空槽"):self.check()
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
    def test_native_reference_diagnostics_recover_without_aliasing_old_values(self):
        native={"candidate":{"stats":{"FutureValue":None}},
                "resources":{"entries":[{"future/value":4}],"origin":"program_assessment"},
                "stage:future":{"config_values":{"new_condition":False}}}
        bad=["native:absent#/entries/0/future~1value",
             "native:candidate#/readiness/entries/0/future~1value",
             "native:resources#/entries/7/future~1value"]
        for reference in bad:
            with self.subTest(reference=reference):
                evidence={reference:{"status":"native_observed","value":999}}
                row=facts.assess_claims([{"claim_id":"current","text":"Check this observation.","subjects":["goal"],"evidence_refs":[reference]}],evidence,native)[0]
                self.assertEqual(row["status"],"unresolved")
                self.assertNotIn(reference,evidence)
                diagnostic=row["diagnostics"][0]["source_diagnostic"]
                contract=diagnostic["reference_contract"]
                self.assertEqual(set(contract),set(native))
                self.assertEqual(contract["resources"]["prefix"],"native:resources#")
                self.assertEqual(contract["stage:future"]["fields"]["config_values"],"native:stage:future#/config_values")
                self.assertIn("expected",diagnostic)
                self.assertIn("location",diagnostic)
        valid="native:resources#/entries/0/future~1value"
        fixed=facts.assess_claims([{"claim_id":"current","text":"A derived observation.","subjects":["goal"],"checks":[{"reference":valid,"op":"eq","value":4}]}],{},native)[0]
        self.assertEqual((fixed["status"],fixed["check_status"]),("model_assessed","supported"))

    def test_current_reference_contract_and_observation_provenance_survive_review(self):
        native={"candidate":{"origin":"native_fixture","stats":{"Observed":6}},
                "resources":{"origin":"program_assessment","timeline":{"status":"unknown","amount":None}},
                "stage:future":{"origin":"current_schema","future/field":{"a~b":False}}}
        refs=["native:candidate#/stats/Observed","native:resources#/timeline/amount","native:stage:future#/future~1field/a~0b"]
        claim={"claim_id":"observed","text":"Scoped values.","subjects":["goal"],"evidence_refs":refs,
               "checks":[{"reference":refs[1],"op":"ge","value":0}]}
        current=facts.assess_claims([claim],{},native)[0]
        self.assertEqual(current["check_status"],"unknown")
        self.assertEqual([r["origin"] for r in current["observations"]],["native_fixture","program_assessment","current_schema"])
        self.assertIsNone(current["observations"][1]["value"])
        self.assertIs(current["observations"][2]["value"],False)
        contract=facts.reference_contract(native)
        self.assertEqual(contract["stage:future"]["fields"],{"future/field":"native:stage:future#/future~1field","origin":"native:stage:future#/origin"})
        self.assertEqual(contract["resources"]["origin"],"program_assessment")
        packet,_=facts.final_review({}, {}, [current], {"reference_contract":contract}, {})
        self.assertEqual(packet["claims"][0]["observations"],current["observations"])

    def test_evidence_shape_has_paths_for_unknown_fields_types_and_missing_values(self):
        for key in ("collected_from","future_bad_field"):
            with self.subTest(key=key),self.assertRaises(bx.ContractError) as error:
                facts.evidence_shape([{"evidence_id":"first"},{"evidence_id":"second",key:"value"}],complete=False)
            self.assertEqual(error.exception.diagnostic["location"],"/evidence/1")
            self.assertEqual(error.exception.diagnostic["actual"],[key])
            self.assertIn("kind",error.exception.diagnostic["expected"])
        with self.assertRaises(bx.ContractError) as error: facts.evidence_shape([{"kind":"artifact","context":[]}],complete=False)
        self.assertEqual(error.exception.diagnostic["location"],"/evidence/0/context")
        with self.assertRaises(bx.ContractError) as error: facts.evidence_shape([{}])
        self.assertEqual(error.exception.diagnostic["location"],"/evidence/0/evidence_id")
        self.assertIsNone(error.exception.diagnostic["actual"])

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
        _,pending=facts.final_review({}, {},checked,{}, {"review":{"token":packet["token"],"findings":"Reviewed current missing values.","conclusions":[{"claim_id":"c","scope":"Current missing metric","findings":"No supported check; unknown retained."}]}})
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
            self.assertEqual(registry["price"]["source_base"],str(folder.resolve()))
            nested=folder/"declarations";nested.mkdir()
            rejected=facts.load_evidence([dict(declaration,path="../source.txt")],nested,{})
            self.assertEqual(rejected["price"]["status"],"unresolved")
            diagnostic=rejected["price"]["diagnostic"]
            self.assertEqual(diagnostic["location"],"/evidence/0/path")
            self.assertEqual(diagnostic["expected"]["source_base"],str(nested.resolve()))
            self.assertEqual(diagnostic["actual"],str((folder/"source.txt").resolve()))
            claim={"claim_id":"explanation","text":"Interpretation","subjects":["goal"],"evidence_refs":["price"]}
            assessed=facts.assess_claims([claim],rejected)[0]
            self.assertEqual(assessed["diagnostics"][0]["source_diagnostic"],diagnostic)
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
    def test_each_skill_mean_drain_uses_fixed_net_regen_and_excludes_leech(self):
        result=resource_fixture();result["resource_rules"]={"energy_shield_protects_mana":False}
        result["stats"].update(ManaUnreserved=240,ManaRegenRecovery=18,ManaDegen=3,ManaLeechGainRate=500)
        result["active_skills"].append({"skill_ref":"secondary","costs":{"ManaCost":20,"ManaPerSecondCost":75,"LifeCost":0,"LifePerSecondCost":0,"ESCost":0,"ESPerSecondCost":0},"usage_rate":{"value":3.75}})
        row=next(r for r in resources.single_payments(result) if r["skill_ref"]=="secondary" and r["resource_id"]=="Mana")
        mean=row["mean_use"]
        self.assertEqual((mean["cost_per_second"],mean["fixed_net_regeneration_per_second"],mean["net_drain_per_second"]),(75,15,60))
        self.assertEqual(mean["mean_pool_depletion_seconds"],4)
        self.assertEqual(mean["status"],"native_mean_projection")
        self.assertEqual(resources.assess(result,{}, {})["sustained_use"]["status"],"unknown")
        result["stats"]["NetManaRegen"]=75
        mean=next(r for r in resources.single_payments(result) if r["skill_ref"]=="secondary" and r["resource_id"]=="Mana")["mean_use"]
        self.assertEqual(mean["net_drain_per_second"],0)
        self.assertIsNone(mean["mean_pool_depletion_seconds"])

    def test_mean_projection_maps_payment_resource_and_preserves_unknown_values(self):
        result=resource_fixture();result["resource_rules"]={"energy_shield_protects_mana":True}
        result["stats"].update(ManaCost=10,ManaPerSecondCost=50,ESCost=2,ESPerSecondCost=10,EnergyShieldRecoveryCap=200,EnergyShieldRegenRecovery=20)
        mana,life,shield=resources.single_payments(result)
        self.assertEqual(mana["mean_use"]["cost_per_second"],0)
        self.assertEqual((shield["mean_use"]["cost_per_second"],shield["mean_use"]["net_drain_per_second"],shield["mean_use"]["mean_pool_depletion_seconds"]),(60,40,5))
        result["stats"].pop("EnergyShieldDegen")
        shield=resources.single_payments(result)[2]["mean_use"]
        self.assertEqual(shield["status"],"unknown")
        self.assertIsNone(shield["mean_pool_depletion_seconds"])
        result["stats"]["NetEnergyShieldRegen"]=-5
        self.assertAlmostEqual(resources.single_payments(result)[2]["mean_use"]["mean_pool_depletion_seconds"],200/65)
        result["resource_rules"]={}
        self.assertEqual(resources.single_payments(result)[0]["mean_use"]["status"],"unknown")

    def test_single_payment_preserves_unknown_rules_life_survival_and_shield_cap(self):
        result=resource_fixture();result["stats"].update(ManaCost=30,LifeCost=1000,ESCost=10,EnergyShieldRecoveryCap=35)
        rows=resources.single_payments(result)
        self.assertEqual([r["status"] for r in rows],["unknown","refuted","unknown"])
        result["resource_rules"]={"energy_shield_protects_mana":True}
        rows=resources.single_payments(result)
        self.assertEqual((rows[0]["payment"],rows[0]["status"]),(0,"supported"))
        self.assertEqual((rows[2]["payment"],rows[2]["pool"],rows[2]["status"]),(40,35,"refuted"))
        result["active_skills"].append({"skill_ref":"unmeasured","costs":{}})
        missing=[r for r in resources.single_payments(result) if r["skill_ref"]=="unmeasured"]
        self.assertEqual([r["status"] for r in missing],["supported","unknown","unknown"])
        result["active_skills"][-1]["no_cost"]=True
        self.assertTrue(all(r["payment"]==0 and r["status"]=="supported" for r in resources.single_payments(result) if r["skill_ref"]=="unmeasured"))

    def test_final_review_requires_each_current_claim_scope(self):
        claims=[{"claim_id":"one","text":"A scoped observation"},{"claim_id":"two","text":"Another conclusion"}]
        packet,_=facts.final_review({}, {}, claims, {}, {})
        review={"token":packet["token"],"findings":"Reviewed everything.","unresolved":[]}
        checked,pending=facts.final_review({}, {}, claims, {}, {"review":review})
        self.assertEqual(checked["status"],"unresolved")
        self.assertEqual(checked["coverage"]["missing_claim_ids"],["one","two"])
        review["conclusions"]=[{"claim_id":"one","scope":"one snapshot only","findings":"Observed, no temporal promise."}]
        self.assertEqual(facts.final_review({}, {}, claims, {}, {"review":review})[0]["coverage"]["missing_claim_ids"],["two"])
        review["conclusions"].append({"claim_id":"two","scope":"user preference","findings":"Interpretation, not a measured guarantee."})
        self.assertFalse(facts.final_review({}, {}, claims, {}, {"review":review})[1])
        review["conclusions"].append({"claim_id":"unknown","scope":"unrelated","findings":"Cannot count this as current coverage."})
        self.assertIn("final_review:unknown_claim:unknown",facts.final_review({}, {}, claims, {}, {"review":review})[1])
        review["conclusions"].pop()
        review["conclusions"].append(copy.deepcopy(review["conclusions"][0]))
        with self.assertRaises(ValueError): facts.final_review({}, {}, claims, {}, {"review":review})

    def test_final_review_covers_each_user_objective_even_without_a_claim(self):
        objectives=[{'objective_id':'custom-first','text':'A player requirement','claim_ids':['one'],'status':'model_assessed'},
                    {'objective_id':'custom-later','text':'Another player requirement','claim_ids':[],'status':'unresolved'}]
        observations={'objective_coverage':copy.deepcopy(objectives),'pending':['objective:custom-later']}
        claims=[{'claim_id':'one','text':'A scoped observation'}]
        packet,_=facts.final_review({}, {}, claims, observations, {})
        review={'token':packet['token'],'findings':'Current claims checked.',
                'conclusions':[{'claim_id':'one','scope':'Current object','findings':'This observation is scoped.'}]}
        checked,pending=facts.final_review({}, {}, claims, observations, {'review':review})
        self.assertEqual(checked['status'],'unresolved')
        self.assertEqual(checked['coverage']['missing_objective_ids'],['custom-first','custom-later'])
        self.assertEqual([row['objective_id'] for row in packet['review_template']['objectives']],['custom-first','custom-later'])
        self.assertIn('final_review:objective:custom-later',pending)
        review['objectives']=[{'objective_id':row['objective_id'],'scope':'Actual player requirement',
                              'findings':'Measured scope reviewed; missing dependencies remain.'} for row in objectives]
        checked,pending=facts.final_review({}, {}, claims, observations, {'review':review})
        self.assertEqual(checked['status'],'model_reviewed');self.assertFalse(pending)
        self.assertEqual(checked['facts']['pending'],['objective:custom-later'])
        self.assertEqual(checked['facts']['objective_coverage'][1]['status'],'unresolved')
        review['objectives'].append({'objective_id':'foreign','scope':'Different requirement','findings':'Cannot substitute this goal.'})
        self.assertIn('final_review:unknown_objective:foreign',facts.final_review({}, {}, claims, observations, {'review':review})[1])
        review['objectives'].pop();review['objectives'].append(copy.deepcopy(review['objectives'][0]))
        with self.assertRaises(bx.ContractError) as caught:
            facts.final_review({}, {}, claims, observations, {'review':review})
        self.assertEqual(caught.exception.diagnostic['location'],'/assessment/review/objectives/2/objective_id')
        observations['objective_coverage'].append({'objective_id':'added','text':'New requirement','claim_ids':[],'status':'unresolved'})
        review['objectives'].pop()
        self.assertIn('final_review:stale',facts.final_review({}, {}, claims, observations, {'review':review})[1])
        self.assertEqual(objectives[1]['status'],'unresolved')

    def test_all_active_skills_have_independent_single_payment_facts(self):
        result=resource_fixture();result["resource_rules"]={"energy_shield_protects_mana":False}
        result["stats"].update(ManaUnreserved=50)
        result["active_skills"].append({"skill_ref":"aux","costs":{"ManaCost":70,"LifeCost":0,"ESCost":0},"usage_rate":{"value":1}})
        audit=resources.assess(result,{}, {})
        row=next(r for r in audit["single_payments"] if r["skill_ref"]=="aux" and r["resource_id"]=="Mana")
        self.assertEqual((row["payment"],row["pool"],row["status"]),(70,50,"refuted"))
        self.assertTrue(audit["errors"])
        result["stats"]["ManaUnreserved"]=100
        audit=resources.assess(result,{}, {})
        row=next(r for r in audit["single_payments"] if r["skill_ref"]=="aux" and r["resource_id"]=="Mana")
        self.assertEqual(row["status"],"supported")
        self.assertEqual(audit["sustained_use"]["status"],"unknown")
        self.assertIn("usage:aux",audit["pending"])

    def test_snapshot_positive_balance_does_not_prove_sustained_use(self):
        result=resource_fixture();result["stats"].update(LifeCost=10,LifePerSecondCost=20,LifeLeechGainRate=30)
        audit=resources.assess(result,{}, {})
        row=next(r for r in audit["resources"] if r["resource_id"]=="Life")
        self.assertTrue(row["snapshot_balance_nonnegative"])
        self.assertNotIn("sustainable_while_hitting",row)
        packet=poe.publication("draft",[],audit["pending"],{"status":"unknown"},{"candidate":result},readiness=audit)
        statement=next(s for s in packet["statements"] if s["kind"]=="sustained_use")
        self.assertEqual(statement["status"],"unknown")
        self.assertIn("持续",statement["text"])

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
        spec["assessment"]["review"]={"token":packet["token"],"findings":"Checked object, numbers, causality, alternatives, conditions and scope.","unresolved":[],"conclusions":[{"claim_id":"c","scope":"Main skill snapshot only","findings":"Source, object and current values match; no temporal guarantee."}]}
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
        self.assertIsNone(audit["resources"][0]["snapshot_balance_nonnegative"])

    def test_flask_prose_cannot_certify_continuous_coverage(self):
        result=resource_fixture();result["equipped"]=[{"slot":"Flask 1","id":1,"active":True,"flask_local":{"duration":5,"chargesMax":30,"chargesUsed":10}}]
        claim={"claim_id":"flask","status":"model_assessed","subjects":["condition:Flask 1"]}
        audit=resources.assess(result,{}, {"usage":[{"skill_ref":"main","continuous":True}]},[claim])
        self.assertIn("capability:flask_coverage:Flask 1",audit["pending"])
        self.assertEqual(audit["conditions"]["flask_facts"][0]["flask_local"]["chargesUsed"],10)

    def test_large_evaluation_terminal_summary_points_to_complete_workspace_record(self):
        with tempfile.TemporaryDirectory() as folder:
            output=io.StringIO();run=Path(folder)
            long={"status":"draft","candidate":"fixture","pending":["unknown:"+str(i)+"x"*80 for i in range(200)],"readiness":{"details":"x"*20000}}
            artifact=run/"candidates/test/evaluation.json";poe.write_json(artifact,{"response":long})
            with patch.object(poe,"evaluate",return_value=long),redirect_stdout(output):
                code=poe.main(["evaluate","--run",str(run),"--name","test","--plan","fixture.json"])
            response=json.loads(output.getvalue())
            self.assertEqual(code,0);self.assertEqual(response["pending_total"],200);self.assertLess(len(output.getvalue()),12000)
            pointers={r["pointer"] for r in response["continuations"]}
            self.assertTrue({"/response/pending","/response/readiness"}<=pointers)
            self.assertEqual(facts.pointer(poe.read_json(response["details_file"]),"/response/pending"),long["pending"])

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
    def test_probe_damage_observation_keeps_selected_skill_and_actor_context(self):
        with tempfile.TemporaryDirectory() as folder:
            run=Path(folder);poe.write_text(run/'original.xml',SAMPLE)
            variant_file=run/'variants.json';poe.write_json(variant_file,{'variants':[{'id':'secondary-change','operations':{}}]})
            baseline={'stats':{'CombinedDPS':100},'main_skill':'fixture-main','main_skill_name':'Fixture main','main_group':1,'config_values':{'native-scene':True},'engine_fingerprint':'fixture','input_xml_sha256':'before',
                      'active_skills':[{'id':'fixture-main','name':'Fixture main','skill_ref':'qualified-main','is_main':True,'actor':'player'}],
                      'stat_metadata':{'CombinedDPS':[{'label':'Combined DPS','format':'.1f','actor':'player'}]}}
            candidate=copy.deepcopy(baseline);candidate.update(input_xml_sha256='after',points=1,ascendancy_points=0,secondary_ascendancy_points=0)
            record=(run,{'intent':{}},{'plan':{'mode':'refine'},'engine_fingerprint':'fixture','baseline':baseline},SAMPLE,run)
            with patch.object(poe,'read_candidate',return_value=record),patch.object(engine,'calculate',side_effect=[baseline,{'calculations':[candidate]}]),patch.object(poe,'materialize_items',side_effect=lambda plan,root:(plan,[])),patch.object(poe,'mechanical_issues',return_value=([],[],[])),patch.object(poe,'check_constraints',return_value=([],[])),patch.object(resources,'assess',return_value={'errors':[],'pending':[],'warnings':[]}):
                response=poe.probe(args(run=str(run),name='fixture',variants=str(variant_file)))
            observation=response['alternatives'][0]['damage_observation']
            self.assertEqual(response['alternatives'][0]['dps_delta'],0)
            self.assertEqual(observation['context']['main_skill_ref'],'qualified-main')
            self.assertEqual(observation['context']['main_group'],1)
            self.assertEqual(observation['meaning']['actor'],'player')
            self.assertEqual(observation['context']['config_values'],{'native-scene':True})
            self.assertEqual(response['baseline_damage_observation']['context']['xml_sha256'],'before')

    def test_damage_observation_selects_current_minion_metric_and_keeps_missing_unknown(self):
        calculation={'stats':{'CombinedDPS':0,'MinionCombinedDPS':230},'main_skill':'native-minion','active_skills':[{'skill_ref':'current-minion','is_main':True,'minion':True,'actor':'minion'}]}
        observed=poe.damage_observation(calculation,'candidate')
        self.assertEqual((observed['locator'],observed['value']),('/stats/MinionCombinedDPS',230))
        self.assertEqual(observed['context']['main_skill_ref'],'current-minion')
        calculation['stats'].pop('MinionCombinedDPS')
        self.assertIsNone(poe.damage_observation(calculation,'candidate')['value'])

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

    def test_route_snapshot_keeps_contextual_tree_path_and_source_repair(self):
        stage={'stage_id':'dynamic','level':12,'ascendancy_allocated_nodes':[],
               'skill_groups':[{'name':'Fixture skill','slot':'Gloves','gems':[{'skill_id':'Fireball','level':1,'quality':0}]}],
               'snapshot':{'operations':{'tree':{'nodes':[1,2,42,43]}}}}
        route={'class_id':1,'stages':[stage]}
        native={'xml':SAMPLE,'engine_fingerprint':'fixture','classes':[{'id':1,'name':'Current class','ascendancies':[]}]}
        actual=bx.parse(SAMPLE);bx.active_spec(actual).set('nodes','1,2')
        result={'xml':bx.serialise(actual),'engine_fingerprint':'fixture','nodes':[1,2],'class_id':1,'ascendancy_id':0}
        catalog={'engine_fingerprint':'fixture','catalog_batch':[{'entries':[{'id':42,'name':'Current node','reachable':True,'points_to_allocate':3,'path':[20,30,42]}]}, {'entries':[]}]}
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'route.html';poe.write_json(output.with_suffix('.state.json'),{})
            for cached in (False,True):
                with self.subTest(cached=cached),patch.object(engine,'calculate',return_value=copy.deepcopy(result)) as calculate,patch.object(engine,'validate_contract'),patch.object(poe,'roundtrip',return_value=result['xml']) as roundtrip,patch.object(poe,'native_queries',return_value=catalog) as query:
                    record=poe.route_snapshots(route,native,Path(folder),Path(folder),output)['dynamic']
                self.assertEqual(record['status'],'calculated',record)
                detail=next(r for r in record['tree_diagnostics'] if r['id']==42)
                self.assertEqual(detail['path'],[20,30,42]);self.assertEqual(detail['points_to_allocate'],3)
                self.assertEqual(record['tree_diagnostics'][-1],{'id':43,'found':False})
                self.assertEqual(query.call_args.args[2],result['xml'])
                self.assertEqual([q['query'] for q in query.call_args.args[1]],['id:42','id:43'])
                diagnostic=next(d for d in record['diagnostics'] if d['code']=='native_tree_nodes_unretained')
                self.assertEqual(diagnostic['location'],'/stages/0/snapshot/operations/tree')
                self.assertEqual(diagnostic['expected'],[1,2,42,43]);self.assertEqual(diagnostic['actual'],[1,2])
                self.assertEqual(diagnostic['native_context']['xml_sha256'],bx.digest(result['xml']))
                self.assertEqual(stage['snapshot']['operations']['tree']['nodes'],[1,2,42,43])
                action=poe.delivery_actions({'status':'blocked','errors':['dropped nodes'],'pending':[],'diagnostics':record['diagnostics']},'route.json','result.json','review.json')[0]
                self.assertEqual(action['diagnostics'],record['diagnostics'])
                self.assertEqual(calculate.call_count,0 if cached else 1)
                self.assertEqual(roundtrip.call_count,0 if cached else 1)

    def test_tree_connection_proposal_is_an_explicit_source_edit_with_current_context(self):
        document=bx.parse(SAMPLE);bx.active_spec(document).set('nodes','1,2,42,43')
        actual=copy.deepcopy(document);bx.active_spec(actual).set('nodes','1,2')
        result={'xml':bx.serialise(actual),'engine_fingerprint':'fixture','nodes':[1,2],'class_id':1,'ascendancy_id':0,'stats':{'Life':100},'classes':[{'id':1,'name':'Current class'}]}
        allocated={**result,'nodes':[1,2,20,30,42,43],'native_tree_allocation':{'method':'PassiveSpec.AllocNode','targets':[{'id':42,'retained':True},{'id':43,'retained':True}]}}
        preview={**allocated,'points':4,'ascendancy_points':0,'secondary_ascendancy_points':0,'stats':{'Life':125},'active_skills':[]}
        catalog={'engine_fingerprint':'fixture','catalog_batch':[
            {'entries':[{'id':42,'reachable':True,'path':[20,30,42],'points_to_allocate':3}]},
            {'entries':[{'id':43,'reachable':True,'path':[30,43],'points_to_allocate':2}]}]}
        location='/stages/6/snapshot/operations/tree'
        for tree in ({'nodes_add':[42,43],'nodes_remove':[99],'masteries_add':{'2':11},'class_id':1},
                     {'nodes':[1,2,42,43],'masteries':{'2':11},'ascendancy_id':0}):
            original=copy.deepcopy(tree);requested=bx.serialise(document)
            with patch.object(poe,'native_queries',return_value=catalog) as query,patch.object(engine,'calculate',side_effect=[allocated,preview]) as calculate,patch.object(poe,'roundtrip',return_value='native saved XML'),patch.object(poe,'mechanical_issues',return_value=([],[],[])),patch.object(resources,'assess',return_value={'errors':[],'warnings':[],'pending':[],'point_budgets':[{'pool':'normal','used':4,'available':4,'status':'within_declared_limit'}]}):
                _,diagnostics=poe.native_tree_diagnostics(Path('.'),document,result,location,tree_operations=tree,limits={'normal_points_available':4})
            proposal=diagnostics[0]['connection_proposal']
            changed=proposal['changes']['replace'][location]
            self.assertEqual(changed['nodes'],[1,2,20,30,42,43])
            self.assertNotIn('nodes_add',changed);self.assertNotIn('nodes_remove',changed)
            for key,value in original.items():
                if key not in {'nodes','nodes_add','nodes_remove'}: self.assertEqual(changed[key],value)
            self.assertEqual(proposal['additional_node_ids'],[20,30])
            self.assertEqual(calculate.call_args_list[0].kwargs,{'tree_targets':[42,43],'tree_masteries':{}})
            self.assertEqual(proposal['preview']['point_budgets'][0]['used'],4)
            self.assertEqual(proposal['preview']['comparison']['Life']['delta'],25)
            self.assertTrue(proposal['adoptable']);self.assertTrue(proposal['preview']['roundtrip_verified'])
            self.assertEqual(query.call_args.args[2],result['xml'])
            self.assertEqual(tree,original);self.assertEqual(bx.serialise(document),requested)
            self.assertEqual(diagnostics[0]['native_context']['engine_fingerprint'],'fixture')
            action=poe.delivery_actions({'status':'blocked','errors':['Dropped nodes'],'pending':[],
                                       'diagnostics':diagnostics},'route.json','result.json','review.json')[0]
            self.assertNotIn('connection_proposal',action['diagnostics'][0])

    def test_tree_connection_proposal_cannot_undo_removals_or_guess_unknown_paths(self):
        document=bx.parse(SAMPLE);bx.active_spec(document).set('nodes','1,2,42')
        actual=copy.deepcopy(document);bx.active_spec(actual).set('nodes','1,2')
        result={'xml':bx.serialise(actual),'engine_fingerprint':'fixture','nodes':[1,2]}
        variants=[{'entries':[]}, {'entries':[{'id':42,'reachable':False,'path':[],'points_to_allocate':1000}]},
                  {'entries':[{'id':42,'reachable':True,'path':[20,42],'points_to_allocate':2}]}]
        for row in variants:
            with patch.object(poe,'native_queries',return_value={'engine_fingerprint':'fixture','catalog_batch':[row]}),patch.object(engine,'calculate',return_value={**result,'nodes':[1,2,20,42],'native_tree_allocation':{'method':'PassiveSpec.AllocNode'}}):
                _,diagnostics=poe.native_tree_diagnostics(Path('.'),document,result,tree_operations={'nodes_add':[42],'nodes_remove':[20]})
            self.assertNotIn('connection_proposal',diagnostics[0])
        with patch.object(poe,'native_queries',return_value={'engine_fingerprint':'fixture','catalog_batch':[variants[-1]]}):
            _,diagnostics=poe.native_tree_diagnostics(Path('.'),document,result)
        self.assertNotIn('connection_proposal',diagnostics[0])
        for batch in ([],[variants[-1]]):
            bx.active_spec(document).set('nodes','1,2,42,43')
            with patch.object(poe,'native_queries',return_value={'engine_fingerprint':'fixture','catalog_batch':batch}):
                details,diagnostics=poe.native_tree_diagnostics(Path('.'),document,result,tree_operations={'nodes_add':[42,43]})
            self.assertEqual([row['id'] for row in details],[42,43])
            self.assertFalse(details[-1]['query_returned'])
            self.assertNotIn('connection_proposal',diagnostics[0])

    def test_final_review_action_points_to_actual_user_goals_and_template(self):
        observations={'objective_coverage':[{'objective_id':'dynamic','text':'Actual requirement','claim_ids':[],'status':'unresolved'}],
                      'pending':['objective:dynamic']}
        packet,_=facts.final_review({}, {}, [], observations, {})
        outcome={'status':'draft','errors':[],'pending':['objective:dynamic','final_review:missing']}
        action=poe.delivery_actions(outcome,'source.json','details.json','current-review.json')[-1]
        self.assertEqual(action['review_file'],'current-review.json')
        self.assertEqual(facts.pointer(packet,action['objective_pointer']),observations['objective_coverage'])
        self.assertEqual(facts.pointer(packet,action['template_pointer']),packet['review_template'])

    def test_delivery_actions_include_objective_and_refuted_check_source_repairs(self):
        diagnostics=[{'code':'objective_subject_missing','action':'link_claim','location':'/claims','required_subject':'objective:actual'},
                     {'code':'objective_basis_missing','action':'record_user_basis','location':'/objectives/0/evidence_refs'},
                     {'code':'claim_check_refuted','action':'revise_conclusion','location':'/claims/0/checks/0','actual':0,'expected':1}]
        outcome={'status':'draft','errors':[],'pending':['objective:actual','claim_check:current'],'diagnostics':diagnostics}
        action=poe.delivery_actions(outcome,'source.json','result.json','review.json','/checked_route')[0]
        self.assertEqual(action['action'],'repair_input')
        self.assertEqual(action['diagnostics'],diagnostics)
        self.assertEqual(action['pointer'],'/checked_route/diagnostics')

    def test_missing_claim_bindings_return_direct_review_action_before_final_review(self):
        diagnostics=[{"code":"claim_binding_missing","action":"review_claims","claim_id":"future-id","location":"/claims/0/bindings"},
                     {"code":"claim_binding_stale","action":"review_claims","claim_id":"other-id","location":"/claims/1/bindings"}]
        outcome={"status":"draft","errors":[],"pending":["claim_revision:future-id","claim_revision:other-id","final_review:missing"],"diagnostics":diagnostics}
        actions=poe.delivery_actions(outcome,'source.json','result.json','review.json','/checked_route')
        action=next(row for row in actions if row["action"]=="review_claims")
        self.assertEqual(action["claim_ids"],["future-id","other-id"])
        self.assertEqual(action["changes"],{"review_claims":["future-id","other-id"]})
        self.assertEqual(action["input_file"],"source.json")
        self.assertEqual(action["diagnostics"],diagnostics)
        self.assertLess(actions.index(action),next(i for i,row in enumerate(actions) if row["action"]=="review_current_result"))

    def test_calculation_facts_exposes_current_keys_without_inventing_values(self):
        result={'stats':{'SeasonSpecificObservation':0,'SeasonSpecificFlag':False,'Life':1}}
        observed=poe.calculation_facts(result)
        self.assertEqual(observed['available_stat_keys'],sorted(result['stats']))
        self.assertNotIn('SeasonSpecificObservation',observed['stats'])
        referenced=poe.calculation_facts(result,[{'evidence_refs':['native:candidate#/stats/SeasonSpecificObservation','native:candidate#/stats/SeasonSpecificFlag']}])
        self.assertEqual(referenced['stats'],result['stats'])

    def test_tree_diagnosis_checks_current_fingerprint_and_skips_retained_nodes(self):
        document=bx.parse(SAMPLE)
        result={'nodes':[1,2],'xml':SAMPLE,'engine_fingerprint':'current'}
        with patch.object(poe,'native_queries',side_effect=AssertionError('no missing nodes')):
            self.assertEqual(poe.native_tree_diagnostics(Path('.'),document,result),([],[]))
        result['nodes']=[1]
        with patch.object(poe,'native_queries',return_value={'engine_fingerprint':'changed'}),self.assertRaisesRegex(ValueError,'引擎变化'):
            poe.native_tree_diagnostics(Path('.'),document,result)

    def test_publication_reports_actual_tree_and_keeps_unknown_distinct_from_zero(self):
        calculation={'level':20,'points':0,'ascendancy_points':None,'nodes':[], 'stats':{'LifeRegenRecovery':0,'ManaRegenRecovery':2}}
        observed=poe.calculation_facts(calculation)
        self.assertEqual(observed['nodes'],[])
        published=poe.publication('draft',[],['objective:future'],{'status':'unknown'},{'stage:future':observed})
        row=next(s for s in published['statements'] if s['kind']=='native_build')
        self.assertEqual(row['values'],{'level':20,'points':0,'ascendancy_points':None})
        self.assertIn('普通天赋已分配 0 点',row['text']);self.assertIn('升华已分配 未知 点',row['text'])
        measurements={r['metric']:r['value'] for r in published['measurements']}
        self.assertEqual(measurements,{'LifeRegenRecovery':0,'ManaRegenRecovery':2})

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

    def test_view_missing_pointer_rejects_and_oversized_selection_pages_real_value(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'evidence.json';poe.write_json(path,{'large':'x'*20000})
            stderr=io.StringIO()
            with redirect_stderr(stderr):code=poe.main(['view','--file',str(path),'--pointer','/missing'])
            response=json.loads(stderr.getvalue());self.assertEqual(code,2);self.assertFalse(response['ok']);self.assertNotIn('value',response)
            stdout=io.StringIO()
            with redirect_stdout(stdout):code=poe.main(['view','--file',str(path),'--pointer','/large','--limit','4000'])
            response=json.loads(stdout.getvalue());self.assertEqual(code,0);self.assertEqual(response['value'],'x'*4000);self.assertEqual(response['next_offset'],4000)

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
            template=root/'template.xml';poe.write_text(template,'original template')
            poe.write_json(source,{'goal':'fixture'});poe.write_text(output,'fixture report')
            poe.write_json(state,{'input':str(source),'input_sha256':bx.digest(source.read_bytes()),'output':str(output),'phase':'exported','file_exported':True,
                                 'stages':{'a':{'status':'calculated'},'b':{'status':'unsupported'}},
                                 'input_artifacts':{str(template):bx.digest(template.read_bytes())},
                                 'engine_fingerprint':'fixture','files':{str(output):bx.digest(output.read_bytes())}})
            with patch.object(engine,'resolve_root',return_value=root),patch.object(engine,'fingerprint',return_value='fixture'):
                self.assertTrue(poe.workflow_status(args(route_state=str(state)))['file_exported'])
                self.assertEqual(poe.workflow_status(args(route_state=str(state)))['completed_stages'],['a'])
                poe.write_text(template,'changed template')
                stale=poe.workflow_status(args(route_state=str(state)))
                self.assertFalse(stale['file_exported'])
                self.assertFalse(stale['input_artifacts_unchanged'])
                self.assertEqual(stale['completed_stages'],[])
                poe.write_text(template,'original template')
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

    def test_finite_window_does_not_require_pool_restoration_or_loop_seam(self):
        result,timeline=self.fixture();result["stats"]["ManaRegenRecovery"]=1
        timeline.update(repeat=False,events=[{"skill_ref":"main","at":0},{"skill_ref":"move","at":1},{"skill_ref":"main","at":2.9}])
        audit=resources.assess(result,{}, {"timeline":timeline})
        self.assertEqual(audit["timeline"]["status"],"supported")
        self.assertFalse(audit["timeline"]["resources"][0]["repeatable"])
        self.assertNotIn("resource:Mana:timeline_repeat",audit["pending"])
        self.assertEqual(audit["sustained_use"]["status"],"unknown")
        packet=poe.publication("draft",[],audit["pending"],{"status":"unknown"},{},readiness=audit)
        resource=next(row for row in packet["statements"] if row["kind"]=="resource" and row["subject"]=="Mana")
        self.assertEqual(resource["status"],"supported")
        timeline["events"][2]["at"]=.1
        self.assertTrue(resources.timeline_assessment(result,timeline)["errors"])
        with self.assertRaises(bx.ContractError):resources.input_shape({"timeline":{**timeline,"repeat":0}})

    def test_unscheduled_paid_skill_has_input_diagnostic_not_capability_label(self):
        result,timeline=self.fixture();timeline["events"]=[e for e in timeline["events"] if e["skill_ref"]!="move"]
        audit=resources.timeline_assessment(result,timeline)
        self.assertEqual(audit["status"],"unknown")
        diagnostic=next(d for d in audit["diagnostics"] if d["code"]=="timeline_skill_unscheduled")
        self.assertEqual(diagnostic["skill_ref"],"move")
        self.assertEqual(diagnostic["resource_id"],"Mana")
        self.assertEqual(diagnostic["location"],"/timeline/events")
        self.assertEqual(diagnostic["action"],"edit_input")
        self.assertNotIn("capability:timeline:Mana",audit["pending"])
        del result["active_skills"][0]["costs"]["ManaCost"]
        self.assertIn("capability:timeline:Mana",resources.timeline_assessment(result,timeline)["pending"])

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
    def saved_proposal(self,kind='route'):
        first=self.edit({'set':{'stages':[{'stage_id':'one','snapshot':{'operations':{'tree':{'nodes_add':[42]}}}}]}}) if kind=='route' else self.edit({'set':{'mode':'refine','operations':{'gems':[{'group':1,'index':1,'skill_id':'FutureSkill','level':20},{'group':1,'index':2,'skill_id':'OtherSkill','level':7}]}}},kind='plan')
        file=self.folder/'result.evidence.json' if kind=='route' else self.folder/'run/candidates/native/evaluation.json'
        change={'replace':{'/stages/0/snapshot/operations/tree':{'nodes':[20,42]}}} if kind=='route' else {'set':{'operations':{'gems':[{'group':1,'index':1,'level':5}]}}}
        proposal={'proposal_type':'native_tree_connection' if kind=='route' else 'native_gem_level','changes':change,'adoptable':True,'blockers':[],'preview':{'roundtrip_verified':True}}
        outcome={'status':'blocked','errors':['native unmet input'],'pending':['objective:actual'],'diagnostics':[{'code':'native_unmet','action':'edit_input','location':'/stages/0/snapshot/operations/tree' if kind=='route' else '/operations/gems','connection_proposal' if kind=='route' else 'change_proposal':proposal}]}
        actions=poe.delivery_actions(outcome,self.source,file,file.with_suffix('.review.json'),'/checked_route' if kind=='route' else '')
        if kind=='route':
            saved={'skill_version':poe.VERSION,'engine_fingerprint':'fixture','checked_route':outcome,'next_actions':actions}
            state_path=self.folder/'result.state.json'
            state={'engine_fingerprint':'fixture','input':str(self.source),'input_sha256':first['revision'],'file_exported':True}
        else:
            original=self.folder/'run/original.xml';poe.write_text(original,SAMPLE)
            saved={**outcome,'skill_version':poe.VERSION,'engine_fingerprint':'fixture','source_plan':str(self.source),'source_plan_sha256':first['revision'],'next_actions':actions,'intent_hash':None,'original_sha256':bx.digest(original.read_bytes())}
            state_path=self.folder/'run/run.json';state={'schema_version':2,'invoked':True,'intent':None,'intent_hash':None,'original_sha256':saved['original_sha256'],'engine_fingerprint':'fixture'}
        poe.write_json(file,saved)
        if kind=='route':state['files']={str(file):bx.digest(file.read_bytes())}
        else:state['candidates']={'native':{'evaluation_sha256':bx.digest(file.read_bytes())}}
        poe.write_json(state_path,state)
        return args(invoked=True,kind=kind,file=str(self.source),changes=None,proposal_file=str(file),proposal_id=proposal['proposal_id'],if_sha256=first['revision']),state_path

    def test_native_proposal_reference_applies_full_changes_and_replays_once(self):
        for kind in ('route','plan'):
            with self.subTest(kind=kind):
                self.source=self.folder/(kind+'.json')
                request,_=self.saved_proposal(kind)
                before=self.source.read_bytes()
                stale=copy.copy(request);stale.if_sha256='old'
                with self.assertRaises(bx.ContractError):poe.edit_document(stale)
                self.assertEqual(self.source.read_bytes(),before)
                with patch.object(engine,'calculate') as native:
                    saved=poe.edit_document(request);replay=poe.edit_document(request)
                native.assert_not_called();self.assertTrue(replay['replayed'])
                self.assertEqual(saved['adopted_proposal']['proposal_id'],request.proposal_id)
                value=poe.read_json(self.source)
                if kind=='route':self.assertEqual(value['stages'][0]['snapshot']['operations']['tree'],{'nodes':[20,42]})
                else:
                    self.assertEqual(value['operations']['gems'][0],{'group':1,'index':1,'skill_id':'FutureSkill','level':5})
                    self.assertEqual(value['operations']['gems'][1]['level'],7)

    def test_native_proposal_reference_rejects_changed_source_engine_and_receipt(self):
        request,state_path=self.saved_proposal()
        original=self.source.read_bytes()
        other=self.folder/'other.json';other.write_bytes(original)
        wrong=copy.copy(request);wrong.file=str(other)
        with self.assertRaises(bx.ContractError):poe.edit_document(wrong)
        wrong=copy.copy(request);wrong.proposal_id='unknown'
        with self.assertRaises(bx.ContractError):poe.edit_document(wrong)
        with patch.object(engine,'fingerprint',return_value='updated'),self.assertRaises(bx.ContractError):poe.edit_document(request)
        self.edit({'set':{'name':'later input'}},request.if_sha256)
        request.if_sha256=bx.digest(self.source.read_bytes())
        newer=self.source.read_bytes()
        with self.assertRaises(bx.ContractError):poe.edit_document(request)
        self.assertEqual(self.source.read_bytes(),newer)
        self.source.write_bytes(original)
        payload=poe.read_json(request.proposal_file);payload['checked_route']['diagnostics'][0]['connection_proposal']['changes']['replace']['/stages/0/snapshot/operations/tree']['nodes'].append(99)
        poe.write_json(request.proposal_file,payload)
        request.if_sha256=bx.digest(original)
        with self.assertRaises(bx.ContractError):poe.edit_document(request)
        state=poe.read_json(state_path);state['files'][request.proposal_file]=bx.digest(Path(request.proposal_file).read_bytes());poe.write_json(state_path,state)
        with self.assertRaises(bx.ContractError):poe.edit_document(request)
        self.assertEqual(self.source.read_bytes(),original)

    def test_native_proposal_cli_and_known_conflict_preserve_source(self):
        request,state_path=self.saved_proposal()
        before=self.source.read_bytes();payload=poe.read_json(request.proposal_file)
        proposal=payload['checked_route']['diagnostics'][0]['connection_proposal'];proposal['adoptable']=False;proposal['blockers']=['point_budget:normal']
        proposal.pop('proposal_id');proposal['proposal_id']=bx.digest(json.dumps(proposal,sort_keys=True,ensure_ascii=False,allow_nan=False))
        request.proposal_id=proposal['proposal_id'];poe.write_json(request.proposal_file,payload)
        state=poe.read_json(state_path);state['files'][request.proposal_file]=bx.digest(Path(request.proposal_file).read_bytes());poe.write_json(state_path,state)
        stdout,stderr=io.StringIO(),io.StringIO()
        with redirect_stdout(stdout),redirect_stderr(stderr):code=poe.main(['edit','--invoked','--kind','route','--file',request.file,'--proposal-file',request.proposal_file,'--proposal-id',request.proposal_id,'--if-sha256',request.if_sha256])
        self.assertEqual(code,2);self.assertIn('point_budget:normal',stderr.getvalue());self.assertEqual(self.source.read_bytes(),before)

    def test_native_proposal_rejects_changed_intent_or_external_template(self):
        for change in ('intent','template','original','route-template'):
            with self.subTest(change=change):
                self.source=self.folder/(change+'.json');request,state_path=self.saved_proposal('route' if change=='route-template' else 'plan')
                before=self.source.read_bytes()
                if change=='intent':
                    state=poe.read_json(state_path);state['intent']={'goal':'later confirmed direction'}
                    state['intent_hash']=bx.digest(json.dumps(state['intent'],sort_keys=True,ensure_ascii=False,allow_nan=False));poe.write_json(state_path,state)
                elif change=='original':
                    original=self.folder/'run/original.xml';poe.write_text(original,SAMPLE.replace('level="86"','level="87"'))
                else:
                    template=self.folder/'real-template.xml';poe.write_text(template,SAMPLE)
                    saved=poe.read_json(request.proposal_file)
                    if change=='route-template':saved['checked_route']['stages']=[{'calculation':{'input_artifacts':{str(template):bx.digest(template.read_bytes())}}}]
                    else:saved['input_artifacts']={str(template):bx.digest(template.read_bytes())}
                    poe.write_json(request.proposal_file,saved)
                    state=poe.read_json(state_path)
                    if change=='route-template':state['files'][request.proposal_file]=bx.digest(Path(request.proposal_file).read_bytes())
                    else:state['candidates']['native']['evaluation_sha256']=bx.digest(Path(request.proposal_file).read_bytes())
                    poe.write_json(state_path,state)
                    poe.write_text(template,SAMPLE.replace('level="86"','level="87"'))
                with self.assertRaises(bx.ContractError):poe.edit_document(request)
                self.assertEqual(self.source.read_bytes(),before)

    def test_edit_receipt_lists_current_input_fields_and_their_owners(self):
        result=self.edit({"set":{"summary":"draft","mode":"refine"}},kind="plan")
        contract=result["input_contract"]
        self.assertEqual(contract["top_level_fields"],sorted(bx.PLAN_FIELDS))
        self.assertEqual(contract["field_owners"]["objectives"],["intent","route"])
        self.assertEqual(poe.read_json(result["receipt_file"])["input_contract"],contract)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.folder=Path(self.temp.name)
        self.source=self.folder/"route.json";self.request=self.folder/"change.json"
        self.root_patch=patch.object(engine,"resolve_root",return_value=self.folder);self.root_patch.start();self.addCleanup(self.root_patch.stop)
        self.engine_patch=patch.object(engine,"fingerprint",return_value="fixture");self.engine_patch.start();self.addCleanup(self.engine_patch.stop)

    def edit(self, change, revision=None, kind="route"):
        poe.write_json(self.request,change)
        return poe.edit_document(args(kind=kind,file=str(self.source),changes=str(self.request),if_sha256=revision,invoked=True))

    def test_public_edit_rejects_present_invalid_values_without_writing_or_calculating(self):
        route_values=[({"game_version":"3.29 explanatory prose"},"/game_version"),
                      ({"stages":[{"stage_id":"s","skill_groups":[{"gems":[{"skill_id":[]}]}]}]},"/stages/0/skill_groups/0/gems/0/skill_id"),
                      ({"stages":[{"stage_id":"s","skill_groups":[{"enabled":"true","gems":[]}]}]},"/stages/0/skill_groups/0/enabled")]
        for field,value in [("level",True),("level",-1),("quality","20")]:
            route_values.append(({"stages":[{"stage_id":"s","skill_groups":[{"gems":[{"skill_id":"FutureSkill",field:value}]}]}]},f"/stages/0/skill_groups/0/gems/0/{field}"))
        plan_values=[({"mode":[]},"/mode"),({"operations":{"weapon_set":3}},"/operations/weapon_set"),
                     ({"operations":{"tree":{"unknown":1}}},"/operations/tree"),
                     ({"operations":{"tree":{"nodes":[1],"nodes_add":[2]}}},"/operations/tree"),
                     ({"operations":{"tree":{"nodes_add":[1,1]}}},"/operations/tree/nodes_add"),
                     ({"operations":{"items":[{"id":1,"craft":{"base":"future","affixes":[{"id":"future","roll":2}]}}]}},"/operations/items/0/craft/affixes/0/roll")]
        for kind,variants in (("route",route_values),("plan",plan_values)):
            for index,(body,location) in enumerate(variants):
                with self.subTest(kind=kind,body=body),patch.object(engine,"calculate") as worker:
                    self.source=self.folder/f"invalid-{kind}-{index}.json"
                    first=self.edit({"set":{"name":"saved"}},kind=kind)
                    before={p:p.read_bytes() for p in (self.source,self.source.with_suffix(".edit.json"))}
                    poe.write_json(self.request,{"set":body})
                    stderr=io.StringIO()
                    with redirect_stderr(stderr),redirect_stdout(io.StringIO()):
                        code=poe.main(["edit","--kind",kind,"--file",str(self.source),"--changes",str(self.request),"--if-sha256",first["revision"],"--invoked"])
                    self.assertEqual(code,2);error=json.loads(stderr.getvalue())["diagnostic"]
                    self.assertEqual(error["code"],"invalid_input");self.assertEqual(error["location"],location)
                    self.assertIn("expected",error);self.assertIn("actual",error)
                    self.assertEqual({p:p.read_bytes() for p in before},before);worker.assert_not_called()

    def test_replace_validates_duplicate_collection_identifiers(self):
        for collection,identifier in (("objectives","objective_id"),("evidence","evidence_id")):
            with self.subTest(collection=collection):
                self.source=self.folder/(collection+".json")
                original=[{identifier:"a"},{identifier:"b"}]
                if collection=="objectives":
                    for row in original: row["text"]="objective"
                else:
                    for row in original: row.update(kind="user_statement",subject="subject")
                first=self.edit({"set":{collection:original}})
                before={p:p.read_bytes() for p in (self.source,self.source.with_suffix(".edit.json"))}
                duplicate=copy.deepcopy(original);duplicate[1][identifier]="a"
                with self.assertRaises(bx.ContractError) as error:
                    self.edit({"replace":{"/"+collection:duplicate}},first["revision"])
                self.assertEqual(error.exception.diagnostic["location"],f"/{collection}/1/{identifier}")
                self.assertEqual({p:p.read_bytes() for p in before},before)

    def test_route_output_roles_cannot_alias_input_before_any_write(self):
        for suffix in (".state.json",".evidence.json",".review.json"):
            with self.subTest(suffix=suffix),patch.object(poe,"calculate_route") as worker:
                folder=self.folder/suffix;folder.mkdir()
                source=folder/("report"+suffix);poe.write_json(source,{"schema_version":2,"notes":"retain"})
                before=source.read_bytes()
                with self.assertRaises(bx.ContractError) as error:
                    poe.route_command(args(file=str(source),out=str(folder/"report.html"),invoked=True))
                self.assertEqual(error.exception.diagnostic["code"],"path_conflict")
                self.assertEqual(source.read_bytes(),before);worker.assert_not_called()
                if suffix!=".state.json": self.assertFalse((folder/"report.state.json").exists())

    def test_edit_rejects_merged_input_size_before_committing(self):
        first=self.edit({"set":{"notes":"a"*7_300_000}})
        before={p:p.read_bytes() for p in (self.source,self.source.with_suffix(".edit.json"))}
        with self.assertRaises(bx.ContractError) as error:
            self.edit({"set":{"risks":"b"*1_400_000}},first["revision"])
        self.assertEqual(error.exception.diagnostic["code"],"input_too_large")
        self.assertEqual({p:p.read_bytes() for p in before},before)

    def test_batch_remove_addresses_one_common_pre_removal_document(self):
        first=self.edit({"set":{"stages":[{"stage_id":s} for s in ("zero","one","two")]}})
        self.edit({"remove":["/stages/0","/stages/1"]},first["revision"])
        self.assertEqual([s["stage_id"] for s in poe.read_json(self.source)["stages"]],["two"])

    def test_partial_unknown_drafts_remain_valid_and_shape_diagnostics_are_shared(self):
        self.edit({"set":{"game_version":None,"ascendancy":None,"stages":[{"stage_id":"later","level":None,"skill_groups":[{"gems":[{"skill_id":"future"}]}]}]}})
        self.assertIsNone(poe.read_json(self.source)["game_version"])
        engine.validate_game_version(None,"unknown","current")
        for complete in (False,True):
            with self.subTest(complete=complete),self.assertRaises(bx.ContractError) as caught:
                bx.plan_shape({"mode":"refine","summary":"purpose","operations":{"gems":[{"group":1,"index":1,"skill_id":[]}]}},complete=complete)
            self.assertEqual(caught.exception.diagnostic["location"],"/operations/gems/0/skill_id")
        with self.assertRaises(bx.ContractError) as caught: routes.link_queries({"schema_version":2,"stages":[{"stage_id":"s","skill_groups":[{"gems":[{"skill_id":[]}]}]}]})
        self.assertEqual(caught.exception.diagnostic["location"],"/stages/0/skill_groups/0/gems/0/skill_id")

    def test_nested_route_template_output_alias_is_rejected_before_worker_and_state(self):
        out=self.folder/"nested.html";sid="a";directory=poe.stage_directory(out,sid)
        for filename in ("snapshot.xml","snapshot.json","evaluation.json"):
            with self.subTest(filename=filename),patch.object(poe,"calculate_route") as worker:
                template=directory/filename;poe.write_text(template,"retain original")
                poe.write_json(self.source,{"schema_version":2,"stages":[{"stage_id":sid,"snapshot":{"template_xml":str(template)}}]})
                with self.assertRaises(bx.ContractError) as caught:poe.route_command(args(file=str(self.source),out=str(out),invoked=True))
                self.assertEqual(caught.exception.diagnostic["code"],"path_conflict");self.assertEqual(template.read_text(),"retain original")
                self.assertFalse(out.with_suffix(".state.json").exists());worker.assert_not_called()

    def test_evaluation_output_alias_preserves_source_state_and_original(self):
        run=self.folder/"run";run.mkdir();source=run/"baseline.json"
        poe.write_json(source,{"schema_version":2,"mode":"refine","summary":"purpose"})
        poe.write_json(run/"run.json",{"retained":"state"});poe.write_text(run/"original.xml",SAMPLE)
        before={p:p.read_bytes() for p in (source,run/"run.json",run/"original.xml")}
        with patch.object(poe,"read_run",return_value=(run,{"intent":{"confirmed":True}})),patch.object(engine,"calculate") as worker,self.assertRaises(bx.ContractError) as caught:
            poe.evaluate(args(run=str(run),name="test",plan=str(source)))
        self.assertEqual(caught.exception.diagnostic["code"],"path_conflict");self.assertEqual({p:p.read_bytes() for p in before},before);worker.assert_not_called()

    def test_view_oversized_single_record_has_recursive_child_continuation(self):
        content={"a/b~c":"字"*13000};poe.write_json(self.source,{"rows":[content,{"small":False}]})
        page=poe.view_json(args(file=str(self.source),pointer="/rows",fields="",keys=False,query="",limit=100,offset=0))
        self.assertEqual(page["deferred_values"],["/rows/0"]);self.assertEqual(page["next_offset"],1)
        child=poe.view_json(args(file=str(self.source),pointer="/rows/0",fields="",keys=False,query="",limit=100,offset=0))
        self.assertEqual(child["deferred_values"],["/rows/0/a~1b~0c"])
        text=poe.view_json(args(file=str(self.source),pointer=child["deferred_values"][0],fields="",keys=False,query="",limit=4000,offset=0))
        self.assertEqual(text["value"],"字"*4000);self.assertEqual(text["next_offset"],4000)

    def test_public_view_pages_long_string_and_includes_bounded_envelope(self):
        expected='中文"\\\n'*4_000
        poe.write_json(self.source,{"notes":expected})
        offset=0;parts=[]
        for _ in range(len(expected)+1):
            stdout=io.StringIO()
            with redirect_stdout(stdout):
                code=poe.main(["view","--file",str(self.source),"--pointer","/notes","--limit","100","--offset",str(offset)])
            self.assertEqual(code,0);self.assertLessEqual(len(stdout.getvalue()),12000)
            page=json.loads(stdout.getvalue());self.assertIsInstance(page["value"],str);parts.append(page["value"])
            if page["next_offset"] is None: break
            self.assertGreater(page["next_offset"],offset);offset=page["next_offset"]
        self.assertEqual("".join(parts),expected)

    def test_public_edit_json_type_domains_preserve_last_valid_source_and_receipt(self):
        first=self.edit({"set":{"name":"last valid"}},kind="plan")
        before={p:p.read_bytes() for p in (self.source,self.source.with_suffix(".edit.json"))}
        domains={"skill_id":[None,True,0,[],{},""],"level":[None,True,0,-1,"1",[],{}],"quality":[None,True,-1,"0",[],{}],
                 "enabled":[None,0,1,"true",[],{}],"count":[None,True,0,"1",[],{}],"skill_part":[None,True,0,"1",[],{}]}
        for field,values in domains.items():
            for value in values:
                with self.subTest(field=field,value=value),patch.object(engine,"calculate") as worker:
                    change={"set":{"operations":{"gems":[{"group":1,"index":1,field:value}]}}}
                    poe.write_json(self.request,change);stderr=io.StringIO()
                    with redirect_stderr(stderr),redirect_stdout(io.StringIO()):
                        code=poe.main(["edit","--kind","plan","--file",str(self.source),"--changes",str(self.request),"--if-sha256",first["revision"],"--invoked"])
                    self.assertEqual(code,2);diagnostic=json.loads(stderr.getvalue())["diagnostic"]
                    self.assertEqual(diagnostic["location"],"/operations/gems/0/"+field)
                    self.assertEqual(diagnostic["actual"],value);self.assertEqual(diagnostic["input_revision"],first["revision"])
                    self.assertFalse(diagnostic["retryable"]);worker.assert_not_called()
                    self.assertEqual({p:p.read_bytes() for p in before},before)

    def test_public_edit_receipt_io_failure_reports_the_actual_commit(self):
        first=self.edit({"set":{"name":"before"}});poe.write_json(self.request,{"set":{"name":"after"}})
        write=poe.write_json
        def failure(path,value):
            if Path(path)==self.source.with_suffix(".edit.json"): raise OSError("receipt failed")
            write(path,value)
        stderr=io.StringIO()
        with patch.object(poe,"write_json",side_effect=failure),redirect_stderr(stderr):
            code=poe.main(["edit","--kind","route","--file",str(self.source),"--changes",str(self.request),"--if-sha256",first["revision"],"--invoked"])
        self.assertEqual(code,2);diagnostic=json.loads(stderr.getvalue())["diagnostic"]
        self.assertTrue(diagnostic["source_committed"]);self.assertFalse(diagnostic["receipt_committed"])
        self.assertFalse(diagnostic["retryable"]);self.assertTrue(diagnostic["recoverable"])
        self.assertEqual(diagnostic["current_revision"],bx.digest(self.source.read_bytes()))
        self.assertEqual(poe.read_json(self.source)["name"],"after")
        self.assertEqual(poe.read_json(self.source.with_suffix(".edit.json"))["revision"],first["revision"])
        self.edit({"set":{"name":"recovered"}},diagnostic["current_revision"])

    def test_doctor_preserves_native_capabilities_and_exposes_adapter_boundaries(self):
        stdout=io.StringIO()
        with patch.object(engine,"health",return_value={"capabilities":{"observed_native_feature":True},"health_probe":{"roundtrip":True}}),redirect_stdout(stdout):
            code=poe.main(["doctor"])
        self.assertEqual(code,0);result=json.loads(stdout.getvalue())
        self.assertTrue(result["capabilities"]["observed_native_feature"])
        self.assertEqual(result["capabilities"]["timeline"],resources.capabilities()["timeline"])
        self.assertEqual(result["delivery_statuses"],["blocked","draft","scoped_result"])
        self.assertEqual(result["entrypoint"]["script"],str(Path(poe.__file__).resolve()))
        self.assertEqual(result["entrypoint"]["python"],str(Path(sys.executable).resolve()))
        self.assertEqual(result["entrypoint"]["skill_version"],poe.VERSION)

    def test_save_direction_then_increment_stage_with_generated_handles_and_retry(self):
        first=self.edit({"set":{"name":"Provisional direction","goal":"user direction"}})
        self.assertEqual(first["status"],"saved_draft");self.assertNotIn("stages",poe.read_json(self.source))
        request={"set":{"stages":[{"name":"Early","skill_groups":[{"name":"main","slot":"","use":"manual","gems":[{"skill_id":"FutureSkill","level":1}]}]}]}}
        second=self.edit(request,first["revision"]);saved=poe.read_json(self.source);gem=saved["stages"][0]["skill_groups"][0]["gems"][0]
        self.assertTrue(saved["stages"][0]["stage_id"]);self.assertTrue(gem["instance_id"])
        self.assertEqual(second["stage_instances"][0]["instances"][0]["instance_id"],gem["instance_id"])
        receipt=poe.read_json(self.source.with_suffix(".edit.json"))
        receipt["entrypoint"]={"script":"previous location","python":"previous runtime"}
        poe.write_json(self.source.with_suffix(".edit.json"),receipt)
        replay=self.edit(request,first["revision"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["entrypoint"]["script"],str(Path(poe.__file__).resolve()))
        self.assertEqual(poe.read_json(self.source.with_suffix(".edit.json"))["entrypoint"],replay["entrypoint"])
        self.assertEqual(poe.read_json(self.source),saved)
        self.assertEqual(routes.gem_ids(saved),["FutureSkill"])

    def test_partial_claim_can_be_saved_but_requires_completion_before_assessment(self):
        for kind in ("route","plan"):
            for body in ({"claim_id":"partial"},{"claim_id":"partial","subjects":["goal"]}):
                with self.subTest(kind=kind,body=body),patch.object(engine,"calculate") as native:
                    self.source=self.folder/(kind+("-subject" if "subjects" in body else "-id")+".json")
                    first=self.edit({"set":{"claims":[body]}},kind=kind)
                    document=poe.read_json(self.source)
                    self.assertEqual(document["claims"][0]["claim_id"],"partial")
                    self.assertNotIn("text",document["claims"][0])
                    with self.assertRaises(bx.ContractError) as error:
                        facts.assess_claims(document["claims"],{},specification=document,engine_fingerprint="fixture")
                    self.assertEqual(error.exception.diagnostic["location"],"/claims/0/text")
                    self.edit({"set":{"claims":[{"claim_id":"partial","text":"Current explanation","subjects":["goal"],"evidence_refs":["basis"]}],
                                      "evidence":[{"evidence_id":"basis","kind":"user_statement","subject":"goal","text":"Actual user direction"}]}},
                              first["revision"],kind=kind)
                    completed=poe.read_json(self.source)
                    rows=facts.assess_claims(completed["claims"],{"basis":{"status":"user_declared"}},
                                             specification=completed,engine_fingerprint="fixture")
                    self.assertEqual(rows[0]["status"],"model_assessed");native.assert_not_called()

    def test_record_patch_retains_other_stages_null_is_data_and_remove_is_explicit(self):
        first=self.edit({"set":{"stages":[{"stage_id":"early","name":"Early"},{"stage_id":"later","name":"Later"}],"allow_mule":False}})
        second=self.edit({"set":{"stages":[{"stage_id":"early","level":None}]}},first["revision"])
        value=poe.read_json(self.source);self.assertEqual(len(value["stages"]),2);self.assertIsNone(value["stages"][0]["level"])
        self.edit({"set":{},"remove":["/allow_mule"]},second["revision"])
        self.assertNotIn("allow_mule",poe.read_json(self.source));self.assertIsNone(poe.read_json(self.source)["stages"][0]["level"])

    def test_nested_evidence_failure_does_not_commit_or_request_native_calculation(self):
        for kind in ("route","plan"):
            with self.subTest(kind=kind):
                self.source=self.folder/(kind+".json")
                first=self.edit({"set":{"name":"original"}},kind=kind);before=self.source.read_bytes()
                with patch.object(engine,"calculate") as native,self.assertRaises(bx.ContractError) as error:
                    self.edit({"set":{"evidence":[{"evidence_id":"e","future_bad_field":True}]}},first["revision"],kind=kind)
                self.assertEqual(error.exception.diagnostic["location"],"/evidence/0")
                native.assert_not_called();self.assertEqual(self.source.read_bytes(),before)

    def test_catalog_batch_failure_identifies_query_and_field_before_native_call(self):
        for query,location in (({"kind":"classes","class_id":2},"/queries/0"),({"kind":"gems","limit":False},"/queries/0/limit"),({"kind":"metrics","offset":-1},"/queries/0/offset")):
            poe.write_json(self.request,{"queries":[query]})
            with self.subTest(query=query),patch.object(engine,"calculate") as native,self.assertRaises(bx.ContractError) as error:
                poe.catalog(args(kind=None,input=None,batch=str(self.request),class_id=None))
            self.assertEqual(error.exception.diagnostic["location"],location)
            self.assertIn("expected",error.exception.diagnostic);self.assertIn("actual",error.exception.diagnostic);native.assert_not_called()

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
        self.assertEqual(state["entrypoint"]["script"],str(Path(poe.__file__).resolve()))
        self.assertEqual(status["entrypoint"],state["entrypoint"])

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
        with patch.object(poe,"write_json",side_effect=fail_receipt),self.assertRaises(bx.ContractError) as caught:self.edit({"set":{"name":"after"}},first["revision"])
        diagnostic=caught.exception.diagnostic
        self.assertEqual(diagnostic["code"],"io_error");self.assertTrue(diagnostic["source_committed"]);self.assertFalse(diagnostic["receipt_committed"])
        view=poe.view_json(args(file=str(self.source),pointer="",fields="name",keys=False,query="",limit=10,offset=0))
        self.assertEqual(view["value"]["name"],"after");self.assertNotEqual(view["revision"],first["revision"])
        self.assertEqual(diagnostic["current_revision"],view["revision"])
        self.edit({"set":{"name":"recovered"}},view["revision"])
        self.assertEqual(poe.read_json(self.source)["name"],"recovered")

    def test_public_route_large_stage_summary_has_reachable_full_continuation(self):
        # Interface simulation: input passes the actual route validator; no claim
        # that eighty native snapshots or a platform truncation was observed.
        fixture=RouteTests();fixture.setUp();template=copy.deepcopy(fixture.route["stages"][-1])
        fixture.route["stages"]=[];fixture.equipment={}
        for index in range(80):
            stage=copy.deepcopy(template);stage["stage_id"]=f"phase-{index:03d}";stage["name"]="Independently selected stage "+str(index)
            fixture.route["stages"].append(stage)
            fixture.equipment[stage["stage_id"]]={"bases":copy.deepcopy(stage["bases"]),"checks":{"Gloves":{"base":"Iron Gauntlets","valid_for_slot":True}},"context":{"basis":"declared_base_layout"}}
        fixture.check()
        poe.write_json(self.source,fixture.route);out=self.folder/"large.html";details=out.with_suffix(".evidence.json")
        payload={"status":"draft","file_exported":True,"stages":80,
                 "stage_summary":[{"stage_id":s["stage_id"],"name":s["name"],"status":"draft","native_calculated":True,"errors_total":0,"pending_total":1} for s in fixture.route["stages"]],
                 "next_actions":[{"action":"edit_input","location":f"/stages/{i}/snapshot"} for i in range(80)]}
        def simulate(_):
            poe.write_json(details,{"response":payload,"stage_summary":payload["stage_summary"],"next_actions":payload["next_actions"]})
            return copy.deepcopy(payload)
        stdout=io.StringIO()
        with patch.object(poe,"calculate_route",side_effect=simulate),redirect_stdout(stdout):
            code=poe.main(["route","--file",str(self.source),"--out",str(out),"--invoked"])
        self.assertEqual(code,0);self.assertLessEqual(len(stdout.getvalue()),12000)
        response=json.loads(stdout.getvalue())
        links=response.get("continuations",[])+response.get("next_actions",[])
        continuation=next(row for row in links if row.get("action")=="view" and row.get("pointer","").endswith("/stage_summary"))
        rows=[];offset=0
        while True:
            page=poe.view_json(args(file=continuation["file"],pointer=continuation["pointer"],fields="",keys=False,query="",limit=10,offset=offset))
            rows.extend(page["value"])
            if page["next_offset"] is None: break
            offset=page["next_offset"]
        self.assertEqual(rows,payload["stage_summary"])

    def test_large_edit_reply_points_to_saved_full_ids_instead_of_tool_truncation(self):
        changes={"set":{"stages":[{"stage_id":f"stage-{i}","skill_groups":[{"gems":[{"skill_id":"Future","level":1}]}]} for i in range(200)]}}
        poe.write_json(self.request,changes);stdout=io.StringIO()
        with redirect_stdout(stdout):code=poe.main(["edit","--invoked","--kind","route","--file",str(self.source),"--changes",str(self.request)])
        self.assertEqual(code,0);reply=json.loads(stdout.getvalue());self.assertLess(len(stdout.getvalue()),12000)
        saved=poe.read_json(reply["receipt_file"]);self.assertEqual(len(saved["stage_instances"]),200)
        self.assertEqual(reply["next_actions"][1]["pointer"],"/stage_instances")
        self.assertEqual(reply["entrypoint"],saved["entrypoint"])

    def test_partial_array_replacement_is_rejected_without_mutation(self):
        first=self.edit({"set":{"stages":[{"stage_id":"a","skill_groups":[{"name":"first","gems":[]},{"name":"second","gems":[]}]}]}})
        before=self.source.read_bytes()
        with self.assertRaises(bx.ContractError) as error:
            self.edit({"set":{"stages":[{"stage_id":"a","skill_groups":[{"name":"first","gems":[]}]}]}},first["revision"])
        self.assertEqual(error.exception.diagnostic["code"],"explicit_replacement_required")
        self.assertEqual(self.source.read_bytes(),before)

    def test_pointer_replacement_preserves_every_other_field_and_counts_are_actual(self):
        first=self.edit({"set":{"stages":[{"stage_id":"a","skill_groups":[{"name":"first","use":"old","gems":[{"skill_id":"Future","level":1}]},{"name":"second","gems":[]}]}]}})
        expected=poe.read_json(self.source);expected["stages"][0]["skill_groups"][0]["use"]="new"
        result=self.edit({"replace":{"/stages/0/skill_groups/0/use":"new"}},first["revision"])
        self.assertEqual(poe.read_json(self.source),expected)
        self.assertEqual(result["counts"]["skill_groups"],2)
        self.assertEqual(result["counts"]["gems"],1)
        self.assertEqual(result["actual_changes"],[{"pointer":"/stages/0/skill_groups/0/use","before":"old","after":"new"}])
        self.assertEqual(result["artifacts"]["temporary"],[])
        self.assertEqual(result["artifacts"]["persistent_state"][0]["path"],str(self.source.with_suffix(".edit.json")))

    def test_nested_mapping_type_fails_before_native_query_or_save(self):
        first=self.edit({"set":{"name":"original"}});before=self.source.read_bytes()
        with patch.object(poe,"native_queries") as query,self.assertRaises(bx.ContractError) as error:
            self.edit({"set":{"constraint_sources":{"/goal":{"evidence_id":"u"}}}},first["revision"])
        query.assert_not_called();self.assertEqual(self.source.read_bytes(),before)
        self.assertEqual(error.exception.diagnostic["location"],"/constraint_sources/~1goal")

    def test_unrelated_stage_change_preserves_binding_and_related_change_invalidates(self):
        first=self.edit({"set":{"stages":[{"stage_id":"a","name":"first"},{"stage_id":"b","name":"second"}],"evidence":[{"evidence_id":"u","kind":"user_statement","subject":"goal","text":"actual user request"}],"claims":[{"claim_id":"c","text":"stage explanation","subjects":["stage:a"],"evidence_refs":["u"]}]}})
        second=self.edit({"set":{"stages":[{"stage_id":"b","name":"unrelated change"}]}},first["revision"])
        self.assertEqual(second["invalidated_claims"],[])
        third=self.edit({"set":{"stages":[{"stage_id":"a","name":"related change"}]}},second["revision"])
        self.assertEqual(third["invalidated_claims"],["c"])
        spec=poe.read_json(self.source)
        checked=facts.assess_claims(spec["claims"],{"u":{"status":"user_declared"}},specification=spec,engine_fingerprint="fixture")
        self.assertEqual(checked[0]["status"],"unresolved")
        refreshed=self.edit({"review_claims":["c"]},third["revision"])
        self.assertEqual(refreshed["invalidated_claims"],[])

    def test_review_submission_cannot_mix_input_changes(self):
        first=self.edit({"set":{"assessment":{"unresolved":[]}}});before=self.source.read_bytes()
        with self.assertRaises(bx.ContractError) as error:
            self.edit({"set":{"assessment":{"unresolved":["new"],"review":{"token":"old","findings":"reviewed"}}}},first["revision"])
        self.assertEqual(error.exception.diagnostic["code"],"review_transaction_mixed")
        self.assertEqual(self.source.read_bytes(),before)

    def test_comparison_declaration_requires_checks_before_save(self):
        first=self.edit({"set":{"name":"draft"}});before=self.source.read_bytes()
        with self.assertRaises(bx.ContractError) as error:
            self.edit({"set":{"claims":[{"claim_id":"c","claim_type":"comparison","text":"target met","subjects":["goal"],"evidence_refs":["native:candidate#/stats/NewMetric"]}]}},first["revision"])
        self.assertEqual(error.exception.diagnostic["location"],"/claims/0/checks")
        self.assertEqual(self.source.read_bytes(),before)

    def test_current_comparison_is_refuted_and_report_exposes_measurement_scope(self):
        ref="native:candidate#/stats/NewMetric"
        first=self.edit({"set":{"claims":[{"claim_id":"c","claim_type":"comparison","text":"target met","subjects":["goal"],"checks":[{"reference":ref,"op":"ge","value":4}]}]}})
        document=poe.read_json(self.source)
        rows=facts.assess_claims(document["claims"],{}, {"candidate":{"stats":{"NewMetric":3}}},specification=document,engine_fingerprint="fixture")
        self.assertEqual(rows[0]["status"],"unresolved");self.assertEqual(rows[0]["check_status"],"refuted")
        published=poe.publication("draft",[],["claim_check:c"],{"status":"unknown"},{},claims=rows)
        self.assertEqual(published["declared_checks"][0]["actual"],3)
        self.assertEqual(published["declared_checks"][0]["status"],"refuted")
        self.assertIn("flask",published["capabilities"]["timeline"]["excluded_recovery"])

    def test_corrupt_receipt_recovers_from_current_source_revision(self):
        first=self.edit({"set":{"name":"before"}})
        self.source.with_suffix(".edit.json").write_text("incomplete JSON",encoding="utf-8")
        result=self.edit({"set":{"name":"after"}},first["revision"])
        self.assertEqual(poe.read_json(self.source)["name"],"after")
        self.assertEqual(poe.read_json(self.source.with_suffix(".edit.json"))["revision"],result["revision"])

    def test_configuration_review_uses_actual_reference_and_current_judgment(self):
        ref="native:stage:a#/config_values"
        claims=[{"claim_id":"c","status":"model_assessed","subjects":["configuration:stage:a"],"evidence_refs":[ref]}]
        self.assertTrue(facts.configuration_reviewed(claims,"stage:a"))
        self.assertFalse(facts.configuration_reviewed(claims,"stage:b"))
        claims[0]["status"]="unresolved"
        self.assertFalse(facts.configuration_reviewed(claims,"stage:a"))

    def test_related_evidence_and_engine_changes_invalidate_judgment(self):
        first=self.edit({"set":{"evidence":[{"evidence_id":"a","kind":"user_statement","subject":"goal","text":"first"},{"evidence_id":"b","kind":"user_statement","subject":"goal","text":"second"}],"claims":[{"claim_id":"c","text":"explanation","subjects":["goal"],"evidence_refs":["a"]}]}})
        second=self.edit({"set":{"evidence":[{"evidence_id":"b","text":"unrelated"}]}},first["revision"])
        self.assertEqual(second["invalidated_claims"],[])
        third=self.edit({"set":{"evidence":[{"evidence_id":"a","text":"related"}]}},second["revision"])
        self.assertEqual(third["invalidated_claims"],["c"])
        fourth=self.edit({"review_claims":["c"]},third["revision"])
        with patch.object(engine,"fingerprint",return_value="changed-native"):
            self.assertEqual(self.edit({"set":{}},fourth["revision"])["invalidated_claims"],["c"])

    def test_idempotent_reply_cannot_reuse_older_engine_context(self):
        request={"set":{"name":"first"}}
        first=self.edit(request)
        with patch.object(engine,"fingerprint",return_value="changed-native"),self.assertRaises(bx.ContractError):
            self.edit(request,"older revision")
        self.assertEqual(poe.read_json(self.source)["name"],"first")

    def test_bool_to_number_edit_is_reported_and_array_change_requires_replace(self):
        first=self.edit({"set":{"operations":{"config":{"observed":False}}}},kind="plan")
        result=self.edit({"set":{"operations":{"config":{"observed":0}}}},first["revision"],kind="plan")
        self.assertEqual(result["actual_changes"],[{"pointer":"/operations/config/observed","before":False,"after":0}])
        with self.assertRaises(bx.ContractError): poe.merge_edit({"notes":[{"value":False}]},{"notes":[{"value":0}]})
        with self.assertRaises(bx.ContractError) as error:
            self.edit({"set":{"operations":{"config":{"observed":False}},"assessment":{"review":{"token":"stale","findings":"reviewed"}}}},result["revision"],kind="plan")
        self.assertEqual(error.exception.diagnostic["code"],"review_transaction_mixed")

    def test_claim_target_type_change_rebinds_current_stage(self):
        first=self.edit({"set":{"stages":[{"stage_id":"a","name":"first"}],"claims":[{"claim_id":"c","subjects":["stage:a"],"text":"interpretation","checks":[{"reference":"native:stage:a#/stats/Observed","op":"eq","value":False}]}]}})
        second=self.edit({"set":{"stages":[{"stage_id":"a","name":"changed"}]}},first["revision"])
        self.assertEqual(second["invalidated_claims"],["c"])
        third=self.edit({"replace":{"/claims/0/checks/0/value":0}},second["revision"])
        self.assertEqual(third["invalidated_claims"],[])

    def test_bad_usage_shape_preserves_source_and_receipt(self):
        first=self.edit({"set":{"name":"draft"}},kind="plan")
        before=self.source.read_bytes();receipt=self.source.with_suffix(".edit.json").read_bytes()
        with self.assertRaises(bx.ContractError) as error:
            self.edit({"set":{"usage":[{"skill_ref":"observed","casts_per_second":True}]}},first["revision"],kind="plan")
        self.assertEqual(error.exception.diagnostic["location"],"/usage/0/casts_per_second")
        self.assertEqual(self.source.read_bytes(),before);self.assertEqual(self.source.with_suffix(".edit.json").read_bytes(),receipt)


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
        current=engine.calculate(cls.pob,bx.serialise(cls.original))
        cls.intent = intent_for(cls.original,game_version=engine.require_data_version(engine.metadata(cls.pob)),normal_points=current['points']+10,ascendancy_points=current['ascendancy_points']+2)
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
        initial=poe.read_json(self.run_dir/"run.json")
        self.assertEqual(initial["entrypoint"]["script"],str(Path(poe.__file__).resolve()))
        initial["last_operation"]={"command":"evaluate","candidate":"refine","status":"blocked","reason":"Prior input failure"}
        poe.write_json(self.run_dir/"run.json",initial)
        result = self.evaluate_plan(self.refine, "refine")
        self.assertEqual(result["status"], "draft", result["errors"])
        self.assertTrue(result["roundtrip_verified"])
        self.assertTrue(any("negative" in warning for warning in result["baseline_warnings"]))
        self.assertNotEqual(result["comparison"]["CombinedDPS"]["delta"], 0)
        self.assertIsNone(result["comparison"]["CombinedDPS"]["percent"])
        operation=poe.read_json(self.run_dir/"run.json")["last_operation"]
        self.assertEqual(operation["status"],result["status"]);self.assertNotIn("reason",operation)
        output = poe.export(args(run=str(self.run_dir), name="refine"))
        operation=poe.read_json(self.run_dir/"run.json")["last_operation"]
        self.assertEqual(operation["command"],"export");self.assertEqual(operation["status"],output["status"])
        self.assertTrue(operation["file_exported"]);self.assertNotIn("reason",operation)
        for path in output["files"]: self.assertTrue(Path(path).is_file())
        final=poe.read_json(self.run_dir/"output/refine/final-assessment.json")
        self.assertTrue(final["comparison_notes"])
        self.assertIsNone(final["comparison"]["CombinedDPS"]["percent"])
        self.assertEqual(output["publication"]["statements"][1]["status"],"unknown")
        evidence = poe.read_json(self.run_dir / "candidates/refine/evaluation.json")
        self.assertEqual(set(evidence["reference_contract"]),{"baseline","candidate","budget","resources"})
        self.assertEqual(evidence["reference_contract"]["resources"]["fields"]["single_payments"],"native:resources#/single_payments")
        self.assertEqual(evidence["reference_contract"]["resources"]["origin"],"program_assessment")
        scene=next(d for d in evidence["diagnostics"] if d["code"]=="configuration_review_missing")
        self.assertEqual(scene["actual"],evidence["result"]["config_values"])
        self.assertEqual(scene["required_subject"],"configuration:candidate")
        self.assertTrue(any(a["action"]=="view" and a["pointer"]=="/reference_contract" for a in result["next_actions"]))
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
        result=engine.calculate(self.pob,catalog='nodes',query='',limit=10000)
        entries=result['catalog']
        ordinary=next(node for node in entries if node.get('anoint_recipe'))
        repeated=next(node for node in entries if node.get('anoint_recipe') and len(node['anoint_recipe'])>len({part['id'] for part in node['anoint_recipe']}))
        no_recipe=next(node for node in entries if node['type']=='Normal' and not node.get('anoint_recipe'))
        native_tree = (self.pob / "TreeData" / result["tree_version"] / "tree.lua").read_text(encoding="utf-8-sig")
        native_tree=native_tree[re.search(r'^    \["nodes"\]\s*=\s*\{',native_tree,re.M).end():]
        for node in (ordinary,repeated):
            stanza = re.search(r"\[" + str(node["id"]) + r"\]\s*=\s*\{.*?\[\"recipe\"\]\s*=\s*\{([^}]+)\}", native_tree, re.S)
            self.assertIsNotNone(stanza)
            self.assertEqual([part["id"] for part in node["anoint_recipe"]], re.findall(r'"([^\"]+)"', stanza[1]))
            self.assertTrue(all(part.get("name_cn") and part.get("display_name") for part in node["anoint_recipe"]))
        oils = repeated['anoint_recipe']
        self.assertGreater(len(oils), len({part["id"] for part in oils}))
        self.assertNotIn('anoint_recipe',no_recipe)

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

    def test_10_native_matching_quality_follows_current_socket_rule(self):
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
        self.assertEqual(saved["data_version"],engine.metadata(self.pob)['data_version'])
        bonus=saved['matching_socket_quality_bonus']
        self.assertEqual(bonus,engine.calculate(self.pob,catalog='rules')['matching_socket_quality_bonus'])
        for gem in saved["gems"]:
            if gem["slot"] == "Boots": self.assertEqual(gem["socket_quality_bonus"], 0)
        standard = engine.calculate(self.pob, bx.serialise(bx.apply_plan(poe.prepared(self.original, self.intent), self.refine)))
        self.assertTrue(any(gem["socket_quality_bonus"] == bonus for gem in standard["gems"] if gem["slot"] == "Boots" and gem['matches_socket']))
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
        self.assertEqual(saved["result"]["ascendancy_points"],self.intent['ascendancy_points'])

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
        for skill_id in ('ShieldCrush','Sunder'):
            self.assertEqual(gems[skill_id]['min_level'],min(row['character_level'] for row in gems[skill_id]['level_requirements']))
        documents=[];expected=[]
        for skill_id in ('Determination','Grace','Discipline','Hatred'):
            row=next(row for row in reversed(gems[skill_id]['level_requirements']) if row.get('naturally_trainable'))
            seed=engine.calculate(self.pob,class_id=self.native['classes'][0]['id'])
            document=bx.apply_plan(bx.parse(seed['xml']),{'mode':'rebuild','summary':'native requirements oracle','operations':{'level':row['character_level'],'replace_skills':[{'slot':'','gems':[{'skill_id':skill_id,'level':row['gem_level']}]}]}},native=seed)
            documents.append(bx.serialise(document));expected.append((skill_id,row))
        calculated=engine.calculate(self.pob,xml_batch=documents)['calculations']
        for (skill_id,row),actual in zip(expected,calculated):
            gem=next(g for g in actual['gems'] if g['id']==skill_id)
            self.assertEqual((gem['req_level'],gem['req_str'],gem['req_dex'],gem['req_int']),(row['character_level'],row['strength'],row['dexterity'],row['intelligence']))
        clarity = gems["Clarity"]["level_requirements"]
        self.assertLess(clarity[0]["base_resources"]["manaReservationFlat"], clarity[-1]["base_resources"]["manaReservationFlat"])
        self.assertGreater(gems["ShieldCrush"]["level_requirements"][0]["base_resources"]["cost"]["Mana"], 0)
        self.assertGreater(gems["SupportMinionDamage"]["level_requirements"][0]["base_resources"]["manaMultiplier"], 0)
        route = route_fixture(self.native['data_version'])
        checked, _ = self.validate_route(route, gems)
        self.assertEqual(checked["stages"][0]["skill_groups"][0]["gems"][0]["name"], gems["ShieldCrush"]["display_name"])
        route["stages"][-1]["skill_groups"][0]["gems"][0]["level"] = "auto"
        checked, _ = self.validate_route(route, gems)
        actual = checked["stages"][-1]["skill_groups"][0]["gems"][0]["level"]
        self.assertEqual(actual, max(row["gem_level"] for row in gems["ShieldCrush"]["level_requirements"] if row["character_level"] <= 68 and row.get("naturally_trainable")))
        gated=next(gem for gem in gems.values() if gem['min_level']>1 and gem['tags'].get('grants_active_skill'))
        route['stages'][0]['level']=gated['min_level']-1
        route["gems"][gated['skill_id']] = copy.deepcopy(route["gems"]["ShieldCrush"])
        route["stages"][0]["skill_groups"][0]["gems"][0]["skill_id"] = gated['skill_id']
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

    def test_48_current_cooldown_supports_zero_speed_manual_payments(self):
        plan=copy.deepcopy(self.rebuild)
        plan["operations"]["replace_skills"].append({"slot":"Helmet","gems":[{"skill_id":"ImmortalCall","level":1,"enable_global1":False}]})
        self.evaluate_plan(plan,"manual-cooldown")
        result=poe.read_json(self.run_dir/"candidates/manual-cooldown/evaluation.json")["result"]
        skill=next(s for s in result["active_skills"] if s["id"]=="ImmortalCall")
        self.assertFalse(skill.get("trigger"));self.assertEqual(skill["costs"]["Speed"],0)
        cooldown=skill["costs"]["Cooldown"]
        self.assertGreater(cooldown,0)
        self.assertAlmostEqual(resources.native_rate(skill),1/cooldown/skill["costs"].get("Repeats",1))
        focused={**result,"active_skills":[skill]}
        timeline={"duration_seconds":cooldown*1.5,"repeat":False,"events":[{"skill_ref":skill["skill_ref"],"at":0},{"skill_ref":skill["skill_ref"],"at":cooldown}]}
        audit=resources.timeline_assessment(focused,timeline)
        self.assertFalse(audit["errors"]);self.assertFalse(audit["pending"])
        row=next(r for r in audit["resources"] if r["resource_id"]=="Mana")
        self.assertEqual([t["payment"] for t in row["trace"]],[skill["costs"]["ManaCost"]]*2)
        timeline["events"][1]["at"]=cooldown/2
        self.assertTrue(resources.timeline_assessment(focused,timeline)["errors"])

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
        import re
        base_name='Two-Toned Boots (Armour/Energy Shield)'
        base=engine.calculate(self.pob,catalog='bases',query='id:'+base_name,limit=1)['catalog'][0]
        entries=engine.calculate(self.pob,catalog='affixes',query=base_name+'|',limit=10000)['catalog']
        selected=[max((entry for entry in entries if any(phrase in line for line in entry['lines'])),key=lambda entry:entry['required_item_level']) for phrase in ('to maximum Life','Movement Speed','Chaos Resistance')]
        recipe={'base':base_name,'ilvl':max(entry['required_item_level'] for entry in selected),'sockets':'-'.join(['W']*base['sockets']),
                'affixes':[{'id':entry['id'],'roll':0} for entry in selected]}
        item_set = bx.active_set(self.original, "Items", "ItemSet", "activeItemSet")
        item_id = int(next(slot for slot in item_set.findall("Slot") if slot.get("name") == "Boots").get("itemId"))
        plan = copy.deepcopy(self.refine)
        plan["operations"]["items"] = [{"id": item_id, "craft": recipe}]
        outcome = self.evaluate_plan(plan, "crafted-boots")
        self.assertEqual(outcome["status"], "draft", outcome["errors"])
        recorded = poe.read_json(self.run_dir / "candidates/crafted-boots/evaluation.json")
        raw = recorded["crafted_items"][0]["text"]
        for entry in selected:
            for line in entry['lines']:
                expected=re.sub(r'\((-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)\)',lambda match:match[1],line)
                expected=re.sub(r'\{[^}]*\}','',expected).strip()
                self.assertIn(expected,raw)
        self.assertEqual([entry['id'] for entry in recorded['crafted_items'][0]['affixes']],[entry['id'] for entry in selected])
        self.assertTrue(base['implicit'])
        self.assertIn("ArmourBasePercentile: 0.5", raw)
        output = poe.export(args(run=str(self.run_dir), name="crafted-boots"))
        self.assertIn("crafted_items", Path(output["files"][1]).read_text(encoding="utf-8"))
        bad = copy.deepcopy(recipe)
        conflicting=next(entry for entry in entries if entry['group']==selected[0]['group'] and entry['id']!=selected[0]['id'])
        bad["affixes"].append({"id":conflicting['id']})
        with self.assertRaisesRegex(ValueError, "Conflicting modifier group"):
            poe.materialize_items({"operations": {"items": [{"id": 20, "craft": bad}]}}, self.pob)
        bad = copy.deepcopy(recipe)
        bad["ilvl"] = recipe['ilvl']-1
        with self.assertRaisesRegex(engine.EngineError, "level is too low") as failure:
            poe.materialize_items({"operations": {"items": [{"id": 20, "craft": bad}]}}, self.pob)
        diagnostic=failure.exception.diagnostic
        self.assertEqual(diagnostic["code"],"native_invalid_input")
        self.assertEqual(diagnostic["action"],"edit_input")
        self.assertTrue(diagnostic["location"].startswith("/operations/items/0/craft/affixes/"))
        self.assertGreater(diagnostic["expected"]["minimum_item_level"],bad["ilvl"])
        self.assertEqual(diagnostic["actual"]["item_level"],bad["ilvl"])
        bad = copy.deepcopy(recipe)
        bad["sockets"] = '-'.join(['W']*(base['sockets']+1))
        with self.assertRaisesRegex(ValueError, "Socket count"):
            poe.materialize_items({"operations": {"items": [{"id": 20, "craft": bad}]}}, self.pob)
        unique_prefixes = {entry["group"]: entry["id"] for entry in entries if entry["type"] == "Prefix" and entry["required_item_level"] <= recipe['ilvl']}
        excess=base['affix_limits']['prefix']+1
        self.assertGreaterEqual(len(unique_prefixes),excess)
        bad = copy.deepcopy(recipe)
        bad["affixes"] = [{"id": mod} for mod in list(unique_prefixes.values())[:excess]]
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
        self.assertEqual(rows[1]["damage_observation"]["context"]["main_group"],1)
        self.assertEqual(rows[1]["damage_observation"]["value"],rows[1]["stats"]["CombinedDPS"])
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
        owner=next(cls for cls in result['classes'] if any(asc['name']=='Inquisitor' for asc in cls['ascendancies']))
        ascendancy=next(asc for asc in owner['ascendancies'] if asc['name']=='Inquisitor')
        seed=engine.calculate(self.pob,class_id=owner['id'])
        context=bx.apply_plan(bx.parse(seed['xml']),{'mode':'rebuild','summary':'native ascendancy count','operations':{'tree':{'ascendancy_id':ascendancy['id']}}},native=seed)
        seed=engine.calculate(self.pob,bx.serialise(context))
        targets=[node['id'] for node in nodes if node['ascendancy_name']=='Inquisitor' and node['type']=='Notable']
        chosen=[targets[0]]
        allocated=engine.calculate(self.pob,seed['xml'],tree_targets=chosen)
        extra=next(target for target in targets[1:] if target not in allocated['nodes'])
        route=route_fixture(self.native['data_version']);route.update(class_id=owner['id'],ascendancy='Inquisitor')
        stage=route['stages'][-1];stage.update(ascendancy_points=allocated['ascendancy_points'],ascendancy_nodes=chosen)
        checked,_=self.validate_route(route,gems,nodes=nodes);self.assertEqual(checked['stages'][-1]['ascendancy_used'],allocated['ascendancy_points'])
        stage['ascendancy_nodes'].append(extra)
        with self.assertRaisesRegex(ValueError,"超过"):self.validate_route(route,gems,nodes=nodes)
        stage['ascendancy_nodes'].remove(extra)
        previous=engine.calculate(self.pob,seed['xml'],tree_targets=[extra])
        route['stages'][1].update(ascendancy_points=previous['ascendancy_points'],ascendancy_nodes=[extra])
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
        self.assertEqual(result["alternatives"][0]["damage_observation"]["meaning"]["actor"],"minion")
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
        route=route_fixture(self.native['data_version']);route.update(class_id=3,ascendancy=None,gems={"Fireball":{}},stages=[])
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
        self.assertIn("config:scenario:a",response["pending"])
        self.assertNotIn("config:scenario:b",response["pending"])
        scenario=next(d for d in checked["diagnostics"] if d["code"]=="configuration_review_missing")
        self.assertEqual(scenario["reference"],"native:stage:a#/config_values")
        self.assertEqual(scenario["required_subject"],"configuration:stage:a")
        self.assertEqual(scenario["actual"],first["calculation"]["config_values"])
        self.assertEqual(checked["reference_contract"]["stage:a"]["fields"]["stats"],"native:stage:a#/stats")
        self.assertEqual(checked["reference_contract"]["resources:stage:a"]["origin"],"program_assessment")
        self.assertTrue(any(a["action"]=="view" and a["pointer"]=="/checked_route/reference_contract" for a in response["next_actions"]))
        claim_action=next(a for a in response["next_actions"] if a["action"]=="review_claims")
        self.assertEqual(claim_action["claim_ids"],["stage"])
        self.assertEqual(claim_action["input_revision"],bx.digest(path.read_bytes()))
        self.assertEqual(first["calculation"]["stats"]["Life"],checked["evidence"]["native:stage:a#/stats/Life"]["value"])
        self.assertEqual(checked["evidence"]["native:stage:a#/stats/Life"]["context"]["xml_sha256"],first["calculation"]["xml_sha256"])
        self.assertTrue(Path(first["calculation"]["xml_path"]).exists())
        self.assertNotIn("stats",second["calculation"]);self.assertEqual(second["calculation"]["status"],"not_calculated")
        self.assertIn("native:snapshot:b",response["pending"])
        self.assertEqual(checked["budget_assessment"]["status"],"user_declared")
        self.assertEqual(checked["budget_assessment"]["materials"][0]["quantity"],1)
        self.assertEqual(first["calculation"]["material_specs"][0]["spec_sha256"],held_item["spec_sha256"])
        stage["snapshot"]["timeline"]={"duration_seconds":2,"repeat":False,"events":[]}
        poe.write_json(path,route)
        with patch.object(facts,"gem_facts",return_value={}):
            missing_schedule=poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        schedule_checked=poe.read_json(output.with_suffix(".evidence.json"))["checked_route"]
        schedule_diagnostic=next(d for d in schedule_checked["diagnostics"] if d["code"]=="timeline_skill_unscheduled")
        self.assertEqual(schedule_diagnostic["location"],"/stages/0/snapshot/timeline/events")
        self.assertEqual(schedule_diagnostic["stage_id"],"a")
        repair=next(a for a in missing_schedule["next_actions"] if a["action"]=="repair_input")
        self.assertIn(schedule_diagnostic,repair["diagnostics"])
        stage["snapshot"].pop("timeline")
        stage["entry_checks"]=[{"check_id":"conditional-life","locator":"/stats/Life","op":"ge","value":first["calculation"]["stats"]["Life"]+1,"claim_id":"stage"}]
        route["assessment"]={"goal_claim_ids":["stage"],"scope":"Fixture only"}
        poe.write_json(path,route)
        with patch.object(facts,"gem_facts",return_value={}),patch.object(poe,"roundtrip",side_effect=ValueError("must reuse this verified stage")):
            response=poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        changed=poe.read_json(output.with_suffix(".evidence.json"))["checked_route"]
        self.assertTrue(changed["stages"][0]["calculation"]["roundtrip_verified"])
        self.assertEqual(changed["stages"][0]["calculation"]["entry_checks"][0]["status"],"unmet")
        route["assessment"]["review"]={"token":response["review_token"],"findings":"Fixture condition remains explicitly unmet.","unresolved":[],"conclusions":[{"claim_id":c["claim_id"],"scope":"Fixture stage only; entry check remains unmet","findings":"Checked current fixture and retained missing prerequisites."} for c in route.get("claims",[])]}
        poe.write_json(path,route)
        with patch.object(facts,"gem_facts",return_value={}):
            response=poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        reviewed=poe.read_json(output.with_suffix(".evidence.json"))["checked_route"]
        self.assertEqual(reviewed["final_review"]["status"],"unresolved")
        self.assertEqual(reviewed["final_review"]["coverage"]["missing_objective_ids"],[row["objective_id"] for row in route["objectives"]])
        template=poe.read_json(output.with_suffix(".review.json"))["review_template"]
        route["assessment"]["review"]["objectives"]=[{**row,"scope":"Current fixture requirement only", "findings":"Current entry check remains unmet; no gameplay guarantee."} for row in template["objectives"]]
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
        scenario_claim={"claim_id":"scenario","text":"Fixture input scene only; no gameplay guarantee.",
                        "subjects":["configuration:stage:a"],"evidence_refs":["native:stage:a#/config_values","native:resources:stage:a#/sustained_use/status"]}
        route["claims"].append(scenario_claim)
        scenario_claim["bindings"]=facts.claim_bindings(route,scenario_claim,engine.fingerprint(self.pob))
        poe.write_json(path,route)
        with patch.object(facts,"gem_facts",return_value={}):
            response=poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        self.assertNotIn("config:scenario:a",response["pending"])
        checked=poe.read_json(output.with_suffix(".evidence.json"))["checked_route"]
        self.assertFalse(any(d["code"]=="configuration_review_missing" for d in checked["diagnostics"]))

    def test_37_unparsed_nonflask_remains_unknown_even_when_native_active_false(self):
        result=engine.calculate(self.pob,bx.serialise(self.original))
        item=next(i for i in result["equipped"] if not i["slot"].startswith("Flask "))
        item.update(active=False,unparsed=["Future unsupported modifier"])
        _,_,pending=poe.mechanical_issues(self.original,result,{})
        self.assertIn("item:"+str(item["id"])+":unparsed",pending)

    def test_38_route_surfaces_stage_errors_and_never_labels_blocked_snapshot_ready(self):
        route=route_fixture(self.native['data_version']);route.update(class_id=3,ascendancy=None,gems={'Fireball':{}})
        seed=engine.calculate(self.pob,class_id=route['class_id'])
        context=engine.calculate(self.pob,seed['xml'],catalog='nodes',query='life',limit=100)
        node=next(n for n in context['catalog'] if not n['allocated'] and not n['ascendancy_name'] and n['reachable'] and n['points_to_allocate']>1 and n['type'] in {'Normal','Notable'})
        route['evidence']=[{'evidence_id':'user','kind':'user_statement','subject':'intent','text':'Offline fixture only'}]
        route['stages']=[{'stage_id':'custom','name':'Diagnostic phase','level':12,'bases':{'Gloves':'Wool Gloves'},
            'skill_groups':[{'name':'Fireball','slot':'Gloves','use':'fixture','gems':[{'instance_id':'one','skill_id':'Fireball','level':1,'quality':0}]}],
            'snapshot':{'operations':{'items':[{'id':201,'text':'Rarity: NORMAL\nUnrecognized fixture base'}],'equip':[{'slot':'Gloves','item_id':201}],'tree':{'nodes_add':[node['id']]},'main_group':1},'metrics':['Life']}}]
        path=Path(self.temp.name)/'diagnostic-route.json';output=path.with_suffix('.html');poe.write_json(path,route)
        with patch.object(facts,'gem_facts',return_value={}):response=poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        self.assertEqual(response['status'],'blocked')
        self.assertTrue(response['errors'])
        self.assertEqual(response['stage_summary'][0]['status'],'blocked')
        state=poe.read_json(output.with_suffix('.state.json'))
        self.assertEqual(state['stages']['custom']['status'],'blocked')
        self.assertTrue(state['stages']['custom']['native_calculated'])
        checked=poe.read_json(output.with_suffix('.evidence.json'))['checked_route']
        detail=next(d for d in checked['stages'][0]['calculation']['tree_diagnostics'] if d['id']==node['id'])
        self.assertEqual(detail['path'],node['path'])
        diagnostic=next(d for d in checked['diagnostics'] if d['code']=='native_tree_nodes_unretained')
        self.assertEqual(diagnostic['stage_id'],'custom')
        self.assertEqual(diagnostic['location'],'/stages/0/snapshot/operations/tree')
        repair=next(a for a in response['next_actions'] if a['action']=='repair_input')
        self.assertTrue(any(row.get('code')=='native_tree_nodes_unretained' and row['location']==diagnostic['location'] for row in repair['diagnostics']))
        self.assertEqual(repair['input_revision'],bx.digest(path.read_bytes()))
        self.assertIn('普通天赋已分配',response['facts_summary'])
        html=output.read_text(encoding='utf8')
        self.assertIn('已知错误',html)
        self.assertIn(response['errors'][0].split('：',1)[-1],html)
        self.assertIn('tree_diagnostics',html)
        # Accept the observed proposal through the real editor, without a build-specific answer.
        proposal=diagnostic['connection_proposal']
        self.assertIn(node['id'],proposal['changes']['replace'][diagnostic['location']]['nodes'])
        self.assertEqual(poe.read_json(path)['stages'][0]['snapshot']['operations']['tree'],{'nodes_add':[node['id']]})
        self.assertTrue(proposal['preview']['roundtrip_verified'])
        self.assertEqual(proposal['preview']['point_budgets'][0]['status'],'unknown')
        with self.assertRaises(bx.ContractError):
            poe.edit_document(args(invoked=True,kind='route',file=str(path),proposal_file=str(output.with_suffix('.evidence.json')),proposal_id=proposal['proposal_id'],if_sha256='stale-source'))
        self.assertEqual(bx.digest(path.read_bytes()),repair['input_revision'])
        edited=poe.edit_document(args(invoked=True,kind='route',file=str(path),proposal_file=str(output.with_suffix('.evidence.json')),proposal_id=proposal['proposal_id'],if_sha256=repair['input_revision']))
        self.assertEqual(edited['revision'],bx.digest(path.read_bytes()))
        with patch.object(facts,'gem_facts',return_value={}):repaired=poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        final=poe.read_json(output.with_suffix('.evidence.json'))['checked_route']['stages'][0]['calculation']
        self.assertEqual(final['tree_diagnostics'],[])
        self.assertIn(node['id'],final['facts']['nodes'])
        self.assertEqual(final['facts']['points'],proposal['preview']['point_budgets'][0]['used'])
        self.assertFalse(any(d['code']=='native_tree_nodes_unretained' for a in repaired['next_actions'] for d in a.get('diagnostics',[])))
        self.assertEqual(repaired['status'],'blocked')  # The independent unknown equipment is still invalid.

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

    def test_43_goal_reference_repair_is_located_without_changing_the_build(self):
        plan=copy.deepcopy(self.refine)
        plan["claims"]=[{"claim_id":"overall","text":"Limited overall interpretation","subjects":["goal"],"evidence_refs":["native:candidate#/stats/Life"]},
                        {"claim_id":"detail","text":"A detail interpretation","subjects":["objective:direction"],"evidence_refs":["native:candidate#/stats/Life"]}]
        plan["assessment"]={"goal_claim_ids":["detail","overall"],"scope":"Fixture scope, not a player recommendation"}
        source=Path(self.temp.name)/"goal-contract.json";request=Path(self.temp.name)/"goal-contract-changes.json"
        poe.write_json(request,{"set":plan})
        first=poe.edit_document(args(invoked=True,kind="plan",file=str(source),changes=str(request),if_sha256=None))
        result=poe.evaluate(args(run=str(self.run_dir),plan=str(source),name="goal-contract"))
        self.assertIn("goal_assessment",result["pending"])
        repair=next(a for a in result["next_actions"] if a["action"]=="repair_input")
        diagnostic=next(d for d in repair["diagnostics"] if d["code"]=="goal_claim_subject_missing")
        self.assertEqual(diagnostic["location"],"/assessment/goal_claim_ids/0")
        self.assertEqual(diagnostic["actual"]["claim_id"],"detail")
        before=poe.read_json(self.run_dir/"candidates/goal-contract/evaluation.json")["result"]["stats"]
        poe.write_json(request,{"replace":{"/assessment/goal_claim_ids":["overall"]}})
        poe.edit_document(args(invoked=True,kind="plan",file=str(source),changes=str(request),if_sha256=first["revision"]))
        revised=poe.evaluate(args(run=str(self.run_dir),plan=str(source),name="goal-contract"))
        self.assertNotIn("goal_assessment",revised["pending"])
        self.assertEqual(poe.read_json(self.run_dir/"candidates/goal-contract/evaluation.json")["result"]["stats"],before)

    def test_44_reevaluation_invalidates_old_delivery_without_deleting_artifacts(self):
        plan=copy.deepcopy(self.refine)
        self.evaluate_plan(plan,"delivery-context")
        exported=poe.export(args(run=str(self.run_dir),name="delivery-context"))
        artifacts={path:Path(path).read_bytes() for path in exported["files"]}
        plan["summary"]="A revised interpretation of the same build inputs."
        self.evaluate_plan(plan,"delivery-context")
        status=poe.workflow_status(args(run=str(self.run_dir)))
        current=next(c for c in status["candidate_records"] if c["name"]=="delivery-context")
        self.assertTrue(current["valid"]);self.assertFalse(current["file_exported"])
        receipt=poe.read_json(self.run_dir/"output/delivery-context/delivery.json")
        self.assertFalse(receipt["file_exported"])
        self.assertTrue(all(Path(path).read_bytes()==content for path,content in artifacts.items()))

    def test_39_final_review_and_optional_url_survive_export_and_failed_render_is_not_success(self):
        plan=copy.deepcopy(self.refine)
        source=Path(self.temp.name)/"local-evidence.txt";source.write_text("Offline local record",encoding="utf-8")
        plan["evidence"]=[{"evidence_id":"artifact","kind":"artifact","subject":"fixture","path":source.name,"excerpt":"Offline local record"}]
        plan["claims"]=[{"claim_id":"review","text":"Fixture conclusion; remaining gaps are retained.","subjects":["goal"],"evidence_refs":["artifact","native:candidate#/stats/Life"]}]
        plan["assessment"]={"goal_claim_ids":["review"],"scope":"Fixture, not a player build"}
        first=self.evaluate_plan(plan,"reviewed")
        template=poe.read_json(self.run_dir/"candidates/reviewed/review.json")["review_template"]
        plan["assessment"]["review"]={"token":first["review_token"],"findings":"Checked current facts, object, conditions and limited conclusion.","unresolved":[],
            "objectives":[{**row,"scope":"Current fixture requirement","findings":"Remaining dependencies retained; not a gameplay certification."} for row in template["objectives"]],
            "conclusions":[{"claim_id":"review","scope":"Fixture only; remaining gaps retained","findings":"Artifact is a local record, not an independent gameplay measurement."}]}
        second=self.evaluate_plan(plan,"reviewed")
        self.assertEqual(first["review_token"],second["review_token"])
        self.assertNotIn("final_review:stale",second["pending"])
        result=poe.export(args(run=str(self.run_dir),name="reviewed"))
        final=poe.read_json(self.run_dir/"output/reviewed/final-assessment.json")
        self.assertEqual(final["review_packet"]["status"],"model_reviewed")
        self.assertTrue(any(value.startswith('objective:') for value in final['pending']))
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



    def test_43_native_equipment_layouts_match_actual_xml_and_do_not_mutate_seed(self):
        xml=bx.serialise(self.original)
        pairs=[("Crude Bow","Spike-Point Arrow Quiver"),("Crude Bow","Splintered Tower Shield"),
               ("Gnarled Branch","Splintered Tower Shield"),("Driftwood Wand","Glass Shank"),
               ("Glass Shank","Driftwood Wand"),(None,"Spike-Point Arrow Quiver")]
        layouts=[{"layout_id":str(i),"bases":{"Weapon 2":offhand,**({"Weapon 1":main} if main else {})},"empty_slots":[] if main else ["Weapon 1"]} for i,(main,offhand) in enumerate(pairs)]
        layouts.append({"layout_id":"unknown","bases":{"Weapon 2":"Spike-Point Arrow Quiver"}})
        seed=engine.calculate(self.pob,xml)
        probed=engine.calculate(self.pob,xml,equipment_layouts=layouts)
        self.assertEqual(probed["stats"],seed["stats"])
        self.assertEqual(poe.semantics(probed),poe.semantics(seed))
        self.assertEqual(bx.item_texts(bx.parse(probed["xml"])),bx.item_texts(bx.parse(seed["xml"])))
        unknown=probed["equipment_layouts"][-1]["checks"]["Weapon 2"]
        self.assertNotIn("valid_for_slot",unknown);self.assertIn("Weapon 1",unknown["unresolved_context"])
        documents=[]
        for main,offhand in pairs:
            changes={"items":[{"id":8002,"text":"Rarity: NORMAL\n"+offhand}],"equip":[{"slot":"Weapon 1","item_id":8001 if main else 0},{"slot":"Weapon 2","item_id":8002}]}
            if main:changes["items"].append({"id":8001,"text":"Rarity: NORMAL\n"+main})
            documents.append(bx.serialise(bx.apply_plan(self.original,{"mode":"refine","summary":"Native slot regression only","operations":changes},self.native)))
        actual=engine.calculate(self.pob,xml_batch=documents)
        for i,(observed,calculated) in enumerate(zip(probed["equipment_layouts"],actual["calculations"])):
            with self.subTest(pair=pairs[i]):
                equipped=next((item for item in calculated["equipped"] if item["slot"]=="Weapon 2"),None)
                valid=observed["checks"]["Weapon 2"]["valid_for_slot"]
                if equipped:self.assertIs(type(equipped["valid_for_slot"]),bool)
                self.assertEqual(valid,equipped["valid_for_slot"] if equipped else False)
                self.assertEqual(valid,i==0)
                self.assertEqual(observed["context"]["basis"],"declared_base_layout")
        catalog=engine.calculate(self.pob,catalog="bases",query="id:Spike-Point Arrow Quiver",limit=1)
        self.assertNotIn("Weapon 2",catalog["catalog"][0]["valid_slots"])
        self.assertEqual(catalog["catalog"][0]["valid_slots_context"]["basis"],"empty_equipment")

    def test_44_route_uses_declared_layout_instead_of_empty_catalog_slot_result(self):
        route=route_fixture(self.native['data_version']);route["ascendancy"]="";route["stages"]=route["stages"][-1:]
        route["evidence"]=[{"evidence_id":"user","kind":"user_statement","subject":"intent","text":"Offline SSF structure validation"}]
        stage=route["stages"][0];stage["bases"].update({"Weapon 1":"Crude Bow","Weapon 2":"Spike-Point Arrow Quiver"})
        path=Path(self.temp.name)/"equipment-route.json";output=path.with_suffix(".html");poe.write_json(path,route)
        with patch.object(facts,"gem_facts",return_value={}):result=poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        self.assertTrue(result["file_exported"]);self.assertEqual(result["errors"],[])
        checked=poe.read_json(output.with_suffix(".evidence.json"))["checked_route"]["stages"][0]
        self.assertTrue(checked["equipment_validation"]["checks"]["Weapon 2"]["valid_for_slot"])
        self.assertNotIn("native:equipment:"+stage["stage_id"]+":Weapon 2",result["pending"])
        self.assertIn("declared_base_layout",output.read_text(encoding="utf-8"))
        stage["bases"]["Future Slot"]=None;poe.write_json(path,route)
        with self.assertRaises(engine.EngineError) as error:poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        self.assertEqual(error.exception.diagnostic["location"],"/stages/0/bases/Future Slot")
        state=poe.read_json(output.with_suffix(".state.json"));self.assertEqual(state["status"],"blocked");self.assertFalse(state["file_exported"])

    def test_49_native_target_allocation_counts_shared_paths_and_preserves_known_removal(self):
        seed=engine.calculate(self.pob,class_id=int(bx.active_spec(self.original).get('classId')))
        catalog=engine.calculate(self.pob,seed['xml'],catalog='nodes',query='life',limit=100)['catalog']
        nodes=[n for n in catalog if n['type'] in {'Normal','Notable'} and not n['allocated'] and not n['ascendancy_name'] and n['reachable'] and 1<n['points_to_allocate']<10]
        first=nodes[0];second=next(n for n in nodes[1:] if set(n['path'])&set(first['path']))
        targets=[first['id'],second['id']]
        connected=engine.calculate(self.pob,seed['xml'],tree_targets=targets)
        self.assertEqual(connected['native_tree_allocation']['method'],'PassiveSpec.AllocNode')
        self.assertTrue(set(targets)<=set(connected['nodes']))
        self.assertLess(connected['points'],sum(n['points_to_allocate'] for n in (first,second)))
        self.assertTrue(set(seed['nodes'])<=set(connected['nodes']))
        self.assertEqual(connected['native_xml']['method'],'Build.SaveDB')
        saved=poe.roundtrip(self.pob,connected,bx.parse(connected['xml']))
        rerun=engine.calculate(self.pob,saved,tree_targets=targets+[10**12])
        self.assertEqual(rerun['native_tree_allocation']['targets'][0]['status'],'already_allocated')
        self.assertEqual(rerun['native_tree_allocation']['targets'][-1]['status'],'unknown_node')
        self.assertEqual(rerun['nodes'],connected['nodes'])
        self.assertEqual(rerun['stats'],connected['stats'])
        document=bx.apply_plan(bx.parse(seed['xml']),{'mode':'rebuild','summary':'native connection proof','operations':{'tree':{'nodes_add':targets}}},native=seed)
        current=engine.calculate(self.pob,bx.serialise(document))
        _,diagnostics=poe.native_tree_diagnostics(self.pob,document,current,tree_operations={'nodes_add':targets},limits={'normal_points_available':0})
        proposal=diagnostics[0]['connection_proposal']
        self.assertFalse(proposal['adoptable']);self.assertEqual(proposal['blockers'],['point_budget:normal'])
        self.assertEqual(proposal['preview']['point_budgets'][0]['used'],connected['points'])
        connector=first['path'][-1]
        prior=engine.calculate(self.pob,seed['xml'],tree_targets=[connector])
        tree={'nodes_add':[first['id']],'nodes_remove':[connector]}
        document=bx.apply_plan(bx.parse(prior['xml']),{'mode':'rebuild','summary':'explicit removal proof','operations':{'tree':tree}},native=prior)
        current=engine.calculate(self.pob,bx.serialise(document))
        _,diagnostics=poe.native_tree_diagnostics(self.pob,document,current,tree_operations=tree)
        self.assertIn(connector,diagnostics[0]['connection_conflicts_with_removal'])
        self.assertNotIn('connection_proposal',diagnostics[0])

    def test_50_native_mastery_target_requires_current_option_and_roundtrip_keeps_selection(self):
        seed=engine.calculate(self.pob,class_id=int(bx.active_spec(self.original).get('classId')))
        nodes=engine.calculate(self.pob,seed['xml'],catalog='nodes',query='',limit=10000)['catalog']
        node=next(n for n in nodes if n['type']=='Mastery' and n.get('mastery_effects') and n.get('reachable') and n.get('points_to_allocate',1000)<15)
        effect=node['mastery_effects'][0]['effect']
        missing=engine.calculate(self.pob,seed['xml'],tree_targets=[node['id']])
        self.assertEqual(missing['native_tree_allocation']['targets'][0]['status'],'mastery_selection_required')
        self.assertNotIn(node['id'],missing['nodes'])
        tree={'nodes_add':[node['id']],'masteries_add':{str(node['id']):effect}}
        document=bx.apply_plan(bx.parse(seed['xml']),{'mode':'rebuild','summary':'native mastery preview','operations':{'tree':tree}},native=seed)
        current=engine.calculate(self.pob,bx.serialise(document))
        _,diagnostics=poe.native_tree_diagnostics(self.pob,document,current,tree_operations=tree)
        proposal=diagnostics[0]['connection_proposal']
        self.assertTrue(proposal['preview']['roundtrip_verified']);self.assertIn(node['id'],proposal['preview']['nodes'])
        adopted=bx.apply_plan(document,{'mode':'rebuild','summary':'apply native mastery proposal','operations':{'tree':proposal['changes']['replace']['/operations/tree']}},native=current)
        actual=engine.calculate(self.pob,bx.serialise(adopted))
        self.assertEqual(actual['mastery_selections'][str(node['id'])],effect)
        poe.roundtrip(self.pob,actual,adopted)
        changed=copy.deepcopy(actual);changed['mastery_selections'][str(node['id'])]=10**12
        self.assertTrue(any('专精' in error for error in poe.mechanical_issues(adopted,changed,{})[0]))
        foreign=next(n for n in nodes if n['ascendancy_name'] and not n['is_ascendancy_start'] and not n['reachable'])
        unreachable=engine.calculate(self.pob,seed['xml'],tree_targets=[foreign['id']])
        self.assertEqual(unreachable['native_tree_allocation']['targets'][0]['status'],'unreachable')
        self.assertFalse(unreachable['native_tree_allocation']['targets'][0]['retained'])

    def test_51_native_gem_requirement_proposal_exposes_attribute_gap_and_single_gem_cost(self):
        native=engine.calculate(self.pob,queries=[{'kind':'gems','query':'id:Fireball','limit':1}])
        gem=native['catalog_batch'][0]['entries'][0]
        highest=max((row for row in gem['level_requirements'] if row.get('naturally_trainable')),key=lambda row:row['gem_level'])
        seed=engine.calculate(self.pob,class_id=int(bx.active_spec(self.original).get('classId')))
        plan={'mode':'rebuild','summary':'native requirement proof','operations':{'level':highest['character_level'],
            'items':[{'id':201,'craft':{'base':self.native['catalog'][0]['name'],'ilvl':1,'sockets':'W-W','affixes':[]}}],
            'equip':[{'slot':'Gloves','item_id':201}],
            'replace_skills':[{'slot':'Gloves','gems':[{'skill_id':'Fireball','level':highest['gem_level']},{'skill_id':'Fireball','level':1}]}],'main_group':1}}
        resolved,_=poe.materialize_items(plan,self.pob)
        document=bx.apply_plan(bx.parse(seed['xml']),resolved,native=seed)
        result=engine.calculate(self.pob,bx.serialise(document))
        before=bx.serialise(document)
        diagnostics=poe.native_gem_diagnostics(self.pob,document,result,gems={'Fireball':gem})
        self.assertEqual(len(diagnostics),1)
        diagnostic=diagnostics[0]
        self.assertIn('intelligence',diagnostic['required_gaps'])
        self.assertEqual(diagnostic['required_gaps']['intelligence']['available'],result['stats']['Int'])
        proposal=diagnostic['change_proposal']
        self.assertEqual(proposal['selected_level'],max(diagnostic['usable_natural_levels']))
        self.assertTrue(proposal['preview']['roundtrip_verified'])
        self.assertEqual(len(proposal['changes']['set']['operations']['gems']),1)
        changed=bx.apply_plan(document,{'mode':'rebuild','summary':'adopt one gem deferral','operations':proposal['changes']['set']['operations']},native=result)
        actual=engine.calculate(self.pob,bx.serialise(changed))
        self.assertEqual(actual['stats'],proposal['preview']['stats'])
        self.assertEqual(bx.groups(changed)[0].findall('Gem')[1].get('level'),'1')
        self.assertFalse(poe.mechanical_issues(changed,actual,{})[0])
        self.assertEqual(bx.serialise(document),before)
        incomplete=copy.deepcopy(result);incomplete['stats'].pop('Int')
        with patch.object(engine,'calculate') as calculate:
            unknown=poe.native_gem_diagnostics(self.pob,document,incomplete,gems={'Fireball':gem})
        calculate.assert_not_called()
        self.assertFalse(unknown)  # Unknown attributes cannot be labelled a measured deficit.


if __name__ == "__main__": unittest.main()
