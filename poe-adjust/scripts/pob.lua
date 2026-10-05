-- Adapter around the bundled upstream HeadlessWrapper, checked at runtime.
-- All application files are read-only. Inputs/outputs cross the Lua API in memory.
package.path = "./lua/?.lua;./lua/?/init.lua;" .. package.path
package.cpath = "./?.dll;" .. package.cpath
arg = {}
local originalOpen = io.open
io.open = function(path, mode)
  mode = mode or "r"
  if mode:find("[wa+]") then error("Engine file writes are disabled: " .. path) end
  if path:match("Settings%.xml$") or path == "first.run" or path == "installed.cfg" then return nil end
  return originalOpen(path, mode)
end
io.read = function() error("Unexpected interactive engine prompt") end
io.output = function() error("Engine file writes are disabled") end
io.popen = function() error("Engine subprocesses are disabled") end
os.execute = io.popen
os.remove = io.output
os.rename = io.output
os.exit = io.output
print = function() end

-- @LOAD_HEADLESS@
local json = require("dkjson")
local request = assert(json.decode(_REQUEST_JSON))
local function inputRule(condition, message, location, expected, actual, capability)
  if condition then return end
  _DIAGNOSTIC_JSON = json.encode({code = capability and "native_capability_unavailable" or "native_invalid_input",
    message = message, location = location, expected = expected, actual = actual,
    action = capability and "inspect_capability" or "edit_input", retryable = false})
  error(message)
end
local function calculateRequest(request)
_DIAGNOSTIC_JSON = nil
if launch.promptMsg then error(launch.promptMsg) end
if request.xml ~= "" then loadBuildFromXML(request.xml, "POE Adjust") end
if launch.promptMsg then error(launch.promptMsg) end
if request.class_id ~= nil then
  assert(request.xml == "", "Class-start query cannot replace an input build")
  assert(build.spec.tree.classes[request.class_id], "Unknown class ID in current tree: " .. tostring(request.class_id))
  build.spec:ResetNodes()
  build.spec:SelectClass(request.class_id)
  build.buildFlag = true
end
local hasTreeContext = request.xml ~= "" or request.class_id ~= nil
local function scalarTable(tbl)
  local values = {}
  for key, value in pairs(tbl or {}) do
    if type(value) == "number" and value == value and value ~= math.huge and value ~= -math.huge then values[key] = value
    elseif type(value) == "boolean" or type(value) == "string" then values[key] = value end
  end
  return values
end
-- Observe convergence within an engineering bound instead of assuming three frames.
local previous, stable, frames = nil, 0, 0
for i = 1, 12 do
  runCallback("OnFrame")
  local stats = scalarTable(build.calcsTab.mainOutput)
  local keys, parts = {}, {}
  for key in pairs(stats) do table.insert(keys, key) end
  table.sort(keys)
  for _, key in ipairs(keys) do table.insert(parts, key .. "=" .. tostring(stats[key])) end
  local signature = table.concat(parts, "|") .. ":" .. tostring(build.spec:CountAllocNodes()) .. ":" .. #build.skillsTab.socketGroupList
  stable = signature == previous and stable + 1 or 0
  previous, frames = signature, i
  if i >= 3 and stable >= 2 and not build.buildFlag then break end
end
assert(stable >= 2 and not build.buildFlag, "Native calculation did not converge within the frame bound")
local output = build.calcsTab.mainOutput or {}
local result = {stats = scalarTable(output), minion = scalarTable(output.Minion), warnings = {}, gems = {}, equipped = {}, nodes = {}, errors = {}, excluded_guard_checks = {}}
result.convergence = {stable_frames = stable + 1, frames = frames, scope = "Scalar output, point count, group count and native build flag"}
if output.Minion then
  result.stats.MinionCombinedDPS = output.Minion.CombinedDPS
  result.stats.MinionHitChance = output.Minion.HitChance
end
-- Read meaning from this installation's display definitions, not a seasonal
-- metric whitelist. Keep alternatives: duplicate labels can be conditional.
result.stat_metadata = setmetatable({}, {__jsontype = "object"})
local function addMetricMeaning(key, label, fmt, source, actor, multiplier)
  if type(key) ~= "string" or type(label) ~= "string" then return end
  local variants = result.stat_metadata[key]
  if not variants then variants = {}; result.stat_metadata[key] = variants end
  local entry = {label = label, format = fmt, source = source, actor = actor,
    display_multiplier = multiplier or 1, status = "native_display_observed"}
  for _, old in ipairs(variants) do
    if old.label == label and old.format == fmt and old.actor == actor and old.display_multiplier == entry.display_multiplier then return end
  end
  table.insert(variants, entry)
end
for _, list in ipairs({{build.displayStats, "player"}, {build.minionDisplayStats, "minion"}}) do
  for index, entry in ipairs(list[1] or {}) do
    if type(entry.stat) == "string" then
      local key = list[2] == "minion" and "Minion" .. entry.stat or entry.stat
      addMetricMeaning(key, entry.label, entry.fmt, "build." .. (list[2] == "minion" and "minionDisplayStats" or "displayStats") .. "/" .. index,
        list[2], entry.pc and 100 or 1)
    end
  end
end
result.resource_rules = {energy_shield_protects_mana = build.calcsTab.mainEnv.modDB:Flag(nil, "EnergyShieldProtectsMana") or false}
result.resource_cost_types = {}
for id, rule in pairs(data.costs or {}) do
  local entry = scalarTable(rule)
  entry.id = id
  table.insert(result.resource_cost_types, entry)
end
table.sort(result.resource_cost_types, function(a,b) return a.id < b.id end)
result.level = build.characterLevel
result.class = build.spec.curClassName
result.ascendancy = build.spec.curAscendClassName
result.class_id = build.spec.curClassId
result.ascendancy_id = build.spec.curAscendClassId
result.secondary_ascendancy_id = build.spec.curSecondaryAscendClassId
result.secondary_ascendancy = build.spec.curSecondaryAscendClassName
result.tree_version = build.spec.treeVersion
result.tree_context = {basis = request.xml ~= "" and "input_xml" or request.class_id ~= nil and "class_start" or "unspecified"}
if hasTreeContext then
  result.tree_context.class_id = build.spec.curClassId
  result.tree_context.class_name = build.spec.curClassName
  result.tree_context.start_node_id = build.spec.curClass.startNodeId
