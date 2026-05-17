"""
cli.py
Command-line interface for CodeGraph.

Usage:
  python -m codegraph index ./myrepo
  python -m codegraph query find_definition Router
  python -m codegraph query what_calls get_application
  python -m codegraph stats
"""

from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from codegraph.ingestion.parser import parse_repo
from codegraph.graph.store import GraphStore
from codegraph.viz import create_viz_app, GraphStore as _GS
import uvicorn, threading, webbrowser


DEFAULT_DB = "codegraph.db"


def cmd_index(args):
    repo = args.repo
    db   = args.db or DEFAULT_DB
    force = args.force

    store = GraphStore(db_path=db)
    if force or not Path(db).exists():
        print(f"Parsing repo: {repo}")
    else:
        print(f"Repo already indexed at {db}. Use --force to re-index.")
        store.load()
        s = store.stats()
        print(f"Current: {s['nodes']} nodes, {s['edges']} edges")
        return

    result = parse_repo(repo)
    store.ingest(result)
    s = store.stats()
    print(f"\n✓ Indexed {s['nodes']} nodes and {s['edges']} edges")
    print(f"Breakdown: {s['by_kind']}")
    print(f"Saved to: {db}")


def cmd_query(args):
    db = args.db or DEFAULT_DB
    store = GraphStore(db_path=db)

    if not Path(db).exists():
        print(f"No index found at {db}. Run: python -m codegraph index ./your-repo")
        sys.exit(1)

    store.load()
    name = args.name

    if args.tool == "find_definition":
        results = store.find_definition(name)
        if not results:
            print(f"No definition found for '{name}'")
            return
        print(f"\nFound {len(results)} match(es) for '{name}':\n")
        for r in results:
            print(f" [{r['kind']}] {r['name']}")
            print(f" {r['filepath']}  lines {r['start_line']}–{r['end_line']}")
            if r['docstring']:
                print(f"    > {r['docstring'][:100]}")
            print()

    elif args.tool == "what_calls":
        results = store.what_calls(name)
        if not results:
            print(f"Nothing calls '{name}' (or not found in graph)")
            return
        print(f"\n'{name}' is called by {len(results)} function(s):\n")
        for r in results:
            print(f"  [{r['kind']}] {r['name']}")
            print(f"    {r['filepath']}  lines {r['start_line']}–{r['end_line']}")
            print()

    elif args.tool == "what_does":
        results = store.what_does(name)
        if not results:
            print(f"'{name}' doesn't call anything resolvable in the graph")
            return
        print(f"\n'{name}' calls {len(results)} function(s):\n")
        for r in results:
            print(f" [{r['kind']}] {r['name']}")
            print(f" {r['filepath']}  lines {r['start_line']}–{r['end_line']}")
            print()

    elif args.tool == "list_files":
        files = store.list_files()
        print(f"\nIndexed {len(files)} file(s):\n")
        for f in files:
            print(f"  {f['filepath']}  ({f['functions']} fn, {f['classes']} cls)")


def cmd_impact(args):
    db = args.db or DEFAULT_DB
    if not Path(db).exists():
        print(f"No index at {db}. Run: python -m codegraph index ./your-repo")
        sys.exit(1)
    store = GraphStore(db_path=db)
    store.load()
    result = store.impact_analysis(args.name, max_depth=args.depth)
    if "error" in result:
        print(result["error"])
        return
    print(f"\nImpact analysis for '{result['target']}'")
    print(f"  {result['total_affected']} function(s) affected across "
          f"{result['files_affected']} file(s)\n")
    for i, level in enumerate(result["by_depth"], 1):
        print(f"Level {i} ({'direct callers' if i == 1 else 'transitive'}) — {len(level)} affected:")
        for n in level[:10]:
            print(f"  [{n['kind']}] {n['name']}  {n['filepath']}:{n['start_line']}")
        if len(level) > 10:
            print(f"  ... and {len(level)-10} more")
        print()


def cmd_viz(args):
    db = args.db or DEFAULT_DB
    if not Path(db).exists():
        print(f"No index at {db}. Run: python -m codegraph index ./your-repo")
        sys.exit(1)
    store = GraphStore(db_path=db)
    store.load()
    app = create_viz_app(store, args.repo)
    print(f"Opening http://localhost:{args.port}")
    threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="error")


def cmd_stats(args):
    db = args.db or DEFAULT_DB
    if not Path(db).exists():
        print(f"No index at {db}")
        sys.exit(1)
    store = GraphStore(db_path=db)
    store.load()
    s = store.stats()
    print(f"\nCodeGraph stats ({db})")
    print(f"  Nodes: {s['nodes']}")
    print(f"  Edges: {s['edges']}")
    print(f"  Breakdown:")
    for kind, count in s['by_kind'].items():
        print(f"{kind:12s} {count}")


def main():
    parser = argparse.ArgumentParser(
        prog="codegraph",
        description="CodeGraph — structural code intelligence for AI agents"
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite db path")
    sub = parser.add_subparsers(dest="command")

    # index
    p_index = sub.add_parser("index", help="Parse and index a repo")
    p_index.add_argument("repo", help="Path to repo")
    p_index.add_argument("--force", action="store_true", help="Re-index even if cached")
    p_index.set_defaults(func=cmd_index)

    # query
    p_query = sub.add_parser("query", help="Query the graph")
    p_query.add_argument("tool", choices=["find_definition", "what_calls", "what_does", "list_files"])
    p_query.add_argument("name", nargs="?", default="", help="Name to look up")
    p_query.set_defaults(func=cmd_query)

    # impact
    p_impact = sub.add_parser("impact", help="Show blast radius of changing a function")
    p_impact.add_argument("name", help="Function or class name to analyze")
    p_impact.add_argument("--depth", type=int, default=4, help="Max traversal depth")
    p_impact.set_defaults(func=cmd_impact)

    # viz
    p_viz = sub.add_parser("viz", help="Open interactive graph visualization")
    p_viz.add_argument("--repo", required=True, help="Path to repo")
    p_viz.add_argument("--port", type=int, default=7070)
    p_viz.set_defaults(func=cmd_viz)

    # stats
    p_stats = sub.add_parser("stats", help="Show graph statistics")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
