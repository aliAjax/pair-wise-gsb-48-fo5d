"""日终处理的纯领域逻辑：权益版本冻结、交收汇总、记账与对账差额。

本模块不访问数据库，全部为可单测的纯函数，便于在事务外完成计算、
在事务内完成落库（见 repository.Repository.confirm_batch）。
"""
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from .domain import ValidationError


BATCH_OPEN = "open"
BATCH_CONFIRMED = "confirmed"
BATCH_CLOSED = "closed"

ORIGIN_SETTLEMENT = "settlement"
ORIGIN_RECONCILIATION = "reconciliation"

LEDGER_SETTLEMENT = "settlement"
LEDGER_ADJUSTMENT = "reconciliation_adjustment"

ITEM_PENDING = "pending"
ITEM_RESOLVED = "resolved"

SPLIT_EVENTS = {"split", "merger"}


@dataclass(frozen=True)
class Entitlement:
    """某券号某一版公司行动权益。version=0 表示从未发布过权益。"""

    instrument: str
    version: int
    event_type: str
    ratio: float
    cash_rate: float

    def to_snapshot(self) -> Dict[str, Any]:
        return asdict(self)


def default_entitlement(instrument: str) -> Entitlement:
    return Entitlement(instrument=instrument, version=0, event_type="none", ratio=1.0, cash_rate=0.0)


def build_snapshot(instruments: List[str], active: Dict[str, Entitlement]) -> Dict[str, Dict[str, Any]]:
    """确认交收时冻结当时的权益版本，逐券号留档。"""
    snapshot: Dict[str, Dict[str, Any]] = {}
    for instrument in sorted(set(instruments)):
        entitlement = active.get(instrument) or default_entitlement(instrument)
        snapshot[instrument] = entitlement.to_snapshot()
    return snapshot


def _entry(snapshot: Dict[str, Dict[str, Any]], instrument: str) -> Dict[str, Any]:
    try:
        return snapshot[instrument]
    except KeyError as exc:
        raise ValidationError("券号%s缺少冻结权益版本" % instrument) from exc


def securities_due(quantity: int, snapshot_entry: Dict[str, Any]) -> int:
    factor = float(snapshot_entry["ratio"]) if snapshot_entry["event_type"] in SPLIT_EVENTS else 1.0
    return int(quantity * factor)


def cash_due(net_amount: float, quantity: int, snapshot_entry: Dict[str, Any]) -> float:
    amount = float(net_amount)
    if snapshot_entry["event_type"] == "dividend":
        amount -= quantity * float(snapshot_entry["cash_rate"])
    return round(max(0.0, amount), 2)


def summarize_batch(
    instructions: List[Dict[str, Any]],
    receipts: Dict[str, Dict[str, Any]],
    snapshot: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """按券号汇总指令的应收/应付，并与回执做交收完整性核对。

    返回每券号一条的记账明细；任一券号回执缺失或数量/资金不符则拒绝确认。
    """
    due_quantity: Dict[str, int] = {}
    due_cash: Dict[str, float] = {}
    for instruction in instructions:
        payload = instruction["payload"]
        instrument = payload["instrument"]
        entry = _entry(snapshot, instrument)
        due_quantity[instrument] = due_quantity.get(instrument, 0) + securities_due(int(payload["quantity"]), entry)
        due_cash[instrument] = round(due_cash.get(instrument, 0.0) + cash_due(float(payload["net_amount"]), int(payload["quantity"]), entry), 2)

    ledger: List[Dict[str, Any]] = []
    for instrument in sorted(set(due_quantity) | set(receipts)):
        receipt = receipts.get(instrument)
        if receipt is None:
            raise ValidationError("券号%s的交收回执尚未到达，无法确认交收" % instrument)
        expected_quantity = due_quantity.get(instrument, 0)
        expected_cash = due_cash.get(instrument, 0.0)
        delivered = int(receipt["delivered_quantity"])
        paid = round(float(receipt["cash_paid"]), 2)
        if delivered != expected_quantity:
            raise ValidationError("券号%s交收证券数量不匹配：应收%s，实收%s" % (instrument, expected_quantity, delivered))
        if paid < expected_cash:
            raise ValidationError("券号%s交收资金不足：应付%s，实付%s" % (instrument, expected_cash, paid))
        ledger.append({"instrument": instrument, "securities_qty": delivered, "cash_amount": paid})
    return ledger


def settlement_idempotency_key(batch_key: str, instrument: str) -> str:
    return "settle:%s:%s" % (batch_key, instrument)


def receipt_matches(existing: Dict[str, Any], delivered_quantity: int, cash_paid: float) -> bool:
    return int(existing["delivered_quantity"]) == int(delivered_quantity) and round(float(existing["cash_paid"]), 2) == round(float(cash_paid), 2)


def reconciliation_key(source_batch_key: str, sequence: int) -> str:
    return "%s#RC%d" % (source_batch_key, sequence)


def adjustment_idempotency_key(source_batch_key: str, instrument: str, sequence: int) -> str:
    return "rec:%s:%s:%d" % (source_batch_key, instrument, sequence)


def adjustment_entry(
    source_batch_key: str,
    instrument: str,
    sequence: int,
    booked: Optional[Dict[str, Any]],
    delivered_quantity: int,
    cash_paid: float,
) -> Dict[str, Any]:
    """关账后补全：以已冻结账务为基准只算差额，生成新结果而不倒改旧账。"""
    booked_quantity = int(booked["securities_qty"]) if booked else 0
    booked_cash = round(float(booked["cash_amount"]), 2) if booked else 0.0
    return {
        "instrument": instrument,
        "securities_qty": int(delivered_quantity) - booked_quantity,
        "cash_amount": round(float(cash_paid) - booked_cash, 2),
        "idempotency_key": adjustment_idempotency_key(source_batch_key, instrument, sequence),
    }