end
result.latest_tree_version = latestTreeVersion
result.matching_socket_quality_bonus = data.misc.MatchingSocketQualityBonus
result.capabilities = {
  matching_socket_quality = type(data.misc.MatchingSocketQualityBonus) == "number" and type(build.skillsTab.UpdateSocketGroups) == "function",
  native_resource_costs = type(GlobalCache.cachedData) == "table" and type(cacheSkillUUID) == "function",
  native_gem_requirements = type(data.gems) == "table",
  native_corpse_sources = SkillType.CreatesCorpse ~= nil,
  excluded_guard_validation = SkillType.Guard ~= nil,
  granted_passives = type(build.calcsTab.mainEnv.grantedPassives) == "table",
  equipment_slots = type(build.itemsTab.slots) == "table",
  calcs_breakdown = type(build.calcsTab.calcs.buildOutput) == "function",
  node_screening = type(build.calcsTab.GetNodeCalculator) == "function",
}
result.classes = {}
for id, class in pairs(build.spec.tree.classes) do
  local ascendancies = {}
  for ascendancyId, ascendancy in pairs(class.classes) do
    if ascendancyId > 0 then table.insert(ascendancies, {id = ascendancyId, name = ascendancy.name}) end
  end
  table.sort(ascendancies, function(a,b) return a.id < b.id end)
  table.insert(result.classes, {id = id, name = class.name, ascendancies = ascendancies})
end
table.sort(result.classes, function(a,b) return a.id < b.id end)
result.secondary_ascendancies = setmetatable({}, {__jsontype = "array"})
for id, entry in pairs(build.spec.tree.alternate_ascendancies or {}) do
  table.insert(result.secondary_ascendancies, {id = id, name = entry.name})
end
table.sort(result.secondary_ascendancies, function(a,b) return a.id < b.id end)
result.slots = {}
for name, slot in pairs(build.itemsTab.slots) do
  table.insert(result.slots, {name = name, weapon_set = slot.weaponSet, active = slot.weaponSet == nil or slot.weaponSet == (build.itemsTab.activeItemSet.useSecondWeaponSet and 2 or 1)})
end
table.sort(result.slots, function(a,b) return a.name < b.name end)
result.rules = {
  {id = "character_level", status = "observed", meaning = "engine_state", value = result.level, source = "build.characterLevel", scope = "Loaded value, not a game-level upper bound"},
  {id = "quest_passive_points", status = "unsupported", meaning = "game_limit", reason = "No structured reward-limit interface; use player progress and applicable sources"},
  {id = "ascendancy_points", status = "unsupported", meaning = "game_limit", reason = "CountAllocNodes gives usage, not completed trial rewards"},
  {id = "gem_base_quality_limit", status = "unsupported", meaning = "game_limit", reason = "Native calculation accepts quality input; it does not prove attainable gem quality"},
  {id = "gem_base_level", status = "observed", meaning = "native_fact", source = "data.gems.naturalMaxLevel", scope = "Natural trainable levels; corruption, variants and effective levels are separate"},
  {id = "socket_quality", status = result.capabilities.matching_socket_quality and "observed" or "unsupported", meaning = "native_fact", value = result.matching_socket_quality_bonus, source = "data.misc.MatchingSocketQualityBonus"},
}
local function socketCategory(base)
  local weapon = data.weaponTypeInfo[base.type]
  if not weapon then return base.type end
  if weapon.oneHand then return "one_hand" end
  if weapon.flag == "Bow" then return "bow" end
  if weapon.twoHand then return "two_hand" end
  return "unknown:" .. tostring(base.type)
end
result.socket_limits = {}
for _, base in pairs(data.itemBases) do
  if type(base.socketLimit) == "number" then
    local category = socketCategory(base)
    local limits = result.socket_limits[category]
    if not limits then
      limits = {min = base.socketLimit, max = base.socketLimit, scope = "Aggregate capacity only; choose a base using its current requirements and acquisition evidence"}
      result.socket_limits[category] = limits
    end
    limits.min = math.min(limits.min, base.socketLimit)
    limits.max = math.max(limits.max, base.socketLimit)
  end
end
result.main_group = build.mainSocketGroup
result.main_skill = build.calcsTab.mainEnv.player.mainSkill.activeEffect.grantedEffect.id
result.main_skill_name = build.calcsTab.mainEnv.player.mainSkill.activeEffect.grantedEffect.name
-- Keep the native Full DPS contributors and each active skill's operating context.
-- A scalar-only output loses this distinction for minions, mirages and triggers.
result.damage_sources, result.active_skills = {}, {}
for _, row in ipairs(output.SkillDPS or {}) do table.insert(result.damage_sources, scalarTable(row)) end
local mainEnv = build.calcsTab.mainEnv
result.granted_passives = setmetatable({}, {__jsontype = "array"})
for id in pairs(mainEnv.grantedPassives or {}) do
  local node = mainEnv.allocNodes and mainEnv.allocNodes[id]
  if node then
    table.insert(result.granted_passives, {id = id, name = node.name, stats = node.sd, allocated_on_tree = build.spec.allocNodes[id] ~= nil})
  else result.capabilities.granted_passives = false end
end
table.sort(result.granted_passives, function(a, b) return a.id < b.id end)
local costKeys = {"ManaCost", "LifeCost", "ESCost", "ManaPerSecondCost", "LifePerSecondCost", "ESPerSecondCost", "Cooldown", "Speed", "SkillTriggerRate", "Repeats", "TotemPlacementSpeed", "TrapThrowingSpeed", "MineLayingSpeed"}
local function supportLevelLimit(effect)
  if not effect or not effect.grantedEffect then return nil end
  for index, stat in ipairs(effect.grantedEffect.stats or {}) do
    if stat == "local_support_gem_max_skill_level_requirement_to_support" then
      local values = effect.grantedEffect.levels[effect.level]
      return assert(values and values[index], "Native trigger support level limit is unavailable")
    end
  end
