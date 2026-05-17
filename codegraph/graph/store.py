"""
store.py
Builds a networkx DiGraph from ParseResult nodes/edges.
Persists to SQLite so you don't re-parse on every run.
Exposes the four core query methods used by the MCP server.
"""

from __future__ import annotations
import sqlite3
import json
from pathlib import Path
from typing import Any

import networkx as nx

from codegraph.ingestion.parser import CodeNode, CodeEdge, ParseResult


# GraphStore
class GraphStore:
    def __init__(self, db_path: str = "codegraph.db"):
        self.db_path = db_path
        self.G: nx.DiGraph = nx.DiGraph()
        self._init_db()

    # DB init
    def _init_db(self):
        con = self._con()
        con.executescript("""
    CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    name TEXT,
    kind TEXT,
    filepath TEXT,
    start_line INTEGER,
    end_line INTEGER,
    docstring TEXT
    );
    CREATE TABLE IF NOT EXISTS edges (
    src TEXT,
    dst TEXT,
    kind TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
    CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
    CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
    """)
        con.commit()
        con.close()

    def _con(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    # Ingest
    def ingest(self, result: ParseResult):
        """Load a ParseResult into the in-memory graph and persist to SQLite."""
        con = self._con()

        for node in result.nodes:
            self.G.add_node(node.id, **{
                "name": node.name,
                "kind": node.kind,
                "filepath": node.filepath,
                "start_line": node.start_line,
                "end_line": node.end_line,
                "docstring": node.docstring,
            })
            con.execute("""
                INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?,?,?)
            """, (node.id, node.name, node.kind, node.filepath,
                  node.start_line, node.end_line, node.docstring))

        for edge in result.edges:
            # resolve unresolved "calls" edges by name
            dst = edge.dst
            if edge.kind == "calls" and "::" not in dst:
                dst = self._resolve_name(dst) or dst
            self.G.add_edge(edge.src, dst, kind=edge.kind)
            con.execute("INSERT INTO edges VALUES (?,?,?)",
                        (edge.src, dst, edge.kind))

        con.commit()
        con.close()

    def _resolve_name(self, name: str) -> str | None:
        """Try to find a node id matching a bare name."""
        for node_id, data in self.G.nodes(data=True):
            if data.get("name") == name:
                return node_id
        return None

    # Persistence: load from SQLite into graph

    def load(self):
        """Reload graph from SQLite (so you don't re-parse every run)."""
        con = self._con()
        for row in con.execute("SELECT * FROM nodes"):
            nid, name, kind, filepath, sl, el, doc = row
            self.G.add_node(nid, name=name, kind=kind, filepath=filepath,
                            start_line=sl, end_line=el, docstring=doc)
        for row in con.execute("SELECT * FROM edges"):
            src, dst, kind = row
            self.G.add_edge(src, dst, kind=kind)
        con.close()

    def is_empty(self) -> bool:
        return self.G.number_of_nodes() == 0

    # Query API (for the MCP tools)

    def find_definition(self, name: str) -> list[dict]:
        """
        Return all nodes whose name contains `name` (case-insensitive).
        Includes file, line range, kind, and docstring.
        """
        name_lower = name.lower()
        results = []
        for node_id, data in self.G.nodes(data=True):
            if name_lower in data.get("name", "").lower():
                results.append({
                    "id": node_id,
                    "name": data["name"],
                    "kind": data["kind"],
                    "filepath": data["filepath"],
                    "start_line": data["start_line"],
                    "end_line": data["end_line"],
                    "docstring": data.get("docstring", ""),
                })
        results.sort(key=lambda x: len(x["name"]))
        return results[:20]

    def what_calls(self, name: str) -> list[dict]:
        """
        Return all nodes that call a function matching `name`.
        """
        # first find the target node(s)
        targets = {r["id"] for r in self.find_definition(name)
                   if r["kind"] == "function"}
        # also match unresolved bare-name edges
        callers = []
        seen = set()
        for src, dst, data in self.G.edges(data=True):
            if data.get("kind") != "calls":
                continue
            if dst in targets or ("::" not in dst and name.lower() in dst.lower()):
                if src not in seen:
                    seen.add(src)
                    src_data = self.G.nodes.get(src, {})
                    callers.append({
                        "id": src,
                        "name": src_data.get("name", src),
                        "kind": src_data.get("kind", "?"),
                        "filepath": src_data.get("filepath", "?"),
                        "start_line": src_data.get("start_line"),
                        "end_line": src_data.get("end_line"),
                    })
        return callers[:20]

    def what_does(self, name: str) -> list[dict]:
        """
        Return all functions/classes called BY a node matching `name`.
        """
        sources = {r["id"] for r in self.find_definition(name)}
        callees = []
        seen = set()
        for src in sources:
            for _, dst, data in self.G.out_edges(src, data=True):
                if data.get("kind") == "calls" and dst not in seen:
                    seen.add(dst)
                    dst_data = self.G.nodes.get(dst, {})
                    callees.append({
                        "id": dst,
                        "name": dst_data.get("name", dst),
                        "kind": dst_data.get("kind", "unknown"),
                        "filepath": dst_data.get("filepath", "?"),
                        "start_line": dst_data.get("start_line"),
                        "end_line": dst_data.get("end_line"),
                    })
        return callees[:20]

    def list_files(self) -> list[dict]:
        """Return a summary of all indexed files with node counts."""
        files: dict[str, dict] = {}
        for _, data in self.G.nodes(data=True):
            fp = data.get("filepath", "unknown")
            if fp not in files:
                files[fp] = {"filepath": fp, "functions": 0, "classes": 0, "total": 0}
            kind = data.get("kind")
            if kind == "function":
                files[fp]["functions"] += 1
            elif kind == "class":
                files[fp]["classes"] += 1
            files[fp]["total"] += 1
        return sorted(files.values(), key=lambda x: x["filepath"])

    def impact_analysis(self, name: str, max_depth: int = 5) -> dict:
        """
        Given a function name, find everything that would be affected if it changed.
        Traverses the call graph in reverse (who calls this, who calls those, etc.)
        Returns a tree of affected nodes grouped by depth level.
        """
        targets = {r["id"] for r in self.find_definition(name)
                   if r["kind"] in ("function", "class")}
        # also match bare unresolved names in edges
        bare_targets = {name}  # for matching unresolved dst strings
        if not targets:
            return {"error": f"No function or class found matching '{name}'"}

        # Build reverse graph for upstream traversal
        reverse_G = self.G.reverse(copy=False)

        visited: set[str] = set(targets)
        levels: list[list[dict]] = []

        current_frontier = targets
        for depth in range(max_depth):
            next_frontier: set[str] = set()
            level_nodes: list[dict] = []

            for node_id in current_frontier:
                for src_id, _, data in self.G.in_edges(node_id, data=True):
                    if data.get("kind") != "calls":
                        continue
                    if src_id in visited:
                        continue
                    visited.add(src_id)
                    next_frontier.add(src_id)
                    caller_data = self.G.nodes.get(src_id, {})
                    level_nodes.append({
                        "id":         src_id,
                        "name":       caller_data.get("name", src_id),
                        "kind":       caller_data.get("kind", "unknown"),
                        "filepath":   caller_data.get("filepath", "?"),
                        "start_line": caller_data.get("start_line"),
                        "end_line":   caller_data.get("end_line"),
                    })

            # also scan all edges for bare-name matches on first pass
            if depth == 0:
                for src_id, dst, data in self.G.edges(data=True):
                    if data.get("kind") != "calls":
                        continue
                    if dst not in bare_targets:
                        continue
                    if src_id in visited:
                        continue
                    visited.add(src_id)
                    next_frontier.add(src_id)
                    caller_data = self.G.nodes.get(src_id, {})
                    level_nodes.append({
                        "id": src_id,
                        "name": caller_data.get("name", src_id),
                        "kind": caller_data.get("kind", "unknown"),
                        "filepath": caller_data.get("filepath", "?"),
                        "start_line": caller_data.get("start_line"),
                        "end_line": caller_data.get("end_line"),
                    })

            if not level_nodes:
                break
            levels.append(level_nodes)
            current_frontier = next_frontier

        # flatten for summary
        all_affected = [n for level in levels for n in level]
        files_affected = len({n["filepath"] for n in all_affected})

        return {
            "target": name,
            "total_affected": len(all_affected),
            "files_affected": files_affected,
            "depth_reached": len(levels),
            "by_depth": levels,
        }

    def graph_export(self, filepath_filter: str = "") -> dict:
        """
        Export the graph as JSON for visualization.
        Optionally filter to nodes in files matching filepath_filter.
        Returns {nodes: [...], edges: [...]} suitable for D3.js.
        """
        nodes = []
        node_ids: set[str] = set()

        for node_id, data in self.G.nodes(data=True):
            fp = data.get("filepath", "")
            if filepath_filter and filepath_filter not in fp:
                continue
            # skip bare unresolved names (no "::" means it's a raw callee name)
            if "::" not in node_id:
                continue
            node_ids.add(node_id)
            nodes.append({
                "id": node_id,
                "name": data.get("name", node_id),
                "kind": data.get("kind", "unknown"),
                "filepath": fp,
                "start_line": data.get("start_line"),
                "end_line": data.get("end_line"),
            })

        edges = []
        for src, dst, data in self.G.edges(data=True):
            if src not in node_ids or dst not in node_ids:
                continue
            edges.append({
                "source": src,
                "target": dst,
                "kind": data.get("kind", "unknown"),
            })

        return {"nodes": nodes, "edges": edges}

    def stats(self) -> dict:
        nodes = self.G.number_of_nodes()
        edges = self.G.number_of_edges()
        kinds: dict[str, int] = {}
        for _, data in self.G.nodes(data=True):
            k = data.get("kind", "unknown")
            kinds[k] = kinds.get(k, 0) + 1
        return {"nodes": nodes, "edges": edges, "by_kind": kinds}
