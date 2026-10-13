"""Shared native resource and usage audit for build and route calculations."""
import math
import buildxml as bx
import facts


def number(data, key):
    value=data.get(key)
    return value if bx.finite_number(value) else None


def input_shape(document, *, complete=True):
    """Check declared schedules without querying the native engine."""
    usage=document.get("usage",[])
    bx.require(isinstance(usage,list),"usage 应为数组。",location="/usage",expected="array",actual=type(usage).__name__)
    seen=set()
    for index,row in enumerate(usage):
        path=f"/usage/{index}"
        bx.require_keys(row,{"skill_ref","casts_per_second","active_seconds","recovery_seconds","continuous","claim_id","phase"},path,"usage 字段无效。")
        if 'phase' in row:
            bx.require(row['phase'] in ('preparation','combat') and (not complete or 'timeline' in document),'phase 在共同时间线中使用 preparation/combat。',location=path+'/phase',expected=['preparation','combat'],actual=row['phase'])
            if complete and row['phase']=='preparation':
                declared=document.get('timeline')
                bx.require(isinstance(declared,dict) and 'preparation' in declared,'准备阶段需要声明实际时长及动作。',location='/timeline/preparation',expected={'duration_seconds':'positive seconds','events':'array'},actual=declared.get('preparation') if isinstance(declared,dict) else declared)
        for key in ("skill_ref","claim_id"):
            if key in row or complete and key=="skill_ref": bx.require(isinstance(row.get(key),str) and row[key].strip(),key+" 应为非空引用。",location=path+"/"+key,expected="nonempty string",actual=row.get(key))
        ref=row.get("skill_ref")
        bx.require(ref is None or ref not in seen,"usage.skill_ref 不能重复。",location=path+"/skill_ref",expected="unique reference",actual=ref);seen.add(ref)
        for key in ("casts_per_second","active_seconds","recovery_seconds"):
            if key in row: bx.require(number(row,key) is not None and row[key]>=0,key+" 需要有限非负数。",location=path+"/"+key,expected="finite nonnegative number",actual=row[key])
        if "continuous" in row: bx.require(type(row["continuous"]) is bool,"usage.continuous 应为布尔值。",location=path+"/continuous",expected="boolean",actual=row["continuous"])
        if "timeline" in document: bx.require(not set(row)&{"casts_per_second","active_seconds","recovery_seconds","continuous"},"共同时间线已定义频率/窗口；usage 关联技能、判断及准备/战斗阶段。",location=path,expected=["skill_ref","claim_id","phase"],actual=sorted(row))
    if "timeline" not in document: return
    timeline=document["timeline"]
    bx.require_keys(timeline,{"duration_seconds","events","repeat","preparation"},"/timeline")
    if 'preparation' in timeline:
        preparation=timeline['preparation']
        bx.require_keys(preparation,{'duration_seconds','events'},'/timeline/preparation')
        try: input_shape({'timeline':{**preparation,'repeat':False}},complete=complete)
        except bx.ContractError as exc:
            exc.diagnostic['location']=exc.diagnostic['location'].replace('/timeline','/timeline/preparation',1)
            raise
    if "repeat" in timeline: bx.require(type(timeline["repeat"]) is bool,"repeat 应为布尔值；false 检查有限窗口，true 检查重复周期。",location="/timeline/repeat",expected="boolean",actual=timeline["repeat"])
    duration=number(timeline,"duration_seconds")
    if complete or "duration_seconds" in timeline: bx.require(duration is not None and duration>0,"时间线需要有限正时长。",location="/timeline/duration_seconds",expected="positive seconds",actual=timeline.get("duration_seconds"))
    if complete or "events" in timeline:
        events=timeline.get("events")
        bx.require(isinstance(events,list) and len(events)<=1000,"events 为至多1000次动作的数组。",location="/timeline/events",expected="array (engineering limit 1000)",actual=type(events).__name__)
        for index,event in enumerate(events):
            path=f"/timeline/events/{index}"
            bx.require_keys(event,{"skill_ref","at"},path)
            if complete or "skill_ref" in event: bx.require(isinstance(event.get("skill_ref"),str) and event["skill_ref"].strip(),"时间线动作需要非空 skill_ref。",location=path+"/skill_ref",expected="nonempty string",actual=event.get("skill_ref"))
            if complete or "at" in event:
                at=number(event,"at")
                bx.require(at is not None and at>=0 and (duration is None or at<duration),"动作时间位于周期内。",location=path+"/at",expected={"minimum":0,"exclusive_maximum":duration},actual=event.get("at"))


def payment(data, cost_key, shield_covers_mana, no_cost=False, suffix="Cost"):
    value=number(data,cost_key+suffix)
    if no_cost and value is None: value=0
    if shield_covers_mana:
        if cost_key=="Mana": return 0
        if cost_key=="ES":
            mana=number(data,"Mana"+suffix)
            if no_cost and mana is None: mana=0
            return value+mana if value is not None and mana is not None else None
    return value


