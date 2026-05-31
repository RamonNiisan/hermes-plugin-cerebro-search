# Cerebro Search

        A Hermes Agent plugin that adds local-first SQLite/FTS search and optional Ollama embeddings for a Markdown knowledge base.

        ## What it is

        This is a public, sanitized Hermes Agent plugin extracted from real local-agent workflows. It is intended as a portfolio-quality example of agent tooling: local-first state, explicit tool schemas, safe defaults, and no committed secrets or personal runtime data.

        ## Installation

        Copy this directory into your Hermes plugins directory and restart Hermes:

        ```bash
        cp -R . "$HERMES_HOME/plugins/cerebro-search"
        ```

        Or symlink it during development:

        ```bash
        ln -s "$(pwd)" "$HERMES_HOME/plugins/cerebro-search"
        ```

## Configuration

Set `CEREBRO_ROOT` or `KNOWLEDGE_BASE_ROOT` to the Markdown vault/knowledge-base directory. Optional embeddings use `CEREBRO_OLLAMA_HOST` and `CEREBRO_EMBED_MODEL`.

        ## Safety notes

        - No API keys, tokens, cookies, logs, browser profiles, or local memory databases are committed.
        - Local paths are resolved from environment variables or the user's home directory.
        - Generated state belongs in ignored runtime directories.

        ## Development checks

        ```bash
        python -m compileall .
        python scripts/security_scan.py .
        ```

        ## License

        MIT
