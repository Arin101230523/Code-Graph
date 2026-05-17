"""
git_reader.py
Reads git history and annotates graph nodes with commit metadata.
Requires gitpython: pip install gitpython
"""

from __future__ import annotations
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import git


def _repo_or_none(repo_path: str):
    if git is None:
        return None
    try:
        return git.Repo(repo_path, search_parent_directories=True)
    except Exception:
        return None


def get_file_history(repo_path: str, filepath: str, max_commits: int = 5) -> list[dict]:
    """
    Return the last N commits that touched a file.
    filepath: relative to repo root
    """
    repo = _repo_or_none(repo_path)
    if not repo:
        return []

    try:
        commits = list(repo.iter_commits(paths=filepath, max_count=max_commits))
        result = []
        for c in commits:
            result.append({
                "sha":     c.hexsha[:8],
                "message": c.message.strip().split("\n")[0][:100],
                "author":  c.author.name,
                "date":    datetime.fromtimestamp(c.committed_date, tz=timezone.utc).strftime("%Y-%m-%d"),
                "days_ago": (datetime.now(tz=timezone.utc) - datetime.fromtimestamp(
                    c.committed_date, tz=timezone.utc)).days,
            })
        return result
    except Exception:
        return []


def get_function_blame(repo_path: str, filepath: str, start_line: int, end_line: int) -> dict:
    """
    Run git blame on a line range to find who last touched a function.
    Returns the most recent commit info touching those lines.
    """
    repo = _repo_or_none(repo_path)
    if not repo:
        return {}

    try:
        blame = repo.blame("HEAD", filepath)
        # blame returns list of (commit, lines) tuples
        most_recent = None
        for commit, lines in blame:
            # figure out which lines this chunk covers
            if most_recent is None or commit.committed_date > most_recent.committed_date:
                most_recent = commit

        if not most_recent:
            return {}

        return {
            "last_author":  most_recent.author.name,
            "last_commit":  most_recent.hexsha[:8],
            "last_message": most_recent.message.strip().split("\n")[0][:100],
            "last_date":    datetime.fromtimestamp(
                most_recent.committed_date, tz=timezone.utc).strftime("%Y-%m-%d"),
            "days_since_change": (datetime.now(tz=timezone.utc) - datetime.fromtimestamp(
                most_recent.committed_date, tz=timezone.utc)).days,
        }
    except Exception:
        return {}


def get_recent_changes(repo_path: str, max_commits: int = 10) -> list[dict]:
    """
    Return the N most recent commits across the whole repo with files changed.
    """
    repo = _repo_or_none(repo_path)
    if not repo:
        return []

    try:
        result = []
        for c in repo.iter_commits(max_count=max_commits):
            files_changed = list(c.stats.files.keys())[:10]  # cap at 10 files per commit
            result.append({
                "sha":           c.hexsha[:8],
                "message":       c.message.strip().split("\n")[0][:100],
                "author":        c.author.name,
                "date":          datetime.fromtimestamp(
                    c.committed_date, tz=timezone.utc).strftime("%Y-%m-%d"),
                "days_ago":      (datetime.now(tz=timezone.utc) - datetime.fromtimestamp(
                    c.committed_date, tz=timezone.utc)).days,
                "files_changed": files_changed,
            })
        return result
    except Exception:
        return []


def get_recently_changed_files(repo_path: str, days: int = 30) -> list[dict]:
    """
    Return files changed in the last N days, sorted by recency.
    """
    repo = _repo_or_none(repo_path)
    if not repo:
        return []

    try:
        cutoff = datetime.now(tz=timezone.utc).timestamp() - (days * 86400)
        seen: dict[str, dict] = {}

        for c in repo.iter_commits():
            if c.committed_date < cutoff:
                break
            for fpath in c.stats.files:
                if fpath not in seen:
                    seen[fpath] = {
                        "filepath":   fpath,
                        "last_author": c.author.name,
                        "last_commit": c.hexsha[:8],
                        "last_message": c.message.strip().split("\n")[0][:80],
                        "days_ago":   (datetime.now(tz=timezone.utc) - datetime.fromtimestamp(
                            c.committed_date, tz=timezone.utc)).days,
                    }

        return sorted(seen.values(), key=lambda x: x["days_ago"])
    except Exception:
        return []