def native_rate(skill):
    observed=skill.get("usage_rate",{})
    value=number(observed,"value")
    if value is not None: return value
    costs=skill.get("costs",{})
    return number(costs,"SkillTriggerRate") or number(costs,"Speed")


def native_trigger_rate(skill):
    observed=skill.get("usage_rate",{})
    return number(observed,"value") if observed.get("metric")=="SkillTriggerRate" else number(skill.get("costs",{}),"SkillTriggerRate")


def available_pool(stats, resource):
    pool=number(stats,resource+"Unreserved")
    if pool is None: pool=number(stats,resource)
    cap=number(stats,"EnergyShieldRecoveryCap") if resource=="EnergyShield" else None
    return min(pool,cap) if pool is not None and cap is not None else pool


def fixed_net_regeneration(stats, resource):
    value=number(stats,"Net"+resource+"Regen")
    regen,degen=number(stats,resource+"RegenRecovery"),number(stats,resource+"Degen")
    return value if value is not None else regen-degen if regen is not None and degen is not None else None


def single_payments(result):
    """Independent payments from a full pool, never a sequence or sustain proof."""
    eb=result.get("resource_rules",{}).get("energy_shield_protects_mana")
    rows=[]
    for index,skill in enumerate(result.get("active_skills",[])):
        if skill.get("disabled"): continue
        data={**(result["stats"] if skill.get("is_main") else {}),**skill.get("costs",{})}
        for resource,cost_key in (("Mana","Mana"),("Life","Life"),("EnergyShield","ES")):
            cost=payment(data,cost_key,eb,skill.get("no_cost") is True)
            pool=available_pool(result["stats"],resource)
            rule_known=resource=="Life" or eb is not None or number(data,"ManaCost")==0 or skill.get("no_cost") is True
            known=rule_known and cost is not None and cost>=0 and pool is not None and pool>=0
            affordable=pool>cost if resource=="Life" and cost and known else pool>=cost if known else None
            full=payment(data,cost_key,eb,skill.get("no_cost") is True,suffix="PerSecondCost")
            regen=fixed_net_regeneration(result["stats"],resource)
            full_rule_known=resource=="Life" or eb is not None or number(data,"ManaPerSecondCost")==0 or skill.get("no_cost") is True
            mean_known=full_rule_known and full is not None and full>=0 and pool is not None and pool>=0 and regen is not None
            drain=full-regen if mean_known else None
            mean={"status":"native_mean_projection" if mean_known else "unknown","cost_per_second":full,
                  "native_action_rate":native_rate(skill),"fixed_net_regeneration_per_second":regen,"net_drain_per_second":drain,
                  "mean_pool_depletion_seconds":max(pool-(1 if resource=="Life" else 0),0)/drain if drain is not None and drain>0 else None,
                  "excluded_recovery":["leech","recharge","flask"],
                  "scope":"仅此技能原生满速费用与当前固定净回复的平均投影；未计其他动作、离散支付时点、吸取、充能、药剂补充或条件变化。时间为平均估算，空值不证明无限续航。"}
            rows.append({"skill_ref":skill.get("skill_ref"),"actor":skill.get("actor"),"resource_id":resource,
                         "payment":cost,"pool":pool,"status":"supported" if affordable is True else "refuted" if affordable is False else "unknown",
                         "mean_use":mean,
                         "cost_source":{"skill_pointer":f"/active_skills/{index}/costs","main_fallback_pointer":"/stats" if skill.get("is_main") else None},
                         "scope":"每项独立从当前满可用储量支付一次；不证明任意时点、连续动作、吸取启动或持续补充。"})
    return rows


def timing(rows):
    """A shared window is computable; independent schedules remain explicit gaps."""
    if not rows: return {},False
    windows={(row.get("continuous"),row.get("active_seconds"),row.get("recovery_seconds")) for row in rows}
    return (rows[0],False) if len(windows)==1 else ({},True)


def capabilities():
    """Describe the projection this adapter actually implements."""
    return {"timeline":{"supported":["discrete_native_skill_payments","constant_native_net_regeneration","declared_fixed_native_trigger_payments"],
            "schedule_modes":{"finite_window":"repeat=false","repeating_cycle":"repeat=true (default)"},
            "excluded_recovery":["leech","recharge","flask"],
            "unsupported":["action_exclusion","changing_conditions","non_discrete_payments","trigger_realization"],
            "event_reference":"current active_skills[].skill_ref",
            "event_semantics":"每个 {skill_ref,at} 只声明该时点的一次支付；不是开始持续施放的指令。模型选择真实动作安排，次数、频率和首末时点由同一时间线计算。",
            "scope":"当前配置固定净回复下的离散费用时间线；未计敌人伤害、额外资源损失、命中吸取、充能、药剂补充、动作互斥或条件变化，也不改变原生 DPS。未测量部分可能使实际结果更好或更差。"}}


