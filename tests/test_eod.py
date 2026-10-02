import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError
from src.eod import BATCH_CLOSED, BATCH_CONFIRMED, BATCH_OPEN


OFFICER = Actor("officer", "settlement_officer")
CA = Actor("ca", "corporate_actions")


def instruction(reference, instrument="ACME", quantity=1000, price=12.5, fees=18.0,
                action="none", ratio=1.0, batch_key="B-DAY1", day=1):
    return {
        "instrument": instrument, "side": "buy", "quantity": quantity, "price": price,
        "fees": fees, "currency": "CNY", "settlement_day": day,
        "corporate_action": action, "action_ratio": ratio, "batch_key": batch_key,
    }, reference


class EodWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _create_instruction(self, reference, **kwargs):
        data, ref = instruction(reference, **kwargs)
        return self.service.create(Actor("trader", "trader"), reference, data)

    def test_confirm_freezes_entitlement_and_books_once(self):
        self._create_instruction("TRD-1")
        self.service.publish_entitlement(CA, {"instrument": "ACME", "event_type": "split", "ratio": 2.0, "cash_rate": 0})
        self.service.ingest_receipt(OFFICER, {"batch_key": "B-DAY1", "settlement_day": 1, "instrument": "ACME",
                                              "delivered_quantity": 2000, "cash_paid": 12518.0, "received_from": "CUSTODY"})
        confirmed = self.service.confirm_settlement(OFFICER, "B-DAY1", 1)
        self.assertEqual(confirmed["state"], BATCH_CONFIRMED)
        self.assertEqual(confirmed["entitlement_snapshot"]["ACME"]["version"], 1)
        self.assertEqual(confirmed["entitlement_snapshot"]["ACME"]["ratio"], 2.0)
        ledger = self.service.ledger(OFFICER, "B-DAY1")
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["securities_qty"], 2000)
        self.assertEqual(ledger[0]["cash_amount"], 12518.0)

    def test_new_entitlement_after_confirm_forces_reconfirm_before_close(self):
        self._create_instruction("TRD-1")
        self.service.publish_entitlement(CA, {"instrument": "ACME", "event_type": "split", "ratio": 2.0, "cash_rate": 0})
        self.service.ingest_receipt(OFFICER, receipt("B-DAY1", "ACME", 2000, 12518.0))
        self.service.confirm_settlement(OFFICER, "B-DAY1", 1)
        # 公司行动权益版本更新，未关账批次回到开放态、冻结快照清空，临时账务清除
        self.service.publish_entitlement(CA, {"instrument": "ACME", "event_type": "split", "ratio": 3.0, "cash_rate": 0})
        batch = self.service.get_batch(OFFICER, "B-DAY1")
        self.assertEqual(batch["state"], BATCH_OPEN)
        self.assertEqual(batch["version"], 3)
        self.assertEqual(batch["entitlement_snapshot"], {})
        self.assertEqual(self.service.ledger(OFFICER, "B-DAY1"), [])
        # 旧回执不满足新权益下的应收数量，重新确认被拒绝
        with self.assertRaises(ValidationError):
            self.service.confirm_settlement(OFFICER, "B-DAY1", 3)
        self.service.ingest_receipt(OFFICER, receipt("B-DAY1", "ACME", 3000, 12518.0))
        reconfirmed = self.service.confirm_settlement(OFFICER, "B-DAY1", 3)
        self.assertEqual(reconfirmed["entitlement_snapshot"]["ACME"]["version"], 2)
        self.assertEqual(reconfirmed["entitlement_snapshot"]["ACME"]["ratio"], 3.0)
        self.assertEqual(len(self.service.ledger(OFFICER, "B-DAY1")), 1)

    def test_duplicate_receipt_counts_once_conflicting_receipt_rejected(self):
        self._create_instruction("TRD-1")
        first = self.service.ingest_receipt(OFFICER, receipt("B-DAY1", "ACME", 1000, 12518.0))
        self.assertEqual(first["outcome"], "inserted")
        dup = self.service.ingest_receipt(OFFICER, receipt("B-DAY1", "ACME", 1000, 12518.0))
        self.assertEqual(dup["outcome"], "duplicate")
        stored = self.service.list_receipts(OFFICER, "B-DAY1")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["ingest_count"], 2)
        # 批次仍开放时允许更正回执（如权益更新后重新交收），仍只保留一条
        replaced = self.service.ingest_receipt(OFFICER, receipt("B-DAY1", "ACME", 1000, 12520.0))
        self.assertEqual(replaced["outcome"], "replaced")
        self.assertEqual(len(self.service.list_receipts(OFFICER, "B-DAY1")), 1)
        # 确认后再到不同回执：冲突拒绝，不能倒改已冻结交收
        self.service.confirm_settlement(OFFICER, "B-DAY1", 1)
        with self.assertRaises(Conflict):
            self.service.ingest_receipt(OFFICER, receipt("B-DAY1", "ACME", 900, 12520.0))
        self.assertEqual(len(self.service.ledger(OFFICER, "B-DAY1")), 1)

    def test_missing_receipt_blocks_confirmation(self):
        self._create_instruction("TRD-1", instrument="ACME")
        self._create_instruction("TRD-2", instrument="GLOBEX")
        self.service.ingest_receipt(OFFICER, receipt("B-DAY1", "ACME", 1000, 12518.0))
        with self.assertRaises(ValidationError):
            self.service.confirm_settlement(OFFICER, "B-DAY1", 1)

    def test_post_close_receipt_enters_reconciliation_and_result_is_delta_only(self):
        self._create_instruction("TRD-1")
        self.service.ingest_receipt(OFFICER, receipt("B-DAY1", "ACME", 1000, 12518.0))
        self.service.confirm_settlement(OFFICER, "B-DAY1", 1)
        self.service.close_batch(OFFICER, "B-DAY1")
        # 关账后到达的回执：不覆盖首条，进待对账
        outcome = self.service.ingest_receipt(OFFICER, receipt("B-DAY1", "ACME", 1000, 12600.0))
        self.assertEqual(outcome["outcome"], "queued_for_reconciliation")
        items = self.service.list_reconciliation(OFFICER, status="pending")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source_batch_key"], "B-DAY1")
        # 已确认的权益与账务不能倒改
        original = self.service.get_batch(OFFICER, "B-DAY1")
        self.assertEqual(original["state"], BATCH_CLOSED)
        self.assertEqual(original["ledger"][0]["cash_amount"], 12518.0)
        # 补全后生成新结果批次，只记差额
        result = self.service.complete_reconciliation(Actor("rec", "reconciliation_clerk"), {
            "source_batch_key": "B-DAY1", "instrument": "ACME",
            "delivered_quantity": 1000, "cash_paid": 12600.0,
        })
        self.assertEqual(result["batch_key"], "B-DAY1#RC1")
        self.assertEqual(result["state"], BATCH_CLOSED)
        self.assertEqual(result["source_batch_key"], "B-DAY1")
        delta = self.service.ledger(OFFICER, "B-DAY1#RC1")
        self.assertEqual(len(delta), 1)
        self.assertEqual(delta[0]["securities_qty"], 0)
        self.assertEqual(delta[0]["cash_amount"], 82.0)
        # 冻结快照沿用原批次，未倒改
        self.assertEqual(result["entitlement_snapshot"], original["entitlement_snapshot"])
        self.assertEqual(self.service.list_reconciliation(OFFICER, status="pending"), [])

    def test_eod_run_resumes_from_last_full_batch_without_double_booking(self):
        self._create_instruction("TRD-1")
        self._create_instruction("TRD-2", instrument="GLOBEX", price=8.0, fees=0.0)
        self.service.ingest_receipt(OFFICER, receipt("B-DAY1", "ACME", 1000, 12518.0))
        self.service.ingest_receipt(OFFICER, receipt("B-DAY1", "GLOBEX", 1000, 8000.0))
        # 注入写库失败：B-DAY1确认事务在提交前回滚
        self.service.repository.confirm_fault_inject = RuntimeError("disk full")
        with self.assertRaises(RuntimeError):
            self.service.run_eod(OFFICER, "EOD-R1")
        self.service.repository.confirm_fault_inject = None
        run = self.service.get_run(OFFICER, "EOD-R1")
        self.assertEqual(run["state"], "failed")
        self.assertEqual(self.service.ledger(OFFICER, "B-DAY1"), [])
        # 重试同一run：从上次完整批次之后继续（本次为首个批次），账务只出现一次
        retry = self.service.run_eod(OFFICER, "EOD-R1")
        self.assertEqual(retry["state"], "completed")
        ledger = self.service.ledger(OFFICER, "B-DAY1")
        self.assertEqual(len(ledger), 2)
        keys = sorted(item["idempotency_key"] for item in ledger)
        self.assertEqual(keys, ["settle:B-DAY1:ACME", "settle:B-DAY1:GLOBEX"])
        self.assertEqual(retry["processed_batches"], ["B-DAY1"])


def receipt(batch_key, instrument, quantity, cash):
    return {"batch_key": batch_key, "instrument": instrument,
            "delivered_quantity": quantity, "cash_paid": cash, "received_from": "CUSTODY"}
