-- Adapter around the version-matched upstream HeadlessWrapper.
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
local function calculateRequest(request)
if launch.promptMsg then error(launch.promptMsg) end
if request.xml ~= "" then loadBuildFromXML(request.xml, "POE Adjust") end
if launch.promptMsg then error(launch.promptMsg) end
-- Give derived socket and tree state a chance to settle before saving/calculating.
for i = 1, 3 do runCallback("OnFrame") end
local output = build.calcsTab.mainOutput
local function scalarTable(tbl)
  local values = {}
  for key, value in pairs(tbl or {}) do
    if type(value) == "number" and value == value and value ~= math.huge and value ~= -math.huge then values[key] = value
    elseif type(value) == "boolean" or type(value) == "string" then values[key] = value end
  end
  return values
end
local result = {stats = scalarTable(output), minion = scalarTable(output.Minion), warnings = {}, gems = {}, equipped = {}, nodes = {}, errors = {}}
result.resource_rules = {energy_shield_protects_mana = build.calcsTab.mainEnv.modDB:Flag(nil, "EnergyShieldProtectsMana") or false}
result.level = build.characterLevel
result.class = build.spec.curClassName
result.ascendancy = build.spec.curAscendClassName
result.tree_version = build.spec.treeVersion
result.data_version = latestTreeVersion:gsub("_", ".")
result.matching_socket_quality_bonus = data.misc.MatchingSocketQualityBonus or 0
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
result.main_group = build.mainSocketGroup
result.main_skill = build.calcsTab.mainEnv.player.mainSkill.activeEffect.grantedEffect.id
result.main_skill_name = build.calcsTab.mainEnv.player.mainSkill.activeEffect.grantedEffect.name
result.use_second_weapon_set = build.itemsTab.activeItemSet.useSecondWeaponSet
result.config_keys = {}
for _, entry in ipairs(LoadModule("Modules/ConfigOptions")) do
  if entry.var then table.insert(result.config_keys, entry.var) end
end
result.points, result.ascendancy_points, result.secondary_ascendancy_points = build.spec:CountAllocNodes()
for _, line in ipairs(build.controls.warnings.lines or {}) do table.insert(result.warnings, line) end
for id, node in pairs(build.spec.allocNodes) do table.insert(result.nodes, id) end
table.sort(result.nodes)
for groupIndex, group in ipairs(build.skillsTab.socketGroupList) do
  for gemIndex, gem in ipairs(group.gemList) do
    local grantedEffect = gem.grantedEffect or (gem.gemData and gem.gemData.grantedEffect)
    local supported = {}
    for activeGem, _ in pairs(gem.supportEffect and gem.supportEffect.isSupporting or {}) do
      table.insert(supported, activeGem.skillId or activeGem.nameSpec)
    end
    table.sort(supported)
    table.insert(result.gems, {group = groupIndex, index = gemIndex, id = gem.skillId, name = gem.nameSpec,
      enabled = gem.enabled, group_enabled = group.enabled, slot = group.slot, source = group.source, level = gem.level, quality = gem.quality,
      max_base_level = gem.gemData and ((gem.gemData.naturalMaxLevel or 20) + 1),
      color = grantedEffect and ({"R", "G", "B", "W"})[grantedEffect.color] or "W",
      support = grantedEffect and grantedEffect.support or false,
      supports_skills = supported, support_applied = #supported > 0,
      matches_socket = gem.matchesSocket or false,
      socket_quality_bonus = gem.matchesSocket and result.matching_socket_quality_bonus or 0,
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
      active = slot.active, sockets = item.sockets,
      socket_colours_always_match = item.sockets and item.sockets.colourAlwaysMatches or false,
      requirements = item.requirements, unparsed = unparsed})
  end
end
table.sort(result.equipped, function(a, b) return a.slot < b.slot end)
local function ordinaryRare(baseName)
  local base = assert(data.itemBases[baseName], "Unknown base; query catalog bases first: " .. tostring(baseName))
  assert(base.type ~= "Jewel" and base.type ~= "Flask" and base.type ~= "Graft" and base.type ~= "Tincture", "Recipe supports ordinary rare equipment only")
  return new("Item", "Rarity: RARE\nPOE Target\n" .. baseName .. "\nCrafted: true")