def timeline_assessment(result, timeline, usage=None):
    """Project declared preparation once, then combat using the resulting balances."""
    input_shape({'timeline':timeline,'usage':usage or []})
    skills={s['skill_ref']:s for s in result.get('active_skills',[]) if s.get('skill_ref') and not s.get('disabled')}
    prepared={row['skill_ref'] for row in usage or [] if row.get('phase')=='preparation'}
    preparation=None;initial=None
    if 'preparation' in timeline:
        declared=timeline['preparation']
        requested=prepared|{event['skill_ref'] for event in declared['events']}
        for ref in requested:
            bx.require(ref in skills,'准备动作使用本次原生 skill_ref。',location='/timeline/preparation/events',expected=list(skills),actual=ref)
        preparation=_timeline_window(result,{**declared,'repeat':False},{ref:skills[ref] for ref in requested},location='/timeline/preparation')
        initial={row['resource_id']:row.get('final') if row['status']=='supported' else None for row in preparation['resources']}
        preparation['scope']='仅声明的准备动作及当前固定净回复；未安排的自动效果、其他损失、条件变化和准备效果在战斗中的覆盖未测得。余额按实际终点带入战斗，不自动回满。'
    combat_refs={event['skill_ref'] for event in timeline['events']}
    combat={ref:skill for ref,skill in skills.items() if ref not in prepared or skill.get('trigger') or ref in combat_refs}
    outcome=_timeline_window(result,timeline,combat,initial=initial)
    if preparation:
        for ref,scheduled in outcome['skill_schedules'].items():
            previous=preparation['skill_schedules'].get(ref,{})
            first,last=scheduled['first_at'],previous.get('last_at')
            rate=scheduled['native_rate']
            if first is not None and last is not None and rate and rate>0 and declared['duration_seconds']+first-last<1/rate-1e-8:
                index=next(event['source_index'] for event in outcome['events'] if event['skill_ref']==ref)
                outcome['errors'].append('准备到战斗的同技能动作间隔不足：'+ref)
                outcome['diagnostics'].append({'code':'timeline_transition_interval','location':f'/timeline/events/{index}/at','skill_ref':ref,
                                               'expected':{'minimum_interval_seconds':1/rate},'actual':declared['duration_seconds']+first-last,
                                               'action':'edit_input','message':'核对准备末次到战斗首次动作的原生间隔；准备只执行一次，战斗重复接缝另查。'})
        outcome['preparation']=preparation
        outcome['combat_status']=outcome['status']
        for field in ('errors','pending','diagnostics'): outcome[field]=preparation[field]+outcome[field]
        outcome['pending']=list(dict.fromkeys(outcome['pending']))
        outcome['status']='refuted' if outcome['errors'] else 'unknown' if outcome['pending'] else 'supported'
        outcome['scope']=preparation['scope']+' '+outcome['scope']
    return outcome


