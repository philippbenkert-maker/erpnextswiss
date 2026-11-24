#!/usr/bin/env python3
"""Exclusive disposable-restore gate: real ORM, unique claims, two SQL sessions.

No provider operations. The optional archive phase commits restored bank-list
rows only, requires the pinned five-entry EUR evidence, and verifies accounting
invariance and replay. Destroy the disposable restore after exporting proof.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
from threading import Barrier, Event
import unittest
from uuid import uuid4

import frappe

from vat_accounting_integrity import accounting_digest
from erpnextswiss.erpnextswiss import ebics_automation as auto
from erpnextswiss.erpnextswiss.tests.native_ebics_movement_cases import (
    TestEbicsMovementNative, fixture, movement,
)
from erpnextswiss.scripts.bank_file_admission import BankFileError


SITE = "kt-platform-test.local"
SITES = "/home/frappe/frappe-bench/sites"
EVIDENCE_SHA = "d0372d44a3977b144504f5f1cdcc5caa3b76fe8c9243eeaaeb13a01cf3cd1ec8"


def connect():
    frappe.init(site=SITE, sites_path=SITES)
    if not (frappe.conf.db_host == "platform-restore-db" and frappe.conf.pause_scheduler):
        raise RuntimeError("Disposable paused internal-network restore required")
    frappe.connect()
    frappe.set_user("Administrator")
    frappe.flags.mute_emails = True
    frappe.db.sql("SET SESSION innodb_lock_wait_timeout = 10")


def native_digest():
    return {table: sha256(json.dumps(frappe.db.sql(
        "SELECT * FROM `tab" + table + "` ORDER BY name", as_dict=True),
        sort_keys=True, default=str).encode()).hexdigest()
        for table in ("Bank Transaction", "Bank Transaction Payments", "EBICS Movement Alias")}


def worker(names, enriched, barrier, plain_committed):
    connect()
    try:
        connection, statement, entry = movement(names, enriched=enriched)
        plan = auto._movement_plan(connection, statement, entry)
        assert plan["state"] == "create", "Both sessions must plan before either applies"
        barrier.wait(timeout=15)
        if enriched:
            assert plain_committed.wait(15), "The first writer did not commit"
            assert not frappe.db.get_value("Bank Transaction", {"bank_account": names.bank_account}, "name")
        result = auto._apply_movement_plan(connection, statement, entry, names.source, plan)
        frappe.db.commit()
        if not enriched:
            plain_committed.set()
        return {key: result[key] for key in ("state", "identity", "bank_transaction")}
    finally:
        frappe.db.rollback()
        frappe.destroy()


def stale_writer(names, name):
    connect()
    try:
        frappe.db.set_value("Bank Transaction", name, {
            "transaction_id": "CHANGED-IN-OTHER-SQL-SESSION",
            auto._MOVEMENT_ID_FIELD: "OTHER-CLAIM",
        })
        frappe.db.commit()
    finally:
        frappe.db.rollback()
        frappe.destroy()


def cleanup_fixture(names):
    # Deliberately bypass immutable production controllers ONLY for committed
    # synthetic fixtures on this disposable DB. Never delete restored bank rows.
    if not (names.company.startswith("KT-MIRROR-C-")
            and names.bank_account.startswith("KT-MIRROR-BA-")):
        raise RuntimeError("Unexpected synthetic fixture scope")
    transactions = frappe.get_all("Bank Transaction", filters={"bank_account": names.bank_account}, pluck="name")
    assert len(transactions) <= 3
    if transactions:
        frappe.db.delete("Bank Transaction Payments", {"parent": ("in", transactions)})
    frappe.db.delete("EBICS Movement Alias", {"bank_account": names.bank_account})
    frappe.db.delete("Bank Transaction", {"bank_account": names.bank_account})
    for doctype, name in (("EBICS Download", names.source), ("Bank Account", names.bank_account),
                          ("Bank", names.bank), ("Account", names.account), ("Company", names.company)):
        frappe.db.delete(doctype, {"name": name})


def concurrent_checks():
    names = []
    try:
        concurrent = fixture(uuid4().hex[:12])
        stale = fixture(uuid4().hex[:12])
        names.extend((concurrent, stale))
        frappe.db.commit()  # Required for independent test sessions to see ONLY these fixtures.
        baseline = accounting_digest()
        with ThreadPoolExecutor(max_workers=2) as pool:
            barrier = Barrier(2)
            plain_committed = Event()
            futures = [pool.submit(worker, concurrent, enriched, barrier, plain_committed)
                       for enriched in (False, True)]
            results = [future.result(timeout=40) for future in futures]
        frappe.db.rollback()  # discard any old snapshot before checking committed workers
        assert sorted(result["state"] for result in results) == ["created", "existing"]
        assert results[0]["identity"] == results[1]["identity"]
        assert results[0]["bank_transaction"] == results[1]["bank_transaction"]
        assert frappe.db.count("Bank Transaction", {"bank_account": concurrent.bank_account}) == 1
        assert frappe.db.count("EBICS Movement Alias", {"bank_account": concurrent.bank_account}) == 2
        connection, statement, entry = movement(stale)
        doc = frappe.get_doc({"doctype": "Bank Transaction", "bank_account": stale.bank_account, "company": stale.company,
                              "date": entry["booking_date"], "currency": "EUR", "withdrawal": 12.34,
                              "transaction_id": entry["reference"]})
        doc.insert(ignore_permissions=True).submit()
        frappe.db.commit()
        frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        # Frappe commit() already began a transaction under the old isolation.
        # Restart it so SET SESSION actually governs this stale-snapshot test.
        frappe.db.rollback()
        plan = auto._movement_plan(connection, statement, entry)
        assert plan["state"] == "adopt"
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(stale_writer, stale, doc.name).result(timeout=30)
        # Demonstrate an ACTUAL stale snapshot, not just an interleaved fake.
        assert frappe.db.get_value("Bank Transaction", doc.name, "transaction_id") == entry["reference"]
        try:
            auto._apply_movement_plan(connection, statement, entry, stale.source, plan)
        except BankFileError:
            pass
        else:
            raise AssertionError("Stale legacy adoption overwrote another SQL session")
        current = frappe.get_doc("Bank Transaction", doc.name, for_update=True)
        assert current.transaction_id == "CHANGED-IN-OTHER-SQL-SESSION"
        assert current.get(auto._MOVEMENT_ID_FIELD) == "OTHER-CLAIM"
        assert not current.get(auto._MOVEMENT_FINGERPRINT_FIELD)
        assert accounting_digest() == baseline
        return {"concurrent_enrichment": "PASS", "repeatable_read_stale_adoption": "PASS"}
    finally:
        frappe.db.rollback()
        for created in names:
            cleanup_fixture(created)
        frappe.db.commit()


def archive_checks(connection_name, evidence_path):
    raw = Path(evidence_path).read_bytes()
    assert sha256(raw).hexdigest() == EVIDENCE_SHA, "Five-entry evidence changed"
    evidence = json.loads(raw)["data"]
    assert len(evidence) == 5 and all(row["entry_currency"] == "EUR" for row in evidence)
    accounting_before = accounting_digest()
    dry = auto.mirror_historical_bank_movements(connection_name)
    assert dry["status"] == "dry_run" and dry["review"] == 0, dry
    applied = auto.mirror_historical_bank_movements(connection_name, apply=True)
    assert applied["status"] == "applied" and applied["review"] == 0, applied
    maps = []
    for name in frappe.get_all("EBICS Download", filters={"connection": connection_name}, pluck="name"):
        record = frappe.get_doc("EBICS Download", name)
        maps.extend(json.loads(record.movement_map_json or "[]"))
    resolved = set()
    for expected in evidence:
        matches = [row for row in maps if row["file_sha256"] == expected["bank_file_sha256"]
                   and row["entry_index"] == expected["entry_position"] - 1]
        targets = {row["bank_transaction"] for row in matches}
        assert len(targets) == 1 and None not in targets, "Archived EUR entry has no unique native row"
        transaction = frappe.get_doc("Bank Transaction", next(iter(targets)))
        assert transaction.docstatus == 1 and transaction.currency == "EUR"
        assert transaction.bank_account == expected["bank_account"]
        assert str(transaction.date) == expected["booking_date"]
        assert transaction.withdrawal > 0 and not transaction.deposit
        assert not transaction.payment_entries
        resolved.add(transaction.name)
    assert len(resolved) == 5, "Two archived EUR entries were conflated"
    native_before_replay = native_digest()
    repeated = auto.mirror_historical_bank_movements(connection_name, apply=True)
    assert repeated["status"] == "applied" and repeated["created"] == repeated["adopted"] == repeated["review"] == 0
    assert native_digest() == native_before_replay, "Replay changed native movements or claims"
    assert accounting_digest() == accounting_before, "Mirror changed accounting data"
    return {"five_archived_eur_entries": "PASS", "full_archive_replay": "PASS",
            "accounting_invariance": "PASS", "resolved_eur_count": len(resolved)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive-connection")
    parser.add_argument("--expected-eur-evidence")
    args = parser.parse_args()
    if bool(args.archive_connection) != bool(args.expected_eur_evidence):
        parser.error("Archive acceptance requires both connection and pinned EUR evidence")
    connect()
    try:
        before, native_before = accounting_digest(), native_digest()
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestEbicsMovementNative)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        assert result.wasSuccessful() and result.testsRun == 4 and not result.skipped, "Four native ORM/SQL tests required"
        frappe.db.rollback()
        concurrency = concurrent_checks()
        assert accounting_digest() == before and native_digest() == native_before, "Synthetic cleanup/invariance failed"
        archive = archive_checks(args.archive_connection, args.expected_eur_evidence) if args.archive_connection else "NOT_RUN"
        print(json.dumps({"status": "PASS" if archive != "NOT_RUN" else "SYNTHETIC_PASS_ARCHIVE_NOT_RUN",
                          "native_tests": result.testsRun, "concurrency": concurrency, "archive": archive}, sort_keys=True))
    finally:
        frappe.db.rollback()
        frappe.destroy()


if __name__ == "__main__":
    main()
