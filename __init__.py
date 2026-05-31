#!/usr/bin/env python3
"""Cerebro Search plugin for Hermes.

Persistent local-first search index for a local Markdown knowledge base.
Retrieval is deterministic and LLM-free; the LLM only synthesizes
after grounded candidates are returned.

Tools provided:
  - cerebro_index    : maintain the SQLite/FTS index (sync/rebuild/update_file/browse/protocol)
  - cerebro_search   : direct search against the Cerebro index
  - context_search   : joint retrieval across Cerebro + Hermes sessions + fact store
  - cerebro_watch    : detect external changes (Obsidian/manual edits) and reindex — no LLM needed
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from tools.registry import registry, tool_error

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def _cerebro_root() -> Path:
    env = os.environ.get("CEREBRO_ROOT") or os.environ.get("KNOWLEDGE_BASE_ROOT")
    if env:
        return Path(env).expanduser()
    candidates = [
        Path.home() / "Documents" / "Cerebro",
        Path.home() / "Documents" / "KnowledgeBase",
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


SCRIPT = Path(os.environ.get("CEREBRO_INDEX_SCRIPT", Path(__file__).parent / "scripts" / "cerebro_search_index.py"))
DB_PATH = _cerebro_root() / ".indices" / "cerebro_search.sqlite"
WATCH_STATE_PATH = _cerebro_root() / ".indices" / "cerebro_watch_state.json"

OLLAMA_HOST = os.environ.get("CEREBRO_OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("CEREBRO_EMBED_MODEL", "nomic-embed-text:latest")


# ---------------------------------------------------------------------------
# Index module loader
# ---------------------------------------------------------------------------

def _load_index_module():
    if not SCRIPT.exists():
        raise FileNotFoundError(f"Cerebro index script not found: {SCRIPT}")
    spec = importlib.util.spec_from_file_location("cerebro_search_index_runtime", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {SCRIPT}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# cerebro_index
# ---------------------------------------------------------------------------

def _cerebro_index_sync(rebuild: bool = False, embeddings: bool = False) -> dict:
    mod = _load_index_module()
    result = mod.sync(
        root=_cerebro_root(),
        db_path=DB_PATH,
        rebuild=rebuild,
        embeddings=embeddings,
        model=OLLAMA_MODEL,
        ollama_host=OLLAMA_HOST,
    )
    return result


def _cerebro_index_update_file(filepath: str) -> dict:
    mod = _load_index_module()
    path = Path(filepath).expanduser().resolve()
    result = mod.index_file(path, _cerebro_root(), DB_PATH, embeddings=False)
    return result


def _cerebro_index_browse(limit: int = 10) -> dict:
    mod = _load_index_module()
    return mod.browse(limit=limit, db_path=DB_PATH)


def _cerebro_index_protocol() -> dict:
    mod = _load_index_module()
    return mod.protocol()


def cerebro_index_tool(
    action: str = "sync",
    rebuild: bool = False,
    update_file: str = "",
    embeddings: bool = False,
    limit: int = 10,
) -> str:
    """Maintain the Cerebro SQLite/FTS search index.

    Args:
        action: sync | rebuild | update_file | browse | protocol
        rebuild: if True, drop and rebuild the full index (used with sync action)
        update_file: absolute path of a single file to reindex
        embeddings: if True, also generate Ollama embeddings during sync/rebuild
        limit: max results for browse
    """
    try:
        if action == "protocol":
            result = _cerebro_index_protocol()
        elif action == "browse":
            result = _cerebro_index_browse(limit=limit)
        elif action == "update_file":
            if not update_file:
                return tool_error("update_file requires a file path")
            result = _cerebro_index_update_file(update_file)
        elif action == "rebuild":
            result = _cerebro_index_sync(rebuild=True, embeddings=embeddings)
        else:
            result = _cerebro_index_sync(rebuild=rebuild, embeddings=embeddings)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return tool_error(str(e))


# ---------------------------------------------------------------------------
# cerebro_search
# ---------------------------------------------------------------------------

def cerebro_search_tool(
    query: str = "",
    limit: int = 5,
    sort: str | None = None,
    vector: bool = False,
    file_id: int | None = None,
    chunk_id: int | None = None,
    path: str | None = None,
    window: int = 5,
) -> str:
    """Search the Cerebro index (lexical FTS + optional Ollama vector).

    Modes (inferred from args):
      - discovery:  query + optional sort/vector
      - scroll:     file_id/path + chunk_id anchor + window
      - browse:     no query → recent files

    Args:
        query: search terms (empty = browse mode)
        limit: max results (1-20)
        sort: newest | oldest | None
        vector: also use Ollama embeddings for semantic search
        file_id: scroll mode — anchor by file DB id
        chunk_id: scroll mode — anchor by chunk DB id
        path: scroll mode — anchor by file path
        window: scroll context window in chunks (1-20)
    """
    try:
        mod = _load_index_module()
        if file_id is not None or path or chunk_id is not None:
            result = mod.scroll(file_id=file_id, path=path, chunk_id=chunk_id, window=window, db_path=DB_PATH)
        elif query.strip():
            result = mod.discover(
                query.strip(),
                limit=max(1, min(int(limit), 20)),
                sort=sort,
                db_path=DB_PATH,
                vector=vector,
                model=OLLAMA_MODEL,
                ollama_host=OLLAMA_HOST,
            )
        else:
            result = mod.browse(limit=max(1, min(int(limit), 50)), db_path=DB_PATH)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return tool_error(str(e))


# ---------------------------------------------------------------------------
# context_search  (joint: Cerebro + sessions + fact_store)
# ---------------------------------------------------------------------------

_fact_search_fn = None
_session_search_fn = None


def _lazy_load_fact_search():
    global _fact_search_fn
    if _fact_search_fn is not None:
        return _fact_search_fn
    try:
        from plugins.memory.holographic import search_fact_store
        _fact_search_fn = search_fact_store
    except Exception:
        _fact_search_fn = None
    return _fact_search_fn


def _lazy_load_session_search():
    global _session_search_fn
    if _session_search_fn is not None:
        return _session_search_fn
    try:
        from tools.session_search_tool import session_search as _ss
        _session_search_fn = _ss
    except Exception:
        _session_search_fn = None
    return _session_search_fn


def context_search_tool(
    query: str,
    sources: list[str] | None = None,
    limit: int = 5,
    sort: str | None = None,
    vector: bool = False,
) -> str:
    """Joint retrieval across Cerebro, Hermes sessions, and holographic fact store.

    Args:
        query: natural-language search terms
        sources: optional subset of ["cerebro", "sessions", "facts"]
        limit: max results per source
        sort: newest | oldest (passed to session_search)
        vector: use Ollama embeddings for Cerebro search
    """
    try:
        sources = sources or ["cerebro", "sessions", "facts"]
        limit = max(1, min(int(limit), 20))

        result: dict[str, Any] = {
            "ok": True,
            "query": query,
            "sources": sources,
            "results": {},
        }

        if "cerebro" in sources:
            try:
                mod = _load_index_module()
                cerebro_res = mod.discover(
                    query, limit=limit, sort=None, db_path=DB_PATH,
                    vector=vector, model=OLLAMA_MODEL, ollama_host=OLLAMA_HOST,
                )
                result["results"]["cerebro"] = cerebro_res
            except Exception as exc:
                result["results"]["cerebro"] = {"ok": False, "error": str(exc)}

        if "sessions" in sources:
            try:
                ss = _lazy_load_session_search()
                if ss is not None:
                    session_res = ss(query=query, limit=limit, sort=sort)
                    result["results"]["sessions"] = {"ok": True, "count": len(session_res), "results": session_res}
                else:
                    result["results"]["sessions"] = {"ok": False, "error": "session_search not available"}
            except Exception as exc:
                result["results"]["sessions"] = {"ok": False, "error": str(exc)}

        if "facts" in sources:
            try:
                fs = _lazy_load_fact_search()
                if fs is not None:
                    fact_res = fs(query=query, limit=limit)
                    result["results"]["facts"] = fact_res
                else:
                    # Fallback: direct SQL
                    fact_db = get_hermes_home() / "memory_store.db"
                    if not fact_db.exists():
                        result["results"]["facts"] = {"ok": False, "error": "memory_store.db not found"}
                    else:
                        con = sqlite3.connect(str(fact_db), timeout=5)
                        con.row_factory = sqlite3.Row
                        rows = con.execute(
                            "SELECT f.fact_id, f.content, f.category, f.tags, f.trust_score, "
                            "snippet(facts_fts, 0, '>>>', '<<<', chr(8230), 32) AS snippet, rank "
                            "FROM facts_fts JOIN facts f ON f.fact_id = facts_fts.rowid "
                            "WHERE facts_fts MATCH ? AND f.trust_score >= 0.3 "
                            "ORDER BY rank, f.trust_score DESC LIMIT ?",
                            (query, limit),
                        ).fetchall()
                        con.close()
                        result["results"]["facts"] = {
                            "ok": True,
                            "count": len(rows),
                            "results": [{"source": "fact_store", **dict(r)} for r in rows],
                        }
            except Exception as exc:
                result["results"]["facts"] = {"ok": False, "error": str(exc)}

        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return tool_error(str(e))


# ---------------------------------------------------------------------------
# cerebro_watch — detect external changes, no LLM
# ---------------------------------------------------------------------------

def _file_hash(path: Path) -> str:
    try:
        data = path.read_bytes()
        return hashlib.sha256(data).hexdigest()
    except OSError:
        return ""


def _iter_cerebro_files():
    root = _cerebro_root()
    exts = {".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".csv", ".py", ".ps1", ".sh", ".bat", ".cmd"}
    skip = {".git", ".obsidian", ".trash", ".indices", "node_modules", "__pycache__"}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in skip for part in p.parts):
            continue
        if p.suffix.lower() not in exts:
            continue
        yield p


def _load_watch_state() -> dict:
    if WATCH_STATE_PATH.exists():
        try:
            return json.loads(WATCH_STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"files": {}, "last_check": None}


def _save_watch_state(state: dict) -> None:
    WATCH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCH_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def cerebro_watch_tool() -> str:
    """Detect external changes to Cerebro files and update the index.

    Compares file hashes (SHA-256) and mtimes against last saved state.
    Any new, modified, or deleted file triggers a reindex of that file.

    This tool does NOT use an LLM — it is a pure filesystem/hash comparison.

    Returns a summary of changed files and reindex results.
    """
    try:
        state = _load_watch_state()
        prev_files: dict = state.get("files", {})
        curr_files: dict[str, dict] = {}
        changed = {"new": [], "modified": [], "deleted": [], "errors": [], "reindexed": []}

        # Scan current filesystem
        for p in _iter_cerebro_files():
            rel = str(p.relative_to(_cerebro_root()))
            try:
                st = p.stat()
                curr_files[rel] = {"mtime": st.st_mtime, "size": st.st_size}
            except OSError:
                continue

        # Detect new and possibly modified (mtime changed)
        for rel, info in curr_files.items():
            if rel not in prev_files:
                changed["new"].append(rel)
            elif info["mtime"] != prev_files[rel].get("mtime") or info["size"] != prev_files[rel].get("size"):
                changed["modified"].append(rel)

        # Detect deleted
        for rel in prev_files:
            if rel not in curr_files:
                changed["deleted"].append(rel)

        total_changes = len(changed["new"]) + len(changed["modified"]) + len(changed["deleted"])

        if total_changes == 0:
            state["last_check"] = time.time()
            _save_watch_state(state)
            return json.dumps({
                "ok": True,
                "changed": False,
                "message": "No external changes detected. Index is up to date.",
                "files_scanned": len(curr_files),
            }, ensure_ascii=False, indent=2)

        # Reindex changed files
        mod = _load_index_module()
        for rel in changed["new"] + changed["modified"]:
            fpath = _cerebro_root() / rel
            try:
                res = mod.index_file(fpath, _cerebro_root(), DB_PATH, embeddings=False)
                changed["reindexed"].append({"path": rel, "action": res.get("action", "unknown")})
            except Exception as exc:
                changed["errors"].append({"path": rel, "error": str(exc)})

        # Delete removed files from index
        for rel in changed["deleted"]:
            try:
                con = sqlite3.connect(str(DB_PATH), timeout=10)
                row = con.execute("SELECT id FROM files WHERE relpath=?", (rel,)).fetchone()
                if row:
                    fid = row[0]
                    # Delete FTS entries for this file's chunks
                    chunk_ids = [r[0] for r in con.execute("SELECT id FROM chunks WHERE file_id=?", (fid,)).fetchall()]
                    for cid in chunk_ids:
                        try:
                            con.execute("DELETE FROM chunks_fts WHERE rowid=?", (cid,))
                        except sqlite3.DatabaseError:
                            pass
                    con.execute("DELETE FROM embeddings WHERE chunk_id IN (SELECT id FROM chunks WHERE file_id=?)", (fid,))
                    con.execute("DELETE FROM tags WHERE file_id=?", (fid,))
                    con.execute("DELETE FROM links WHERE file_id=?", (fid,))
                    con.execute("DELETE FROM chunks WHERE file_id=?", (fid,))
                    con.execute("DELETE FROM files WHERE id=?", (fid,))
                    con.commit()
                con.close()
                changed["reindexed"].append({"path": rel, "action": "deleted"})
            except Exception as exc:
                changed["errors"].append({"path": rel, "error": str(exc)})

        # Save new state (hash only changed/new/deleted files to save time)
        new_files_state = {}
        for rel, info in curr_files.items():
            p = _cerebro_root() / rel
            new_files_state[rel] = {"mtime": info["mtime"], "size": info["size"], "sha256": _file_hash(p)}
        state["files"] = new_files_state
        state["last_check"] = time.time()
        _save_watch_state(state)

        return json.dumps({
            "ok": True,
            "changed": True,
            "summary": {
                "new": len(changed["new"]),
                "modified": len(changed["modified"]),
                "deleted": len(changed["deleted"]),
                "reindexed": len(changed["reindexed"]),
                "errors": len(changed["errors"]),
            },
            "details": changed,
            "files_scanned": len(curr_files),
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return tool_error(str(e))


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def _tool_schema(name: str, description: str, properties: dict | None = None, required: list[str] | None = None) -> dict:
    schema = {"name": name, "description": description, "parameters": {"type": "object", "properties": properties or {}}}
    if required:
        schema["parameters"]["required"] = required
    return schema


def _register_tool(ctx, name: str, handler, description: str, properties: dict | None = None, required: list[str] | None = None) -> None:
    schema = _tool_schema(name, description, properties, required)
    try:
        ctx.register_tool(name=name, toolset="cerebro_search", schema=schema, handler=handler)
    except TypeError:
        # Compatibility with older Hermes plugin APIs.
        ctx.register_tool(name, handler)


def register(ctx) -> None:
    _register_tool(ctx, "cerebro_index", cerebro_index_tool, "Maintain the local Markdown knowledge-base SQLite/FTS index.", {"action": {"type": "string", "default": "sync"}, "rebuild": {"type": "boolean"}, "update_file": {"type": "string"}, "embeddings": {"type": "boolean"}, "limit": {"type": "integer", "default": 10}})
    _register_tool(ctx, "cerebro_search", cerebro_search_tool, "Search the local Markdown knowledge-base index.", {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}, "sort": {"type": "string"}, "vector": {"type": "boolean"}, "file_id": {"type": "integer"}, "chunk_id": {"type": "integer"}, "path": {"type": "string"}, "window": {"type": "integer", "default": 5}})
    _register_tool(ctx, "context_search", context_search_tool, "Joint retrieval across the knowledge-base index, Hermes sessions, and optional fact store.", {"query": {"type": "string"}, "sources": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer", "default": 5}, "sort": {"type": "string"}, "vector": {"type": "boolean"}}, ["query"])
    _register_tool(ctx, "cerebro_watch", cerebro_watch_tool, "Detect external knowledge-base changes and update the local index.", {})
