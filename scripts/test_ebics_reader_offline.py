"""Run the financial adapter suites without database or network access."""
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import fintech
import frappe

fintech.register()
with tempfile.TemporaryDirectory(prefix="ebics-tests-") as directory:
    (Path(directory) / "apps.txt").write_text("frappe\nerpnext\nerpnextswiss\n", encoding="utf-8")
    (Path(directory) / "test.invalid").mkdir()
    (Path(directory) / "test.invalid" / "site_config.json").write_text("{}", encoding="utf-8")
    frappe.init(site="test.invalid", sites_path=directory, force=True)
    frappe.local.db = MagicMock()
    try:
        with (patch.object(socket.socket, "connect", side_effect=RuntimeError("Offline tests: no network")),
              patch.object(socket.socket, "connect_ex", side_effect=RuntimeError("Offline tests: no network"))):
            suite = unittest.defaultTestLoader.loadTestsFromNames([
                "erpnextswiss.erpnextswiss.tests.test_ebics_automation",
                "erpnextswiss.erpnextswiss.tests.test_python_ebics_reader",
                "erpnextswiss.erpnextswiss.tests.test_match_payments",
            ])
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            if not result.wasSuccessful() or result.skipped:
                raise SystemExit(1)
    finally:
        frappe.destroy()
