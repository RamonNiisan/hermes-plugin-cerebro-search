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

Set `CEREBRO_ROOT` or `KNOWLEDGE_BASE_ROOT` to the knowledge-base directory. Optional embedding configuration uses `CEREBRO_OLLAMA_HOST`, `CEREBRO_EMBED_MODEL`, or `OLLAMA_HOST`.

## Installation

Clone the repository into your Hermes plugins directory or symlink it during development:

```bash
git clone https://github.com/RamonNiisan/hermes-plugin-cerebro-search.git
ln -s "$(pwd)/hermes-plugin-cerebro-search" "$HERMES_HOME/plugins/cerebro-search"
```

Restart Hermes after installing or changing plugins so the tool registry is rebuilt.

## Development

Run the basic validation checks before opening a pull request:

```bash
python -m compileall .
python scripts/security_scan.py .
```

## License

MIT. See [LICENSE](LICENSE).
