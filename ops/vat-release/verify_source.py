#!/usr/bin/env python3
"""Verify every pinned Git source file in the built ERPNextSwiss app."""
import hashlib
import json
from pathlib import Path
import sys

root = Path('/home/frappe/frappe-bench/apps/erpnextswiss')
manifest = json.loads(Path(sys.argv[1]).read_text())
assert len(manifest) > 100
for name, expected in manifest.items():
    path = root / name
    assert path.resolve().is_relative_to(root), name
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, name
print(json.dumps({'source_integrity': 'PASS', 'files': len(manifest)}))
