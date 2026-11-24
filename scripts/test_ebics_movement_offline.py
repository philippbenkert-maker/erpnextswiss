"""Bounded stdlib-only mirror regressions; never claims native SQL acceptance.

Executes unmodified production helper bodies and five existing test methods from
their source files. This fallback requires no Frappe install and performs no
network/SQL. The regular Frappe offline runner still executes the full module.
"""

import ast
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import json
from pathlib import Path
import re
from types import ModuleType
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(path):
    module = ModuleType(path.stem)
    module.__dict__.update(__file__=str(path))
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


identity = load_module(ROOT / "erpnextswiss/erpnextswiss/tests/test_ebics_movement_identity.py")
metadata = load_module(ROOT / "tests/test_ebics_movement_metadata.py")
fixtures = load_module(ROOT / "tests/test_fixture_exports.py")
automation = ModuleType("automation")
frappe = MagicMock()
frappe._dict = identity.Row
automation.__dict__.update(frappe=frappe, Decimal=Decimal, InvalidOperation=InvalidOperation,
                           ROUND_HALF_UP=ROUND_HALF_UP, date=date, sha256=sha256, json=json,
                           re=re, BankFileError=identity.BankFileError,
                           QueryDeadlockError=identity.QueryDeadlockError)
path = ROOT / "erpnextswiss/erpnextswiss/ebics_automation.py"
tree = ast.parse(path.read_text(encoding="utf-8"))
tree.body = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.Assign))]
exec(compile(tree, str(path), "exec"), automation.__dict__)

names = {
    "_movement_entry", "test_non_finite_money_is_not_accepted",
    "test_movement_identity_is_stable_across_053_054_and_changed_content_conflicts",
    "test_legacy_bank_transaction_is_adopted_only_by_exact_reference",
    "test_native_movement_creation_submits_no_payment_or_gl_allocation",
    "test_historical_movement_backfill_is_dry_run_and_fail_closed",
}
path = ROOT / "erpnextswiss/erpnextswiss/tests/test_ebics_automation.py"
tree = ast.parse(path.read_text(encoding="utf-8"))
cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TestEbicsAutomation")
cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names]
assert {node.name for node in cls.body} == names
env = dict(TestCase=unittest.TestCase, MagicMock=MagicMock, patch=patch, automation=automation,
           frappe=frappe, json=json)
exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), "exec"), env)
suite = unittest.TestSuite([
    unittest.defaultTestLoader.loadTestsFromModule(identity),
    unittest.defaultTestLoader.loadTestsFromModule(metadata),
    unittest.defaultTestLoader.loadTestsFromModule(fixtures),
    unittest.defaultTestLoader.loadTestsFromTestCase(env["TestEbicsAutomation"]),
])
result = unittest.TextTestRunner(verbosity=2).run(suite)
if not result.wasSuccessful() or result.skipped:
    raise SystemExit(1)
