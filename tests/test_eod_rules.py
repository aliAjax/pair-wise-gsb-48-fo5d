import unittest

from src.domain import ValidationError
from src.eod import (
    Entitlement,
    adjustment_entry,
    build_snapshot,
    cash_due,
    receipt_matches,
    securities_due,
    summarize_batch,
)


def instr(instrument="ACME", quantity=1000, net_amount=12518.0):
    return {"id": 1, "state": "approved",
            "payload": {"instrument": instrument, "quantity": quantity, "net_amount": net_amount}}


class EodRulesTest(unittest.TestCase):
    def test_snapshot_freezes_current_versions_and_defaults_missing(self):
        active = {"ACME": Entitlement("ACME", 2, "split", 3.0, 0.0)}
        snapshot = build_snapshot(["GLOBEX", "ACME"], active)
        self.assertEqual(snapshot["ACME"]["version"], 2)
        self.assertEqual(snapshot["ACME"]["ratio"], 3.0)
        self.assertEqual(snapshot["GLOBEX"]["version"], 0)
        self.assertEqual(snapshot["GLOBEX"]["ratio"], 1.0)

    def test_split_entitlement_changes_due_quantity_only(self):
        entry = {"version": 1, "event_type": "split", "ratio": 2.0, "cash_rate": 0.0}
        self.assertEqual(securities_due(1000, entry), 2000)
        self.assertEqual(cash_due(12518.0, 1000, entry), 12518.0)

    def test_dividend_entitlement_receivable_cash(self):
        entry = {"version": 1, "event_type": "dividend", "ratio": 1.0, "cash_rate": 0.5}
        self.assertEqual(securities_due(1000, entry), 1000)
        self.assertEqual(cash_due(12518.0, 1000, entry), 12018.0)
        self.assertEqual(cash_due(100.0, 1000, entry), 0.0)

    def test_summarize_aggregates_instructions_per_instrument(self):
        snapshot = build_snapshot(["ACME"], {})
        instructions = [instr("ACME", 600, 7500.0), instr("ACME", 400, 5018.0)]
        receipts = {"ACME": {"delivered_quantity": 1000, "cash_paid": 12518.0}}
        ledger = summarize_batch(instructions, receipts, snapshot)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0], {"instrument": "ACME", "securities_qty": 1000, "cash_amount": 12518.0})

    def test_summarize_rejects_missing_and_mismatched_receipt(self):
        snapshot = build_snapshot(["ACME", "GLOBEX"], {})
        receipts = {"ACME": {"delivered_quantity": 1000, "cash_paid": 12518.0}}
        with self.assertRaises(ValidationError):
            summarize_batch([instr("ACME"), instr("GLOBEX", 100, 800.0)], receipts, snapshot)
        with self.assertRaises(ValidationError):
            summarize_batch([instr()], {"ACME": {"delivered_quantity": 900, "cash_paid": 12518.0}}, snapshot)
        with self.assertRaises(ValidationError):
            summarize_batch([instr()], {"ACME": {"delivered_quantity": 1000, "cash_paid": 1.0}}, snapshot)

    def test_snapshot_version_is_immutable_input_to_later_publications(self):
        # 确认时冻结v1；之后发布v2，汇总仍按冻结快照计算
        frozen = build_snapshot(["ACME"], {"ACME": Entitlement("ACME", 1, "split", 2.0, 0.0)})
        ledger = summarize_batch([instr()], {"ACME": {"delivered_quantity": 2000, "cash_paid": 12518.0}}, frozen)
        self.assertEqual(ledger[0]["securities_qty"], 2000)

    def test_reconciliation_adjustment_is_delta_only(self):
        booked = {"securities_qty": 1000, "cash_amount": 12518.0}
        entry = adjustment_entry("B1", "ACME", 1, booked, 1000, 12600.0)
        self.assertEqual(entry["securities_qty"], 0)
        self.assertEqual(entry["cash_amount"], 82.0)
        self.assertEqual(entry["idempotency_key"], "rec:B1:ACME:1")
        missing = adjustment_entry("B1", "NEW", 1, None, 50, 400.0)
        self.assertEqual((missing["securities_qty"], missing["cash_amount"]), (50, 400.0))

    def test_receipt_match_dedup(self):
        existing = {"delivered_quantity": 1000, "cash_paid": 12518.0}
        self.assertTrue(receipt_matches(existing, 1000, 12518.0))
        self.assertFalse(receipt_matches(existing, 1000, 12518.01))
        self.assertFalse(receipt_matches(existing, 999, 12518.0))