end
for _, skill in ipairs(mainEnv.player.activeSkillList) do
  local cache = GlobalCache.cachedData and GlobalCache.cachedData[mainEnv.mode]
  local cached = cache and type(cacheSkillUUID) == "function" and cache[cacheSkillUUID(skill, mainEnv)]
  local computed = cached and cached.Env.player.mainSkill or skill
  local costs = {}
  for _, key in ipairs(costKeys) do costs[key] = cached and cached.Env.player.output[key] or nil end
  for key, value in pairs(cached and cached.Env.player.output or {}) do
    if type(key) == "string" and key:match("Cost$") and type(value) == "number" then costs[key] = value end
  end
  local noCost = computed.skillModList and computed.skillModList:Flag(computed.skillCfg, "HasNoCost")
  local groupIndex
  for index, group in ipairs(build.skillsTab.socketGroupList) do if skill.socketGroup == group then groupIndex = index; break end end
  local limit = supportLevelLimit(computed.triggeredBy)
  local instance = skill.activeEffect.srcInstance
  local gemIndex
  for index, gem in ipairs(skill.socketGroup and skill.socketGroup.gemList or {}) do if gem == instance then gemIndex = index; break end end
  local actor = computed.skillData.triggeredByGeneralsCry and "mirage" or computed.skillFlags.minion and "minion" or "player"
  local nativeOutput = cached and cached.Env.player.output or {}
  local rateKey = computed.skillFlags.trap and "TrapThrowingSpeed" or computed.skillFlags.mine and "MineLayingSpeed" or computed.skillFlags.totem and "TotemPlacementSpeed" or "Speed"
  local rate = nativeOutput.SkillTriggerRate or nativeOutput[rateKey]
  local repeats = nativeOutput.Repeats or 1
  if type(rate) == "number" and type(repeats) == "number" and repeats > 0 then rate = rate / repeats else rate = nil end
  local requiredLevel = limit and instance and instance.gemData and assert(instance.reqLevel, "Native trigger target requirement is unavailable")
  table.insert(result.active_skills, {id = skill.activeEffect.grantedEffect.id, name = skill.activeEffect.grantedEffect.name,
    group = groupIndex, index = gemIndex, slot = skill.socketGroup and skill.socketGroup.slot,
    skill_ref = tostring(groupIndex)..":"..tostring(gemIndex)..":"..skill.activeEffect.grantedEffect.id..":"..actor..":"..tostring(skill.socketGroup and skill.socketGroup.source or "gem")..":"..tostring(build.itemsTab.activeItemSet.useSecondWeaponSet and 2 or 1),
    actor = actor, cost_scope = "Native player output for this skill; actor recovery attribution must be respected",
    no_cost = noCost, no_cost_source = "Native skillModList:Flag(skillCfg, HasNoCost)",
    is_main = skill == mainEnv.player.mainSkill, disabled = computed.skillFlags.disable or false,
    minion = computed.skillFlags.minion or false, trigger = computed.infoTrigger,
    creates_corpses = SkillType.CreatesCorpse ~= nil and skill.activeEffect.grantedEffect.skillTypes[SkillType.CreatesCorpse] or false,
    trigger_level_limit = limit, trigger_target_required_level = requiredLevel,
    info = computed.infoMessage, info_secondary = computed.infoMessage2,
    generals_cry = computed.skillData.triggeredByGeneralsCry or false,
    usage_rate = {value = rate, metric = nativeOutput.SkillTriggerRate and "SkillTriggerRate" or rateKey, repeats = repeats, scope = "Native action frequency; cost projection additionally checks the full native per-second cost."},
    costs = scalarTable(costs)})
end
result.use_second_weapon_set = build.itemsTab.activeItemSet.useSecondWeaponSet
local configOptions = LoadModule("Modules/ConfigOptions")
result.config_keys, result.config_lists = {}, {}
result.config_values = scalarTable(mainEnv.configInput)
result.config_defaults = setmetatable({}, {__jsontype = "object"})
result.config_saved_values = setmetatable({}, {__jsontype = "object"})
assert(type(build.configTab.GetDefaultState) == "function", "Native config save defaults are unavailable")
for key, value in pairs(result.config_values) do
  local default = build.configTab:GetDefaultState(key, type(value))
  result.config_defaults[key] = default
  if value ~= default then result.config_saved_values[key] = value end
end
result.config_labels = {}
for _, entry in ipairs(configOptions) do
  if entry.var then
    table.insert(result.config_keys, entry.var)
    if entry.type == "list" then
      local values = setmetatable({}, {__jsontype = "array"})
      if type(entry.list) == "table" then
        for _, option in ipairs(entry.list) do
          assert(type(option.val) == "string" or type(option.val) == "number" or type(option.val) == "boolean", "Unsupported native config option: " .. entry.var)
          table.insert(values, option.val)
          if option.val == result.config_values[entry.var] then result.config_labels[entry.var] = option.label end
        end
      end
      result.config_lists[entry.var] = values
    end
  end
