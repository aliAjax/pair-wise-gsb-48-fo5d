"""证券结算与企业行动处理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Conflict, ValidationError, choice, integer, number, optional_text, text


INITIAL_STATE = "captured"
CREATE_ROLES = {'trader'}
ACTION_ROLES = {'apply_corporate': {'corporate_actions'}, 'approve': {'settlement_officer'}, 'settle': {'settlement_officer'}, 'fail': {'settlement_officer'}, 'reverse': {'corporate_actions', 'settlement_officer'}}
TRANSITIONS = {'apply_corporate': {'captured': 'adjusted'}, 'approve': {'captured': 'approved', 'adjusted': 'approved'}, 'settle': {'approved': 'settled'}, 'fail': {'approved': 'failed'}, 'reverse': {'settled': 'reversed', 'failed': 'reversed'}}

# 批次生命周期：open（未关账）→ confirmed（已确认、版本已冻结）→ closed（已关账）。
# 权益版本更新后，已确认但未关账的批次被标记为 stale，需要按新版本重新确认。
BATCH_OPEN = "open"
BATCH_CONFIRMED = "confirmed"
BATCH_CLOSED = "closed"
BATCH_STALE = "stale"
BATCH_CONFIRMABLE = {BATCH_OPEN, BATCH_STALE}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES) | {"settlement_officer", "custodian"}
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "instrument")
        choice(p, "side", ["buy", "sell"])
        integer(p, "quantity", 1)
        number(p, "price", 0.01)
        number(p, "fees", 0)
        choice(p, "currency", ["CNY", "USD", "HKD"])
        integer(p, "settlement_day", 0)
        choice(p, "corporate_action", ["none", "split", "dividend", "merger"])
        number(p, "action_ratio", 0.01)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        gross = float(p["quantity"]) * float(p["price"])
        fee = float(p["fees"])
        p["gross_amount"] = round(gross, 2)
        p["net_amount"] = round(gross + fee if p["side"] == "buy" else gross - fee, 2)
        p["adjusted_quantity"] = p["quantity"]
        p["adjusted_price"] = p["price"]
        if p["corporate_action"] == "split":
            p["adjusted_quantity"] = int(float(p["quantity"]) * float(p["action_ratio"]))
            p["adjusted_price"] = round(float(p["price"]) / float(p["action_ratio"]), 4)
        elif p["corporate_action"] == "dividend":
            p["cash_entitlement"] = round(float(p["quantity"]) * float(p["action_ratio"]), 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"settled", "reversed"} and item["payload"].get("instrument") == payload.get("instrument") and item["payload"].get("settlement_day") == payload.get("settlement_day"):
                if item["payload"].get("side") == payload.get("side") and item["payload"].get("quantity") == payload.get("quantity") and item["payload"].get("price") == payload.get("price"):
                    raise Conflict("疑似重复结算指令")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "apply_corporate":
            if p["corporate_action"] == "none":
                raise ValidationError("没有待处理的公司行动")
            changes["corporate_applied"] = True
            changes["effective_quantity"] = p["adjusted_quantity"]
            changes["effective_price"] = p["adjusted_price"]
            summary = "公司行动已应用"
        elif action == "approve":
            changes["approved_amount"] = p["net_amount"]
            summary = "结算指令复核通过"
        elif action == "settle":
            delivered = integer(data, "delivered_quantity", 0)
            paid = number(data, "cash_paid", 0)
            required_quantity = int(p.get("effective_quantity", p["quantity"]))
            if delivered != required_quantity:
                raise ValidationError("交收证券数量不匹配")
            if paid < float(p["net_amount"]):
                raise ValidationError("交收资金不足")
            changes["delivered_quantity"] = delivered
            changes["cash_paid"] = paid
            summary = "交收完成"
        elif action == "fail":
            changes["fail_reason"] = text(data, "fail_reason")
            summary = "交收失败"
        elif action == "reverse":
            changes["reverse_reason"] = text(data, "reverse_reason")
            summary = "交收冲正"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ------------------------------------------------------------------
    # 公司行动权益版本
    # ------------------------------------------------------------------
    def validate_entitlement(self, data: Dict[str, Any]) -> Dict[str, Any]:
        data = data or {}
        terms = {}
        terms["cash_per_share"] = number(data, "cash_per_share", 0)
        terms["quantity_ratio"] = number(data, "quantity_ratio", 0.0)
        terms["note"] = optional_text(data, "note")
        return terms

    # ------------------------------------------------------------------
    # 交收回执
    # ------------------------------------------------------------------
    def validate_receipt(self, data: Dict[str, Any]) -> Dict[str, Any]:
        data = data or {}
        receipt = {}
        receipt["instrument"] = text(data, "instrument")
        receipt["delivered_quantity"] = integer(data, "delivered_quantity", 0)
        receipt["cash_paid"] = number(data, "cash_paid", 0)
        receipt["source"] = optional_text(data, "source")
        return receipt

    def require_confirmable(self, batch: Dict[str, Any]) -> None:
        if batch["state"] == BATCH_CLOSED:
            raise Conflict("批次已关账，不能再次确认")
        if batch["state"] == BATCH_CONFIRMED:
            raise Conflict("批次已确认，权益版本无变化时无需重复确认")
        if batch["state"] not in BATCH_CONFIRMABLE:
            raise Conflict("当前批次状态不允许确认")

    def require_open_for_close(self, batch: Dict[str, Any]) -> None:
        if batch["state"] != BATCH_CONFIRMED:
            raise Conflict("只有已确认的批次才能关账")

    def require_fresh(self, batch: Dict[str, Any], current_versions: Dict[str, int]) -> bool:
        """确认前校验冻结版本：返回True表示版本与当前一致。

        stale 批次必须按最新权益版本重新确认；open 批次在确认瞬间冻结当前版本。
        """
        frozen = batch.get("frozen_versions") or {}
        for instrument, version in current_versions.items():
            if int(frozen.get(instrument, 0)) != int(version):
                return False
        return True

    def build_confirmation_plan(
        self,
        batch: Dict[str, Any],
        instructions: List[Dict[str, Any]],
        receipts: List[Dict[str, Any]],
        versions: Dict[str, int],
    ) -> Dict[str, Any]:
        """生成批次确认所需的交收与记账计划。

        每个券号在批次内只保留一张有效回执（入库时已按批次+券号去重）；
        指令必须已复核，回执数量必须与指令应交数量一致，资金不得少于应付净额。
        """
        self.require_confirmable(batch)
        # received：待确认的新回执；applied：首次确认已用回执，重新确认时据此复核（去重后仍只有一张）。
        active = [r for r in receipts if r["status"] in ("received", "applied")]
        receipts_by_instrument = {}
        for receipt in active:
            key = receipt["instrument"]
            if key in receipts_by_instrument:
                # 理论上不会发生：唯一索引已拦截，这里双保险保证“只算一次”。
                raise Conflict("批次内券号%s存在多张有效回执" % key)
            receipts_by_instrument[key] = receipt

        plans: List[Dict[str, Any]] = []
        for record in instructions:
            if record["state"] not in ("approved", "settled"):
                raise Conflict("结算指令%s未复核，不能确认交收" % record["reference"])
            payload = record["payload"]
            instrument = payload["instrument"]
            required_qty = int(payload.get("effective_quantity", payload["quantity"]))
            receipt = receipts_by_instrument.get(instrument)
            if receipt is None:
                raise Conflict("券号%s缺少交收回执，批次未就绪" % instrument)
            if int(receipt["delivered_quantity"]) != required_qty:
                raise Conflict("券号%s回执交收数量与指令不匹配" % instrument)
            if float(receipt["cash_paid"]) < float(payload["net_amount"]):
                raise Conflict("券号%s回执交收资金不足" % instrument)
            # settled 指令是重新确认场景：分录靠 posting_key 幂等保留，不重复记账。
            plans.append({
                "record_id": record["id"],
                "reference": record["reference"],
                "instrument": instrument,
                "delivered_quantity": required_qty,
                "cash_paid": float(receipt["cash_paid"]),
                "receipt_id": receipt["id"],
                "posting_key": self.confirmation_posting_key(batch["id"], record["id"]),
            })
        if not plans:
            raise Conflict("批次内没有可确认的结算指令")
        return {"frozen_versions": dict(versions), "plans": plans}

    @staticmethod
    def confirmation_posting_key(batch_id: int, record_id: int) -> str:
        return "settle:batch-%s:record-%s" % (batch_id, record_id)

    @staticmethod
    def adjustment_posting_key(batch_ref: str, source_batch_id: int, instrument: str) -> str:
        return "recon:batch-%s:%s:from-%s" % (batch_ref, instrument, source_batch_id)
