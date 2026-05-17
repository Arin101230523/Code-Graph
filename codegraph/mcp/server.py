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
from codegraph.ingestion.git_reader import (
    get_file_history, get_function_blame,
    get_recent_changes, get_recently_changed_files,
)


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
def create_app(store: GraphStore, repo_path: str = "") -> FastMCP:
    mcp = FastMCP("codegraph")
    _repo = Path(repo_path).resolve() if repo_path else None

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

    @mcp.tool()
    def read_file(filepath: str, start_line: int = 1, end_line: int = 80) -> str:
        """
        Read the actual source code of a file in the indexed repo.
        Use find_definition first to get the filepath and line numbers, then call this.
        filepath:   relative path from the repo root (as returned by find_definition)
        start_line: line to start reading from (1-indexed, default 1)
        end_line:   line to stop reading at (default 80 — increase for longer functions)
        """
        if not _repo:
            return "No repo path configured on this server."

        full_path = _repo / filepath
        if not full_path.exists():
            return f"File not found: {filepath}"
        if not full_path.resolve().is_relative_to(_repo):
            return "Access denied: path outside repo."

        try:
            all_lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
            total     = len(all_lines)
            s = max(1, start_line) - 1
            e = min(total, end_line)
            selected  = all_lines[s:e]
            header    = f"// {filepath}  lines {s+1}–{e} of {total}\n"
            numbered  = "\n".join(f"{s+1+i:4d}  {line}" for i, line in enumerate(selected))
            return header + numbered
        except Exception as exc:
            return f"Error reading {filepath}: {exc}"

    @mcp.tool()
    def recent_changes(days: int = 14) -> str:
        """
        Show files and commits changed recently in the repo.
        Useful for understanding what's been actively worked on.
        days: how many days back to look (default 14)
        """
        if not _repo:
            return "No repo path configured."
        files = get_recently_changed_files(str(_repo), days=days)
        commits = get_recent_changes(str(_repo), max_commits=10)
        if not files and not commits:
            return "No git history found (is this a git repo?)"

        lines = [f"Recent activity (last {days} days):\n"]
        lines.append(f"Files changed ({len(files)}):")
        for f in files[:15]:
            lines.append(f"  {f['filepath']}  ({f['days_ago']}d ago by {f['last_author']})")
            lines.append(f"    {f['last_message']}")

        lines.append(f"\nRecent commits:")
        for c in commits:
            lines.append(f"  [{c['sha']}] {c['date']}  {c['author']}")
            lines.append(f"    {c['message']}")

        return "\n".join(lines)

    @mcp.tool()
    def file_history(filepath: str) -> str:
        """
        Show the git commit history for a specific file.
        filepath: relative path from repo root (as returned by find_definition)
        """
        if not _repo:
            return "No repo path configured."
        commits = get_file_history(str(_repo), filepath, max_commits=8)
        if not commits:
            return f"No git history found for {filepath}"
        lines = [f"Git history for {filepath}:\n"]
        for c in commits:
            lines.append(f"  [{c['sha']}] {c['date']}  {c['author']}  ({c['days_ago']}d ago)")
            lines.append(f"    {c['message']}")
        return "\n".join(lines)

    @mcp.tool()
    def who_changed(filepath: str, start_line: int = 1, end_line: int = 50) -> str:
        """
        Show who last changed a specific function or file region using git blame.
        Use find_definition to get the filepath and line numbers first.
        filepath:   relative path from repo root
        start_line: start of the function (from find_definition)
        end_line:   end of the function (from find_definition)
        """
        if not _repo:
            return "No repo path configured."
        info = get_function_blame(str(_repo), filepath, start_line, end_line)
        if not info:
            return f"No blame info found for {filepath}:{start_line}-{end_line}"
        return (
            f"Last change to {filepath} lines {start_line}-{end_line}:\n"
            f"  Author:  {info['last_author']}\n"
            f"  Date:    {info['last_date']} ({info['days_since_change']} days ago)\n"
            f"  Commit:  {info['last_commit']}\n"
            f"  Message: {info['last_message']}"
        )

    return mcp

# Entry 
def main():
    parser = argparse.ArgumentParser(description="CodeGraph MCP Server")
    parser.add_argument("--repo",  required=True, help="Path to repo to index")
    parser.add_argument("--db",    default="codegraph.db", help="SQLite db path")
    parser.add_argument("--force", action="store_true", help="Force re-parse")
    args = parser.parse_args()

    store = build_store(args.repo, args.db, force=args.force)
    app = create_app(store, repo_path=args.repo)
    app.run()


if __name__ == "__main__":
    main()
