"""Shared native resource and usage audit for build and route calculations."""
import math
import buildxml as bx
import facts


def number(data, key):
    value=data.get(key)
    return value if type(value) in (int,float) and math.isfinite(value) else None


def input_shape(document, *, complete=True):
    """Check declared schedules without querying the native engine."""
    usage=document.get("usage",[])
    bx.require(isinstance(usage,list),"usage 应为数组。",location="/usage",expected="array",actual=type(usage).__name__)
    seen=set()
    for index,row in enumerate(usage):
        path=f"/usage/{index}"
        bx.require_keys(row,{"skill_ref","casts_per_second","active_seconds","recovery_seconds","continuous","claim_id"},path,"usage 字段无效。")
        for key in ("skill_ref","claim_id"):
            if key in row or complete and key=="skill_ref": bx.require(isinstance(row.get(key),str) and row[key].strip(),key+" 应为非空引用。",location=path+"/"+key,expected="nonempty string",actual=row.get(key))
        ref=row.get("skill_ref")
        bx.require(ref is None or ref not in seen,"usage.skill_ref 不能重复。",location=path+"/skill_ref",expected="unique reference",actual=ref);seen.add(ref)
        for key in ("casts_per_second","active_seconds","recovery_seconds"):
            if key in row: bx.require(number(row,key) is not None and row[key]>=0,key+" 需要有限非负数。",location=path+"/"+key,expected="finite nonnegative number",actual=row[key])
        if "continuous" in row: bx.require(type(row["continuous"]) is bool,"usage.continuous 应为布尔值。",location=path+"/continuous",expected="boolean",actual=row["continuous"])
        if "timeline" in document: bx.require(not set(row)&{"casts_per_second","active_seconds","recovery_seconds","continuous"},"共同时间线已定义频率/窗口；usage 只关联技能和判断。",location=path,expected=["skill_ref","claim_id"],actual=sorted(row))
    if "timeline" not in document: return
    timeline=document["timeline"]
    bx.require_keys(timeline,{"duration_seconds","events"},"/timeline")
    duration=number(timeline,"duration_seconds")
    if complete or "duration_seconds" in timeline: bx.require(duration is not None and duration>0,"时间线需要有限正周期。",location="/timeline/duration_seconds",expected="positive seconds",actual=timeline.get("duration_seconds"))
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


def payment(data, cost_key, shield_covers_mana, no_cost=False):
    value=number(data,cost_key+"Cost")
    if no_cost and value is None: value=0
    if shield_covers_mana:
        if cost_key=="Mana": return 0
        if cost_key=="ES":
            mana=number(data,"ManaCost")
            if no_cost and mana is None: mana=0
            return value+mana if value is not None and mana is not None else None
    return value


def native_rate(skill):
    observed=skill.get("usage_rate",{})
    value=number(observed,"value")
    if value is not None: return value
    costs=skill.get("costs",{})
    return number(costs,"SkillTriggerRate") or number(costs,"Speed")


def timing(rows):
    """A shared window is computable; independent schedules remain explicit gaps."""
    if not rows: return {},False
    windows={(row.get("continuous"),row.get("active_seconds"),row.get("recovery_seconds")) for row in rows}
    return (rows[0],False) if len(windows)==1 else ({},True)


def capabilities():
    """Describe the projection this adapter actually implements."""
    return {"timeline":{"supported":["discrete_native_skill_payments","constant_native_net_regeneration"],
            "excluded_recovery":["leech","recharge","flask"],
            "unsupported":["action_exclusion","changing_conditions","non_discrete_payments","automatic_cost_schedules"],
            "event_reference":"current active_skills[].skill_ref",
            "scope":"当前配置固定净回复下的离散费用时间线；不含命中吸取、充能、药剂补充、动作互斥或条件效果变化，也不改变原生 DPS。"}}


