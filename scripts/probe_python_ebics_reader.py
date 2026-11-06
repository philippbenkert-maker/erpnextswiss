"""Non-consuming live pilot. SQL is read-only, bank receipt is negative (KEEP)."""
import argparse
import json
import sys

import frappe

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--site", required=True)
parser.add_argument("--connection", required=True)
args = parser.parse_args()
frappe.init(site=args.site, sites_path="/home/frappe/frappe-bench/sites")
frappe.connect()
try:
    frappe.db.sql("SET TRANSACTION READ ONLY")
    from erpnextswiss.erpnextswiss import ebics_automation as automation
    from erpnextswiss.erpnextswiss.python_ebics_reader import PythonEbicsReader
    from erpnextswiss.scripts.bank_file_admission import read_bank_archive

    connection = frappe.get_doc("ebics Connection", args.connection)
    if not connection.activated or not connection.enable_sync:
        raise ValueError("Connection is not enabled")
    summary = {"mode": "read_only_keep", "statements": 0, "entries": 0, "exact_open_matches": 0}
    dates = []
    def preview(transaction_id, archive):
        admitted = read_bank_archive(archive, automation._statement_profile(connection))
        payload = automation._archive_bytes({m.name: m.content for m in admitted.files})
        result = automation._preview(payload, connection)
        for statement in result["statements"]:
            summary["statements"] += 1
            for entry in statement["entries"]:
                summary["entries"] += 1
                if entry.get("booking_date"):
                    dates.append(str(entry["booking_date"]))
                if automation._strict_invoice_candidate(connection, statement, entry) is not None:
                    summary["exact_open_matches"] += 1
    transaction = PythonEbicsReader(connection).download(preview, keep=True)
    summary.update(status="no_data" if transaction is None else "validated_not_consumed",
                   latest_booking_date=max(dates) if dates else None)
    print(json.dumps(summary))
except Exception as error:
    print(json.dumps({"status": "failed", "error_type": type(error).__name__,
                      "bank_code": getattr(error, "code", None)}))
    sys.exit(1)
finally:
    frappe.db.rollback()
    frappe.destroy()