def _timeline_window(result, timeline, skills, *, initial=None, location='/timeline'):
    """Replay discrete payments on one clock using constant native net regen.

    This bounded projection excludes leech, recharge and flask refills. Channels
    and automatic effects without a complete declared native-rate payment pattern
    stay unknown. A payment projection never certifies the trigger actually fires.
    """
    duration=number(timeline,"duration_seconds")
    repeating=timeline.get("repeat",True)
    events=timeline.get("events")
    rows=[]; diagnostics=[]; pending=[]; errors=[]; previous={}; automatic_schedules={}
    for index,event in enumerate(events):
        path=f"{location}/events/{index}"
        ref,at=event.get("skill_ref"),number(event,"at")
        bx.require(ref in skills,"时间线动作使用当前原生 skill_ref。",location=path+"/skill_ref",expected=list(skills),actual=ref)
        rows.append({**event,"source_index":index})
    rows.sort(key=lambda row:(row["at"],row["source_index"]))
    for event in rows:
        ref,at=event["skill_ref"],event["at"]
        rate=native_trigger_rate(skills[ref]) if skills[ref].get("trigger") else native_rate(skills[ref])
        if rate is None or rate<=0: pending.append("capability:timeline:frequency:"+ref)
        elif ref in previous and at-previous[ref]<1/rate-1e-8:
            message="时间线同技能动作间隔短于当前原生频率："+ref
            errors.append(message);diagnostics.append({"code":"timeline_frequency_exceeded","location":f"{location}/events/{event['source_index']}/at","expected":{"minimum_interval_seconds":1/rate},"actual":at-previous[ref],"message":message,"action":"edit_input"})
        previous[ref]=at
    # The wraparound interval is also part of a claimed repeating cycle.
    for ref,last in previous.items() if repeating else []:
        first=next(e["at"] for e in rows if e["skill_ref"]==ref)
        rate=native_trigger_rate(skills[ref]) if skills[ref].get("trigger") else native_rate(skills[ref])
        if rate and rate>0 and duration-last+first<1/rate-1e-8:
            errors.append("时间线重复周期的同技能间隔不足："+ref)
            diagnostics.append({"code":"timeline_repeat_interval","location":location+"/duration_seconds","skill_ref":ref,"expected":{"minimum_duration_seconds":last-first+1/rate},"actual":duration,"action":"edit_input","message":"重复周期需要核对末次到下周期首次动作的间隔。"})
    for ref,skill in skills.items():
        if not skill.get("trigger"): continue
        rate=native_trigger_rate(skill)
        times=[row['at'] for row in rows if row['skill_ref']==ref]
        interval=1/rate if rate is not None and rate>0 else None
        complete=bool(times) and interval is not None and times[0]<interval and duration-times[-1]<=interval+1e-8
        if complete:
            complete=all(math.isclose(after-before,interval,rel_tol=1e-7,abs_tol=1e-8) for before,after in zip(times,times[1:]))
        if complete and repeating: complete=math.isclose(duration-times[-1]+times[0],interval,rel_tol=1e-7,abs_tol=1e-8)
        automatic_schedules[ref]={'status':'supported' if complete else 'unknown','native_rate':rate,'interval_seconds':interval,
                                  'event_count':len(times),'first_payment_at':times[0] if times else None,
                                  'scope':'Declared fixed native SkillTriggerRate payment pattern and phase only; startup, targets, trigger realization and changing rates are unmeasured.'}
    eb=result.get("resource_rules",{}).get("energy_shield_protects_mana")
    records=[]
    for resource,cost_key in (("Mana","Mana"),("Life","Life"),("EnergyShield","ES")):
        stats=result["stats"]
        pool=available_pool(stats,resource)
        starting=initial.get(resource) if initial is not None else pool
        regen=fixed_net_regeneration(stats,resource)
        payments={ref:payment(s.get("costs",{}),cost_key,eb,s.get("no_cost") is True) for ref,s in skills.items()}
        gaps=['preparation_balance_unavailable'] if initial is not None and starting is None else []
        for ref,skill in skills.items():
            single=payments[ref];full=number(skill.get("costs",{}),cost_key+"PerSecondCost")
            rate=native_trigger_rate(skill) if skill.get("trigger") else native_rate(skill)
            if skill.get("no_cost") is True and full is None: full=0
            if single is not None and single<0 or full is not None and full<0: gaps.append("negative_payment:"+ref)
            if eb is None and resource in {"Mana","EnergyShield"} and (number(skill.get("costs",{}),"ManaCost") or 0)>0: gaps.append("payment_rule:"+ref)
            if skill.get("trigger") and (single!=0 or full!=0) and automatic_schedules[ref]['status']!='supported':
                gaps.append("automatic:"+ref)
                code='timeline_automatic_rate_unavailable' if rate is None or rate<=0 else 'timeline_automatic_schedule_incomplete'
                pending.append(('capability:timeline:trigger:' if code.endswith('unavailable') else 'resource:'+resource+':timeline_schedule:')+ref)
                diagnostics.append({'code':code,'location':location+'/events','skill_ref':ref,'resource_id':resource,'actual':automatic_schedules[ref],
                                    'action':'inspect_capability' if code.endswith('unavailable') else 'edit_input',
                                    'message':'带费用的自动技能需要当前原生 SkillTriggerRate 和完整声明的固定频率支付安排；模型确定相位、补事件或修改源方案后重算。此投影不证明实际触发或条件覆盖。'})
            if single is None or full is None or (single!=0 or full!=0) and (rate is None or not math.isclose(full,single*rate,rel_tol=1e-5,abs_tol=1e-5)):
                gaps.append("payment_projection:"+ref)
            if single and single>0 and not any(e["skill_ref"]==ref for e in rows) and not skill.get("trigger"):
                gaps.append("unscheduled:"+ref)
                pending.append("resource:"+resource+":timeline_schedule:"+ref)
                diagnostics.append({"code":"timeline_skill_unscheduled","location":location+"/events","skill_ref":ref,"resource_id":resource,"payment":single,
                                    "skill":{key:skill[key] for key in ("id","name","display_name","group","index","actor","costs","usage_rate","trigger") if key in skill},
                                    "affected_window":location,"action":"edit_input",
                                    "repair_options":{"schedule":{"location":location+"/events","event":{"at":None,"skill_ref":ref},"requires":"模型选择实际时点，并核对原生频率及其他动作条件"},
                                                      "unused":{"usage":{"skill_ref":ref,"phase":"unused"},"requires":"模型确认此阶段确实不使用该技能，并撤回依赖其效果的结论"},
                                                      "change_character":{"requires":"按当前原生技能位置停用或调整，重新物化及计算"}},
                                    "message":"此阶段启用的有费用技能未安排到共同时间线；由模型补实际动作、停用或说明阶段不用后重算。准备不替代战斗中的效果覆盖。"})
        known=pool is not None and pool>=0 and starting is not None and starting>=0 and regen is not None and not gaps
        record={"resource_id":resource,"duration_seconds":duration,"initial":starting,"capacity":pool,"net_regeneration_per_second":regen,
                "included_recovery":"native_net_regen_only","excluded_recovery":capabilities()["timeline"]["excluded_recovery"],"status":"unknown","trace":[],"initial_pool_restored":None,"gaps":gaps}
        if known:
            balance=starting;last=0;failed=False
            for event in rows:
                balance=min(pool,balance+regen*(event["at"]-last));before=balance
                cost=payments[event["skill_ref"]];balance-=cost;last=event["at"]
                affordable=before>=cost and (balance>0 if resource=="Life" else balance>=0)
                if not affordable: failed=True
                record["trace"].append({"at":last,"skill_ref":event["skill_ref"],"before":before,"payment":cost,"after":balance,"affordable":affordable})
            balance=min(pool,balance+regen*(duration-last))
            if (resource=="Life" and balance<=0) or (resource!="Life" and balance<0): failed=True
            record.update(final=balance,minimum_balance=min([starting,balance]+[value for event in record["trace"] for value in (event["before"],event["after"])]),
                          status="refuted" if failed else "supported",initial_pool_restored=not failed and balance>=starting-1e-8)
            if failed: errors.append("共同时间线中存在无法支付或耗尽的资源："+resource)
            elif repeating and not record["initial_pool_restored"]: pending.append("resource:"+resource+":timeline_repeat")
        elif pool is None or pool<0 or regen is None or any(gap.startswith(('payment_','negative_payment:')) for gap in gaps): pending.append("capability:timeline:"+resource)
        if initial is not None and starting is None: pending.append('resource:'+resource+':preparation_balance')
        # Sum the same replayed fees, including attempted unaffordable payments.
        # An incomplete projection has no certified total; empty traces are not zero.
        record['scheduled_payment_total']=sum(event['payment'] for event in record['trace']) if known else None
        record['scheduled_payments_by_skill']={ref:sum(event['payment'] for event in record['trace'] if event['skill_ref']==ref) if known else None for ref in skills}
        records.append(record)
    schedules={ref:{'skill_ref':ref,'event_count':sum(event['skill_ref']==ref for event in rows),
                    'first_at':next((event['at'] for event in rows if event['skill_ref']==ref),None),
                    'last_at':previous.get(ref),'native_rate':native_trigger_rate(skill) if skill.get('trigger') else native_rate(skill),
                    'window_average_rate':sum(event['skill_ref']==ref for event in rows)/duration,
                    'scheduled_payments':{record['resource_id']:record['scheduled_payments_by_skill'][ref] for record in records}} for ref,skill in skills.items()}
    return {"status":"refuted" if errors else "unknown" if pending else "supported","duration_seconds":duration,"repeat":repeating,"events":rows,"skill_schedules":schedules,"automatic_schedules":automatic_schedules,"resources":records,"errors":errors,"pending":list(dict.fromkeys(pending)),"diagnostics":diagnostics,
            "scope":capabilities()["timeline"]["scope"]}


