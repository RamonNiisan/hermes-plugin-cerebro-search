#!/usr/bin/env python3
"""Cerebro Search Index — local-first FTS + optional Ollama embeddings.

Design:
- Markdown/text files in the knowledge base stay the source of truth.
- SQLite is only an index/cache: metadata, FTS chunks, wikilinks, tags, hashes,
  and optional embedding vectors.
- Search is deterministic/no-LLM. The LLM consumes the returned snippets/files
  only after retrieval.

Modes:
- sync/rebuild: maintain the index.
- update-file: incremental update for a changed file.
- query: hybrid lexical + vector retrieval.
- scroll/browse: session_search-like drilldown.
- protocol: print the retrieval protocol.
"""
from __future__ import annotations

import argparse
import array
import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


def _default_root() -> Path:
    env = os.environ.get("CEREBRO_ROOT") or os.environ.get("OBSIDIAN_VAULT_PATH")
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


ROOT = _default_root()
INDEX_DIR = ROOT / ".indices"
DB = INDEX_DIR / "cerebro_search.sqlite"
TEXT_EXTS = {".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".csv", ".py", ".ps1", ".sh", ".bat", ".cmd"}
SKIP_DIRS = {".git", ".obsidian", ".trash", ".indices", "node_modules", "__pycache__"}
MAX_FILE_BYTES = 5_000_000
CHUNK_CHARS = 1800
OVERLAP = 180
DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST") or os.environ.get("CEREBRO_OLLAMA_HOST") or "http://127.0.0.1:11434"
DEFAULT_EMBED_MODEL = os.environ.get("CEREBRO_EMBED_MODEL") or "nomic-embed-text:latest"
PROTOCOL_VERSION = 2


@dataclass
class Chunk:
    index: int
    heading: str
    content: str
    start_line: int
    end_line: int


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_text(path: Path) -> str:
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        return ""
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


def parse_frontmatter(text: str) -> dict:
    fm = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, flags=re.S)
    if not fm:
        return {}
    out: dict[str, object] = {}
    for line in fm.group(1).splitlines():
        if ":" not in line or line.startswith(" "):
            continue
        k, v = line.split(":", 1)
        k = k.strip()
        v = v.strip().strip('"\'')
        if not k:
            continue
        if v.startswith("[") and v.endswith("]"):
            out[k] = [x.strip().strip('"\'') for x in v[1:-1].split(",") if x.strip()]
        else:
            out[k] = v
    return out


def title_for(path: Path, text: str, fm: dict | None = None) -> str:
    fm = fm or parse_frontmatter(text)
    title = fm.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()[:180]
    for line in text.splitlines()[:100]:
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()[:180] or path.stem
    return path.stem.replace("-", " ").replace("_", " ")[:180]


def tags_for(text: str, fm: dict | None = None) -> list[str]:
    fm = fm or parse_frontmatter(text)
    tags: list[str] = []
    raw = fm.get("tags")
    if isinstance(raw, list):
        tags.extend(str(x).strip() for x in raw if str(x).strip())
    elif isinstance(raw, str) and raw.strip():
        tags.extend(x.strip() for x in re.split(r"[,\s]+", raw) if x.strip())
    tags.extend(m.group(1).strip(".,;:!?)]") for m in re.finditer(r"(?<!\w)#([\wÀ-ÿ/-]+)", text))
    seen = []
    for t in tags:
        if t and t not in seen:
            seen.append(t[:80])
    return seen


