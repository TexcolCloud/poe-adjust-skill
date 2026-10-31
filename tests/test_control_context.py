"""Requested native control identity survives the shared experiment publication."""
import copy
import unittest
from test_workflow import poe


class ControlObservationTests(unittest.TestCase):
    def test_experiment_keeps_observed_control_meaning_and_unknown_sides(self):
        condition={'id':'future-effect','type':'check','label':'Native effect switch',
                   'display_label':'当前效果开关','value':False,'default':False,
                   'value_basis':'native_unchecked_default','shown':True,
                   'if_enemy_cond':'FutureCondition',
                   'apply_source':{'source':'engine/Future.lua','line':9,'count':3}}
        before={'config_controls':{'future-effect':condition,'unrequested':{'id':'unrequested'}},
                'stats':{'CombinedDPS':0}}
        after=copy.deepcopy(before)
        after['config_controls']['future-effect'].update(value=True,value_basis='input')
        after['config_controls']['future-hidden']={'id':'future-hidden','label':'Distinct native switch',
                                                   'value':False,'default':True,'shown':False}
        originals=copy.deepcopy((before,after))
        operations={'config':{'future-effect':True,'future-hidden':False,'future-unknown':None}}
        measurement=poe.measurement_facts(before,after,operations)
        controls=measurement['requested_config_controls']
        self.assertEqual(set(controls),set(operations['config']))
        self.assertEqual(controls['future-effect']['before'],condition)
        self.assertEqual(controls['future-effect']['after'],after['config_controls']['future-effect'])
        self.assertIsNone(controls['future-hidden']['before'])
        self.assertIs(controls['future-hidden']['after']['value'],False)
        self.assertEqual(controls['future-unknown'],{'before':None,'after':None})
        self.assertEqual((before,after),originals)
        packet=poe.publication('scoped_result',[],[],{'status':'not_requested'},{'current':before},keys=[],
            experiments=[{'id':'joint-request','status':'screened','measurement':measurement}])
        statement=next(row for row in packet['statements'] if row['kind']=='experiment')
        self.assertEqual(statement['measurement']['requested_config_controls'],controls)
        for label in ('当前效果开关','Distinct native switch','future-unknown：原生含义未观测'):
            self.assertIn(label,statement['text'])
            self.assertIn(label,packet['facts_summary'])
        self.assertFalse(measurement['individual_contributions_measured'])

    def test_unrequested_controls_do_not_expand_experiment_payload(self):
        calculation={'config_controls':{'future':{'id':'future','label':'Observed only'}},'stats':{}}
        for operations in ({},{'nodes':{'add':[1]}}):
            with self.subTest(operations=operations):
                measured=poe.measurement_facts(calculation,calculation,operations)
                self.assertEqual(measured['requested_config_controls'],{})
                self.assertEqual(measured['actual_changes'],[])


if __name__=='__main__':
    unittest.main()
