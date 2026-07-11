"""hippocampus graph — local memory-vault link-graph visualization.

Scans the local ``~/.claude/projects/*/memory/*.md`` vault, resolves ``[[link]]``
references by filestem within each project, and emits a self-contained HTML
force-graph (Obsidian graph-view analog).

Design: docs/designs/MEMORY_LINK_GRAPH.md §7 (ultramagi-reviewed).

Security (invariant #7, all enforced here):
  - All memory-derived strings are JSON-serialized then escaped (``<>&`` +
    U+2028/U+2029 -> ``\\uXXXX``) so a body containing ``</script>`` cannot break
    out — see ``_json_escape``.
  - ZERO external JS (vanilla canvas renderer); emitted HTML carries a CSP that
    blocks all network egress (``connect-src 'none'``).
  - Output is ``chmod 0600`` at a gitignored default path; the tool refuses to
    write under any git work-tree (the file aggregates ALL local memory incl.
    ``scope: private``) unless ``--force``.

Usage:
    hippocampus graph [--root DIR ...] [--out PATH] [--force]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

from .ghost_promote import _fm_get, _parse_md  # reuse the in-package YAML parser

_TYPES = ("user", "feedback", "project", "reference")
_DEFAULT_OUT = os.path.expanduser("~/.claude/hippocampus_graph.html")


def _type_of(fm: dict, stem: str) -> str:
    """Prefer frontmatter metadata.type; fall back to the filename prefix."""
    t = _fm_get(fm, "type")
    if t in _TYPES:
        return t
    prefix = stem.split("_", 1)[0]
    return prefix if prefix in _TYPES else "other"


def scan_vault(roots: list[str]) -> tuple[list[dict], list[dict]]:
    """Return (nodes, edges). Node id = '<project>::<stem>'."""
    from .ingest.wikilinks import extract_wikilinks

    # First pass: collect all real nodes + per-project stem index.
    nodes: dict[str, dict] = {}
    proj_stems: dict[str, set[str]] = {}
    raw: list[tuple[str, str, str]] = []  # (project, stem, body)
    for mem_dir in roots:
        for fp in sorted(glob.glob(os.path.join(mem_dir, "*.md"))):
            stem = Path(fp).stem
            if stem == "MEMORY":
                continue
            project = Path(mem_dir).parent.name
            try:
                text = Path(fp).read_text(encoding="utf-8")
            except Exception:
                continue
            parsed = _parse_md(text)
            fm = parsed.frontmatter if parsed else {}
            body = parsed.body if parsed else text
            nid = f"{project}::{stem}"
            nodes[nid] = {
                "id": nid,
                "label": str(fm.get("description") or fm.get("name") or stem),
                "type": _type_of(fm, stem),
                "project": project,
                "scope": str(fm.get("scope", "local")),
                "stub": False,
                "deg": 0,
            }
            proj_stems.setdefault(project, set()).add(stem.lower())
            raw.append((project, stem, body))

    # Second pass: edges, resolving targets by filestem within the same project.
    edges: list[dict] = []
    seen_edge: set[tuple[str, str]] = set()
    for project, stem, body in raw:
        src = f"{project}::{stem}"
        for target, _alias in extract_wikilinks(body, own_stem=stem):
            tkey = target.lower()
            if tkey in proj_stems.get(project, set()):
                # resolve to the actual node id (case-insensitive stem match)
                tnode = next(
                    (nid for nid in nodes
                     if nid.split("::", 1)[0] == project
                     and nid.split("::", 1)[1].lower() == tkey),
                    None,
                )
                dst, dangling = tnode, False
            else:
                dst, dangling = f"{project}::{target}", True
                if dst not in nodes:
                    nodes[dst] = {
                        "id": dst, "label": target, "type": "other",
                        "project": project, "scope": "local", "stub": True, "deg": 0,
                    }
            if dst is None or (src, dst) in seen_edge:
                continue
            seen_edge.add((src, dst))
            edges.append({"s": src, "t": dst, "dangling": dangling})

    for e in edges:
        nodes[e["s"]]["deg"] += 1
        nodes[e["t"]]["deg"] += 1
    return list(nodes.values()), edges


def _json_escape(obj) -> str:
    """JSON-encode then neutralize </script> breakout (codex-6)."""
    s = json.dumps(obj, ensure_ascii=True, separators=(",", ":"))
    return (s.replace("<", "\\u003c").replace(">", "\\u003e")
             .replace("&", "\\u0026")
             .replace(" ", "\\u2028").replace(" ", "\\u2029"))


def build_html(nodes: list[dict], edges: list[dict]) -> str:
    data = _json_escape({"nodes": nodes, "edges": edges})
    return _HTML_TEMPLATE.replace("/*__GRAPH_DATA__*/null", data)


def _under_git_worktree(path: str) -> str | None:
    d = os.path.dirname(os.path.realpath(path))
    try:
        top = subprocess.run(
            ["git", "-C", d, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if top.returncode == 0 and top.stdout.strip():
        return top.stdout.strip()
    return None


def _ensure_git_excluded(worktree: str, path: str) -> None:
    """Append path (relative to worktree) to .git/info/exclude if not already there."""
    try:
        rel = os.path.relpath(path, worktree)
        exclude = os.path.join(worktree, ".git", "info", "exclude")
        existing = ""
        if os.path.exists(exclude):
            existing = Path(exclude).read_text(encoding="utf-8")
        if rel not in existing.split():
            os.makedirs(os.path.dirname(exclude), exist_ok=True)
            with open(exclude, "a", encoding="utf-8") as f:
                f.write(f"\n# hippocampus graph (private memory dump)\n{rel}\n")
            print(f"  (added {rel} to {worktree}/.git/info/exclude)")
    except Exception as exc:  # best-effort; never block the write
        print(f"  WARN: could not gitignore {path}: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hippocampus graph")
    ap.add_argument("--root", action="append", default=None,
                    help="memory dir glob (repeatable); default ~/.claude/projects/*/memory")
    ap.add_argument("--out", default=_DEFAULT_OUT, help=f"output HTML (default {_DEFAULT_OUT})")
    ap.add_argument("--force", action="store_true",
                    help="allow writing under a git work-tree (file contains private memory)")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    roots = args.root or glob.glob(os.path.expanduser("~/.claude/projects/*/memory"))
    if not roots:
        print("no memory dirs found", file=sys.stderr)
        return 1

    out = os.path.realpath(os.path.expanduser(args.out))
    wt = _under_git_worktree(out)
    if out != os.path.realpath(_DEFAULT_OUT):
        if wt and not args.force:
            print(f"refusing to write into git work-tree {wt}\n"
                  f"  (output aggregates ALL local memory incl. scope: private)\n"
                  f"  use --force to override, or pick a path outside the repo",
                  file=sys.stderr)
            return 2
    elif wt:
        # default path, but ~/.claude is itself a git repo (F5): make sure the
        # private-memory dump can never be accidentally committed.
        _ensure_git_excluded(wt, out)

    nodes, edges = scan_vault(roots)
    html = build_html(nodes, edges)
    # write 0600 (private-memory dump at a predictable path)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(html)
    os.chmod(out, 0o600)
    real = sum(1 for n in nodes if not n["stub"])
    print(f"wrote {out} ({real} memories, {len(nodes) - real} dangling stubs, "
          f"{len(edges)} links)")
    print("  NOTE: contains private memory — delete when done, do not commit/share")
    return 0


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'">
<title>hippocampus memory graph</title>
<style>
  html,body{margin:0;height:100%;background:#11151c;color:#cdd6e0;
    font:13px/1.4 system-ui,sans-serif;overflow:hidden}
  #c{display:block;width:100vw;height:100vh;cursor:grab}
  #c:active{cursor:grabbing}
  #hud{position:fixed;top:10px;left:12px;pointer-events:none;
    text-shadow:0 1px 2px #000}
  #hud b{font-size:15px}
  #legend{position:fixed;bottom:10px;left:12px}
  #legend span{margin-right:12px}
  .dot{display:inline-block;width:10px;height:10px;border-radius:50%;
    margin-right:4px;vertical-align:-1px}
  #tip{position:fixed;pointer-events:none;background:#1c2530;border:1px solid #38465a;
    padding:4px 8px;border-radius:4px;max-width:360px;display:none;z-index:9}
  #search{position:fixed;top:10px;right:12px;background:#1c2530;border:1px solid #38465a;
    color:#cdd6e0;padding:4px 8px;border-radius:4px;width:200px}
</style>
</head>
<body>
<canvas id="c"></canvas>
<div id="hud"><b>memory graph</b><br><span id="stats"></span></div>
<input id="search" placeholder="filter (slug / title)…" autocomplete="off">
<div id="legend"></div>
<div id="tip"></div>
<script type="application/json" id="graph-data">/*__GRAPH_DATA__*/null</script>
<script>
"use strict";
const DATA = JSON.parse(document.getElementById("graph-data").textContent);
const COLORS = {user:"#4fc3f7",feedback:"#ffb74d",project:"#81c784",
  reference:"#ba68c8",other:"#90a4ae"};
const cv = document.getElementById("c"), ctx = cv.getContext("2d");
let W,H,DPR;
function resize(){DPR=devicePixelRatio||1;W=innerWidth;H=innerHeight;
  cv.width=W*DPR;cv.height=H*DPR;ctx.setTransform(DPR,0,0,DPR,0,0);}
addEventListener("resize",resize);resize();

const nodes = DATA.nodes, edges = DATA.edges;
const byId = {}; nodes.forEach(n=>{byId[n.id]=n;
  n.x=(Math.random()-0.5)*Math.min(W,H); n.y=(Math.random()-0.5)*Math.min(W,H);
  n.vx=0; n.vy=0;});
edges.forEach(e=>{e.S=byId[e.s];e.T=byId[e.t];});

// force-directed layout (O(n^2) — fine for hundreds of nodes)
let alpha=1.0;
function step(){
  if(alpha<0.005) return;
  const k=Math.max(30, 700/Math.sqrt(nodes.length+1));
  for(let i=0;i<nodes.length;i++){const a=nodes[i];
    for(let j=i+1;j<nodes.length;j++){const b=nodes[j];
      let dx=a.x-b.x, dy=a.y-b.y, d2=dx*dx+dy*dy+0.01, d=Math.sqrt(d2);
      let f=(k*k)/d2; if(d>600){f=0;}
      let fx=f*dx/d, fy=f*dy/d;
      a.vx+=fx;a.vy+=fy;b.vx-=fx;b.vy-=fy;}}
  edges.forEach(e=>{let dx=e.T.x-e.S.x, dy=e.T.y-e.S.y,
    d=Math.sqrt(dx*dx+dy*dy)+0.01, f=(d-k)*0.02;
    let fx=f*dx/d, fy=f*dy/d;
    e.S.vx+=fx;e.S.vy+=fy;e.T.vx-=fx;e.T.vy-=fy;});
  nodes.forEach(n=>{n.vx-=n.x*0.002;n.vy-=n.y*0.002; // gravity to center
    if(n===drag) return;
    n.x+=n.vx*alpha;n.y+=n.vy*alpha;n.vx*=0.85;n.vy*=0.85;});
  alpha*=0.985;
}

let tx=W/2, ty=H/2, scale=1, drag=null, pan=false, lpx=0,lpy=0, hover=null, filter="";
function toScreen(n){return [n.x*scale+tx, n.y*scale+ty];}
function fromScreen(px,py){return [(px-tx)/scale,(py-ty)/scale];}

function draw(){
  step();
  ctx.clearRect(0,0,W,H);
  ctx.lineWidth=1;
  edges.forEach(e=>{const [x1,y1]=toScreen(e.S),[x2,y2]=toScreen(e.T);
    ctx.strokeStyle=e.dangling?"rgba(120,90,90,0.35)":"rgba(120,140,170,0.30)";
    if(e.dangling){ctx.setLineDash([4,4]);}else{ctx.setLineDash([]);}
    ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.stroke();});
  ctx.setLineDash([]);
  nodes.forEach(n=>{const [x,y]=toScreen(n);
    const r=Math.min(18,4+Math.sqrt(n.deg)*2);
    const match=filter && ((n.id.toLowerCase().includes(filter))||
      (n.label.toLowerCase().includes(filter)));
    ctx.globalAlpha = (!filter||match)?1:0.15;
    ctx.fillStyle=n.stub?"#3a4250":(COLORS[n.type]||COLORS.other);
    ctx.beginPath();ctx.arc(x,y,r,0,7);ctx.fill();
    if(n===hover||match){ctx.strokeStyle="#fff";ctx.lineWidth=1.5;ctx.stroke();}
    // labels only on hover / search-match / strong zoom — keeps the overview clean
    if(scale>2.2||n===hover||match){ctx.globalAlpha=(!filter||match)?0.95:0.15;
      ctx.fillStyle="#e8eef5";ctx.font="11px system-ui";
      ctx.fillText(n.id.split("::")[1], x+r+2, y+4);}
  });
  ctx.globalAlpha=1;
  requestAnimationFrame(draw);
}

function pick(px,py){let best=null,bd=400;
  for(const n of nodes){const [x,y]=toScreen(n);
    const d=(x-px)**2+(y-py)**2; if(d<bd){bd=d;best=n;}}
  return best;}

cv.addEventListener("mousedown",e=>{const n=pick(e.clientX,e.clientY);
  if(n){drag=n;}else{pan=true;} lpx=e.clientX;lpy=e.clientY;});
addEventListener("mousemove",e=>{
  if(drag){const [wx,wy]=fromScreen(e.clientX,e.clientY);drag.x=wx;drag.y=wy;
    drag.vx=0;drag.vy=0;alpha=Math.max(alpha,0.3);}
  else if(pan){tx+=e.clientX-lpx;ty+=e.clientY-lpy;lpx=e.clientX;lpy=e.clientY;}
  else{hover=pick(e.clientX,e.clientY);
    const tip=document.getElementById("tip");
    if(hover){tip.style.display="block";tip.style.left=(e.clientX+12)+"px";
      tip.style.top=(e.clientY+12)+"px";
      tip.textContent=hover.id.split("::")[1]+(hover.stub?" (dangling)":"")+
        "  ["+hover.type+"/"+hover.scope+"]  ·  "+hover.label;}
    else tip.style.display="none";}});
addEventListener("mouseup",()=>{drag=null;pan=false;});
cv.addEventListener("wheel",e=>{e.preventDefault();
  const f=e.deltaY<0?1.1:0.9;const mx=e.clientX,my=e.clientY;
  tx=mx-(mx-tx)*f;ty=my-(my-ty)*f;scale*=f;},{passive:false});
document.getElementById("search").addEventListener("input",e=>{
  filter=e.target.value.toLowerCase();});

const types={}; nodes.forEach(n=>{if(!n.stub)types[n.type]=(types[n.type]||0)+1;});
document.getElementById("legend").innerHTML=Object.keys(COLORS).map(t=>
  '<span><i class="dot" style="background:'+COLORS[t]+'"></i>'+t+
  (types[t]?" ("+types[t]+")":"")+'</span>').join("");
document.getElementById("stats").textContent=
  nodes.filter(n=>!n.stub).length+" memories · "+edges.length+" links · drag/scroll/search";
draw();
</script>
</body>
</html>
"""
