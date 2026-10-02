import tempfile
import unittest
from pathlib import Path
from threading import Barrier, Thread

from app import build_service
from src.domain import Actor, Conflict
from src.eod import BATCH_CONFIRMED, BATCH_OPEN


OFFICER_A = Actor("officer-a", "settlement_officer")
OFFICER_B = Actor("officer-b", "settlement_officer")


def make_instruction(service, reference="TRD-1", batch_key="B-CONC"):
    service.create(Actor("trader", "trader"), reference, {
        "instrument": "ACME", "side": "buy", "quantity": 1000, "price": 12.5,
        "fees": 18.0, "currency": "CNY", "settlement_day": 1,
        "corporate_action": "none", "action_ratio": 1.0, "batch_key": batch_key,
    })


class ConcurrentConfirmTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        make_instruction(self.service)
        self.service.ingest_receipt(OFFICER_A, {
            "batch_key": "B-CONC", "settlement_day": 1, "instrument": "ACME",
            "delivered_quantity": 1000, "cash_paid": 12518.0, "received_from": "CUSTODY",
        })

    def tearDown(self):
        self.temp.cleanup()

    def test_two_officers_only_one_confirmation_passes(self):
        results = []
        barrier = Barrier(2)

        def confirm(actor):
            barrier.wait()
            try:
                batch = self.service.confirm_settlement(actor, "B-CONC", 1)
                results.append(("ok", batch["state"], batch["version"], actor.user_id))
            except Conflict as exc:
                results.append(("conflict", str(exc), None, actor.user_id))

        t1 = Thread(target=confirm, args=(OFFICER_A,))
        t2 = Thread(target=confirm, args=(OFFICER_B,))
        t1.start()
        t2.start()
        t1.join(10)
        t2.join(10)

        self.assertEqual(len(results), 2)
        statuses = sorted(item[0] for item in results)
        self.assertEqual(statuses, ["conflict", "ok"])
        winner = next(item for item in results if item[0] == "ok")
        self.assertEqual(winner[1], BATCH_CONFIRMED)
        self.assertEqual(winner[2], 2)
        # 账务只记一次
        ledger = self.service.ledger(OFFICER_A, "B-CONC")
        self.assertEqual(len(ledger), 1)
        # 后到者刷新后看到已是确认态，不能再次确认
        batch = self.service.get_batch(OFFICER_B, "B-CONC")
        self.assertEqual(batch["state"], BATCH_CONFIRMED)
        with self.assertRaises(Conflict):
            self.service.confirm_settlement(OFFICER_B, "B-CONC", 2)
