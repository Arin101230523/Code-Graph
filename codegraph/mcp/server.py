"""
server.py
MCP server wrapping the GraphStore query methods.
Run with: python -m codegraph.mcp.server --repo ./path/to/repo

Exposes four tools:
  find_definition  — where is this function/class defined?
  what_calls — what code calls this function?
  what_does — what does this function call?
  list_files — what files are indexed?
"""

from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from codegraph.ingestion.parser import parse_repo
from codegraph.graph.store import GraphStore


# Bootstrap: parse or load from cache
def build_store(repo_path: str, db_path: str, force: bool = False) -> GraphStore:
    store = GraphStore(db_path=db_path)

    if not force and Path(db_path).exists() and not store.is_empty():
        print(f"[codegraph] loading from cache: {db_path}", file=sys.stderr)
        store.load()
    else:
        print(f"[codegraph] parsing repo: {repo_path}", file=sys.stderr)
        result = parse_repo(repo_path)
        store.ingest(result)
        s = store.stats()
        print(f"[codegraph] indexed {s['nodes']} nodes, {s['edges']} edges", file=sys.stderr)

    return store


# MCP app
def create_app(store: GraphStore) -> FastMCP:
    mcp = FastMCP("codegraph")

    @mcp.tool()
    def find_definition(name: str) -> str:
        """
        Find where a function or class is defined in the codebase.
        Returns file path, line numbers, kind, and docstring.
        name: the function or class name to look up (partial match supported)
        """
        results = store.find_definition(name)
        if not results:
            return f"No definition found for '{name}'"
        lines = [f"Found {len(results)} match(es) for '{name}':\n"]
        for r in results:
            lines.append(
                f"  [{r['kind']}] {r['name']}\n"
                f"    File: {r['filepath']}:{r['start_line']}-{r['end_line']}\n"
                + (f"    Doc:  {r['docstring'][:120]}\n" if r['docstring'] else "")
            )
        return "\n".join(lines)

    @mcp.tool()
    def what_calls(name: str) -> str:
        """
        Find all functions that call a given function.
        Useful for understanding how a function is used across the codebase.
        name: the function name to find callers of
        """
        callers = store.what_calls(name)
        if not callers:
            return f"No callers found for '{name}' (or it hasn't been resolved in the graph)"
        lines = [f"'{name}' is called by {len(callers)} function(s):\n"]
        for c in callers:
            lines.append(
                f"  [{c['kind']}] {c['name']}\n"
                f"    File: {c['filepath']}:{c['start_line']}-{c['end_line']}\n"
            )
        return "\n".join(lines)

    @mcp.tool()
    def what_does(name: str) -> str:
        """
        Find all functions that a given function calls.
        Useful for understanding what a function depends on.
        name: the function name to inspect
        """
        callees = store.what_does(name)
        if not callees:
            return f"No outgoing calls found from '{name}'"
        lines = [f"'{name}' calls {len(callees)} function(s):\n"]
        for c in callees:
            lines.append(
                f"  [{c['kind']}] {c['name']}\n"
                f"    File: {c['filepath']}:{c['start_line'] or '?'}-{c['end_line'] or '?'}\n"
            )
        return "\n".join(lines)

    @mcp.tool()
    def list_files() -> str:
        """
        List all files indexed in the code graph with function and class counts.
        """
        files = store.list_files()
        if not files:
            return "No files indexed."
        lines = [f"Indexed {len(files)} file(s):\n"]
        for f in files:
            lines.append(
                f"  {f['filepath']}  "
                f"({f['functions']} functions, {f['classes']} classes)"
            )
        return "\n".join(lines)

    return mcp


# Entry 
def main():
    parser = argparse.ArgumentParser(description="CodeGraph MCP Server")
    parser.add_argument("--repo",  required=True, help="Path to repo to index")
    parser.add_argument("--db",    default="codegraph.db", help="SQLite db path")
    parser.add_argument("--force", action="store_true", help="Force re-parse")
    args = parser.parse_args()

    store = build_store(args.repo, args.db, force=args.force)
    app   = create_app(store)
    app.run()


if __name__ == "__main__":
    main()