end
result.points, result.ascendancy_points, result.secondary_ascendancy_points = build.spec:CountAllocNodes()
for _, line in ipairs(build.controls.warnings.lines or {}) do table.insert(result.warnings, line) end
for id, node in pairs(build.spec.allocNodes) do table.insert(result.nodes, id) end
table.sort(result.nodes)
for groupIndex, group in ipairs(build.skillsTab.socketGroupList) do
  for gemIndex, gem in ipairs(group.gemList) do
    local grantedEffect = gem.grantedEffect or (gem.gemData and gem.gemData.grantedEffect)
    if (group.enabled or groupIndex == result.main_group) and group.slotEnabled and gem.enabled then
      for effectIndex, effect in ipairs(gem.gemData and gem.gemData.grantedEffectList or {grantedEffect}) do
        if effect.skillTypes and effect.skillTypes[SkillType.Guard] and effect.hasGlobalEffect and not gem["enableGlobal"..effectIndex] then
          table.insert(result.excluded_guard_checks, {group = groupIndex, index = gemIndex, effect = effectIndex, skill_id = effect.id})
        end
      end
    end
    local supported = {}
    for activeGem, _ in pairs(gem.supportEffect and gem.supportEffect.isSupporting or {}) do
      table.insert(supported, activeGem.skillId or activeGem.nameSpec)
    end
    table.sort(supported)
    table.insert(result.gems, {group = groupIndex, index = gemIndex, id = gem.skillId, name = gem.nameSpec,
      enabled = gem.enabled, group_enabled = group.enabled, slot = group.slot, source = group.source, level = gem.level, quality = gem.quality,
      count = gem.count or 1, group_count = group.groupCount or 1,
      skill_part = gem.skillPart, skill_stage_count = gem.skillStageCount, skill_minion = gem.skillMinion,
      -- SkillsTab.Load uses first active and false when these optional fields
      -- are absent (new granted groups); normalize that native save contract.
      main_active_skill = group.mainActiveSkill or 1, include_in_full_dps = group.includeInFullDPS == true,
      natural_max_level = gem.gemData and gem.gemData.naturalMaxLevel,
      native_level_present = grantedEffect and grantedEffect.levels and grantedEffect.levels[gem.level] ~= nil,
      color = grantedEffect and ({"R", "G", "B", "W"})[grantedEffect.color] or "unknown",
      support = grantedEffect and grantedEffect.support or false,
      supports_skills = supported, support_applied = #supported > 0,
      matches_socket = gem.matchesSocket or false,
      socket_quality_bonus = result.capabilities.matching_socket_quality and (gem.matchesSocket and result.matching_socket_quality_bonus or 0) or nil,
      recognised = gem.gemData ~= nil or gem.grantedEffect ~= nil,
      req_level = gem.reqLevel, req_str = gem.reqStr, req_dex = gem.reqDex, req_int = gem.reqInt})
  end
end
for name, slot in pairs(build.itemsTab.slots) do
  local item = build.itemsTab.items[slot.selItemId]
  if item then
    local unparsed = {}
    for _, field in ipairs({"implicitModLines", "explicitModLines", "enchantModLines", "scourgeModLines", "crucibleModLines"}) do
      for _, mod in ipairs(item[field] or {}) do
        if mod.extra or not mod.modList then table.insert(unparsed, mod.line) end
      end
    end
    table.insert(result.equipped, {slot = name, id = slot.selItemId, name = item.name, base = item.baseName, base_recognised = item.base ~= nil,
      active = slot.active, sockets = item.sockets, weapon_set = slot.weaponSet,
      valid_for_slot = build.itemsTab:IsItemValidForSlot(item, name) == true,
      socket_colours_always_match = item.sockets and item.sockets.colourAlwaysMatches or false,
      requirements = item.requirements, unparsed = unparsed,
      flask_local = item.flaskData and scalarTable(item.flaskData),
      flask_scope = item.flaskData and "Item-local values; excludes character modifiers, current charges, refill events and actual effect coverage"})
  end
end
table.sort(result.equipped, function(a, b) return a.slot < b.slot end)
local function ordinaryRare(baseName, location)
  local base = data.itemBases[baseName]
  inputRule(base, "Unknown base; query catalog bases first: " .. tostring(baseName), location or "/query", "current catalog base name", baseName)
  inputRule(base.type ~= "Jewel" and base.type ~= "Flask" and base.type ~= "Graft" and base.type ~= "Tincture",
    "Recipe supports ordinary equipment only; use actual item text for this type", location or "/query", "ordinary equipment recipe", base.type, true)
  return new("Item", "Rarity: RARE\nPOE Target\n" .. baseName .. "\nCrafted: true")
