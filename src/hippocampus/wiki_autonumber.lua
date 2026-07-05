--[[ wiki_autonumber.lua — render-time section numbering + symbolic §-refs.

Design: docs/designs/wiki-symbolic-section-refs.md (rev3, plateau'd).

Contract (MANDATED shape — see design §3.2 / dual-magi R1-F1):
  * single Pandoc(doc) entry point; NO standalone top-level Header/Link
    functions (pandoc dispatches Inline before Block, so a Link handler
    would fire while the number map is still empty -> every ref "§?").
  * pass 1 numbers Headers and REASSIGNS doc.blocks (:walk returns a copy);
    pass 2 resolves refs and reassigns again. The `numbers` closure bridges
    the two passes.

Behavior:
  * Headers level >= 2 without the .unnumbered class get "N.M... " prepended,
    counted in document order. H1 (page title) is never numbered.
  * Level skips are clamped (an H4 right after an H2 counts at depth 2, not
    depth 3) so a label can never start with "0.".
  * A Link whose entire visible text is "§" and whose target is "#<id>"
    becomes "§<number>" (keeps the href). Unknown id -> "§?" + stderr warn.
    Links with any other text (incl. empty) are untouched.
  * The opt-in sentinel comment <!-- wiki:autonumber --> is dropped from
    output so it does not ship in the HTML bytes.
]]

local SENTINEL = "wiki:autonumber"

function Pandoc(doc)
  local numbers = {}   -- header id -> "1.2"
  local counters = {}  -- depth (1 = first numbered level) -> count
  local prev_depth = 0
  local skip_below = nil  -- level of an .unnumbered header whose subtree
                          -- stays unnumbered (a "1.1" under an unnumbered
                          -- section would point readers into the wrong
                          -- numbered section)

  -- pass 1: number headers, drop the sentinel comment (reassign the copy)
  doc.blocks = doc.blocks:walk({
    RawBlock = function(rb)
      if rb.format == "html" and rb.text:find(SENTINEL, 1, true) then
        return {}
      end
    end,
    RawInline = function(ri)
      -- a mid-paragraph sentinel parses as RawInline, not RawBlock; drop it
      -- too so the comment never ships in the HTML bytes (R1-B4).
      if ri.format == "html" and ri.text:find(SENTINEL, 1, true) then
        return {}
      end
    end,
    Header = function(h)
      if h.level < 2 then
        return nil
      end
      if skip_below and h.level > skip_below then
        return nil  -- descendant of an unnumbered section stays unnumbered
      end
      skip_below = nil
      if h.classes:includes("unnumbered") then
        skip_below = h.level
        return nil
      end
      -- H2 -> depth 1, H3 -> depth 2, ... ; clamp skips to prev_depth + 1
      local depth = h.level - 1
      if depth > prev_depth + 1 then
        depth = prev_depth + 1
      end
      counters[depth] = (counters[depth] or 0) + 1
      for d = depth + 1, #counters do
        counters[d] = nil
      end
      prev_depth = depth
      local parts = {}
      for d = 1, depth do
        parts[#parts + 1] = tostring(counters[d] or 0)
      end
      local label = table.concat(parts, ".")
      if h.identifier and h.identifier ~= "" then
        if numbers[h.identifier] then
          -- last-wins in the map but the browser anchors to the FIRST id;
          -- a ref's displayed number and landing point would disagree.
          io.stderr:write("wiki_autonumber: duplicate id #"
                          .. h.identifier .. "\n")
        end
        numbers[h.identifier] = label
      end
      h.content:insert(1, pandoc.Space())
      h.content:insert(1, pandoc.Str(label))
      return h
    end,
  })

  -- pass 2: resolve [§](#id) refs (reassign again)
  doc.blocks = doc.blocks:walk({
    Link = function(l)
      if #l.content == 1 and l.content[1].t == "Str"
          and l.content[1].text == "§"
          and l.target:sub(1, 1) == "#" then
        local id = l.target:sub(2)
        local n = numbers[id]
        if not n then
          io.stderr:write("wiki_autonumber: dangling ref " .. l.target .. "\n")
        end
        l.content = pandoc.Inlines({ pandoc.Str("§" .. (n or "?")) })
        return l
      end
    end,
  })

  return doc
end