def timeline_assessment(result, timeline):
    """Replay discrete payments on one clock using constant native net regen.

    This bounded projection excludes leech, recharge and flask refills. Channels
    and automatic effects that cannot be reduced to discrete payments stay unknown.
    """
    input_shape({"timeline":timeline})
    duration=number(timeline,"duration_seconds")
    events=timeline.get("events")
    skills={s["skill_ref"]:s for s in result.get("active_skills",[]) if s.get("skill_ref") and not s.get("disabled")}
    rows=[]; diagnostics=[]; pending=[]; errors=[]; previous={}
    for index,event in enumerate(events):
        path=f"/timeline/events/{index}"
        ref,at=event.get("skill_ref"),number(event,"at")
        bx.require(ref in skills,"时间线动作使用当前原生 skill_ref。",location=path+"/skill_ref",expected=list(skills),actual=ref)
        rows.append({**event,"source_index":index})
    rows.sort(key=lambda row:(row["at"],row["source_index"]))
    for event in rows:
        ref,at=event["skill_ref"],event["at"]
        rate=native_rate(skills[ref])
        if skills[ref].get("trigger"):
            pending.append("capability:timeline:trigger:"+ref)
        elif rate is None or rate<=0: pending.append("capability:timeline:frequency:"+ref)
        elif ref in previous and at-previous[ref]<1/rate-1e-8:
            message="时间线同技能动作间隔短于当前原生频率："+ref
            errors.append(message);diagnostics.append({"code":"timeline_frequency_exceeded","location":f"/timeline/events/{event['source_index']}/at","expected":{"minimum_interval_seconds":1/rate},"actual":at-previous[ref],"message":message,"action":"edit_input"})
        previous[ref]=at
    # The wraparound interval is also part of a claimed repeating cycle.
    for ref,last in previous.items():
        first=next(e["at"] for e in rows if e["skill_ref"]==ref);rate=native_rate(skills[ref])
        if rate and rate>0 and duration-last+first<1/rate-1e-8:
            errors.append("时间线重复周期的同技能间隔不足："+ref)
    eb=result.get("resource_rules",{}).get("energy_shield_protects_mana")
    records=[]
    for resource,cost_key in (("Mana","Mana"),("Life","Life"),("EnergyShield","ES")):
        stats=result["stats"]
        pool=number(stats,resource+"Unreserved")
        if pool is None: pool=number(stats,resource)
        if resource=="EnergyShield" and pool is not None and number(stats,"EnergyShieldRecoveryCap") is not None: pool=min(pool,stats["EnergyShieldRecoveryCap"])
        regen=number(stats,"Net"+resource+"Regen")
        if regen is None and number(stats,resource+"RegenRecovery") is not None and number(stats,resource+"Degen") is not None: regen=stats[resource+"RegenRecovery"]-stats[resource+"Degen"]
        payments={ref:payment(s.get("costs",{}),cost_key,eb,s.get("no_cost") is True) for ref,s in skills.items()}
        gaps=[]
        for ref,skill in skills.items():
            single=payments[ref];full=number(skill.get("costs",{}),cost_key+"PerSecondCost");rate=native_rate(skill)
            if skill.get("no_cost") is True and full is None: full=0
            if single is not None and single<0 or full is not None and full<0: gaps.append("negative_payment:"+ref)
            if eb is None and resource in {"Mana","EnergyShield"} and (number(skill.get("costs",{}),"ManaCost") or 0)>0: gaps.append("payment_rule:"+ref)
            if skill.get("trigger") and full!=0: gaps.append("automatic:"+ref)
            if any(e["skill_ref"]==ref for e in rows) and (single is None or full is None or (single!=0 or full!=0) and (rate is None or not math.isclose(full,single*rate,rel_tol=1e-5,abs_tol=1e-5))): gaps.append("payment_projection:"+ref)
            if single and not any(e["skill_ref"]==ref for e in rows) and not skill.get("trigger"): gaps.append("unscheduled:"+ref)
        known=pool is not None and pool>=0 and regen is not None and not gaps
        record={"resource_id":resource,"duration_seconds":duration,"initial":pool,"net_regeneration_per_second":regen,
                "included_recovery":"native_net_regen_only","excluded_recovery":capabilities()["timeline"]["excluded_recovery"],"status":"unknown","trace":[],"repeatable":None,"gaps":gaps}
        if known:
            balance=pool;last=0;failed=False
            for event in rows:
                balance=min(pool,balance+regen*(event["at"]-last));before=balance
                cost=payments[event["skill_ref"]];balance-=cost;last=event["at"]
                affordable=before>=cost and (balance>0 if resource=="Life" else balance>=0)
                if not affordable: failed=True
                record["trace"].append({"at":last,"skill_ref":event["skill_ref"],"before":before,"payment":cost,"after":balance,"affordable":affordable})
            balance=min(pool,balance+regen*(duration-last))
            if (resource=="Life" and balance<=0) or (resource!="Life" and balance<0): failed=True
            record.update(final=balance,status="refuted" if failed else "supported",repeatable=not failed and balance>=pool-1e-8)
            if failed: errors.append("共同时间线中存在无法支付或耗尽的资源："+resource)
            elif not record["repeatable"]: pending.append("resource:"+resource+":timeline_repeat")
        else: pending.append("capability:timeline:"+resource)
        records.append(record)
    return {"status":"refuted" if errors else "unknown" if pending else "supported","duration_seconds":duration,"events":rows,"resources":records,"errors":errors,"pending":pending,"diagnostics":diagnostics,
            "scope":capabilities()["timeline"]["scope"]}