end
if request.crafts then
  result.crafted_items = {}
  for craftIndex, spec in ipairs(request.crafts) do
    local location = "/crafts/" .. (craftIndex - 1)
    local item = ordinaryRare(spec.base, location .. "/base")
    item.itemLevel = spec.ilvl
    item.quality = spec.quality or 0
    local rare = #spec.affixes > 0
    local header = rare and {"Rarity: RARE", "POE Target", spec.base, "Crafted: true"} or {"Rarity: NORMAL", spec.base}
    table.insert(header, "Item Level: " .. spec.ilvl)
    table.insert(header, "Quality: " .. item.quality)
    local affixLimits = {total = item.affixLimit, prefix = item.prefixes.limit or item.affixLimit / 2, suffix = item.suffixes.limit or item.affixLimit / 2}
    for _, defence in ipairs({"Armour", "Evasion", "EnergyShield", "Ward"}) do
      if item.base.armour and item.base.armour[defence .. "BaseMin"] then table.insert(header, defence .. "BasePercentile: 0.5") end
    end
    if item.base.socketLimit then
      inputRule(spec.sockets and #spec.sockets > 0, "Recipe requires explicit sockets for this base", location .. "/sockets", {maximum = item.base.socketLimit}, spec.sockets or "")
      local count = 0
      for _ in spec.sockets:gmatch("[RGBW]") do count = count + 1 end
      inputRule(count <= item.base.socketLimit, "Socket count exceeds base limit", location .. "/sockets", {maximum = item.base.socketLimit}, {count = count})
      table.insert(header, "Sockets: " .. spec.sockets)
    else
      inputRule(not spec.sockets or spec.sockets == "", "This ordinary base has no gem sockets", location .. "/sockets", "", spec.sockets)
    end
    local seen, counts, audit = {}, {Prefix = 0, Suffix = 0}, {}
    for affixIndex, affix in ipairs(spec.affixes) do
      local affixPath = location .. "/affixes/" .. (affixIndex - 1)
      local mod = data.itemMods.Explicit[affix.id]
      inputRule(mod, "Not an ordinary explicit modifier: " .. affix.id, affixPath .. "/id", "current ordinary modifier ID", affix.id)
      inputRule(mod.type == "Prefix" or mod.type == "Suffix", "Recipe accepts prefixes and suffixes only", affixPath .. "/id", {"Prefix", "Suffix"}, mod.type)
      inputRule(item:GetModSpawnWeight(mod) > 0, "Modifier cannot naturally roll on base: " .. affix.id, affixPath .. "/id", {positive_spawn_weight = true, base = spec.base}, {spawn_weight = item:GetModSpawnWeight(mod)})
      inputRule(mod.level <= spec.ilvl, "Item level is too low for modifier: " .. affix.id, affixPath, {minimum_item_level = mod.level}, {item_level = spec.ilvl})
      inputRule(not seen[mod.group], "Conflicting modifier group: " .. mod.group, affixPath .. "/id", {unique_group = mod.group}, affix.id)
      seen[mod.group] = true
      counts[mod.type] = counts[mod.type] + 1
      local nativeLimit
      if mod.type == "Prefix" then nativeLimit = item.prefixes.limit else nativeLimit = item.suffixes.limit end
      nativeLimit = nativeLimit or item.affixLimit and item.affixLimit / 2
      inputRule(type(nativeLimit) == "number", "Native affix limit is unavailable", affixPath, "native item affix limit", nil, true)
      inputRule(counts[mod.type] <= nativeLimit, "Too many " .. mod.type .. " modifiers for current native item", affixPath, {type = mod.type, maximum = nativeLimit}, {count = counts[mod.type]})
      local roll = affix.roll or 0.5
      table.insert(header, mod.type .. ": {range:" .. roll .. "}" .. affix.id)
      table.insert(audit, {id = affix.id, type = mod.type, group = mod.group, required_item_level = mod.level, roll = roll})
    end
    local implicits = {}
    for line in (item.base.implicit or ""):gmatch("[^\n]+") do table.insert(implicits, itemLib.applyRange(line, 0.5)) end
    table.insert(header, "Implicits: " .. #implicits)
    for _, line in ipairs(implicits) do table.insert(header, line) end
    item = new("Item", table.concat(header, "\n"))
    if rare then item:Craft() end
    table.insert(result.crafted_items, {id = spec.id, text = item:BuildRaw(), base = spec.base, affixes = audit,
      rarity = item.rarity, affix_limits = affixLimits,
      scope = "Ordinary native modifier pool, modifier item-level requirements, exclusive groups and native item affix limits. Base availability, quality attainability, crafting process and prices require evidence."})
  end
end
local function queryCatalog(kind, query, limit, offset)
  local entries = {}
  local q = string.lower(query or "")
  local function matches(value) return q == "" or string.find(string.lower(value or ""), q, 1, true) end
  if kind == "gems" then
    for id, gem in pairs(data.gems) do
      if matches(id) or matches(gem.name) or matches(gem.grantedEffectId) or q == "id:" .. string.lower(gem.grantedEffectId) then
        local skill = data.skills[gem.grantedEffectId]
        local requirements = {}
        for level, values in pairs(skill and skill.levels or {}) do
          if type(level) == "number" then
            local resources = {}
            for key, value in pairs(values) do
              if type(key) == "string" and (key == "cost" or key == "manaMultiplier" or string.find(key, "Reservation", 1, true)) then
                resources[key] = value
              end
            end
            table.insert(requirements, {gem_level = level, character_level = values.levelRequirement, naturally_trainable = gem.naturalMaxLevel ~= nil and level <= gem.naturalMaxLevel,
              strength = calcLib.getGemStatRequirement(values.levelRequirement, skill.support, gem.reqStr or 0),
              dexterity = calcLib.getGemStatRequirement(values.levelRequirement, skill.support, gem.reqDex or 0),
              intelligence = calcLib.getGemStatRequirement(values.levelRequirement, skill.support, gem.reqInt or 0),
              base_resources = next(resources) and resources or nil})
          end
        end
        table.sort(requirements, function(a,b) return a.gem_level < b.gem_level end)
        table.insert(entries, {id = id, name = gem.name, skill_id = gem.grantedEffectId, tags = gem.tags,
          description = skill and skill.description,
          min_level = requirements[1] and requirements[1].character_level, level_requirements = requirements,
          natural_max_level = gem.naturalMaxLevel, vaal = gem.vaalGem or false,
          special_variant = string.find(gem.grantedEffectId, "Alt[XYZ]") ~= nil or string.find(id, "Awakened", 1, true) ~= nil})
      end
    end
  elseif kind == "skills" then
    for id, skill in pairs(data.skills) do
      if matches(id) or matches(skill.name) then
        table.insert(entries, {id = id, name = skill.name, description = skill.description,
          from_item = skill.fromItem or false, support = skill.support or false,
          constant_stats = skill.constantStats, stat_ids = skill.stats})
      end
    end
  elseif kind == "links" then
    local ids = {}
    for id in string.gmatch(query or "", "[^,%s]+") do table.insert(ids, id) end
    local errors, supports, actives, relations = {}, {}, {}, {}
    for index, id in ipairs(ids) do
      local skill = data.skills[id]
      if not skill then table.insert(errors, "Unknown skill: " .. id)
      elseif skill.support then table.insert(supports, {grantedEffect = skill, input_index = index})
      else table.insert(actives, {skill = skill, id = id, input_index = index}) end
    end
    if #ids == 0 then table.insert(errors, "links query requires gem IDs") end
    local applied = {}
    if #errors == 0 then
      for _, entry in ipairs(actives) do
        local active = build.calcsTab.calcs.createActiveSkill({grantedEffect = entry.skill, gemData = data.gems[data.gemForSkill[entry.id]]}, supports, build.calcsTab.mainEnv.player)
        for _, support in ipairs(supports) do
          local found = false
          for _, effect in ipairs(active.effectList) do if effect == support then found = true end end
          if found then applied[support.input_index] = true end
          table.insert(relations, {active_index = entry.input_index, support_index = support.input_index, applied = found})
        end
      end
    end
    local compatible, incompatible = {}, {}
    for _, support in ipairs(supports) do table.insert(applied[support.input_index] and compatible or incompatible, support.grantedEffect.id) end
    table.insert(entries, {id = query, relations = relations, compatible = compatible, incompatible = incompatible, errors = errors,
      valid = #errors == 0, scope = "Native skill-type relations by input position; unused supports are diagnostics. Actual sockets, trigger levels, costs and cooldowns require a character calculation."})
  elseif kind == "classes" then
    for _, class in ipairs(result.classes) do
      if matches(class.name) or tostring(class.id) == q then table.insert(entries, class) end
    end
  elseif kind == "bases" then
    local exactName = q:match("^id:(.+)$")
    for name, base in pairs(data.itemBases) do
      if (exactName and string.lower(name) == exactName) or (not exactName and (matches(name) or matches(base.type))) then
        table.insert(entries, {id = name, name = name, type = base.type, sub_type = base.subType,
          requirements = base.req, sockets = base.socketLimit, socket_category = socketCategory(base), implicit = base.implicit, tags = base.tags})
      end
    end
  elseif kind == "affixes" then
    local baseName, filter = (query or ""):match("^([^|]+)|?(.*)$")
    local item = ordinaryRare(baseName)
    q = string.lower(filter or "")
    for id, mod in pairs(data.itemMods.Explicit) do
      if (mod.type == "Prefix" or mod.type == "Suffix") and item:GetModSpawnWeight(mod) > 0 and (matches(id) or matches(table.concat(mod, " "))) then
        local lines = {}
        for _, line in ipairs(mod) do table.insert(lines, line) end
        table.insert(entries, {id = id, type = mod.type, affix = mod.affix, group = mod.group, required_item_level = mod.level, lines = lines})
      end
    end
  elseif kind == "nodes" then
    local exactId = q:match("^id:(%d+)$")
    for id, node in pairs(build.spec.nodes) do
      local ascendancy = build.spec.tree.ascendNameMap[node.ascendancyName]
      local ascendancyName = ascendancy and ascendancy.ascendClass.name or node.ascendancyName or ""
      if (exactId and tostring(id) == exactId) or (not exactId and (matches(node.name) or matches(ascendancyName) or matches(node.ascendancyName) or matches(table.concat(node.sd or {}, " ")) or tostring(id) == q)) then
        local links, path = {}, {}
        for _, adjacent in pairs(node.linked or {}) do table.insert(links, adjacent.id) end
        for _, pathNode in ipairs(node.path or {}) do table.insert(path, pathNode.id) end
        table.sort(links)
        local entry = {id = id, name = node.name, type = node.type, stats = node.sd, linked = links, mastery_effects = node.masteryEffects,
          ascendancy_name = ascendancyName, ascendancy_internal_name = node.ascendancyName or "",
          no_tree_connection = ascendancyName ~= "" and not node.isAscendancyStart and #links == 0,
          is_ascendancy_start = node.isAscendancyStart or false}
        if ascendancy then
          for classId, class in pairs(build.spec.tree.classes) do
            for _, candidate in pairs(class.classes) do
              if candidate.name == ascendancyName then
                entry.owner_class_id, entry.owner_class_name = classId, class.name
              end
            end
          end
        end
        if node.recipe then
          entry.anoint_recipe = setmetatable({}, {__jsontype = "array"})
          for _, ingredient in ipairs(node.recipe) do
            table.insert(entry.anoint_recipe, {id = ingredient, name = ingredient})
          end
        end
        if hasTreeContext then
          entry.allocated = node.alloc or false
          entry.granted = mainEnv.grantedPassives[id] or false
          entry.reachable = node.path ~= nil
          entry.points_to_allocate = node.pathDist
          entry.path = path
        end
        table.insert(entries, entry)
      end
    end
  elseif kind == "uniques" then
    for _, group in pairs(data.uniques) do
      for _, raw in pairs(group) do if matches(raw) then table.insert(entries, {raw = raw}) end end
    end
  elseif kind == "rules" then
    for _, rule in ipairs(result.rules) do if matches(rule.id) then table.insert(entries, rule) end end
    if matches("slots") then table.insert(entries, {id = "slots", status = "observed", meaning = "engine_constraint", value = result.slots, source = "build.itemsTab.slots"}) end
    if matches("secondary_ascendancies") then table.insert(entries, {id = "secondary_ascendancies", status = "observed", meaning = "native_fact", value = result.secondary_ascendancies, source = "build.spec.tree.alternate_ascendancies"}) end
  elseif kind == "metrics" then
    assert(request.xml ~= "", "metrics requires an actual character XML; a default character is not your character")
    for key, value in pairs(result.stats) do
      if type(value) == "number" and matches(key) then
        table.insert(entries, {id = key, value = value, source = "stats." .. key, actor = key:match("^Minion") and "minion" or "player",
          meanings = result.stat_metadata[key] or {},
          scope = "Native output under this XML and resolved configuration; consult the native metric meaning before comparing"})
      end
    end
  elseif kind == "config" then
    for _, section in ipairs(configOptions) do
      if section.var and (matches(section.var) or matches(section.label) or matches(type(section.tooltip) == "string" and section.tooltip or "")) then
        local option = scalarTable(section)
        if type(section.list) == "table" then
          option.options = {}
          for _, value in ipairs(section.list) do table.insert(option.options, scalarTable(value)) end
          local default = section.defaultIndex and section.list[section.defaultIndex]
          if default then option.default_value = default.val end
        end
        table.insert(entries, option)
      end
    end
  else error("Unknown catalog") end
  table.sort(entries, function(a,b)
    if kind == "nodes" then
      return a.id < b.id
    end
    return tostring(a.id or a.raw or a.var) < tostring(b.id or b.raw or b.var)
  end)
  local selected = {}
  offset = offset or 0
  for i = offset + 1, math.min(#entries, offset + (limit or 30)) do table.insert(selected, entries[i]) end
  if kind == "bases" then
    for _, entry in ipairs(selected) do
      local item = new("Item", "Rarity: RARE\nPOE Catalog\n" .. entry.name .. "\nCrafted: true")
      entry.valid_slots = setmetatable({}, {__jsontype = "array"})
      for slotName in pairs(build.itemsTab.slots) do
        if build.itemsTab:IsItemValidForSlot(item, slotName) then table.insert(entry.valid_slots, slotName) end
      end
      table.sort(entry.valid_slots)
      entry.valid_slots_context = {basis = request.xml ~= "" and "input_xml" or "empty_equipment", tree = result.tree_context,
        scope = "Observed against this query's current equipment and tree; not an unconditional base-slot rule"}
      entry.affix_limits = {total = item.affixLimit, prefix = item.prefixes and item.prefixes.limit, suffix = item.suffixes and item.suffixes.limit}
    end
  end
  return {kind = kind, query = query, total = #entries, offset = offset, next_offset = offset + #selected < #entries and offset + #selected or nil, entries = selected}
end
if request.equipment_layouts then
  result.equipment_layouts = setmetatable({}, {__jsontype = "array"})
  for index, layout in ipairs(request.equipment_layouts) do
    local location = layout.location or "/equipment_layouts/" .. (index - 1)
    inputRule(type(layout.bases) == "table", "Layout bases must map current slots to base names", location .. "/bases", "object", type(layout.bases))
    local items, missing, bases, checks = {}, {}, setmetatable({}, {__jsontype = "object"}), setmetatable({}, {__jsontype = "object"})
    local itemSet = setmetatable({}, {__index = function(_, key)
      missing[key] = true
      return build.itemsTab.slots[key] and {selItemId = 0} or build.itemsTab.activeItemSet[key]
    end})
    for slotName, baseName in pairs(layout.bases) do
      inputRule(build.itemsTab.slots[slotName] ~= nil, "Unknown native slot", location .. "/bases/" .. slotName, "current native slot", slotName)
      inputRule(data.itemBases[baseName] ~= nil, "Unknown native base", location .. "/bases/" .. slotName, "current native base", baseName)
      local item = new("Item", "Rarity: NORMAL\n" .. baseName)
      local id = #items + 1
      items[id], itemSet[slotName], bases[slotName] = item, {selItemId = id}, baseName
    end
    for _,slotName in ipairs(layout.empty_slots or {}) do
      inputRule(build.itemsTab.slots[slotName] ~= nil and layout.bases[slotName] == nil, "Empty slot must be native and not also contain a base", location .. (layout.location and "/bases/" .. slotName or "/empty_slots"), "unoccupied native slot", slotName)
      itemSet[slotName], bases[slotName] = {selItemId = 0}, json.null
      checks[slotName] = {base = json.null, valid_for_slot = true, status = "declared_empty"}
    end
    -- Borrow the installed predicate with an isolated item set. No game-specific
    -- weapon pairing branches and no mutations to the loaded build or its XML.
    local tab = setmetatable({items = items}, {__index = build.itemsTab})
    for slotName, baseName in pairs(layout.bases) do
      missing = {}
      local valid = tab:IsItemValidForSlot(items[itemSet[slotName].selItemId], slotName, itemSet)
      local unresolved = setmetatable({}, {__jsontype = "array"})
      for key in pairs(missing) do table.insert(unresolved, key) end
      table.sort(unresolved)
      checks[slotName] = {base = baseName, unresolved_context = unresolved}
      if #unresolved == 0 then checks[slotName].valid_for_slot = valid == true end
    end
    table.insert(result.equipment_layouts, {layout_id = layout.layout_id, bases = bases, checks = checks,
      context = {basis = "declared_base_layout", tree = result.tree_context,
        scope = "Native compatibility in declared equipment and current tree. Missing item-set fields actually read by the native predicate remain unknown; empty_slots explicitly declares absence. Excludes rolls, availability, sockets and actual passive allocation"}})
  end
end
if request.catalog and request.catalog ~= "" then
  local catalog = queryCatalog(request.catalog, request.query, request.limit, request.offset)
  result.catalog_total, result.catalog = catalog.total, catalog.entries
  result.catalog_offset, result.catalog_next_offset = catalog.offset, catalog.next_offset
end
if request.queries then
  result.catalog_batch = {}
  for _, query in ipairs(request.queries) do
    table.insert(result.catalog_batch, queryCatalog(query.kind, query.query, query.limit, query.offset))
  end
end
result.xml = assert(build:SaveDB("POE Adjust"))
-- Optional evidence is captured after saving, so Calcs selection never changes
-- the exported build. Copy only JSON-safe native values; disclose omissions.
local function evidenceTable(value)
  local omissions, seen, count = {}, {}, 0
  local function visit(v, path, depth)
    count = count + 1
    if count > 8000 or depth > 12 then table.insert(omissions, path .. ": size/depth bound"); return nil end
    if type(v) == "string" or type(v) == "boolean" then return v end
    if type(v) == "number" and v == v and math.abs(v) ~= math.huge then return v end
    if type(v) ~= "table" or seen[v] then table.insert(omissions, path .. ": " .. type(v) .. " or cycle"); return nil end
    seen[v] = true
    local out = {}
    for k, child in pairs(v) do
      if type(k) == "string" or type(k) == "number" then out[k] = visit(child, path .. "/" .. tostring(k), depth+1) end
      if count > 8000 then break end
    end
    seen[v] = nil
    return out
  end
  return visit(value, "", 0), omissions
end
if request.node_probes then
  result.node_screening = {}
  local calculator, base
  if result.capabilities.node_screening then calculator, base = build.calcsTab:GetNodeCalculator() end
  for _, ids in ipairs(request.node_probes) do
    local nodes, issue = {}, not calculator and "Native node calculator unavailable" or nil
    for _, id in ipairs(ids) do
      local node = build.spec.nodes[id]
      if not node then issue = "Unknown current-tree node"
      elseif node.alloc or mainEnv.grantedPassives[id] then issue = "Already allocated or granted"
      else table.insert(nodes,node) end
    end
    if issue then table.insert(result.node_screening,{nodes=ids,status="unsupported",reason=issue})
    else
      local computed = calculator(nodes)
      table.insert(result.node_screening,{nodes=ids,status="screened",baseline=scalarTable(base),stats=scalarTable(computed),minion=scalarTable(computed.Minion),
        scope="Native node-modifier screening only; excludes path cost, allocation legality and combined-build certification"})
    end
  end
end
if request.breakdowns then
  result.breakdowns = {}
  local environments = {}
  for _, query in ipairs(request.breakdowns) do
    local target, skill
    for index, row in ipairs(result.active_skills) do
      if query.skill_ref and row.skill_ref == query.skill_ref or not query.skill_ref and row.is_main then
        target, skill = row, mainEnv.player.activeSkillList[index]; break
      end
    end
    local row = {metric=query.metric,actor=query.actor or target and target.actor or "unknown",skill_ref=target and target.skill_ref,
      status="unsupported",scope="Native CALCS in EFFECTIVE mode, using the input build's combat configuration and selected skill state"}
    if not target or not skill or not target.group then row.reason = "Requested active skill is unavailable in this XML"
    elseif row.actor == "mirage" and target.actor ~= "mirage" then row.reason = "No native mirage actor for this skill"
    else
      local env = environments[target.skill_ref]
      if not env then
        for _, group in ipairs(build.skillsTab.socketGroupList) do
          group.mainActiveSkillCalcs = group.mainActiveSkill
          for _, gem in ipairs(group.gemList) do
            local keys = {}
            for key in pairs(gem) do if type(key)=="string" and key:match("^skill.*Calcs$") then table.insert(keys,key) end end
            for _, key in ipairs(keys) do gem[key] = gem[key:gsub("Calcs$","")] end
          end
        end
        local group = build.skillsTab.socketGroupList[target.group]
        local chosen
        for index, active in ipairs(group.displaySkillList or {}) do
          if active.activeEffect.grantedEffect.id == target.id and active.activeEffect.srcInstance == group.gemList[target.index] then chosen=index; break end
        end
        if chosen and result.capabilities.calcs_breakdown then
          group.mainActiveSkillCalcs = chosen
          build.calcsTab.input.skill_number = target.group
          build.calcsTab.input.misc_buffMode = "EFFECTIVE"
          env = build.calcsTab.calcs.buildOutput(build,"CALCS")
          environments[target.skill_ref] = env
        end
      end
      local actor = env and (row.actor == "minion" and env.minion or (row.actor == "player" or row.actor == "mirage") and env.player)
      if not actor then row.reason = "Requested native actor or skill selection is unavailable"
      else
        row.native_actor = row.actor == "mirage" and "player_output_for_mirage_skill" or row.actor
        row.selected_skill_id = env.player.mainSkill.activeEffect.grantedEffect.id
        row.value = actor.output[query.metric]
        row.main_value = target.is_main and (row.actor=="minion" and result.minion or result.stats)[query.metric] or row.actor=="player" and target.costs[query.metric] or nil
        if type(row.value)=="number" and type(row.main_value)=="number" then row.matches_main_value = math.abs(row.value-row.main_value) <= math.max(0.00001,math.abs(row.main_value)*0.00000001) end
        row.available_metrics = {}
        for key,value in pairs(actor.breakdown or {}) do if type(key)=="string" and type(value)=="table" then table.insert(row.available_metrics,key) end end
        table.sort(row.available_metrics)
        local detail = actor.breakdown and actor.breakdown[query.metric]
        if type(detail)=="table" then
          row.detail,row.omissions = evidenceTable(detail)
          row.status = #row.omissions == 0 and "observed" or "partial"
          if row.matches_main_value == false then row.status="different_context"; row.reason="CALCS value differs from MAIN; this breakdown cannot explain the MAIN value without further investigation" end
        else row.reason = "No native breakdown for this metric; use an isolated input change and full recalculation for a bounded comparison" end
      end
    end
    table.insert(result.breakdowns,row)
  end
end
-- Global-effect toggles remove guards from PoB's active-skill list entirely.
-- Inspect their real links and costs in a separate in-memory calculation while
-- preserving the original XML, permanent output, and recovery values above.
if not request.guard_check and #result.excluded_guard_checks > 0 then
  local excluded = {}
  for _, ref in ipairs(result.excluded_guard_checks) do
    build.skillsTab.socketGroupList[ref.group].gemList[ref.index]["enableGlobal"..ref.effect] = true
    excluded[ref.group..":"..ref.skill_id] = true
  end
  local audit = calculateRequest({xml = assert(build:SaveDB("POE Guard Check")), guard_check = true})
  local auditGems = {}
  for _, gem in ipairs(audit.gems) do auditGems[gem.group..":"..gem.index] = gem end
  for _, ref in ipairs(result.excluded_guard_checks) do
    ref.status = "native_checked"
    local checkedGem = assert(auditGems[ref.group..":"..ref.index])
    for _, gem in ipairs(result.gems) do
      if gem.group == ref.group and gem.index == ref.index then
        for _, key in ipairs({"req_level", "req_str", "req_dex", "req_int"}) do gem[key] = checkedGem[key] end
      end
    end
  end
  for _, gem in ipairs(result.gems) do
    if gem.support and not gem.support_applied then
      local checkedGem = assert(auditGems[gem.group..":"..gem.index])
      gem.supports_excluded_guards = {}
      for _, skillId in ipairs(checkedGem.supports_skills) do
        if excluded[gem.group..":"..skillId] then table.insert(gem.supports_excluded_guards, skillId) end
      end
    end
  end
  for _, skill in ipairs(audit.active_skills) do
    if excluded[skill.group..":"..skill.id] then
      skill.benefit_excluded, skill.is_main = true, false
      table.insert(result.active_skills, skill)
    end
  end
end
return result
end
local response
if request.xml_batch then
  assert(#request.xml_batch >= 1 and #request.xml_batch <= 12, "A calculation batch accepts 1-12 builds")
  response = {calculations = {}}
  for _, xml in ipairs(request.xml_batch) do table.insert(response.calculations, calculateRequest({xml = xml})) end
else
  response = calculateRequest(request)
  if request.health_probe then
    local reread = calculateRequest({xml = response.xml})
    local same = response.level == reread.level and response.class == reread.class and response.tree_version == reread.tree_version
    for _, key in ipairs({"Life", "Str", "Dex", "Int", "ManaUnreserved", "CombinedDPS"}) do
      if response.stats[key] ~= reread.stats[key] then same = false end
    end
    response.health_probe = {startup = true, catalog = true, save = true, roundtrip = same}
  end
end
_RESULT_JSON = assert(json.encode(response))
