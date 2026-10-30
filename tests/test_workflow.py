"""Offline behavioral tests, including the installed real calculation engine.

Run with TeleAgent's Python: python -m unittest discover -s tests -v
The user's XML and installed application files are never edited.
"""
import argparse
import base64
import copy
import html
from datetime import date, datetime, timedelta, timezone
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
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


def native_test_sample():
    """Both native sample consumers use the documented private fixture entry point."""
    value=os.environ.get('POE_TEST_XML')
    if not value or not Path(value).is_file():
        raise unittest.SkipTest('需要 POE_TEST_XML 指向有效私人原生 XML；样本结构见 TEST-REPORT.md，不随机换构筑。')
    return Path(value).resolve()


def review_row(identity, key, scope, findings, *, verdict='unknown', evidence_refs=(), counter_evidence_refs=(), unimplemented=()):
    return {identity:key,'scope':scope,'findings':findings,'verdict':verdict,'evidence_refs':list(evidence_refs),
            'counter_evidence_refs':list(counter_evidence_refs),'conditions':[],'unimplemented':list(unimplemented)}


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
    def test_empty_plan_preserves_level_mode_and_all_xml(self):
        for mode in (None,'true','false'):
            with self.subTest(mode=mode):
                original=bx.parse(SAMPLE)
                if mode is not None:original.find('Build').set('characterLevelAutoMode',mode)
                before=bx.serialise(original)
                result=bx.apply_plan(original,{'mode':'refine','summary':'Observe the original role','operations':{}})
                self.assertEqual(bx.serialise(result),before)
                changed=bx.apply_plan(original,{'mode':'refine','summary':'Explicit level change','operations':{'level':87}})
                self.assertEqual(changed.find('Build').get('level'),'87')
                self.assertEqual(changed.find('Build').get('characterLevelAutoMode'),'false')
                self.assertEqual(bx.serialise(original),before)

    def test_native_explicit_gem_state_rejects_ignored_or_changed_requests(self):
        current={'group':1,'index':1,'id':'ShieldCrush','source':'Item:fixture','level':20,'quality':0,
                 'enabled':True,'count':1,'enable_global1':True,'enable_global2':False}
        cases={'skill_id':'NativeOtherVariant','level':19,'quality':1,'enabled':False,
               'count':2,'enable_global1':False,'enable_global2':True}
        for field,expected in cases.items():
            with self.subTest(field=field):
                document=bx.parse(SAMPLE);bx.groups(document)[0].find('Gem').set(bx.GEM_FIELDS[field],bx.attr(expected))
                result={'xml':SAMPLE,'gems':[current]}
                with self.assertRaises(bx.ContractError) as caught:
                    bx.verify_selections(document,result,{'gems':[{'group':1,'index':1,field:expected}]})
                diagnostic=caught.exception.diagnostic
                self.assertEqual(diagnostic['code'],'native_selection_changed')
                self.assertEqual(diagnostic['location'],'/operations/gems/0/'+field)
                self.assertEqual(diagnostic['expected'],expected)
                self.assertEqual(diagnostic['actual'],current['id' if field=='skill_id' else field])

    def test_native_explicit_group_state_uses_actual_group_not_requested_xml(self):
        native={'group':1,'index':1,'id':'ShieldCrush','group_enabled':True,'slot':'Boots',
                'group_count':1,'include_in_full_dps':False}
        for field,expected in {'enabled':False,'slot':'Helmet','group_count':2,'include_in_full_dps':True}.items():
            with self.subTest(field=field):
                document=bx.parse(SAMPLE);bx.groups(document)[0].set(bx.GROUP_FIELDS[field],bx.attr(expected))
                group={'group':1,'enabled':True,'slot':'Boots','group_count':1,'include_in_full_dps':False}
                with self.assertRaises(bx.ContractError) as caught:
                    bx.verify_selections(document,{'xml':SAMPLE,'gems':[native],'skill_selections':[group]}, {'groups':[{'index':1,field:expected}]})
                self.assertEqual(caught.exception.diagnostic['location'],'/operations/groups/0/'+field)

    def test_native_explicit_gem_distinguishes_missing_object_from_missing_field(self):
        document=bx.parse(SAMPLE);bx.groups(document)[0].find('Gem').set('enabled','false')
        for gems,action in (([],'edit_input'),([{'group':1,'index':1,'recognised':False}],'edit_input'),
                            ([{'group':1,'index':1,'id':'ShieldCrush'}],'inspect_capability')):
            with self.subTest(action=action),self.assertRaises(bx.ContractError) as caught:
                bx.verify_selections(document,{'xml':SAMPLE,'gems':gems},{'gems':[{'group':1,'index':1,'enabled':False}]})
            self.assertEqual(caught.exception.diagnostic['action'],action)
            self.assertEqual(caught.exception.diagnostic['location'],'/operations/gems/0/enabled')
            self.assertEqual(caught.exception.diagnostic['actual'],None)

    def test_native_explicit_matching_state_and_replacement_accept_normalization(self):
        document=bx.parse(SAMPLE)
        native={'group':1,'index':1,'id':'ShieldCrush','level':20,'quality':0,'enabled':True,
                'count':1,'enable_global1':True,'group_enabled':True,'slot':'Boots','group_count':1}
        declared={'slot':'Boots','enabled':True,'gems':[{'skill_id':'ShieldCrush','level':20,'quality':0,'enabled':True,'count':1,'enable_global1':True}]}
        bx.verify_selections(document,{'xml':SAMPLE,'gems':[native],'skill_selections':[{'group':1,'enabled':True,'slot':'Boots'}]},{'replace_skills':[declared]})

    def setUp(self):
        self.root = bx.parse(SAMPLE)

    def test_native_checkbox_rejects_truthy_strings_and_numbers_before_materialization(self):
        native={'config_types':{'future-checkbox':'check'},'config_lists':{}}
        original=bx.serialise(self.root)
        for value in ('false','true',0,1):
            with self.subTest(value=value):
                with self.assertRaises(bx.ContractError) as caught:
                    bx.apply_plan(self.root,{'mode':'refine','summary':'Native input type fixture','operations':{'config':{'future-checkbox':value}}},native=native)
                self.assertEqual(caught.exception.diagnostic['location'],'/operations/config/future-checkbox')
                self.assertEqual(bx.serialise(self.root),original)
        document=bx.apply_plan(self.root,{'mode':'refine','summary':'Native boolean fixture','operations':{'config':{'future-checkbox':False}}},native=native)
        self.assertEqual(bx.active_config(document).find('Input').attrib,{'name':'future-checkbox','boolean':'false'})

    def test_shared_scenario_uses_the_current_native_input_type_contract(self):
        native={'config_types':{'future-checkbox':'check','future-count':'countAllowZero','future-text':'text','future-enum':'list'},
                'config_lists':{'future-enum':[False,'new-season-option',3]}}
        for key,value in [('future-checkbox','false'),('future-count',True),('future-text',1),('future-enum',0)]:
            with self.subTest(key=key):
                with self.assertRaises(bx.ContractError) as caught:
                    poe.prepared(self.root,{'scenario':{key:value}},native=native)
                self.assertEqual(caught.exception.diagnostic['location'],'/scenario/'+key)
                intent=intent_for(self.root);intent['scenario']={key:value}
                with self.assertRaises(bx.ContractError) as intent_error:
                    poe.validate_intent(intent,self.root,native)
                self.assertEqual(intent_error.exception.diagnostic['location'],'/scenario/'+key)
        source=poe.prepared(self.root,{'scenario':{'future-checkbox':False,'future-count':2,'future-text':'abc','future-enum':False}},native=native)
        self.assertEqual(len(bx.active_config(source).findall('Input')),4)

    def test_config_validation_is_atomic_and_keeps_clear_distinct_from_false(self):
        native={'config_types':{'future-switch':'check','future-count':'float'},'config_lists':{}}
        bx.set_config(self.root,{'future-switch':True,'obsolete-field':'preserve'})
        original=bx.serialise(self.root)
        with self.assertRaises(bx.ContractError):
            bx.set_config(self.root,{'future-switch':False,'future-count':True},native=native)
        self.assertEqual(bx.serialise(self.root),original)
        bx.set_config(self.root,{'future-switch':None,'obsolete-field':None},native=native)
        self.assertEqual(bx.active_config(self.root).findall('Input'),[])
        with self.assertRaises(bx.ContractError):
            poe.prepared(self.root,{'scenario':{'future-switch':False}},scenario={'future-switch':0},native=native)

    def test_missing_or_future_config_metadata_returns_a_capability_diagnostic(self):
        for native in ({},{'config_types':{'future-field':'check'},'capabilities':{'native_config_types':False}},
                       {'config_types':{'future-field':'new-native-control'}},
                       {'config_types':{'future-field':'list'},'config_lists':{'future-field':[]}}):
            with self.subTest(native=native),self.assertRaises(bx.ContractError) as caught:
                bx.set_config(self.root,{'future-field':False},native=native)
            self.assertEqual(caught.exception.diagnostic['action'],'inspect_capability')
            self.assertEqual(caught.exception.diagnostic['location'],'/operations/config/future-field')
        with self.assertRaises(bx.ContractError) as caught:
            bx.set_config(self.root,{'unknown-new-field':True},native={'config_types':{'current-field':'check'}})
        self.assertEqual(caught.exception.diagnostic['code'],'unknown_native_config')
        self.assertEqual(caught.exception.diagnostic['available_options'],['current-field'])

    def test_xml_config_values_cannot_hide_ambiguous_or_invalid_types(self):
        for attributes in ({'string':'false','boolean':'false'},{'boolean':'False'},{'number':'NaN'},{'number':'abc'},{}):
            with self.subTest(attributes=attributes),self.assertRaises(bx.ContractError):
                bx.config_input_value(ET.Element('Input',{'name':'future-field',**attributes}),'/config/future-field')
        self.assertIs(bx.config_input_value(ET.Element('Input',{'boolean':'false'}),'/config/future-field'),False)
        self.assertEqual(bx.config_input_value(ET.Element('Input',{'string':'false'}),'/config/future-field'),'false')

    def test_invalid_native_checkbox_observation_is_unknown_in_effect_state(self):
        for value in ('false',0,1):
            with self.subTest(value=value):
                state=engine.effect_state({'config_controls':{'future-switch':{'type':'check','value':value}}})
                self.assertIsNone(state['config_switches']['future-switch'])
                self.assertEqual(state['invalid_config_switch_values'],{'future-switch':value})

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
            self.assertNotIn("version_source_hint",info)
            self.assertNotIn("version_evidence", info)
            self.assertEqual(info['data_layout']['tree_locations']['3_30'],['TreeData/3_30/tree.lua'])
            self.assertEqual(info['data_layout']['missing_declared_trees'],['3_29','3_30_ruthless'])
            self.assertEqual(info['data_layout']['unregistered_tree_directories'],['3_50_unregistered'])
            self.assertEqual(info['latest_standard_tree'],'3_30')
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
        preserved=intent_for(self.root)
        preserved['keep_skill_groups']=[1]
        preserved['constraint_sources']['/keep_skill_groups']='user'
        self.assertEqual(poe.validate_intent(preserved,self.root)['keep_skill_groups'],[1])
        missing=copy.deepcopy(preserved);missing['constraint_sources'].pop('/keep_skill_groups')
        with self.assertRaisesRegex(ValueError,'/keep_skill_groups'):poe.validate_intent(missing,self.root)
        for value in (None,[True],[0],[2],[1,1],{'group':1}):
            with self.subTest(keep_skill_groups=value),self.assertRaises(ValueError):
                poe.validate_intent({**preserved,'keep_skill_groups':value},self.root)

    def test_budget_keeps_unknown_and_foreign_prices_unverified(self):
        intent={"budget":{"amount":20,"currency":"divine"},"league":"CN"}
        item={"kind":"item","item_id":5,"spec_sha256":bx.digest('fixture item'),'quantity':1}
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
        group=bx.groups(self.root)[0]
        group.set('slot','')
        self.assertTrue(poe.socket_layout_issues(self.root,result))
        group.set('enabled','false')
        self.assertEqual(poe.socket_layout_issues(self.root,result),[])
        group.set('enabled','true');group.set('slot','Boots')
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
        retained=copy.deepcopy(old)
        retained['gems']+=copy.deepcopy(old['gems'])
        retained['gems'][1].update(slot='Gloves',group_enabled=False)
        duplicate=poe.material_requirements(self.root,self.root,old,retained)[0]
        self.assertEqual((duplicate['original_quantity'],duplicate['final_quantity'],duplicate['quantity']),(1,2,1))
        self.assertIn('需另备',duplicate['description'])

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
            "stages":[{"stage_id":"stage-"+str(i),"name":"测试阶段","level":level,"act":act,"entry_conditions":[],"bases":{"Gloves":"Wool Gloves"},
                       "skill_groups":[{"name":"输出","slot":"Gloves","use":"按需使用","gems":[{"instance_id":"one","skill_id":"ShieldCrush","level":1,"quality":0}]}],
                       "ascendancy_points":0,"ascendancy_nodes":[],"required_items":[]} for i,(level,act) in enumerate(((1,1),(12,2),(68,11)))]}


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.route=route_fixture()
        self.gems={"ShieldCrush":{"skill_id":"ShieldCrush","name":"Shield Crush","tags":{"grants_active_skill":True},"level_requirements":[{"gem_level":1,"character_level":1,"naturally_trainable":True,"strength":10,"dexterity":0,"intelligence":0,"base_resources":{"manaCost":6}},{"gem_level":20,"character_level":70,"naturally_trainable":True}]}}
        self.classes=[{"id":4,"name":"Duelist","ascendancies":[{"id":3,"name":"Champion"}]}]
        self.evidence={"user":{"kind":"user_statement","status":"user_declared","subject":"intent"}}
        self.bases={"Wool Gloves":{"name":"Wool Gloves","sockets":4,"valid_slots":["Gloves"]}}
        self.slots=[{"name":"Gloves"}]
        self.equipment={stage["stage_id"]:{"bases":copy.deepcopy(stage["bases"]),"checks":{"Gloves":{"base":"Wool Gloves","valid_for_slot":True}},"context":{"basis":"declared_base_layout"}} for stage in self.route["stages"]}
    def check(self):
        return routes.validate(self.route,self.gems,"3.29",self.classes,bases=self.bases,slots=self.slots,equipment=self.equipment,evidence=self.evidence)

    def test_unmaterialized_stage_checks_native_base_level_without_guessing_attributes(self):
        self.bases["Wool Gloves"]["requirements"]={"level":2,"str":999}
        with self.assertRaises(bx.ContractError) as error:self.check()
        self.assertEqual(error.exception.diagnostic["location"],"/stages/0/bases/Gloves")
        self.assertEqual(error.exception.diagnostic["expected"]["minimum_character_level"],2)
        self.route["stages"][0]["level"]=2
        checked,_=self.check()  # No actual attributes were supplied; do not invent them.
        self.assertEqual(checked["stages"][0]["level"],2)
        self.route["stages"][0]["level"]=1
        self.route["stages"][0]["snapshot"]={"assumptions":[],"operations":{}}
        self.check()  # The actual snapshot item requirements, not its bare base, govern this case.

    def test_goal_reference_diagnostics_use_explicit_selection_without_duplicate_subject(self):
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
                         ["/assessment/goal_claim_ids/0","/assessment/goal_claim_ids/2"])
        self.assertEqual([d["code"] for d in checked["diagnostics"]],
                         ["goal_claim_missing","goal_claim_unresolved"])
        self.assertTrue(all(d["available_goal_claim_ids"]==["detail","overall"] for d in checked["diagnostics"]))
        self.route["assessment"]["goal_claim_ids"]=["detail","overall"]
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
        self.bases["Wool Gloves"]["valid_slots"]=["Future Slot"]
        checked,_=self.check();self.assertNotIn("native:equipment:stage-0:Gloves",checked["pending"])
        self.equipment["stage-0"]["checks"]["Gloves"]["valid_for_slot"]=False
        with self.assertRaisesRegex(ValueError,"不相符"):self.check()
        self.equipment.clear();checked,_=self.check();self.assertIn("native:equipment:stage-0:Gloves",checked["pending"])

    def test_snapshot_uses_actual_socket_capacity_without_duplicate_base_declaration(self):
        stage=self.route['stages'][0]
        stage.pop('bases');stage['snapshot']={}
        layout=self.equipment['stage-0']
        layout['context']={'basis':'snapshot_xml','xml_sha256':'current-snapshot'}
        layout['checks']['Gloves']['socket_capacity']=1
        before=copy.deepcopy(self.route)
        checked,_=self.check()
        self.assertNotIn('native:socket_capacity:stage-0:Gloves',checked['pending'])
        self.assertEqual(checked['stages'][0]['socket_evidence']['Gloves']['capacity'],1)
        self.assertEqual(self.route,before)
        stage['skill_groups'][0]['gems'].append(dict(stage['skill_groups'][0]['gems'][0],instance_id='second'))
        with self.assertRaisesRegex(ValueError,'孔位|占孔'):self.check()
        stage['skill_groups'][0]['gems'].pop()
        layout['context']['basis']='declared_base_layout'
        self.assertIn('native:socket_capacity:stage-0:Gloves',self.check()[0]['pending'])

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

    def test_route_retracts_invalidated_judgment_in_all_three_display_locations(self):
        claim={"claim_id":"entry","text":"Use the former arrangement before entering.",
               "subjects":["objective:fixture-goal","stage:stage-0"],"evidence_refs":["user"],
               "conditions":["Current model prerequisite."],"uncertainties":["Current unresolved coverage."]}
        self.route["claims"]=[claim]
        self.route["stages"][0]["entry_conditions"]=["entry"]
        for invalidation in ("stale","missing",None):
            with self.subTest(invalidation=invalidation):
                current=copy.deepcopy(self.route)
                if invalidation!="missing":
                    current["claims"][0]["bindings"]=facts.claim_bindings(current,current["claims"][0],"fixture")
                if invalidation=="stale": current["stages"][0]["level"]=2
                claims=facts.assess_claims(current["claims"],self.evidence,specification=current,engine_fingerprint="fixture")
                checked,warnings=routes.validate(current,self.gems,"3.29",self.classes,bases=self.bases,
                    slots=self.slots,equipment=self.equipment,evidence=self.evidence,claims=claims)
                rendered=routes.render(checked,warnings,"future","3_future")
                self.assertEqual(rendered.count(claim["text"]),0 if invalidation else 3)
                for text in claim["conditions"]+claim["uncertainties"]:
                    self.assertEqual(rendered.count(text),0 if invalidation else 3)
                self.assertEqual(current["claims"][0]["text"],claim["text"])
                entry_diagnostics=[row for row in checked["diagnostics"] if row["code"]=="entry_condition_claim_unresolved"]
                self.assertEqual(entry_diagnostics,[])
        self.route["stages"][0]["entry_conditions"]=["absent"]
        checked,_=self.check()
        self.assertIn("entry_condition:stage-0:absent",checked["pending"])
        self.assertTrue(any(row["code"]=="entry_condition_claim_unresolved" and row["actual"]=="absent"
                            and row["location"]=="/stages/0/entry_conditions/0"
                            for row in checked["diagnostics"]))

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
        stage=self.route["stages"][0];sid=stage["stage_id"]
        stage["snapshot"]={"assumptions":["Snapshot explanation instead of a claim ID"],"operations":{}}
        checked,_=self.check()
        diagnostic=next(d for d in checked["diagnostics"] if d["code"]=="snapshot_assumptions_unresolved")
        self.assertEqual(diagnostic["location"],"/stages/0/snapshot/assumptions")
        self.assertEqual(diagnostic["actual"],stage["snapshot"]["assumptions"])
        stage["snapshot"]["assumptions"]=["chosen"]
        checked,_=routes.validate(self.route,self.gems,"3.29",self.classes,bases=self.bases,slots=self.slots,
                                 equipment=self.equipment,evidence=self.evidence,
                                 claims=[{"claim_id":"chosen","status":"model_assessed","subjects":["objective:fixture-goal"]}])
        self.assertNotIn("snapshot_assumptions:"+sid,checked["pending"])
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
            url = "https://poe.qq.com/current-announcement-fixture"
            record = {"html": '<div id="newsDetail"><h2>S30 patch</h2><p>在3.29.0版本中，测试挑战赛季开放。</p><p>' + "other updates " * 20000 + '</p></div>',
                      "url": url, "checked_at": "2026-10-04", "sha256": "fixture"}
            info = {"data_version": "3.29", "engine_fingerprint": "fixture", "version_source_hint": {"url": url, "status": "unverified_hint"}}
            with patch.object(engine, "resolve_root", return_value=Path(temp)), patch.object(engine, "check", return_value=info), \
                    patch.object(engine, "calculate", side_effect=AssertionError("version research needs no calculation")), patch.object(facts, "fetch", return_value=record):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = poe.main(["research", "--version", "--league", "国服S30", "--source",url, "--out", str(output)])
                self.assertEqual(code, 0)
                result = json.loads(stdout.getvalue())
                self.assertEqual(result["version_evidence"]["status"], "source_observed")
                self.assertIn("3.29.0", result["version_evidence"]["excerpt"])
                self.assertLess(len(stdout.getvalue()), 2400)
                self.assertNotIn("other updates", output.read_text(encoding="utf-8"))
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(poe.main(["research", "--version", "--league", "S29", "--source",url, "--out", str(output)]), 0)
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
    def test_worker_structured_error_is_independent_of_runtime_stderr(self):
        failure={'error':'Native selection is invalid','diagnostic':{'code':'native_invalid_input',
                 'location':'/queries/0/selection','expected':['current-a','current-b'],'actual':'stale',
                 'action':'edit_input','retryable':False}}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(engine,'check',return_value={'engine_fingerprint':'fixture'}),patch.object(engine.subprocess,'run',return_value=argparse.Namespace(
                returncode=1,stdout=json.dumps(failure).encode('utf-8'),stderr=b'Runtime diagnostic stack\n')):
                with self.assertRaises(engine.EngineError) as caught:
                    engine.calculate(root,cache=False)
            diagnostic=caught.exception.diagnostic
            for key,value in failure['diagnostic'].items(): self.assertEqual(diagnostic[key],value)
            self.assertEqual(diagnostic['message'],failure['error'])
            self.assertNotIn('Runtime diagnostic',str(caught.exception))
            for output in (b'',b'Invalid protocol output',b'[]',b'{"error":4}',b'{"error":"failed","diagnostic":[]}'):
                with self.subTest(output=output),patch.object(engine,'check',return_value={'engine_fingerprint':'fixture'}),patch.object(engine.subprocess,'run',return_value=argparse.Namespace(
                    returncode=1,stdout=output,stderr=b'Runtime failure without a structured response')):
                    with self.assertRaises(engine.EngineError) as unstructured:
                        engine.calculate(root,cache=False)
                    self.assertEqual(unstructured.exception.diagnostic['code'],'engine_failure')
                    self.assertIn('Runtime failure without a structured response',str(unstructured.exception))
                    self.assertNotIn('location',unstructured.exception.diagnostic)

    def test_current_local_names_bind_equipment_and_controls_without_replacing_native_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=base/'native';root.mkdir()
            translations=base/'Data/Translate/zh-rCN';translations.mkdir(parents=True)
            (translations/'Items_Future.txt.csv').write_text('Future Base,当前基底\n',encoding='utf-8')
            (translations/'Uniques.txt.csv').write_text('Future Unique,当前传奇\n',encoding='utf-8')
            (translations/'ConfigOptions.csv').write_text('Future input:,当前输入:\n',encoding='utf-8')
            (translations/'tree_dn.csv').write_text('Future Shared,当前天赋\n',encoding='utf-8')
            (translations/'Items_Gems.txt.csv').write_text('Future Shared,当前宝石\n',encoding='utf-8')
            (translations/'GUI.csv').write_text('Future Class,当前职业\nFuture Shared,界面同名\n',encoding='utf-8')
            (translations/'Items_Future.txt.csv').write_text('Future Base,当前基底\nFuture Shared,当前物品\nFuture Ambiguous,物品译名甲\nFuture Ambiguous,物品译名乙\n',encoding='utf-8')
            native={'catalog':[{'id':'future-base','name':'Future Base'}],
                    'equipped':[{'id':31,'name':'Future Unique','base':'Future Base'},
                                {'id':32,'name':'Untranslated Object','base':'Untranslated Base'},
                                {'id':33,'name':'Future Shared','base':'Future Ambiguous'},
                                {'id':34,'name':'Future Unique, Future Base','title':'Future Unique','base':'Future Base'}],
                    'gems':[{'id':'future-gem','name':'Future Shared'}],
                    'granted_passives':[{'id':41,'name':'Future Shared'}],
                    'classes':[{'id':51,'name':'Future Class'}],
                    'config_controls':{'future':{'id':'future','label':'Future input:','value':False}}}
            def worker(*_,**kwargs):
                request=json.loads(kwargs['input']);value=copy.deepcopy(native);value['request_id']=request['request_id']
                if request['catalog']=='nodes':value['catalog']=[{'id':41,'name':'Future Shared'}];value['catalog_total']=1
                return argparse.Namespace(returncode=0,stdout=json.dumps(value).encode('utf-8'),stderr=b'')
            info={'engine_fingerprint':'fixture','engine_version':'future','data_version':'current',
                  'data_version_basis':{},'available_tree_versions':[]}
            with patch.object(engine,'check',return_value=info),patch.object(engine,'fingerprint',return_value='fixture'),patch.object(engine.subprocess,'run',side_effect=worker):
                result=engine.calculate(root,catalog='bases',cache=False)
                nodes=engine.calculate(root,catalog='nodes',cache=False)
            item=result['equipped'][0]
            self.assertEqual((item['id'],item['name'],item['base']),(31,'Future Unique','Future Base'))
            self.assertEqual(item['name_cn'],'当前传奇');self.assertEqual(item['base_cn'],'当前基底')
            self.assertEqual(item['name_source'],'translations/Uniques.txt.csv')
            self.assertEqual(item['base_name_source'],'translations/Items_Future.txt.csv')
            self.assertEqual(result['catalog'][0]['display_name'],'当前基底（Future Base）')
            self.assertEqual(result['equipped'][1],native['equipped'][1])
            self.assertEqual(result['equipped'][2]['name_cn'],'当前物品')
            self.assertNotIn('base_cn',result['equipped'][2])
            self.assertEqual(result['gems'][0]['name_cn'],'当前宝石')
            self.assertEqual(result['granted_passives'][0]['name_cn'],'当前天赋')
            self.assertEqual(nodes['catalog'][0]['name_cn'],'当前天赋')
            self.assertEqual(result['classes'][0]['name_cn'],'当前职业')
            titled=result['equipped'][3]
            self.assertEqual(titled['name'],'Future Unique, Future Base')
            self.assertEqual(titled['title_cn'],'当前传奇')
            self.assertEqual(titled['title_name_source'],'translations/Uniques.txt.csv')
            self.assertIn('当前传奇，当前基底',titled['display_name'])
            control=result['config_controls']['future']
            self.assertEqual((control['id'],control['label'],control['value']),('future','Future input:',False))
            self.assertEqual(control['display_label'],'当前输入:（Future input:）')
            self.assertEqual(control['label_source'],'translations/ConfigOptions.csv')

    def test_unknown_config_distinguishes_current_output_from_native_input(self):
        native={'stats':{'FutureCalculated':True},'config_types':{'FutureControl':'check'},
                'config_controls':{'FutureControl':{'id':'FutureControl','type':'check'}},'gems':[]}
        with self.assertRaises(bx.ContractError) as caught:
            bx.config_value_shape('FutureCalculated',False,native,'/operations/config/FutureCalculated')
        diagnostic=caught.exception.diagnostic
        self.assertEqual(diagnostic['field_role'],'calculated_output')
        self.assertEqual(diagnostic['output_locator'],'/stats/FutureCalculated')
        self.assertEqual(diagnostic['available_options'],['FutureControl'])
        self.assertEqual(diagnostic['inspect_fields'],['config_types','config_controls','gems'])

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
    def test_misplaced_nested_reference_offers_current_cross_root_paths_without_aliasing(self):
        native={'candidate':{'stats':{}},'resources':{'entries':[{'future/value':4}]}}
        invalid='native:candidate#/readiness/entries/0/future~1value'
        with self.assertRaises(bx.ContractError) as caught:
            facts.resolve_reference(invalid,{},native)
        self.assertIn('native:resources#/entries/0/future~1value',caught.exception.diagnostic['related_references'])
        self.assertNotIn('value',caught.exception.diagnostic)
        self.assertEqual(facts.resolve_reference('native:resources#/entries/0/future~1value',{},native)['value'],4)
        native['candidate']['measurement_audit']={'status':'observed'}
        registry={'declared':{'status':'source_observed'}}
        for invalid,expected in (('resources#/entries/0/future~1value',['native:resources#/entries/0/future~1value']),
                                 ('measurement_audit',['native:candidate#/measurement_audit']),('invented',[])):
            with self.subTest(reference=invalid),self.assertRaises(bx.ContractError) as error:
                facts.resolve_reference(invalid,registry,native)
            self.assertEqual(error.exception.diagnostic['code'],'unknown_evidence_reference')
            self.assertEqual(error.exception.diagnostic['related_references'],expected)
            self.assertEqual(error.exception.diagnostic['reference_roots'],sorted(native))
            self.assertNotIn(invalid,registry)
        registry['measurement_audit']={'status':'user_declared'}
        self.assertIs(facts.resolve_reference('measurement_audit',registry,native),registry['measurement_audit'])

    def test_unresolved_reference_prose_stays_in_audit_but_not_current_delivery_advice(self):
        claim={'claim_id':'c','text':'Unsupported current guarantee','subjects':['goal'],'evidence_refs':['absent']}
        registry={'basis':{'status':'user_declared'}}
        native={'candidate':{'stats':{'Observed':7}}}
        for refs in (['absent'],[]):
            with self.subTest(references=refs):
                row=facts.assess_claims([{**claim,'evidence_refs':refs}],registry,native)[0]
                self.assertEqual(row['text'],claim['text'])
                self.assertEqual(row['status'],'unresolved')
                if not refs:
                    diagnostic=next(d for d in row['diagnostics'] if d['code']=='claim_evidence_missing')
                    self.assertEqual(diagnostic['location'],'/claims/0/evidence_refs')
                    self.assertEqual(diagnostic['actual'],[])
                    self.assertEqual(diagnostic['available_options'],['basis'])
                    self.assertEqual(diagnostic['reference_roots'],['candidate'])
                    actions=poe.delivery_actions({'status':'draft','errors':[],'pending':['claim:c'],
                        'claims':[row],'diagnostics':facts.claim_diagnostics([row])},'plan.json','evaluation.json','review.json')
                    self.assertEqual(actions[0]['action'],'repair_input')
                    self.assertEqual(actions[0]['repair_count'],1)
                displayed=facts.claim_display(row)
                self.assertTrue(displayed['revision_pending'])
                self.assertNotIn(claim['text'],displayed['text'])
        conditional={**row,'missing_evidence':[],'diagnostics':[],'uncertainties':['Effect coverage unmeasured']}
        self.assertFalse(facts.claim_display(conditional)['revision_pending'])
        for referenced in ({**claim,'evidence_refs':['basis']},
                           {**claim,'evidence_refs':[],'text':'{{value:native:candidate#/stats/Observed}}'}):
            current=facts.assess_claims([referenced],registry,native)[0]
            self.assertEqual(current['status'],'model_assessed')
            self.assertFalse(facts.claim_display(current)['revision_pending'])

    def test_claim_display_retracts_invalidated_prose_and_preserves_source_for_audit(self):
        claim={"claim_id":"model-choice","status":"unresolved","text":"Original model choice.",
               "rendered_text":"Retired instruction.","conditions":["Retired condition."],
               "uncertainties":["Retired uncertainty."],"evidence_refs":["current-source"],
               "check_status":"supported","verification_scope":"Declared check only"}
        for code in ("claim_binding_stale","claim_binding_missing","claim_check_refuted","missing_reference"):
            with self.subTest(code=code):
                source={**claim,"diagnostics":[{"code":code,"action":"review_claims"}]}
                before=copy.deepcopy(source);display=facts.claim_display(source)
                self.assertNotIn(source["rendered_text"],json.dumps(display))
                self.assertEqual((display["conditions"],display["uncertainties"]),([],[]))
                self.assertTrue(display["revision_pending"])
                self.assertEqual((display["claim_id"],display["status"]),("model-choice","unresolved"))
                self.assertEqual(display["diagnostics"],source["diagnostics"])
                self.assertEqual(source,before)

    def test_bad_array_reference_returns_current_indexed_choices_without_full_registry(self):
        native={'resources':{'timeline':{'resources':[{'resource_id':'Mana','status':'unknown'},
                                                      {'resource_id':'Life','status':'supported'}]}},
                'candidate':{'unrelated_'+str(i):None for i in range(200)}}
        with self.assertRaises(bx.ContractError) as caught:
            facts.resolve_reference('native:resources#/timeline/resources/Life/status',{},native)
        diagnostic=caught.exception.diagnostic
        self.assertLess(len(json.dumps(diagnostic)),2500)
        choices=diagnostic['available_references']
        self.assertEqual(choices[1]['identity'],{'resource_id':'Life'})
        self.assertEqual(choices[1]['reference'],'native:resources#/timeline/resources/1')
        self.assertEqual(facts.resolve_reference(choices[1]['reference']+'/status',{},native)['value'],'supported')

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
                self.assertEqual(set(diagnostic['reference_roots']),set(native))
                self.assertNotIn('reference_contract',diagnostic)
                if reference.startswith('native:resources#'):
                    self.assertEqual(diagnostic['reference_prefix'],'native:resources#/entries')
                    self.assertEqual(diagnostic['available_references'][0]['reference'],'native:resources#/entries/0')
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
        self.assertEqual(set(row["observations"][0]["text_templates"]),{"fact"})
        claim["text"]="{{value:"+ref+"}}"
        self.assertEqual(facts.assess_claims([claim],{},{"catalog":result})[0]["status"],"unresolved")

    def test_current_text_templates_distinguish_raw_values_from_complete_facts(self):
        native={"candidate":{"stats":{"FutureRate":8,"Scaled":.125,"Zero":0,"Disabled":False,"Unknown":None},
                "stat_metadata":{"FutureRate":[{"label":"Future per second","format":".1f","actor":"player"}],
                                 "Scaled":[{"label":"Future fraction","format":".1f%%","display_multiplier":100}]}}}
        def current(metric):
            ref="native:candidate#/stats/"+metric
            return facts.resolve_reference(ref,{},native)
        rate=current("FutureRate")
        claim={"claim_id":"rate","text":"费用="+rate["text_templates"]["value"]+"/s。","subjects":["goal"]}
        checked=facts.assess_claims([claim],{},native)[0]
        self.assertEqual(checked["rendered_text"],"费用=8/s。")
        self.assertEqual(checked["observations"][0]["text_templates"],rate["text_templates"])
        claim["text"]=current("Scaled")["text_templates"]["fact"]
        self.assertTrue(facts.assess_claims([claim],{},native)[0]["rendered_text"].endswith("12.5%"))
        claim["text"]=current("Scaled")["text_templates"]["value"]
        self.assertEqual(facts.assess_claims([claim],{},native)[0]["rendered_text"],"0.125")
        for key,expected in (("Zero","0"),("Disabled","false")):
            claim["text"]=current(key)["text_templates"]["value"]
            self.assertEqual(facts.assess_claims([claim],{},native)[0]["rendered_text"],expected)
        self.assertNotIn("value",current("Unknown")["text_templates"])
        native["candidate"]["stats"]["Raw{Field}"]=7
        self.assertFalse(current("Raw{Field}")["text_templates"])
        self.assertEqual(current("Raw{Field}")["value"],7)
        native["candidate"]["stats"]["FutureRate"]=13
        self.assertEqual(facts.assess_claims([{"claim_id":"rate","text":rate["text_templates"]["value"],"subjects":["goal"]}],{},native)[0]["rendered_text"],"13")
        published=poe.publication("draft",[],[],{"status":"unknown"},native,keys=["FutureRate"])
        measurement=published["measurements"][0]
        self.assertEqual(measurement["text_templates"],current("FutureRate")["text_templates"])

    def test_missing_measurement_cannot_pass_even_with_matching_review_token(self):
        ref="native:candidate#/stats/X";claim={"claim_id":"c","text":"at least 10","subjects":["goal"],"checks":[{"reference":ref,"op":"ge","value":10}]}
        checked=facts.assess_claims([claim],{},{"candidate":{"stats":{}}})
        packet,_=facts.final_review({}, {},checked,{}, {})
        _,pending=facts.final_review({}, {},checked,{}, {"review":{"token":packet["token"],"findings":"Reviewed current missing values.","conclusions":[review_row('claim_id','c','Current missing metric','No supported check; unknown retained.')]}})
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
            self.assertNotIn('maximum_bytes',diagnostic['expected'])
            source=folder/'source.txt';original=source.read_bytes();stamp=source.stat().st_mtime_ns
            absolute=facts.load_evidence([dict(declaration,path=str(source.resolve()))],nested,{})['price']
            self.assertEqual(absolute['status'],'artifact_observed')
            self.assertEqual(absolute['path'],str(source.resolve()))
            self.assertEqual(absolute['content_sha256'],bx.digest(original))
            self.assertEqual(source.read_bytes(),original);self.assertEqual(source.stat().st_mtime_ns,stamp)
            self.assertEqual(list(nested.iterdir()),[])
            self.assertEqual(facts.load_evidence([dict(declaration,path=str(source.resolve()),excerpt='invented')],nested,{})['price']['status'],'unresolved')
            missing=facts.load_evidence([dict(declaration,path='missing/source.txt')],folder,{})['price']
            self.assertEqual(missing['status'],'unresolved')
            self.assertEqual(missing['diagnostic']['expected'],{'source_base':str(folder.resolve()),'type':'existing text file'})
            self.assertEqual(missing['diagnostic']['actual'],str((folder/'missing/source.txt').resolve()))
            oversized=folder/'large.txt';oversized.write_bytes(b'x'*2_000_001)
            large=facts.load_evidence([dict(declaration,path=oversized.name)],folder,{})['price']
            self.assertEqual(large['status'],'unresolved')
            self.assertEqual(large['diagnostic']['expected'],{'maximum_bytes':2_000_000})
            self.assertEqual(large['diagnostic']['actual'],oversized.stat().st_size)
            self.assertEqual(large['repair']['source_base'],str(folder.resolve()))
            self.assertNotIn('next_actions',large['repair'])
            claim={"claim_id":"explanation","text":"Interpretation","subjects":["goal"],"evidence_refs":["price"]}
            assessed=facts.assess_claims([claim],rejected)[0]
            self.assertEqual(assessed["diagnostics"][0]["source_diagnostic"],diagnostic)
            with self.assertRaises(ValueError):facts.load_evidence([dict(declaration,status="verified")],folder,{})
            declaration["excerpt"]="invented price";self.assertEqual(facts.load_evidence([declaration],folder,{})["price"]["status"],"unresolved")
            # Text source pages must be reusable without model transcription or newline conversion.
            source.write_bytes(('第一行\r\n原文包含 \"引号\" 与 literal\\path。\r\n'+'长文字'*4000).encode('utf-8-sig'))
            original=source.read_bytes();stamp=source.stat().st_mtime_ns
            failed=facts.load_evidence([dict(declaration,path=str(source.resolve()),excerpt='paraphrased claim')],nested,{})['price']
            self.assertEqual(failed['status'],'unresolved')
            stdout=io.StringIO()
            with patch.object(engine,'calculate',side_effect=AssertionError('text view must not calculate')),redirect_stdout(stdout):
                code=poe.main(['view','--file',str(source),'--limit','12000'])
            self.assertEqual(code,0)
            page=json.loads(stdout.getvalue());text=original.decode('utf-8-sig')
            self.assertLessEqual(len(stdout.getvalue()),10000)
            self.assertEqual(page['value'],text[:page['next_offset']])
            self.assertEqual(page['page_unit'],'characters');self.assertEqual(page['revision'],bx.digest(original))
            template=page['evidence_template']
            self.assertEqual(template,{'kind':'artifact','path':str(source.resolve()),'excerpt':page['value']})
            actual=facts.load_evidence([{**template,'evidence_id':'actual','subject':'source'}],nested,{})['actual']
            self.assertEqual(actual['status'],'artifact_observed');self.assertEqual(actual['text'],page['value'])
            self.assertEqual(actual['content_sha256'],page['revision'])
            self.assertEqual(failed['repair']['source_revision'],page['revision'])
            action=failed['repair']['next_actions'][0]
            self.assertEqual(action,{'action':'view','file':str(source.resolve()),'pointer':'','offset':0,'limit':2000})
            next_page=poe.view_json(args(file=str(source),pointer='',fields='',keys=False,query='',offset=page['next_offset'],limit=2000))
            self.assertEqual(next_page['value'],text[page['next_offset']:page['next_offset']+2000])
            for options in ({'pointer':'/invented'},{'fields':'invented'},{'keys':True},{'query':'invented'}):
                parameters=dict(file=str(source),pointer='',fields='',keys=False,query='',offset=0,limit=20);parameters.update(options)
                with self.assertRaises(bx.ContractError):poe.view_json(args(**parameters))
            self.assertEqual(source.read_bytes(),original);self.assertEqual(source.stat().st_mtime_ns,stamp)
            self.assertEqual(list(nested.iterdir()),[])
            with self.assertRaises(bx.ContractError):poe.view_json(args(file=str(oversized),pointer='',fields='',keys=False,query='',offset=0,limit=20))
    def test_json_artifact_pointer_preserves_values_and_reports_exact_repairs(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);source=folder/'observations.json'
            poe.write_json(source,{'rows':[{'future/value':False,'zero':0,'missing':None}]})
            declaration={'evidence_id':'observed','kind':'artifact','subject':'fixture','path':source.name,'pointer':'/rows/0/future~1value'}
            nested=folder/'review';nested.mkdir()
            template=poe.view_json(args(file=str(source),pointer=declaration['pointer'],fields='',keys=False,query='',offset=0,limit=20))['evidence_template']
            absolute=facts.load_evidence([{**declaration,**template}],nested,{})['observed']
            self.assertEqual(absolute['status'],'artifact_observed');self.assertIs(absolute['value'],False)
            record=facts.load_evidence([declaration],folder,{})['observed']
            self.assertEqual(record['status'],'artifact_observed');self.assertIs(record['value'],False)
            self.assertEqual(record['locator'],declaration['pointer']);self.assertEqual(record['content_sha256'],bx.digest(source.read_bytes()))
            previous=record['digest'];poe.write_json(source,{'rows':[{'future/value':True,'zero':0,'missing':None}]})
            self.assertNotEqual(facts.load_evidence([declaration],folder,{})['observed']['digest'],previous)
            for pointer,expected in [('/rows/0/zero',0),('/rows/0/missing',None)]:
                self.assertEqual(facts.load_evidence([dict(declaration,pointer=pointer)],folder,{})['observed']['value'],expected)
            failed=facts.load_evidence([dict(declaration,pointer='/rows/0/absent')],folder,{})['observed']
            self.assertEqual(failed['status'],'unresolved');self.assertEqual(failed['diagnostic']['location'],'/evidence/0/pointer')
            self.assertIn('future/value',failed['diagnostic']['source_diagnostic']['expected']['fields'])
            action=failed['repair']['next_actions'][0]
            self.assertEqual(action,{'action':'view','file':str(source.resolve()),'pointer':'','keys':True,'offset':0,'limit':20})
            self.assertEqual(failed['repair']['source_revision'],bx.digest(source.read_bytes()))
            paraphrase={k:v for k,v in declaration.items() if k!='pointer'}
            paraphrase['excerpt']='Invented explanation of the current value.'
            failed=facts.load_evidence([paraphrase],folder,{})['observed']
            self.assertEqual(failed['repair']['next_actions'],[action])
            self.assertEqual(failed['status'],'unresolved');self.assertNotIn('value',failed)
            outside=folder/'nested';outside.mkdir()
            failed=facts.load_evidence([dict(paraphrase,path='../observations.json')],outside,{})['observed']
            self.assertNotIn('next_actions',failed['repair'])
            for raw in ('{"duplicate":1,"duplicate":2}','{"value":NaN}','{"value":1e309}'):
                source.write_text(raw,encoding='utf-8')
                failed=facts.load_evidence([dict(declaration,pointer='')],folder,{})['observed']
                self.assertEqual(failed['status'],'unresolved');self.assertNotIn('next_actions',failed['repair'])
            with self.assertRaises(bx.ContractError): facts.evidence_shape([dict(declaration,excerpt='also text')])
            with self.assertRaises(bx.ContractError): facts.evidence_shape([dict(declaration,kind='web')])

    def test_current_json_view_evidence_reuses_large_original_without_transcription(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);source=folder/'native-observations.json'
            poe.write_json(source,{'observed':False,'other_observations':'x'*2_000_001})
            original=source.read_bytes();stamp=source.stat().st_mtime_ns
            page=poe.view_json(args(file=str(source),pointer='/observed',fields='',keys=False,query='',offset=0,limit=20))
            self.assertIn('evidence_template',page)
            declaration={**page['evidence_template'],'evidence_id':'current','subject':'current native observation'}
            record=facts.load_evidence([declaration],folder,{})['current']
            self.assertEqual(record['status'],'artifact_observed',record)
            self.assertIs(record['value'],False)
            self.assertEqual(record['content_sha256'],page['revision'])
            self.assertEqual(source.read_bytes(),original)
            self.assertEqual(source.stat().st_mtime_ns,stamp)
            value=json.loads(original);value['calculation']={'stats':{'Observed':7}}
            value['reference_contract']=facts.reference_contract({'current':value['calculation']},{'current':{'pointer':'/calculation'}})
            poe.write_json(source,value)
            external=poe.view_json(args(file=str(source),pointer='/observed',fields='',keys=False,query='',offset=0,limit=20))
            self.assertIn('evidence_template',external)
            self.assertIs(facts.load_evidence([{**external['evidence_template'],'evidence_id':'current','subject':'observation'}],folder,{})['current']['value'],False)
            native=poe.view_json(args(file=str(source),pointer='/calculation/stats/Observed',fields='',keys=False,query='',offset=0,limit=20))
            self.assertNotIn('evidence_template',native)
            self.assertEqual(native['native_references']['/calculation/stats/Observed'],'native:current#/stats/Observed')
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
        need={"kind":"item","item_id":1,"spec_sha256":bx.digest('spec'),"quantity":2}
        ref=facts.material_ref(need)
        quote={**need,"low":6,"high":8,"currency":"divine","evidence_refs":["p"]}
        evidence={"p":{"evidence_id":"p","kind":"artifact","subject":ref,"status":"artifact_observed","checked_at":"2026-10-04","context":{"realm":"cn","league":"test","currency":"divine","quantity":2,"low":6,"high":8,"spec_sha256":need['spec_sha256']}}}
        claims=[{"claim_id":"q","status":"model_assessed","subjects":["quote:"+ref],"evidence_refs":["p"]}]
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
    def test_empty_assessment_container_preserves_review_identity_but_scope_change_invalidates(self):
        for nested in (False,True):
            with self.subTest(nested=nested):
                specification={'plan':{'schema_version':2}} if nested else {'schema_version':2}
                first,_=facts.final_review(specification,{},[],{}, {})
                reviewed=copy.deepcopy(specification);target=reviewed['plan'] if nested else reviewed
                review={**first['review_template'],'findings':'Actual current review'}
                target['assessment']={'review':review}
                packet,pending=facts.final_review(reviewed,{},[],{},target['assessment'])
                self.assertEqual(packet['token'],first['token']);self.assertNotIn('final_review:stale',pending)
                target['assessment']['scope']='A different actual assessment scope'
                changed,pending=facts.final_review(reviewed,{},[],{},target['assessment'])
                self.assertNotEqual(changed['token'],first['token']);self.assertIn('final_review:stale',pending)

    def test_compact_review_matches_explicit_empty_lists_without_mutating_source(self):
        native={'candidate':{'stats':{'Life':123}}}
        reference='native:candidate#/stats/Life'
        claims=[{'claim_id':'life','text':'{{fact:'+reference+'}}','subjects':['goal'],'evidence_refs':[reference]}]
        observed=facts.assess_claims(claims,{},native)
        observations={'objective_coverage':[{'objective_id':'requested','text':'Current observation'}]}
        packet,_=facts.final_review({}, {}, observed, observations, {},native=native)
        review={'token':packet['token'],'findings':'Current finite observation only.',
                'conclusions':[review_row('claim_id','life','Current snapshot','Observed directly',verdict='supported',evidence_refs=[reference])],
                'objectives':[review_row('objective_id','requested','Current request','Current observation reported',verdict='supported',evidence_refs=[reference])]}
        expected,expected_pending=facts.final_review({}, {}, observed,observations,{'review':review},native=native)
        compact=copy.deepcopy(review)
        for field in ('conclusions','objectives'):
            for row in compact[field]:
                for key in ('counter_evidence_refs','conditions','unimplemented'):row.pop(key)
        original=copy.deepcopy(compact)
        actual,pending=facts.final_review({}, {}, observed,observations,{'review':compact},native=native)
        self.assertEqual(actual,expected);self.assertEqual(pending,expected_pending)
        self.assertEqual(compact,original)
        for invalid in (None,'',False):
            broken=copy.deepcopy(compact);broken['conclusions'][0]['conditions']=invalid
            with self.subTest(invalid=invalid),self.assertRaises(bx.ContractError):
                facts.final_review({}, {}, observed,observations,{'review':broken},native=native)
        for field in ('scope','findings','verdict','evidence_refs'):
            broken=copy.deepcopy(compact);broken['conclusions'][0].pop(field)
            with self.subTest(missing=field),self.assertRaises(bx.ContractError):
                facts.final_review({}, {}, observed,observations,{'review':broken},native=native)
        broken=copy.deepcopy(compact);broken['conclusions'][0]['verdict']='refuted'
        with self.assertRaises(bx.ContractError):
            facts.final_review({}, {}, observed,observations,{'review':broken},native=native)

    def test_review_current_references_render_facts_and_change_invalidates_old_result(self):
        native={'candidate':{'stats':{'Future':7},'character_identity':{'digest':'current'}}}
        observations={'objective_coverage':[{'objective_id':'g','text':'Actual requirement','claim_ids':[],'status':'unresolved'}]}
        packet,_=facts.final_review({}, {}, [], observations, {},native=native)
        row=packet['review_template']['objectives'][0]
        reference='native:candidate#/stats/Future'
        review={**packet['review_template'],'findings':'Limited measurement: {{value:'+reference+'}}',
                'objectives':[{**row,'verdict':'supported','scope':'One snapshot','findings':'{{fact:'+reference+'}}','evidence_refs':[reference]}]}
        checked,pending=facts.final_review({}, {}, [], observations, {'review':review},native=native)
        self.assertFalse(pending);self.assertIn('7',checked['objectives'][0]['findings'])
        self.assertEqual(checked['findings'],'Limited measurement: 7')
        self.assertEqual(review['findings'],'Limited measurement: {{value:'+reference+'}}')
        native['candidate']['stats']['Future']=8
        changed,pending=facts.final_review({}, {}, [], observations, {'review':review},native=native)
        self.assertIn('final_review:stale',pending);self.assertEqual(changed['objectives'],[])
        self.assertIsNone(changed['findings'])
        review['token']=changed['token'];review['findings']='Missing: {{value:native:candidate#/stats/Absent}}'
        checked,pending=facts.final_review({}, {}, [], observations, {'review':review},native=native)
        self.assertEqual(checked['status'],'unresolved')
        self.assertIn('native:candidate#/stats/Absent',checked['findings'])
        self.assertNotIn('{{value:',checked['findings'])
        self.assertEqual(checked['diagnostics'][0]['location'],'/assessment/review/findings')
        self.assertTrue(any(value.endswith(':native:candidate#/stats/Absent') for value in pending))
        review['findings']='Limited measurement: {{fact:'+reference+'}}'
        review['token']=changed['token'];review['objectives'][0]['evidence_refs']=['native:candidate#/stats/Absent']
        checked,pending=facts.final_review({}, {}, [], observations, {'review':review},native=native)
        self.assertEqual(checked['status'],'unresolved')
        self.assertTrue(checked['diagnostics'][0]['reference'].endswith('/Absent'))

    def test_review_template_requires_actual_evidence_and_keeps_unknown_goals_scoped(self):
        observations={'objective_coverage':[{'objective_id':'g','text':'Player requirement','claim_ids':[],'status':'unresolved'}]}
        packet,_=facts.final_review({}, {}, [], observations, {})
        row=packet['review_template']['objectives'][0]
        self.assertEqual(set(row),{'objective_id','scope','findings','verdict','evidence_refs'})
        review={**packet['review_template'],'findings':'Partial result only','objectives':[{**row,'scope':'This finite window',
                'findings':'Not measured','verdict':'unknown','unimplemented':['No relevant measured result']}]}
        checked,pending=facts.final_review({}, {}, [], observations, {'review':review})
        self.assertIn('objective:g:review:unknown',pending)
        self.assertEqual(checked['status'],'model_reviewed')
        self.assertEqual(checked['objectives'][0]['verdict'],'unknown')
        review['objectives'][0]['verdict']='supported'
        with self.assertRaises(bx.ContractError): facts.final_review({}, {}, [], observations, {'review':review})

    def test_current_timeline_publication_reports_native_name_per_skill_count_and_window_rate(self):
        result=resource_fixture();result.update({'class':'FutureClass','ascendancy':'FutureAsc','ascendancy_cn':'本机升华'})
        result['active_skills'][0].update(name='FutureSkill',display_name='本机技能',costs={'ManaCost':0,'ManaPerSecondCost':0,'LifeCost':7,'LifePerSecondCost':14,'ESCost':0,'ESPerSecondCost':0},usage_rate={'value':2})
        result['resource_rules']={'energy_shield_protects_mana':False}
        audit=resources.assess(result,{}, {'timeline':{'duration_seconds':3,'repeat':False,'events':[{'at':0,'skill_ref':'main'},{'at':0.5,'skill_ref':'main'}]}})
        packet=poe.publication('draft',[],['future'],{'status':'unknown','limit':None},{'candidate':result},readiness=audit,keys=[])
        schedule=next(s for s in packet['statements'] if s['kind']=='skill_schedule')
        self.assertEqual(schedule['values']['event_count'],2)
        self.assertAlmostEqual(schedule['values']['window_average_rate'],2/3)
        self.assertEqual(schedule['values']['scheduled_payments']['Life'],14)
        self.assertIn('Life 14',schedule['text'])
        life=next(s for s in packet['statements'] if s['kind']=='resource' and s['subject']=='Life')
        self.assertEqual(life['window']['scheduled_payment_total'],14)
        self.assertIn('本机技能',schedule['text'])
        self.assertIn('本机升华',packet['facts_summary'])

    def test_character_identity_tracks_effects_specs_and_real_config_but_not_transport(self):
        value={'class':'fixture','ascendancy':'fixture','level':12,'tree_version':'future',
               'nodes':[2,1],'main_skill':'future-effect','main_group':1,
               'config_values':{'native_default':False},'config_saved_values':{},
               'gems':[{'group':1,'index':1,'id':'future-gem','enable_global1':False,'enable_global2':True}],
               'equipped':[{'slot':'Boots','id':1,'name':'fixture','base':'fixture','text':'actual spec','active':False}],
               'input_xml_sha256':'requested','xml_sha256':'saved'}
        identity=engine.character_state(value)
        changed=copy.deepcopy(value);changed.update(input_xml_sha256='reloaded',xml_sha256='reformatted');changed['nodes'].reverse()
        self.assertEqual(engine.character_state(changed),identity)
        for section,key,new in [('gems','enable_global1',True),('equipped','text','different spec')]:
            changed=copy.deepcopy(value);changed[section][0][key]=new
            self.assertNotEqual(engine.character_state(changed),identity)
        changed=copy.deepcopy(value);changed['config_saved_values']['non_default']=False
        self.assertNotEqual(engine.character_state(changed),identity)

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
        for fixed in (15,75,95):
            result['stats']['NetManaRegen']=fixed
            audit=resources.assess(result,{}, {})
            packet=poe.publication('draft',[],audit['pending'],{'status':'unknown'},{'candidate':result},readiness=audit,keys=[])
            published=next(row for row in packet['statements'] if row['kind']=='single_skill_mean'
                           and row['skill_ref']=='secondary' and row['resource_id']=='Mana')
            actual=next(row for row in audit['single_payments'] if row['skill_ref']=='secondary' and row['resource_id']=='Mana')
            self.assertEqual(published['values'],actual['mean_use'])
            self.assertEqual(published['actor'],actual['actor'])
            self.assertIn(str(75-fixed),published['text'])
            self.assertIn(published['text'],packet['facts_summary'])
            self.assertEqual(packet['resource_audits']['candidate']['sustained_use']['status'],'unknown')
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
        diagnostic=checked['diagnostics'][0]
        self.assertEqual(diagnostic['code'],'review_coverage_incomplete')
        self.assertEqual(diagnostic['actual']['missing_claim_ids'],['one','two'])
        self.assertEqual(diagnostic['expected']['template_pointer'],'/review_template/conclusions')
        actions=poe.delivery_actions({'status':'draft','errors':[],'pending':pending,'diagnostics':checked['diagnostics']},'input.json','details.json','review.json')
        self.assertEqual(actions[0]['action'],'repair_input')
        self.assertEqual(actions[0]['pointer'],'/diagnostics')
        review["conclusions"]=[review_row('claim_id','one','one snapshot only','Observed, no temporal promise.')]
        self.assertEqual(facts.final_review({}, {}, claims, {}, {"review":review})[0]["coverage"]["missing_claim_ids"],["two"])
        review["conclusions"].append(review_row('claim_id','two','user preference','Interpretation, not a measured guarantee.'))
        self.assertFalse(facts.final_review({}, {}, claims, {}, {"review":review})[1])
        review["conclusions"].append(review_row('claim_id','unknown','unrelated','Cannot count this as current coverage.'))
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
                'conclusions':[review_row('claim_id','one','Current object','This observation is scoped.')]}
        checked,pending=facts.final_review({}, {}, claims, observations, {'review':review})
        self.assertEqual(checked['status'],'unresolved')
        self.assertEqual(checked['coverage']['missing_objective_ids'],['custom-first','custom-later'])
        diagnostic=checked['diagnostics'][0]
        self.assertEqual(diagnostic['code'],'review_coverage_incomplete')
        self.assertEqual(diagnostic['location'],'/assessment/review/objectives')
        self.assertEqual(diagnostic['actual']['missing_objective_ids'],['custom-first','custom-later'])
        self.assertEqual([row['objective_id'] for row in packet['review_template']['objectives']],['custom-first','custom-later'])
        self.assertIn('final_review:objective:custom-later',pending)
        review['objectives']=[review_row('objective_id',row['objective_id'],'Actual player requirement','Measured scope reviewed; missing dependencies remain.') for row in objectives]
        checked,pending=facts.final_review({}, {}, claims, observations, {'review':review})
        self.assertEqual(checked['status'],'model_reviewed');self.assertIn('objective:custom-later:review:unknown',pending)
        self.assertEqual(checked['facts']['pending'],['objective:custom-later'])
        self.assertEqual(checked['facts']['objective_coverage'][1]['status'],'unresolved')
        review['objectives'].append(review_row('objective_id','foreign','Different requirement','Cannot substitute this goal.'))
        self.assertIn('final_review:unknown_objective:foreign',facts.final_review({}, {}, claims, observations, {'review':review})[1])
        review['objectives'].pop();review['objectives'].append(copy.deepcopy(review['objectives'][0]))
        with self.assertRaises(bx.ContractError) as caught:
            facts.final_review({}, {}, claims, observations, {'review':review})
        self.assertEqual(caught.exception.diagnostic['location'],'/assessment/review/objectives/2/objective_id')
        observations['objective_coverage'].append({'objective_id':'added','text':'New requirement','claim_ids':[],'status':'unresolved'})
        review['objectives'].pop()
        self.assertIn('final_review:stale',facts.final_review({}, {}, claims, observations, {'review':review})[1])
        self.assertEqual(objectives[1]['status'],'unresolved')

    def test_review_retains_declared_user_basis_separate_from_model_objectives(self):
        original={'evidence_id':'request','kind':'user_statement','text':'Original user request, preserved verbatim.',
                  'origin':'agent_recorded_user_statement','status':'user_declared','message_ref':'visible user message'}
        evidence={'request':original,'inventory':{'kind':'user_statement','text':'Unrelated ownership declaration','status':'user_declared'}}
        observations={'objective_coverage':[{'objective_id':'first','text':'Model interpretation differs.','evidence_refs':['request']},
                                             {'objective_id':'second','text':'Another interpretation.','evidence_refs':['request']}]}
        before=copy.deepcopy(observations)
        packet,_=facts.final_review({},evidence,[],observations,{})
        sources=packet['facts']['user_goal_sources']
        self.assertEqual(len(sources),1)
        self.assertEqual(sources[0]['evidence_id'],'request')
        self.assertEqual(sources[0]['text'],original['text'])
        self.assertEqual(sources[0]['origin'],original['origin'])
        self.assertEqual(sources[0]['status'],'user_declared')
        self.assertEqual(sources[0]['message_ref'],original['message_ref'])
        self.assertEqual(packet['facts']['objective_coverage'],before['objective_coverage'])
        self.assertEqual(observations,before)
        review={**packet['review_template'],'findings':'Interpretations remain to be reviewed.',
                'objectives':[{**row,'scope':'This objective','findings':'Not measured','verdict':'unknown',
                               'unimplemented':['Evidence still needed']} for row in packet['review_template']['objectives']]}
        evidence['request']={**original,'text':'A changed original request.'}
        changed,pending=facts.final_review({},evidence,[],observations,{'review':review})
        self.assertIn('final_review:stale',pending)
        self.assertEqual(changed['facts']['user_goal_sources'][0]['text'],evidence['request']['text'])

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
        result["stats"]["ManaUnreserved"]=-3
        result["active_skills"][1].update(trigger=True)
        result["active_skills"][1]["costs"].update(SkillTriggerRate=1,ManaPerSecondCost=70)
        audit=resources.assess(result,{}, {})
        failure=next(row for row in audit['diagnostics'] if row.get('payment_facts') and row['subject']=='resource:Mana')
        self.assertEqual([(row['skill_ref'],row['payment'],row['pool']) for row in failure['payment_facts']],[('aux',70,-3)])
        self.assertIn(failure['message'],audit['errors'])
        self.assertEqual(failure['category'],'usage')

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
        self.assertEqual(engine.character_state(explicit),engine.character_state(omitted))
        changed={**common,"config_values":{},"config_saved_values":{}}
        self.assertNotEqual(engine.character_state(explicit),engine.character_state(changed))

    def test_review_binds_facts_conditions_prose_and_source_content(self):
        spec={"summary":"A conditional recommendation","assessment":{"scope":"fixture"}}
        claims=[{"claim_id":"c","text":"Main skill only","subjects":["goal"],"evidence_refs":["native:candidate#/stats/Life"]}]
        evidence={"source":{"status":"artifact_observed","digest":"one","recorded_at":"earlier"}}
        native={"candidate":{"stats":{"Life":100},"xml_sha256":"a","engine_fingerprint":"v"}}
        assessed=facts.assess_claims(claims,evidence,native)
        packet,pending=facts.final_review(spec,evidence,assessed,native,spec["assessment"])
        self.assertIn("final_review:missing",pending)
        spec["assessment"]["review"]={"token":packet["token"],"findings":"Checked object, numbers, causality, alternatives, conditions and scope.","unresolved":[],"conclusions":[review_row('claim_id','c','Main skill snapshot only','Source, object and current values match; no temporal guarantee.')]}
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
        native={'candidate':{'stats':{'FutureValue':7}}}
        reference='native:candidate#/stats/FutureValue'
        observations={'objective_coverage':[{'objective_id':'observed'},{'objective_id':'later'}]}
        packet,_=facts.final_review({}, {}, [], observations, {},native=native)
        observed=review_row('objective_id','observed','One current state','{{fact:'+reference+'}}',
                            verdict='supported',evidence_refs=[reference])
        observed['conditions']=['One measured window only']
        later=review_row('objective_id','later','Another requirement','Temporal coverage unknown.',
                         unimplemented=['No relevant timeline'])
        review={"review":{"token":packet["token"],"findings":"Temporal coverage unknown.",
                          "unresolved":["coverage"],"objectives":[observed,later]}}
        checked,pending=facts.final_review({}, {}, [], observations, review,native=native)
        self.assertEqual(checked["status"],"unresolved");self.assertIn("final_review:coverage",pending)
        published=poe.publication('draft',[],pending,{'status':'unknown','limit':None},native,review=checked,keys=[])
        rows={row['subject']:row for row in published['statements'] if row['kind']=='objective_review'}
        self.assertEqual(rows['observed']['status'],'supported')
        self.assertIn('One current state',rows['observed']['text'])
        self.assertIn('FutureValue = 7',rows['observed']['text'])
        self.assertIn('Temporal coverage unknown.',published['facts_summary'])
        self.assertIn('One measured window only',rows['observed']['text'])
        self.assertEqual(rows['observed']['evidence_scope']['counts'],{'native_observed':1})
        self.assertEqual(rows['later']['status'],'unknown')
        self.assertIn('No relevant timeline',rows['later']['text'])
        self.assertEqual(published['delivery_brief']['status'],'draft')
        later.update(verdict='supported',evidence_refs=['native:candidate#/stats/Absent'])
        checked,pending=facts.final_review({}, {}, [], observations, review,native=native)
        published=poe.publication('draft',[],pending,{'status':'unknown','limit':None},native,review=checked,keys=[])
        rows={row['subject']:row for row in published['statements'] if row['kind']=='objective_review'}
        self.assertEqual(rows['observed']['status'],'supported')
        self.assertEqual(rows['later']['status'],'unknown')
        self.assertEqual(rows['later']['reference_status'],'unresolved')
        self.assertIn('Another requirement',rows['later']['text'])
        later.update(evidence_refs=['native:candidate#/stats'],findings='{{value:native:candidate#/stats}}')
        checked,pending=facts.final_review({}, {}, [], observations, review,native=native)
        published=poe.publication('draft',[],pending,{'status':'unknown','limit':None},native,review=checked,keys=[])
        rows={row['subject']:row for row in published['statements'] if row['kind']=='objective_review'}
        self.assertEqual(rows['observed']['status'],'supported')
        self.assertEqual(rows['later']['reference_status'],'unresolved')
        native['candidate']['stats']['FutureValue']=8
        checked,pending=facts.final_review({}, {}, [], observations, review,native=native)
        self.assertIn('final_review:stale',pending)
        published=poe.publication('draft',[],pending,{'status':'unknown','limit':None},native,review=checked,keys=[])
        self.assertFalse(any(row['kind']=='objective_review' for row in published['statements']))

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
            for count in (40,100):
                dense={**long,**{'observation-'+str(i):['current native observation '*200] for i in range(count)}}
                poe.write_json(artifact,{'response':dense});before=artifact.read_bytes();output=io.StringIO()
                with patch.object(poe,'evaluate',return_value=dense),redirect_stdout(output):
                    code=poe.main(['evaluate','--run',str(run),'--name','test','--plan','fixture.json'])
                self.assertEqual(code,0,output.getvalue())
                self.assertLess(len(output.getvalue()),12000)
                reply=json.loads(output.getvalue())
                self.assertEqual(reply['status'],'draft')
                self.assertEqual(artifact.read_bytes(),before)
                saved=poe.read_json(reply['details_file'])
                self.assertTrue(reply['continuations'])
                for row in reply['continuations']: facts.pointer(saved,row['pointer'])
                # A saved parent is a valid recovery path for every child; the
                # metadata enumerating omitted fields must itself remain bounded.
                for key in dense:
                    if key in reply and reply[key]==dense[key]: continue
                    pointer='/response/'+key
                    self.assertTrue(any(pointer==row['pointer'] or pointer.startswith(row['pointer']+'/')
                                        for row in reply['continuations']),key)

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
    def test_successful_export_files_survive_terminal_envelope_reduction(self):
        with tempfile.TemporaryDirectory() as folder:
            run=Path(folder);artifact=run/'output/final/final-assessment.json'
            files=[str(run/'output/final/role.xml'),str(run/'output/final/report.html')]
            response={'status':'scoped_result','file_exported':True,'xml_exported':True,'files':files,
                      'facts_summary':'Observed current fact. '*300,
                      'reply_policy':'Use the current delivered files and their limits. '*45,
                      'delivery_brief':{'scope':'Current measured window; other conditions unknown. '*60},
                      'publication':{'scope':'window '*600},
                      'entrypoint':{'script':'current entrypoint','python':'current Python','skill_version':poe.VERSION},
                      'runtime':{'elapsed_seconds':1},'extra_observations':['Current observation. '*1000]}
            poe.write_json(artifact,{'response':response});original=artifact.read_bytes()
            reply=poe.bound_reply(response,args(command='export',run=str(run),name='final'))
            self.assertEqual(reply['files'],files)
            self.assertTrue(reply['file_exported']);self.assertTrue(reply['xml_exported'])
            self.assertLess(len(json.dumps({'ok':True,**reply},ensure_ascii=False))+1,12000)
            self.assertEqual(artifact.read_bytes(),original)
            for row in reply['continuations']:facts.pointer(poe.read_json(artifact),row['pointer'])

            report=run/'route.html';details=report.with_suffix('.evidence.json')
            files=[str(run/('materialized-stage-'+str(index)+'-'+'x'*120)/'snapshot.xml') for index in range(90)]
            response={'status':'scoped_result','file_exported':True,'files':files,
                      'facts_summary':'Current measured observations. '*300,'reply_policy':'Use current saved files and declared limits.'}
            poe.write_json(details,{'response':response});original=details.read_bytes()
            stdout,stderr=io.StringIO(),io.StringIO()
            with patch.object(poe,'route_command',return_value=copy.deepcopy(response)),redirect_stdout(stdout),redirect_stderr(stderr):
                code=poe.main(['route','--file',str(run/'route.json'),'--out',str(report)])
            self.assertEqual(code,0,stderr.getvalue())
            reply=json.loads(stdout.getvalue())
            self.assertTrue(reply['ok']);self.assertTrue(reply['file_exported'])
            self.assertTrue(reply['files_is_preview']);self.assertEqual(reply['files_total'],len(files))
            self.assertEqual(reply['files'],files[:len(reply['files'])]);self.assertLess(len(reply['files']),len(files))
            self.assertLess(len(stdout.getvalue()),12000)
            continuation=next(row for row in reply['continuations'] if row['pointer']=='/response/files')
            self.assertEqual(facts.pointer(poe.read_json(continuation['file']),continuation['pointer']),files)
            self.assertEqual(details.read_bytes(),original)

    def test_nonstandard_native_resource_observations_reach_facts_and_diagnosis(self):
        current={'stats':{'LifeOnFutureEvent':7,'ManaFutureGain':-2,'OtherOutput':9},
                 'points':0,'ascendancy_points':0,'active_skills':[]}
        projected=poe.calculation_facts(current)
        self.assertEqual(projected['stats'],{'LifeOnFutureEvent':7,'ManaFutureGain':-2})
        summary=poe.diagnosis_summary(current)
        self.assertIn('LifeOnFutureEvent',summary);self.assertIn('ManaFutureGain',summary)
        with patch.object(poe,'skill_statements',return_value=[{'kind':'skill','text':'Long skill detail '*1000}]):
            summary=poe.diagnosis_summary(current)
        self.assertLess(summary.index('LifeOnFutureEvent'),summary.index('Long skill detail'))
        review={'objectives':[{'objective_id':'goal','reference_status':'resolved','verdict':'supported',
                              'evidence_scope':{'counts':{}},'scope':'Declared scope','findings':'Model interpretation '*1000,
                              'evidence_refs':[],'counter_evidence_refs':[],'conditions':[],'unimplemented':[]}]}
        current['equipped']=[{'slot':'Flask 1','active':True}]
        audit={'execution_coverage':{'status':'unknown','payment_schedule_status':'not_requested','scope':'Schedule not measured'}}
        published=poe.publication('draft',[],[],{'status':'unknown'},{'candidate':current},review=review,readiness=audit)
        self.assertEqual([row['metric'] for row in published['measurements']],['LifeOnFutureEvent','ManaFutureGain'])
        self.assertIn(poe.effect_statement(current,'candidate',compact=True)['text'],published['facts_summary'][:1500])
        self.assertIn('not_requested',published['facts_summary'][:1500])
        materials=[{'requirement_id':'service:generic-material-'+str(index)+'-specification',
                    'quantity':1,'declared_owned':1,'remaining':0} for index in range(120)]
        acquisition={'status':'user_declared','materials':materials}
        material_publication=poe.publication('draft',[],[],acquisition,{'candidate':current},readiness=audit)
        self.assertIn(poe.effect_statement(current,'candidate',compact=True)['text'],material_publication['facts_summary'][:1500])
        self.assertIn('not_requested',material_publication['facts_summary'][:1500])
        material_text=next(row['text'] for row in material_publication['statements'] if row['kind']=='budget')
        self.assertNotIn('原 XML',material_text)
        self.assertNotIn('额外库存已声明',material_text)
        self.assertEqual(acquisition['materials'],materials)
        long_current={**current,'stats':{**current['stats'],**{key:1 for key in poe.METRICS}}}
        short_review=copy.deepcopy(review);short_review['objectives'][0]['findings']='Requested outcome remains unimplemented'
        long_publication=poe.publication('draft',[],[],{'status':'unknown'},
                                       {str(i):long_current for i in range(8)},review=short_review)
        self.assertIn('Requested outcome remains unimplemented',long_publication['facts_summary'][:5000])
        self.assertIn('OtherOutput',[row['metric'] for row in poe.publication('draft',[],[],{'status':'unknown'},
                      {'candidate':current},keys=['OtherOutput'])['measurements']])
        self.assertEqual(poe.publication('draft',[],[],{'status':'unknown'},{'candidate':current},keys=[])['measurements'],[])

    def test_config_input_origin_distinguishes_active_inputs_placeholders_and_runtime(self):
        document=bx.parse(SAMPLE)
        config=document.find('Config');config.set('activeConfigSet','second')
        old=ET.SubElement(config,'ConfigSet',{'id':'first'})
        ET.SubElement(old,'Input',{'name':'inactive-field','boolean':'true'})
        current=ET.SubElement(config,'ConfigSet',{'id':'second'})
        ET.SubElement(current,'Input',{'name':'future-switch','boolean':'false'})
        ET.SubElement(current,'Input',{'name':'future-count','number':'0'})
        ET.SubElement(current,'Input',{'name':'future-text','string':'false'})
        ET.SubElement(current,'Placeholder',{'name':'future-default','number':'7'})
        xml=bx.serialise(document)
        origin=engine.config_input_origin(xml)
        self.assertEqual(origin['active_set_id'],'second')
        self.assertEqual(origin['inputs'],{'future-switch':False,'future-count':0,'future-text':'false'})
        self.assertEqual(origin['placeholders'],{'future-default':7})
        self.assertEqual(origin['input_xml_sha256'],bx.digest(xml))
        self.assertNotIn('inactive-field',origin['inputs'])
        self.assertEqual(engine.config_input_origin('')['status'],'no_input_xml')
        legacy=bx.parse(SAMPLE)
        ET.SubElement(legacy.find('Config'),'Input',{'name':'legacy-field','boolean':'true'})
        self.assertEqual(engine.config_input_origin(bx.serialise(legacy))['inputs'],{'legacy-field':True})
        self.assertNotIn('ConfigSet',ET.tostring(legacy,encoding='unicode'))

    def test_view_exposes_actual_json_artifact_template_without_paraphrasing(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);source=folder/'catalog.json'
            poe.write_json(source,{'entries':[{'id':'FutureObject','description':'Actual native text','observed':False}]})
            page=poe.view_json(args(file=str(source),pointer='/entries/0',fields='id',keys=False,query='',offset=0,limit=1))
            declaration={**page['evidence_template'],'evidence_id':'current-object','subject':'fixture'}
            record=facts.load_evidence([declaration],folder,{})['current-object']
            self.assertEqual(record['value']['description'],'Actual native text');self.assertIs(record['value']['observed'],False)
            self.assertEqual(declaration['pointer'],'/entries/0');self.assertNotIn('excerpt',declaration)
            poe.write_json(source,{'reference_contract':facts.reference_contract({'candidate':{'stats':{'FutureMetric':1}}})})
            native=poe.view_json(args(file=str(source),pointer='',fields='',keys=False,query='',offset=0,limit=1))
            self.assertNotIn('evidence_template',native)

    def test_blocked_report_does_not_instruct_import_of_unexported_xml(self):
        budget={'status':'unknown','materials':[],'totals_complete':False,'low':0,'high':0,'currency':None,'limit':None}
        outcome={'plan':{'summary':'Candidate'},'result':{},'budget':budget,'status':'blocked','errors':['Known contradiction'],'pending':[],'warnings':[],
                 'comparison':{},'readiness':{'resources':[]},'objective_coverage':[],'claims':[],'evidence':{},'actual_changes':[],'comparison_notes':[],
                 'publication':poe.publication('blocked',['Known contradiction'],[],budget,{}),'validation':{'scope':'Structural checks only'},
                 'name':'candidate','engine_version':'fixture','xml_sha256':'fixture','engine_fingerprint':'fixture'}
        page=poe.report_html({'intent':{'goal':'Actual player goal'}},outcome)
        self.assertNotIn('在 PoECharm 中打开同目录 XML',page);self.assertIn('未导出交付文件',page)
        actions=poe.delivery_actions(outcome,'plan.json','evaluation.json','review.json')
        self.assertFalse(any(row['action']=='export' for row in actions))
        self.assertEqual(outcome['errors'],['Known contradiction']);self.assertEqual(outcome['status'],'blocked')
        self.assertFalse(any(row['action']=='export' for row in poe.delivery_actions(outcome,'plan.json','final-assessment.json','review.json',report_exported=True)))
        self.assertFalse(any(row['action']=='export' for row in poe.delivery_actions(outcome,'route.json','route.json','review.json','/checked_route')))

    def test_readonly_report_uses_current_publication_and_preserves_unknown_controls(self):
        native={'class':'New Class','class_id':17,'points':2,'ascendancy_points':0,'stats':{'CombinedDPS':91,'GuardSkillActive':True},
                'config_values':{'new-scene':'boss'},'config_placeholders':{'new-mode':3},
                'config_loader_source':{'source':'engine/Classes/CurrentLoader.lua','line':21,'count':4,'total_lines':4},
                'main_group':1,'config_labels':{},'gems':[
                    {'group':1,'index':1,'id':'NewSkill','name':'New native skill','display_name':None,'slot':'Current Slot','group_enabled':True,'slot_enabled':False,'enabled':True,
                     'granted_effects':[{'id':'NewSkill','global_enabled':False,'active_skill_present':False},
                                        {'id':'OtherEffect','global_enabled':True,'active_skill_present':True}]},
                    {'group':1,'index':2,'id':'NewSupport','name':'New native support','display_name':'当次辅助（New native support）','slot':'Current Slot','group_enabled':True,'slot_enabled':False,'enabled':False,'support':True,'support_applied':False}],
                'active_skills':[{'group':1,'index':1,'id':'NewSkill','name':'New native skill','skill_ref':'1:1:NewSkill:mirage:gem:1','actor':'mirage','is_main':True,
                    'description':'Current native main definition <guard & effect>.',
                    'supports':[{'id':'FutureExtraSupport','name':'Future current native support','level':None,'quality':None,'effective_level':8,'effective_quality':3}],
                    'supports_source':'Current native applied effect list',
                    'usage_rate':{'value':3.75,'metric':'CurrentRate','basis':'native_action_rate','scope':'Current native rate only'},'costs':{'LifeCost':2.5,'LifePerSecondCost':9.375,'ManaCost':0},'cost_scope':'Current actor costs only'},
                    {'group':1,'index':1,'id':'OtherEffect','name':'Other native effect','skill_ref':'1:1:OtherEffect:minion:gem:1','actor':'minion','is_main':False,
                     'description':'Current native secondary definition: expires after its observed duration.','trigger':'CurrentNativeTrigger',
                     'benefit_excluded':True,
                     'usage_rate':{'value':None,'metric':None},'costs':{'ManaCost':4,'ManaPerSecondCost':None},'cost_scope':'Native player output only',
                     'native_timing':{'cooldown_seconds':2,'duration_seconds':None,'source':'Native player output only'}}],
                'config_controls':{'new-mode':{'id':'new-mode','type':'list','value':None,'default':1,'shown':False,'if_cond':'FutureCondition',
                    'input_origin':{'status':'input_xml_observed','input_present':False,'placeholder_present':True,'placeholder_value':1},
                    'runtime_placeholder_present':True,'runtime_placeholder_value':3,
                    'options':[{'val':1,'label':'Current default'},{'val':2,'label':'Other'}]}}}
        with tempfile.TemporaryDirectory() as folder:
            original=Path(folder)/'original.xml';original.write_text(SAMPLE,encoding='utf-8');output=Path(folder)/'current.json';report=Path(folder)/'current.html'
            original_bytes=original.read_bytes();stdout=io.StringIO()
            with patch.object(engine,'calculate',return_value=native),patch.object(engine,'resolve_root',return_value=Path(folder)),redirect_stdout(stdout):
                code=poe.main(['inspect','--input',str(original),'--calculate','--out',str(output),'--report',str(report)])
            self.assertEqual(code,0,stdout.getvalue())
            saved=poe.read_json(output);receipt=json.loads(stdout.getvalue())
            self.assertEqual(saved['reference_contract']['current']['artifact'],{'file':str(output),'pointer':'/calculation'})
            self.assertEqual(saved['reference_contract']['resources']['artifact'],{'file':str(output),'pointer':'/readiness'})
            self.assertIsNone(saved['readiness']['timeline'])
            self.assertEqual(saved['readiness']['sustained_use']['basis'],'no_common_timeline')
            self.assertTrue(any(row['kind']=='single_payment' for row in saved['publication']['statements']))
            self.assertEqual(next(row for row in saved['publication']['statements'] if row['kind']=='sustained_use')['status'],'unknown')
            self.assertIs(saved['calculation']['config_controls']['new-mode']['value'],None)
            published=receipt['publication'].get('statements')
            if published is None:
                continuation=next(row for row in receipt['continuations'] if row['pointer']=='/publication')
                published=[];offset=0
                while offset is not None:
                    chunk=poe.view_json(args(file=continuation['file'],pointer=continuation['pointer']+'/statements',fields='',keys=False,query='',offset=offset,limit=1))
                    self.assertFalse(chunk.get('deferred_values'))
                    published.extend(chunk['value']);offset=chunk['next_offset']
            self.assertEqual(saved['publication']['statements'],published)
            self.assertEqual(saved['publication']['facts_summary'].split('\n\n',1)[0],saved['publication']['delivery_brief']['message'])
            self.assertEqual(saved['publication']['statements'][0]['text'],saved['publication']['delivery_brief']['message'])
            self.assertEqual(receipt['report'],str(report))
            self.assertEqual(receipt['reply_policy'],poe.READONLY_REPLY_POLICY)
            self.assertEqual(saved['reply_policy'],receipt['reply_policy'])
            action=next(row for row in receipt['next_actions'] if row['pointer']=='/reference_contract')
            self.assertEqual(action['action'],'view');self.assertEqual(action['file'],str(output))
            self.assertEqual(saved['next_actions'],receipt['next_actions'])
            self.assertEqual(action['explanation_reference'],'references/build.md#只读解释')
            group=next(row for row in saved['publication']['statements'] if row['kind']=='native_skill_group')
            main=next(row for row in saved['publication']['statements'] if row['kind']=='damage_subject')
            self.assertIn('当次辅助（New native support）',receipt['publication']['facts_summary'])
            self.assertEqual(group['values']['gems'][1]['id'],'NewSupport')
            self.assertIs(group['values']['gems'][1]['support_applied'],False)
            self.assertIs(group['values']['gems'][0]['slot_enabled'],False)
            self.assertEqual(main['values']['usage_rate'],native['active_skills'][0]['usage_rate'])
            self.assertEqual(main['values']['costs'],native['active_skills'][0]['costs'])
            self.assertEqual(main['values']['description'],native['active_skills'][0]['description'])
            self.assertIn(native['active_skills'][0]['description'],receipt['publication']['facts_summary'])
            self.assertEqual(main['values']['supports'],native['active_skills'][0]['supports'])
            self.assertIn('Future current native support',main['text'])
            self.assertIn('3.75',main['text']);self.assertIn('mirage',main['text'])
            secondary=next(row for row in saved['publication']['statements'] if row['kind']=='native_skill_observation')
            self.assertEqual(secondary['reference'],'native:current#/active_skills/1')
            self.assertEqual(secondary['skill_ref'],native['active_skills'][1]['skill_ref'])
            self.assertEqual(secondary['values']['costs'],{'ManaCost':4,'ManaPerSecondCost':None})
            self.assertEqual(secondary['actor'],'minion')
            self.assertEqual(secondary['values']['description'],native['active_skills'][1]['description'])
            self.assertIn(native['active_skills'][1]['description'],secondary['text'])
            self.assertEqual(secondary['values']['trigger'],'CurrentNativeTrigger')
            effect=next(row for row in saved['publication']['statements'] if row['kind']=='effect_state')
            self.assertIs(effect['values']['guard_active'],True)
            self.assertIn('GuardSkillActive',effect['text']);self.assertIn('守护技能',effect['text'])
            self.assertIs(secondary['values']['benefit_excluded'],True)
            self.assertIn('条件审计',secondary['text'])
            self.assertEqual(group['values']['gems'][0]['granted_effects'],native['gems'][0]['granted_effects'])
            page=report.read_text(encoding='utf-8')
            self.assertIn('New native skill',page);self.assertNotIn('None',page)
            self.assertIn(html.escape(group['text']),page)
            self.assertIn(html.escape(secondary['text']),page)
            self.assertIn(html.escape(main['text']),page)
            self.assertIn('Native player output only',page)
            self.assertIn('OtherEffect',page)
            self.assertIn('未知',page);self.assertIn('Current default',page)
            self.assertIn('可见性与关联条件',page);self.assertIn('FutureCondition',page)
            self.assertIn('XML 输入来源',page);self.assertIn('input_present',page)
            self.assertIn('运行时占位值',page);self.assertIn('runtime_placeholder_value',page)
            self.assertIn('config_placeholders',page);self.assertIn('engine/Classes/CurrentLoader.lua',page)
            self.assertIn('new-scene',page);self.assertIn('boss',page)
            self.assertIn('只读',page);self.assertNotIn('在 PoECharm 中打开同目录 XML',page)
            self.assertEqual(original.read_bytes(),original_bytes)

    def test_large_readonly_reply_keeps_current_facts_and_paginates_details(self):
        native={'class':'Current native class','class_id':17,'engine_version':'current.engine',
                'data_version':'current.data','tree_version':'current.tree','engine_fingerprint':'current fingerprint',
                'points':2,'ascendancy_points':0,'stats':{'Life':91},'main_group':4,
                'gems':[{'group':4,'index':1,'id':'CurrentSupport','name':'Current Native Support','name_cn':'当次辅助名','display_name':'当次辅助名（Current Native Support）'}],
                'active_skills':[{'is_main':True,'id':'CurrentMain','name':'Current Main','actor':'player','skill_ref':'4:1:CurrentMain:player:gem:1',
                    'usage_rate':{'value':3.75,'metric':'CurrentRate','scope':'Native scope'},'costs':{'FutureCost':2.5}}],
                'config_values':{f'field-{i}':'Native context '*40 for i in range(100)},
                'config_controls':{f'field-{i}':{'type':'count','value':None,'tooltip':'Native context '*40} for i in range(100)}}
        with tempfile.TemporaryDirectory() as folder:
            original=Path(folder)/'original.xml';original.write_text(SAMPLE,encoding='utf-8')
            output=Path(folder)/'current.json';report=Path(folder)/'current.html';stdout=io.StringIO()
            with patch.object(engine,'calculate',return_value=native),patch.object(engine,'resolve_root',return_value=Path(folder)),redirect_stdout(stdout):
                code=poe.main(['inspect','--input',str(original),'--calculate','--out',str(output),'--report',str(report)])
            self.assertEqual(code,0)
            receipt=json.loads(stdout.getvalue());saved=poe.read_json(output)
            self.assertLess(stdout.getvalue().index('"reply_policy"'),stdout.getvalue().index('"publication"'))
            self.assertEqual(receipt['publication']['facts_summary'],saved['publication']['facts_summary'])
            self.assertEqual(receipt['publication']['delivery_brief'],saved['publication']['delivery_brief'])
            self.assertIn('current.engine',receipt['diagnosis_summary'])
            self.assertIn('current.data',receipt['publication']['facts_summary'])
            self.assertIn('当次辅助名',receipt['publication']['facts_summary'])
            self.assertIn('3.75',receipt['publication']['facts_summary'])
            self.assertLessEqual(len(stdout.getvalue()),12000)
            self.assertTrue(any(row['pointer']=='/publication' for row in receipt['continuations']))
            for row in receipt['continuations']:facts.pointer(saved,row['pointer'])
            saved['publication']['facts_summary']='Current fact line.\n'*1000
            poe.write_json(output,saved)
            projected=poe.bound_reply({'publication':saved['publication'],'inspection':str(output)},args(command='inspect',out=str(output)))
            shown=projected['publication']['facts_summary']
            self.assertTrue(saved['publication']['facts_summary'].startswith(shown))
            continuation=next(row for row in projected['continuations'] if row['pointer']=='/publication/facts_summary')
            self.assertEqual(continuation['offset'],len(shown))
            self.assertLessEqual(len(json.dumps(projected,ensure_ascii=False)),12000)
            saved['diagnosis_summary']='Current diagnosis line.\n'*1000
            poe.write_json(output,saved)
            projected=poe.bound_reply({'inspection':str(output),'report':str(report),'build':saved['build'],
                'publication':saved['publication'],'diagnosis_summary':saved['diagnosis_summary'],
                'reply_policy':poe.READONLY_REPLY_POLICY,'entrypoint':{'script':str(Path(poe.__file__).resolve()),'python':sys.executable}},args(command='inspect',out=str(output)))
            self.assertEqual(next(iter(projected)),'reply_policy')
            self.assertEqual(projected['reply_policy'],poe.READONLY_REPLY_POLICY)
            self.assertLessEqual(len(json.dumps(projected,ensure_ascii=False)),12000)
            for owner,key,pointer in [(projected,'diagnosis_summary','/diagnosis_summary'),
                                      (projected['publication'],'facts_summary','/publication/facts_summary')]:
                shown=owner[key];original=facts.pointer(saved,pointer)
                self.assertTrue(original.startswith(shown));self.assertGreater(len(shown),160)
                continuation=next(row for row in projected['continuations'] if row['pointer']==pointer)
                self.assertEqual(continuation['offset'],len(shown))
            saved['publication']={'facts_summary':'Current fact. '*320,'scope':'Current scope','delivery_brief':{'status':'scoped_result'}}
            saved['diagnosis_summary']='Current diagnosis line.\n'*150
            poe.write_json(output,saved)
            original_publication=dict(saved['publication'])
            projected=poe.bound_reply({'inspection':str(output),'publication':saved['publication'],
                'diagnosis_summary':saved['diagnosis_summary'],'reply_policy':'Current reply policy. '*200},args(command='inspect',out=str(output)))
            self.assertEqual(saved['publication'],original_publication)
            self.assertTrue(saved['publication']['facts_summary'].startswith(projected['publication']['facts_summary']))
            self.assertEqual(projected['publication']['delivery_brief'],saved['publication']['delivery_brief'])
            self.assertLessEqual(len(json.dumps(projected,ensure_ascii=False)),12000)
            small={'inspection':str(output),'publication':{'scope':'Current native observation'},'reply_policy':'Current rule scope'}
            projected=poe.bound_reply(small,args(command='inspect',out=str(output)))
            self.assertEqual(next(iter(projected)),'reply_policy')
            self.assertEqual(projected,small)

    def test_saved_inspection_returns_breakdown_feedback_and_current_references(self):
        native={'stats':{'FutureMetric':0},'points':0,'ascendancy_points':0,'breakdowns':[
            {'metric':'FutureMetric','actor':'player','status':'unsupported','value_status':'observed','detail_status':'unavailable','value':0,'main_value':0,
             'matches_main_value':True,'reason':'No native breakdown','available_metrics':['CurrentNativeMetric'],
             'scope':'Current selected actor only'},
            {'metric':'CurrentNativeMetric','actor':'player','status':'partial','value_status':'observed','detail_status':'partial','value':4,'main_value':4,
             'detail':['Actual native formula'],'omissions':['Unavailable detail'],'available_metrics':['CurrentNativeMetric']}]}
        with tempfile.TemporaryDirectory() as folder:
            original=Path(folder)/'original.xml';original.write_text(SAMPLE,encoding='utf-8');output=Path(folder)/'current.json'
            original_bytes=original.read_bytes();stdout=io.StringIO()
            with patch.object(engine,'calculate',return_value=native),patch.object(engine,'resolve_root',return_value=Path(folder)),redirect_stdout(stdout):
                code=poe.main(['inspect','--input',str(original),'--metrics','FutureMetric,CurrentNativeMetric','--out',str(output),'--report',str(output.with_suffix('.html'))])
            self.assertEqual(code,0,stdout.getvalue());receipt=json.loads(stdout.getvalue());saved=poe.read_json(output)
            self.assertEqual(saved['reference_contract']['current']['artifact'],{'file':str(output),'pointer':'/calculation'})
            missing,partial=receipt['breakdown_summary']
            self.assertEqual(missing['status'],'unsupported');self.assertEqual(missing['value'],0)
            self.assertEqual(missing['value_status'],'observed');self.assertEqual(missing['detail_status'],'unavailable')
            self.assertEqual(partial['value_status'],'observed');self.assertEqual(partial['detail_status'],'partial')
            self.assertEqual(partial['omission_count'],1);self.assertEqual(partial['status'],'partial')
            self.assertEqual(missing['available_metrics_pointer'],'/calculation/breakdowns/0/available_metrics')
            action=next(row for row in receipt['next_actions'] if row['pointer']==missing['available_metrics_pointer'])
            self.assertEqual(action['file'],str(output));self.assertEqual(action['action'],'view')
            self.assertNotIn('replacement_metric',missing)
            page=poe.view_json(args(file=str(output),pointer=partial['detail_pointer'],fields='',keys=False,query='',offset=0,limit=10))
            self.assertEqual(page['native_references'][partial['detail_pointer']],'native:current#/breakdowns/1/detail')
            self.assertEqual(saved['report_sha256'],receipt['report_sha256'])
            self.assertEqual(page['report_integrity']['status'],'verified')
            revision=page['revision'];report=Path(saved['report'])
            report.write_text(report.read_text(encoding='utf-8')+'<!-- later edit -->',encoding='utf-8')
            changed=poe.view_json(args(file=str(output),pointer='/report_sha256',fields='',keys=False,query='',offset=0,limit=64))
            self.assertEqual(changed['report_integrity']['status'],'changed')
            self.assertEqual(changed['revision'],revision);self.assertEqual(changed['value'],saved['report_sha256'])
            self.assertTrue(any(row['action']=='regenerate_report' for row in changed['next_actions']))
            arbitrary=Path(folder)/'ordinary.json';poe.write_json(arbitrary,{'report':str(report),'report_sha256':saved['report_sha256']})
            read_bytes=Path.read_bytes
            with patch.object(Path,'read_bytes',autospec=True,side_effect=read_bytes) as reader:
                not_checked=poe.view_json(args(file=str(arbitrary),pointer='/report',fields='',keys=False,query='',offset=0,limit=10))
            self.assertNotIn(report,[call.args[0] for call in reader.call_args_list])
            self.assertEqual(not_checked['report_integrity']['status'],'not_checked')
            self.assertIsNone(not_checked['report_integrity']['actual_sha256'])
            self.assertEqual(original.read_bytes(),original_bytes)
            for selection in (['--calculate'],['--metrics','FutureMetric,CurrentNativeMetric']):
                stdout=io.StringIO();details=Path(folder)/'detail-only.json'
                with patch.object(engine,'calculate',return_value=native),patch.object(engine,'resolve_root',return_value=Path(folder)),redirect_stdout(stdout):
                    code=poe.main(['inspect','--input',str(original),*selection,'--out',str(details)])
                self.assertEqual(code,0,stdout.getvalue())
                receipt=json.loads(stdout.getvalue());saved=poe.read_json(details)
                self.assertEqual(receipt['reply_policy'],poe.READONLY_REPLY_POLICY)
                self.assertEqual(saved['reply_policy'],poe.READONLY_REPLY_POLICY)
                self.assertNotIn('report',receipt)
                self.assertNotIn('publication',saved)
                self.assertEqual(original.read_bytes(),original_bytes)

    def test_current_flask_parameters_are_shared_by_diagnosis_and_publication(self):
        native={'stats':{},'points':0,'ascendancy_points':0,'equipped':[
            {'slot':'Future Slot','id':71,'name':'Current native object','display_name':'当次本机名称（Current native object）','active':True,
             'flask_local':{'lifeInstant':0,'lifeGradual':12,'duration':2,'chargesMax':30,'chargesUsed':4,'FutureObserved':False},
             'flask_base':{'life':10,'duration':3,'chargesMax':25,'chargesUsed':5,'FutureBase':0},
             'flask_local_source':{'source':'engine/Classes/CurrentItem.lua','line':17,'count':20,'total_lines':20},
             'flask_scope':'Item-local current observation'}]}
        published=poe.publication('draft',[],[],{'status':'unknown'},{'当前角色':native})
        row=next(row for row in published['statements'] if row['kind']=='native_flask_parameters')
        self.assertEqual(row['values']['flask_local'],native['equipped'][0]['flask_local'])
        self.assertIn(row['text'],poe.diagnosis_summary(native))
        self.assertIs(row['values']['flask_local']['FutureObserved'],False)
        self.assertEqual(row['values']['flask_local']['lifeInstant'],0)
        self.assertIn(native['equipped'][0]['display_name'],row['text'])
        self.assertEqual(row['values']['display_name'],native['equipped'][0]['display_name'])
        self.assertEqual(row['values']['flask_base'],native['equipped'][0]['flask_base'])
        self.assertEqual(row['values']['flask_local_source'],native['equipped'][0]['flask_local_source'])
        self.assertEqual(row['values']['flask_base']['FutureBase'],0)
        self.assertIn('基底生命总量 10',row['text'])
        self.assertIn('单件处理后',row['text'])

    def test_probe_missing_equipment_id_feedback_uses_current_object_without_mutation(self):
        operations={'equip':[{'slot':'Current Slot','active':False}]}
        native={'engine_fingerprint':'current','equipped':[{'slot':'Current Slot','id':93,'name':'Current item','active':True}]}
        before=copy.deepcopy(operations)
        try:bx.operations_shape(operations)
        except bx.ContractError as exc:
            record=poe.failure_record(exc,'/variants/2','request.json',native=native,operations=operations)
        else:self.fail('missing item selector must still be rejected')
        d=record['diagnostic']
        self.assertEqual(d['location'],'/variants/2/operations/equip/0/item_id')
        self.assertEqual(d['repair_template'],{'slot':'Current Slot','item_id':93,'active':False})
        self.assertEqual(d['current_object']['engine_fingerprint'],'current')
        self.assertEqual(operations,before)
        self.assertEqual(native['equipped'][0]['active'],True)
        for invalid in ([{}],{'equip':None}):
            with self.subTest(invalid=invalid):
                try:bx.operations_shape(invalid)
                except bx.ContractError as exc:
                    failed=poe.failure_record(exc,native=native,operations=invalid)
                else:self.fail('invalid structure must still be rejected')
                self.assertIn('diagnostic',failed)

    def test_cli_cache_is_scoped_to_output_or_run_and_restored_after_failure(self):
        previous=engine.CACHE_DIRECTORY
        with tempfile.TemporaryDirectory() as folder:
            workspace=Path(folder).resolve();out=workspace/'facts'/'observation.json';run=workspace/'session'
            seen=[]
            def probe(_):
                seen.append(engine.CACHE_DIRECTORY)
                return {'status':'screened','alternatives':[],'next':'inspect current evidence'}
            with patch.object(poe,'probe',side_effect=probe),redirect_stdout(io.StringIO()):
                self.assertEqual(poe.main(['probe','--input',str(workspace/'input.xml'),'--ids','FutureSkill','--out',str(out)]),0)
            self.assertEqual(seen,[out.parent/'.poe-evidence'/'native-cache'])
            self.assertEqual(engine.CACHE_DIRECTORY,previous)
            with patch.object(poe,'probe',side_effect=probe),patch.object(Path,'cwd',return_value=workspace),redirect_stdout(io.StringIO()):
                self.assertEqual(poe.main(['probe','--input',str(workspace/'input.xml'),'--ids','FutureSkill']),0)
            self.assertEqual(seen[-1],workspace/'.poe-evidence'/'native-cache')
            self.assertEqual(engine.CACHE_DIRECTORY,previous)
            def failed(_):
                seen.append(engine.CACHE_DIRECTORY)
                raise ValueError('controlled interface failure')
            with patch.object(poe,'probe',side_effect=failed),redirect_stderr(io.StringIO()):
                self.assertEqual(poe.main(['probe','--run',str(run),'--name','candidate','--ids','FutureSkill']),2)
            self.assertEqual(seen[-1],run/'.poe-evidence'/'native-cache')
            self.assertEqual(engine.CACHE_DIRECTORY,previous)

    def test_character_changes_keeps_global_controls_and_missing_values(self):
        before={'gems':[{'group':2,'index':1,'id':'FutureEffect','name':'Current native effect','display_name':'当前效果','enable_global1':True}],
                'config_values':{'future':False,'removed':None},'nodes':[2,1]}
        before['gems'][0]['granted_effects']=[{'id':'CurrentBurst','index':1,'name':'Native burst','operation_field':'enable_global1','skill_types':['FutureBurst'],'global_enabled':True},
                                         {'id':'CurrentSteady','index':2,'name':'Native steady','operation_field':'enable_global2','skill_types':['HasReservation'],'global_enabled':False}]
        after=copy.deepcopy(before)
        after['gems'][0]['enable_global1']=False
        after['config_values']={'future':0,'added':None}
        after['nodes']=[1,2]
        changes=engine.character_changes(before,after)
        rows={row['locator']:row for row in changes}
        self.assertEqual(rows['/gems/0/enable_global1']['before'],True)
        self.assertEqual(rows['/gems/0/enable_global1']['after'],False)
        self.assertEqual(rows['/gems/0/enable_global1']['object_before']['group'],2)
        self.assertEqual(rows['/gems/0/enable_global1']['object_after']['id'],'FutureEffect')
        self.assertEqual(rows['/gems/0/enable_global1']['object_after']['display_name'],'当前效果')
        effects=rows['/gems/0/enable_global1']['object_after']['granted_effects']
        self.assertEqual([effect['id'] for effect in effects],['CurrentBurst','CurrentSteady'])
        self.assertEqual(effects[1]['operation_field'],'enable_global2')
        self.assertEqual(effects[1]['skill_types'],['HasReservation'])
        self.assertIs(effects[1]['global_enabled'],False)
        self.assertEqual(engine.character_state(before)['gems'][0]['granted_effects'],before['gems'][0]['granted_effects'])
        self.assertEqual(type(rows['/config_values/future']['before']),bool)
        self.assertEqual(type(rows['/config_values/future']['after']),int)
        self.assertFalse(rows['/config_values/added']['before_present'])
        self.assertFalse(rows['/config_values/removed']['after_present'])
        self.assertNotIn('/nodes',rows)

    def test_diagnosis_and_publication_share_current_native_names(self):
        native={'class':'FutureClass','class_id':12,'class_cn':'当前职业','ascendancy':'FutureAscendancy','ascendancy_id':4,
                'ascendancy_cn':'当前升华','main_skill':'FutureSkill','main_skill_name':'Future Skill','main_skill_name_cn':'当前主技能',
                'points':1,'ascendancy_points':0,'stats':{}}
        published=poe.publication('draft',[],[],{'status':'unknown'},{'current':native})
        identity=next(row for row in published['statements'] if row['kind']=='native_identity')
        summary=poe.diagnosis_summary(native)
        self.assertIn(identity['text'].split(' · ',1)[1],summary)
        self.assertEqual(identity['values']['main_skill']['id'],'FutureSkill')
        native['ascendancy_cn']=None
        self.assertIn('FutureAscendancy',poe.diagnosis_summary(native))

    def test_effect_controls_subset_never_hides_other_native_inputs(self):
        native={'config_controls':{'visible':{'type':'check','value':False}},
                'config_values':{'visible':False,'not-shown':True,'future-count':0}}
        state=engine.effect_state(native)
        self.assertEqual(state['config_switches'],{'visible':False})
        self.assertEqual(state['config_values_not_in_controls'],{'not-shown':True,'future-count':0})
        self.assertIn('config_controls',state['configuration_scope'])
        self.assertIn('config_values',state['configuration_scope'])
        statement=poe.effect_statement(native,'current')
        self.assertEqual(statement['values']['config_values_not_in_controls']['not-shown'],True)
        self.assertIn('控件集合',statement['text'])

    def test_comparison_context_separates_control_observability_from_actual_conditions(self):
        before={'config_values':{},'config_controls':{'shared':{'type':'check','value':False}},'stats':{'FutureMetric':10}}
        for included in (None,False,True):
            after=copy.deepcopy(before)
            after['equipped']=[{'slot':'Future Item Slot','id':9,'active':False,'effective_in_calculation':included}]
            context=poe.comparison_context(before,after)
            self.assertEqual(context['changes'],[])
            self.assertEqual(poe.compare(before,after,not context['changes'])['FutureMetric']['percent'],0)
        after=copy.deepcopy(before)
        after['config_controls']['newly-visible']={'type':'check','value':False,'value_basis':'native_unchecked_default'}
        context=poe.comparison_context(before,after)
        self.assertEqual(context['changes'],[])
        observation=context['control_observations'][0]
        self.assertEqual(observation['id'],'newly-visible')
        self.assertFalse(observation['before_present'])
        self.assertIsNone(observation['before'])
        self.assertIs(observation['after'],False)
        self.assertNotIn('newly-visible',engine.effect_state(before)['config_switches'])
        after['config_controls']['shared']['value']=True
        self.assertIn('config_switches',poe.comparison_context(before,after)['changes'])
        after=copy.deepcopy(before);after['config_values']['newly-visible']=True
        self.assertIn('config_values',poe.comparison_context(before,after)['changes'])
        after=copy.deepcopy(before);after['config_controls']['shared']['value']='false'
        unknown=poe.comparison_context(before,after)['control_observations'][0]
        self.assertIsNone(unknown['after'])
        after=copy.deepcopy(before);after['stats']['GuardSkillActive']=True
        self.assertIn('effects',poe.comparison_context(before,after)['changes'])

    def test_comparison_reference_keeps_native_identity_and_arithmetic_scope(self):
        before={'engine_fingerprint':'fixture-native','input_xml_sha256':'a'*64,'character_identity':{'digest':'b'*64},
                'stats':{'FutureMetric':10},'stat_metadata':{'FutureMetric':[{'label':'Current native rate','format':'.1f','actor':'player'}]}}
        after=copy.deepcopy(before);after['stats']['FutureMetric']=12;after['input_xml_sha256']='c'*64
        native={'baseline':before,'candidate':after,'comparison':poe.compare(before,after)}
        observed=facts.resolve_reference('native:comparison#/FutureMetric/percent',{},native)
        self.assertEqual(observed['value'],20)
        self.assertEqual(observed['origin'],'program_assessment')
        self.assertEqual(observed['context']['engine_fingerprint'],'fixture-native')
        self.assertEqual(observed['context']['comparison_before']['input_xml_sha256'],'a'*64)
        self.assertEqual(observed['meaning']['unit'],'%')
        self.assertIn('Current native rate',observed['meaning']['label'])
        self.assertIn('算术',observed['meaning']['scope'])
        self.assertNotIn('因果已证实',observed['formatted_fact'])
        raw=[{'claim_id':'change','subjects':['goal'],'text':observed['text_templates']['fact'],
              'checks':[{'reference':'native:comparison#/FutureMetric/percent','op':'ge','value':15}]}]
        self.assertEqual(facts.assess_claims(raw,{},native)[0]['check_status'],'supported')
        native['comparison']=poe.compare(before,after,False)
        self.assertIsNone(facts.resolve_reference('native:comparison#/FutureMetric/percent',{},native)['value'])
        self.assertEqual(facts.assess_claims(raw,{},native)[0]['check_status'],'unknown')

    def test_view_field_projection_exposes_current_control_source_read(self):
        control={'id':'FutureField','type':'count','label':'Current label','tooltip':'Native meaning',
                 'apply_source':{'source':'engine/Modules/Future.lua','line':53,'count':3,'total_lines':3}}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'current.json'
            poe.write_json(path,{'entries':[{'id':'FutureField','current_control':control}]})
            before=path.read_bytes()
            page=poe.view_json(args(file=str(path),pointer='/entries',fields='id',keys=False,query='',offset=0,limit=1))
            reads=[row for row in page['next_actions'] if row['action']=='data']
            self.assertEqual([(row['path'],row['line'],row['limit']) for row in reads],[('engine/Modules/Future.lua',53,3)])
            self.assertEqual(reads[0]['control_pointer'],'/entries/0/current_control')
            self.assertEqual(path.read_bytes(),before)
            keys=poe.view_json(args(file=str(path),pointer='/entries/0',fields='',keys=True,query='',offset=0,limit=10))
            self.assertFalse(keys['next_actions'])
            control['apply_source']['line']=False
            poe.write_json(path,control)
            invalid=poe.view_json(args(file=str(path),pointer='',fields='id',keys=False,query='',offset=0,limit=1))
            self.assertFalse(invalid['next_actions'])
            locations={'config_loader_source':{'source':'engine/Classes/CurrentLoader.lua','line':21,'count':4},
                       'flask_local_source':{'source':'engine/Classes/CurrentItem.lua','line':31,'count':5}}
            poe.write_json(path,locations)
            for pointer in ['', '/config_loader_source', '/flask_local_source']:
                page=poe.view_json(args(file=str(path),pointer=pointer,fields='',keys=False,query='',offset=0,limit=10))
                reads=[row for row in page['next_actions'] if row['action']=='data']
                expected=list(locations.values()) if not pointer else [locations[pointer[1:]]]
                self.assertEqual({(row['path'],row['line'],row['limit']) for row in reads},
                                 {(row['source'],row['line'],row['count']) for row in expected})

    def test_probe_measurement_keeps_all_joint_actual_inputs_and_observation_subject(self):
        before={'nodes':[],'stats':{'Measured':10},'main_skill':'FutureSkill','main_group':1,
                'config_values':{'condition':False},'equipped':[{'slot':'Flask 1','id':1,'active':False},{'slot':'Flask 2','id':2,'active':False}]}
        after=copy.deepcopy(before)
        for item in after['equipped']: item['active']=True
        measurement=poe.measurement_facts(before,after,{'equip':[{'slot':item['slot'],'item_id':item['id'],'active':True} for item in after['equipped']]})
        self.assertEqual(len(measurement['actual_changes']),2)
        self.assertEqual(measurement['observation_scope'],'native_state_difference')
        self.assertEqual(measurement['changed_state_fields'],2)
        self.assertEqual(measurement['observed_subject']['main_skill'],'FutureSkill')
        self.assertEqual(measurement['common_conditions']['config_values'],{'condition':False})
        self.assertFalse(measurement['individual_contributions_measured'])
        # One intervention can change both a requested input and its derived state.
        before['equipped'][0]['effective_in_calculation']=False
        after=copy.deepcopy(before)
        after['equipped'][0].update(active=True,effective_in_calculation=True)
        operations={'equip':[{'slot':'Flask 1','item_id':1,'active':True}]}
        single=poe.measurement_facts(before,after,operations)
        self.assertEqual(single['requested_operations'],operations)
        self.assertEqual(single['changed_state_fields'],2)
        self.assertEqual(single['observation_scope'],'native_state_difference')
        self.assertFalse(single['individual_contributions_measured'])
        self.assertEqual({row['locator'] for row in single['actual_changes']},
                         {'/equipped/0/active','/equipped/0/effective_in_calculation'})

    def test_view_returns_current_native_references_for_projected_array_rows(self):
        native={'timeline':{'resources':[{'resource_id':'Mana','status':'unknown'},
                                         {'resource_id':'Life','status':'supported','future/value':False}]}}
        contract=facts.reference_contract({'resources':native})
        contract['resources']['artifact']={'pointer':'/readiness'}
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'evaluation.json'
            poe.write_json(source,{'readiness':native,'reference_contract':contract})
            page=poe.view_json(args(file=str(source),pointer='/readiness/timeline/resources',fields='resource_id,status,future/value',keys=False,query='',offset=1,limit=1))
            ref=page['native_references']['/readiness/timeline/resources/1/future~1value']
            self.assertEqual(ref,'native:resources#/timeline/resources/1/future~1value')
            self.assertIs(facts.resolve_reference(ref,{}, {'resources':native})['value'],False)
            self.assertNotIn('/readiness/timeline/resources/0/status',page['native_references'])
            self.assertEqual(page['revision'],bx.digest(source.read_bytes()))
            source_before=source.read_bytes()
            keys=poe.view_json(args(file=str(source),pointer='/readiness/timeline/resources/1',fields='',keys=True,query='future',offset=0,limit=1))
            self.assertEqual(keys['native_references']['/readiness/timeline/resources/1/future~1value'],ref)
            self.assertEqual(source.read_bytes(),source_before)
            document={'readiness':native,'reference_contract':contract,
                      'review_packet':{'facts':{'resources':copy.deepcopy(native)}}}
            poe.write_json(source,document)
            projected='/review_packet/facts/resources/timeline/resources/1'
            page=poe.view_json(args(file=str(source),pointer=projected,fields='resource_id,status,future/value',keys=False,query='',offset=0,limit=10))
            self.assertEqual(page['native_references'][projected+'/future~1value'],ref)
            document['review_packet']['facts']['resources']['timeline']['resources'][1]['future/value']=0
            poe.write_json(source,document)
            page=poe.view_json(args(file=str(source),pointer=projected,fields='resource_id,status,future/value',keys=False,query='',offset=0,limit=10))
            self.assertNotIn(projected+'/future~1value',page['native_references'])
            self.assertEqual(page['native_references'][projected+'/status'],'native:resources#/timeline/resources/1/status')

    def test_report_distinguishes_inactive_slot_and_disabled_gems_from_enabled_group(self):
        native={'config_values':{},'config_labels':{},'gems':[
            {'group':1,'index':1,'id':'future-reserve','level':3,'quality':2,'slot':'Weapon 2 Swap',
             'group_enabled':True,'slot_enabled':False,'enabled':True,'granted_effects':[]},
            {'group':2,'index':1,'id':'future-disabled','level':4,'quality':0,'slot':'Helmet',
             'group_enabled':True,'slot_enabled':True,'enabled':False,'granted_effects':[]}]}
        budget={'materials':[],'status':'unknown','totals_complete':False,'low':None,'high':None,'currency':None,'limit':None}
        outcome={'name':'Current activation','plan':{'summary':'Pending model review'},'result':native,'budget':budget,
                 'status':'draft','errors':[],'pending':[],'warnings':[],'comparison':{},'readiness':{'resources':[]},
                 'claims':[],'objective_coverage':[],'evidence':{},'actual_changes':[],'comparison_notes':[],
                 'publication':poe.publication('draft',[],[],budget,{'candidate':native},keys=[]),'validation':{'scope':'Observed only'},
                 'xml_sha256':'fixture','engine_fingerprint':'fixture','engine_version':'future'}
        rendered=poe.report_html({'intent':{'goal':'Use currently active effects'}},outcome)
        self.assertIn('槽位未启用',rendered)
        self.assertIn('future-disabled [future-disabled]',rendered)
        self.assertIn('保存等级/品质 4/0',rendered)
        self.assertIn('当前有效等级/品质 未知/未知',rendered)
        self.assertIn('宝石未启用',rendered)
        self.assertNotIn('参与计算',rendered)
        for code in ('claim_binding_stale','claim_binding_missing'):
            with self.subTest(code=code):
                outcome['claims']=[{'claim_id':'stale','status':'unresolved','rendered_text':'Enable future-disabled for the old arrangement',
                                    'conditions':['Use the retired setup.'],'uncertainties':['Retired risk estimate.'],
                                    'evidence_refs':[],'diagnostics':[{'code':code}]}]
                before=copy.deepcopy(outcome)
                rendered=poe.report_html({'intent':{'goal':'Use currently active effects'}},outcome)
                for retired in ('Enable future-disabled for the old arrangement','Use the retired setup.','Retired risk estimate.'):
                    self.assertNotIn(retired,rendered)
                self.assertEqual(outcome,before)
        outcome['claims'][0]['diagnostics']=[{'code':'missing_reference'}]
        rendered=poe.report_html({'intent':{'goal':'Use currently active effects'}},outcome)
        self.assertNotIn('Use the retired setup.',rendered)
        self.assertNotIn('Retired risk estimate.',rendered)

    def test_stage_config_type_failure_has_its_actual_stage_location(self):
        native={'xml':SAMPLE,'classes':[{'id':4,'name':'Duelist','ascendancies':[]}],
                'config_types':{'future-switch':'check'},'config_lists':{},'engine_fingerprint':'fixture'}
        stage={'stage_id':'future-stage','level':12,'ascendancy_allocated_nodes':[],
               'skill_groups':[{'name':'main','slot':'','gems':[{'skill_id':'Fireball','level':1}]}],
               'snapshot':{'operations':{'config':{'future-switch':'false'}}}}
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'route.html';poe.write_json(output.with_suffix('.state.json'),{})
            with patch.object(engine,'calculate',side_effect=AssertionError('Invalid config must fail before native calculation')):
                result=poe.route_snapshots({'class_id':4,'stages':[stage]},native,Path(folder),Path(folder),output)['future-stage']
            self.assertEqual(result['status'],'blocked')
            self.assertEqual(result['diagnostic']['location'],'/stages/0/snapshot/operations/config/future-switch')
            self.assertEqual(result['diagnostic']['actual'],'false')
            self.assertFalse(any(Path(folder).rglob('snapshot.xml')))

    def test_input_xml_probe_reuses_variant_operations_without_writing_the_role(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'supplied.xml';poe.write_text(source,SAMPLE)
            variants=Path(folder)/'variants.json';poe.write_json(variants,{'variants':[{'id':'observed-switch','operations':{'config':{'future-switch':True}}}]})
            native={'xml':SAMPLE,'stats':{'CombinedDPS':100},'engine_fingerprint':'fixture','main_skill':'fixture-main',
                    'points':1,'ascendancy_points':0,'secondary_ascendancy_points':0,'active_skills':[],
                    'nodes':[1,2],'equipped':[],
                    'config_types':{'future-switch':'check'},'config_lists':{},
                    'config_controls':{'future-switch':{'type':'check','value':False}}}
            native['stats'].update(ManaUnreserved=200,NetManaRegen=15)
            native['resource_rules']={'energy_shield_protects_mana':False}
            native['active_skills']=[{'skill_ref':'future-ref','actor':'player','is_main':True,
                'costs':{'ManaCost':20,'ManaPerSecondCost':60},'usage_rate':{'value':3}}]
            def calculate(*pos,**kw):
                if 'xml_batch' in kw:
                    changed=copy.deepcopy(native);changed['config_controls']['future-switch']['value']=True
                    changed['stats']['NetManaRegen']=70
                    return {'calculations':[{**changed,'request_id':engine.batch_id('xml',i,xml),'input_xml_sha256':bx.digest(xml)} for i,xml in enumerate(kw['xml_batch'])]}
                return native
            before=source.read_bytes()
            with patch.object(poe,'read_candidate',side_effect=AssertionError('Read-only input must not need a candidate session')),patch.object(engine,'resolve_root',return_value=Path(folder)),patch.object(engine,'check',return_value={'engine_fingerprint':'fixture'}),patch.object(engine,'calculate',side_effect=calculate),patch.object(poe,'mechanical_issues',return_value=([],[],[])),patch.object(poe,'check_constraints',return_value=([],[])),patch.object(resources,'assess',side_effect=lambda current,*pos:{'errors':[],'pending':[],'warnings':[],'single_payments':resources.single_payments(current)}):
                response=poe.probe(args(input=str(source),variants=str(variants)))
                self.assertEqual(response['source_mode'],'input_xml')
                self.assertIs(response['baseline_effect_state']['config_switches']['future-switch'],False)
                self.assertIs(response['alternatives'][0]['effect_state']['config_switches']['future-switch'],True)
                self.assertIn('config_switches',response['alternatives'][0]['context_changes'])
                self.assertIsNone(response['alternatives'][0]['comparison']['CombinedDPS']['percent'])
                self.assertEqual(response['alternatives'][0]['operations'],{'config':{'future-switch':True}})
                with self.assertRaises(bx.ContractError):poe.probe(args(input=str(source),run=str(Path(folder)/'run'),variants=str(variants)))
                output=Path(folder)/'comparison.json';report=Path(folder)/'comparison.html';stdout=io.StringIO()
                with redirect_stdout(stdout):
                    code=poe.main(['probe','--input',str(source),'--variants',str(variants),'--out',str(output),'--report',str(report)])
                self.assertEqual(code,0,stdout.getvalue())
                saved=poe.read_json(output);receipt=json.loads(stdout.getvalue())
                self.assertEqual(saved['reference_contract']['current']['artifact']['pointer'],'/baseline_calculation')
                self.assertEqual(saved['publication']['facts_summary'].split('\n\n',1)[0],saved['publication']['delivery_brief']['message'])
                self.assertEqual(saved['publication']['statements'][0]['text'],saved['publication']['delivery_brief']['message'])
                self.assertEqual(saved['input_sha256'],bx.digest(before))
                self.assertEqual(receipt['report_sha256'],bx.digest(report.read_bytes()))
                self.assertEqual(receipt['reply_policy'],poe.READONLY_REPLY_POLICY)
                experiment=next(s for s in saved['publication']['statements'] if s['kind']=='experiment')
                self.assertEqual(experiment['pointer'],'/alternatives/0')
                self.assertEqual(experiment['measurement'],{k:saved['alternatives'][0]['measurement'].get(k) for k in experiment['measurement']})
                self.assertEqual(experiment['comparison'],saved['alternatives'][0]['comparison'])
                self.assertIn('future-switch',experiment['text'])
                self.assertIn('联合变化只支持联合观察',experiment['text'])
                self.assertIn(experiment['text'],saved['publication']['facts_summary'])
                self.assertIn('observed-switch',receipt['publication']['facts_summary'])
                self.assertEqual(receipt['next_actions'][0]['pointer'],'/publication')
                page=report.read_text(encoding='utf-8')
                self.assertIn('observed-switch',page);self.assertIn('future-switch',page)
                self.assertIn('CombinedDPS',page);self.assertIn('100.00',page)
                self.assertIn('原始文件字节 SHA256 '+bx.digest(before),page)
                self.assertIn('request_selections_verified',page);self.assertIn('actual_changes',page)
                self.assertIn('数值差不证明因果',page)
                means=[row for row in saved['publication']['statements'] if row['kind']=='single_skill_mean' and row['resource_id']=='Mana']
                self.assertEqual({row['subject']:row['values']['net_drain_per_second'] for row in means},
                                 {'current':45,'variant:observed-switch':-10})
                for row in means:
                    self.assertIn(row['text'],saved['publication']['facts_summary'])
                    self.assertIn(html.escape(row['text']),page)
                self.assertEqual(saved['publication']['resource_audits']['variant:observed-switch'],saved['alternatives'][0]['readiness'])
                def changed_source(*pos,**kw):
                    source.write_bytes(before+b'\n')
                    return calculate(*pos,**kw)
                with patch.object(engine,'calculate',side_effect=changed_source),self.assertRaises(bx.ContractError) as caught:
                    poe.probe(args(input=str(source),variants=str(variants)))
                self.assertEqual(caught.exception.diagnostic['location'],'/input')
                source.write_bytes(before)
                # Real CLI projection must keep the measured actor beside a batch result,
                # rather than filling the preview with repeated audit/context details.
                native['stat_metadata']={'CombinedDPS':[{'label':'Combined DPS','actor':'player','format':'.1f'}]}
                native['config_values']={'large-current-context':'context '*1500}
                poe.write_json(variants,{'variants':[{'id':'observed-switch-'+str(i),'operations':{'config':{'future-switch':True}}} for i in range(12)]})
                stdout=io.StringIO()
                with redirect_stdout(stdout):
                    code=poe.main(['probe','--input',str(source),'--variants',str(variants),'--out',str(output),'--report',str(report)])
                self.assertEqual(code,0,stdout.getvalue())
                saved=poe.read_json(output);receipt=json.loads(stdout.getvalue())
                experiments=[s for s in saved['publication']['statements'] if s['kind']=='experiment']
                first=experiments[0]
                self.assertIn('player',first['text'])
                self.assertIn('future-ref',first['text'])
                self.assertIn('Combined DPS',first['text'])
                self.assertLess(len(first['text']),2500)
                self.assertIn('future-ref',receipt['publication']['facts_summary'])
                self.assertIn('observed-switch-0',receipt['publication']['facts_summary'])
                self.assertEqual(saved['alternatives'][0]['measurement']['metric_observation']['context']['config_values'],native['config_values'])
                page=report.read_text(encoding='utf-8')
                self.assertIn('<p>'+html.escape(first['text'])+'</p>',page)
            self.assertEqual(source.read_bytes(),before)
            self.assertEqual({row.name for row in Path(folder).iterdir()},{'supplied.xml','variants.json','comparison.json','comparison.html'})

    def test_input_probe_output_cannot_overwrite_xml_or_parameter_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'role.xml';poe.write_text(source,SAMPLE)
            variants=Path(folder)/'variants.json';poe.write_json(variants,{'variants':[]})
            for output in (source,variants):
                with self.subTest(output=output),patch.object(poe,'probe',return_value={'status':'screened','alternatives':[],'next':'fixture'}),redirect_stderr(io.StringIO()):
                    self.assertEqual(poe.main(['probe','--input',str(source),'--variants',str(variants),'--out',str(output)]),2)
            output=Path(folder)/'output.json'
            for report in (source,variants,output):
                with self.subTest(report=report),patch.object(poe,'probe',side_effect=AssertionError('Invalid report path must fail before calculating')),redirect_stderr(io.StringIO()):
                    self.assertEqual(poe.main(['probe','--input',str(source),'--variants',str(variants),'--out',str(output),'--report',str(report)]),2)
            self.assertEqual(source.read_text(encoding='utf-8'),SAMPLE)
            self.assertEqual(poe.read_json(variants),{'variants':[]})

    def test_effect_state_preserves_false_unknown_and_each_native_variant(self):
        calculation={'config_controls':{'future-item-switch':{'id':'future-item-switch','type':'check','label':'Future item effect','value':False,'value_basis':'native_unchecked_default'},
                                        'future-stacks':{'id':'future-stacks','type':'count','value':None,'default':7,'value_basis':'unknown','if_option':'future-item-switch'}},
                     'gems':[{'group':2,'index':3,'id':'future-combined-gem','enabled':True,'group_enabled':True,'slot_enabled':True,
                              'granted_effects':[{'id':'future-burst','name':'Future burst','has_global_effect':True,'global_enabled':False,'active_skill_present':False},
                                                 {'id':'future-steady','name':'Future steady','has_global_effect':True,'global_enabled':True,'active_skill_present':True}]}],
                     'equipped':[{'slot':'Flask 1','active':False}],'stats':{'GuardSkillActive':False}}
        state=engine.effect_state(calculation)
        self.assertIs(state['config_switches']['future-item-switch'],False)
        self.assertNotIn('future-stacks',state['config_switches'])
        self.assertEqual([(row['id'],row['global_enabled']) for row in state['global_effects']], [('future-burst',False),('future-steady',True)])
        self.assertEqual(state['flasks'],{'Flask 1':False});self.assertIs(state['guard_active'],False)
        observed=facts.resolve_reference('native:candidate#/config_controls/future-stacks/value',{}, {'candidate':calculation})
        self.assertIsNone(observed['value']);self.assertEqual(facts.check_value(observed['value'],'eq',7),'unknown')
        self.assertEqual(observed['context']['effect_state']['config_switches'],state['config_switches'])
        ref='native:candidate#/config_controls/future-item-switch/value'
        claim={'claim_id':'active-item','claim_type':'comparison','text':'Item is active','subjects':['configuration:candidate'],'checks':[{'reference':ref,'op':'eq','value':True}]}
        assessed=facts.assess_claims([claim],{}, {'candidate':calculation})[0]
        self.assertEqual(assessed['checks'][0]['status'],'refuted')
        self.assertTrue(facts.configuration_reviewed([{'status':'model_assessed','subjects':['configuration:candidate'],'evidence_refs':[ref]}],'candidate'))

    def test_publication_exposes_current_switches_and_effects_once_per_calculation(self):
        native={'stats':{'Observed':12,'GuardSkillActive':True},'config_controls':{'future-condition':{'type':'check','value':False}},
                'gems':[{'group':1,'index':1,'id':'future-gem','enabled':True,'group_enabled':True,'slot_enabled':True,
                         'granted_effects':[{'id':'future-guard','name':'Future guard','has_global_effect':True,'global_enabled':True,'active_skill_present':True}]}],
                'equipped':[{'slot':'Future Native Slot','active':False,'effective_in_calculation':True}]}
        result=poe.publication('draft',[],[],{'status':'unknown'}, {'candidate':native},keys=['Observed'])
        rows=[row for row in result['statements'] if row['kind']=='effect_state']
        self.assertEqual(len(rows),1);self.assertIs(rows[0]['values']['config_switches']['future-condition'],False)
        self.assertIs(rows[0]['values']['equipment_inclusion']['Future Native Slot'],True)
        self.assertIn('future-guard',result['facts_summary']);self.assertIn('future-condition=false',result['facts_summary'])
        self.assertIs(poe.calculation_facts(native)['config_controls']['future-condition']['value'],False)
        note='Shared current-native control scope'
        native['config_controls']['future-condition']['scope']=note
        native['config_controls']['future-counter']={'type':'integer','value':0,'scope':note}
        before=copy.deepcopy(native);projected=poe.calculation_facts(native)
        self.assertIs(projected['config_controls']['future-condition']['value'],False)
        self.assertEqual(projected['config_controls']['future-counter']['value'],0)
        self.assertEqual(json.dumps(projected,ensure_ascii=False).count(note),1)
        self.assertEqual(native,before)
        native['config_controls']['future-counter']['scope']='Different source scope'
        self.assertEqual(poe.calculation_facts(native)['config_controls'],native['config_controls'])

    def test_common_measurement_cannot_hide_a_locked_main_skill_change(self):
        before={'level':86,'tree_version':'future','class':'fixture','ascendancy':'fixture','main_skill':'changed-by-control','gems':[],'equipped':[],
                'measurement_audit':{'selected_main_before':{'main_skill':'original-main'}}}
        after=copy.deepcopy(before);original=bx.parse(SAMPLE);plan={'mode':'refine'}
        errors,pending=poe.check_constraints(original,original,before,after,{'allow_main_skill_change':False},plan)
        self.assertTrue(errors)
        errors,pending=poe.check_constraints(original,original,before,after,{},plan)
        self.assertIn('permission:allow_main_skill_change',pending)
        errors,pending=poe.check_constraints(original,original,before,after,{'allow_main_skill_change':True},plan)
        self.assertFalse(errors);self.assertNotIn('permission:allow_main_skill_change',pending)
        groups=bx.active_set(original,'Skills','SkillSet','activeSkillSet')
        groups.append(ET.fromstring('<Skill slot="Gloves" enabled="true"><Gem skillId="FutureBackup" level="7" quality="3" enabled="true" enableGlobal2="true"/></Skill>'))
        locked={'allow_main_skill_change':True,'keep_skill_groups':[2]}
        def violations(document):return poe.check_constraints(original,document,before,after,locked,plan)[0]
        formatting=copy.deepcopy(original);group=bx.groups(formatting)[1]
        group.attrib=dict(reversed(list(group.attrib.items())));group.text='\n  ';group.find('Gem').tail='\n'
        self.assertFalse(violations(formatting))
        permitted=copy.deepcopy(original);bx.groups(permitted)[0].find('Gem').set('level','19')
        self.assertFalse(violations(permitted))
        for field,value in (('skillId','FutureOther'),('enabled','false'),('level','8'),('quality','4'),('enableGlobal2','false')):
            changed=copy.deepcopy(original);bx.groups(changed)[1].find('Gem').set(field,value)
            with self.subTest(preserved_gem_field=field):
                self.assertTrue(any('技能组 2' in error for error in violations(changed)))
        changed=copy.deepcopy(original);bx.groups(changed)[1].set('enabled','false')
        self.assertTrue(any('技能组 2' in error for error in violations(changed)))
        removed=copy.deepcopy(original)
        bx.active_set(removed,'Skills','SkillSet','activeSkillSet').remove(bx.groups(removed)[1])
        self.assertTrue(any('技能组 2' in error for error in violations(removed)))
        native_original={'xml':bx.serialise(original)}
        observed={**after,'xml':bx.serialise(formatting)}
        self.assertFalse(poe.check_constraints(original,formatting,before,observed,locked,plan,original_native=native_original)[0])
        observed['xml']=bx.serialise(changed)
        self.assertTrue(poe.check_constraints(original,original,before,observed,locked,plan,original_native=native_original)[0])
        observed['xml']=native_original['xml']
        self.assertTrue(poe.check_constraints(original,changed,before,observed,locked,plan,original_native=native_original)[0])

    def test_measurement_resolves_native_effect_order_and_keeps_physical_assignment(self):
        document=bx.parse(SAMPLE)
        gem=bx.groups(document)[0].find('Gem')
        gem.set('enableGlobal1','true');gem.set('enableGlobal2','false')
        native={'gems':[{'group':1,'index':1,'id':gem.get('skillId'),
                 'granted_effects':[{'id':'future-steady','index':1,'has_global_effect':True,'operation_field':'enable_global1'},
                                    {'id':'future-burst','index':2,'has_global_effect':True,'operation_field':'enable_global2'}]}],
                'equipped':[{'slot':'Boots','id':1,'type':'Armour','active':False}]}
        controls={'effects':[{'skill_id':'future-steady','enabled':False},{'skill_id':'future-burst','enabled':True}]}
        changed,audit=bx.apply_measurement(document,controls,native)
        actual=bx.groups(changed)[0].find('Gem')
        self.assertEqual((actual.get('enableGlobal1'),actual.get('enableGlobal2')),('false','true'))
        self.assertEqual(actual.get('enabled'),gem.get('enabled'))
        self.assertEqual(bx.item_texts(changed),bx.item_texts(document))
        self.assertEqual(audit['effects'][0]['instances'][0]['gem_id'],gem.get('skillId'))
        controls['effects'][0]['skill_id']='not-in-this-build'
        _,audit=bx.apply_measurement(document,controls,native)
        self.assertEqual(audit['effects'][0]['status'],'absent')
        controls['effects'][0]['skill_id']='future-steady';native['gems'][0]['granted_effects'][0]['has_global_effect']=False
        with self.assertRaises(bx.ContractError):bx.apply_measurement(document,controls,native)
        for invalid in ({'flasks_enabled':0},{'effects':[{'skill_id':'x','enabled':True},{'skill_id':'x','enabled':False}]},{'effects':[{'skill_id':'x','enabled':'false'}]}):
            with self.subTest(invalid=invalid),self.assertRaises(bx.ContractError):bx.measurement_shape(invalid)

    def test_probe_metric_observation_keeps_selected_skill_and_actor_context(self):
        with tempfile.TemporaryDirectory() as folder:
            run=Path(folder);poe.write_text(run/'original.xml',SAMPLE)
            variant_file=run/'variants.json';poe.write_json(variant_file,{'variants':[{'id':'secondary-change','operations':{}}]})
            baseline={'stats':{'CombinedDPS':100,'FutureMetric':5},'main_skill':'fixture-main','main_skill_name':'Fixture main','main_group':1,'config_values':{'native-scene':True},'engine_fingerprint':'fixture','input_xml_sha256':'before',
                      'active_skills':[{'id':'fixture-main','name':'Fixture main','skill_ref':'qualified-main','is_main':True,'actor':'player'}],
                      'nodes':[1,2],'equipped':[],
                      'stat_metadata':{'CombinedDPS':[{'label':'Combined DPS','format':'.1f','actor':'player'}],'FutureMetric':[{'label':'Future metric','format':'.1f','actor':'player'}]}}
            candidate=copy.deepcopy(baseline);candidate.update(input_xml_sha256='after',points=1,ascendancy_points=0,secondary_ascendancy_points=0)
            candidate['stats']['FutureMetric']=12
            record=(run,{'intent':{}},{'plan':{'mode':'refine'},'engine_fingerprint':'fixture','baseline':baseline},SAMPLE,run)
            def calculate(*pos,**kw):
                if 'xml_batch' in kw:
                    return {'calculations':[{**candidate,'xml':xml,'request_id':engine.batch_id('xml',i,xml),'input_xml_sha256':bx.digest(xml)} for i,xml in enumerate(kw['xml_batch'])]}
                return baseline
            with patch.object(poe,'read_candidate',return_value=record),patch.object(engine,'calculate',side_effect=calculate),patch.object(poe,'materialize_items',side_effect=lambda plan,root:(plan,[])),patch.object(poe,'mechanical_issues',return_value=([],[],[])),patch.object(poe,'check_constraints',return_value=([],[])),patch.object(resources,'assess',return_value={'errors':[],'pending':[],'warnings':[]}):
                response=poe.probe(args(run=str(run),name='fixture',variants=str(variant_file),metrics='FutureMetric,CombinedDPS',out=str(run/'comparison.json')))
            observation=response['alternatives'][0]['metric_observation']
            self.assertEqual(response['alternatives'][0]['stats']['FutureMetric'],12)
            self.assertEqual(response['alternatives'][0]['comparison']['FutureMetric']['delta'],7)
            self.assertEqual(response['alternatives'][0]['comparison']['CombinedDPS']['delta'],0)
            self.assertEqual(response['alternatives'][0]['observed_delta'],7)
            self.assertEqual((observation['locator'],observation['value']),('/stats/FutureMetric',12))
            self.assertEqual(response['baseline_metric_observation']['value'],5)
            text=next(row['text'] for row in response['publication']['statements'] if row['kind']=='experiment')
            self.assertIn('Future metric',text)
            self.assertEqual(observation['context']['main_skill_ref'],'qualified-main')
            self.assertEqual(observation['context']['main_group'],1)
            self.assertEqual(observation['meaning']['actor'],'player')
            self.assertEqual(observation['context']['config_values'],{'native-scene':True})
            self.assertEqual(response['baseline_metric_observation']['context']['xml_sha256'],'before')
            self.assertIn('actual_changes',response['alternatives'][0])
            self.assertEqual(response['alternatives'][0]['configuration_changes'],{})
            # A display panel can belong to the player while another actor executes
            # the selected skill; publication must not equate those native fields.
            other=copy.deepcopy(baseline)
            other['active_skills'][0].update(actor='mirage',skill_ref='qualified-other-actor')
            measured=poe.measurement_facts(baseline,other,{})
            observed=poe.publication('scoped_result',[],[],{'status':'not_requested'},{'current':baseline},
                experiments=[{'id':'different-native-actor','status':'screened','measurement':measured,'comparison':{}}])
            text=next(row['text'] for row in observed['statements'] if row['kind']=='experiment')
            self.assertEqual(measured['metric_observation']['meaning']['actor'],'player')
            self.assertEqual(other['active_skills'][0]['actor'],'mirage')
            self.assertIn('面板归属 player',text)
            self.assertIn('qualified-other-actor',text)

    def test_metric_observation_selects_current_minion_metric_and_keeps_missing_unknown(self):
        calculation={'stats':{'CombinedDPS':0,'MinionCombinedDPS':230},'main_skill':'native-minion','active_skills':[{'skill_ref':'current-minion','is_main':True,'minion':True,'actor':'minion'}]}
        observed=poe.metric_observation(calculation,'candidate')
        self.assertEqual((observed['locator'],observed['value']),('/stats/MinionCombinedDPS',230))
        self.assertEqual(observed['context']['main_skill_ref'],'current-minion')
        calculation['stats'].pop('MinionCombinedDPS')
        missing=poe.metric_observation(calculation,'candidate')
        self.assertIsNone(missing['value'])
        self.assertEqual(missing['status'],'unknown')
        explicit=poe.metric_observation(calculation,'candidate','FutureMetric')
        self.assertEqual((explicit['locator'],explicit['value'],explicit['status']),('/stats/FutureMetric',None,'unknown'))

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
        for pointer in ('', '/checked_route'):
            current={'status':'draft','errors':[], 'pending':['assessment:Actual unresolved work']}
            action=next(row for row in poe.delivery_actions(current,'source.json','result.json','review.json',pointer)
                        if row['action']=='assess_dependencies')
            self.assertEqual(action.get('input_pointer'),'/assessment/unresolved')
            self.assertEqual(action['pointer'],pointer+'/pending')
            self.assertEqual(current['pending'],['assessment:Actual unresolved work'])
        self.assertNotIn('input_pointer',actions[1])


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
                self.assertEqual(facts.pointer(record,action['pointer']),record['diagnostics'])
                self.assertNotIn('diagnostics',action)
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
            self.assertNotIn('diagnostics',action)
            self.assertEqual(action['pointer'],'/diagnostics')
            self.assertIn('connection_proposal',diagnostics[0])

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
        self.assertEqual(facts.pointer(packet,action['method_pointer']),packet['check'])
        self.assertEqual(facts.pointer(packet,action['template_pointer']),packet['review_template'])

    def test_delivery_actions_include_objective_and_refuted_check_source_repairs(self):
        diagnostics=[{'code':'objective_subject_missing','action':'link_claim','location':'/claims','required_subject':'objective:actual'},
                     {'code':'objective_basis_missing','action':'record_user_basis','location':'/objectives/0/evidence_refs'},
                     {'code':'claim_check_refuted','action':'revise_conclusion','location':'/claims/0/checks/0','actual':0,'expected':1}]
        outcome={'status':'draft','errors':[],'pending':['objective:actual','claim_check:current'],'diagnostics':diagnostics}
        action=poe.delivery_actions(outcome,'source.json','result.json','review.json','/checked_route')[0]
        self.assertEqual(action['action'],'repair_input')
        self.assertEqual(action['pointer'],'/checked_route/diagnostics')
        self.assertEqual(facts.pointer({'checked_route':outcome},action['pointer']),diagnostics)
        self.assertEqual(action['repair_count'],3)
        self.assertEqual(action['total'],len(diagnostics))
        self.assertNotIn('diagnostics',action)
        self.assertEqual(action['edit_arguments'],{'kind':'route','file':'source.json'})
        # A retained object error must not hide independent source-reference repairs.
        large=[{**diagnostics[0],'location':'/claims/'+str(i)+'/evidence_refs',
                'source_diagnostic':{'expected':['native value '+str(j) for j in range(100)]}}
               for i in range(40)]
        outcome.update(errors=['Retained original input is not payable.'],diagnostics=large)
        with tempfile.TemporaryDirectory() as folder:
            run=Path(folder);file=run/'candidates/fixture/evaluation.json'
            actions=poe.delivery_actions(outcome,'source.json',str(file),'review.json','/checked_route')
            completion={'kind':'build','status':'blocked','scope':'Current requested task',
                        'errors':outcome['errors'],'pending':['dependency:'+str(i) for i in range(40)]}
            response={'status':'blocked','next_actions':actions,'facts_summary':'observed scope '*500,
                      'readiness':{'long':'x'*16000},'task_completion':completion,
                      'character_readiness':copy.deepcopy(completion)}
            poe.write_json(file,{'checked_route':outcome,'response':response})
            bounded=poe.bound_reply(response,argparse.Namespace(command='evaluate',run=str(run),name='fixture'))
            repair=bounded['next_actions'][0]
            self.assertEqual(repair['action'],'repair_input')
            self.assertEqual(repair['error_count'],1)
            self.assertEqual(repair['repair_count'],40)
            self.assertEqual(facts.pointer(poe.read_json(file),repair['errors_pointer']),outcome['errors'])
            self.assertEqual(facts.pointer(poe.read_json(file),repair['pointer']),large)
            self.assertEqual(bounded['task_completion']['pending'],completion['pending'])
            def complete_actions(reply):
                continuation=next((row for row in reply['continuations'] if row['pointer']=='/response/next_actions'),None)
                tail=facts.pointer(poe.read_json(file),continuation['pointer'])[continuation['offset']:] if continuation else []
                return reply['next_actions']+tail
            self.assertEqual(complete_actions(bounded),actions)
            action_reads=[row for row in bounded['continuations'] if 'purpose' in row]
            self.assertEqual([row.get('purpose',row['action']) for row in bounded['next_actions']+action_reads],
                             [row['action'] for row in actions])
            for index,row in enumerate(bounded['next_actions']+action_reads):
                if 'purpose' in row:
                    self.assertEqual(row['action'],'view')
                    self.assertEqual(facts.pointer(poe.read_json(file),row['pointer']),actions[index])
            response.update(task_completion={**completion,'pending':['task '+str(i)+' '+('x'*180) for i in range(20)],'errors':[]},
                            character_readiness={**completion,'pending':['role '+str(i)+' '+('x'*820) for i in range(8)],'errors':[]},
                            next_actions=[{'action':'repair_input','detail':'x'*2200} for i in range(12)],facts_summary='x'*6000)
            poe.write_json(file,{'response':response})
            bounded=poe.bound_reply(response,argparse.Namespace(command='evaluate',run=str(run),name='fixture'))
            self.assertLessEqual(len(json.dumps({'ok':True,**bounded},ensure_ascii=False))+1,12000)
            self.assertEqual(complete_actions(bounded),response['next_actions'])
            for key in ('task_completion','character_readiness'):
                self.assertEqual(bounded[key]['pending_total'],len(response[key]['pending']))
                self.assertEqual(bounded[key]['pending_is_preview'],len(bounded[key]['pending'])<len(response[key]['pending']))
            for continuation in bounded['continuations']:
                facts.pointer(poe.read_json(file),continuation['pointer'])
            self.assertLess(len(json.dumps(bounded,ensure_ascii=False)),12000)

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
        # Large diagnostics must not bury the current edit/review arguments.
        diagnostics[0]['source_diagnostic']={'actual':'observed context '*1000}
        with tempfile.TemporaryDirectory() as folder:
            run=Path(folder);details=run/'candidates/current/evaluation.json'
            response={'status':'draft','file_exported':False,'files':[], 'next_actions':actions}
            poe.write_json(details,{'response':response})
            bounded=poe.bound_reply(response,argparse.Namespace(command='evaluate',run=str(run),name='current'))
            indexed={row['purpose']:row for row in bounded['continuations'] if 'purpose' in row}
            self.assertEqual(indexed['review_claims']['changes'],action['changes'])
            review=next(row for row in actions if row['action']=='review_current_result')
            for key in ('edit_arguments','review_file','template_pointer'):
                self.assertEqual(indexed['review_current_result'][key],review[key])
            continuation=next(row for row in bounded['continuations'] if row['pointer']=='/response/next_actions')
            self.assertEqual(bounded['next_actions']+actions[continuation['offset']:],actions)
            self.assertLessEqual(len(json.dumps({'ok':True,**bounded},ensure_ascii=False))+1,12000)
            self.assertEqual((bounded['status'],bounded['file_exported'],bounded['files']),('draft',False,[]))

    def test_calculation_facts_exposes_current_keys_without_inventing_values(self):
        result={'stats':{'SeasonSpecificObservation':0,'SeasonSpecificFlag':False,'Life':1},
                'rules':[{'id':'current-native-rule','applied':False,'value':0,'scope':'native wording'}]}
        before=copy.deepcopy(result)
        observed=poe.calculation_facts(result)
        self.assertEqual(observed['rules'],result['rules'])
        self.assertEqual(result,before)
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

    def test_claim_and_review_source_scope_does_not_judge_free_prose(self):
        ref='native:candidate#/stats/FutureValue'
        registry={'user':{'kind':'user_statement','status':'user_declared','origin':'agent_recorded_user_statement'}}
        native={'candidate':{'stats':{'FutureValue':21}}}
        raw=[{'claim_id':'preference','text':'A preference with 123 in its wording','subjects':['goal'],'evidence_refs':['user']},
             {'claim_id':'observation','text':'{{fact:'+ref+'}}; model interpretation','subjects':['goal']}]
        claims=facts.assess_claims(raw,registry,native)
        self.assertEqual(claims[0]['status'],'model_assessed')
        self.assertTrue(claims[0]['evidence_scope']['user_declarations_only'])
        self.assertFalse(claims[1]['evidence_scope']['user_declarations_only'])
        self.assertEqual(claims[1]['evidence_scope']['counts'],{'native_observed':1})
        self.assertEqual(facts.claim_display(claims[0])['evidence_scope'],claims[0]['evidence_scope'])
        packet,_=facts.final_review({},registry,claims,{}, {},native=native)
        review={'token':packet['token'],'findings':'Model reviews the stated purposes','unresolved':[],
                'objectives':[], 'conclusions':[review_row('claim_id',c['claim_id'],'Declared purpose','Model finding',
                    verdict='supported',evidence_refs=c['evidence_refs']) for c in claims]}
        current,pending=facts.final_review({},registry,claims,{}, {'review':review},native=native)
        self.assertFalse(pending)
        self.assertEqual(current['status'],'model_reviewed')
        self.assertEqual(current['conclusions'][0]['verdict'],'supported')
        self.assertTrue(current['conclusions'][0]['evidence_scope']['user_declarations_only'])
        review['conclusions'][1]['evidence_refs']=['user']
        review['conclusions'][1]['findings']='{{fact:'+ref+'}}; model interpretation'
        current,pending=facts.final_review({},registry,claims,{}, {'review':review},native=native)
        self.assertFalse(pending)
        self.assertEqual(current['conclusions'][1]['evidence_scope']['counts'],{'user_declared':1,'native_observed':1})
        self.assertFalse(current['conclusions'][1]['evidence_scope']['user_declarations_only'])
        for prefix in ('','/checked_route'):
            action=next(row for row in poe.delivery_actions({'status':'draft','errors':[],'pending':[],'claims':claims},
                'source.json','details.json','review.json',prefix) if row['action']=='review_evidence_scope')
            self.assertEqual([row['claim_id'] for row in action['claims']],['preference'])
            self.assertEqual(action['reference_contract_pointer'],prefix+'/reference_contract')

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
                # Multiple real recovery records must remain readable without
                # recalculation; output snapshots cannot overwrite run state.
                state=poe.read_json(run/'run.json')
                state['input_sha256']=bx.digest((run/'original.xml').read_bytes())
                state['candidates'].update({f'missing-{i}':{} for i in range(30)})
                poe.write_json(run/'run.json',state)
                stdout,stderr=io.StringIO(),io.StringIO()
                with redirect_stdout(stdout),redirect_stderr(stderr):
                    code=poe.main(['status','--run',str(run)])
                self.assertEqual(code,2)
                self.assertEqual(json.loads(stderr.getvalue())['diagnostic']['code'],'response_too_large')
                saved=run/'status-snapshot.json';stdout=io.StringIO()
                with redirect_stdout(stdout):
                    code=poe.main(['status','--run',str(run),'--out',str(saved)])
                self.assertEqual(code,0);self.assertLessEqual(len(stdout.getvalue()),12000)
                complete=poe.read_json(saved)
                self.assertEqual(len(complete['candidate_records']),31)
                response=json.loads(stdout.getvalue());self.assertTrue(response['continuations'])
                for continuation in response['continuations']:
                    self.assertEqual(Path(continuation['file']),saved)
                    facts.pointer(complete,continuation['pointer'])
                self.assertEqual(response['runtime']['requests'],0)
                before=(run/'run.json').read_bytes();stderr=io.StringIO()
                with redirect_stderr(stderr):
                    self.assertEqual(poe.main(['status','--run',str(run),'--out',str(run/'run.json')]),2)
                self.assertEqual((run/'run.json').read_bytes(),before)
                stdout=io.StringIO()
                with redirect_stdout(stdout):
                    code=poe.main(['start','--invoked','--input',str(run/'original.xml'),'--run',str(run)])
                self.assertEqual(code,0);self.assertLess(len(stdout.getvalue()),2000)
                self.assertEqual(json.loads(stdout.getvalue())['next_actions'][0]['command'],'status')
                self.assertEqual((run/'run.json').read_bytes(),before)

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


class ManualActionWindowTests(unittest.TestCase):
    def fixture(self, modes=(('a','manual',.5,'player'),('b','manual',.5,'player'))):
        result=resource_fixture();result['resource_rules']={'energy_shield_protects_mana':False}
        costs={key:0 for key in ('ManaCost','LifeCost','ESCost','ManaPerSecondCost','LifePerSecondCost','ESPerSecondCost')};costs['Speed']=2
        result['stats'].update(costs)
        result['active_skills']=[{'id':ref,'skill_ref':ref,'is_main':i==0,'actor':actor,'costs':dict(costs),
            'native_timing':{'action_mode':mode,'action_time_seconds':seconds,'action_time_source':'current native player output'}}
            for i,(ref,mode,seconds,actor) in enumerate(modes)]
        return result

    def audit(self, result, timeline=None, usage=None):
        usage=usage or [{'skill_ref':s['skill_ref'],'claim_id':s['skill_ref']} for s in result['active_skills']]
        claims=[{'claim_id':s['skill_ref'],'subjects':['skill:'+s['skill_ref']],'status':'model_assessed'} for s in result['active_skills']]
        return resources.assess(result,{}, {'usage':usage,**({'timeline':timeline} if timeline else {})},claims)

    def test_cross_skill_overlap_blocks_completion_without_refuting_payments(self):
        result=self.fixture();timeline={'duration_seconds':2,'repeat':False,'events':[{'skill_ref':'a','at':0},{'skill_ref':'b','at':.25}]}
        audit=self.audit(result,timeline)
        self.assertTrue(audit['errors'])
        self.assertEqual(audit['timeline']['status'],'supported')
        self.assertTrue(all(r['status']=='supported' for r in audit['timeline']['resources']))
        self.assertEqual(audit['manual_actions']['status'],'refuted')
        timeline['events'][1]['at']=.5
        self.assertFalse(self.audit(result,timeline)['errors'])
        timeline['events'].pop()
        self.assertFalse(self.audit(result,timeline)['errors'])

    def test_native_mode_not_damage_actor_controls_shared_time(self):
        result=self.fixture((('a','instant',0,'player'),('b','automatic',0,'player'),('c','manual',.5,'minion'),('d','manual',.5,'player')))
        timeline={'duration_seconds':2,'repeat':False,'events':[{'skill_ref':ref,'at':0} for ref in ('a','b','c')]}
        self.assertFalse(self.audit(result,timeline)['errors'])
        timeline['events'].append({'skill_ref':'d','at':.25})
        self.assertTrue(self.audit(result,timeline)['errors'])

    def test_repeat_and_preparation_seams_share_the_same_action_clock(self):
        result=self.fixture();timeline={'duration_seconds':1,'repeat':True,'events':[{'skill_ref':'a','at':.1},{'skill_ref':'b','at':.9}]}
        self.assertTrue(self.audit(result,timeline)['errors'])
        timeline['repeat']=False
        self.assertFalse(self.audit(result,timeline)['errors'])  # payment window may end during its final action
        timeline={'duration_seconds':1,'repeat':False,'events':[{'skill_ref':'b','at':0}],
                  'preparation':{'duration_seconds':1,'events':[{'skill_ref':'a','at':.8}]}}
        self.assertTrue(self.audit(result,timeline)['errors'])
        timeline['events'][0]['at']=.3
        self.assertFalse(self.audit(result,timeline)['errors'])

    def test_unknown_action_duration_is_not_free_or_a_gameplay_failure(self):
        result=self.fixture((('a','manual',None,'player'),))
        audit=self.audit(result,{'duration_seconds':2,'repeat':False,'events':[{'skill_ref':'a','at':0}]})
        self.assertFalse(audit['errors'])
        self.assertEqual(audit['manual_actions']['status'],'unknown')
        self.assertTrue(any(x.startswith('capability:manual_action:') for x in audit['pending']))
        self.assertEqual(audit['timeline']['status'],'supported')

    def test_continuous_mean_capacity_is_checked_without_inventing_exact_events(self):
        result=self.fixture((('a','manual',.6,'player'),('b','manual',.6,'player')))
        usage=[{'skill_ref':ref,'claim_id':ref,'casts_per_second':1,'continuous':True} for ref in ('a','b')]
        audit=self.audit(result,usage=usage)
        self.assertTrue(audit['errors'])
        self.assertEqual(audit['manual_actions']['status'],'refuted')
        usage[1]['casts_per_second']=.5
        audit=self.audit(result,usage=usage)
        self.assertFalse(audit['errors'])
        self.assertEqual(audit['manual_actions']['status'],'supported')
        self.assertEqual(audit['manual_actions']['basis'],'declared_mean_capacity_only')

    def test_different_mean_windows_are_not_assumed_concurrent(self):
        result=self.fixture((('a','manual',.6,'player'),('b','manual',.6,'player')))
        usage=[{'skill_ref':ref,'claim_id':ref,'casts_per_second':1,'active_seconds':seconds} for ref,seconds in (('a',1),('b',2))]
        audit=self.audit(result,usage=usage)
        self.assertFalse(audit['errors'])
        self.assertEqual(audit['manual_actions']['status'],'unknown')


class ResourceRegressionTests(unittest.TestCase):
    def test_declared_points_cannot_override_observed_native_global_limits(self):
        result=resource_fixture()
        for maximum in (5,12):
            with self.subTest(maximum=maximum):
                result.update(ascendancy_points=maximum+1,point_limits={'status':'observed',
                    'source':'build.controls.pointDisplay.str','pools':{'ascendancy':{'used':maximum+1,'maximum':maximum}}})
                audit=resources.assess(result,{'ascendancy_points':maximum+1},{})
                self.assertTrue(audit['errors'])
                budget=next(row for row in audit['point_budgets'] if row['pool']=='ascendancy')
                self.assertEqual(budget['native_global_maximum'],maximum)
                self.assertEqual(budget['status'],'exceeded_native_maximum')
                result['ascendancy_points']=maximum
                result['point_limits']['pools']['ascendancy']['used']=maximum
                self.assertTrue(resources.assess(result,{'ascendancy_points':maximum+1},{})['errors'])
                valid=resources.assess(result,{}, {})
                self.assertFalse(valid['errors'])
                self.assertIn('progress:ascendancy',valid['pending'])
                result['point_limits']={'status':'unsupported'}
                self.assertNotIn('native_global_maximum',resources.assess(result,{}, {})['point_budgets'][1])

    def test_declared_point_budget_cannot_override_current_native_progress_bounds(self):
        result=resource_fixture()
        result.update(level=20,points=24,progress_limits={'status':'observed','chapters':[
            {'entering_act':1,'quest_points':0,'extra_points':0},
            {'entering_act':2,'quest_points':2,'extra_points':0},
            {'entering_act':3,'quest_points':4,'extra_points':1}]})
        intent={'normal_points_available':24,'act':1}
        first=resources.assess(result,intent,{})
        self.assertTrue(first['errors'])
        result['progress_limits']['chapters'][1]['quest_points']=5
        changed=resources.assess(result,intent,{})
        self.assertFalse(changed['errors'])
        self.assertEqual(changed['point_budgets'][0]['native_maximum'],24)
        self.assertEqual(changed['point_budgets'][0]['status'],'within_declared_limit')
        unknown=resources.assess(result,{}, {})
        self.assertIn('progress:normal',unknown['pending'])

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
    def test_native_duration_at_window_end_is_event_relative_arithmetic_not_coverage(self):
        result,timeline=self.fixture()
        result['active_skills'][0]['description']='长原生说明 '*1200
        result['active_skills'][0]['native_timing']['duration_seconds']=3
        timeline.update(repeat=False,preparation={'duration_seconds':5.25,'events':[{'skill_ref':'main','at':4}]})
        audit=resources.timeline_assessment(result,timeline)
        position=audit['preparation']['skill_schedules']['main']['native_duration_at_window_end']
        self.assertEqual(position['last_event_at'],4)
        self.assertEqual(position['window_end_at'],5.25)
        self.assertEqual(position['elapsed_seconds'],1.25)
        self.assertEqual(position['nominal_remaining_seconds'],1.75)
        self.assertEqual(position['effect_coverage_status'],'unmeasured')
        self.assertEqual(audit['native_snapshot'],audit['preparation']['native_snapshot'])
        published=poe.publication('draft',[],[],{'status':'unknown'},{'candidate':result},resource_audits={'candidate':{'timeline':audit}})
        statement=next(row for row in published['statements'] if row['kind']=='skill_schedule' and row['phase']=='preparation' and row['skill_ref']=='main')
        self.assertEqual(statement['values']['native_duration_at_window_end'],position)
        self.assertIn('1.75',statement['text'])
        self.assertIn('名义剩余',statement['text'])
        self.assertIn('不证明效果',statement['text'])
        summary=published['facts_summary']
        self.assertIn('最低余额',summary[:5000])
        self.assertIn('名义剩余',summary[:5000])
        self.assertLess(summary.index('时间线测量范围'),summary.index('最低余额'))
        self.assertEqual(summary.count(statement['text']),1)

    def test_native_duration_end_position_keeps_missing_unknown_and_expired_zero(self):
        result,timeline=self.fixture();timeline['repeat']=False
        timing=result['active_skills'][0]['native_timing']
        for value in (None,0):
            timing['duration_seconds']=value
            with self.subTest(duration=value):
                audit=resources.timeline_assessment(result,timeline)
                self.assertIsNone(audit['skill_schedules']['main'].get('native_duration_at_window_end'))
        timing['duration_seconds']=.75
        audit=resources.timeline_assessment(result,timeline)
        self.assertEqual(audit['skill_schedules']['main']['native_duration_at_window_end']['nominal_remaining_seconds'],0)
        timeline['events']=[row for row in timeline['events'] if row['skill_ref']!='main']
        audit=resources.timeline_assessment(result,timeline)
        self.assertIsNone(audit['skill_schedules']['main'].get('native_duration_at_window_end'))

    def test_native_effect_end_condition_does_not_relabel_the_payment_projection(self):
        result,timeline=self.fixture()
        result['active_skills'][0]['native_timing']={**result['active_skills'][0]['native_timing'],'duration_seconds':.75,'cooldown_seconds':.5,
            'cooldown_paused_during_effect':True,'source':'Current native skill modifier and output'}
        audit=resources.timeline_assessment(result,timeline)
        self.assertEqual(audit['resources'][0]['status'],'supported')
        condition=next(row for row in audit['timing_conditions'] if row['skill_ref']=='main')
        self.assertEqual(condition['status'],'unknown')
        self.assertEqual(condition['interval_basis'],'scheduled_event_minimum_gap')
        self.assertEqual(condition['full_duration_recast_seconds'],1.25)
        self.assertEqual(condition['required_early_end_within_seconds'],.5)
        readiness=resources.assess(result,{}, {'timeline':timeline})
        self.assertIn('capability:action_timing:main',readiness['pending'])
        self.assertEqual(readiness['timeline']['status'],'supported')
        published=poe.publication('draft',[],[],{'status':'unknown'},{'candidate':result},readiness=readiness,keys=[])
        statement=next(row for row in published['statements'] if row['kind']=='native_timing_condition')
        self.assertIn(statement['text'],published['facts_summary'])
        self.assertEqual(statement['status'],'unknown')
        timeline['events'][-1]['at']=1.5
        self.assertFalse(resources.timeline_assessment(result,timeline)['timing_conditions'])
        timeline['events'][-1]['at']=1
        result['active_skills'][0]['native_timing']['cooldown_paused_during_effect']=False
        self.assertFalse(resources.timeline_assessment(result,timeline)['timing_conditions'])

    def test_nominal_payment_rates_and_timeline_gaps_share_the_native_recast_check(self):
        result,timeline=self.fixture()
        result['active_skills'][0]['native_timing']={'duration_seconds':.75,'cooldown_seconds':.5,
            'cooldown_paused_during_effect':True}
        timed=resources.assess(result,{}, {'timeline':timeline})
        plan={'usage':[{'skill_ref':'main','casts_per_second':1}]}
        averaged=resources.assess(result,{},plan)
        condition=averaged['timing_conditions'][0]
        self.assertEqual(condition['phase'],'usage')
        self.assertEqual(condition['native_timing'],timed['timing_conditions'][0]['native_timing'])
        self.assertEqual(condition['full_duration_recast_rate'],.8)
        self.assertEqual(condition['interval_basis'],'declared_mean_rate')
        self.assertEqual(condition['declared_mean_interval_seconds'],timed['timing_conditions'][0]['observed_minimum_interval_seconds'])
        self.assertNotIn('observed_minimum_interval_seconds',condition)
        self.assertNotIn('required_early_end_within_seconds',condition)
        self.assertIn('capability:action_timing:main',averaged['pending'])
        self.assertEqual(resources.native_rate(result['active_skills'][0]),2)
        self.assertEqual(averaged['resources'][0]['cost_per_second'],20)
        published=poe.publication('draft',[],[],{'status':'unknown'},{'candidate':result},readiness=averaged,keys=[])
        statement=next(row for row in published['statements'] if row['kind']=='native_timing_condition')
        self.assertEqual(statement['phase'],'usage');self.assertIn(statement['text'],published['facts_summary'])
        self.assertIn('平均间隔不证明实际最短间隔',statement['text'])
        plan['usage'][0]['casts_per_second']=.8
        self.assertFalse(resources.assess(result,{},plan)['timing_conditions'])
        plan['usage'][0]['casts_per_second']=0
        self.assertFalse(resources.assess(result,{},plan)['timing_conditions'])
        result['active_skills'][0]['native_timing']['cooldown_paused_during_effect']=False
        plan['usage'][0]['casts_per_second']=1
        self.assertFalse(resources.assess(result,{},plan)['timing_conditions'])

    def test_action_timing_uses_existing_current_condition_review_without_refuting_payments(self):
        result,timeline=self.fixture()
        result['active_skills'][0]['native_timing']={**result['active_skills'][0]['native_timing'],'duration_seconds':.75,'cooldown_seconds':.5,
            'cooldown_paused_during_effect':True}
        claim={'claim_id':'current','subjects':['skill:main','skill:move','objective:requested'],
               'evidence_refs':['native:resources#/timing_conditions/0'],'missing_evidence':[],
               'check_status':'not_requested','status':'model_assessed','diagnostics':[]}
        usage=[{'skill_ref':ref,'claim_id':'current'} for ref in ('main','move')]
        audit=resources.assess(result,{}, {'timeline':timeline,'usage':usage},[claim])
        subject='capability:action_timing:main'
        self.assertEqual(audit['pending'],[subject]);self.assertEqual(audit['errors'],[])
        self.assertEqual(audit['timeline']['status'],'supported')
        reviewed={'reference_status':'resolved','verdict':'supported','conditions':[],'unimplemented':[]}
        review={'completion_pending':[],'conclusions':[{'claim_id':'current',**reviewed}],
                'objectives':[{'objective_id':'requested',**reviewed}],'condition_reviews':[],'facts':{},'diagnostics':[]}
        coverage=[{'objective_id':'requested','basis_status':'user_declared','claim_ids':['current']}]
        def completion():return poe.plan_completion('build_plan',audit['errors'],audit['pending'],audit['diagnostics'],coverage,[claim],review)
        self.assertEqual(completion()['status'],'draft')
        review['condition_reviews']=[{'subjects':[subject],'claim_ids':['current'],'reference_status':'resolved',
            'conditions':['Use the shorter interval only after verifying the actual early termination.'],
            'fallback':'Otherwise use the full-duration recast branch.','evidence_refs':claim['evidence_refs']}]
        completed=completion()
        self.assertEqual(completed['status'],'scoped_result',completed)
        self.assertEqual(completed['conditions'][0]['subjects'],[subject])
        self.assertEqual(audit['pending'],[subject])
        review['condition_reviews'][0]['fallback']=''
        self.assertEqual(completion()['status'],'draft')

    def test_active_flask_snapshot_needs_coverage_for_finite_declared_windows_too(self):
        result,timeline=self.fixture()
        result['equipped']=[{'slot':'Flask 1','active':True,'flask_local':{'duration':4}}]
        subject='capability:flask_coverage:Flask 1'
        finite=resources.assess(result,{}, {'timeline':timeline})
        self.assertIn(subject,finite['pending'])
        self.assertEqual(finite['timeline']['status'],'supported')
        averaged=resources.assess(result,{}, {'usage':[{'skill_ref':'main','casts_per_second':1,'active_seconds':10}]})
        self.assertIn(subject,averaged['pending'])
        empty=resources.assess(result,{}, {'usage':[{'skill_ref':'main','casts_per_second':1,'active_seconds':0}]})
        self.assertNotIn(subject,empty['pending'])
        self.assertNotIn(subject,resources.assess(result,{}, {})['pending'])
        result['equipped'][0]['active']=False
        self.assertNotIn(subject,resources.assess(result,{}, {'timeline':timeline})['pending'])

    def test_timeline_exposes_actual_fixed_snapshot_and_action_definition(self):
        result,timeline=self.fixture()
        result.update(xml_sha256='a'*64,engine_fingerprint='b'*64,
            config_controls={'future_switch':{'type':'check','value':False}})
        result['active_skills'][0].update(description='Uses an identified material, not another created entity.',
            skill_types=['Spell'],native_timing={'cooldown_seconds':.5,'duration_seconds':None})
        timeline['preparation']={'duration_seconds':1,'events':[{'skill_ref':'move','at':0}]}
        audit=resources.timeline_assessment(result,timeline)
        self.assertEqual(audit['native_snapshot']['xml_sha256'],result['xml_sha256'])
        self.assertEqual(audit['native_snapshot']['effect_state'],engine.effect_state(result))
        self.assertEqual(audit['preparation']['native_snapshot'],audit['native_snapshot'])
        action=audit['skill_schedules']['main']['native_action']
        self.assertEqual(action['description'],result['active_skills'][0]['description'])
        self.assertEqual(action['native_timing'],result['active_skills'][0]['native_timing'])

    def test_preparation_phase_with_invalid_timeline_is_a_located_input_error(self):
        for value in (None,[],{},'invalid'):
            with self.subTest(value=value):
                with self.assertRaises(bx.ContractError) as caught:
                    resources.input_shape({'usage':[{'skill_ref':'future','phase':'preparation'}],'timeline':value})
                self.assertEqual(caught.exception.diagnostic['location'],'/timeline/preparation')

    def test_unplanned_skill_with_unknown_native_fee_is_not_treated_as_free(self):
        result,timeline=self.fixture()
        result['active_skills'].append({'skill_ref':'future-unknown-fee','actor':'player',
                                        'costs':{'Speed':2,'LifeCost':0,'LifePerSecondCost':0,'ESCost':0,'ESPerSecondCost':0}})
        audit=resources.timeline_assessment(result,timeline)
        self.assertEqual(audit['resources'][0]['status'],'unknown')
        self.assertIsNone(audit['resources'][0]['scheduled_payment_total'])
        self.assertIsNone(audit['resources'][0]['scheduled_payments_by_skill']['main'])
        self.assertIn('payment_projection:future-unknown-fee',audit['resources'][0]['gaps'])
        self.assertEqual(audit['resources'][1]['status'],'supported')

    def test_preparation_needs_real_duration_and_checks_transition_frequency(self):
        result,timeline,usage=self.preparation_fixture()
        missing=copy.deepcopy(timeline);del missing['preparation']
        with self.assertRaises(bx.ContractError) as caught: resources.assess(result,{}, {'timeline':missing,'usage':usage})
        self.assertEqual(caught.exception.diagnostic['location'],'/timeline/preparation')
        timeline['preparation']['duration_seconds']=.55
        timeline['events'].append({'skill_ref':'future-startup','at':0})
        audit=resources.assess(result,{}, {'timeline':timeline,'usage':usage})
        self.assertTrue(any(row['code']=='timeline_transition_interval' and row['skill_ref']=='future-startup' for row in audit['diagnostics']))
        self.assertEqual(audit['timeline']['status'],'refuted')

    def test_unknown_preparation_balance_propagates_and_auto_fees_remain_in_combat(self):
        result,timeline,usage=self.preparation_fixture()
        del result['stats']['ManaRegenRecovery']
        audit=resources.assess(result,{}, {'timeline':timeline,'usage':usage})
        self.assertIsNone(audit['timeline']['resources'][0]['initial'])
        self.assertEqual(audit['timeline']['resources'][0]['status'],'unknown')
        result,timeline=self.automatic_fixture()
        timeline['events']=[event for event in timeline['events'] if event['skill_ref']!='future-auto']
        timeline['preparation']={'duration_seconds':4,'events':[{'skill_ref':'future-auto','at':0},{'skill_ref':'future-auto','at':2}]}
        audit=resources.assess(result,{}, {'timeline':timeline,'usage':[{'skill_ref':'future-auto','phase':'preparation'}]})
        self.assertEqual(audit['timeline']['preparation']['status'],'supported')
        self.assertIn('automatic:future-auto',audit['timeline']['resources'][0]['gaps'])
        self.assertEqual(audit['sustained_use']['status'],'unknown')

    def test_usage_diagnoses_missing_claim_identifier_with_current_subject_choices(self):
        result,timeline=self.fixture()
        claims=[{'claim_id':'actual-payment','subjects':['skill:main'],'status':'model_assessed'}]
        audit=resources.assess(result,{}, {'timeline':timeline,'usage':[{'skill_ref':'main','claim_id':'absent'}]},claims)
        diagnostic=next(row for row in audit['diagnostics'] if row['code']=='usage_claim_missing')
        self.assertEqual(diagnostic['location'],'/usage/0/claim_id')
        self.assertEqual(diagnostic['actual'],'absent')
        self.assertEqual(diagnostic['expected'],['actual-payment'])
        self.assertEqual(diagnostic['required_subject'],'skill:main')
        schedule=diagnostic['payment_schedule']
        self.assertEqual((schedule['phase'],schedule['event_count'],schedule['first_at'],schedule['last_at']),('combat',2,0,1))
        self.assertEqual(schedule['duration_seconds'],timeline['duration_seconds'])
        self.assertEqual(schedule['native_rate'],2)
        self.assertEqual(schedule['window_average_rate'],2/timeline['duration_seconds'])
        facts.resolve_reference(schedule['reference'],{}, {'resources':audit})
        self.assertEqual(audit['timeline']['skill_schedules']['main']['claim_id'],'absent')

    def test_preparation_claim_feedback_uses_current_phase_and_escaped_reference(self):
        result,timeline,usage=self.preparation_fixture()
        ref='future/startup~1'
        result['active_skills'][-1]['skill_ref']=ref
        for event in timeline['preparation']['events']: event['skill_ref']=ref
        usage=[{'skill_ref':ref,'phase':'preparation','claim_id':'current-prep'}]
        claims=[{'claim_id':'current-prep','subjects':['skill:'+ref,'objective:prepare'],'status':'unresolved'}]
        audit=resources.assess(result,{}, {'timeline':timeline,'usage':usage},claims,reference_name='resources:stage:future')
        diagnostic=next(row for row in audit['diagnostics'] if row['code']=='usage_claim_unresolved')
        schedule=diagnostic['payment_schedule']
        self.assertEqual(schedule['phase'],'preparation')
        self.assertEqual(schedule['objective_ids'],['prepare'])
        self.assertEqual(schedule['event_count'],2)
        self.assertEqual(schedule['duration_seconds'],timeline['preparation']['duration_seconds'])
        reference=schedule['reference']
        self.assertTrue(reference.endswith('/future~1startup~01'))
        observed=facts.resolve_reference(reference,{}, {'resources:stage:future':audit})
        self.assertEqual(observed['value']['scheduled_payments'],schedule['scheduled_payments'])
        claims[0]['status']='model_assessed'
        assessed=resources.assess(result,{}, {'timeline':timeline,'usage':usage},claims,reference_name='resources:stage:future')
        self.assertFalse(any(row['code'].startswith('usage_claim_') for row in assessed['diagnostics']))
        self.assertEqual(assessed['timeline']['preparation']['skill_schedules'][ref]['claim_id'],'current-prep')
        self.assertEqual(assessed['execution_coverage']['status'],'unknown')

    def preparation_fixture(self):
        result,timeline=self.fixture()
        costs={'ManaCost':30,'ManaPerSecondCost':60,'LifeCost':0,'LifePerSecondCost':0,'ESCost':0,'ESPerSecondCost':0,'Speed':2}
        result['active_skills'].append({'skill_ref':'future-startup','actor':'player','costs':costs})
        timeline['preparation']={'duration_seconds':1,'events':[{'skill_ref':'future-startup','at':0},{'skill_ref':'future-startup','at':.5}]}
        usage=[{'skill_ref':'future-startup','claim_id':'setup','phase':'preparation'}]
        return result,timeline,usage

    def test_preparation_carries_its_actual_balance_into_combat_without_pool_reset(self):
        result,timeline,usage=self.preparation_fixture()
        audit=resources.assess(result,{}, {'timeline':timeline,'usage':usage})
        self.assertEqual(audit['timeline']['preparation']['resources'][0]['final'],10)
        self.assertEqual(audit['timeline']['resources'][0]['initial'],10)
        self.assertEqual(audit['timeline']['resources'][0]['status'],'refuted')
        timeline['preparation']['duration_seconds']=2
        ready=resources.assess(result,{}, {'timeline':timeline,'usage':usage})
        self.assertEqual(ready['timeline']['preparation']['resources'][0]['final'],30)
        self.assertEqual(ready['timeline']['resources'][0]['initial'],30)
        self.assertEqual(ready['timeline']['status'],'supported')
        self.assertNotIn('unscheduled:future-startup',ready['timeline']['resources'][0]['gaps'])
        self.assertEqual(ready['execution_coverage']['effect_coverage'],'unmeasured')
        self.assertEqual(ready['timeline']['skill_schedules']['main']['event_count'],2)
        self.assertEqual(ready['timeline']['skill_schedules']['move']['event_count'],1)

    def test_publication_exposes_both_actual_windows_and_preserves_unknown_balances(self):
        result,timeline,usage=self.preparation_fixture()
        timeline['preparation']['duration_seconds']=2
        timeline['repeat']=False
        def publish(stage=False):
            audit=resources.assess(result,{}, {'timeline':timeline,'usage':usage})
            return poe.publication('draft',[],[],{'status':'unknown'},{'candidate':result},
                                   **({'resource_audits':{'stage:future':audit}} if stage else {'readiness':audit}),keys=[])
        publication=publish()
        windows={(s['phase'],s['subject']):s for s in publication['statements'] if s['kind']=='resource'}
        setup=windows[('preparation','Mana')];combat=windows[('combat','Mana')]
        self.assertEqual((setup['window']['at'],setup['window']['last_event_at'],setup['window']['final']),(2,.5,30))
        self.assertEqual(combat['window']['initial'],setup['window']['final'])
        self.assertEqual(setup['window']['event_count'],2)
        self.assertFalse(combat['window']['repeat'])
        self.assertIn(setup['text'],publication['facts_summary'])
        snapshot=next(s for s in publication['statements'] if s['kind']=='resource_snapshot')
        self.assertIn(snapshot['text'],publication['facts_summary'])
        self.assertTrue(all(scope in snapshot['text'] for scope in snapshot['scope']))
        self.assertEqual(next(s['status'] for s in publication['statements'] if s['kind']=='sustained_use'),'unknown')
        staged=publish(stage=True)
        stage_setup=next(s for s in staged['statements'] if s['kind']=='resource' and s['phase']=='preparation' and s['subject']=='Mana')
        self.assertEqual(stage_setup['window'],setup['window'])
        self.assertEqual(stage_setup['calculation'],'stage:future')
        del result['stats']['ManaRegenRecovery']
        unknown=publish()
        row=next(s for s in unknown['statements'] if s['kind']=='resource' and s['phase']=='combat' and s['subject']=='Mana')
        self.assertIsNone(row['window']['initial'])
        self.assertIsNone(row['window']['final'])
        self.assertEqual(row['status'],'unknown')

    def test_preparation_declaration_without_actual_events_remains_an_input_gap(self):
        result,timeline,usage=self.preparation_fixture()
        timeline['preparation']['events']=[]
        audit=resources.assess(result,{}, {'timeline':timeline,'usage':usage})
        self.assertEqual(audit['timeline']['preparation']['status'],'unknown')
        self.assertEqual(audit['sustained_use']['status'],'unknown')
        missing=next(row for row in audit['diagnostics'] if row['code']=='timeline_skill_unscheduled')
        self.assertEqual(missing['location'],'/timeline/preparation/events')
        self.assertEqual(missing['skill_ref'],'future-startup')
        repair=missing['repair_options']['unused']['usage']
        resources.input_shape({'timeline':timeline,'usage':[repair]})
        self.assertEqual(repair['phase'],'preparation')
        self.assertEqual(resources.timeline_assessment(result,timeline,[repair])['preparation']['status'],'unknown')

    def test_active_automatic_payments_cover_preparation_and_keep_their_phase(self):
        result,timeline=self.automatic_fixture()
        timeline['preparation']={'duration_seconds':4,'events':[{'skill_ref':'main','at':0}]}
        missing=resources.timeline_assessment(result,timeline)
        setup=missing['preparation']
        self.assertEqual(setup['status'],'unknown')
        self.assertIn('automatic:future-auto',setup['resources'][0]['gaps'])
        self.assertIsNone(missing['resources'][0]['initial'])
        self.assertEqual(missing['resources'][1]['status'],'supported')
        self.assertTrue(any(row['code']=='timeline_automatic_schedule_incomplete'
                            and row['location']=='/timeline/preparation/events' for row in missing['diagnostics']))
        timeline['preparation']['events'] += [{'skill_ref':'future-auto','at':0},{'skill_ref':'future-auto','at':2}]
        measured=resources.timeline_assessment(result,timeline)
        self.assertEqual(measured['status'],'supported')
        self.assertEqual(measured['preparation']['resources'][0]['scheduled_payment_total'],40)
        self.assertEqual(measured['resources'][0]['initial'],measured['preparation']['resources'][0]['final'])
        for event in timeline['events']:
            if event['skill_ref']=='future-auto':event['at']+=.5
        shifted=resources.timeline_assessment(result,timeline)
        self.assertEqual(shifted['preparation']['status'],'supported')
        self.assertEqual(shifted['resources'][0]['status'],'unknown')
        self.assertIsNone(shifted['resources'][0]['scheduled_payment_total'])
        self.assertEqual(shifted['automatic_schedules']['future-auto']['previous_window']['gap_seconds'],2.5)

    def automatic_fixture(self):
        result,timeline=self.fixture();timeline['repeat']=False
        costs={'ManaCost':10,'ManaPerSecondCost':5,'LifeCost':0,'LifePerSecondCost':0,'ESCost':0,'ESPerSecondCost':0,'SkillTriggerRate':.5}
        result['active_skills'].append({'skill_ref':'future-auto','actor':'player','trigger':'Native trigger description','costs':costs,
                                        'usage_rate':{'value':.5,'metric':'SkillTriggerRate','basis':'native_action_rate'}})
        timeline['events'] += [{'skill_ref':'future-auto','at':0},{'skill_ref':'future-auto','at':2}]
        return result,timeline

    def test_fixed_automatic_payments_keep_balances_when_a_window_has_no_payment_due(self):
        result,_=self.automatic_fixture()
        result['active_skills']=result['active_skills'][-1:]
        result['stats'].update(Mana=100,ManaRegenRecovery=0)
        events=[{'skill_ref':'future-auto','at':at} for at in (1,3,5,7)]
        flat=resources.timeline_assessment(result,{'duration_seconds':8,'repeat':False,'events':events})
        self.assertEqual(flat['status'],'supported')
        for boundary in (.5,1,1.5,3,7.5):
            timeline={'duration_seconds':8-boundary,'repeat':False,
                      'preparation':{'duration_seconds':boundary,'events':[e for e in events if e['at']<boundary]},
                      'events':[{**e,'at':e['at']-boundary} for e in events if e['at']>=boundary]}
            with self.subTest(boundary=boundary):
                split=resources.timeline_assessment(result,timeline)
                self.assertEqual(split['status'],'supported',split['diagnostics'])
                setup,combat=split['preparation']['resources'][0],split['resources'][0]
                self.assertEqual(combat['initial'],setup['final'])
                self.assertEqual(combat['final'],flat['resources'][0]['final'])
                self.assertEqual(setup['scheduled_payment_total']+combat['scheduled_payment_total'],40)
        timeline['repeat']=True
        self.assertEqual(resources.timeline_assessment(result,timeline)['status'],'unknown')

    def test_declared_native_trigger_payments_share_clock_without_certifying_execution(self):
        result,timeline=self.automatic_fixture();audit=resources.assess(result,{}, {'timeline':timeline})
        self.assertEqual(audit['timeline']['status'],'supported')
        mana=audit['timeline']['resources'][0]
        self.assertEqual([row['payment'] for row in mana['trace'] if row['skill_ref']=='future-auto'],[10,10])
        self.assertEqual(audit['timeline']['automatic_schedules']['future-auto']['event_count'],2)
        self.assertEqual(audit['execution_coverage']['trigger_realization'],'unmeasured')
        self.assertEqual(audit['sustained_use']['status'],'unknown')
        result['stats']['Mana']=25
        self.assertEqual(resources.timeline_assessment(result,timeline)['resources'][0]['status'],'refuted')

    def test_missing_trigger_rate_incomplete_schedule_and_non_discrete_fee_stay_unknown(self):
        original,timeline=self.automatic_fixture()
        for case in ('native-rate','missing','sparse','non-discrete'):
            result=copy.deepcopy(original);schedule=copy.deepcopy(timeline);skill=result['active_skills'][-1]
            if case=='native-rate':
                skill['usage_rate']['metric']='Speed';del skill['costs']['SkillTriggerRate'];skill['costs']['Speed']=.5
            elif case=='missing':schedule['events']=[row for row in schedule['events'] if row['skill_ref']!='future-auto']
            elif case=='sparse':schedule['events']=[row for row in schedule['events'] if row.get('at')!=2]
            else:skill['costs']['ManaPerSecondCost']=9
            with self.subTest(case=case):
                audit=resources.timeline_assessment(result,schedule)
                self.assertEqual(audit['resources'][0]['status'],'unknown')
                self.assertEqual(audit['resources'][1]['status'],'supported')
                if case in ('missing','sparse'):
                    self.assertTrue(any(row['code']=='timeline_automatic_schedule_incomplete' and row['action']=='edit_input' for row in audit['diagnostics']))

    def test_trigger_phase_and_repeating_seam_must_cover_the_declared_pattern(self):
        result,timeline=self.automatic_fixture()
        for event in timeline['events']:
            if event['skill_ref']=='future-auto':event['at']+=.5
        self.assertEqual(resources.timeline_assessment(result,timeline)['resources'][0]['status'],'supported')
        timeline['duration_seconds']=4;timeline['repeat']=True
        self.assertEqual(resources.timeline_assessment(result,timeline)['status'],'supported')
        timeline['duration_seconds']=3
        self.assertEqual(resources.timeline_assessment(result,timeline)['status'],'refuted')
        timeline.update(duration_seconds=4,repeat=False)
        timeline['events']=[row for row in timeline['events'] if row['skill_ref']!='future-auto']+[{'skill_ref':'future-auto','at':2},{'skill_ref':'future-auto','at':3.99}]
        self.assertEqual(resources.timeline_assessment(result,timeline)['resources'][0]['status'],'unknown')

    def test_trigger_payments_use_native_shield_routing_and_zero_fee_needs_no_schedule(self):
        result,timeline=self.automatic_fixture();result['resource_rules']['energy_shield_protects_mana']=True
        for skill in result['active_skills']:
            skill['costs']['ESPerSecondCost']=skill['costs']['ManaPerSecondCost'];skill['costs']['ManaPerSecondCost']=0
        audit=resources.timeline_assessment(result,timeline)
        self.assertEqual(audit['status'],'supported')
        self.assertTrue(all(row['payment']==0 for row in audit['resources'][0]['trace']))
        self.assertEqual([row['payment'] for row in audit['resources'][2]['trace'] if row['skill_ref']=='future-auto'],[10,10])
        skill=result['active_skills'][-1];skill['no_cost']=True;skill['costs']={};skill['usage_rate']={}
        timeline['events']=[row for row in timeline['events'] if row['skill_ref']!='future-auto']
        self.assertEqual(resources.timeline_assessment(result,timeline)['status'],'supported')

    def test_finite_window_keeps_payment_and_execution_scopes_separate(self):
        result,timeline=self.fixture();timeline['repeat']=False
        audit=resources.assess(result,{}, {'timeline':timeline})
        self.assertEqual(audit['timeline']['status'],'supported')
        self.assertEqual(audit['execution_coverage']['status'],'unknown')
        for row in audit['timeline']['resources']:
            self.assertEqual(row['scheduled_payment_total'],sum(e['payment'] for e in row['trace']))
            self.assertEqual(sum(row['scheduled_payments_by_skill'].values()),row['scheduled_payment_total'])
        ref='native:resources#/timeline/resources/0/scheduled_payment_total'
        observed=facts.resolve_reference(ref,{}, {'resources':audit})
        self.assertIn('{{fact:'+ref+'}}',observed['text_templates'].values())
        packet=poe.publication('draft',[],audit['pending'],{'status':'unknown'},{},readiness=audit)
        execution=next(row for row in packet['statements'] if row['kind']=='execution_coverage')
        self.assertEqual(execution['status'],'unknown')
        resource=next(row for row in packet['statements'] if row['kind']=='resource' and row['subject']=='Mana')
        self.assertEqual(resource['window']['final'],audit['timeline']['resources'][0]['final'])
        self.assertEqual(resource['window']['minimum_balance'],20)
        self.assertLess(resource['window']['minimum_balance'],resource['window']['initial'])
        self.assertEqual(resource['window']['initial'],resource['window']['final'])
        self.assertIn('最低余额 20',resource['text'])
        self.assertEqual(resource['window']['at'],timeline['duration_seconds'])
        scopes=[row for row in packet['statements'] if row['kind']=='timeline_scope']
        self.assertEqual(len(scopes),1)
        self.assertIn(audit['timeline']['scope'],packet['facts_summary'])

    def test_restored_finite_window_does_not_certify_an_invalid_repeat_seam(self):
        result,timeline=self.fixture();result['stats']['ManaRegenRecovery']=200
        timeline.update(repeat=False,events=[{'skill_ref':'main','at':0},{'skill_ref':'move','at':1},{'skill_ref':'main','at':2.9}])
        audit=resources.assess(result,{}, {'timeline':timeline})
        self.assertEqual(audit['timeline']['status'],'supported')
        self.assertTrue(audit['timeline']['resources'][0]['initial_pool_restored'])
        self.assertNotIn('repeatable',audit['timeline']['resources'][0])
        self.assertNotIn('repeatable_at_native_regen_only',audit['resources'][0])
        self.assertEqual(audit['sustained_use']['status'],'unknown')
        packet=poe.publication('draft',[],audit['pending'],{'status':'unknown'},{},readiness=audit)
        for statement in (row for row in packet['statements'] if row['kind']=='resource'):
            self.assertEqual(statement['status'],'supported')
            self.assertNotIn('可在当前固定净回复下重复',statement['text'])
        timeline['repeat']=True
        cycle=resources.assess(result,{}, {'timeline':timeline})
        self.assertEqual(cycle['sustained_use']['status'],'refuted')
        self.assertTrue(any(row['code']=='timeline_repeat_interval' for row in cycle['timeline']['diagnostics']))

    def fixture(self):
        result=resource_fixture();result["resource_rules"]={"energy_shield_protects_mana":False}
        for index,cost in enumerate((20,10)):
            ref="main" if index==0 else "move"
            costs={"ManaCost":cost,"ManaPerSecondCost":cost*2,"LifeCost":0,"LifePerSecondCost":0,"ESCost":0,"ESPerSecondCost":0,"Speed":2}
            skill={"skill_ref":ref,"is_main":index==0,"actor":"player","costs":costs,
                   "native_timing":{"action_mode":"manual","action_time_seconds":.5,"action_time_source":"fixture native duration"}}
            if index==0:result["active_skills"][0]=skill;result["stats"].update(costs)
            else:result["active_skills"].append(skill)
        result["stats"].update(Mana=50,ManaRegenRecovery=20)
        timeline={"duration_seconds":3,"events":[{"skill_ref":"main","at":0},{"skill_ref":"move","at":.5},{"skill_ref":"main","at":1}]}
        return result,timeline

    def test_shared_events_are_paid_on_one_clock_and_order_invariant(self):
        result,timeline=self.fixture();audit=resources.timeline_assessment(result,timeline)
        self.assertEqual(audit["status"],"supported");self.assertTrue(audit["resources"][0]["initial_pool_restored"])
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
        self.assertEqual(row["status"],"supported");self.assertFalse(row["initial_pool_restored"])
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
        self.assertFalse(audit["resources"][0]["initial_pool_restored"]);self.assertEqual(audit["status"],"unknown")
        packet=poe.publication("draft",[],audit["pending"],{"status":"unknown"},{},readiness={"timeline":audit})
        mana=next(row for row in packet['statements'] if row['kind']=='resource' and row['subject']=='Mana')
        self.assertEqual(mana['status'],'supported')
        self.assertIn('此项仅覆盖窗口内支付',mana['text'])

    def test_finite_window_does_not_require_pool_restoration_or_loop_seam(self):
        result,timeline=self.fixture();result["stats"]["ManaRegenRecovery"]=1
        timeline.update(repeat=False,events=[{"skill_ref":"main","at":0},{"skill_ref":"move","at":1},{"skill_ref":"main","at":2.9}])
        audit=resources.assess(result,{}, {"timeline":timeline})
        self.assertEqual(audit["timeline"]["status"],"supported")
        self.assertFalse(audit["timeline"]["resources"][0]["initial_pool_restored"])
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
        repair=diagnostic['repair_options']['unused']['usage']
        resources.input_shape({'timeline':timeline,'usage':[repair]})
        self.assertEqual(repair['phase'],'combat')
        self.assertEqual(resources.timeline_assessment(result,timeline,[repair])['status'],'unknown')
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
                        self.assertEqual(row["initial_pool_restored"],pool>=20 and regen==20)

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
    def test_review_from_replaces_complete_authored_review_without_container_prerequisite(self):
        for kind in ('route','plan'):
            for previous in (None,{'findings':'Prior incomplete review','unresolved':['Old outstanding work'],
                                   'conclusions':[],'objectives':[]}):
                with self.subTest(kind=kind,previous=previous):
                    self.source=self.folder/(kind+str(previous is None)+'.json')
                    prior,state_path=self.saved_proposal(kind)
                    source=poe.read_json(self.source)
                    if previous is None:source.pop('assessment')
                    else:source['assessment']={'review':previous}
                    poe.write_json(self.source,source)
                    file=Path(prior.proposal_file);saved=poe.read_json(file);state=poe.read_json(state_path)
                    packet={'token':'current-review','coverage':{'required_objective_ids':[]}}
                    if kind=='route':
                        saved['checked_route']['final_review']=packet
                        state['input_sha256']=bx.digest(self.source.read_bytes())
                    else:
                        saved['review_packet']=packet;saved['source_plan_sha256']=bx.digest(self.source.read_bytes())
                    poe.write_json(file,saved)
                    if kind=='route':state['files'][str(file)]=bx.digest(file.read_bytes())
                    else:state['candidates']['native']['evaluation_sha256']=bx.digest(file.read_bytes())
                    poe.write_json(state_path,state)
                    review={'findings':'Current authored review','unresolved':[],'conclusions':[],'objectives':[]}
                    poe.write_json(self.request,{'set':{'assessment':{'review':review}}})
                    request=args(invoked=True,kind=kind,file=str(self.source),changes=str(self.request),review_from=str(file))
                    before={k:v for k,v in source.items() if k!='assessment'}
                    result=poe.edit_document(request)
                    current=poe.read_json(self.source)
                    self.assertEqual(current['assessment']['review'],{**review,'token':'current-review'})
                    self.assertEqual({k:v for k,v in current.items() if k!='assessment'},before)
                    self.assertEqual(result['status'],'saved_draft')
                    self.assertTrue(poe.edit_document(request)['replayed'])

    def test_review_from_binds_authored_review_to_current_calculation_without_transcription(self):
        for case in ('route','plan','export'):
            with self.subTest(case=case):
                kind='plan' if case=='export' else case
                self.source=self.folder/(case+'.json');prior,state_path=self.saved_proposal(kind)
                file=Path(prior.proposal_file);saved=poe.read_json(file)
                outcome=saved['checked_route'] if kind=='route' else saved
                outcome['final_review' if kind=='route' else 'review_packet']={'token':'opaque-current-calculation',
                    'coverage':{'required_objective_ids':[]}}
                poe.write_json(file,saved);state=poe.read_json(state_path)
                if kind=='route':state['files'][str(file)]=bx.digest(file.read_bytes())
                else:state['candidates']['native']['evaluation_sha256']=bx.digest(file.read_bytes())
                poe.write_json(state_path,state)
                if case=='export':
                    file=self.folder/'run/output/native/final-assessment.json';poe.write_json(file,saved)
                    poe.write_json(file.parent/'delivery.json',{'file_exported':False,'candidate':'native',
                        'files':{'final-assessment.json':bx.digest(file.read_bytes())}})
                review={'findings':'Reviewer-authored result; real input remains blocked.','unresolved':['Actual work remains'],
                        'conclusions':[],'objectives':[]}
                poe.write_json(self.request,{'set':{'assessment':{'review':review}}})
                request=args(invoked=True,kind=kind,file=str(self.source),changes=str(self.request),review_from=str(file))
                with patch.object(engine,'calculate') as native:
                    result=poe.edit_document(request)
                    replay=poe.edit_document(request)
                native.assert_not_called()
                self.assertEqual(poe.read_json(self.source)['assessment']['review'],{**review,'token':'opaque-current-calculation'})
                self.assertEqual(result['reviewed_calculation']['file'],str(file))
                self.assertTrue(replay['replayed'])
                self.assertEqual(result['status'],'saved_draft')
                self.assertEqual(poe.read_json(file),saved)  # Never edits the result or makes it pass.

    def test_review_from_rejects_stale_calculation_and_mixed_changes_before_writes(self):
        prior,state_path=self.saved_proposal();file=Path(prior.proposal_file)
        saved=poe.read_json(file);saved['checked_route']['final_review']={'token':'current','coverage':{'required_objective_ids':[]}}
        poe.write_json(file,saved);state=poe.read_json(state_path);state['files'][str(file)]=bx.digest(file.read_bytes());poe.write_json(state_path,state)
        review={'findings':'Authored assessment','unresolved':[],'conclusions':[],'objectives':[]}
        request=args(invoked=True,kind='route',file=str(self.source),changes=str(self.request),review_from=str(file),if_sha256=prior.if_sha256)
        for change in ('mixed','receipt','source','missing_findings','shape','token_override'):
            with self.subTest(change=change):
                before=self.source.read_bytes();receipt=self.source.with_suffix('.edit.json');old_receipt=receipt.read_bytes()
                payload={'set':{'assessment':{'review':copy.deepcopy(review)}}}
                original_file=file.read_bytes()
                if change=='mixed':payload['set']['name']='Unreviewed modification'
                elif change=='receipt':poe.write_json(file,{**saved,'changed':True})
                elif change=='source':
                    value=poe.read_json(self.source);value['name']='Later source';poe.write_json(self.source,value)
                    request.if_sha256=bx.digest(self.source.read_bytes());before=self.source.read_bytes()
                elif change=='missing_findings':payload['set']['assessment']['review'].pop('findings')
                elif change=='shape':payload['set']['assessment']=[]
                else:payload['replace']={'/assessment/review/token':'wrong-context'}
                poe.write_json(self.request,payload)
                with self.assertRaises(bx.ContractError):poe.edit_document(request)
                self.assertEqual(self.source.read_bytes(),before);self.assertEqual(receipt.read_bytes(),old_receipt)
                file.write_bytes(original_file)
                if change=='source':
                    value.pop('name');poe.write_json(self.source,value);request.if_sha256=bx.digest(self.source.read_bytes())

    def saved_proposal(self,kind='route'):
        first=self.edit({'set':{'assessment':{},'stages':[{'stage_id':'one','snapshot':{'operations':{'tree':{'nodes_add':[42]}}}}]}}) if kind=='route' else self.edit({'set':{'assessment':{},'mode':'refine','operations':{'gems':[{'group':1,'index':1,'skill_id':'FutureSkill','level':20},{'group':1,'index':2,'skill_id':'OtherSkill','level':7}]}}},kind='plan')
        file=self.folder/'result.evidence.json' if kind=='route' else self.folder/'run/candidates/native/evaluation.json'
        change={'replace':{'/stages/0/snapshot/operations/tree':{'nodes':[20,42]}}} if kind=='route' else {'set':{'operations':{'gems':[{'group':1,'index':1,'level':5}]}}}
        proposal={'proposal_type':'native_tree_connection' if kind=='route' else 'native_gem_level','changes':change,'adoptable':True,'blockers':[],'preview':{'roundtrip_verified':True}}
        outcome={'status':'blocked','errors':['native unmet input'],'pending':['objective:actual'],'diagnostics':[{'code':'native_unmet','action':'edit_input','location':'/stages/0/snapshot/operations/tree' if kind=='route' else '/operations/gems','connection_proposal' if kind=='route' else 'change_proposal':proposal}]}
        actions=poe.delivery_actions(outcome,self.source,file,file.with_suffix('.review.json'),'/checked_route' if kind=='route' else '')
        if kind=='route':
            saved={'skill_version':poe.VERSION,'engine_fingerprint':'fixture','checked_route':outcome,'next_actions':actions}
            state_path=self.folder/'result.state.json'
            state={'engine_fingerprint':'fixture','input':str(self.source),'input_sha256':first['revision'],'phase':'evaluated','file_exported':False}
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
        self.assertEqual(contract['nested_fields']['operations']['tree']['fields'],sorted(bx.TREE_FIELDS))
        action=next(row for row in result['next_actions'] if row['action']=='calculate_current_input')
        self.assertEqual(action['command'],'evaluate');self.assertEqual(action['arguments'],{'plan':str(self.source)})
        self.assertEqual(action['required_arguments'],['run','name'])

    def test_start_exposes_and_saves_contract_before_the_first_intent_file(self):
        source=self.folder/'original.xml';source.write_text(SAMPLE,encoding='utf-8');run=self.folder/'run'
        with patch.object(engine,'health',return_value={'data_version':'3.29','pob_path':str(self.folder)}):
            response=poe.begin(args(invoked=True,input=str(source),run=str(run)))
        self.assertEqual(response['input_contract']['kind'],'intent')
        self.assertEqual(response['input_contract']['nested_fields']['objectives']['entry_fields'],['objective_id','text','evidence_refs'])
        self.assertEqual(response['next_actions'][0]['arguments'],{'run':str(run)})
        self.assertEqual(response['next_actions'][0]['required_arguments'],['file'])
        self.assertEqual(poe.read_json(response['inspection'])['response'],response)

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
            fixture.equipment[stage["stage_id"]]={"bases":copy.deepcopy(stage["bases"]),"checks":{"Gloves":{"base":"Wool Gloves","valid_for_slot":True}},"context":{"basis":"declared_base_layout"}}
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
        self.assertTrue(any(row.get('pointer')=='/stage_instances' for row in reply['next_actions']))
        self.assertEqual(reply['next_actions'][0]['command'],'route')
        self.assertEqual(reply['next_actions'][0]['arguments'],{'file':str(self.source)})
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
        claims=[{"claim_id":key,"text":"stage explanation","subjects":subjects,"evidence_refs":["u"]}
                for key,subjects in (("c",["stage:a"]),("combined",["skill:Future","stage:a"]),("overall",["goal","stage:a"]),("selected",["objective:direction","stage:a"]),("usage",["skill:Future"]))]
        first=self.edit({"set":{"stages":[{"stage_id":"a","name":"first","snapshot":{"usage":[{"skill_ref":"Future","claim_id":"usage"}]}},{"stage_id":"b","name":"second"}],"evidence":[{"evidence_id":"u","kind":"user_statement","subject":"goal","text":"actual user request"}],"claims":claims,"assessment":{"goal_claim_ids":["selected"]}}})
        second=self.edit({"set":{"stages":[{"stage_id":"b","name":"unrelated change"}]}},first["revision"])
        self.assertEqual(second["invalidated_claims"],["overall","selected"])
        third=self.edit({"set":{"stages":[{"stage_id":"a","name":"related change"}]}},second["revision"])
        self.assertEqual(third["invalidated_claims"],["c","combined","overall","selected","usage"])
        spec=poe.read_json(self.source)
        checked=facts.assess_claims(spec["claims"],{"u":{"status":"user_declared"}},specification=spec,engine_fingerprint="fixture")
        self.assertEqual(checked[0]["status"],"unresolved")
        diagnostics=[d for row in checked for d in row.get('diagnostics',[])]
        outcome={'status':'draft','errors':[],'pending':['claim_revision:'+row['claim_id'] for row in checked],
                 'diagnostics':diagnostics}
        action=next(row for row in poe.delivery_actions(outcome,self.source,'details.json','review.json','/checked_route')
                    if row['action']=='review_claims')
        self.assertEqual(action['edit_arguments']['if_sha256'],third['revision'])
        poe.write_json(self.request,action['changes'])
        refreshed=poe.edit_document(args(invoked=True,changes=str(self.request),**action['edit_arguments']))
        self.assertEqual(refreshed["invalidated_claims"],[])

    def test_review_submission_requires_current_claim_coverage_before_save(self):
        for kind in ('plan','route'):
            with self.subTest(kind=kind),patch.object(engine,'calculate') as native:
                self.source=self.folder/(kind+'.json')
                first=self.edit({'set':{'assessment':{},'claims':[{'claim_id':key,'text':'Scoped observation','subjects':['goal']}
                                                for key in ('first','later')]}},kind=kind)
                before={path:path.read_bytes() for path in (self.source,self.source.with_suffix('.edit.json'))}
                review={'token':'current-unverified-token','findings':'Limited review; dependencies remain.',
                        'unresolved':['Unmeasured execution'],'conclusions':[review_row('claim_id','first','Current observation','Unknown remains.')],
                        'objectives':[]}
                with self.assertRaises(bx.ContractError) as error:
                    self.edit({'set':{'assessment':{'review':review}}},first['revision'],kind=kind)
                self.assertEqual(error.exception.diagnostic['code'],'review_coverage_incomplete')
                self.assertEqual(error.exception.diagnostic['location'],'/assessment/review/conclusions')
                self.assertEqual(error.exception.diagnostic['actual']['missing_claim_ids'],['later'])
                self.assertEqual({path:path.read_bytes() for path in before},before)
                incomplete=copy.deepcopy(review)
                incomplete['conclusions']=[{'scope':'No current object selected'},review_row('claim_id','foreign','Other object','Not part of this input.')]
                with self.assertRaises(bx.ContractError) as error:
                    self.edit({'set':{'assessment':{'review':incomplete}}},first['revision'],kind=kind)
                self.assertEqual(error.exception.diagnostic['location'],'/assessment/review/conclusions/0/claim_id')
                self.assertEqual({path:path.read_bytes() for path in before},before)
                incomplete['conclusions'][0]['claim_id']='first'
                with self.assertRaises(bx.ContractError) as error:
                    self.edit({'set':{'assessment':{'review':incomplete}}},first['revision'],kind=kind)
                self.assertEqual(error.exception.diagnostic['actual']['unknown_claim_ids'],['foreign'])
                self.assertEqual(error.exception.diagnostic['actual']['missing_claim_ids'],['later'])
                self.assertEqual({path:path.read_bytes() for path in before},before)
                review['conclusions'].append(review_row('claim_id','later','Other observation','Not yet proved.',unimplemented=['Missing measurement']))
                saved=self.edit({'set':{'assessment':{'review':review}}},first['revision'],kind=kind)
                self.assertEqual(saved['status'],'saved_draft')
                self.assertEqual(poe.read_json(self.source)['assessment']['review'],review)
                native.assert_not_called()

    def test_review_submission_cannot_mix_input_changes(self):
        first=self.edit({"set":{"assessment":{"unresolved":[]}}});before=self.source.read_bytes()
        with self.assertRaises(bx.ContractError) as error:
            self.edit({"set":{"assessment":{"unresolved":["new"],"review":{"token":"old","findings":"reviewed"}}}},first["revision"])
        self.assertEqual(error.exception.diagnostic["code"],"review_transaction_mixed")
        self.assertEqual(self.source.read_bytes(),before)
        saved=self.edit({'set':{'claims':[{'claim_id':'c','text':'Current stage explanation','subjects':['stage:a']}],
                                'stages':[{'stage_id':'a','name':'before'}]}},first['revision'])
        reviewed=self.edit({'set':{'assessment':{'review':{'token':'prior','findings':'Prior review',
                            'conclusions':[review_row('claim_id','c','Prior stage','Unmeasured dependencies remain.')]}}}},saved['revision'])
        changed=self.edit({'set':{'stages':[{'stage_id':'a','name':'after'}]}},reviewed['revision'])
        rebound=self.edit({'review_claims':['c']},changed['revision'])
        self.assertEqual(rebound['invalidated_claims'],[])
        self.assertEqual(rebound['prior_review']['status'],'requires_recalculation')
        self.assertEqual(rebound['prior_review']['token'],'prior')
        self.assertTrue(any(a['action']=='calculate_then_review' for a in rebound['next_actions']))
        self.assertIn('旧整份复核',rebound['facts_summary'])
        self.assertEqual(poe.read_json(self.source)['assessment']['review']['token'],'prior')
        unchanged=self.edit({'set':{}},rebound['revision'])
        self.assertEqual(unchanged['prior_review']['status'],'not_validated')
        self.assertEqual(reviewed['prior_review']['status'],'not_validated')

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
        observation=rows[0]["observations"][0]
        self.assertEqual(published["declared_checks"][0]["observation"]["scope"],observation["meaning"]["scope"])
        comparison_ref="native:comparison#/NewMetric/after"
        comparison_claim={"claim_id":"generic-comparison","claim_type":"comparison","text":"model explanation", "subjects":["goal"],
                          "checks":[{"reference":comparison_ref,"op":"le","value":4}]}
        comparison=facts.assess_claims([comparison_claim],{}, {"comparison":{"NewMetric":{"before":2,"after":3}},"candidate":{"stats":{"NewMetric":3}}})
        self.assertEqual(comparison[0]["check_status"],"supported")
        scope=comparison[0]["observations"][0]["scope"]
        self.assertEqual(comparison[0]["checks"][0]["observation"],{"status":"native_observed","origin":"program_assessment","scope":scope})
        statement=next(s for s in poe.publication("draft",[],[],{"status":"unknown"},{},claims=comparison)["statements"] if s["kind"]=="declared_checks")
        self.assertEqual(statement["source_scopes"],[scope]);self.assertIn(scope,statement["text"])

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
        for index,malformed in enumerate(("current",None,{"claim_id":["current"]})):
            with self.subTest(partial_entry_check=malformed):
                self.source=self.folder/f"partial-route-{index}.json"
                saved=self.edit({"set":{"stages":[{"stage_id":"a","entry_checks":[malformed]}],
                                        "claims":[{"claim_id":"current","text":"Draft interpretation","subjects":["stage:a"]}]}},kind="route")
                self.assertEqual(saved["status"],"saved_draft")
                with self.assertRaises(bx.ContractError):
                    facts.entry_checks([malformed],{},[],"stage:a")


class DeliveryIntegrityTests(unittest.TestCase):
    def route_state(self, root):
        source=root/'route.json';output=root/'route.html';state=root/'route.state.json'
        poe.write_json(source,{'goal':'fixture'});poe.write_text(output,'report')
        xml=poe.stage_directory(output,'a')/'snapshot.xml';poe.write_text(xml,SAMPLE)
        value={'input':str(source),'input_sha256':bx.digest(source.read_bytes()),'output':str(output),
               'pob_path':str(root/'selected-engine'),'phase':'exported','file_exported':True,
               'engine_fingerprint':'fixture','files':{str(output):bx.digest(output.read_bytes())},
               'stages':{'a':{'status':'calculated','native_calculated':True,'xml_path':str(xml),'xml_sha256':bx.digest(xml.read_bytes())}}}
        poe.write_json(state,value)
        return source,output,state,xml

    def test_route_delivery_tracks_stage_xml_and_allows_only_its_stage_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);_,_,state,xml=self.route_state(root)
            with patch.object(engine,'resolve_root',return_value=root),patch.object(engine,'fingerprint',return_value='fixture'):
                self.assertTrue(poe.workflow_status(args(route_state=str(state)))['file_exported'])
                for content in (None,SAMPLE+' '):
                    with self.subTest(content=content):
                        if content is None: xml.unlink()
                        else: poe.write_text(xml,content)
                        result=poe.workflow_status(args(route_state=str(state)))
                        self.assertFalse(result['file_exported'])
                        self.assertEqual(result['completed_stages'],['a'])
                        self.assertIn(str(xml),result['invalid_artifacts'])
                        self.assertEqual(result['next_actions'][0]['action'],'resume_route')
                poe.write_text(xml,SAMPLE)
                value=poe.read_json(state);value['files'][str(xml)]=bx.digest(xml.read_bytes());poe.write_json(state,value)
                self.assertTrue(poe.workflow_status(args(route_state=str(state)))['file_exported'])
                outside=root/'other.xml';poe.write_text(outside,SAMPLE)
                value['stages']['a']['xml_path']=str(outside);poe.write_json(state,value)
                self.assertFalse(poe.workflow_status(args(route_state=str(state)))['file_exported'])
            if os.name=='nt':
                case=root/'junction-case';case.mkdir()
                source,output,state,xml=self.route_state(case)
                directory=xml.parent;xml.unlink();directory.rmdir()
                target=root/'outside-case';target.mkdir();poe.write_text(target/'snapshot.xml',SAMPLE)
                quote=lambda value:"'"+str(value).replace("'","''")+"'"
                junction=engine.subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
                    'New-Item -ItemType Junction -Path '+quote(directory)+' -Target '+quote(target)+' | Out-Null'],
                    capture_output=True,creationflags=engine.subprocess.CREATE_NO_WINDOW)
                self.assertEqual(junction.returncode,0,junction.stderr.decode(errors='replace'))
                with patch.object(engine,'resolve_root',return_value=case),patch.object(engine,'fingerprint',return_value='fixture'):
                    status=poe.workflow_status(args(route_state=str(state)))
                    self.assertTrue(status['input_unchanged']);self.assertFalse(status['files_intact'])
                    self.assertFalse(status['file_exported']);self.assertIn(str(xml.resolve()),status['invalid_artifacts'])
                    poe.write_json(source,{'stages':[{'stage_id':'a','snapshot':{}}]})
                    state_before=state.read_bytes();report_before=output.read_bytes();xml_before=(target/'snapshot.xml').read_bytes()
                    with self.assertRaises(bx.ContractError) as caught:
                        poe.route_command(args(invoked=True,file=str(source),out=str(output)))
                    self.assertEqual(caught.exception.diagnostic['code'],'path_conflict')
                    self.assertEqual(state.read_bytes(),state_before);self.assertEqual(output.read_bytes(),report_before)
                    self.assertEqual((target/'snapshot.xml').read_bytes(),xml_before)

    def test_route_restore_reuses_selected_engine_and_accepts_explicit_override(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);_,_,state,_=self.route_state(root)
            with patch.object(engine,'resolve_root',return_value=root) as resolve,patch.object(engine,'fingerprint',return_value='fixture'):
                poe.workflow_status(args(route_state=str(state)))
                self.assertEqual(resolve.call_args.args[0],str(root/'selected-engine'))
                request=args(route_state=str(state));request.pob=str(root/'override')
                poe.workflow_status(request)
                self.assertEqual(resolve.call_args.args[0],request.pob)

    def test_stale_candidate_keeps_source_and_recovery_diagnostic(self):
        with tempfile.TemporaryDirectory() as folder:
            run=Path(folder);xml=bx.serialise(bx.parse(SAMPLE));poe.write_text(run/'original.xml',xml)
            plan=run/'source-plan.json';poe.write_json(plan,{'operations':{}})
            candidate=run/'candidates/test';poe.write_text(candidate/'candidate.xml',xml);poe.write_text(candidate/'requested.xml',xml)
            value={'schema_version':2,'skill_version':poe.VERSION,'intent_hash':None,'original_sha256':bx.digest(xml),
                   'xml_sha256':bx.digest(xml),'requested_sha256':bx.digest(xml),'source_plan':str(plan),
                   'source_plan_sha256':bx.digest(plan.read_bytes()),'engine_fingerprint':'fixture','status':'draft','pending':[],'errors':[]}
            poe.write_json(candidate/'evaluation.json',value)
            poe.write_json(run/'run.json',{'schema_version':2,'invoked':True,'phase':'evaluated','original_sha256':bx.digest(xml),
                                          'pob_path':str(run),'candidates':{'test':{'evaluation_sha256':bx.digest((candidate/'evaluation.json').read_bytes())}}})
            poe.write_json(plan,{'operations':{'main_group':2}})
            with patch.object(engine,'calculate',side_effect=AssertionError('status must not calculate')):
                result=poe.workflow_status(args(run=str(run)))
            row=result['candidate_records'][0];action=next(a for a in result['next_actions'] if a['action']=='reevaluate')
            self.assertFalse(row['valid']);self.assertEqual(row['plan_file'],str(plan))
            self.assertEqual(row['diagnostic']['location'],'/source_plan')
            self.assertEqual(row['diagnostic']['code'],'candidate_input_stale')
            self.assertEqual(row['diagnostic']['next_actions'][0]['arguments'],{'run':str(run),'name':'test','plan':str(plan)})
            self.assertEqual(row['input_revision'],bx.digest(plan.read_bytes()))
            self.assertEqual(action['plan_file'],str(plan));self.assertEqual(action['run'],str(run))
            self.assertEqual(action['details_file'],str(candidate/'evaluation.json'))
            self.assertEqual(action['command'],'evaluate');self.assertEqual(action['arguments'],{'run':str(run),'name':'test','plan':str(plan)})

    def test_material_identity_uses_specification_across_local_xml_ids(self):
        a={'kind':'item','item_id':1,'spec_sha256':bx.digest('first'),'quantity':1,'evidence_refs':['a']}
        b={**a,'spec_sha256':bx.digest('second'),'evidence_refs':['b']}
        same={**a,'item_id':500}
        self.assertNotEqual(facts.material_ref(a),facts.material_ref(b))
        self.assertEqual(facts.material_ref(a),facts.material_ref(same))
        evidence={key:{'kind':'user_statement','status':'user_declared','subject':facts.material_ref(row),
                       'context':{'quantity':1,'spec_sha256':row['spec_sha256']}} for key,row in [('a',a),('b',b)]}
        budget=poe.budget_summary({'inventory':[a,b]}, {},[a,b],evidence)
        self.assertEqual([m['remaining'] for m in budget['materials']],[0,0])
        self.assertEqual(len({m['requirement_id'] for m in budget['materials']}),2)

    def test_disabled_route_gem_preserves_the_same_physical_group(self):
        fixture=RouteTests();fixture.setUp();fixture.route['stages'][0]['skill_groups'][0]['gems'][0]['enabled']=False
        checked,_=fixture.check()
        self.assertFalse(checked['stages'][0]['skill_groups'][0]['gems'][0]['enabled'])
        self.assertEqual(len(checked['stages'][0]['skill_groups'][0]['gems']),1)
        fixture.route['stages'][0]['skill_groups'][0]['gems'][0]['enabled']='false'
        with self.assertRaises(bx.ContractError): fixture.check()

    def test_material_aggregation_keeps_distinct_specs_and_shared_spec_quantities(self):
        fixture=RouteTests();fixture.setUp()
        first={'kind':'item','item_id':1,'spec_sha256':bx.digest('first'),'quantity':1,'instance_id':'first-item'}
        second={**first,'spec_sha256':bx.digest('second'),'instance_id':'second-item'}
        fixture.route['stages'][0]['required_items']=[first]
        fixture.route['stages'][1]['required_items']=[second]
        checked,_=fixture.check()
        requirements={}
        for stage in checked['stages']:
            for item in stage['required_items']: facts.add_material(requirements,item)
        self.assertEqual(sorted(row['quantity'] for row in requirements.values()),[1,1])
        facts.add_material(requirements,{**first,'item_id':77,'instance_id':'same-spec-other-item'})
        self.assertEqual(requirements[facts.material_ref(first)]['quantity'],2)
        self.assertEqual(requirements[facts.material_ref(second)]['quantity'],1)
        draft={'inventory':[{'kind':'item'}]};facts.model_shape(draft,complete=False)
        with self.assertRaises(bx.ContractError): facts.model_shape(draft)
        with self.assertRaises(bx.ContractError): facts.model_shape({'inventory':[first,{**first,'item_id':77}]})
        for invalid in ([],{},True,1,'unknown'):
            with self.subTest(kind=invalid):
                with self.assertRaises(bx.ContractError) as caught:
                    facts.model_shape({'inventory':[{'kind':invalid}]},complete=False)
                self.assertEqual(caught.exception.diagnostic['location'],'/inventory/0/kind')

    def test_incomplete_probe_batch_has_an_outcome_for_every_variant(self):
        with tempfile.TemporaryDirectory() as folder:
            run=Path(folder);poe.write_text(run/'original.xml',SAMPLE)
            variants=run/'variants.json';poe.write_json(variants,{'variants':[{'id':'first','operations':{}},{'id':'second','operations':{}}]})
            baseline={'active_skills':[],'engine_fingerprint':'fixture','stats':{}}
            record=(run,{'intent':{}},{'plan':{'mode':'refine'},'engine_fingerprint':'fixture','baseline':baseline},SAMPLE,run)
            for count in (0,1,2):
                singles=[0]
                def calculate(*pos,**kw):
                    if 'xml_batch' in kw: return {'calculations':[{'failure':'injected per-item failure'} for _ in range(count)]}
                    singles[0]+=1
                    if singles[0]>1: raise engine.EngineError('injected single failure')
                    return baseline
                with self.subTest(count=count),patch.object(poe,'read_candidate',return_value=record),patch.object(engine,'calculate',side_effect=calculate),patch.object(poe,'materialize_items',side_effect=lambda plan,root:(plan,[])):
                    output=run/('failed-probe-'+str(count)+'.json')
                    result=poe.probe(args(run=str(run),name='fixture',variants=str(variants),out=str(output)))
                    self.assertEqual({r['id'] for r in result['alternatives']},{'first','second'})
                    self.assertTrue(all(r['status']=='failed' for r in result['alternatives']))
                    self.assertEqual(result['status'],'failed')
                    poe.write_json(output,result)
                    # Follow actual advertised paths, without judging prose semantics.
                    for statement in result['publication']['statements']:
                        if statement['kind']!='experiment': continue
                        for pointer in set(re.findall(r'/alternatives/\d+(?:/\w+)*',statement['text'])):
                            poe.view_json(args(file=str(output),pointer=pointer,offset=0,limit=20,fields='',keys=False,query=''))

    def test_incomplete_catalog_batch_is_a_structured_failure(self):
        queries=[{'kind':'gems','query':'a'},{'kind':'gems','query':'b'}]
        with patch.object(engine,'calculate',return_value={'engine_fingerprint':'fixture','catalog_batch':[{'kind':'gems','query':'a'}]}):
            with self.assertRaises(engine.EngineError) as caught: poe.native_queries(Path('.'),queries)
        self.assertEqual(caught.exception.diagnostic['code'],'native_response_mismatch')

    def test_huge_json_number_is_located_and_route_records_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'route.json';output=root/'route.html'
            route=route_fixture();route['budget']={'amount':10**400,'currency':'divine'};poe.write_json(source,route)
            captured=io.StringIO()
            with patch.object(engine,'resolve_root',return_value=root),patch.object(engine,'fingerprint',return_value='fixture'),redirect_stdout(captured),redirect_stderr(captured):
                code=poe.main(['route','--invoked','--file',str(source),'--out',str(output)])
            self.assertNotEqual(code,0)
            failure=json.loads(captured.getvalue())
            self.assertFalse(failure['ok']);self.assertEqual(failure['diagnostic']['location'],'/budget/amount')
            self.assertEqual(poe.read_json(output.with_suffix('.state.json'))['phase'],'failed')
            self.assertIsNone(resources.number({'metric':10**400},'metric'))
            self.assertEqual(resources.number({'metric':100},'metric'),100)


class TaskCompletionTests(unittest.TestCase):
    def test_read_only_scope_conflicts_offer_current_intent_choices_without_switching(self):
        contract=poe.input_contract('intent')['nested_fields']['task_kind']
        for field in ('operations','template_xml','scenario','measurement'):
            intent={'task_kind':'read_only_review'}
            plan={'mode':'refine',field:{'requested':'value'}}
            before=copy.deepcopy((intent,plan))
            with self.subTest(field=field):
                with self.assertRaises(bx.ContractError) as caught:poe.require_read_only_plan(intent,plan)
                diagnostic=poe.failure_record(caught.exception)['diagnostic']
                self.assertEqual(diagnostic['location'],'/'+field)
                self.assertEqual(diagnostic['intent_fields']['/task_kind'],contract)
                self.assertIn('build_plan',contract['purposes'])
                self.assertEqual((intent,plan),before)
        intent={'schema_version':2,'confirmed':True,'task_kind':'read_only_review','goal':'Review existing arrangement','scenario':{'requested':'value'}}
        with self.assertRaises(bx.ContractError) as caught:poe.validate_intent(intent,bx.parse(SAMPLE))
        self.assertEqual(caught.exception.diagnostic['intent_fields']['/task_kind'],contract)

    def test_reviewed_configuration_and_goal_have_consistent_pending_and_diagnostics(self):
        claim={'claim_id':'current','subjects':['configuration:candidate','objective:requested'],
               'evidence_refs':['native:candidate#/config_values'],'missing_evidence':[],
               'check_status':'not_requested','status':'unresolved','diagnostics':[]}
        reviewed={'reference_status':'resolved','verdict':'partial','conditions':['Verify coverage before using this scene.'],'unimplemented':[]}
        review={'completion_pending':[],'conclusions':[{'claim_id':'current',**reviewed}],
                'objectives':[{'objective_id':'requested',**reviewed}],'condition_reviews':[],'facts':{},'diagnostics':[]}
        coverage=[{'objective_id':'requested','basis_status':'user_declared','claim_ids':['current']}]
        diagnostics=[{'code':'configuration_review_missing','action':'review_configuration','required_subject':'configuration:candidate','location':'/claims'},
                     {'code':'goal_claim_unresolved','action':'edit_input','subject':'goal_assessment','location':'/assessment/goal_claim_ids/0','actual':{'claim_id':'current'}}]
        pending=['config:scenario','goal_assessment']
        result=poe.plan_completion('build_plan',[],pending,diagnostics,coverage,[claim],review)
        self.assertEqual(result['status'],'scoped_result',result)
        self.assertEqual(result['pending'],[]);self.assertEqual(result['diagnostics'],[])
        self.assertEqual(pending,['config:scenario','goal_assessment'])
        for failure in ('user_only','missing_reference','refuted_check','missing_binding'):
            altered=copy.deepcopy(claim)
            if failure=='user_only': altered['evidence_refs']=['user']
            elif failure=='missing_reference': altered['missing_evidence']=['missing actual source']
            elif failure=='refuted_check': altered['check_status']='refuted'
            else: altered['missing_evidence']=['claim_revision:current']
            with self.subTest(failure=failure):
                result=poe.plan_completion('build_plan',[],pending,diagnostics,coverage,[altered],review)
                self.assertEqual(result['status'],'draft');self.assertIn('config:scenario',result['pending'])
                self.assertTrue(any(row['code']=='configuration_review_missing' for row in result['diagnostics']))

    def test_single_configuration_claim_covers_scenario_and_operation_controls(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);document,intent,native,plan=self.fixture(folder,task_kind='build_plan')
            plan['scenario']={'enemyLevel':82,'enemyIsBoss':'fixture'}
            plan['operations']={'config':{'buffFortification':False}}
            native['config_values']={**plan['scenario'],**plan['operations']['config']}
            plan['claims'][0].update(subjects=['configuration:candidate','goal','objective:requested'],
                                    evidence_refs=['native:candidate#/config_values'],uncertainties=[])
            plan['assessment']={'goal_claim_ids':['current'],'scope':'Current selected scene and controls'}
            plan['claims'][0]['bindings']=facts.claim_bindings(plan,plan['claims'][0],'fixture')
            with patch.object(engine,'calculate',return_value=copy.deepcopy(native)),patch.object(poe,'mechanical_issues',return_value=([],[],[])):
                checked=poe.assess_candidate(document,document,native,native,intent,plan,folder,root=folder)
            self.assertFalse(any(subject.startswith('config:') for subject in checked['pending']),checked['pending'])
            self.assertEqual(checked['review_packet']['facts']['candidate']['config_values'],native['config_values'])

    def test_unfinished_export_retains_repair_files_and_never_delivers_xml_or_attachments(self):
        with tempfile.TemporaryDirectory() as temp:
            run=Path(temp)
            for kind in ('read_only_review','build_plan'):
                document,intent,native,plan=self.fixture(run,task_kind=kind)
                assessed=self.assess(run,document,intent,native,plan)
                source=run/'plan.json';poe.write_json(source,plan)
                xml=bx.serialise(document);poe.write_text(run/'original.xml',xml);poe.write_text(run/'candidates/draft/requested.xml',xml)
                outcome={**assessed,'plan':plan,'source_plan':str(source),'evidence_folder':str(run),'baseline':native,'result':native,
                         'engine_fingerprint':'fixture','engine_version':'fixture','xml_sha256':native['xml_sha256'],'name':'draft'}
                state={'intent':intent,'pob_path':str(run),'intent_evidence_folder':str(run)}
                output=run/'output/draft';poe.write_text(output/'改进配装.xml',xml)
                poe.write_text(output/'配装说明.html','Previous generated report')
                with self.subTest(kind=kind),patch.object(poe,'read_candidate',return_value=(run,state,outcome,xml,run)),patch.object(poe,'assess_candidate',return_value=assessed),patch.object(engine,'calculate',return_value=copy.deepcopy(native)):
                    exported=poe.export(args(run=str(run),name='draft'))
                    self.assertFalse(exported['file_exported']);self.assertFalse(exported['xml_exported'])
                    self.assertEqual(exported['files'],[]);self.assertEqual(list(output.glob('*.xml')),[])
                    self.assertTrue((output/'final-assessment.json').is_file());self.assertTrue((output/'review.json').is_file())
                    self.assertFalse(poe.read_json(output/'delivery.json')['file_exported'])
                    self.assertEqual(poe.read_json(run/'run.json')['phase'],'evaluated')
                    self.assertFalse((output/'配装说明.html').exists())
                    self.assertEqual(set(poe.read_json(output/'delivery.json')['files']),{'final-assessment.json','review.json'})

    def test_export_cli_reports_failed_delivery_without_losing_repair_response(self):
        for complete in (False,True):
            response={'status':'scoped_result' if complete else 'draft','file_exported':complete,
                      'files':['current.html'] if complete else [],'details_file':'current-assessment.json'}
            output=io.StringIO()
            with self.subTest(complete=complete),patch.object(poe,'export',return_value=response),redirect_stdout(output):
                code=poe.main(['export','--run','fixture','--name','current'])
                saved=json.loads(output.getvalue())
                self.assertEqual(code,0 if complete else 2);self.assertIs(saved['ok'],complete)
                self.assertEqual(saved['details_file'],'current-assessment.json');self.assertEqual(saved['files'],response['files'])

    def test_formal_assessment_keeps_uncertified_ordinary_text_as_projection(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);original,intent,baseline,plan=self.fixture(folder,task_kind='build_plan')
            text='Rarity: RARE\nProjected Boots\nIron Greaves\nItem Level: 1\nSockets: R-R-R-R\n+1000 to maximum Life'
            document=bx.apply_plan(original,{'mode':'refine','summary':'Future target projection','operations':{
                'items':[{'id':2,'text':text}],'equip':[{'slot':'Boots','item_id':2}]}},native={'slots':[{'name':'Boots'}]})
            result={**copy.deepcopy(baseline),'equipped':[{'slot':'Boots','id':2,'name':'Projected Boots','type':'Boots',
                                                         'ordinary_craft_supported':True}]}
            audit={'errors':[],'warnings':[],'pending':[],'resources':[],'diagnostics':[]}
            for origin in ('items','template'):
                current=copy.deepcopy(plan)
                if origin=='items': current['operations']={'items':[{'id':2,'text':text}]}
                else: current['template_xml']='future-target.xml';current['sources']=['Local projected snapshot']
                with self.subTest(origin=origin),patch.object(engine,'calculate',return_value=baseline),patch.object(poe,'mechanical_issues',return_value=([],[],[])),patch.object(resources,'assess',return_value=copy.deepcopy(audit)):
                    observed=poe.assess_candidate(original,document,baseline,result,intent,current,folder,root=folder)
                self.assertEqual(observed['errors'],[])
                self.assertTrue(any(gap.startswith('native:item_generation:') for gap in observed['pending']),observed['pending'])
                self.assertEqual(observed['status'],'draft')
                self.assertEqual(observed['validation']['item_generation'],'unverified')
                self.assertIn('生成依据',observed['publication']['facts_summary'])

    def test_formal_generation_uses_existing_sources_owned_quantity_and_native_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);original,intent,baseline,plan=self.fixture(folder,task_kind='build_plan')
            for case in ('source','owned','recipe','unique','outside_pool','unknown_capability','insufficient_owned','wrong_owned_spec'):
                text=('Rarity: UNIQUE' if case=='unique' else 'Rarity: RARE')+'\nObserved Fixture\nIron Greaves\nSockets: R-R-R-R'
                document=bx.apply_plan(original,{'mode':'refine','summary':'Source fixture','operations':{'items':[{'id':2,'text':text}],
                    'equip':[{'slot':'Boots','item_id':2}]}},native={'slots':[{'name':'Boots'}]})
                result={**copy.deepcopy(baseline),'equipped':[{'slot':'Boots','id':2,'name':'Observed Fixture','type':'Boots',
                    'rarity':'UNIQUE' if case=='unique' else 'RARE','ordinary_craft_supported':None if case=='unknown_capability' else case!='outside_pool'}]}
                current=copy.deepcopy(plan);source=document if case=='source' else original;recipes=[]
                if case=='recipe': recipes=[{'id':2,'text':text}]
                if case in ('owned','insufficient_owned','wrong_owned_spec'):
                    spec=bx.digest(bx.normal_item(text));quantity=2 if case=='insufficient_owned' else 1
                    current['inventory']=[{'kind':'item','spec_sha256':spec,'quantity':quantity,'evidence_refs':['held']}]
                    current['evidence']=[{'evidence_id':'held','kind':'user_statement','subject':'item:'+spec,'text':'Fixture user holds one complete specification.',
                        'context':{'quantity':1,'spec_sha256':'0'*64 if case=='wrong_owned_spec' else spec}}]
                audit={'errors':[],'warnings':[],'pending':[],'resources':[],'diagnostics':[]}
                with self.subTest(case=case),patch.object(engine,'calculate',return_value=baseline),patch.object(poe,'mechanical_issues',return_value=([],[],[])),patch.object(resources,'assess',return_value=audit):
                    observed=poe.assess_candidate(source,document,baseline,result,intent,current,folder,root=folder,crafted_items=recipes)
                gaps=[gap for gap in observed['pending'] if gap.startswith('native:item_generation:')]
                self.assertEqual(bool(gaps),case in ('unknown_capability','insufficient_owned','wrong_owned_spec'))
                self.assertEqual(observed['errors'],[])

    def test_comparison_scope_only_excludes_current_unchanged_groups_with_user_bound_review(self):
        with tempfile.TemporaryDirectory() as temp:
            run=Path(temp);document,intent,native,plan=self.fixture(run,task_kind='build_plan')
            intent['delivery_scope']='snapshot_comparison'
            intent['constraint_sources']['/delivery_scope']='user'
            group=copy.deepcopy(bx.groups(document)[0]);group.set('label','Unchanged other action')
            bx.active_set(document,'Skills','SkillSet','activeSkillSet').append(group)
            native['gems']=[{'group':2,'index':1,'id':'other','enabled':True,'level':1,'quality':0}]
            native['active_skills']=[{'group':2,'skill_ref':'current-other','is_main':False}]
            plan['claims'][0]['subjects']+=['delivery_scope','goal','configuration:candidate']
            plan['claims'][0]['evidence_refs']+=['user','native:materialization#/actual_changes','native:candidate#/config_values']
            plan['claims'][0]['uncertainties']=[]
            plan['claims'][0]['bindings']=facts.claim_bindings(plan,plan['claims'][0],'fixture')
            plan['assessment']={'goal_claim_ids':['current'],'scope':'Only the requested snapshot comparison'}
            pending=['usage:current-other']
            audit={'errors':[],'warnings':[],'pending':pending,'resources':[],'diagnostics':[]}
            assess_resources=resources.assess
            use_native_resources=False
            def assess(current_plan=plan,current_native=native,current_intent=intent,errors=()):
                with patch.object(engine,'calculate',return_value=copy.deepcopy(native)),patch.object(poe,'mechanical_issues',return_value=(list(errors),[],[])),patch.object(resources,'assess',side_effect=lambda *a,**k:assess_resources(*a,**k) if use_native_resources else copy.deepcopy(audit)):
                    return poe.assess_candidate(document,document,copy.deepcopy(native),copy.deepcopy(current_native),current_intent,current_plan,run,root=run)
            first=assess();review=copy.deepcopy(first['review_packet']['review_template'])
            review['findings']='Comparison scope comes from the user; full operation schedule is outside this request.'
            for field in ('conclusions','objectives'):
                for row in review[field]:
                    row.update(scope='Snapshot comparison only',findings='Current unchanged group and requested scope checked.',verdict='partial',
                               conditions=['Use only within the measured data range.'],evidence_refs=['user','native:materialization#/actual_changes'])
            review['condition_reviews']=[{'subjects':['version:target'],'claim_ids':['current'],
                'conditions':['Verify target rules before applying.'],'fallback':'Keep the original role until verified.',
                'evidence_refs':['user','native:materialization#/actual_changes']}]
            plan['assessment']['review']=review
            current=assess()
            self.assertEqual(current['status'],'scoped_result',current['task_completion'])
            self.assertEqual(current['task_completion']['scope_exclusions'],pending)
            self.assertIn(pending[0],current['character_readiness']['pending'])
            self.assertNotIn(pending[0],[s for row in current['task_completion']['conditions'] for s in row['subjects']])
            for failure in ('default_scope','changed_group','missing_scope_claim','missing_user_basis','uncomputed_original'):
                altered=copy.deepcopy(plan);snapshot=copy.deepcopy(native);request=copy.deepcopy(intent)
                if failure=='default_scope': request['delivery_scope']='execution_plan'
                elif failure=='changed_group': snapshot['gems'][0]['level']=2
                elif failure=='missing_scope_claim': altered['claims'][0]['subjects'].remove('delivery_scope')
                elif failure=='missing_user_basis': altered['claims'][0]['evidence_refs'].remove('user')
                with self.subTest(failure=failure):
                    if failure=='uncomputed_original':
                        with patch.object(poe,'mechanical_issues',return_value=([],[],[])),patch.object(resources,'assess',return_value=copy.deepcopy(audit)):
                            result=poe.assess_candidate(document,document,native,snapshot,request,altered,run)
                    else: result=assess(altered,snapshot,request)
                    self.assertEqual(result['status'],'draft');self.assertTrue(result['task_completion']['pending'])
            self.assertEqual(assess(errors=['Known invalid current item'])['status'],'blocked')
            kept=copy.deepcopy(intent);kept['keep_skill_groups']=[1]
            changed=copy.deepcopy(document);bx.groups(changed)[0].find('Gem').set('level','19')
            with patch.object(engine,'calculate',return_value=copy.deepcopy(native)),patch.object(poe,'mechanical_issues',return_value=([],[],[])),patch.object(resources,'assess',return_value=copy.deepcopy(audit)):
                violation=poe.assess_candidate(document,changed,native,native,kept,plan,run,root=run)
            self.assertEqual(violation['task_completion']['status'],'blocked')
            self.assertTrue(any('技能组 1' in error for error in violation['task_completion']['errors']))

            # Inherited payment failures still block the character, but a current
            # user-scoped comparison may explicitly review their irrelevance.
            native['resource_rules']={'energy_shield_protects_mana':False}
            native['stats']['ManaUnreserved']=-3
            native['active_skills']=[copy.deepcopy(resource_fixture()['active_skills'][0]),
                {'group':2,'skill_ref':'current-other','actor':'player','is_main':False,'trigger':True,
                 'costs':{'ManaCost':7,'ManaPerSecondCost':7,'LifeCost':0,'LifePerSecondCost':0,
                          'ESCost':0,'ESPerSecondCost':0,'SkillTriggerRate':1}}]
            use_native_resources=True
            audit=resources.assess(native,intent,plan,[])
            original_audit=copy.deepcopy(audit)
            first=assess()
            issue=first['review_packet']['facts']['delivery_scope']['inherited_usage_errors'][0]
            self.assertIn(issue['error'],first['character_readiness']['errors'])
            self.assertEqual(first['status'],'blocked')
            reviewed=copy.deepcopy(first['review_packet']['review_template'])
            reviewed['findings']='Only the requested comparison; inherited action payment remains invalid.'
            for field in ('conclusions','objectives'):
                for row in reviewed[field]:
                    row.update(scope='Current snapshot only',findings='The compared benefit does not require this unchanged action.',
                               verdict='partial',conditions=['Use only for this comparison; do not execute the invalid action.'],
                               evidence_refs=['user','native:materialization#/actual_changes','native:candidate#/stats'])
            reviewed['condition_reviews']=[{'subjects':first['review_packet']['facts']['pending'],
                'claim_ids':['current'],'conditions':['Inherited payment failure is outside this specific comparison.'],
                'fallback':'Keep the file; do not claim that the character is executable.',
                'evidence_refs':issue['evidence_refs']+['user','native:materialization#/actual_changes']}]
            plan['assessment']['review']=reviewed
            current=assess()
            self.assertEqual(current['status'],'scoped_result',current['task_completion'])
            self.assertEqual(current['task_completion']['errors'],[])
            self.assertEqual([row['error'] for row in current['task_completion']['scope_excluded_errors']],audit['errors'])
            self.assertEqual(current['character_readiness']['errors'],audit['errors'])
            self.assertEqual(current['character_readiness']['status'],'blocked')
            self.assertEqual(audit,original_audit)
            self.assertTrue(any(row['kind']=='comparison_scope_exclusion' for row in current['publication']['statements']))
            explanation=next(row for row in current['publication']['statements'] if row['kind']=='comparison_scope_exclusion')
            self.assertEqual(explanation['condition_review']['fallback'],reviewed['condition_reviews'][0]['fallback'])
            self.assertIn(reviewed['condition_reviews'][0]['fallback'],explanation['text'])
            self.assertIn(reviewed['condition_reviews'][0]['conditions'][0],explanation['text'])
            missing=copy.deepcopy(plan)
            missing['assessment']['review']['condition_reviews'][0]['subjects'].remove(issue['subject'])
            self.assertEqual(assess(missing)['status'],'blocked')
            self.assertEqual(assess(errors=audit['errors'])['status'],'blocked')
            for changed_input in ('cost','pool','rules','group','main'):
                snapshot=copy.deepcopy(native)
                if changed_input=='cost': snapshot['active_skills'][1]['costs']['ManaCost']=8
                elif changed_input=='pool': snapshot['stats']['ManaUnreserved']=-4
                elif changed_input=='rules': snapshot['resource_rules']['energy_shield_protects_mana']=True
                elif changed_input=='group': snapshot['gems'][0]['level']=2
                else: snapshot['active_skills'][1]['is_main']=True
                with self.subTest(changed_input=changed_input):
                    checked=assess(current_native=snapshot)
                    self.assertEqual(checked['review_packet']['facts']['delivery_scope']['inherited_usage_errors'],[])
                    self.assertNotEqual(checked['status'],'scoped_result')
                    if checked['errors']: self.assertEqual(checked['status'],'blocked')
            native['stats']['ManaUnreserved']=3
            first=assess()
            issues=first['review_packet']['facts']['delivery_scope']['inherited_usage_errors']
            self.assertTrue(issues)
            reviewed['token']=first['review_packet']['token']
            reviewed['condition_reviews'][0]['subjects']=first['review_packet']['facts']['pending']
            reviewed['condition_reviews'][0]['evidence_refs']=list(dict.fromkeys(
                ref for row in issues for ref in row['evidence_refs']))+['user','native:materialization#/actual_changes']
            plan['assessment']['review']=reviewed
            checked=assess()
            self.assertEqual(checked['status'],'scoped_result',checked['task_completion'])
            self.assertEqual(checked['character_readiness']['status'],'blocked')

    def test_comparison_intent_requires_explicit_user_scope_and_keeps_default_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            document,intent,_,_=self.fixture(Path(temp),task_kind='build_plan')
            self.assertEqual(intent['delivery_scope'],'execution_plan')
            intent['delivery_scope']='snapshot_comparison'
            with self.assertRaises(ValueError): poe.validate_intent(intent,document)
            intent['constraint_sources']['/delivery_scope']='user'
            self.assertEqual(poe.validate_intent(intent,document)['delivery_scope'],'snapshot_comparison')
            for kind in ('build','read_only_review'):
                with self.subTest(kind=kind),self.assertRaises(ValueError):
                    poe.validate_intent({**intent,'task_kind':kind,'league':'Fixture'},document)

    def test_conditional_stage_requires_native_met_checks_and_all_current_assumptions(self):
        native={'stage:s':{'stats':{'Life':50},'engine_fingerprint':'fixture'}}
        reference='native:stage:s#/stats/Life'
        raw=[{'claim_id':key,'text':'Use the current measured snapshot after the declared future step.',
              'subjects':[subject,'objective:goal'],'evidence_refs':[reference],'uncertainties':['Future step not observed']}
             for key,subject in [('entry','stage:s'),('snapshot','snapshot:s')]]
        raw[1]['subjects']=['objective:goal']
        specification={'claims':raw}
        for row in raw: row['bindings']=facts.claim_bindings(specification,row,'fixture')
        claims=facts.assess_claims(raw,{},native,specification=specification,engine_fingerprint='fixture')
        checks,check_pending=facts.entry_checks([{'check_id':'measured','locator':'/stats/Life','op':'ge','value':50,'claim_id':'entry'}],native['stage:s'],claims,'stage:s')
        self.assertEqual(checks[0]['status'],'met');self.assertEqual(checks[0]['basis_status'],'unresolved')
        pending=['stage:s:'+value for value in check_pending]+['snapshot_assumptions:s','entry_condition:s:entry']
        coverage=[{'objective_id':'goal','basis_status':'user_declared','claim_ids':['entry','snapshot'],'evidence_refs':[]}]
        observations={'pending':pending,'errors':[],'objective_coverage':coverage,
                      'stages':[{'stage_id':'s','snapshot_assumptions':['snapshot'],'entry_conditions':['entry'],
                                 'calculation':{'native_calculated':True,'errors':[],'entry_checks':checks}}]}
        packet,_=facts.final_review(specification,{},claims,observations,{},native=native)
        review=copy.deepcopy(packet['review_template']);review['findings']='Conditional plan reviewed against this current snapshot.'
        for field in ('conclusions','objectives'):
            for row in review[field]:
                row.update(scope='Conditional execution',findings='Native requirement is met; the future step remains necessary.',
                           verdict='partial',conditions=['Verify the actual future step before execution.'],evidence_refs=[reference])
        review['condition_reviews']=[{'subjects':pending,'claim_ids':['entry','snapshot'],
            'conditions':['Verify the actual future step before execution.'],'fallback':'Retain the previous stage until verified.',
            'evidence_refs':[reference]}]
        packet,_=facts.final_review(specification,{},claims,observations,{'review':review},native=native)
        result=poe.plan_completion('leveling_plan',[],pending,[],coverage,claims,packet)
        self.assertEqual(result['status'],'scoped_result',result);self.assertEqual(result['pending'],[])
        self.assertEqual(observations['pending'],pending);self.assertEqual(claims[0]['status'],'unresolved')
        uncalculated=copy.deepcopy(packet)
        uncalculated['facts']['stages'][0]['calculation']['native_calculated']=False
        result=poe.plan_completion('leveling_plan',[],[],[],coverage,claims,uncalculated)
        self.assertEqual(result['status'],'draft')
        self.assertIn('native:snapshot:missing',result['pending'])
        self.assertTrue(any(row['code']=='route_snapshot_missing' for row in result['diagnostics']))
        for failure in ('unmet','unknown','uncomputed','missing_claim','missing_entry_claim','bad_reference','incomplete_condition'):
            current=copy.deepcopy(packet);current_claims=copy.deepcopy(claims)
            if failure in ('unmet','unknown'): current['facts']['stages'][0]['calculation']['entry_checks'][0]['status']=failure
            elif failure=='uncomputed': current['facts']['stages'][0]['calculation']['native_calculated']=False
            elif failure=='missing_claim': current['facts']['stages'][0]['snapshot_assumptions'].append('absent')
            elif failure=='missing_entry_claim': current['facts']['stages'][0]['entry_conditions']=['absent']
            elif failure=='bad_reference': current_claims[1]['missing_evidence']=['missing actual source']
            else: current['condition_reviews'][0]['claim_ids']=['entry']
            with self.subTest(failure=failure):
                result=poe.plan_completion('leveling_plan',[],pending,[],coverage,current_claims,current)
                self.assertEqual(result['status'],'draft');self.assertTrue(result['pending'])
        result=poe.plan_completion('leveling_plan',['Actual invalid current object'],pending,[],coverage,claims,packet)
        self.assertEqual(result['status'],'blocked');self.assertEqual(result['errors'],['Actual invalid current object'])
        # A stage ID is an opaque identifier, including delimiters. Its exact
        # known prefix must not turn a current mechanical gap into a condition.
        qualified=copy.deepcopy(packet)
        qualified['facts']['stages'][0]['stage_id']='s:branch'
        for missing in ('stage:s:branch:native:socket_capacity:group',
                        'native:item_generation:ordinary-spec',
                        'stage:s:branch:native:item_generation:ordinary-spec',
                        'entry_condition:s:branch:absent',
                        'stage:s:branch:entry_condition:s:branch:absent'):
            qualified['condition_reviews'][0]['subjects']=[missing]
            with self.subTest(current_work=missing):
                result=poe.plan_completion('leveling_plan',[],[missing],[],coverage,claims,qualified)
                self.assertEqual(result['status'],'draft');self.assertIn(missing,result['pending'])

    def test_conditional_plan_exports_current_files_but_keeps_readiness_and_rejects_current_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            run=Path(temp);document,intent,native,plan=self.fixture(run,task_kind='build_plan')
            plan['claims'][0]['subjects']+=['goal','configuration:candidate']
            plan['claims'][0]['evidence_refs'].append('native:candidate#/config_values')
            plan['claims'][0]['uncertainties']=[]
            plan['assessment']={'goal_claim_ids':['current'],'scope':'Current conditional plan only'}
            plan['claims'][0]['bindings']=facts.claim_bindings(plan,plan['claims'][0],'fixture')
            audit={'errors':[],'warnings':[],'pending':['resource:Life:leech','progress:unknown'],'resources':[],'diagnostics':[]}
            def assess(current=plan, errors=()):
                with patch.object(engine,'calculate',return_value=copy.deepcopy(native)),patch.object(poe,'mechanical_issues',return_value=(list(errors),[],[])),patch.object(resources,'assess',return_value=copy.deepcopy(audit)):
                    return poe.assess_candidate(document,document,copy.deepcopy(native),copy.deepcopy(native),intent,current,run,root=run)
            first=assess()
            unpriced={**first['budget'],'status':'unknown','within_limit':None,'limit':None,
                      'unresolved':['fixture-future-material']}
            with patch.object(poe,'budget_summary',return_value=unpriced):
                no_limit=assess()
            self.assertIn('material:fixture-future-material',no_limit['pending'])
            self.assertNotIn('budget',no_limit['pending'])
            with patch.dict(intent,{'budget':{'amount':10,'currency':'fixture'}}),patch.object(poe,'budget_summary',return_value={**unpriced,'limit':10}):
                priced=assess()
            self.assertIn('budget',priced['pending'])
            review=copy.deepcopy(first['review_packet']['review_template'])
            review['findings']='Current file and conditional plan reviewed; execution prerequisites remain unmet.'
            for field in ('conclusions','objectives'):
                for row in review[field]:
                    row.update(scope='Conditional planning only',findings='Current snapshot retained; execute after prerequisites are verified.',
                               verdict='partial',evidence_refs=['native:candidate#/stats/Life'],
                               conditions=['Verify the actual acquisition, progress and resource assumptions before execution.'],
                               unimplemented=[])
            review['condition_reviews']=[{'subjects':first['review_packet']['facts']['pending'],'claim_ids':['current'],
                'conditions':['Verify the actual acquisition, progress and resource assumptions before execution.'],
                'fallback':'Keep the original role and defer this change until the listed prerequisites are verified.',
                'evidence_refs':['native:candidate#/stats/Life']}]
            plan['assessment']['review']=review
            current=assess()
            self.assertEqual(current['status'],'scoped_result',current['task_completion'])
            self.assertEqual(current['task_completion']['pending'],[])
            self.assertEqual(current['character_readiness']['status'],'draft')
            for subject in audit['pending']:
                self.assertIn(subject,current['character_readiness']['pending'])
            self.assertEqual(current['review_packet']['objectives'][0]['verdict'],'partial')
            self.assertTrue(current['task_completion']['conditions'])
            user_only=copy.deepcopy(plan)
            user_only['assessment']['review']['objectives'][0]['evidence_refs']=['user']
            insufficient=assess(user_only)
            self.assertEqual(insufficient['status'],'draft')
            self.assertIn('plan_objective:requested',insufficient['task_completion']['pending'])
            self.assertTrue(any(row['code']=='objective_observation_missing'
                                for row in insufficient['task_completion']['diagnostics']))
            incomplete=copy.deepcopy(plan)
            incomplete['assessment']['review']['objectives'][0]['unimplemented']=['Necessary equipment specifications and acquisition steps remain undesigned.']
            unfinished=assess(incomplete)
            self.assertEqual(unfinished['status'],'draft')
            self.assertIn('plan_objective:requested',unfinished['task_completion']['pending'])
            incomplete=copy.deepcopy(plan)
            incomplete['claims'].append({**copy.deepcopy(incomplete['claims'][0]),'claim_id':'aux'})
            incomplete['assessment'].pop('review')
            first_incomplete=assess(incomplete)
            aux_review=copy.deepcopy(review);aux_review['token']=first_incomplete['review_packet']['token']
            aux_review['conclusions'].append({**copy.deepcopy(aux_review['conclusions'][0]),'claim_id':'aux',
                'unimplemented':['Necessary transition remains undesigned.']})
            incomplete['assessment']['review']=aux_review
            unfinished=assess(incomplete)
            self.assertEqual(unfinished['status'],'draft')
            self.assertIn('plan_claim:aux:unimplemented',unfinished['task_completion']['pending'])
            source=run/'plan.json';poe.write_json(source,plan)
            xml=bx.serialise(document);poe.write_text(run/'original.xml',xml);poe.write_text(run/'candidates/plan/requested.xml',xml)
            outcome={**current,'plan':plan,'source_plan':str(source),'evidence_folder':str(run),'baseline':native,'result':native,
                     'engine_fingerprint':'fixture','engine_version':'fixture','xml_sha256':native['xml_sha256'],'name':'plan'}
            state={'intent':intent,'pob_path':str(run),'intent_evidence_folder':str(run)}
            with patch.object(poe,'read_candidate',return_value=(run,state,outcome,xml,run)),patch.object(engine,'calculate',return_value=copy.deepcopy(native)),patch.object(poe,'mechanical_issues',return_value=([],[],[])),patch.object(resources,'assess',return_value=copy.deepcopy(audit)):
                exported=poe.export(args(run=str(run),name='plan'))
            self.assertEqual(exported['status'],'scoped_result');self.assertTrue(exported['file_exported']);self.assertTrue(exported['xml_exported'])
            self.assertEqual(exported['character_readiness']['status'],'draft')
            self.assertTrue(all(Path(path).is_file() for path in exported['files']))
            blocked=assess(errors=['Actual current equipment is invalid'])
            self.assertEqual(blocked['status'],'blocked')
            self.assertIn('Actual current equipment is invalid',blocked['task_completion']['errors'])
            stale=copy.deepcopy(plan);stale['claims'][0]['bindings']['engine']='old'
            invalid=assess(stale)
            self.assertEqual(invalid['status'],'draft');self.assertTrue(invalid['task_completion']['pending'])
            missing=copy.deepcopy(plan);missing['assessment']['review']['condition_reviews'][0]['evidence_refs']=['native:candidate#/stats/Absent']
            invalid=assess(missing)
            self.assertEqual(invalid['status'],'draft');self.assertTrue(invalid['task_completion']['pending'])

    def fixture(self, folder, *, task_kind='read_only_review'):
        document=bx.parse(SAMPLE)
        intent={'schema_version':2,'confirmed':True,'task_kind':task_kind,'goal':'Review this existing statement',
                'objectives':[{'objective_id':'requested','text':'Review this existing statement','evidence_refs':['user']}],
                'constraint_sources':{'/goal':'user'},'evidence':[{'evidence_id':'user','kind':'user_statement','subject':'intent','text':'Review this existing statement without changing the role.'}]}
        if task_kind=='build': intent['league']='Fixture league'
        intent=poe.validate_intent(intent,document)
        native={**resource_fixture(),'engine_fingerprint':'fixture','engine_version':'fixture','xml':bx.serialise(document),
                'xml_sha256':bx.digest(bx.serialise(document)),'tree_version':'3_29','data_version':'3.29','class':'Duelist',
                'class_id':4,'ascendancy':'Fixture','ascendancy_id':3,'level':86,'main_skill':'main','main_group':1,'gems':[],
                'nodes':[1,2],'config_keys':[],'config_values':{},'warnings':[]}
        claim={'claim_id':'current','text':'Current snapshot {{fact:native:candidate#/stats/Life}}; effects remain unknown.',
               'subjects':['objective:requested'],'evidence_refs':['native:candidate#/stats/Life'],'conditions':[],
               'uncertainties':['Effect coverage not measured']}
        plan={'schema_version':2,'mode':'refine','summary':'Review the existing role','operations':{},'claims':[claim],'assessment':{}}
        claim['bindings']=facts.claim_bindings(plan,claim,'fixture')
        return document,intent,native,plan

    def assess(self, folder, document, intent, native, plan):
        audit={'errors':['Original resource conflict'],'warnings':['Original warning'],'pending':['progress:unknown'],
               'resources':[],'diagnostics':[]}
        with patch.object(engine,'calculate',return_value=native),patch.object(poe,'mechanical_issues',return_value=(['Original invalid equipment'],[],['native:unmeasured'])),patch.object(resources,'assess',return_value=audit):
            return poe.assess_candidate(document,document,copy.deepcopy(native),copy.deepcopy(native),intent,plan,folder,root=folder)

    def complete_review(self, first, plan, verdict='unknown'):
        review=copy.deepcopy(first['review_packet']['review_template']);review['findings']='Current source reviewed; role problems remain.'
        for field in ('conclusions','objectives'):
            for row in review[field]:
                row.update(scope='Existing role snapshot only',findings='No measured effect coverage.',verdict=verdict,
                           unimplemented=['Coverage not observed'],counter_evidence_refs=['native:candidate#/stats/Life'] if verdict=='refuted' else [])
        plan['assessment']={'review':review}

    def test_read_only_review_completes_without_passing_original_role_and_build_keeps_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            for kind in ('read_only_review','build'):
                document,intent,native,plan=self.fixture(folder,task_kind=kind)
                first=self.assess(folder,document,intent,native,plan)
                self.complete_review(first,plan)
                checked=self.assess(folder,document,intent,native,plan)
                self.assertEqual(checked['character_readiness']['status'],'blocked')
                self.assertEqual(checked['errors'],['Original invalid equipment','Original resource conflict'])
                self.assertIn('progress:unknown',checked['pending']);self.assertIn('version:target',checked['pending'])
                self.assertEqual(checked['status'],'scoped_result' if kind=='read_only_review' else 'blocked')
                self.assertEqual(checked['review_packet']['objectives'][0]['verdict'],'unknown')
                if kind=='read_only_review':
                    self.assertEqual(checked['task_completion']['pending'],[])
                    actions=poe.delivery_actions(checked,folder/'plan.json',folder/'evaluation.json',folder/'review.json')
                    self.assertFalse(any(row['action'] in {'repair_input','assess_dependencies'} for row in actions))
                    self.assertTrue(any(row['action']=='export' for row in actions))
                else:
                    self.assertIn('objective:requested:review:unknown',checked['pending'])

    def test_review_missing_stale_uncovered_and_broken_reference_cannot_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);document,intent,native,plan=self.fixture(folder)
            first=self.assess(folder,document,intent,native,plan)
            self.assertEqual(first['status'],'draft');self.assertIn('final_review:missing',first['task_completion']['pending'])
            self.complete_review(first,plan)
            good=copy.deepcopy(plan)
            for failure in ('token','objective','reference','claim_binding'):
                current=copy.deepcopy(good)
                if failure=='token': current['assessment']['review']['token']='stale'
                elif failure=='objective': current['assessment']['review']['objectives']=[]
                elif failure=='reference': current['assessment']['review']['conclusions'][0]['evidence_refs']=['native:candidate#/stats/Absent']
                else: current['claims'][0]['bindings']['engine']='old'
                with self.subTest(failure=failure):
                    checked=self.assess(folder,document,intent,native,current)
                    self.assertEqual(checked['status'],'draft');self.assertTrue(checked['task_completion']['pending'])

    def test_refuted_numeric_statement_is_completed_review_but_cannot_be_affirmed(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);document,intent,native,plan=self.fixture(folder)
            plan['claims'][0]['checks']=[{'reference':'native:candidate#/stats/Life','op':'eq','value':0}]
            plan['claims'][0]['bindings']=facts.claim_bindings(plan,plan['claims'][0],'fixture')
            first=self.assess(folder,document,intent,native,plan);self.complete_review(first,plan,'refuted')
            checked=self.assess(folder,document,intent,native,plan)
            self.assertEqual(checked['status'],'scoped_result');self.assertEqual(checked['claims'][0]['check_status'],'refuted')
            display=facts.claim_display(checked['claims'][0],review=checked['review_packet'])
            self.assertEqual(display['review_verdict'],'refuted');self.assertEqual(display['status'],'reviewed')
            self.assertIn('待审判断',display['text']);self.assertIn('refuted',display['text'])
            plan['assessment']['review']['conclusions'][0].update(verdict='supported',evidence_refs=['native:candidate#/stats/Life'])
            checked=self.assess(folder,document,intent,native,plan)
            self.assertEqual(checked['status'],'draft');self.assertIn('review_claim:current:unsupported_check',checked['task_completion']['pending'])

    def test_read_only_actions_use_task_diagnostics_instead_of_original_role_repairs(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);document,intent,native,plan=self.fixture(folder)
            plan['claims'][0]['evidence_refs']=['missing-current-reference']
            checked=self.assess(folder,document,intent,native,plan)
            actions=poe.delivery_actions(checked,folder/'plan.json',folder/'evaluation.json',folder/'review.json')
            repairs=[row for row in actions if row['action']=='repair_input']
            self.assertEqual(repairs[0]['pointer'],'/task_completion/diagnostics')
            dependencies=[row for row in actions if row['action']=='assess_dependencies']
            self.assertEqual(dependencies[0]['pointer'],'/task_completion/pending')
            self.assertNotIn('Original invalid equipment',checked['task_completion']['errors'])

    def test_publication_does_not_affirm_a_review_with_unresolved_current_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);document,intent,native,plan=self.fixture(folder)
            first=self.assess(folder,document,intent,native,plan);self.complete_review(first,plan)
            conclusion=plan['assessment']['review']['conclusions'][0]
            conclusion.update(verdict='supported',findings='Claim is supported.',evidence_refs=['native:candidate#/stats/Absent'])
            checked=self.assess(folder,document,intent,native,plan)
            self.assertNotIn('final_review:stale',checked['task_completion']['pending'])
            self.assertEqual(checked['status'],'draft')
            self.assertEqual(checked['review_packet']['conclusions'][0]['reference_status'],'unresolved')
            statement=next(row for row in checked['publication']['statements'] if row['kind']=='claim_review')
            display=facts.claim_display(checked['claims'][0],review=checked['review_packet'])
            self.assertEqual(statement['status'],display['review_verdict']);self.assertEqual(statement['status'],'unknown')
            self.assertTrue(statement['revision_pending']);self.assertIn(display['text'],statement['text'])
            self.assertNotIn('Claim is supported.',checked['publication']['facts_summary'])
            self.assertNotIn('本次评审：supported',checked['publication']['facts_summary'])

    def test_read_only_intent_and_plan_cannot_change_role_or_invent_target_version(self):
        with tempfile.TemporaryDirectory() as temp:
            document,intent,native,plan=self.fixture(Path(temp))
            self.assertIsNone(intent['league']);self.assertIsNone(intent['budget'])
            self.assertEqual(bx.serialise(poe.prepared(document,intent)),bx.serialise(document))
            for field,value in (('operations',{'level':87}),('scenario',{'enemyLevel':83}),('measurement',{'flasks_enabled':False}),('template_xml','author.xml')):
                with self.subTest(field=field),self.assertRaises(bx.ContractError):poe.require_read_only_plan(intent,{**plan,field:value})
            with self.assertRaises(bx.ContractError):poe.validate_intent({**intent,'game_version':'3.29'},document)
            with self.assertRaises(bx.ContractError):poe.validate_intent({**intent,'objectives':[]},document)
            build=intent_for(document);build.pop('task_kind',None)
            self.assertEqual(poe.validate_intent(build,document)['task_kind'],'build')
            changed=copy.deepcopy(document);changed.find('Build').set('level','87')
            complete={'completion_pending':[],'conclusions':[],'diagnostics':[]}
            task=poe.read_only_completion(document,changed,{'actual_changes':[{'locator':'/level'}]},[],[],complete)
            self.assertEqual(task['status'],'blocked');self.assertTrue(task['errors'])

    def test_read_only_export_saves_report_and_render_failure_cannot_claim_success(self):
        with tempfile.TemporaryDirectory() as temp:
            run=Path(temp);document,intent,native,plan=self.fixture(run)
            first=self.assess(run,document,intent,native,plan);self.complete_review(first,plan)
            assessed=self.assess(run,document,intent,native,plan)
            source=run/'plan.json';poe.write_json(source,plan)
            poe.write_text(run/'original.xml',bx.serialise(document));poe.write_text(run/'candidates/review/requested.xml',bx.serialise(document))
            outcome={**assessed,'plan':plan,'source_plan':str(source),'evidence_folder':str(run),'baseline':native,'result':native,
                     'engine_fingerprint':'fixture','engine_version':'fixture','xml_sha256':native['xml_sha256'],'name':'review'}
            state={'intent':intent,'pob_path':str(run),'intent_evidence_folder':str(run)}
            xml=bx.serialise(document)
            with patch.object(poe,'read_candidate',return_value=(run,state,outcome,xml,run)),patch.object(engine,'calculate',return_value=copy.deepcopy(native)),patch.object(poe,'mechanical_issues',side_effect=lambda *pos,**kw:(['Original invalid equipment'],[],['native:unmeasured'])),patch.object(resources,'assess',return_value={'errors':['Original resource conflict'],'warnings':['Original warning'],'pending':['progress:unknown'],'resources':[],'diagnostics':[]}):
                exported=poe.export(args(run=str(run),name='review'))
                previous_report=(run/'output/review/配装说明.html').read_bytes()
                with patch.object(poe,'report_html',side_effect=OSError('fixture render failure')),self.assertRaises(OSError):
                    poe.export(args(run=str(run),name='review'))
                self.assertEqual(poe.read_json(run/'run.json')['phase'],'exporting')
                self.assertFalse(poe.read_json(run/'output/review/delivery.json')['file_exported'])
                self.assertEqual((run/'output/review/配装说明.html').read_bytes(),previous_report)
                exported=poe.export(args(run=str(run),name='review'))
            self.assertEqual(exported['status'],'scoped_result');self.assertTrue(exported['file_exported']);self.assertFalse(exported['xml_exported'])
            self.assertEqual(exported['pending'],[])
            self.assertEqual(exported['errors'],[])
            self.assertIn('progress:unknown',exported['character_readiness']['pending'])
            self.assertEqual({Path(path).suffix for path in exported['files']},{'.html','.json'})
            output=run/'output/review';receipt=poe.read_json(output/'delivery.json')
            self.assertTrue(receipt['file_exported']);self.assertFalse(receipt['xml_exported'])
            self.assertEqual({p.name for p in output.glob('*.xml')},set())
            page=(output/'配装说明.html').read_text(encoding='utf-8')
            self.assertIn('Original invalid equipment',page);self.assertIn('原对象的配装状态',page)
            self.assertNotIn('在 PoECharm 中打开同目录 XML',page)
            self.assertEqual((run/'original.xml').read_text(encoding='utf-8'),xml)
            self.assertEqual(exported['judgments'][0]['status'],'reviewed')
            self.assertEqual(exported['judgments'][0]['review_verdict'],'unknown')
            bounded=poe.bound_reply(exported,args(command='export',run=str(run),name='review'))
            self.assertEqual(bounded['task_completion']['status'],'scoped_result')
            self.assertEqual(bounded['character_readiness']['status'],'blocked')
            self.assertLessEqual(len(json.dumps({'ok':True,**bounded},ensure_ascii=False)),12000)
            final=poe.read_json(output/'final-assessment.json')
            final['status']='draft';final['task_completion']['status']='draft';final['task_completion']['pending']=['final_review:stale']
            poe.write_json(output/'final-assessment.json',final)
            receipt['status']='draft';receipt['files']['final-assessment.json']=bx.digest((output/'final-assessment.json').read_bytes());poe.write_json(output/'delivery.json',receipt)
            state['candidates']={'review':{}}
            with patch.object(poe,'read_run',return_value=(run,state)),patch.object(poe,'read_candidate',return_value=(run,state,outcome,xml,run)):
                status=poe.workflow_status(args(run=str(run)))
            candidate=status['candidate_records'][0]
            self.assertEqual(candidate['status'],'draft');self.assertEqual(candidate['task_completion']['pending_total'],1)
            self.assertEqual(candidate['details_file'],str(output/'final-assessment.json'))
            self.assertFalse(any(row['action']=='repair_input' for row in status['next_actions']))


class NativeSelectionIntegrityTests(unittest.TestCase):
    def test_native_candidate_probe_preserves_groups_across_saved_defaults(self):
        root=engine.resolve_root(os.environ.get('POECHARM_PATH'))
        native=engine.calculate(root,'')
        plan={'mode':'rebuild','summary':'Native transport normalization fixture',
              'operations':{'replace_skills':[{'gems':[{'skill_id':'Fireball','level':1}]},
                                            {'gems':[{'skill_id':'Fireball','level':1}]}],'main_group':1}}
        source=bx.apply_plan(bx.parse(native['xml']),plan,native=native)
        preserved=bx.groups(source)[1];preserved.set('includeInFullDPS','nil')
        preserved.find('Gem').set('enableGlobal2','nil');preserved.find('Gem').set('count','nil')
        with tempfile.TemporaryDirectory() as folder:
            work=Path(folder);original=work/'original.xml';poe.write_text(original,bx.serialise(source));original_hash=bx.digest(original.read_bytes())
            run=work/'run';poe.begin(args(invoked=True,input=str(original),run=str(run)))
            intent={'schema_version':2,'confirmed':True,'task_kind':'build_plan','goal':'Keep the second skill group while comparing this native snapshot.',
                    'keep_skill_groups':[2],
                    'objectives':[{'objective_id':'preserve','text':'Preserve the second group.','evidence_refs':['user']}],
                    'constraint_sources':{'/goal':'user','/keep_skill_groups':'user'},
                    'evidence':[{'evidence_id':'user','kind':'user_statement','subject':'intent','text':'Preserve the second skill group.'}]}
            intent_path=work/'intent.json';poe.write_json(intent_path,intent);poe.set_intent(args(run=str(run),file=str(intent_path)))
            path=work/'plan.json';poe.write_json(path,{'schema_version':2,'mode':'refine','summary':'Keep current native declarations.','operations':{}})
            poe.evaluate(args(run=str(run),name='baseline',plan=str(path)))
            variants=work/'variants.json';poe.write_json(variants,{'variants':[
                {'id':'unchanged','operations':{}},
                {'id':'group-disabled','operations':{'groups':[{'index':2,'enabled':False}]}},
                {'id':'gem-quality','operations':{'gems':[{'group':2,'index':1,'quality':1}]}}]})
            observed=poe.probe(args(run=str(run),name='baseline',variants=str(variants)))
            rows={row['id']:row for row in observed['alternatives']}
            self.assertFalse(any('技能组 2' in error for error in rows['unchanged']['errors']),rows['unchanged']['errors'])
            for name in ('group-disabled','gem-quality'):
                self.assertTrue(any('技能组 2' in error for error in rows[name]['errors']),rows[name]['errors'])
            self.assertEqual(bx.digest(original.read_bytes()),original_hash)

    def test_native_read_only_auto_level_review_repeats_and_exports(self):
        root=engine.resolve_root(os.environ.get('POECHARM_PATH'))
        native=engine.calculate(root,'')
        self.assertEqual(native['point_limits']['status'],'observed')
        self.assertTrue(native['capabilities']['native_point_limits'])
        for pool,key in (('normal','points'),('ascendancy','ascendancy_points')):
            self.assertEqual(native['point_limits']['pools'][pool]['used'],native[key])
            self.assertGreaterEqual(native['point_limits']['pools'][pool]['maximum'],0)
        stale=copy.deepcopy(native);stale['point_limits']['pools']['ascendancy']['used']+=1
        with self.assertRaises(engine.EngineError) as mismatch:engine.validate_contract(stale)
        self.assertEqual(mismatch.exception.diagnostic['location'],'/point_limits')
        seed=bx.parse(native['xml'])
        seed.find('Build').set('characterLevelAutoMode','true')
        with tempfile.TemporaryDirectory() as folder:
            work=Path(folder);original=work/'original.xml';poe.write_text(original,bx.serialise(seed))
            input_hash=bx.digest(original.read_bytes());run=work/'run'
            poe.begin(args(invoked=True,input=str(original),run=str(run)))
            canonical_hash=bx.digest((run/'original.xml').read_bytes())
            goal='Report current native Life; combat coverage is outside this snapshot.'
            intent={'schema_version':2,'confirmed':True,'task_kind':'read_only_review','goal':goal,
                    'objectives':[{'objective_id':'life','text':goal,'evidence_refs':['user']}],
                    'constraint_sources':{'/goal':'user'},
                    'evidence':[{'evidence_id':'user','kind':'user_statement','subject':'intent','text':goal}]}
            intent_path=work/'intent.json';poe.write_json(intent_path,intent)
            poe.set_intent(args(run=str(run),file=str(intent_path)))
            source=work/'plan.json';change=work/'changes.json'
            poe.write_json(change,{'set':{'schema_version':2,'mode':'refine','summary':goal,'operations':{},
                'claims':[{'claim_id':'life','text':'Current Life is {{fact:native:candidate#/stats/Life}}; combat coverage is unknown.',
                           'subjects':['goal','objective:life'],'evidence_refs':['native:candidate#/stats/Life']}]}})
            poe.edit_document(args(invoked=True,kind='plan',file=str(source),changes=str(change)))
            current=poe.evaluate(args(run=str(run),name='readonly',plan=str(source)))
            self.assertEqual(current['task_completion']['errors'],[])
            for review_number in (1,2):
                details=poe.read_json(current.get('evaluation_file') or current['details_file'])
                review=copy.deepcopy(details['review_packet']['review_template']);review.pop('token')
                review['findings']=f'Authored snapshot review {review_number}; no combat inference.'
                for field in ('objectives','conclusions'):
                    for row in review[field]:
                        row.update(verdict='supported',scope='Current Life snapshot only',
                                   findings=f'Review {review_number}: native Life is present.',
                                   evidence_refs=['native:candidate#/stats/Life'])
                poe.write_json(change,{'set':{'assessment':{'review':review}}})
                poe.edit_document(args(invoked=True,kind='plan',file=str(source),changes=str(change),
                                       review_from=current.get('evaluation_file') or current['details_file']))
                poe.evaluate(args(run=str(run),name='readonly',plan=str(source)))
                current=poe.export(args(run=str(run),name='readonly'))
                self.assertEqual(current['status'],'scoped_result',json.dumps(current['task_completion'],ensure_ascii=False));self.assertTrue(current['file_exported'])
                self.assertFalse(current['xml_exported']);self.assertEqual(current['task_completion']['pending'],[])
                self.assertEqual(current['task_completion']['errors'],[])
                self.assertTrue(all(Path(name).is_file() for name in current['files']))
                requested=bx.read_document(run/'candidates/readonly/requested.xml')
                self.assertEqual(requested.find('Build').get('characterLevelAutoMode'),'true')
                self.assertEqual(bx.digest(original.read_bytes()),input_hash)
                self.assertEqual(bx.digest((run/'original.xml').read_bytes()),canonical_hash)

    def test_saved_and_effective_gem_specs_follow_native_effects_without_enabling_disabled_gems(self):
        root=engine.resolve_root(os.environ.get('POECHARM_PATH'))
        base=engine.calculate(root,'')
        document=bx.apply_plan(bx.parse(base['xml']),{'mode':'rebuild','summary':'Native saved/effective specification regression',
            'operations':{'items':[{'id':201,'text':'Rarity: RARE\nNative Specs\nIron Gauntlets\nSockets: B-B-R\n+2 to Level of Socketed Gems\n+5% to Quality of Socketed Gems\nSocketed Gems are Supported by Level 1 Added Chaos Damage\nSocketed Gems are Supported by Level 1 Melee Physical Damage'}],
                          'equip':[{'slot':'Gloves','item_id':201}], 'main_group':1,
                          'replace_skills':[{'slot':'Gloves','gems':[{'skill_id':'Fireball','level':1,'quality':0},
                              {'skill_id':'SupportFasterCasting','level':1,'quality':0},
                              {'skill_id':'MoltenShell','level':1,'quality':0,'enabled':False}]}]}},native=base)
        result=engine.calculate(root,bx.serialise(document),cache=False,
                                breakdowns=[{'metric':'GemLevel'},{'metric':'GemQuality'}])
        active,support,disabled=result['gems'][:3]
        for gem in (active,support):
            self.assertEqual((gem['level'],gem['quality']),(1,0))
            self.assertEqual(gem['effective_specification_status'],'observed')
            self.assertGreater(gem['effective_level'],gem['level'])
            self.assertGreater(gem['effective_quality'],gem['quality'])
        self.assertTrue(support['support_applied'])
        self.assertEqual(support['effective_specification_source'],'MAIN gem.supportEffect')
        main=next(skill for skill in result['active_skills'] if skill['is_main'])
        applied={row['id']:row for row in main['supports']}
        self.assertIn('SupportAddedChaosDamage',applied)
        self.assertNotIn('SupportMeleePhysicalDamage',applied)
        extra=applied['SupportAddedChaosDamage']
        self.assertEqual((extra['level'],extra['quality']),(None,None))
        self.assertEqual((extra['effective_level'],extra['effective_quality']),(1,0))
        linked=applied[support['id']]
        self.assertEqual((linked['level'],linked['quality']),(support['level'],support['quality']))
        self.assertEqual((linked['effective_level'],linked['effective_quality']),
                         (support['effective_level'],support['effective_quality']))
        self.assertEqual((active['effective_level'],active['effective_quality']),
                         (main['effective_level'],main['effective_quality']))
        displayed=poe.publication('scoped_result',[],[],{'status':'not_requested'},{'current':result},keys=[])['facts_summary']
        self.assertIn('保存等级/品质 1/0',displayed)
        self.assertIn(f"当前有效等级/品质 {active['effective_level']}/{active['effective_quality']}",displayed)
        measured={row['metric']:row for row in result['breakdowns']}
        for metric,field in (('GemLevel','effective_level'),('GemQuality','effective_quality')):
            self.assertEqual(measured[metric]['status'],'observed')
            self.assertEqual(active[field],measured[metric]['value'])
        self.assertFalse(disabled['enabled'])
        self.assertEqual(disabled['effective_specification_status'],'not_observed')
        self.assertIsNone(disabled['effective_level']);self.assertIsNone(disabled['effective_quality'])
        self.assertEqual(disabled['effective_specifications'],[])
        saved=bx.groups(bx.parse(result['xml']))[0].findall('Gem')
        self.assertEqual([(gem.get('level'),gem.get('quality')) for gem in saved],[('1','0')]*3)
        self.assertEqual(saved[2].get('enabled'),'false')

    def test_explicit_main_selection_is_checked_against_materialized_native_choices(self):
        root=engine.resolve_root(os.environ.get('POECHARM_PATH'))
        source=bx.read_document(native_test_sample())
        native=engine.calculate(root,bx.serialise(source))
        for selection in (1,2,999):
            gems=[{'skill_id':'Fireball','level':1,'quality':0} for _ in range(2 if selection==2 else 1)]
            document=bx.apply_plan(source,{'mode':'rebuild','summary':'native selection regression',
                'operations':{'replace_skills':[{'main_active_skill':selection,'gems':gems}],'main_group':1}},native=native)
            result=engine.calculate(root,bx.serialise(document),cache=False)
            with self.subTest(selection=selection):
                if selection in (1,2):
                    self.assertTrue(poe.roundtrip(root,result,document))
                    self.assertEqual(result['skill_selections'][0]['main_active_skill'],selection)
                    self.assertEqual(result['skill_selections'][0]['choices'][selection-1]['gem_index'],selection)
                else:
                    with self.assertRaises(bx.ContractError) as caught: poe.roundtrip(root,result,document)
                    self.assertEqual(caught.exception.diagnostic['expected'],999)
                    self.assertEqual(caught.exception.diagnostic['actual'],1)
                    self.assertIn('main_active_skill',caught.exception.diagnostic['location'])
                    self.assertEqual(len(caught.exception.diagnostic['available_options']),1)
                    with tempfile.TemporaryDirectory() as folder:
                        run=Path(folder);poe.write_text(run/'original.xml',bx.serialise(source))
                        variants=run/'variants.json';poe.write_json(variants,{'variants':[{'id':'invalid-selection','operations':{'replace_skills':[{'main_active_skill':999,'gems':gems}],'main_group':1}}]})
                        record=(run,{'intent':{}},{'plan':{'mode':'rebuild'},'engine_fingerprint':native['engine_fingerprint'],'baseline':native},bx.serialise(source),root)
                        with patch.object(poe,'read_candidate',return_value=record):
                            probed=poe.probe(args(run=str(run),name='fixture',variants=str(variants)))
                        row=probed['alternatives'][0]
                        self.assertEqual(row['status'],'blocked');self.assertNotIn('stats',row)
                        self.assertEqual(row['diagnostic']['location'],'/variants/0/operations/replace_skills/0/main_active_skill')

    def test_native_batch_correspondence_and_cache_reject_transport_faults(self):
        root=engine.resolve_root(os.environ.get('POECHARM_PATH'))
        source=bx.read_document(native_test_sample())
        changed=copy.deepcopy(source);changed.find('Build').set('level',str(int(source.find('Build').get('level'))-1))
        inputs=[bx.serialise(source),bx.serialise(changed)]
        control=engine.calculate(root,xml_batch=inputs,cache=False)
        self.assertEqual([row['input_xml_sha256'] for row in control['calculations']],[bx.digest(xml) for xml in inputs])
        self.assertEqual([row['config_input_origin'] for row in control['calculations']],[engine.config_input_origin(xml) for xml in inputs])
        def process(value):
            return argparse.Namespace(returncode=0,stdout=json.dumps(value,ensure_ascii=False).encode('utf-8'),stderr=b'')
        reordered=copy.deepcopy(control);reordered['calculations'].reverse()
        with patch.object(engine.subprocess,'run',return_value=process(reordered)):
            result=engine.calculate(root,xml_batch=inputs,cache=False)
        self.assertEqual([row['level'] for row in result['calculations']],[row['level'] for row in control['calculations']])
        faulty=[]
        missing=copy.deepcopy(control);missing['calculations'].pop();faulty.append(missing)
        duplicate=copy.deepcopy(control);duplicate['calculations'][1]=copy.deepcopy(duplicate['calculations'][0]);faulty.append(duplicate)
        wrong=copy.deepcopy(control);wrong['calculations'][0]['request_id']='unknown';faulty.append(wrong)
        wrong_hash=copy.deepcopy(control);wrong_hash['calculations'][0]['input_xml_sha256']=bx.digest('different');faulty.append(wrong_hash)
        wrong_request=copy.deepcopy(control);wrong_request['request_id']='different request';faulty.append(wrong_request)
        for index,value in enumerate(faulty):
            with self.subTest(fault=index),patch.object(engine.subprocess,'run',return_value=process(value)):
                with self.assertRaises(engine.EngineError) as caught: engine.calculate(root,xml_batch=inputs,cache=False)
                self.assertEqual(caught.exception.diagnostic['code'],'native_response_mismatch')
        with tempfile.TemporaryDirectory() as folder:
            cache=Path(folder);key=control['runtime']['cache_key']
            missing_choices=copy.deepcopy(control);missing_choices['calculations'][0]['skill_selections'][0]['choices']=None
            with patch.object(engine.subprocess,'run',return_value=process(missing_choices)):
                with self.assertRaises(engine.EngineError) as caught: engine.calculate(root,xml_batch=inputs,cache=False)
                self.assertEqual(caught.exception.diagnostic['code'],'native_capability_unavailable')
            for corrupted in (duplicate,missing_choices):
                payload=json.dumps(corrupted,sort_keys=True,ensure_ascii=False,allow_nan=False)
                poe.write_json(cache/(key+'.json'),{'key':key,'sha256':bx.digest(payload),'result':corrupted})
                with patch.object(engine,'CACHE_DIRECTORY',cache),patch.object(engine.subprocess,'run',return_value=process(control)) as worker:
                    recovered=engine.calculate(root,xml_batch=inputs)
                self.assertEqual(worker.call_count,1);self.assertFalse(recovered['runtime']['cache_hit'])
                self.assertEqual(len(recovered['calculations']),2)

    def test_catalog_batches_restore_order_and_reject_wrong_query_identity(self):
        root=engine.resolve_root(os.environ.get('POECHARM_PATH'))
        queries=[{'kind':'gems','query':'Fireball','limit':1},{'kind':'rules','query':'socket','limit':1}]
        control=poe.native_queries(root,queries)
        reordered=copy.deepcopy(control);reordered['catalog_batch'].reverse()
        with patch.object(engine,'calculate',return_value=reordered):
            restored=poe.native_queries(root,queries)
        self.assertEqual([row['query'] for row in restored['catalog_batch']],[query['query'] for query in queries])
        for field in ('request_id','query'):
            wrong=copy.deepcopy(control);wrong['catalog_batch'][0][field]='different'
            with self.subTest(field=field),patch.object(engine,'calculate',return_value=wrong):
                with self.assertRaises(engine.EngineError) as caught: poe.native_queries(root,queries)
                self.assertEqual(caught.exception.diagnostic['code'],'native_response_mismatch')

    def test_route_snapshot_preserves_disabled_gem_in_same_physical_links(self):
        root=engine.resolve_root(os.environ.get('POECHARM_PATH'));native=engine.calculate(root,'')
        class_id=int(bx.active_spec(bx.parse(native['xml'])).get('classId'))
        stage={'stage_id':'disabled','name':'native disabled-gem regression','level':12,'ascendancy_allocated_nodes':[],
               'skill_groups':[{'name':'main','slot':'','gems':[{'skill_id':'Fireball','level':1,'quality':0},{'skill_id':'Fireball','level':1,'quality':0,'enabled':False}]}],
               'snapshot':{'operations':{'tree':{'nodes':[]},'main_group':1}}}
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'route.html';poe.write_json(output.with_suffix('.state.json'),{})
            record=poe.route_snapshots({'class_id':class_id,'stages':[stage]},native,root,Path(folder),output)['disabled']
            self.assertEqual(record['status'],'calculated',record)
            gems=bx.groups(bx.parse(Path(record['xml_path']).read_text(encoding='utf-8')))[0].findall('Gem')
            self.assertEqual(len(gems),2);self.assertEqual(gems[1].get('enabled'),'false')
            self.assertEqual([gem['enabled'] for gem in record['result']['gems']],[True,False])


class RealEngineTests(unittest.TestCase):
    def test_native_action_metadata_distinguishes_self_cast_fallback_from_instant(self):
        current=engine.calculate(self.pob,bx.serialise(self.original))
        instants=[s for s in current['active_skills'] if s.get('native_timing',{}).get('action_mode')=='instant']
        self.assertTrue(instants)
        self.assertTrue(all(s['base_cast_time_seconds']==0 for s in instants))
        plan={'mode':'rebuild','summary':'Native trigger source unavailable; preserve its self-cast fallback',
              'operations':{'replace_skills':[{'gems':[{'skill_id':'Fireball','level':1},
                                                     {'skill_id':'SupportCastOnCriticalStrike','level':1}]}],'main_group':1}}
        document=bx.apply_plan(self.original,plan,native=current)
        native=engine.calculate(self.pob,bx.serialise(document),cache=False)
        skill=next(s for s in native['active_skills'] if s['id']=='Fireball')
        self.assertEqual(skill['trigger'],'')
        self.assertIn('Self-Cast',skill['info_secondary'])
        self.assertEqual(skill['native_timing']['action_mode'],'unknown')
        self.assertIsNone(skill['native_timing']['action_time_seconds'])

    def test_catalog_preserves_current_gem_effect_identity_without_character_switches(self):
        current=engine.calculate(self.pob,bx.serialise(self.original))
        multi=[gem for gem in current['gems'] if len(gem.get('granted_effects',[]))>1]
        self.assertTrue(multi,'Native sample must exercise the multi-effect query contract')
        result=engine.calculate(self.pob,queries=[{'kind':'gems','query':'id:'+gem['id'],'limit':10} for gem in multi])
        for gem,query in zip(multi,result['catalog_batch']):
            entry=next(row for row in query['entries'] if row['skill_id']==gem['id'])
            self.assertEqual(entry['level_requirements_effect_id'],entry['skill_id'])
            self.assertEqual([row['id'] for row in entry['granted_effects']],[row['id'] for row in gem['granted_effects']])
            self.assertTrue(all('support' in row and 'skill_types' in row for row in entry['granted_effects']))
            self.assertTrue(all('global_enabled' not in row and 'active_skill_present' not in row for row in entry['granted_effects']))

    def test_native_preparation_projection_and_saved_view_share_current_payment_evidence(self):
        before=engine.calculate(self.pob,bx.serialise(self.original))
        plan={'mode':'rebuild','summary':'Two native standalone actions',
              'operations':{'replace_skills':[{'gems':[{'skill_id':'Fireball','level':1}]},
                                            {'gems':[{'skill_id':'Fireball','level':1}]}],'main_group':1}}
        document=bx.apply_plan(self.original,plan,native=before)
        native=engine.calculate(self.pob,bx.serialise(document),cache=False)
        skills=[row for row in native['active_skills'] if row['id']=='Fireball' and not row.get('disabled')]
        self.assertEqual(len(skills),2)
        main=next(row for row in skills if row.get('is_main'));startup=next(row for row in skills if row is not main)
        timeline={'duration_seconds':1,'repeat':False,'events':[{'skill_ref':main['skill_ref'],'at':0}],
                  'preparation':{'duration_seconds':.1,'events':[{'skill_ref':startup['skill_ref'],'at':0}]}}
        audit=resources.assess(native,{}, {'timeline':timeline,'usage':[{'skill_ref':startup['skill_ref'],'phase':'preparation'}]})
        preparation=next(row for row in audit['timeline']['preparation']['resources'] if row['resource_id']=='Mana')
        self.assertEqual(preparation['status'],'supported')
        self.assertEqual(preparation['trace'][0]['payment'],startup['costs']['ManaCost'])
        self.assertLess(preparation['final'],preparation['capacity'])
        mana=next(row for row in audit['timeline']['resources'] if row['resource_id']=='Mana')
        self.assertEqual(mana['initial'],preparation['final'])
        published=poe.publication('draft',[],[],{'status':'unknown'},{'candidate':native},readiness=audit,keys=['ManaUnreserved'])
        preparation_statement=next(row for row in published['statements'] if row['kind']=='resource' and row['phase']=='preparation' and row['subject']=='Mana')
        combat_statement=next(row for row in published['statements'] if row['kind']=='resource' and row['phase']=='combat' and row['subject']=='Mana')
        self.assertEqual(preparation_statement['window']['final'],combat_statement['window']['initial'])
        self.assertIn(preparation_statement['text'],published['facts_summary'])
        self.assertIn('value',published['measurements'][0]['text_templates'])
        self.assertEqual(mana['trace'][0]['payment'],main['costs']['ManaCost'])
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'evaluation.json'
            contract=facts.reference_contract({'resources':audit},{'resources':{'pointer':'/readiness'}})
            poe.write_json(source,{'readiness':audit,'reference_contract':contract})
            index=next(index for index,row in enumerate(audit['timeline']['resources']) if row['resource_id']=='Mana')
            page=poe.view_json(args(file=str(source),pointer='/readiness/timeline/resources',fields='resource_id,initial,status',keys=False,query='',offset=index,limit=1))
            reference=page['native_references'][f'/readiness/timeline/resources/{index}/initial']
            self.assertEqual(facts.resolve_reference(reference,{}, {'resources':audit})['value'],preparation['final'])
        self.assertEqual(bx.digest(self.sample.read_bytes()),self.original_hash)

    def test_native_explicit_skill_requests_compare_observed_source_state(self):
        current=engine.calculate(self.pob,bx.serialise(self.original))
        selected=next(gem for gem in current['gems'] if gem.get('source') and gem.get('enabled') is True)
        operation={'group':selected['group'],'index':selected['index'],'skill_id':selected['id'],'enabled':False}
        plan={'mode':'refine','summary':'Native requested state validation','operations':{'gems':[operation]}}
        document=bx.apply_plan(self.original,plan,native=current)
        observed=engine.calculate(self.pob,bx.serialise(document),cache=False)
        preservation_errors,_=poe.check_constraints(self.original,document,current,observed,
            {'allow_main_skill_change':True,'keep_skill_groups':[selected['group']]},plan)
        self.assertTrue(any('技能组 '+str(selected['group']) in error for error in preservation_errors))
        actual=next(gem for gem in observed['gems'] if gem['group']==selected['group'] and gem['index']==selected['index'])
        if actual['enabled'] is False:
            bx.verify_selections(document,observed,plan['operations'])
        else:
            with self.assertRaises(bx.ContractError) as caught:
                bx.verify_selections(document,observed,plan['operations'])
            self.assertEqual(caught.exception.diagnostic['location'],'/operations/gems/0/enabled')
            self.assertIs(caught.exception.diagnostic['expected'],False)
            self.assertIs(caught.exception.diagnostic['actual'],True)
        with tempfile.TemporaryDirectory() as folder:
            variants=Path(folder)/'variants.json';poe.write_json(variants,{'variants':[{'id':'source-state','operations':plan['operations']}]})
            request=args(input=str(self.sample),variants=str(variants));request.pob=str(self.pob)
            result=poe.probe(request)['alternatives'][0]
        if actual['enabled'] is False:
            self.assertIn('stats',result)
        else:
            self.assertEqual(result['status'],'blocked');self.assertNotIn('stats',result)
            self.assertEqual(result['diagnostic']['location'],'/variants/0/operations/gems/0/enabled')
        self.assertEqual(bx.digest(self.sample.read_bytes()),self.original_hash)

    def test_native_config_types_reject_truthy_checkbox_variants_without_changing_input(self):
        current=engine.calculate(self.pob,bx.serialise(self.original),cache=False)
        self.assertTrue(current['capabilities']['native_config_types'])
        self.assertEqual(set(current['config_keys']),set(current['config_types']))
        control=next(row for row in current['config_controls'].values() if row['type']=='check' and row['value'] is False and row.get('if_skill'))
        selected=control['id'];self.assertEqual(current['config_types'][selected],'check')
        missing=copy.deepcopy(current);missing['config_types'].pop(selected)
        with self.assertRaises(engine.EngineError):engine.validate_contract(missing)
        invalid=copy.deepcopy(self.original)
        bx.set_config(invalid,{selected:'false'})  # Imported malformed XML, not an authorized typed mutation.
        self.assertTrue(any(selected in error for error in poe.mechanical_issues(invalid,current,{})[0]))
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'角色.xml';source.write_bytes(self.sample.read_bytes());before=source.read_bytes()
            values=[False,True,'false',0,1]
            variants=Path(folder)/'variants.json';poe.write_json(variants,{'variants':[{'id':f'observation-{index}','operations':{'config':{selected:value}}} for index,value in enumerate(values)]})
            arguments=args(input=str(source),variants=str(variants));arguments.pob=str(self.pob)
            response=poe.probe(arguments);self.assertEqual(response['calculated_variants'],2)
            rows={row['id']:row for row in response['alternatives']}
            for index,value in enumerate(values[:2]):self.assertIs(rows[f'observation-{index}']['effect_state']['config_switches'][selected],value)
            for index in range(2,len(values)):
                row=rows[f'observation-{index}'];self.assertEqual(row['status'],'blocked')
                self.assertEqual(row['diagnostic']['location'],f'/variants/{index}/operations/config/{selected}')
                self.assertEqual(row['diagnostic']['expected'],'boolean')
            self.assertEqual(source.read_bytes(),before);self.assertFalse((Path(folder)/'run.json').exists())

    def test_native_input_probe_uses_current_control_values_without_xml_rewriting(self):
        current=engine.calculate(self.pob,bx.serialise(self.original))
        control=next(row for row in current['config_controls'].values() if row['type']=='check' and row['value'] is False and row.get('if_skill'))
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'角色.xml';source.write_bytes(self.sample.read_bytes());before=source.read_bytes()
            variants=Path(folder)/'variants.json';poe.write_json(variants,{'variants':[{'id':'toggle-observation','operations':{'config':{control['id']:True}}}]})
            output=Path(folder)/'comparison.json';report=Path(folder)/'comparison.html';stdout=io.StringIO()
            with redirect_stdout(stdout):
                code=poe.main(['probe','--pob',str(self.pob),'--input',str(source),'--variants',str(variants),'--metrics','LifeRegenRecovery,MaximumFortification','--out',str(output),'--report',str(report)])
            self.assertEqual(code,0,stdout.getvalue())
            result=poe.read_json(output)
            self.assertEqual(result['source_mode'],'input_xml');self.assertEqual(result['calculated_variants'],1)
            self.assertEqual(result['publication']['facts_summary'].split('\n\n',1)[0],result['publication']['delivery_brief']['message'])
            self.assertEqual(result['publication']['statements'][0]['text'],result['publication']['delivery_brief']['message'])
            self.assertIs(result['baseline_effect_state']['config_switches'][control['id']],False)
            self.assertIs(result['alternatives'][0]['effect_state']['config_switches'][control['id']],True)
            page=report.read_text(encoding='utf-8')
            self.assertEqual(result['input_sha256'],bx.digest(before))
            self.assertIn('原始文件字节 SHA256 '+bx.digest(before),page)
            self.assertIn('toggle-observation',page);self.assertIn(control['id'],page)
            self.assertIn('LifeRegenRecovery',page);self.assertIn('actual_changes',page)
            self.assertEqual(json.loads(stdout.getvalue())['report_sha256'],bx.digest(report.read_bytes()))
            self.assertEqual(source.read_bytes(),before);self.assertFalse((Path(folder)/'run.json').exists())

    def test_native_trigger_fee_pattern_uses_observed_rates_and_preserves_combat_unknowns(self):
        current=engine.calculate(self.pob,bx.serialise(self.original))
        effects=[{'skill_id':effect['id'],'enabled':False} for gem in current['gems'] for effect in gem['granted_effects']
                 if effect['has_global_effect'] and ('Guard' in effect['skill_types'] or len(gem['granted_effects'])>1 and all(row['has_global_effect'] for row in gem['granted_effects']))]
        _,native=poe.measured_calculation(self.pob,self.original,{'flasks_enabled':False,'effects':effects})
        skill=next(row for row in native['active_skills'] if row.get('trigger') and row['usage_rate']['metric']=='SkillTriggerRate'
                   and row['usage_rate']['value']>0 and any((row['costs'].get(key) or 0)>0 for key in ('ManaCost','LifeCost','ESCost')))
        selected=copy.deepcopy(native);selected['active_skills']=[skill]
        interval=1/skill['usage_rate']['value']
        timeline={'duration_seconds':3*interval,'repeat':False,'events':[{'skill_ref':skill['skill_ref'],'at':i*interval} for i in range(3)]}
        audit=resources.assess(selected,{}, {'timeline':timeline})
        self.assertEqual(audit['timeline']['automatic_schedules'][skill['skill_ref']]['status'],'supported')
        for row,key in zip(audit['timeline']['resources'],('Mana','Life','ES')):
            self.assertIn(row['status'],('supported','refuted'))
            cost=resources.payment(skill['costs'],key,native['resource_rules']['energy_shield_protects_mana'],skill.get('no_cost') is True)
            self.assertEqual([payment['payment'] for payment in row['trace']],[cost]*3)
        self.assertEqual(audit['execution_coverage']['trigger_realization'],'unmeasured')
        self.assertEqual(audit['execution_coverage']['status'],'unknown')
        self.assertEqual(selected['stats'],native['stats'])

    def test_native_configuration_controls_expose_unchecked_values_and_related_options(self):
        current=engine.calculate(self.pob,bx.serialise(self.original),cache=False)
        self.assertTrue(current['capabilities']['native_config_controls'])
        controls=current['config_controls'];self.assertTrue(controls)
        self.assertIsInstance(current['config_placeholders'],dict)
        loader=current['config_loader_source']
        loader_receipt=engine.read_source(self.pob,loader['source'],loader['line'],loader['count'])
        self.assertTrue(loader_receipt['lines'])
        self.assertEqual(loader['count'],min(loader['total_lines'],200))
        for key,row in controls.items():
            self.assertEqual(row['runtime_placeholder_present'],key in current['config_placeholders'])
            self.assertEqual(row['runtime_placeholder_value'],current['config_placeholders'].get(key))
        for item in current['equipped']:
            if not item.get('flask_local'):continue
            self.assertIsInstance(item['flask_base'],dict)
            local_source=item['flask_local_source']
            self.assertTrue(engine.read_source(self.pob,local_source['source'],local_source['line'],local_source['count'])['lines'])
        self.assertEqual(current['config_input_origin'],engine.config_input_origin(bx.serialise(self.original)))
        effects={(gem['group'],gem['index'],effect['id']):effect for gem in current['gems'] for effect in gem['granted_effects']}
        for skill in current['active_skills']:
            with self.subTest(skill_ref=skill['skill_ref']):
                effect=effects[skill['group'],skill['index'],skill['id']]
                self.assertIn('description',skill)
                self.assertEqual(skill['description'],effect.get('description'))
                self.assertEqual(skill['skill_types'],effect['skill_types'])
                self.assertIn('base_cast_time_seconds',skill)
                self.assertEqual(skill['base_cast_time_seconds'],effect['base_cast_time_seconds'])
                direct={key for key,row in controls.items() if row.get('if_skill')==skill['name'] or isinstance(row.get('if_skill'),list) and skill['name'] in row['if_skill']}
                linked=set(skill['config_control_ids'])
                self.assertEqual(skill['config_control_ids'],sorted(linked))
                self.assertTrue(direct.issubset(linked))
                self.assertTrue(linked.issubset(controls))
                if not direct: self.assertFalse(linked)
                for key,row in controls.items():
                    parents=row.get('if_option',[])
                    parents=parents if isinstance(parents,list) else [parents]
                    for parent in parents:
                        if parent in controls and (key in linked or parent in linked):
                            self.assertTrue({key,parent}.issubset(linked))
        unchecked=[row for row in controls.values() if row['type']=='check' and row['value'] is False and row.get('if_skill')]
        self.assertTrue(unchecked)
        selected=unchecked[0]
        source=selected['apply_source']
        receipt=engine.read_source(self.pob,source['source'],source['line'],source['count'])
        self.assertGreater(source['total_lines'],0)
        self.assertTrue(receipt['lines'])
        self.assertEqual(source['count'],min(source['total_lines'],200))
        query=engine.calculate(self.pob,bx.serialise(self.original),catalog='config',query=selected['id'],limit=30,cache=False)
        option=next(row for row in query['catalog'] if row['var']==selected['id'])
        self.assertIs(option['current_control']['value'],False)
        self.assertEqual(option['current_control']['input_origin'],selected['input_origin'])
        self.assertEqual(option['current_control']['apply_source'],source)
        skill=next(row for row in current['active_skills'] if row['base_cast_time_seconds'] is not None)
        catalogue=next(row for row in engine.calculate(self.pob,'',catalog='skills',query=skill['id'],limit=100,cache=False)['catalog'] if row['id']==skill['id'])
        for field in ('description','skill_types','base_cast_time_seconds'):
            self.assertEqual(catalogue[field],skill[field])
        saved=engine.calculate(self.pob,current['xml'],cache=False)
        self.assertIs(saved['config_controls'][selected['id']]['value'],False)
        changed=bx.apply_plan(self.original,{'mode':'refine','summary':'Native configuration control test','operations':{'config':{selected['id']:True}}},native=current)
        enabled=engine.calculate(self.pob,bx.serialise(changed),cache=False)
        self.assertIs(enabled['config_controls'][selected['id']]['value'],True)
        self.assertIs(enabled['config_controls'][selected['id']]['input_origin']['input_present'],True)
        self.assertIs(enabled['config_controls'][selected['id']]['input_origin']['input_value'],True)
        related=[row for row in controls.values() if row.get('if_option')]
        self.assertTrue(related)
        for row in related:
            parents=row['if_option'] if isinstance(row['if_option'],list) else [row['if_option']]
            self.assertTrue(any(parent in controls for parent in parents))
        unknown=[row for row in controls.values() if row['value_basis']=='unknown']
        self.assertTrue(unknown);self.assertTrue(all(row['value'] is None for row in unknown))
        enumerated=[row for row in controls.values() if row['type']=='list']
        self.assertTrue(enumerated)
        for row in enumerated:
            self.assertEqual([option['val'] for option in row['options']],current['config_lists'][row['id']])
            self.assertTrue(all('label' in option for option in row['options']))
        self.assertIsNone(next(row for row in enumerated if row['value_basis']=='unknown')['value'])

    def test_native_named_effects_common_measurement_and_jewel_activity(self):
        current=engine.calculate(self.pob,bx.serialise(self.original))
        multiple=next(gem for gem in current['gems'] if len(gem['granted_effects'])>1 and all(effect['has_global_effect'] for effect in gem['granted_effects']))
        controls={'flasks_enabled':False,'effects':[{'skill_id':effect['id'],'enabled':False} for effect in multiple['granted_effects']]}
        document,result=poe.measured_calculation(self.pob,self.original,controls)
        actual=next(gem for gem in result['gems'] if gem['group']==multiple['group'] and gem['index']==multiple['index'])
        self.assertEqual([(effect['index'],effect['id']) for effect in actual['granted_effects']],[(effect['index'],effect['id']) for effect in multiple['granted_effects']])
        self.assertTrue(all(not effect['global_enabled'] and not effect['active_skill_present'] for effect in actual['granted_effects']))
        self.assertTrue(all(not item['active'] and not item['effective_in_calculation'] for item in result['equipped'] if item['type']=='Flask'))
        self.assertEqual(bx.item_texts(document),bx.item_texts(self.original))
        saved=poe.roundtrip(self.pob,result,document)
        self.assertEqual(result['character_identity'],engine.calculate(self.pob,saved,cache=False)['character_identity'])
        empty=bx.apply_plan(self.original,{'mode':'refine','summary':'Native inclusion test, not a build proposal','operations':{'tree':{'nodes':[]}}},native=current)
        unallocated=engine.calculate(self.pob,bx.serialise(empty))
        jewels=[item for item in unallocated['equipped'] if item.get('socket_node_id')]
        self.assertTrue(jewels)
        self.assertTrue(all(item['assigned'] and item['socket_allocated'] is False and item['effective_in_calculation'] is False for item in jewels))

    def test_native_role_reference_survives_transport_change_without_ignoring_byte_claims(self):
        document=poe.prepared(self.original,self.intent,baseline=True)
        original=engine.calculate(self.pob,bx.serialise(document))
        xml=poe.roundtrip(self.pob,original,document);original['xml_sha256']=bx.digest(xml)
        saved=engine.calculate(self.pob,xml,cache=False);saved['xml_sha256']=bx.digest(xml)
        self.assertEqual(original['character_identity'],saved['character_identity'])
        self.assertNotEqual(original['input_xml_sha256'],saved['input_xml_sha256'])
        self.assertEqual(original['config_input_origin'],saved['config_input_origin'])
        self.assertEqual(original['config_input_origin']['input_xml_sha256'],bx.digest(xml))
        self.assertEqual(original['config_controls'],saved['config_controls'])
        contract=facts.reference_contract({'candidate':original})['candidate']['identity_references']
        self.assertEqual(set(contract),{'character','input_bytes','saved_bytes'})
        for role,expected_stale in (('character',False),('input_bytes',True)):
            claim={'claim_id':'identity','text':'Observed identity for this scope','subjects':['goal'],'evidence_refs':[contract[role]['reference']]}
            specification={'claims':[claim]};evidence={}
            first=facts.assess_claims([claim],evidence,{'candidate':original},specification=specification,engine_fingerprint=original['engine_fingerprint'])
            packet,_=facts.final_review(specification,evidence,first,{'role':original['character_identity']},{},native={'candidate':original})
            review={'review':{'token':packet['token'],'findings':'Native identity scope only','unresolved':[],
                             'conclusions':[review_row('claim_id','identity','Observed identity only','Transport and role are distinguished')]}}
            current=facts.assess_claims([claim],evidence,{'candidate':saved},specification=specification,engine_fingerprint=saved['engine_fingerprint'])
            _,pending=facts.final_review(specification,evidence,current,{'role':saved['character_identity']},review,native={'candidate':saved})
            self.assertEqual('final_review:stale' in pending,expected_stale)

    def test_native_common_measurement_baseline_cache_and_export_review(self):
        current=engine.calculate(self.pob,bx.serialise(self.original))
        multiple=next(gem for gem in current['gems'] if len(gem['granted_effects'])>1 and all(effect['has_global_effect'] for effect in gem['granted_effects']))
        plan=copy.deepcopy(self.refine)
        plan['measurement']={'flasks_enabled':False,'effects':[{'skill_id':effect['id'],'enabled':False} for effect in multiple['granted_effects']]}
        plan['claims']=[{'claim_id':'measurement','text':'Same declared native effect controls; no gameplay coverage guarantee',
                        'subjects':['measurement','configuration:candidate','goal'],
                        'evidence_refs':['native:baseline#/measurement_audit','native:candidate#/measurement_audit','native:candidate#/character_identity/digest']}]
        plan['assessment']={'goal_claim_ids':['measurement'],'scope':'Native measurement interface fixture'}
        first=self.evaluate_plan(plan,'common-measurement')
        first_details=poe.read_json(self.run_dir/'candidates/common-measurement/evaluation.json')
        for name in ('baseline','result'):
            self.assertTrue(all(not item['active'] for item in first_details[name]['equipped'] if item['type']=='Flask'))
        template=poe.read_json(self.run_dir/'candidates/common-measurement/review.json')['review_template']
        plan['assessment']['review']={**template,'findings':'Only the native interface scope is reviewed',
            'objectives':[{**row,'verdict':'unknown','scope':'Fixture scope only','findings':'Unresolved gameplay dependencies retained'} for row in template['objectives']],
            'conclusions':[{**row,'verdict':'unknown','scope':'Native control mapping only','findings':'Two roles use the chosen controls; no temporal guarantee'} for row in template['conclusions']]}
        # The author writes findings; the editor binds current native identity.
        authored=copy.deepcopy(plan['assessment']['review']);authored.pop('token')
        change=Path(self.temp.name)/'common-measurement-review-change.json'
        poe.write_json(change,{'set':{'assessment':{'review':authored}}})
        stdout=io.StringIO()
        with redirect_stdout(stdout):
            code=poe.main(['edit','--invoked','--kind','plan','--file',str(Path(self.temp.name)/'common-measurement.json'),
                          '--changes',str(change),'--review-from',first['evaluation_file']])
        self.assertEqual(code,0,stdout.getvalue())
        plan=poe.read_json(Path(self.temp.name)/'common-measurement.json')
        self.assertEqual(plan['assessment']['review']['token'],first['review_token'])
        self.assertEqual(plan['assessment']['review']['findings'],authored['findings'])
        second=self.evaluate_plan(plan,'common-measurement')
        self.assertEqual(first['review_token'],second['review_token'])
        exported=poe.export(args(run=str(self.run_dir),name='common-measurement'))
        self.assertNotIn('final_review:stale',exported['pending'])
        actual=poe.read_json(self.run_dir/'output/common-measurement/final-assessment.json')
        reviewed=poe.edit_document(args(invoked=True,kind='plan',file=str(Path(self.temp.name)/'common-measurement.json'),
            changes=str(change),review_from=exported['details_file']))
        self.assertEqual(reviewed['reviewed_calculation']['file'],exported['details_file'])
        self.assertEqual(poe.read_json(Path(self.temp.name)/'common-measurement.json')['assessment']['review']['token'],actual['review_packet']['token'])
        self.assertEqual(actual['configuration_audit']['measurement_actual'],first_details['configuration_audit']['measurement_actual'])
        self.assertEqual(engine.character_state(actual['original']),engine.character_state(current))
        changes=actual['materialization']['actual_changes']
        self.assertTrue(any(row['locator'].endswith('/global_enabled') and row['before'] is True and row['after'] is False for row in changes))
        statements=[row for row in actual['publication']['statements'] if row['kind']=='materialization_change']
        self.assertEqual([row['values'] for row in statements],changes)
        global_control=next(row for row in statements if row['values']['locator'].endswith('/enable_global1'))
        self.assertIn('id',global_control['values']['object_after'])
        self.assertIn('true → false',global_control['text'])
        effect_fields={effect['operation_field'] for effect in multiple['granted_effects']}
        effect_control=next(row for row in statements if row['values']['locator'].rsplit('/',1)[-1] in effect_fields and
                            (row['values']['object_after']['group'],row['values']['object_after']['index'])==(multiple['group'],multiple['index']))
        observed=effect_control['values']['object_after']['granted_effects']
        self.assertEqual([effect['id'] for effect in observed],[effect['id'] for effect in multiple['granted_effects']])
        for effect in observed:
            self.assertIn(effect['id'],effect_control['text'])
            self.assertIn(effect['operation_field'],effect_control['text'])
        self.assertIn('原角色',actual['publication']['facts_summary'])
        report=poe.report_html(poe.read_json(self.run_dir/'run.json'),actual)
        for effect in observed: self.assertIn(effect['id'],report)
        self.assertIn('原角色到交付',report)
        self.assertIn('global_enabled',report)
        plan['measurement']['flasks_enabled']=True;plan['assessment'].pop('review')
        changed=self.evaluate_plan(plan,'common-measurement')
        details=poe.read_json(self.run_dir/'candidates/common-measurement/evaluation.json')
        self.assertNotEqual(first_details['baseline']['context_hash'],details['baseline']['context_hash'])
        self.assertTrue(any(item['active'] for item in details['baseline']['equipped'] if item['type']=='Flask'))

    @classmethod
    def setUpClass(cls):
        # Offline engine tests use an explicit source-boundary stub, not live web.
        cls.fact_patch = patch.object(facts, "version_fact", return_value={"status": "source_observed", "scope": "offline source-boundary fixture"})
        cls.fact_patch.start()
        cls.addClassCleanup(cls.fact_patch.stop)
        try:
            cls.pob = engine.resolve_root(os.environ.get("POECHARM_PATH"))
            cls.sample = native_test_sample()
        except ValueError as exc:
            raise unittest.SkipTest(str(exc))
        cls.original_hash = bx.digest(cls.sample.read_bytes())
        cls.settings_hash = bx.digest((cls.pob / "Settings.xml").read_bytes())
        cls.cache_hash = bx.digest((cls.pob / "Data/ModCache.lua").read_bytes())
        cls.temp = tempfile.TemporaryDirectory(prefix="poe-offline-")
        cls.run_dir = Path(cls.temp.name) / "中文 配装任务"
        poe.begin(args(invoked=True, input=str(cls.sample), run=str(cls.run_dir)))
        cls.original = bx.read_document(cls.sample)
        cls.native = engine.calculate(cls.pob, catalog="bases", query="id:Wool Gloves", limit=1)
        current=engine.calculate(cls.pob,bx.serialise(cls.original))
        point_budget=resources.assess(current,{}, {})['point_budgets'][0]
        point_limit=min(current['points']+10,point_budget.get('native_maximum',current['points']+10))
        cls.intent = intent_for(cls.original,game_version=engine.require_data_version(engine.metadata(cls.pob)),normal_points=point_limit,ascendancy_points=current['ascendancy_points']+2)
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
        self.assertFalse(operation["file_exported"]);self.assertNotIn("reason",operation)
        self.assertEqual(output["files"],[])
        final=poe.read_json(self.run_dir/"output/refine/final-assessment.json")
        self.assertTrue(final["comparison_notes"])
        self.assertIsNone(final["comparison"]["CombinedDPS"]["percent"])
        self.assertEqual(output["publication"]["statements"][1]["status"],"unknown")
        evidence = poe.read_json(self.run_dir / "candidates/refine/evaluation.json")
        self.assertEqual(set(evidence["reference_contract"]),{"original","materialization","baseline","candidate","budget","resources","comparison"})
        self.assertEqual(evidence['reference_contract']['comparison']['artifact']['pointer'],'/comparison')
        reference='native:comparison#/CombinedDPS/delta'
        observed=facts.resolve_reference(reference,{}, {'comparison':evidence['comparison'],'candidate':evidence['result'],'baseline':evidence['baseline']})
        self.assertEqual(observed['value'],evidence['comparison']['CombinedDPS']['delta'])
        self.assertEqual(observed['context']['engine_fingerprint'],evidence['engine_fingerprint'])
        self.assertEqual(evidence['reference_contract']['original']['artifact']['pointer'],'/original')
        self.assertEqual(evidence['reference_contract']['materialization']['artifact']['pointer'],'/materialization')
        self.assertEqual(evidence["reference_contract"]["resources"]["fields"]["single_payments"],"native:resources#/single_payments")
        self.assertEqual(evidence["reference_contract"]["resources"]["origin"],"program_assessment")
        scene=next(d for d in evidence["diagnostics"] if d["code"]=="configuration_review_missing")
        self.assertEqual(scene["actual"],evidence["result"]["config_values"])
        self.assertEqual(scene["required_subject"],"configuration:candidate")
        self.assertTrue(any(a["action"]=="view" and a["pointer"]=="/reference_contract" for a in result["next_actions"]))
        granted = [gem for gem in evidence["result"]["gems"] if gem.get("source")]
        self.assertTrue(granted)
        self.assertTrue(all(not gem["group_enabled"] for gem in granted), granted)
        saved = bx.read_document(self.run_dir/"candidates/refine/candidate.xml")
        self.assertTrue(all(group.get("enabled") == "false" and group.get("slot")
                            for group in bx.groups(saved) if group.get("source")))
        drifted = copy.deepcopy(evidence["result"])
        next(gem for gem in drifted["gems"] if gem.get("source"))["group_enabled"] = True
        errors, _, _ = poe.mechanical_issues(saved, drifted, poe.read_json(self.run_dir / "run.json")["intent"])
        self.assertTrue(any("授予技能状态" in error for error in errors), errors)
        report = poe.report_html(poe.read_json(self.run_dir/'run.json'),evidence)
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
        with self.assertRaises(bx.ContractError) as caught:
            self.evaluate_plan(plan, "bad-gem")
        self.assertEqual(caught.exception.diagnostic['action'],'edit_input')
        self.assertEqual(caught.exception.diagnostic['location'],'/operations/gems/0/skill_id')
        self.assertFalse((self.run_dir/'candidates/bad-gem').exists())
        self.assertFalse((self.run_dir/'output/bad-gem').exists())

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
        with self.assertRaises(bx.ContractError) as caught:
            self.evaluate_plan(plan, "invalid-config-label")
        self.assertEqual(caught.exception.diagnostic['location'],'/operations/config/enemyIsBoss')
        self.assertIn('Boss',caught.exception.diagnostic['available_options'])
        self.assertFalse((self.run_dir/'candidates/invalid-config-label/evaluation.json').exists())
        plan["operations"]["config"]["enemyIsBoss"] = "Boss"
        outcome = self.evaluate_plan(plan, "valid-config-value")
        self.assertEqual(outcome["status"], "draft", outcome["errors"])
        recorded = poe.read_json(self.run_dir / "candidates/valid-config-value/evaluation.json")
        self.assertEqual(recorded["result"]["config_values"]["enemyLevel"], self.intent["scenario"]["enemyLevel"])
        self.assertEqual(recorded["result"]["config_values"]["enemyIsBoss"], "Boss")
        self.assertEqual(recorded["result"]["config_labels"]["enemyIsBoss"], "Standard Boss")
        output = poe.export(args(run=str(self.run_dir), name="valid-config-value"))
        self.assertIn("敌人类型：Standard Boss（Boss）；敌人等级参数：83", output["facts_summary"])
        self.assertEqual(output['files'],[])
        report = poe.report_html(poe.read_json(self.run_dir/'run.json'),poe.read_json(output['details_file']))
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
        self.assertEqual(gems["SupportMinionDamage"]["level_requirements"][0]["base_resource_units"]["manaMultiplier"], "native_MORE_percent")
        for gem in gems.values():
            for row in gem["level_requirements"]:
                self.assertEqual(set(row.get("base_resource_units", {})), set(row.get("base_resources", {})))
                for resource in row.get("base_resources", {}).get("cost", {}):
                    definition = row["base_resource_units"]["cost"][resource]
                    self.assertEqual(definition["Resource"], resource)
                    self.assertGreater(definition["Divisor"], 0)
                    self.assertIn("{0}", definition["ResourceString"])
        route = route_fixture(self.native['data_version'])
        checked, _ = self.validate_route(route, gems)
        self.assertEqual(checked["stages"][0]["skill_groups"][0]["gems"][0]["name"], gems["ShieldCrush"]["display_name"])
        self.assertEqual(checked["stages"][0]["skill_groups"][0]["gems"][0]["base_resource_units"]["cost"]["Mana"]["Resource"], "Mana")
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
        self.assertTrue(all(type(s['native_timing']['cooldown_paused_during_effect']) is bool for s in result['active_skills']))
        self.assertTrue(any(s['native_timing']['cooldown_paused_during_effect'] is False for s in result['active_skills']))
        self.assertTrue(skill['native_timing']['cooldown_paused_during_effect'])
        self.assertGreater(skill['native_timing']['duration_seconds'],0)
        self.assertAlmostEqual(resources.native_rate(skill),1/cooldown/skill["costs"].get("Repeats",1))
        focused={**result,"active_skills":[skill]}
        timeline={"duration_seconds":cooldown*1.5,"repeat":False,"events":[{"skill_ref":skill["skill_ref"],"at":0},{"skill_ref":skill["skill_ref"],"at":cooldown}]}
        audit=resources.timeline_assessment(focused,timeline)
        self.assertFalse(audit["errors"]);self.assertFalse(audit["pending"])
        condition=next(row for row in audit['timing_conditions'] if row['skill_ref']==skill['skill_ref'])
        self.assertAlmostEqual(condition['full_duration_recast_seconds'],cooldown+skill['native_timing']['duration_seconds'])
        self.assertEqual(condition['status'],'unknown')
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
            {"kind":"bases","query":"id:Stone Axe","limit":1},{"kind":"bases","query":"id:Fishing Rod","limit":1},{"kind":"classes","query":"Templar","limit":1},
            {"kind":"links","query":"ShieldCrush","limit":1},{"kind":"links","query":"SupportGreaterMultipleProjectiles","limit":1}])
        multi,unused,unknown,axe,rod,classes,active_only,support_only=[q["entries"][0] for q in result["catalog_batch"]]
        self.assertTrue(multi["valid"]);self.assertEqual([r["active_index"] for r in multi["relations"] if r["applied"]],[1,2])
        self.assertTrue(unused["valid"]);self.assertEqual(unused["incompatible"],["SupportGreaterMultipleProjectiles"])
        self.assertFalse(unknown["valid"]);self.assertTrue(unknown["errors"])
        self.assertEqual(multi["checked_pairs"], len(multi["relations"]))
        self.assertEqual(multi["status"], "observed")
        self.assertEqual(multi["provided_ids"], ["Fireball", "FreezingPulse", "SupportAddedLightningDamage"])
        self.assertEqual(unknown["status"], "invalid_inputs")
        for row in (active_only, support_only):
            self.assertTrue(row["valid"])
            self.assertEqual(row["status"], "not_tested")
            self.assertEqual(row["checked_pairs"], 0)
            self.assertEqual(row["relations"], [])
            self.assertEqual(row["incompatible"], [])
            self.assertTrue(row["next"])
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
        # Replacing the granting equipment removes its old source-skill object.
        plan['operations']['groups']=[change for change in plan['operations']['groups']
                                      if not bx.groups(self.original)[change['index']-1].get('source')]
        outcome = self.evaluate_plan(plan, "crafted-boots")
        self.assertEqual(outcome["status"], "draft", outcome["errors"])
        recorded = poe.read_json(self.run_dir / "candidates/crafted-boots/evaluation.json")
        equipped=next(item for item in recorded['result']['equipped'] if item['slot']=='Boots')
        self.assertIs(equipped['ordinary_craft_supported'],True)
        self.assertEqual(next(row for row in recorded['materialization']['item_generation']['items'] if row['slot']=='Boots')['basis'],'native_recipe')
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
        self.assertEqual(output['files'],[])
        final=poe.read_json(self.run_dir/'output/crafted-boots/final-assessment.json')
        self.assertIn("crafted_items",poe.report_html(poe.read_json(self.run_dir/'run.json'),final))
        self.assertFalse(any(gap.startswith('native:item_generation:') for gap in final['pending']))
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
        self.assertEqual(diagnostic["code"],"native_invalid_input",diagnostic)
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
        response=poe.export(args(run=str(self.run_dir),name='missing-belt'))
        final=poe.read_json(self.run_dir/'output/missing-belt/final-assessment.json')
        self.assertEqual(response['status'],'blocked');self.assertFalse(response['file_exported'])
        self.assertIn(final['publication']['facts_summary'],response['facts_summary'])
        self.assertIn('未导出交付文件',response['facts_summary'])
        self.assertIn('自检未通过',response['reply_policy'])
        self.assertEqual(response['files'],[])

    def test_17_gem_probe_matches_individual_engine_and_does_not_write_candidate(self):
        self.evaluate_plan(self.refine, "gem-probe")
        folder = self.run_dir / "candidates/gem-probe"
        snapshot = {path.name: bx.digest(path.read_bytes()) for path in folder.iterdir()}
        result = poe.probe(args(run=str(self.run_dir), name="gem-probe", group=1, index=2,
                                ids="SupportIronGrip,SupportBrutality,SupportSpellEcho", level=19, quality=0))
        self.assertEqual(result["status"], "screened")
        rows = result["alternatives"]
        self.assertAlmostEqual(rows[1]["observed_delta"], 0, places=6)
        self.assertEqual(rows[1]["metric_observation"]["context"]["main_group"],1)
        self.assertEqual(rows[1]["metric_observation"]["value"],rows[1]["stats"]["CombinedDPS"])
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
        self.assertEqual(next(item for item in result['equipped'] if item['slot']=='Helmet')['rarity'],'UNIQUE')
        self.assertFalse(any(gap.startswith('native:item_generation:') for gap in outcome['pending']))
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
        self.assertEqual(exported['files'],[])
        report = poe.report_html(poe.read_json(self.run_dir/'run.json'),poe.read_json(exported['details_file']))
        self.assertIn("原生 Full DPS 分项", report)
        self.assertIn("automatic_cost_per_second", report)
        self.assertIn("19/0", report)
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
        self.assertEqual(result["alternatives"][0]["observed_metric"], "MinionCombinedDPS")
        self.assertEqual(result["alternatives"][0]["metric_observation"]["meaning"]["actor"],"minion")
        self.assertAlmostEqual(result["alternatives"][0]["observed_delta"], 0)
        self.assertNotEqual(result["alternatives"][1]["observed_delta"], 0)
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
        self.assertEqual(result["reply_policy"],poe.READONLY_REPLY_POLICY)
        self.assertLessEqual(len(stdout.getvalue()), 12000)
        self.assertNotIn("calculation", result)
        self.assertGreater(len(path.read_text(encoding="utf-8")), 12000)
        for key in ("engine_version", "data_version", "tree_version"):
            self.assertIn(str(saved["calculation"][key]), result["diagnosis_summary"])

        report=Path(self.temp.name)/'readonly.html';stdout=io.StringIO()
        before=self.sample.read_bytes()
        with redirect_stdout(stdout):
            code=poe.main(['inspect','--input',str(self.sample),'--metrics','Life,UnavailableMetric','--out',str(path),'--report',str(report)])
        self.assertEqual(code,0)
        saved=poe.read_json(path);receipt=json.loads(stdout.getvalue())
        self.assertEqual(saved['publication']['facts_summary'].split('\n\n',1)[0],saved['publication']['delivery_brief']['message'])
        self.assertEqual(saved['publication']['statements'][0]['text'],saved['publication']['delivery_brief']['message'])
        self.assertEqual(receipt['report_sha256'],bx.digest(report.read_bytes()))
        self.assertEqual(receipt['reply_policy'],poe.READONLY_REPLY_POLICY)
        self.assertEqual(saved['input_sha256'],bx.digest(before));self.assertEqual(self.sample.read_bytes(),before)
        self.assertEqual(saved['reference_contract']['current']['artifact']['pointer'],'/calculation')
        def complete_table(key):
            rows=list(receipt[key])
            link=next((row for row in receipt.get('continuations',[]) if row['pointer']=='/'+key),None)
            if link:
                offset=link['offset']
                while True:
                    page=poe.view_json(args(file=link['file'],pointer=link['pointer'],fields='',keys=False,
                                            query='',limit=link['limit'],offset=offset))
                    self.assertEqual(page.get('deferred_values',[]),[])
                    rows.extend(page['value'])
                    if page['next_offset'] is None:break
                    self.assertGreater(page['next_offset'],offset);offset=page['next_offset']
            self.assertEqual(rows,saved[key])
            return rows
        self.assertEqual(complete_table('breakdown_summary')[1]['status'],'unsupported')
        action=next(row for row in complete_table('next_actions') if row['pointer']=='/calculation/breakdowns/1/available_metrics')
        self.assertIn('Life',facts.pointer(saved,action['pointer']))
        for row in receipt.get('continuations',[]):facts.pointer(saved,row['pointer'])
        page=report.read_text(encoding='utf-8')
        self.assertIn(saved['input_sha256'],page)
        for row in saved['publication']['measurements']:
            self.assertIn(html.escape(row['text']),page)
        for row in saved['calculation']['gems']:
            self.assertIn(html.escape(row.get('display_name') or row.get('name') or row['id']),page)

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
        result=engine.calculate(self.pob,xml,breakdowns=[{"metric":"Life"},{"metric":"UnavailableMetric"},{"metric":"Life","skill_ref":minion["skill_ref"],"actor":"minion"},{"metric":"LifeLeechRate"},{"metric":"LifeLeech"}])
        life,missing,summon,rate,detail=result["breakdowns"]
        for row in result['breakdowns']:
            self.assertEqual(row['value_status'],'observed' if bx.finite_number(row.get('value')) else 'unavailable')
            self.assertEqual(row['detail_status'],row['status'] if 'detail' in row else 'unavailable')
            if bx.finite_number(row.get('value')):self.assertIn(row['metric'],row['available_metrics'])
        self.assertIn(rate['metric'],missing['available_metrics'])
        self.assertEqual(life["status"],"observed");self.assertEqual(life["value"],baseline["stats"]["Life"])
        self.assertTrue(life["matches_main_value"]);self.assertEqual(life["context"]["xml_sha256"],bx.digest(xml))
        self.assertEqual(missing["status"],"unsupported");self.assertNotIn("value",missing)
        self.assertEqual(summon["selected_skill_id"],minion["id"]);self.assertEqual(summon["actor"],"minion")
        self.assertGreater(summon["value"],0)
        if "detail" not in summon: self.assertEqual(summon["status"],"unsupported");self.assertIn("No native breakdown",summon["reason"])
        self.assertEqual(rate["status"],"observed");self.assertEqual(rate["value"],baseline["stats"]["LifeLeechRate"])
        self.assertTrue(rate["matches_main_value"]);self.assertEqual(rate["detail_metric"],detail["metric"])
        self.assertEqual(rate["detail"],detail["detail"])
        self.assertTrue(rate["detail_sources"])
        self.assertTrue(all(row["module"]=="Modules/CalcSections" and row["output"]=="LifeLeechRate" for row in rate["detail_sources"]))
        self.assertEqual(engine.character_state(result),engine.character_state(baseline));self.assertEqual(result["stats"],baseline["stats"])
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
            changes=row['equipment_changes']['changes']
            if variant['operations'].get('equip'):
                change=next(change for change in changes if change['slot']=='Belt')
                actual=next(item for item in measured['equipped'] if item['slot']=='Belt')
                self.assertEqual(change['after'],actual)
                self.assertTrue(any('Belt' in text for text in row['actual_changes']))
            else:
                self.assertEqual(changes,[])
        self.assertEqual(bx.digest(candidate.read_bytes()),original_hash)
        screen=poe.probe(args(run=str(self.run_dir),name="combined-probe",node_ids=str(node["id"])))
        self.assertEqual(screen["alternatives"][0]["status"],"screened")
        self.assertIn("excludes path cost",screen["alternatives"][0]["scope"])

    def test_52_route_material_specs_and_xml_recovery_cross_stage_local_ids(self):
        route=route_fixture(self.native['data_version']);route.update(class_id=3,ascendancy=None,gems={'Fireball':{}},stages=[],inventory=[])
        gem={'kind':'gem','instance_id':'fire','skill_id':'Fireball','level':1,'quality':0,'quantity':1,'acquisition':'owned','evidence_refs':['held-gem']}
        route['evidence']=[{'evidence_id':'user','kind':'user_statement','subject':'intent','text':'Offline interface fixture'},
                           {'evidence_id':'held-gem','kind':'user_statement','subject':facts.material_ref(gem),'text':'held one gem','context':{'quantity':1,'instance_id':'fire'}}]
        for index,sockets in enumerate(('B','B-B')):
            text='Rarity: NORMAL\nWool Gloves\nSockets: '+sockets
            item={'kind':'item','item_id':1,'instance_id':'item-'+str(index),'spec_sha256':bx.digest(bx.normal_item(text)),
                  'quantity':1,'acquisition':'owned','evidence_refs':['held-item-'+str(index)]}
            route['inventory'].append({key:value for key,value in item.items() if key not in {'item_id','instance_id','acquisition'}})
            route['evidence'].append({'evidence_id':item['evidence_refs'][0],'kind':'user_statement','subject':facts.material_ref(item),
                                      'text':'held exact specification','context':{'quantity':1,'spec_sha256':item['spec_sha256']}})
            route['stages'].append({'stage_id':'phase-'+str(index),'name':'Fixture stage','level':12+index,'bases':{'Gloves':'Wool Gloves'},
                'skill_groups':[{'name':'Fireball','slot':'Gloves','use':'fixture','gems':[{'instance_id':'fire','skill_id':'Fireball','level':1,'quality':0}]}],
                'required_items':([gem] if index==0 else [])+[item],
                'snapshot':{'operations':{'items':[{'id':1,'text':text}],'equip':[{'slot':'Gloves','item_id':1}],'main_group':1},
                            **({'metrics':['Life','FutureRequestedMetric']} if index else {})}})
        source=Path(self.temp.name)/'specs-route.json';output=Path(self.temp.name)/'specs-route.html';poe.write_json(source,route)
        with patch.object(facts,'gem_facts',return_value={}),patch.dict(poe.METRICS,{'FutureOptionalMetric':'Optional display metric'}):
            poe.route_command(args(invoked=True,file=str(source),out=str(output)))
        checked=poe.read_json(output.with_suffix('.evidence.json'))['checked_route']
        self.assertIsNone(checked['stages'][0]['calculation']['stats']['FutureOptionalMetric'])
        self.assertNotIn('stage:phase-0:metric:FutureOptionalMetric',checked['pending'])
        self.assertIsNone(checked['stages'][1]['calculation']['stats']['FutureRequestedMetric'])
        self.assertIn('stage:phase-1:metric:FutureRequestedMetric',checked['pending'])
        materials=[row for row in checked['budget_assessment']['materials'] if row['kind']=='item']
        self.assertEqual(len(materials),2);self.assertEqual([row['quantity'] for row in materials],[1,1])
        self.assertEqual([row['remaining'] for row in materials],[0,0])
        self.assertEqual(len({row['requirement_id'] for row in materials}),2)
        self.assertTrue(all(stage['calculation']['native_calculated'] for stage in checked['stages']))
        self.assertFalse(any(':material:item:' in gap for gap in checked['pending']))
        self.assertFalse(any('native:item_generation:' in gap for gap in checked['pending']))
        state=output.with_suffix('.state.json');value=poe.read_json(state)
        xmls=[Path(row['calculation']['xml_path']) for row in checked['stages']]
        self.assertTrue(all(str(path) in value['files'] for path in xmls))
        current=poe.workflow_status(args(route_state=str(state)))
        self.assertFalse(current['file_exported']);self.assertTrue(current['files_intact'])
        unchanged=xmls[1].read_bytes();stamp=xmls[1].stat().st_mtime_ns
        xmls[0].unlink()
        missing=poe.workflow_status(args(route_state=str(state)))
        self.assertFalse(missing['file_exported']);self.assertIn(str(xmls[0]),missing['invalid_artifacts'])
        self.assertEqual(missing['completed_stages'],['phase-0','phase-1'])
        with patch.object(facts,'gem_facts',return_value={}),patch.object(poe,'roundtrip',wraps=poe.roundtrip) as replay:
            poe.route_command(args(invoked=True,file=str(source),out=str(output)))
        self.assertEqual(replay.call_count,1)
        self.assertEqual(xmls[1].read_bytes(),unchanged);self.assertEqual(xmls[1].stat().st_mtime_ns,stamp)
        current=poe.workflow_status(args(route_state=str(state)))
        self.assertFalse(current['file_exported']);self.assertTrue(current['files_intact'])

    def test_36_route_snapshots_bind_native_claims_and_leave_missing_stage_unknown(self):
        route=route_fixture(self.native['data_version']);route.update(class_id=3,ascendancy=None,gems={"Fireball":{}},stages=[])
        text="Rarity: NORMAL\nWool Gloves\nSockets: B"
        held_gem={"kind":"gem","instance_id":"fire","skill_id":"Fireball","level":1,"quality":0,"quantity":1,"acquisition":"owned","evidence_refs":["held-gem"]}
        held_item={"kind":"item","item_id":201,"spec_sha256":bx.digest(bx.normal_item(text)),"quantity":1,"acquisition":"owned","evidence_refs":["held-item"]}
        route["evidence"]=[{"evidence_id":"user","kind":"user_statement","subject":"intent","text":"Offline fixture only"},
            {"evidence_id":"held-gem","kind":"user_statement","subject":facts.material_ref(held_gem),"text":"Fixture: held Fireball 1/0 x1","context":{"quantity":1,"instance_id":"fire"}},
            {"evidence_id":"held-item","kind":"user_statement","subject":facts.material_ref(held_item),"text":"Fixture: held exact gloves","context":{"quantity":1,"spec_sha256":held_item["spec_sha256"]}}]
        route["claims"]=[{"claim_id":"stage","text":"Conditional snapshot, not a gameplay recommendation","subjects":["stage:a","snapshot:a","goal"],"evidence_refs":["native:stage:a#/stats/Life"]},
                         {"claim_id":"preference","text":"Recorded user preference","subjects":["goal"],"evidence_refs":["user"]}]
        stage={"stage_id":"a","name":"Fixture phase","level":12,"bases":{"Gloves":"Wool Gloves"},"key_transition":True,
               "skill_groups":[{"name":"Fireball","slot":"Gloves","use":"fixture","gems":[{"instance_id":"fire","skill_id":"Fireball","level":1,"quality":0}]}],
               "required_items":[held_gem,held_item],"gear":["Actual fixture gloves"],"transition":["Keep the preceding configuration until the next prerequisite is met"],"snapshot":{"assumptions":["stage"],"operations":{"items":[{"id":201,"text":text}],"equip":[{"slot":"Gloves","item_id":201}],"main_group":1},"metrics":["Life","ManaCost"]}}
        route["stages"]=[stage,{**copy.deepcopy(stage),"stage_id":"b","level":None,"required_items":[]}]
        stage.pop('bases')
        path=Path(self.temp.name)/"route.json";output=Path(self.temp.name)/"route.html";poe.write_json(path,route)
        poe.write_text(output,'Previous generated route')
        with patch.object(facts,"gem_facts",return_value={"Fireball":{"status":"unverified"}}):
            response=poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        checked=poe.read_json(output.with_suffix(".evidence.json"))["checked_route"]
        first,second=checked["stages"]
        self.assertNotIn('native:socket_capacity:a:Gloves',checked['pending'])
        self.assertEqual(first['socket_evidence']['Gloves']['capacity'],1)
        review_stages=poe.read_json(output.with_suffix(".review.json"))["facts"]["stages"]
        self.assertIsNone(review_stages[0]["previous_stage_id"])
        self.assertEqual(review_stages[1]["previous_stage_id"],"a")
        for index,current in enumerate((first,second)):
            for field in ("bases","skill_groups","required_items","instance_transitions","gear","transition"):
                self.assertEqual(review_stages[index][field],current.get(field,{} if field=="bases" else []))
        self.assertEqual(review_stages[0]["equipment_reference"],"native:stage:a#/equipped")
        self.assertIsNone(review_stages[1]["equipment_reference"])
        self.assertEqual(review_stages[1]["calculation"]["status"],"not_calculated")
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
        self.assertEqual(claim_action["claim_ids"],["stage","preference"])
        scope_action=next(a for a in response["next_actions"] if a["action"]=="review_evidence_scope")
        self.assertEqual([row['claim_id'] for row in scope_action['claims']],['preference'])
        self.assertEqual(scope_action['reference_contract_pointer'],'/checked_route/reference_contract')
        self.assertEqual(claim_action["input_revision"],bx.digest(path.read_bytes()))
        self.assertEqual(first["calculation"]["stats"]["Life"],checked["evidence"]["native:stage:a#/stats/Life"]["value"])
        self.assertEqual(checked["evidence"]["native:stage:a#/stats/Life"]["context"]["xml_sha256"],first["calculation"]["xml_sha256"])
        self.assertTrue(Path(first["calculation"]["xml_path"]).exists())
        self.assertNotIn("stats",second["calculation"]);self.assertEqual(second["calculation"]["status"],"not_calculated")
        self.assertIn("native:snapshot:b",response["pending"])
        self.assertEqual(checked["budget_assessment"]["status"],"user_declared")
        self.assertFalse(output.exists())
        saved_state=poe.read_json(output.with_suffix('.state.json'))
        self.assertNotIn(str(output),saved_state['files'])
        self.assertTrue(poe.workflow_status(args(route_state=str(output.with_suffix('.state.json'))))['files_intact'])
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
        self.assertIn(schedule_diagnostic,facts.pointer(poe.read_json(output.with_suffix(".evidence.json")),repair["pointer"]))
        stage["snapshot"].pop("timeline")
        stage["entry_checks"]=[{"check_id":"conditional-life","locator":"/stats/Life","op":"ge","value":first["calculation"]["stats"]["Life"]+1,"claim_id":"stage"}]
        route["assessment"]={"goal_claim_ids":["stage"],"scope":"Fixture only"}
        poe.write_json(path,route)
        with patch.object(facts,"gem_facts",return_value={}),patch.object(poe,"roundtrip",side_effect=ValueError("must reuse this verified stage")):
            response=poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        changed=poe.read_json(output.with_suffix(".evidence.json"))["checked_route"]
        self.assertTrue(changed["stages"][0]["calculation"]["roundtrip_verified"])
        self.assertEqual(changed["stages"][0]["calculation"]["entry_checks"][0]["status"],"unmet")
        route["assessment"]["review"]={"token":response["review_token"],"findings":"Fixture condition remains explicitly unmet.","unresolved":[],"conclusions":[review_row('claim_id',c['claim_id'],'Fixture stage only; entry check remains unmet','Checked current fixture and retained missing prerequisites.') for c in route.get("claims",[])]}
        poe.write_json(path,route)
        with patch.object(facts,"gem_facts",return_value={}):
            response=poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        reviewed=poe.read_json(output.with_suffix(".evidence.json"))["checked_route"]
        self.assertEqual(reviewed["final_review"]["status"],"unresolved")
        self.assertEqual(reviewed["final_review"]["coverage"]["missing_objective_ids"],[row["objective_id"] for row in route["objectives"]])
        template=poe.read_json(output.with_suffix(".review.json"))["review_template"]
        route["assessment"]["review"]["objectives"]=[{**row,"verdict":"unknown","scope":"Current fixture requirement only", "findings":"Current entry check remains unmet; no gameplay guarantee."} for row in template["objectives"]]
        poe.write_json(path,route)
        with patch.object(facts,"gem_facts",return_value={}):
            response=poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        reviewed=poe.read_json(output.with_suffix(".evidence.json"))["checked_route"]
        self.assertEqual(reviewed["final_review"]["status"],"model_reviewed")
        self.assertTrue(any("entry_check:" in p for p in response["pending"]))
        self.assertFalse(poe.read_json(output.with_suffix(".state.json"))["file_exported"])
        self.assertEqual(response['files'],[])
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
        ref=first["calculation"]["active_skills"][0]["skill_ref"]
        stage["snapshot"]["usage"]=[{"skill_ref":ref,"claim_id":"explicit-use"}]
        usage_claim={"claim_id":"explicit-use","text":"Current stage action; execution coverage remains unmeasured.",
                     "subjects":["skill:"+ref],"evidence_refs":["native:stage:a#/active_skills"]}
        route["claims"].append(usage_claim)
        usage_claim["bindings"]=facts.claim_bindings(route,usage_claim,engine.fingerprint(self.pob))
        poe.write_json(path,route)
        with patch.object(facts,"gem_facts",return_value={}):
            poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        checked=poe.read_json(output.with_suffix(".evidence.json"))["checked_route"]
        self.assertFalse(any(d["code"].startswith("usage_claim_") for d in checked["diagnostics"]),checked["diagnostics"])
        stage["snapshot"]["metrics"].append("UnobservedMetric")
        poe.write_json(path,route)
        with patch.object(facts,"gem_facts",return_value={}):
            poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        checked=poe.read_json(output.with_suffix(".evidence.json"))["checked_route"]
        metric=next(d for d in checked["diagnostics"] if d["code"]=="snapshot_metric_unobserved")
        self.assertEqual(metric["location"],"/stages/0/snapshot/metrics")
        self.assertEqual(metric["actual"],["UnobservedMetric"])
        self.assertIn("Life",metric["expected"]["observed_stats"])
        self.assertIn("stage:a:metric:UnobservedMetric",checked["task_completion"]["pending"])

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
        self.assertTrue(any(row.get('code')=='native_tree_nodes_unretained' and row['location']==diagnostic['location'] for row in facts.pointer(poe.read_json(output.with_suffix('.evidence.json')),repair['pointer'])))
        self.assertEqual(repair['input_revision'],bx.digest(path.read_bytes()))
        self.assertIn('普通天赋已分配',response['facts_summary'])
        self.assertFalse(output.exists())
        html=routes.render(checked,response['warnings'],self.native['engine_version'],self.native['tree_version'],evidence_file=output.with_suffix('.evidence.json').name)
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
        self.assertFalse(any(d['code']=='native_tree_nodes_unretained' for d in poe.read_json(output.with_suffix('.evidence.json'))['checked_route']['diagnostics']))
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
        named_items=[(index,item) for index,item in enumerate(result['equipped']) if item.get('name_cn')]
        titled_items=[item for item in result['equipped'] if item.get('title_cn')]
        self.assertTrue(titled_items)
        for item in titled_items:
            self.assertIn(item['title'],item['name'])
            self.assertIn(item['title_cn'],item['display_name'])
            self.assertEqual(item['title_name_source'],'translations/Uniques.txt.csv')
        self.assertTrue(named_items)
        for index,item in named_items:
            row=facts.resolve_reference(f'native:candidate#/equipped/{index}/name_cn',{},{'candidate':result})
            self.assertEqual(row['value'],item['name_cn'])
            self.assertEqual(row['meaning']['object_identity']['id'],item['id'])
            self.assertEqual(row['meaning']['object_identity']['name'],item['name'])
            self.assertEqual(row['meaning']['object_identity']['name_source'],item['name_source'])
            self.assertIn(item['name'],item['display_name'])
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
        self.assertEqual(len(exported["judgments"]),1)
        display=exported["judgments"][0]
        self.assertEqual((display["claim_id"],display["status"],display["revision_pending"]),("facts","unresolved",True))
        self.assertNotIn(final["claims"][0]["rendered_text"],display["text"])
        self.assertEqual(final["response"]["judgments"],exported["judgments"])
        self.assertTrue(any(d["code"]=="claim_binding_missing" for d in display["diagnostics"]))
        report=poe.report_html(poe.read_json(self.run_dir/'run.json'),final)
        for measurement in exported["publication"]["measurements"]:
            self.assertIn(measurement["text"],report)

    def test_43_goal_reference_repair_is_located_without_changing_the_build(self):
        plan=copy.deepcopy(self.refine)
        plan["claims"]=[{"claim_id":"overall","text":"Limited overall interpretation","subjects":["goal"],"evidence_refs":["native:candidate#/stats/Life"]},
                        {"claim_id":"detail","text":"A detail interpretation","subjects":["objective:direction"],"evidence_refs":["native:candidate#/stats/Life"]}]
        plan["assessment"]={"goal_claim_ids":["absent","overall"],"scope":"Fixture scope, not a player recommendation"}
        source=Path(self.temp.name)/"goal-contract.json";request=Path(self.temp.name)/"goal-contract-changes.json"
        poe.write_json(request,{"set":plan})
        first=poe.edit_document(args(invoked=True,kind="plan",file=str(source),changes=str(request),if_sha256=None))
        result=poe.evaluate(args(run=str(self.run_dir),plan=str(source),name="goal-contract"))
        self.assertIn("goal_assessment",result["pending"])
        repair=next(a for a in result["next_actions"] if a["action"]=="repair_input")
        diagnostic=next(d for d in facts.pointer(poe.read_json(self.run_dir/"candidates/goal-contract/evaluation.json"),repair["pointer"]) if d["code"]=="goal_claim_missing")
        self.assertEqual(diagnostic["location"],"/assessment/goal_claim_ids/0")
        self.assertEqual(diagnostic["actual"]["claim_id"],"absent")
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
        artifacts={str(path):path.read_bytes() for path in (self.run_dir/'output/delivery-context').iterdir() if path.name!='delivery.json'}
        self.assertTrue(artifacts);self.assertEqual(exported['files'],[])
        plan["summary"]="A revised interpretation of the same build inputs."
        self.evaluate_plan(plan,"delivery-context")
        status=poe.workflow_status(args(run=str(self.run_dir)))
        current=next(c for c in status["candidate_records"] if c["name"]=="delivery-context")
        self.assertTrue(current["valid"]);self.assertFalse(current["file_exported"])
        receipt=poe.read_json(self.run_dir/"output/delivery-context/delivery.json")
        self.assertFalse(receipt["file_exported"])
        self.assertTrue(all(Path(path).read_bytes()==content for path,content in artifacts.items()))

    def test_39_final_review_and_optional_url_survive_blocked_export_without_render(self):
        plan=copy.deepcopy(self.refine)
        source=Path(self.temp.name)/"local-evidence.txt";source.write_text("Offline local record",encoding="utf-8")
        plan["evidence"]=[{"evidence_id":"artifact","kind":"artifact","subject":"fixture","path":source.name,"excerpt":"Offline local record"}]
        plan["claims"]=[{"claim_id":"review","text":"Fixture conclusion; remaining gaps are retained.","subjects":["goal"],"evidence_refs":["artifact","native:candidate#/stats/Life","native:candidate#/xml"]}]
        plan["assessment"]={"goal_claim_ids":["review"],"scope":"Fixture, not a player build"}
        first=self.evaluate_plan(plan,"reviewed")
        template=poe.read_json(self.run_dir/"candidates/reviewed/review.json")["review_template"]
        plan["assessment"]["review"]={"token":first["review_token"],"findings":"Checked current facts, object, conditions and limited conclusion.","unresolved":[],
            "objectives":[{**row,"verdict":"unknown","scope":"Current fixture requirement","findings":"Remaining dependencies retained; not a gameplay certification."} for row in template["objectives"]],
            "conclusions":[review_row('claim_id','review','Fixture only; remaining gaps retained','Artifact is a local record, not an independent gameplay measurement.')]}
        second=self.evaluate_plan(plan,"reviewed")
        self.assertEqual(first["review_token"],second["review_token"])
        self.assertNotIn("final_review:stale",second["pending"])
        result=poe.export(args(run=str(self.run_dir),name="reviewed"))
        final=poe.read_json(self.run_dir/"output/reviewed/final-assessment.json")
        self.assertEqual(final["review_packet"]["status"],"model_reviewed")
        self.assertEqual(len(result["judgments"]),1)
        self.assertTrue(result["judgments"][0]["revision_pending"])
        self.assertNotIn(plan["claims"][0]["text"],result["judgments"][0]["text"])
        self.assertEqual(final["claims"][0]["text"],plan["claims"][0]["text"])
        self.assertTrue(any(d["code"]=="claim_binding_missing" for d in result["judgments"][0]["diagnostics"]))
        self.assertTrue(any(value.startswith('objective:') for value in final['pending']))
        self.assertIsNone(final["evidence"]["artifact"]["url"])
        self.assertFalse(result["file_exported"]);self.assertEqual(result['files'],[])
        manifest=poe.read_json(self.run_dir/"output/reviewed/delivery.json")
        for name,digest in manifest["files"].items(): self.assertEqual(bx.digest((self.run_dir/"output/reviewed"/name).read_bytes()),digest)
        with patch.object(poe,"report_html",side_effect=OSError("must not render blocked delivery")) as render:
            self.assertFalse(poe.export(args(run=str(self.run_dir),name="reviewed"))["file_exported"])
        render.assert_not_called()
        self.assertEqual(poe.read_json(self.run_dir/"run.json")["phase"],"evaluated")
        self.assertFalse(poe.read_json(self.run_dir/"output/reviewed/delivery.json")["file_exported"])
        self.assertFalse(poe.export(args(run=str(self.run_dir),name="reviewed"))["file_exported"])
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
        self.assertEqual(engine.character_state(probed),engine.character_state(seed))
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
        self.assertFalse(result["file_exported"]);self.assertEqual(result['files'],[]);self.assertEqual(result["errors"],[])
        checked=poe.read_json(output.with_suffix(".evidence.json"))["checked_route"]["stages"][0]
        self.assertTrue(checked["equipment_validation"]["checks"]["Weapon 2"]["valid_for_slot"])
        self.assertNotIn("native:equipment:"+stage["stage_id"]+":Weapon 2",result["pending"])
        self.assertIn("declared_base_layout",json.dumps(checked["equipment_validation"]))
        self.assertFalse(output.exists())
        stage["bases"]["Future Slot"]=None;poe.write_json(path,route)
        with self.assertRaises(engine.EngineError) as error:poe.route_command(args(invoked=True,file=str(path),out=str(output)))
        self.assertEqual(error.exception.diagnostic.get("location"),"/stages/0/bases/Future Slot",error.exception.diagnostic)
        state=poe.read_json(output.with_suffix(".state.json"));self.assertEqual(state["status"],"blocked");self.assertFalse(state["file_exported"])

    def test_53_native_route_marks_raw_and_template_ordinary_targets_as_projections(self):
        seed=engine.calculate(self.pob,class_id=3)
        text='Rarity: RARE\nProjected Gloves\nWool Gloves\nItem Level: 1\nSockets: B\n+1000 to maximum Life'
        operations={'items':[{'id':201,'text':text}],'equip':[{'slot':'Gloves','item_id':201}]}
        template=Path(self.temp.name)/'projected-template.xml'
        poe.write_text(template,bx.serialise(bx.apply_plan(bx.parse(seed['xml']),{'mode':'refine','summary':'Projection fixture','operations':operations},native=seed)))
        for origin in ('items','template'):
            route=route_fixture(self.native['data_version']);route.update(class_id=3,ascendancy=None,gems={'Fireball':{}},stages=[])
            route['evidence']=[{'evidence_id':'user','kind':'user_statement','subject':'intent','text':'Offline generation projection fixture'}]
            stage={'stage_id':'projected','name':'Projected stage','level':12,'act':1,'completed_act':1,'bases':{'Gloves':'Wool Gloves'},
                   'skill_groups':[{'name':'Fireball','slot':'Gloves','use':'fixture','gems':[{'instance_id':'fire','skill_id':'Fireball','level':1,'quality':0}]}],
                   'required_items':[{'kind':'item','spec_sha256':bx.digest(bx.normal_item(text)),'quantity':1,'acquisition':'drop'}],
                   'snapshot':{'operations':copy.deepcopy(operations) if origin=='items' else {},'metrics':['Life']}}
            if origin=='template': stage['snapshot']['template_xml']=str(template)
            route['stages']=[stage]
            path=Path(self.temp.name)/('projection-'+origin+'.json');output=path.with_suffix('.html');poe.write_json(path,route)
            with self.subTest(origin=origin),patch.object(facts,'gem_facts',return_value={}):
                result=poe.route_command(args(invoked=True,file=str(path),out=str(output)))
                self.assertEqual(result['errors'],[])
                checked=poe.read_json(output.with_suffix('.evidence.json'))['checked_route']['stages'][0]['calculation']
                self.assertTrue(checked['native_calculated']);self.assertGreater(checked['stats']['Life'],1000)
                self.assertTrue(any(gap.startswith('native:item_generation:') for gap in checked['pending']),checked['pending'])
                self.assertEqual(checked['status'],'draft');self.assertEqual(result['task_completion']['status'],'draft')
                detail=poe.read_json(Path(checked['evidence_path']))
                self.assertEqual(detail['item_generation'],checked['item_generation'])
                native_item=next(item for item in detail['native']['equipped'] if item['slot']=='Gloves')
                self.assertIs(native_item['ordinary_craft_supported'],True);self.assertEqual(native_item['rarity'],'RARE')
                self.assertFalse(output.exists())

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
