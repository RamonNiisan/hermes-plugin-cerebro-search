from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_index_module(repo_root: Path):
    script = repo_root / "scripts" / "cerebro_search_index.py"
    spec = importlib.util.spec_from_file_location("cerebro_search_index_test", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_indexes_arbitrary_markdown_knowledge_base(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    mod = load_index_module(repo_root)

    root = tmp_path / "my-vault"
    (root / "Areas" / "Alpha").mkdir(parents=True)
    (root / "Projects" / "Beta" / "Notes").mkdir(parents=True)
    (root / "People").mkdir(parents=True)

    (root / "Areas" / "Alpha" / "overview.md").write_text(
        "---\ntitle: Alpha Overview\ntags: [alpha, custom-layer]\n---\n"
        "# Alpha Overview\n"
        "This custom vault links to [[Projects/Beta/Notes/spec|Beta Spec]].\n",
        encoding="utf-8",
    )
    (root / "Projects" / "Beta" / "Notes" / "spec.md").write_text(
        "# Beta Spec\nThe beta project mentions [[People/Ada|Ada]] and agent search.\n",
        encoding="utf-8",
    )
    (root / "People" / "Ada.md").write_text("# Ada\nAda works on Beta.\n", encoding="utf-8")

    db = root / ".indices" / "search.sqlite"
    sync = mod.rebuild(root=root, db_path=db)
    assert sync["ok"] is True
    assert sync["indexed"] == 3
    assert sync["errors"] == []

    result = mod.discover("agent search", limit=5, db_path=db)
    assert result["ok"] is True
    assert result["count"] >= 1
    assert result["results"][0]["relpath"] == "Projects/Beta/Notes/spec.md"

    browse = mod.browse(limit=10, db_path=db)
    layers = {row["relpath"]: row["layer"] for row in browse["results"]}
    assert layers["Areas/Alpha/overview.md"] == "Areas"
    assert layers["Projects/Beta/Notes/spec.md"] == "Projects"
    assert layers["People/Ada.md"] == "People"

    protocol = mod.protocol(name="Example Vault", root=root, db_path=db)
    assert protocol["source_of_truth"] == "Markdown files in the configured knowledge-base root"
    assert protocol["knowledge_base"] == "Example Vault"
    assert "specific Cérebro folder layout" in protocol["portability"]