def assess(result, intent, plan, claims=None, *, reference_name="resources"):
    """Report observed constraints and scoped usage, not a universal play style."""
    input_shape(plan)
    claims = claims or []
    errors, warnings, pending, resources = [], [], [], []
    stats = result["stats"]
    def needs(subject):
        if not facts.has_claim(claims,subject): pending.append(subject)
    unused = {}; point_budgets=[]
    for label, used_key, limit_key in (("normal","points","normal_points_available"),("ascendancy","ascendancy_points","ascendancy_points"),("secondary_ascendancy","secondary_ascendancy_points","secondary_ascendancy_points")):
        used, limit = result.get(used_key), intent.get(limit_key)
        unused[label] = limit - used if type(limit) is int and type(used) is int else None
        point_budgets.append({"pool":label,"used":used,"available":limit,"status":"unknown" if unused[label] is None else "exceeded" if unused[label]<0 else "within_declared_limit","limit_basis":"declared_input" if limit is not None else "not_declared"})
        if unused[label] is not None and unused[label] < 0: errors.append(f"{label} 使用 {used} 点，超过已确认可用 {limit} 点。")
        elif limit is None and used: needs("progress:" + label)
    guard = stats.get("GuardSkillActive")
    flasks = [item["slot"] for item in result.get("equipped",[]) if item["slot"].startswith("Flask ") and item.get("active")]
    if guard: needs("condition:guard")
    for slot in flasks: needs("condition:" + slot)
    active = [skill for skill in result.get("active_skills",[]) if not skill.get("disabled")]
    main = next((skill for skill in active if skill.get("is_main")),None)
    known_cost_keys={prefix+suffix for prefix in ("Mana","Life","ES") for suffix in ("Cost","PerSecondCost")}
    extra_costs=[]
    for subject,data in [("main",stats)]+[(skill.get("skill_ref",skill.get("id")),skill.get("costs",{})) for skill in active]:
        for key,value in data.items():
            if key.endswith("Cost") and key not in known_cost_keys and type(value) in (int,float) and value>0:
                extra_costs.append({"subject":subject,"metric":key,"value":value,"status":"native_observed","scope":"当前三资源账目未覆盖此项，不按免费处理。"})
                pending.append("capability:resource_cost:"+str(subject)+":"+key)
    automatic = [skill for skill in active if not skill.get("is_main") and skill.get("trigger") and (number(skill.get("costs",{}),"SkillTriggerRate") or 0) > 0]
    usage = plan.get("usage",[])
    by_ref = {skill.get("skill_ref"):skill for skill in active if skill.get("skill_ref")}
    usages = {}
    diagnostics=[]
    for index,row in enumerate(usage):
        ref = row.get("skill_ref")
        bx.require(ref in by_ref and ref not in usages,"usage.skill_ref 需要本次原生返回的唯一技能引用。",location=f"/usage/{index}/skill_ref",expected=list(by_ref),actual=ref)
        usages[ref] = row
        rate=row.get("casts_per_second")
        limit=native_rate(by_ref[ref])
        if rate is not None and (limit is None or limit<=0):
            pending.append("capability:usage_frequency:"+ref)
        elif rate is not None and rate>limit+1e-8:
            message="使用频率超过本机当前技能频率："+ref
            errors.append(message)
            diagnostics.append({"code":"usage_frequency_exceeded","location":f"/usage/{index}/casts_per_second","subject":ref,"actual":rate,"native_limit":limit,"message":message,"status":"failed"})
        linked=next((c for c in claims if c['claim_id']==row.get('claim_id')),None)
        subject='skill:'+ref
        if not linked or subject not in linked['subjects'] or linked['status']!='model_assessed':
            needs(subject)
            diagnostics.append({'code':'usage_claim_missing' if not linked else 'usage_claim_subject_missing' if subject not in linked['subjects'] else 'usage_claim_unresolved',
                                'location':f'/usage/{index}/claim_id','actual':row.get('claim_id'),
                                'expected':[c['claim_id'] for c in claims if subject in c['subjects']],
                                'required_subject':subject,'action':'link_claim','message':'关联本次实际存在、覆盖此技能并已评审的判断；按当前事实修正文句或对象关系后重算。'})
    for skill in active:
        ref = skill.get("skill_ref") or skill.get("id")
        if skill.get("generals_cry"): needs("dependency:" + ref)
        if skill is not main and skill not in automatic and any((number(skill.get("costs",{}),key) or 0)>0 for key in known_cost_keys) and ref not in usages:
            pending.append("usage:" + ref)
    for gem in result.get("gems",[]):
        if gem.get("enabled") and gem.get("group_enabled") and (gem.get("count",1)>1 or gem.get("group_count",1)>1): needs(f"count:{gem['group']}:{gem['index']}")
    eb = result.get("resource_rules",{}).get("energy_shield_protects_mana")
    main_usage = usages.get(main.get("skill_ref") if main else None,{})
    flask_facts=[{k:item[k] for k in ("slot","id","flask_base","flask_local","flask_local_source","flask_scope") if k in item} for item in result.get("equipped",[]) if item["slot"] in flasks]
    if any(row.get("continuous") is True for row in usage):
        pending += ["capability:flask_coverage:"+slot for slot in flasks]
    for resource, cost_key, label in (("Mana","Mana","魔力"),("Life","Life","生命"),("EnergyShield","ES","能量护盾")):
        single, native_cost = payment(stats,cost_key,eb,main and main.get("no_cost") is True), number(stats,cost_key+"PerSecondCost")
        raw_native_cost=native_cost
        if main and main.get("no_cost") is True:
            if single is None: single=0
            if native_cost is None: native_cost=0
        pool = available_pool(stats,resource)
        raw_regen, degen = number(stats,resource+"RegenRecovery"), number(stats,resource+"Degen")
        regen = fixed_net_regeneration(stats,resource)
        native_leech = number(stats,resource+"LeechGainRate")
        leech = 0 if main and main.get("actor") in {"mirage","minion"} else native_leech
        automatic_values = [number(skill.get("costs",{}),cost_key+"PerSecondCost") for skill in automatic]
        auto_cost = sum(automatic_values) if all(value is not None for value in automatic_values) else None
        cost = native_cost + auto_cost if native_cost is not None and auto_cost is not None else None
        single_values = [single] + [payment(skill.get("costs",{}),cost_key,eb,skill.get("no_cost") is True) for skill in automatic]
        single = max(single_values) if all(value is not None for value in single_values) else None
        # A paced main skill is projected only when native cost agrees with single-cost × rate.
        projection = "native_full_speed"
        rate = main_usage.get("casts_per_second")
        if rate is not None and main:
            main_rate=native_rate(main)
            main_single=payment(main.get("costs",{}),cost_key,eb,main.get("no_cost") is True)
            if main_rate is None or main_rate<=0 or native_cost is None or main_single is None or not math.isclose(native_cost,main_single*main_rate,rel_tol=1e-5,abs_tol=1e-5) or automatic:
                pending.append("capability:usage_projection:"+main["skill_ref"])
                projection="unsupported"
            elif rate>main_rate+1e-8:
                projection="unsupported"
            else:
                cost=main_single*rate
                projection="linear_native_cost_only"
        main_projected=cost-auto_cost if cost is not None and auto_cost is not None else None
        contributors=[main_usage] if main_projected and main_projected>0 else []
        if auto_cost and auto_cost>0: contributors.append(main_usage)
        continuous_cost=sum(value or 0 for value,skill in zip(automatic_values,automatic) if usages.get(skill.get("skill_ref"),main_usage).get("continuous") is True)
        if main_projected and main_usage.get("continuous") is True: continuous_cost+=main_projected
        manual_cost=0
        # A timeline already defines these payments; usage then only links claims.
        for ref,row in (usages if "timeline" not in plan else {}).items():
            skill=by_ref[ref]
            if skill is main or skill in automatic: continue
            value=payment(skill.get("costs",{}),cost_key,eb,skill.get("no_cost") is True)
            if value is None or row.get("casts_per_second") is None:
                manual_cost=None
                pending.append("capability:usage_projection:"+ref)
                break
            full=number(skill.get("costs",{}),cost_key+"PerSecondCost")
            skill_rate=native_rate(skill)
            if full is None or skill_rate is None or not math.isclose(full,value*skill_rate,rel_tol=1e-5,abs_tol=1e-5):
                pending.append("capability:usage_projection:"+ref)
                manual_cost=None
                break
            projected=value*row["casts_per_second"]
            manual_cost+=projected
            if projected>0:
                contributors.append(row)
                single=max(single,value) if single is not None else None
                if row.get("continuous") is True: continuous_cost+=projected
        if cost is not None and manual_cost is not None: cost+=manual_cost
        elif manual_cost is None: cost=None
        subject="resource:"+resource
        if single is not None and pool is not None and single>0 and (single>pool or resource=="Life" and single>=pool):
            errors.append(f"可用{label} {pool:g} 不能支付单次消耗 {single:g}。")
        recharge=number(stats,"EnergyShieldRecharge") if resource=="EnergyShield" else None
        recharge_counted=0
        if recharge and facts.has_claim(claims,"resource:EnergyShield:recharge"):
            recharge_counted=recharge
            delay=number(stats,"EnergyShieldRechargeDelay")
            if delay is None: pending.append("native:resource:EnergyShield:delay")
            elif None not in (cost,regen,leech,pool) and max(cost-regen-leech,0)*delay>pool:
                pending.append("resource:EnergyShield:startup")
            warnings.append("能盾充能仅在对应 claim 的启动/打断条件下计入；不是持续覆盖证明。")
        if projection == "linear_native_cost_only" and leech and rate < main_rate:
            leech = None
            pending.append("capability:"+subject+":paced_leech")
        known=all(value is not None for value in (pool,single,cost,regen,leech))
        net=cost-regen-leech-recharge_counted if known else None
        record={"resource":label,"resource_id":resource,"pool":pool,"single_cost":single,"native_cost_per_second":raw_native_cost,"cost_per_second":cost,
                "main_cost_scope":"not_applicable_by_native_flag" if main and main.get("no_cost") is True else "native_value_or_unknown",
                "automatic_cost_per_second":auto_cost,"manual_cost_per_second":manual_cost,"regeneration":regen,"raw_regeneration":raw_regen,"degen":degen,
                "native_leech_on_hit":native_leech,"leech_on_hit":leech,"recharge":recharge,"recharge_counted":recharge_counted,"net_cost":net,
                "snapshot_balance_nonnegative":net<=0.01 if net is not None else None,"projection":projection,"scope":"所列原生/声明频率及假设回复的快照账目；非吸取启动、任意时点支付或持续覆盖证明。DPS 未按使用节奏重算。"}
        if not known: pending.append("native:"+subject)
        elif net>0.01 and "timeline" not in plan:
            maximum=max(pool-(1 if resource=="Life" else 0),0)/net
            record["maximum_burst_seconds"]=maximum
            window,mixed=timing(contributors or [main_usage])
            duration=window.get("active_seconds")
            if continuous_cost-regen-leech-recharge_counted>0.01: errors.append(f"{label}净缺口使声明的持续技能无法无限使用。")
            elif mixed:
                pending.append("capability:"+subject+":timing")
                record["timing_scope"]="不同技能的使用/恢复窗口未声明共同时间安排；不借用主技能窗口放行副技能。"
            elif window.get("continuous") is True: errors.append(f"{label}净缺口 {net:g}/秒，不满足声明的持续使用。")
            elif duration is not None:
                if duration>maximum: errors.append(f"声明的使用时长超过{label}静态储量可支持的 {maximum:g} 秒。")
                recovery=net*duration/regen if regen>0 else None
                record["recovery_seconds_at_regen_only"]=recovery
                if recovery is None: pending.append("capability:"+subject+":recovery")
                elif window.get("recovery_seconds") is None: pending.append(subject+":recovery")
                elif window["recovery_seconds"]<recovery: errors.append(f"{label}声明恢复时间小于净秒回所需时间 {recovery:g} 秒。")
            else: pending.append(subject+":usage")
            warnings.append(f"{label}当前使用方式有净缺口 {net:g}/秒；不能据此声称无限持续。")
        if leech and leech>0: needs(subject+":leech")
        resources.append(record)
    timeline=None
    if "timeline" in plan:
        timeline=timeline_assessment(result,plan["timeline"],usage)
        errors+=timeline["errors"];pending+=timeline["pending"];diagnostics+=timeline["diagnostics"]
        pending=[p for p in pending if not p.startswith("native:resource:") or next((r["status"] for r in timeline["resources"] if "native:resource:"+r["resource_id"]==p),"unknown")=="unknown"]
        for row in timeline["resources"]:
            if row["status"]!="unknown":
                target=next(r for r in resources if r["resource_id"]==row["resource_id"])
                target["timeline_status"]=row["status"];target["timeline_initial_pool_restored"]=row["initial_pool_restored"]
        for index,row in enumerate(usage):
            phase=row.get("phase","combat")
            window=timeline.get("preparation",{}) if phase=="preparation" else timeline
            schedule=window.get("skill_schedules",{}).get(row["skill_ref"])
            if schedule is None: continue
            linked=next((claim for claim in claims if claim["claim_id"]==row.get("claim_id")),{})
            schedule.update(claim_id=row.get("claim_id"),objective_ids=[subject[len("objective:"):] for subject in linked.get("subjects",[]) if subject.startswith("objective:")])
            diagnostic=next((d for d in diagnostics if d["location"]==f"/usage/{index}/claim_id" and d["code"].startswith("usage_claim_")),None)
            if diagnostic is not None:
                pointer="/timeline"+("/preparation" if phase=="preparation" else "")+"/skill_schedules/"+row["skill_ref"].replace("~","~0").replace("/","~1")
                diagnostic["payment_schedule"]={**schedule,"phase":phase,"duration_seconds":window["duration_seconds"],"repeat":window["repeat"],
                    "reference":"native:"+reference_name+"#"+pointer,"window_payment_status":window["status"],
                    "scope":capabilities()["timeline"]["event_semantics"]+" 所列支付不认证动作实际执行、效果持续或关联目标达成。"}
    payments=single_payments(result)
    for row in payments:
        if row["status"]=="refuted":
            message=f"技能 {row['skill_ref']} 的 {row['resource_id']} 单次支付 {row['payment']:g} 超出可用储量 {row['pool']:g}（生命须支付后存活）。"
            errors.append(message);diagnostics.append({"code":"single_payment_unaffordable","location":row["cost_source"]["skill_pointer"],"subject":row["skill_ref"],"resource_id":row["resource_id"],"expected":{"pool":row["pool"],"life_must_remain_positive":row["resource_id"]=="Life"},"actual":row["payment"],"message":message,"action":"inspect_error"})
        elif row["status"]=="unknown": pending.append("native:single_payment:"+str(row["skill_ref"])+":"+row["resource_id"])
    finite=timeline is not None and not timeline["repeat"]
    execution={"status":"unknown","scope":"未测得整套动作实际可执行；逐次支付、同技能间隔与跨技能动作互斥、触发实现、效果覆盖分别判断。",
               "payment_schedule_status":timeline["status"] if timeline else "not_requested",
               "cross_skill_action_exclusion":"unmeasured","trigger_realization":"unmeasured","effect_coverage":"unmeasured"}
    sustained={"status":timeline["status"] if timeline and (not finite or timeline["status"]=="refuted") else "unknown",
               "basis":"finite_window_only" if finite else "discrete_timeline_at_native_net_regen" if timeline else "no_common_timeline",
               "scope":"本次仅检查所列有限窗口；没有核对重复接缝，窗口支付通过不证明持续重复。" if finite else capabilities()["timeline"]["scope"] if timeline else "仅有资源快照与独立支付事实；连续动作、启动/中断和效果覆盖未测得。"}
    unscheduled=[row for row in diagnostics if row.get("code")=="timeline_skill_unscheduled"]
    for row in unscheduled:
        row["affected_subjects"]=["resource:"+row["resource_id"],"usage:"+row["skill_ref"]]
        linked=[claim for claim in claims or [] if any(subject==required or subject.startswith(required+":") for subject in claim.get("subjects",[]) for required in row["affected_subjects"])]
        row["affected_claim_ids"]=[claim["claim_id"] for claim in linked]
        row["affected_objective_ids"]=sorted({subject[len("objective:"):] for claim in linked for subject in claim.get("subjects",[]) if subject.startswith("objective:")})
    return {"errors":list(dict.fromkeys(errors)),"warnings":list(dict.fromkeys(warnings)),"pending":list(dict.fromkeys(pending)),"unused_points":unused,"point_budgets":point_budgets,"timeline":timeline,"single_payments":payments,"sustained_use":sustained,"execution_coverage":execution,"unscheduled_skills":unscheduled,
            "conditions":{"guard_active":guard,"active_flasks":flasks,"flask_facts":flask_facts,"config_values":result.get("config_values",{}),"scope":"实际原生配置；效果覆盖由具体 claims 说明。药剂局部数据不证明持续补充，无持续覆盖测量时保留 capability 缺口。"},"resources":resources,"additional_native_costs":extra_costs,"diagnostics":diagnostics}
