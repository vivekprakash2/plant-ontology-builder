"""Renders a KnowledgeGraph as a self-contained, interactive HTML page
(vis-network via CDN, data embedded inline so it opens directly from disk
with no local server / CORS issues).
"""
from __future__ import annotations

import json
from pathlib import Path

from .graph import KnowledgeGraph

# One color per node label, plus a distinct shape for the physical Asset
# nodes so they stand out as the graph's "hubs".
_STYLE = {
    "Asset": {"color": "#f9c74f", "shape": "star", "size": 30},
    "AlarmEvent": {"color": "#f94144", "shape": "dot", "size": 14},
    "WorkOrder": {"color": "#577590", "shape": "dot", "size": 14},
    "OperatorAction": {"color": "#f8961e", "shape": "dot", "size": 14},
    "HealthEvent": {"color": "#9b5de5", "shape": "dot", "size": 14},
    "CostPosting": {"color": "#43aa8b", "shape": "dot", "size": 14},
    "HistorianTag": {"color": "#adb5bd", "shape": "dot", "size": 10},
}
_DEFAULT_STYLE = {"color": "#adb5bd", "shape": "dot", "size": 12}


def _node_title(node_id: str, label: str, properties: dict) -> str:
    rows = "".join(
        f"<tr><td><b>{k}</b></td><td>{v}</td></tr>"
        for k, v in properties.items()
        if v is not None
    )
    return f"<b>{label}</b>: {node_id}<table>{rows}</table>"


def _short_label(node_id: str, label: str, properties: dict) -> str:
    if label == "Asset":
        return properties.get("canonical_name", node_id)
    return node_id


def to_vis_payload(kg: KnowledgeGraph) -> dict:
    vis_nodes = []
    for node in kg.nodes.values():
        style = _STYLE.get(node.label, _DEFAULT_STYLE)
        vis_nodes.append(
            {
                "id": node.id,
                "label": _short_label(node.id, node.label, node.properties),
                "group": node.label,
                "shape": style["shape"],
                "size": style["size"],
                "color": style["color"],
                "title": _node_title(node.id, node.label, node.properties),
            }
        )
    vis_edges = [
        {
            "from": e.source,
            "to": e.target,
            "label": e.rel_type,
            "arrows": "to",
            "font": {"size": 9, "align": "middle"},
            "color": {"color": "#ced4da", "highlight": "#495057"},
        }
        for e in kg.edges
    ]
    return {"nodes": vis_nodes, "edges": vis_edges}


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
  html, body {{ margin: 0; height: 100%; font-family: -apple-system, Arial, sans-serif; }}
  #graph {{ width: 100%; height: 100vh; }}
  #legend {{
    position: absolute; top: 12px; left: 12px; background: rgba(255,255,255,0.95);
    border: 1px solid #dee2e6; border-radius: 8px; padding: 10px 14px; font-size: 13px;
    box-shadow: 0 2px 6px rgba(0,0,0,0.1); z-index: 10;
  }}
  #legend div {{ display: flex; align-items: center; margin: 3px 0; }}
  #legend span.swatch {{ width: 12px; height: 12px; border-radius: 50%; margin-right: 8px; display: inline-block; }}
  #search {{
    position: absolute; top: 12px; right: 12px; z-index: 10;
    padding: 6px 10px; border: 1px solid #dee2e6; border-radius: 6px; font-size: 13px; width: 220px;
  }}
</style>
</head>
<body>
<div id="legend"></div>
<input id="search" type="text" placeholder="Search node id / name...">
<div id="graph"></div>
<script>
  const payload = {payload_json};
  const style = {style_json};

  const legend = document.getElementById('legend');
  Object.keys(style).forEach(function(label) {{
    const row = document.createElement('div');
    row.innerHTML = '<span class="swatch" style="background:' + style[label].color + '"></span>' + label;
    legend.appendChild(row);
  }});

  const nodes = new vis.DataSet(payload.nodes);
  const edges = new vis.DataSet(payload.edges);
  const container = document.getElementById('graph');
  const data = {{ nodes: nodes, edges: edges }};
  const options = {{
    physics: {{ stabilization: true, barnesHut: {{ gravitationalConstant: -8000, springLength: 120 }} }},
    interaction: {{ hover: true, tooltipDelay: 100 }},
    groups: Object.fromEntries(Object.entries(style).map(function(e) {{ return [e[0], {{}}]; }})),
    edges: {{ smooth: {{ type: 'continuous' }} }},
  }};
  const network = new vis.Network(container, data, options);

  document.getElementById('search').addEventListener('input', function(e) {{
    const q = e.target.value.trim().toLowerCase();
    if (!q) {{
      nodes.update(payload.nodes);
      return;
    }}
    const updates = payload.nodes.map(function(n) {{
      const match = n.id.toLowerCase().includes(q) || (n.label || '').toLowerCase().includes(q);
      return {{ id: n.id, hidden: !match }};
    }});
    nodes.update(updates);
  }});
</script>
</body>
</html>
"""


def write_html(kg: KnowledgeGraph, path: Path, title: str = "Plant Ontology Graph") -> None:
    payload = to_vis_payload(kg)
    html = _HTML_TEMPLATE.format(
        title=title,
        payload_json=json.dumps(payload),
        style_json=json.dumps(_STYLE),
    )
    path.write_text(html, encoding="utf-8")
