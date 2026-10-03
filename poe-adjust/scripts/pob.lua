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
result.level = build.characterLevel
result.class = build.spec.curClassName
result.ascendancy = build.spec.curAscendClassName
result.tree_version = build.spec.treeVersion
result.data_version = latestTreeVersion:gsub("_", ".")
result.matching_socket_quality_bonus = data.misc.MatchingSocketQualityBonus or 0
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
    table.insert(result.gems, {group = groupIndex, index = gemIndex, id = gem.skillId, name = gem.nameSpec,
      enabled = gem.enabled, group_enabled = group.enabled, slot = group.slot, source = group.source, level = gem.level, quality = gem.quality,
      color = grantedEffect and ({"R", "G", "B", "W"})[grantedEffect.color] or "W",
      support = grantedEffect and grantedEffect.support or false,
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
_RESULT_JSON = assert(json.encode(result))