end
if request.crafts then
  result.crafted_items = {}
  for _, spec in ipairs(request.crafts) do
    local item = ordinaryRare(spec.base)
    assert(spec.ilvl >= (item.base.req.level or 1), "Item level is below base requirement: " .. spec.base)
    item.itemLevel = spec.ilvl
    item.quality = spec.quality or 20
    local header = {"Rarity: RARE", "POE Target", spec.base, "Crafted: true", "Item Level: " .. spec.ilvl, "Quality: " .. item.quality}
    for _, defence in ipairs({"Armour", "Evasion", "EnergyShield", "Ward"}) do
      if item.base.armour and item.base.armour[defence .. "BaseMin"] then table.insert(header, defence .. "BasePercentile: 0.5") end
    end
    if item.base.socketLimit then
      assert(spec.sockets and #spec.sockets > 0, "Recipe requires explicit sockets for this base")
      local count = 0
      for _ in spec.sockets:gmatch("[RGBW]") do count = count + 1 end
      assert(count <= item.base.socketLimit, "Socket count exceeds base limit")
      table.insert(header, "Sockets: " .. spec.sockets)
    else
      assert(not spec.sockets or spec.sockets == "", "This ordinary base has no gem sockets")
    end
    local seen, counts, audit = {}, {Prefix = 0, Suffix = 0}, {}
    for _, affix in ipairs(spec.affixes) do
      local mod = assert(data.itemMods.Explicit[affix.id], "Not an ordinary explicit modifier: " .. affix.id)
      assert(mod.type == "Prefix" or mod.type == "Suffix", "Recipe accepts prefixes and suffixes only")
      assert(item:GetModSpawnWeight(mod) > 0, "Modifier cannot naturally roll on base: " .. affix.id)
      assert(mod.level <= spec.ilvl, "Item level is too low for modifier: " .. affix.id)
      assert(not seen[mod.group], "Conflicting modifier group: " .. mod.group)
      seen[mod.group] = true
      counts[mod.type] = counts[mod.type] + 1
      assert(counts[mod.type] <= 3, "Too many " .. mod.type .. " modifiers")
      local roll = affix.roll or 0.5
      table.insert(header, mod.type .. ": {range:" .. roll .. "}" .. affix.id)
      table.insert(audit, {id = affix.id, type = mod.type, group = mod.group, required_item_level = mod.level, roll = roll})
    end
    local implicits = {}
    for line in (item.base.implicit or ""):gmatch("[^\n]+") do table.insert(implicits, itemLib.applyRange(line, 0.5)) end
    table.insert(header, "Implicits: " .. #implicits)
    for _, line in ipairs(implicits) do table.insert(header, line) end
    item = new("Item", table.concat(header, "\n"))
    item:Craft()
    table.insert(result.crafted_items, {id = spec.id, text = item:BuildRaw(), base = spec.base, affixes = audit,
      scope = "Ordinary uninfluenced modifier pool, item level, exclusive groups and 3 prefixes/3 suffixes only. Availability, crafting process and prices are not verified."})
  end
end
local function queryCatalog(kind, query, limit)
  local entries = {}
  local q = string.lower(query or "")
  local function matches(value) return q == "" or string.find(string.lower(value or ""), q, 1, true) end
  if kind == "gems" then
    for id, gem in pairs(data.gems) do
      if matches(id) or matches(gem.name) or matches(gem.grantedEffectId) or q == "id:" .. string.lower(gem.grantedEffectId) then
        local skill = data.skills[gem.grantedEffectId]
        local requirements = {}
        for level, values in pairs(skill and skill.levels or {}) do
          if level <= (gem.naturalMaxLevel or 20) then table.insert(requirements, {gem_level = level, character_level = values.levelRequirement}) end
        end
        table.sort(requirements, function(a,b) return a.gem_level < b.gem_level end)
        table.insert(entries, {id = id, name = gem.name, skill_id = gem.grantedEffectId, tags = gem.tags,
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
    assert(#ids >= 1 and #ids <= 6, "links query needs main skill ID and up to five supports")
    local main = assert(data.skills[ids[1]], "Unknown main skill: " .. ids[1])
    assert(not main.support, "Main skill must be active")
    local supports = {}
    for i = 2, #ids do
      local skill = assert(data.skills[ids[i]], "Unknown support: " .. ids[i])
      assert(skill.support, "Expected support gem: " .. ids[i])
      table.insert(supports, {grantedEffect = skill})
    end
    local active = build.calcsTab.calcs.createActiveSkill({grantedEffect = main, gemData = data.gems[data.gemForSkill[ids[1]]]}, supports, build.calcsTab.mainEnv.player)
    local compatible, incompatible = {}, {}
    for _, support in ipairs(supports) do
      local found = false
      for _, effect in ipairs(active.effectList) do if effect == support then found = true end end
      table.insert(found and compatible or incompatible, support.grantedEffect.id)
    end
    table.insert(entries, {id = query, skill_id = ids[1], compatible = compatible, incompatible = incompatible,
      scope = "Native skill-type compatibility only; excludes actual equipment, trigger level/cooldown, resources and damage conversion."})
  elseif kind == "classes" then
    for _, class in ipairs(result.classes) do
      if matches(class.name) or tostring(class.id) == q then table.insert(entries, class) end
    end
  elseif kind == "bases" then
    for name, base in pairs(data.itemBases) do
      if matches(name) or matches(base.type) then
        table.insert(entries, {id = name, name = name, type = base.type, sub_type = base.subType,
          requirements = base.req, sockets = base.socketLimit or 0, implicit = base.implicit, tags = base.tags})
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
    for id, node in pairs(build.spec.nodes) do
      if matches(node.name) or matches(node.ascendancyName) or matches(table.concat(node.sd or {}, " ")) or tostring(id) == q then
        local links, path = {}, {}
        for _, adjacent in pairs(node.linked or {}) do table.insert(links, adjacent.id) end
        for _, pathNode in ipairs(node.path or {}) do table.insert(path, pathNode.id) end
        table.sort(links)
        table.insert(entries, {id = id, name = node.name, type = node.type, stats = node.sd, linked = links, mastery_effects = node.masteryEffects,
          ascendancy_name = node.ascendancyName or "", is_ascendancy_start = node.isAscendancyStart or false,
          allocated = node.alloc or false, reachable = node.path ~= nil, points_to_allocate = node.pathDist, path = path})
      end
    end
  elseif kind == "uniques" then
    for _, group in pairs(data.uniques) do
      for _, raw in pairs(group) do if matches(raw) then table.insert(entries, {raw = raw}) end end
    end
  elseif kind == "config" then
    for _, section in ipairs(LoadModule("Modules/ConfigOptions")) do
      if section.var and (matches(section.var) or matches(section.label) or matches(type(section.tooltip) == "string" and section.tooltip or "")) then
        table.insert(entries, scalarTable(section))
      end
    end
  else error("Unknown catalog") end
  table.sort(entries, function(a,b)
    if kind == "nodes" then
      local da, db = a.points_to_allocate or 1000, b.points_to_allocate or 1000
      if da ~= db then return da < db end
      return a.id < b.id
    end
    return tostring(a.id or a.raw or a.var) < tostring(b.id or b.raw or b.var)
  end)
  local selected = {}
  for i = 1, math.min(#entries, limit or 30) do selected[i] = entries[i] end
  return {kind = kind, query = query, total = #entries, entries = selected}
end
if request.catalog and request.catalog ~= "" then
  local catalog = queryCatalog(request.catalog, request.query, request.limit)
  result.catalog_total, result.catalog = catalog.total, catalog.entries
end
if request.queries then
  result.catalog_batch = {}
  for _, query in ipairs(request.queries) do
    table.insert(result.catalog_batch, queryCatalog(query.kind, query.query, query.limit))
  end
end
result.xml = assert(build:SaveDB("POE Adjust"))
return result
end
local response
if request.xml_batch then
  assert(#request.xml_batch >= 1 and #request.xml_batch <= 12, "A calculation batch accepts 1-12 builds")
  response = {calculations = {}}
  for _, xml in ipairs(request.xml_batch) do table.insert(response.calculations, calculateRequest({xml = xml})) end
else
  response = calculateRequest(request)
end
_RESULT_JSON = assert(json.encode(response))