def wikilinks_for(text: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    for m in re.finditer(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]", text):
        target = m.group(1).strip()
        label = (m.group(2) or target).strip()
        if target:
            links.append((target[:300], label[:300]))
    return links


def classify(relpath: str) -> str:
    parts = relpath.replace("\\", "/").split("/")
    known = {"Fontes", "Memória", "Estado", "Decisões", "Visões", "Guias", "Protocolos", "Ferramentas", "Logs", "Arquivo", "Projetos", "Entrada", "Pessoas"}
    for p in parts:
        if p in known:
            return p
    return parts[0] if parts else "Raiz"


def iter_files(root: Path = ROOT) -> Iterable[Path]:
    if not root.exists():
        return
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in TEXT_EXTS:
            continue
        yield p


def line_for_offset(line_starts: list[int], offset: int) -> int:
    # Small/chunked files: linear is fine and avoids bisect import in older envs.
    lo = 0
    hi = len(line_starts)
    while lo < hi:
        mid = (lo + hi) // 2
        if line_starts[mid] <= offset:
            lo = mid + 1
        else:
            hi = mid
    return max(1, lo)


def current_heading(prefix: str) -> str:
    for line in reversed(prefix.splitlines()[-100:]):
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()[:180]
    return ""


def chunk_text(text: str) -> list[Chunk]:
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    if not text:
        return []
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())
    chunks: list[Chunk] = []
    i = 0
    n = len(text)
    idx = 0
    while i < n:
        end = min(n, i + CHUNK_CHARS)
        cut = text.rfind("\n\n", i + 700, end)
        if cut == -1:
            cut = text.rfind(". ", i + 700, end)
        if cut == -1:
            cut = end
        body = text[i:cut].strip()
        if body:
            chunks.append(Chunk(idx, current_heading(text[: i + 1]), body, line_for_offset(starts, i), line_for_offset(starts, cut)))
            idx += 1
        if cut >= n:
            break
        i = max(cut - OVERLAP, i + 1)
    return chunks


