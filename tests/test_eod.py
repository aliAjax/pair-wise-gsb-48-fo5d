import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


OFFICER = lambda tag="officer": Actor(tag, "settlement_officer")
TRADER = Actor("trader1", "trader")
CUSTODIAN = Actor("custody1", "custodian")
CA = Actor("ca1", "corporate_actions")

INSTR = "ACME"


def instr_payload(**overrides):
    payload = {'instrument': INSTR, 'side': 'buy', 'quantity': 1000, 'price': 12.5, 'fees': 18.0,
               'currency': 'CNY', 'settlement_day': 2, 'corporate_action': 'none', 'action_ratio': 1.0}
    payload.update(overrides)
    return payload


def approved_instruction(service, reference, batch_ref, **overrides):
    data = instr_payload(**overrides)
    service.create(TRADER, reference, data, batch_ref=batch_ref)
    batch_id = service.repository.get_batch_by_ref(batch_ref)["id"]
    record = next(r for r in service.repository.batch_instructions(batch_id) if r["reference"] == reference)
    return service.act(OFFICER(), record["id"], record["version"], "approve", {})


class EodTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)
        self.service.create_batch(OFFICER(), "B-001", 2)
        approved_instruction(self.service, "TRD-1", "B-001")
        self.service.publish_entitlement(CA, INSTR, {"cash_per_share": 0.5, "quantity_ratio": 1.0})

    def tearDown(self):
        self.temp.cleanup()

    def test_confirm_freezes_entitlement_version_and_books_once(self):
        # 回执先到（晚到也一样，只要未关账即可补登）
        receipt = self.service.ingest_receipt(CUSTODIAN, "B-001", {"instrument": INSTR, "delivered_quantity": 1000, "cash_paid": 12518.0})
        self.assertFalse(receipt["duplicate"])

        batch = self.service.confirm_batch(OFFICER(), 1)
        self.assertEqual(batch["state"], "confirmed")
        self.assertEqual(batch["frozen_versions"], {INSTR: 1})

        ledger = self.service.ledger(OFFICER(), batch_id=1)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["posting_key"], "settle:batch-1:record-1")

        # 已确认且权益无变化时重复确认被拒
        with self.assertRaises(Conflict):
            self.service.confirm_batch(OFFICER(), 1)

    def test_duplicate_receipts_for_same_batch_and_instrument_count_once(self):
        first = self.service.ingest_receipt(CUSTODIAN, "B-001", {"instrument": INSTR, "delivered_quantity": 1000, "cash_paid": 12518.0})
        second = self.service.ingest_receipt(CUSTODIAN, "B-001", {"instrument": INSTR, "delivered_quantity": 1000, "cash_paid": 99999.0})
        self.assertTrue(second["duplicate"])
        receipts = self.service.repository.batch_receipts(1)
        valid = [r for r in receipts if r["status"] != "duplicate"]
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["cash_paid"], 12518.0)

        batch = self.service.confirm_batch(OFFICER(), 1)
        self.assertEqual(batch["state"], "confirmed")
        self.assertEqual(len(self.service.ledger(OFFICER(), batch_id=1)), 1)

    def test_concurrent_confirmation_only_one_wins(self):
        self.service.ingest_receipt(CUSTODIAN, "B-001", {"instrument": INSTR, "delivered_quantity": 1000, "cash_paid": 12518.0})
        results = []

        def confirm(tag, barrier):
            try:
                barrier.wait()
                results.append(("ok", self.service.confirm_batch(OFFICER(tag), 1, expected_version=1)["state"]))
            except Conflict as exc:
                results.append(("conflict", str(exc)))
            except Exception as exc:  # pragma: no cover - 暴露意外错误
                results.append(("error", repr(exc)))

        barrier = threading.Barrier(2)
        threads = [threading.Thread(target=confirm, args=("officer-a", barrier)),
                   threading.Thread(target=confirm, args=("officer-b", barrier))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        statuses = sorted(r[0] for r in results)
        self.assertEqual(statuses, ["conflict", "ok"])
        self.assertEqual(len(self.service.ledger(OFFICER(), batch_id=1)), 1)

    def test_write_failure_retries_from_last_completed_batch_without_double_booking(self):
        # 批次1就绪可确认；批次2尚缺回执，日终应在批次2阻断，断点停在批次1。
        self.service.ingest_receipt(CUSTODIAN, "B-001", {"instrument": INSTR, "delivered_quantity": 1000, "cash_paid": 12518.0})
        self.service.create_batch(OFFICER(), "B-002", 2)
        approved_instruction(self.service, "TRD-2", "B-002", instrument="OTHER")

        # 注入“批次1提交前写库失败”：事务整体回滚，批次1未完成，分录不落库。
        repo = self.service.repository

        def boom(connection):
            connection.execute("SELECT no_such_column FROM batches")

        repo._confirm_fault = boom
        with self.assertRaises(sqlite3.Error):
            self.service.run_eod(OFFICER(), 2)
        # 失败的事务整体回滚：批次未确认、分录不落库、断点不前移
        self.assertEqual(repo.get_checkpoint(2), 0)
        self.assertEqual(len(self.service.ledger(OFFICER())), 0)
        self.assertEqual(repo.get_batch(1)["state"], "open")

        # 恢复后重跑：批次1完整成功，再在缺回执的批次2阻断
        repo._confirm_fault = lambda connection: None
        blocked = self.service.run_eod(OFFICER(), 2)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["blocked_batch"]["reference"], "B-002")
        self.assertEqual(repo.get_checkpoint(2), 1)
        self.assertEqual(len(self.service.ledger(OFFICER())), 1)

        # 幂等重放：已完成批次不重复记账
        self.service.run_eod(OFFICER(), 2)
        self.assertEqual(len(self.service.ledger(OFFICER())), 1)

        # 补齐批次2回执后，日终从断点继续
        self.service.ingest_receipt(CUSTODIAN, "B-002", {"instrument": "OTHER", "delivered_quantity": 1000, "cash_paid": 12518.0})
        done = self.service.run_eod(OFFICER(), 2)
        self.assertEqual(done["status"], "complete")
        self.assertEqual(len(self.service.ledger(OFFICER())), 2)

    def test_entitlement_update_marks_confirmed_unclosed_batch_stale_and_reconfirm_freezes_new(self):
        self.service.ingest_receipt(CUSTODIAN, "B-001", {"instrument": INSTR, "delivered_quantity": 1000, "cash_paid": 12518.0})
        self.service.confirm_batch(OFFICER(), 1)
        self.assertEqual(self.service.repository.get_batch(1)["frozen_versions"], {INSTR: 1})

        # 权益版本更新：已确认未关账批次转为 stale，需要重新确认
        self.service.publish_entitlement(CA, INSTR, {"cash_per_share": 0.8, "quantity_ratio": 1.0})
        stale = self.service.repository.get_batch(1)
        self.assertEqual(stale["state"], "stale")

        with self.assertRaises(Conflict):
            self.service.close_batch(OFFICER(), 1)

        reconfirmed = self.service.confirm_batch(OFFICER(), 1)
        self.assertEqual(reconfirmed["state"], "confirmed")
        self.assertEqual(reconfirmed["frozen_versions"], {INSTR: 2})
        # 重新确认不重复记账
        self.assertEqual(len(self.service.ledger(OFFICER(), batch_id=1)), 1)

    def test_late_receipt_after_close_goes_to_reconciliation_and_new_result_does_not_touch_closed(self):
        self.service.ingest_receipt(CUSTODIAN, "B-001", {"instrument": INSTR, "delivered_quantity": 1000, "cash_paid": 12518.0})
        self.service.confirm_batch(OFFICER(), 1)
        self.service.close_batch(OFFICER(), 1)
        closed_frozen = dict(self.service.repository.get_batch(1)["frozen_versions"])
        closed_ledger = list(self.service.ledger(OFFICER(), batch_id=1))

        # 关账后到达的回执进入待对账，不倒改已确认的权益和账务
        late = self.service.ingest_receipt(CUSTODIAN, "B-001", {"instrument": INSTR, "delivered_quantity": 1100, "cash_paid": 13000.0})
        self.assertEqual(late["status"], "pending")
        self.assertEqual(self.service.repository.get_batch(1)["state"], "closed")
        self.assertEqual(self.service.repository.get_batch(1)["frozen_versions"], closed_frozen)
        self.assertEqual(self.service.ledger(OFFICER(), batch_id=1), closed_ledger)

        # 重复到达的关账后回执同样只算一次
        dup = self.service.ingest_receipt(CUSTODIAN, "B-001", {"instrument": INSTR, "delivered_quantity": 1100, "cash_paid": 13000.0})
        self.assertTrue(dup["duplicate"])

        # 补全：生成新的调整批次与分录，原批次保持关账
        result = self.service.complete_reconciliation(OFFICER(), late["id"])
        self.assertEqual(result["item"]["status"], "resolved")
        self.assertEqual(result["result"]["delta_quantity"], 100)
        self.assertEqual(result["result"]["delta_cash"], 482.0)
        adjustment_batch_id = result["result"]["adjustment_batch_id"]
        adjustment = self.service.repository.get_batch(adjustment_batch_id)
        self.assertEqual(adjustment["state"], "closed")
        self.assertEqual(self.service.repository.get_batch(1)["state"], "closed")
        entries = self.service.ledger(OFFICER(), batch_id=adjustment_batch_id)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["entry_type"], "reconciliation_adjustment")

        # 已补全的待对账项不能重复处理
        with self.assertRaises(Conflict):
            self.service.complete_reconciliation(OFFICER(), late["id"])

    def test_unready_batch_blocks_confirmation(self):
        # 没有回执直接确认 -> 未就绪
        with self.assertRaises(Conflict):
            self.service.confirm_batch(OFFICER(), 1)
        # 数量不符
        self.service.ingest_receipt(CUSTODIAN, "B-001", {"instrument": INSTR, "delivered_quantity": 900, "cash_paid": 12518.0})
        with self.assertRaises(Conflict):
            self.service.confirm_batch(OFFICER(), 1)


if __name__ == "__main__":
    unittest.main()
