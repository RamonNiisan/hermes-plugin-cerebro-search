# Hermes Plugin: Cerebro Search

A local-first search plugin for Hermes Agent that indexes a Markdown knowledge base with SQLite FTS and optional Ollama embeddings.

## Overview

This repository contains a Hermes Agent plugin designed for local-first agent workflows. It focuses on practical automation: explicit tool boundaries, inspectable state, deterministic behavior where possible, and small pieces that can be understood independently.

## Features

- Builds and maintains a SQLite/FTS index over Markdown and text files.
- Supports discovery, browse, update-file, rebuild, and scroll-style retrieval flows.
- Can combine knowledge-base retrieval with Hermes session search and fact-store results.
- Optionally uses Ollama embeddings for semantic retrieval.
- Detects external file changes and updates the index incrementally.

## Tools

- `cerebro_index`
- `cerebro_search`
- `context_search`
- `cerebro_watch`

## Architecture

- `__init__.py` registers Hermes tools and routes calls into the index runtime.
- `scripts/cerebro_search_index.py` implements indexing, chunking, FTS querying, browsing, and optional vector search.
- `plugin.yaml` declares tool metadata for Hermes.

## Configuration

The plugin is intentionally portable: the Markdown knowledge base is selected by environment variable, not by a hardcoded local path.

Set one of these variables before starting Hermes:

- `CEREBRO_ROOT` — preferred, explicit knowledge-base root.
- `KNOWLEDGE_BASE_ROOT` — generic alias for non-Cérebro vaults.
- `OBSIDIAN_VAULT_PATH` — useful when the knowledge base is an Obsidian vault.

If no variable is set, the plugin tries these conventional folders:

1. `~/Documents/Cerebro`
2. `~/Documents/Cérebro`
3. `~/Documents/KnowledgeBase`

Optional embedding configuration:

- `CEREBRO_OLLAMA_HOST` or `OLLAMA_HOST` — default `http://127.0.0.1:11434`.
- `CEREBRO_EMBED_MODEL` — default `nomic-embed-text:latest`.

Embeddings are optional. FTS5 search works without Ollama.

## Installation

Clone the repository into your Hermes plugins directory or symlink it during development:

```bash
mkdir -p "${HERMES_HOME:-$HOME/.hermes}/plugins"
git clone https://github.com/RamonNiisan/hermes-plugin-cerebro-search.git "${HERMES_HOME:-$HOME/.hermes}/plugins/cerebro-search"
```

Or, for local development:

```bash
mkdir -p "${HERMES_HOME:-$HOME/.hermes}/plugins"
git clone https://github.com/RamonNiisan/hermes-plugin-cerebro-search.git
ln -s "$(pwd)/hermes-plugin-cerebro-search" "${HERMES_HOME:-$HOME/.hermes}/plugins/cerebro-search"
```

Then export your knowledge-base path and restart Hermes so the tool registry is rebuilt:

```bash
export CEREBRO_ROOT="$HOME/Documents/Cerebro"   # adjust to your vault/root
hermes
```

For persistent configuration, place the export in the shell/service environment that starts Hermes.

## CLI smoke test

The index script can be tested outside Hermes:

```bash
export CEREBRO_ROOT="$HOME/Documents/Cerebro"   # adjust to your vault/root
python "${HERMES_HOME:-$HOME/.hermes}/plugins/cerebro-search/scripts/cerebro_search_index.py" --rebuild
python "${HERMES_HOME:-$HOME/.hermes}/plugins/cerebro-search/scripts/cerebro_search_index.py" --query "example" --limit 3
```

The SQLite index is created under `$CEREBRO_ROOT/.indices/` and is cache/state, not the source of truth.

## Development

Run the basic validation checks before opening a pull request:

```bash
python -m compileall .
python scripts/security_scan.py .
```

## License

MIT. See [LICENSE](LICENSE).
