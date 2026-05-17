"""
viz.py
Local web server for the CodeGraph visualization.
Run with: python -m codegraph.viz --db codegraph.db --repo ./path/to/repo

Opens a D3.js force-directed graph in your browser.
Click any node to read its source code.
Filter by file path to zoom into a subsystem.
"""

from __future__ import annotations
import argparse
import json
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from codegraph.graph.store import GraphStore


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>CodeGraph Visualizer</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f1117; color: #e2e8f0; font-family: 'Segoe UI', system-ui, sans-serif; overflow: hidden; }

  #toolbar {
    position: fixed; top: 0; left: 0; right: 0; height: 52px;
    background: #1a1d27; border-bottom: 1px solid #2d3148;
    display: flex; align-items: center; gap: 12px; padding: 0 16px; z-index: 100;
  }
  #toolbar h1 { font-size: 15px; font-weight: 600; color: #a78bfa; white-space: nowrap; }
  #filter { flex: 1; max-width: 340px; background: #252836; border: 1px solid #3d4166;
    color: #e2e8f0; padding: 6px 12px; border-radius: 6px; font-size: 13px; outline: none; }
  #filter:focus { border-color: #7c3aed; }
  #load-btn { background: #7c3aed; color: white; border: none; padding: 7px 16px;
    border-radius: 6px; font-size: 13px; cursor: pointer; white-space: nowrap; }
  #load-btn:hover { background: #6d28d9; }
  #stats { font-size: 12px; color: #94a3b8; white-space: nowrap; }

  #legend {
    position: fixed; bottom: 16px; left: 16px; background: #1a1d27;
    border: 1px solid #2d3148; border-radius: 8px; padding: 10px 14px; z-index: 100;
    font-size: 12px;
  }
  .legend-item { display: flex; align-items: center; gap: 8px; margin: 4px 0; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; }

  #panel {
    position: fixed; right: 0; top: 52px; bottom: 0; width: 420px;
    background: #1a1d27; border-left: 1px solid #2d3148;
    display: flex; flex-direction: column; z-index: 100;
    transform: translateX(100%); transition: transform 0.2s ease;
  }
  #panel.open { transform: translateX(0); }
  #panel-header { padding: 14px 16px; border-bottom: 1px solid #2d3148; display: flex; justify-content: space-between; align-items: center; }
  #panel-title { font-size: 14px; font-weight: 600; color: #a78bfa; }
  #panel-meta { font-size: 11px; color: #64748b; margin-top: 2px; }
  #close-btn { background: none; border: none; color: #64748b; cursor: pointer; font-size: 18px; line-height: 1; }
  #close-btn:hover { color: #e2e8f0; }
  #code-wrap { flex: 1; overflow: auto; }
  #code { font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 12px;
    line-height: 1.6; padding: 14px 16px; white-space: pre; color: #e2e8f0; }

  svg { position: fixed; top: 52px; left: 0; right: 0; bottom: 0; }
  .node circle { cursor: pointer; stroke-width: 1.5px; }
  .node text { font-size: 10px; fill: #cbd5e1; pointer-events: none; }
  .link { stroke-opacity: 0.35; }
  .node.highlighted circle { stroke: #fbbf24 !important; stroke-width: 3px; }

  #tooltip {
    position: fixed; background: #252836; border: 1px solid #3d4166;
    border-radius: 6px; padding: 8px 12px; font-size: 12px; pointer-events: none;
    max-width: 280px; z-index: 200; display: none;
  }
</style>
</head>
<body>
<div id="toolbar">
  <h1>⬡ CodeGraph</h1>
  <input id="filter" type="text" placeholder="Filter by filepath (e.g. fastapi/routing)" />
  <button id="load-btn" onclick="loadGraph()">Load</button>
  <span id="stats"></span>
</div>

<div id="legend">
  <div class="legend-item"><div class="legend-dot" style="background:#a78bfa"></div> class</div>
  <div class="legend-item"><div class="legend-dot" style="background:#38bdf8"></div> function</div>
  <div class="legend-item"><div class="legend-dot" style="background:#fb923c"></div> module</div>
  <div class="legend-item" style="margin-top:8px; color:#64748b">click node to read code</div>
</div>

<div id="panel">
  <div id="panel-header">
    <div>
      <div id="panel-title">—</div>
      <div id="panel-meta">—</div>
    </div>
    <button id="close-btn" onclick="closePanel()">✕</button>
  </div>
  <div id="code-wrap"><pre id="code">Select a node to view its source.</pre></div>
</div>

<div id="tooltip"></div>
<svg id="graph"></svg>

<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<script>
const COLOR = { class: '#a78bfa', function: '#38bdf8', module: '#fb923c', unknown: '#94a3b8' };
const EDGE_COLOR = { calls: '#6366f1', contains: '#334155', imports: '#0f766e' };

let simulation, svg, g;
const W = () => window.innerWidth;
const H = () => window.innerHeight - 52;

function init() {
  svg = d3.select('#graph').attr('width', W()).attr('height', H());
  g = svg.append('g');
  svg.call(d3.zoom().scaleExtent([0.05, 8]).on('zoom', e => g.attr('transform', e.transform)));
  window.addEventListener('resize', () => svg.attr('width', W()).attr('height', H()));
}

async function loadGraph() {
  const filter = document.getElementById('filter').value.trim();
  const url = '/api/graph' + (filter ? `?filter=${encodeURIComponent(filter)}` : '');
  const data = await fetch(url).then(r => r.json());
  render(data);
}

function render(data) {
  g.selectAll('*').remove();
  if (simulation) simulation.stop();

  const { nodes, edges } = data;
  document.getElementById('stats').textContent =
    `${nodes.length} nodes · ${edges.length} edges`;

  // build id → index map
  const idMap = new Map(nodes.map((n, i) => [n.id, i]));
  const links = edges
    .filter(e => idMap.has(e.source) && idMap.has(e.target))
    .map(e => ({ ...e, source: idMap.get(e.source), target: idMap.get(e.target), _sk: e.kind }));

  simulation = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).distance(d => d._sk === 'contains' ? 60 : 120).strength(0.4))
    .force('charge', d3.forceManyBody().strength(-120))
    .force('center', d3.forceCenter(W() / 2, H() / 2))
    .force('collision', d3.forceCollide(18));

  const link = g.append('g').selectAll('line').data(links).join('line')
    .attr('class', 'link')
    .attr('stroke', d => EDGE_COLOR[d._sk] || '#334155')
    .attr('stroke-width', d => d._sk === 'calls' ? 1.5 : 0.8);

  const node = g.append('g').selectAll('g').data(nodes).join('g')
    .attr('class', 'node')
    .call(d3.drag()
      .on('start', (e, d) => { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on('drag',  (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on('end',   (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }))
    .on('click', (e, d) => openPanel(d))
    .on('mouseover', (e, d) => showTooltip(e, d))
    .on('mouseout', hideTooltip);

  node.append('circle')
    .attr('r', d => d.kind === 'class' ? 9 : d.kind === 'module' ? 7 : 5)
    .attr('fill', d => COLOR[d.kind] || COLOR.unknown)
    .attr('stroke', d => d3.color(COLOR[d.kind] || COLOR.unknown).darker(1));

  node.append('text')
    .attr('dx', 10).attr('dy', 4)
    .text(d => d.name.length > 28 ? d.name.slice(0, 26) + '…' : d.name);

  simulation.on('tick', () => {
    link.attr('x1', d => d.source.x).attr('y1', d => d.source.y)
        .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
    node.attr('transform', d => `translate(${d.x},${d.y})`);
  });
}

const tip = document.getElementById('tooltip');
function showTooltip(e, d) {
  tip.style.display = 'block';
  tip.style.left = (e.clientX + 14) + 'px';
  tip.style.top  = (e.clientY - 8)  + 'px';
  tip.innerHTML = `<b>${d.name}</b><br><span style="color:#64748b">${d.kind} · ${d.filepath}</span>`;
}
function hideTooltip() { tip.style.display = 'none'; }

async function openPanel(d) {
  document.getElementById('panel').classList.add('open');
  document.getElementById('panel-title').textContent = d.name;
  document.getElementById('panel-meta').textContent = `${d.kind} · ${d.filepath}:${d.start_line}–${d.end_line}`;
  document.getElementById('code').textContent = 'Loading…';

  const res = await fetch(`/api/read?filepath=${encodeURIComponent(d.filepath)}&start=${d.start_line}&end=${d.end_line}`);
  const { code } = await res.json();
  document.getElementById('code').textContent = code || '(no source available)';
}
function closePanel() { document.getElementById('panel').classList.remove('open'); }

init();
loadGraph();
</script>
</body>
</html>
"""


def create_viz_app(store: GraphStore, repo_path: str) -> FastAPI:
    app = FastAPI(title="CodeGraph Visualizer")
    _repo = Path(repo_path).resolve()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTML

    @app.get("/api/graph")
    def graph(filter: str = Query(default="")):
        data = store.graph_export(filepath_filter=filter)
        # cap at 2000 nodes for browser performance
        if len(data["nodes"]) > 2000:
            data["nodes"] = data["nodes"][:2000]
            node_ids = {n["id"] for n in data["nodes"]}
            data["edges"] = [e for e in data["edges"]
                             if e["source"] in node_ids and e["target"] in node_ids]
        return JSONResponse(data)

    @app.get("/api/read")
    def read(filepath: str, start: int = 1, end: int = 80):
        full = _repo / filepath
        if not full.exists():
            return JSONResponse({"code": f"File not found: {filepath}"})
        try:
            lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
            s, e  = max(0, start - 1), min(len(lines), end)
            numbered = "\n".join(f"{s+1+i:4d}  {l}" for i, l in enumerate(lines[s:e]))
            return JSONResponse({"code": numbered})
        except Exception as ex:
            return JSONResponse({"code": str(ex)})

    return app


def main():
    parser = argparse.ArgumentParser(description="CodeGraph Visualizer")
    parser.add_argument("--db",   default="codegraph.db")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--port", type=int, default=7070)
    args = parser.parse_args()

    store = GraphStore(db_path=args.db)
    store.load()

    s = store.stats()
    print(f"[codegraph viz] loaded {s['nodes']} nodes, {s['edges']} edges")
    print(f"[codegraph viz] opening http://localhost:{args.port}")

    app = create_viz_app(store, args.repo)

    # open browser after short delay
    threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="error")


if __name__ == "__main__":
    main()
