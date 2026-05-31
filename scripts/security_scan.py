#!/usr/bin/env python3
from __future__ import annotations
import re, sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
skip = {".git", "__pycache__", ".pytest_cache", ".ruff_cache"}
patterns = {
    "private_path": re.compile(r"(C:\\Users\\[^\\\s]+|C:/Users/[^/\s]+|/home/[^/\s]+)", re.I),
    "secret_literal": re.compile(r"(?i)(api[_-]?key|secret|password|token|credential)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
    "machine_name": re.compile(r"desktop-[a-z0-9-]+", re.I),
}
findings = []
for path in root.rglob("*"):
    if path.name == "security_scan.py" or any(part in skip for part in path.parts):
        continue
    if not path.is_file() or path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pyc"}:
        continue
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        continue
    for name, pat in patterns.items():
        for i, line in enumerate(text.splitlines(), 1):
            if pat.search(line):
                findings.append(f"{path.relative_to(root)}:{i}: {name}: {line[:160]}")
if findings:
    print("Security scan failed:")
    print("\n".join(findings))
    raise SystemExit(1)
print("Security scan passed")