def assess(result, intent, plan, claims=None):
    """Report observed constraints and scoped usage, not a universal play style."""
    input_shape(plan)
    claims = claims or []
    errors, warnings, pending, resources = [], [], [], []
    stats = result["stats"]
    def needs(subject):
        if not facts.has_claim(claims,subject): pending.append(subject)
    unused = {}
    for label, used_key, limit_key in (("normal","points","normal_points_available"),("ascendancy","ascendancy_points","ascendancy_points"),("secondary_ascendancy","secondary_ascendancy_points","secondary_ascendancy_points")):
        used, limit = result.get(used_key), intent.get(limit_key)
        unused[label] = limit - used if type(limit) is int and type(used) is int else None
        if unused[label] is not None and unused[label] < 0: errors.append(f"{label} 使用 {used} 点，超过已确认可用 {limit} 点。")
        elif limit is None and used: needs("progress:" + label)
    guard = stats.get("GuardSkillActive")
    flasks = [item["slot"] for item in result.get("equipped",[]) if item["slot"].startswith("Flask ") and item.get("active")]
    if guard: needs("condition:guard")
    for slot in flasks: needs("condition:" + slot)
    for key in plan.get("operations",{}).get("config",{}): needs("config:" + key)
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
        if not any(c["claim_id"]==row.get("claim_id") and c["status"]=="model_assessed" and "skill:"+ref in c["subjects"] for c in claims): needs("skill:"+ref)
    for skill in active:
        ref = skill.get("skill_ref") or skill.get("id")
        if skill.get("generals_cry"): needs("dependency:" + ref)
        if skill is not main and skill not in automatic and any((number(skill.get("costs",{}),key) or 0)>0 for key in known_cost_keys) and ref not in usages:
            pending.append("usage:" + ref)
    for gem in result.get("gems",[]):
        if gem.get("enabled") and gem.get("group_enabled") and (gem.get("count",1)>1 or gem.get("group_count",1)>1): needs(f"count:{gem['group']}:{gem['index']}")
    eb = result.get("resource_rules",{}).get("energy_shield_protects_mana")
    main_usage = usages.get(main.get("skill_ref") if main else None,{})
    flask_facts=[{k:item[k] for k in ("slot","id","flask_local","flask_scope") if k in item} for item in result.get("equipped",[]) if item["slot"] in flasks]
    if any(row.get("continuous") is True for row in usage):
        pending += ["capability:flask_coverage:"+slot for slot in flasks]
    for resource, cost_key, label in (("Mana","Mana","魔力"),("Life","Life","生命"),("EnergyShield","ES","能量护盾")):
        single, native_cost = payment(stats,cost_key,eb,main and main.get("no_cost") is True), number(stats,cost_key+"PerSecondCost")
        raw_native_cost=native_cost
        if main and main.get("no_cost") is True:
            if single is None: single=0
            if native_cost is None: native_cost=0
        pool = number(stats,resource+"Unreserved")
        if pool is None: pool = number(stats,resource)
        raw_regen, degen = number(stats,resource+"RegenRecovery"), number(stats,resource+"Degen")
        regen = number(stats,"Net"+resource+"Regen")
        if regen is None and raw_regen is not None and degen is not None: regen = raw_regen-degen
        native_leech = number(stats,resource+"LeechGainRate")
        leech = 0 if main and main.get("actor") in {"mirage","minion"} else native_leech
        automatic_values = [number(skill.get("costs",{}),cost_key+"PerSecondCost") for skill in automatic]
        auto_cost = sum(automatic_values) if all(value is not None for value in automatic_values) else None
        cost = native_cost + auto_cost if native_cost is not None and auto_cost is not None else None
        single_values = [single] + [payment(skill.get("costs",{}),cost_key,eb,skill.get("no_cost") is True) for skill in automatic]
        single = max(single_values) if all(value is not None for value in single_values) else None
        if resource=="EnergyShield":
            cap=number(stats,"EnergyShieldRecoveryCap")
            if pool is not None and cap is not None: pool=min(pool,cap)
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
                "sustainable_while_hitting":net<=0.01 if net is not None else None,"projection":projection,"scope":"所列技能及条件下的支付账目；DPS 仍是原生快照，未按使用节奏重算的伤害不能当实际持续输出。"}
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
        timeline=timeline_assessment(result,plan["timeline"])
        errors+=timeline["errors"];pending+=timeline["pending"];diagnostics+=timeline["diagnostics"]
        pending=[p for p in pending if not p.startswith("native:resource:") or next((r["status"] for r in timeline["resources"] if "native:resource:"+r["resource_id"]==p),"unknown")=="unknown"]
        for row in timeline["resources"]:
            if row["status"]!="unknown":
                target=next(r for r in resources if r["resource_id"]==row["resource_id"])
                target["timeline_status"]=row["status"];target["repeatable_at_native_regen_only"]=row["repeatable"]
    return {"errors":list(dict.fromkeys(errors)),"warnings":list(dict.fromkeys(warnings)),"pending":list(dict.fromkeys(pending)),"unused_points":unused,"timeline":timeline,
            "conditions":{"guard_active":guard,"active_flasks":flasks,"flask_facts":flask_facts,"config_values":result.get("config_values",{}),"scope":"实际原生配置；效果覆盖由具体 claims 说明。药剂局部数据不证明持续补充，无持续覆盖测量时保留 capability 缺口。"},"resources":resources,"additional_native_costs":extra_costs,"diagnostics":diagnostics}