def connect(db_path: Path = DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        con.execute("PRAGMA journal_mode=DELETE")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def setup(con: sqlite3.Connection, rebuild: bool = False) -> None:
    if rebuild:
        con.executescript(
            """
            DROP TABLE IF EXISTS embeddings;
            DROP TABLE IF EXISTS links;
            DROP TABLE IF EXISTS tags;
            DROP TABLE IF EXISTS chunks_fts;
            DROP TABLE IF EXISTS chunks;
            DROP TABLE IF EXISTS files;
            DROP TABLE IF EXISTS meta;
            """
        )
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY,
            path TEXT UNIQUE NOT NULL,
            relpath TEXT NOT NULL,
            title TEXT,
            layer TEXT,
            size INTEGER,
            sha256 TEXT,
            mtime REAL,
            chars INTEGER,
            chunk_count INTEGER,
            frontmatter_json TEXT DEFAULT '{}',
            tags_text TEXT DEFAULT '',
            indexed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            heading TEXT,
            content TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            content, title, path, layer, heading,
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TABLE IF NOT EXISTS embeddings (
            chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
            model TEXT NOT NULL,
            dim INTEGER NOT NULL,
            vector BLOB NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tags (
            file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            tag TEXT NOT NULL,
            PRIMARY KEY(file_id, tag)
        );
        CREATE TABLE IF NOT EXISTS links (
            file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            target TEXT NOT NULL,
            label TEXT,
            PRIMARY KEY(file_id, target, label)
        );
        CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime DESC);
        CREATE INDEX IF NOT EXISTS idx_files_layer ON files(layer);
        CREATE INDEX IF NOT EXISTS idx_files_sha ON files(sha256);
        CREATE INDEX IF NOT EXISTS idx_chunks_file_index ON chunks(file_id, chunk_index);
        CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
        CREATE INDEX IF NOT EXISTS idx_links_target ON links(target);
        """
    )
    # Migrate old indexes in-place.
    cols = {r[1] for r in con.execute("PRAGMA table_info(files)").fetchall()}
    if "frontmatter_json" not in cols:
        con.execute("ALTER TABLE files ADD COLUMN frontmatter_json TEXT DEFAULT '{}'")
    if "tags_text" not in cols:
        con.execute("ALTER TABLE files ADD COLUMN tags_text TEXT DEFAULT ''")
    ccols = {r[1] for r in con.execute("PRAGMA table_info(chunks)").fetchall()}
    if "start_line" not in ccols:
        con.execute("ALTER TABLE chunks ADD COLUMN start_line INTEGER")
    if "end_line" not in ccols:
        con.execute("ALTER TABLE chunks ADD COLUMN end_line INTEGER")
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('protocol_version',?)", (str(PROTOCOL_VERSION),))
    con.commit()


def _delete_file_rows(con: sqlite3.Connection, file_id: int) -> None:
    chunk_ids = [r[0] for r in con.execute("SELECT id FROM chunks WHERE file_id=?", (file_id,)).fetchall()]
    for cid in chunk_ids:
        try:
            con.execute("DELETE FROM chunks_fts WHERE rowid=?", (cid,))
        except sqlite3.DatabaseError:
            pass
    con.execute("DELETE FROM embeddings WHERE chunk_id IN (SELECT id FROM chunks WHERE file_id=?)", (file_id,))
    con.execute("DELETE FROM tags WHERE file_id=?", (file_id,))
    con.execute("DELETE FROM links WHERE file_id=?", (file_id,))
    con.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
    con.execute("DELETE FROM files WHERE id=?", (file_id,))


def _pack_vector(vec: list[float]) -> bytes:
    arr = array.array("f", (float(x) for x in vec))
    return arr.tobytes()


def _unpack_vector(blob: bytes) -> list[float]:
    arr = array.array("f")
    arr.frombytes(blob)
    return list(arr)


def ollama_embed(text: str, model: str = DEFAULT_EMBED_MODEL, host: str = DEFAULT_OLLAMA_HOST, timeout: int = 20) -> list[float]:
    hosts = [host.rstrip("/")]
    for fallback in ("http://127.0.0.1:11434",):
        if fallback not in hosts:
            hosts.append(fallback)
    errors: list[str] = []
    for h in hosts:
        payload = json.dumps({"model": model, "prompt": text}).encode("utf-8")
        req = urllib.request.Request(h + "/api/embeddings", data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            emb = data.get("embedding")
            if isinstance(emb, list):
                return [float(x) for x in emb]
        except Exception as e:
            errors.append(f"{h}/api/embeddings: {e}")
            # Newer Ollama /api/embed fallback.
            try:
                payload = json.dumps({"model": model, "input": text}).encode("utf-8")
                req = urllib.request.Request(h + "/api/embed", data=payload, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                embs = data.get("embeddings")
                if isinstance(embs, list) and embs and isinstance(embs[0], list):
                    return [float(x) for x in embs[0]]
            except Exception as e2:
                errors.append(f"{h}/api/embed: {e2}")
    raise RuntimeError("Ollama embedding failed: " + " | ".join(errors[-4:]))


def index_file(path: Path, root: Path = ROOT, db_path: Path = DB, *, embeddings: bool = False, model: str = DEFAULT_EMBED_MODEL, ollama_host: str = DEFAULT_OLLAMA_HOST) -> dict:
    con = connect(db_path)
    setup(con)
    path = path.expanduser().resolve()
    root = root.expanduser().resolve()
    old = con.execute("SELECT id, sha256 FROM files WHERE path=? OR relpath=?", (str(path), str(path))).fetchone()
    if not path.exists() or not path.is_file() or path.suffix.lower() not in TEXT_EXTS or any(part in SKIP_DIRS for part in path.parts):
        if old:
            _delete_file_rows(con, int(old["id"]))
            con.commit(); con.close()
            return {"ok": True, "action": "deleted", "path": str(path)}
        con.close(); return {"ok": True, "action": "ignored", "path": str(path)}
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        con.close(); return {"ok": True, "action": "skipped_large", "path": str(path), "size": len(data)}
    digest = sha256_bytes(data)
    if old and old["sha256"] == digest:
        con.close(); return {"ok": True, "action": "unchanged", "path": str(path)}
    text = read_text(path)
    if not text.strip():
        if old:
            _delete_file_rows(con, int(old["id"]))
        con.commit(); con.close(); return {"ok": True, "action": "skipped_empty", "path": str(path)}
    if old:
        _delete_file_rows(con, int(old["id"]))
    try:
        rel = str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        rel = str(path)
    fm = parse_frontmatter(text)
    title = title_for(path, text, fm)
    tag_list = tags_for(text, fm)
    layer = classify(rel)
    chunks = chunk_text(text)
    cur = con.execute(
        "INSERT INTO files(path, relpath, title, layer, size, sha256, mtime, chars, chunk_count, frontmatter_json, tags_text, indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(path), rel, title, layer, len(data), digest, path.stat().st_mtime, len(text), len(chunks), json.dumps(fm, ensure_ascii=False), ",".join(tag_list), now_iso()),
    )
    file_id = int(cur.lastrowid)
    for tag in tag_list:
        con.execute("INSERT OR IGNORE INTO tags(file_id, tag) VALUES (?,?)", (file_id, tag))
    for target, label in wikilinks_for(text):
        con.execute("INSERT OR IGNORE INTO links(file_id, target, label) VALUES (?,?,?)", (file_id, target, label))
    embedded = 0
    embed_error = None
    for ch in chunks:
        cur2 = con.execute(
            "INSERT INTO chunks(file_id, chunk_index, heading, content, start_line, end_line) VALUES (?,?,?,?,?,?)",
            (file_id, ch.index, ch.heading, ch.content, ch.start_line, ch.end_line),
        )
        cid = int(cur2.lastrowid)
        con.execute("INSERT INTO chunks_fts(rowid, content, title, path, layer, heading) VALUES (?,?,?,?,?,?)", (cid, ch.content, title, rel, layer, ch.heading))
        if embeddings:
            try:
                vec = ollama_embed((title + "\n" + ch.heading + "\n" + ch.content)[:4000], model=model, host=ollama_host)
                con.execute("INSERT OR REPLACE INTO embeddings(chunk_id, model, dim, vector, created_at) VALUES (?,?,?,?,?)", (cid, model, len(vec), _pack_vector(vec), now_iso()))
                embedded += 1
            except Exception as e:
                embed_error = str(e)
                embeddings = False  # fail soft; keep FTS index
    con.commit(); con.close()
    return {"ok": True, "action": "indexed", "path": str(path), "relpath": rel, "chunks": len(chunks), "embedded_chunks": embedded, "embedding_error": embed_error}


def sync(root: Path = ROOT, db_path: Path = DB, *, rebuild: bool = False, embeddings: bool = False, model: str = DEFAULT_EMBED_MODEL, ollama_host: str = DEFAULT_OLLAMA_HOST) -> dict:
    con = connect(db_path)
    setup(con, rebuild=rebuild)
    con.close()
    started = time.time()
    indexed = unchanged = deleted = skipped = embedded = 0
    errors: list[dict] = []
    seen: set[str] = set()
    for path in iter_files(root) or []:
        try:
            res = index_file(path, root, db_path, embeddings=embeddings, model=model, ollama_host=ollama_host)
            seen.add(str(path.expanduser().resolve()))
            action = res.get("action")
            indexed += action == "indexed"
            unchanged += action == "unchanged"
            skipped += str(action).startswith("skipped") or action == "ignored"
            embedded += int(res.get("embedded_chunks") or 0)
        except Exception as e:
            errors.append({"path": str(path), "error": str(e)})
    con = connect(db_path)
    rows = con.execute("SELECT id, path FROM files").fetchall()
    for r in rows:
        if str(Path(r["path"]).expanduser().resolve()) not in seen and not Path(r["path"]).exists():
            _delete_file_rows(con, int(r["id"])); deleted += 1
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_sync',?)", (now_iso(),))
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('embedding_model',?)", (model if embeddings else "",))
    con.commit(); con.close()
    out = {"ok": True, "root": str(root), "db": str(db_path), "indexed": indexed, "unchanged": unchanged, "deleted": deleted, "skipped": skipped, "embedded_chunks": embedded, "errors": errors[:20], "seconds": round(time.time() - started, 3)}
    db_path.parent.mkdir(parents=True, exist_ok=True)
    (db_path.parent / "cerebro_search_manifest.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def rebuild(root: Path = ROOT, db_path: Path = DB, *, embeddings: bool = False, model: str = DEFAULT_EMBED_MODEL, ollama_host: str = DEFAULT_OLLAMA_HOST) -> dict:
    return sync(root, db_path, rebuild=True, embeddings=embeddings, model=model, ollama_host=ollama_host)


def shape_chunk(row: sqlite3.Row, anchor_id: int | None = None) -> dict:
    entry = {"chunk_id": row["id"], "chunk_index": row["chunk_index"], "heading": row["heading"] or "", "start_line": row["start_line"], "end_line": row["end_line"], "content": row["content"] or ""}
    if anchor_id is not None and entry["chunk_id"] == anchor_id:
        entry["anchor"] = True
    return entry


def bookends(con: sqlite3.Connection, file_id: int, anchor_chunk_id: int, window: int = 2, bookend: int = 2) -> dict:
    anchor = con.execute("SELECT * FROM chunks WHERE id=? AND file_id=?", (anchor_chunk_id, file_id)).fetchone()
    if not anchor:
        return {"window": [], "bookend_start": [], "bookend_end": [], "chunks_before": 0, "chunks_after": 0}
    idx = anchor["chunk_index"]
    rows = con.execute("SELECT * FROM chunks WHERE file_id=? AND chunk_index BETWEEN ? AND ? ORDER BY chunk_index", (file_id, max(0, idx - window), idx + window)).fetchall()
    start = con.execute("SELECT * FROM chunks WHERE file_id=? AND chunk_index < ? ORDER BY chunk_index ASC LIMIT ?", (file_id, max(0, idx - window), bookend)).fetchall()
    end = con.execute("SELECT * FROM chunks WHERE file_id=? AND chunk_index > ? ORDER BY chunk_index DESC LIMIT ?", (file_id, idx + window, bookend)).fetchall()
    before = con.execute("SELECT COUNT(*) FROM chunks WHERE file_id=? AND chunk_index < ?", (file_id, idx)).fetchone()[0]
    after = con.execute("SELECT COUNT(*) FROM chunks WHERE file_id=? AND chunk_index > ?", (file_id, idx)).fetchone()[0]
    return {"window": [shape_chunk(r, anchor_id=anchor_chunk_id) for r in rows], "bookend_start": [shape_chunk(r) for r in start], "bookend_end": [shape_chunk(r) for r in reversed(end)], "chunks_before": before, "chunks_after": after}


def _fts_query(query: str, limit: int, sort: str | None, db_path: Path = DB) -> list[dict]:
    con = connect(db_path); setup(con)
    order = "rank"
    if sort == "newest": order = "f.mtime DESC, rank"
    elif sort == "oldest": order = "f.mtime ASC, rank"
    sql = f"""
        SELECT c.id AS chunk_id, c.file_id, c.chunk_index, c.heading, c.start_line, c.end_line,
               f.path, f.relpath, f.title, f.layer, f.mtime, f.chunk_count,
               snippet(chunks_fts, 0, '>>>', '<<<', '…', 32) AS snippet,
               rank
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN files f ON f.id = c.file_id
        WHERE chunks_fts MATCH ?
        ORDER BY {order}
        LIMIT ?
    """
    raw = con.execute(sql, (query, limit * 8)).fetchall()
    seen=set(); results=[]
    for r in raw:
        if r["file_id"] in seen: continue
        seen.add(r["file_id"])
        slices = bookends(con, r["file_id"], r["chunk_id"], window=2, bookend=2)
        results.append({"source": "cerebro_fts", "score": float(r["rank"]), "file_id": r["file_id"], "path": r["path"], "relpath": r["relpath"], "title": r["title"], "layer": r["layer"], "mtime": r["mtime"], "chunk_count": r["chunk_count"], "match_chunk_id": r["chunk_id"], "match_chunk_index": r["chunk_index"], "heading": r["heading"] or "", "start_line": r["start_line"], "end_line": r["end_line"], "snippet": r["snippet"] or "", **slices})
        if len(results) >= limit: break
    con.close(); return results


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b): return 0.0
    dot = sum(x*y for x,y in zip(a,b)); na = math.sqrt(sum(x*x for x in a)); nb = math.sqrt(sum(y*y for y in b))
    return dot/(na*nb) if na and nb else 0.0


def _vector_query(query: str, limit: int, db_path: Path = DB, model: str = DEFAULT_EMBED_MODEL, ollama_host: str = DEFAULT_OLLAMA_HOST) -> tuple[list[dict], str | None]:
    try:
        qv = ollama_embed(query, model=model, host=ollama_host)
    except Exception as e:
        return [], str(e)
    con = connect(db_path); setup(con)
    rows = con.execute("""
        SELECT e.vector, e.dim, c.id AS chunk_id, c.file_id, c.chunk_index, c.heading, c.content, c.start_line, c.end_line,
               f.path, f.relpath, f.title, f.layer, f.mtime, f.chunk_count
        FROM embeddings e JOIN chunks c ON c.id=e.chunk_id JOIN files f ON f.id=c.file_id
        WHERE e.model=?
    """, (model,)).fetchall()
    scored=[]
    for r in rows:
        if int(r["dim"]) != len(qv): continue
        score = _cosine(qv, _unpack_vector(r["vector"]))
        if score > 0:
            scored.append((score,r))
    scored.sort(key=lambda x: x[0], reverse=True)
    seen=set(); results=[]
    for score,r in scored:
        if r["file_id"] in seen: continue
        seen.add(r["file_id"])
        slices=bookends(con, r["file_id"], r["chunk_id"], window=2, bookend=2)
        results.append({"source":"cerebro_vector", "score": round(float(score),6), "file_id": r["file_id"], "path": r["path"], "relpath": r["relpath"], "title": r["title"], "layer": r["layer"], "mtime": r["mtime"], "chunk_count": r["chunk_count"], "match_chunk_id": r["chunk_id"], "match_chunk_index": r["chunk_index"], "heading": r["heading"] or "", "start_line": r["start_line"], "end_line": r["end_line"], "snippet": (r["content"] or "")[:360], **slices})
        if len(results)>=limit: break
    con.close(); return results, None


def discover(query: str, limit: int = 5, sort: str | None = None, db_path: Path = DB, *, vector: bool = False, model: str = DEFAULT_EMBED_MODEL, ollama_host: str = DEFAULT_OLLAMA_HOST) -> dict:
    limit=max(1,min(int(limit or 5),20))
    try:
        fts=_fts_query(query, limit, sort, db_path)
    except sqlite3.OperationalError as e:
        fts=[]; fts_error=str(e)
    else:
        fts_error=None
    vec=[]; vec_error=None
    if vector:
        vec, vec_error = _vector_query(query, limit, db_path, model, ollama_host)
    merged=[]; seen=set()
    # Prefer exact lexical evidence, then add semantic-only hits.
    for item in fts + vec:
        key=item.get("file_id")
        if key in seen: continue
        seen.add(key); merged.append(item)
        if len(merged)>=limit: break
    return {"ok": not fts_error, "mode":"discover", "query":query, "count":len(merged), "vector_enabled": vector, "fts_error": fts_error, "vector_error": vec_error, "results": merged}


def scroll(file_id: int | None = None, path: str | None = None, chunk_id: int | None = None, window: int = 5, db_path: Path = DB) -> dict:
    con=connect(db_path); setup(con)
    f=None
    if file_id is not None: f=con.execute("SELECT * FROM files WHERE id=?",(file_id,)).fetchone()
    elif path: f=con.execute("SELECT * FROM files WHERE path=? OR relpath=?",(path,path)).fetchone()
    if not f:
        con.close(); return {"ok":False,"error":"file not found"}
    if chunk_id is None:
        first=con.execute("SELECT id FROM chunks WHERE file_id=? ORDER BY chunk_index LIMIT 1",(f["id"],)).fetchone()
        if not first:
            con.close(); return {"ok":False,"error":"file has no chunks"}
        chunk_id=first["id"]
    v=bookends(con, f["id"], int(chunk_id), window=max(1,min(int(window),20)), bookend=0)
    con.close()
    if not v.get("window"): return {"ok":False,"error":f"chunk_id {chunk_id} not found in file_id {f['id']}"}
    return {"ok":True,"mode":"scroll","file_id":f["id"],"path":f["path"],"relpath":f["relpath"],"title":f["title"],"anchor_chunk_id":chunk_id,**v}


def browse(limit: int = 10, db_path: Path = DB) -> dict:
    con=connect(db_path); setup(con)
    rows=con.execute("SELECT id, path, relpath, title, layer, mtime, chunk_count, tags_text FROM files ORDER BY mtime DESC LIMIT ?",(max(1,min(int(limit),50)),)).fetchall()
    con.close(); return {"ok":True,"mode":"browse","count":len(rows),"results":[dict(r) for r in rows]}


def protocol() -> dict:
    return {"ok": True, "protocol": "Índice encontra → Markdown confirma → LLM responde → se houver escrita, Markdown atualiza → índice sincroniza", "source_of_truth": "Cerebro em Markdown", "index_role": "SQLite/FTS/embeddings são cache operacional, não fonte canônica", "retrieval_order": ["Cerebro FTS para nomes/datas/títulos/tags", "Cerebro vector/Ollama para semântica", "session_search para histórico conversacional", "fact_store/ontology para fatos estruturados e relações", "abrir Markdown original antes de responder"], "conflict_rule": "Markdown canônico vence SQL, fact_store e memória auxiliar."}


def main(argv: list[str] | None = None) -> int:
    ap=argparse.ArgumentParser(description="Fast local Cerebro FTS/vector index, no chat LLM.")
    ap.add_argument("--root", default=str(ROOT)); ap.add_argument("--db", default=str(DB))
    ap.add_argument("--rebuild", action="store_true"); ap.add_argument("--sync", action="store_true")
    ap.add_argument("--update-file", default=""); ap.add_argument("--embeddings", action="store_true")
    ap.add_argument("--ollama-host", default=DEFAULT_OLLAMA_HOST); ap.add_argument("--embedding-model", default=DEFAULT_EMBED_MODEL)
    ap.add_argument("--query", default=""); ap.add_argument("--vector", action="store_true")
    ap.add_argument("--limit", type=int, default=5); ap.add_argument("--sort", choices=["newest","oldest"], default=None)
    ap.add_argument("--file-id", type=int, default=None); ap.add_argument("--path", default=None); ap.add_argument("--chunk-id", type=int, default=None); ap.add_argument("--window", type=int, default=5)
    ap.add_argument("--browse", action="store_true"); ap.add_argument("--protocol", action="store_true")
    args=ap.parse_args(argv)
    root=Path(args.root); db_path=Path(args.db)
    try:
        if args.protocol: result=protocol()
        elif args.rebuild: result=rebuild(root, db_path, embeddings=args.embeddings, model=args.embedding_model, ollama_host=args.ollama_host)
        elif args.sync: result=sync(root, db_path, embeddings=args.embeddings, model=args.embedding_model, ollama_host=args.ollama_host)
        elif args.update_file: result=index_file(Path(args.update_file), root, db_path, embeddings=args.embeddings, model=args.embedding_model, ollama_host=args.ollama_host)
        elif args.file_id is not None or args.path or args.chunk_id is not None: result=scroll(args.file_id, args.path, args.chunk_id, args.window, db_path)
        elif args.query.strip(): result=discover(args.query.strip(), args.limit, args.sort, db_path, vector=args.vector, model=args.embedding_model, ollama_host=args.ollama_host)
        else: result=browse(args.limit, db_path)
    except Exception as e:
        result={"ok":False,"error":str(e)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
