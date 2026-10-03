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
    table.insert(result.gems, {group = groupIndex, index = gemIndex, id = gem.skillId, name = gem.nameSpec,
      enabled = gem.enabled, group_enabled = group.enabled, slot = group.slot, source = group.source, level = gem.level, quality = gem.quality,
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
      requirements = item.requirements, unparsed = unparsed})
  end
end
table.sort(result.equipped, function(a, b) return a.slot < b.slot end)
if request.catalog and request.catalog ~= "" then
  local entries = {}
  local q = string.lower(request.query or "")
  local function matches(value) return q == "" or string.find(string.lower(value or ""), q, 1, true) end
  if request.catalog == "gems" then
    for id, gem in pairs(data.gems) do
      if matches(id) or matches(gem.name) then table.insert(entries, {id = id, name = gem.name, skill_id = gem.grantedEffectId, tags = gem.tags}) end
    end
  elseif request.catalog == "nodes" then
    for id, node in pairs(build.spec.nodes) do
      if matches(node.name) or matches(table.concat(node.sd or {}, " ")) or tostring(id) == q then
        local links = {}
        for _, adjacent in pairs(node.linked or {}) do table.insert(links, adjacent.id) end
        table.insert(entries, {id = id, name = node.name, type = node.type, stats = node.sd, linked = links, mastery_effects = node.masteryEffects})
      end
    end
  elseif request.catalog == "uniques" then
    for _, group in pairs(data.uniques) do
      for _, raw in pairs(group) do if matches(raw) then table.insert(entries, {raw = raw}) end end
    end
  elseif request.catalog == "config" then
    for _, section in ipairs(LoadModule("Modules/ConfigOptions")) do
      if section.var and matches(section.var) then table.insert(entries, scalarTable(section)) end
    end
  else error("Unknown catalog") end
  table.sort(entries, function(a,b) return tostring(a.id or a.raw or a.var) < tostring(b.id or b.raw or b.var) end)
  result.catalog_total = #entries
  result.catalog = {}
  for i = 1, math.min(#entries, request.limit or 30) do result.catalog[i] = entries[i] end
end
result.xml = assert(build:SaveDB("POE Adjust"))
_RESULT_JSON = assert(json.encode(result))
