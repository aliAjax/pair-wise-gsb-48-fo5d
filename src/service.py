"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, DomainError, PermissionDenied, text
from .repository import Repository
from .rules import BATCH_CLOSED, DomainRules


BATCH_ROLES = {'settlement_officer'}
ENTITLEMENT_ROLES = {'corporate_actions'}
RECEIPT_ROLES = {'custodian', 'settlement_officer'}


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _require_role(self, actor: Actor, roles: set) -> None:
        if actor.role != "admin" and actor.role not in roles:
            raise PermissionDenied("角色无权执行该操作")

    # ------------------------------------------------------------------
    # 结算指令
    # ------------------------------------------------------------------
    def create(self, actor: Actor, reference: str, payload: Dict[str, Any], batch_ref: Optional[str] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        batch_id = None
        if batch_ref:
            batch = self.repository.get_batch_by_ref(text({"batch_ref": batch_ref}, "batch_ref"))
            if batch["state"] != "open":
                from .domain import Conflict
                raise Conflict("只有未关账的开放批次能挂接结算指令")
            batch_id = batch["id"]
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id, batch_id=batch_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100, batch_id: Optional[int] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit, batch_id=batch_id)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ------------------------------------------------------------------
    # 日终批次
    # ------------------------------------------------------------------
    def create_batch(self, actor: Actor, reference: str, settlement_day: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, BATCH_ROLES)
        reference = text({"reference": reference}, "reference")
        from .domain import integer
        settlement_day = integer({"settlement_day": settlement_day}, "settlement_day", 0)
        return self.repository.create_batch(reference, settlement_day, actor.user_id)

    def list_batches(self, actor: Actor, state: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_batches(state=state)

    def get_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.batch_detail(batch_id)

    def _build_plan(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        instructions = self.repository.batch_instructions(batch["id"])
        receipts = self.repository.batch_receipts(batch["id"])
        instruments = sorted({item["payload"]["instrument"] for item in instructions})
        versions = self.repository.current_entitlement_versions(instruments)
        return self.rules.build_confirmation_plan(batch, instructions, receipts, versions)

    def confirm_batch(self, actor: Actor, batch_id: int, expected_version: Optional[int] = None, settlement_day: Optional[int] = None) -> Dict[str, Any]:
        """确认交收：冻结当时的权益版本；整个批次单事务记账，可安全重试。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, BATCH_ROLES)
        batch = self.repository.get_batch(batch_id)
        self.rules.require_confirmable(batch)
        if expected_version is None:
            expected_version = batch["version"]
        plan = self._build_plan(batch)
        return self.repository.confirm_batch(
            batch_id, int(expected_version), plan, actor.user_id, settlement_day=settlement_day
        )

    def close_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, BATCH_ROLES)
        batch = self.repository.get_batch(batch_id)
        self.rules.require_open_for_close(batch)
        return self.repository.close_batch(batch_id, actor.user_id)

    def run_eod(self, actor: Actor, settlement_day: int) -> Dict[str, Any]:
        """日终处理：从上次完整批次之后继续，未就绪批次阻断并上报，已记账批次重跑不重复。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, BATCH_ROLES)
        from .domain import integer
        settlement_day = integer({"settlement_day": settlement_day}, "settlement_day", 0)
        processed: List[Dict[str, Any]] = []
        last = self.repository.get_checkpoint(settlement_day)
        while True:
            candidates = self.repository.eod_candidates(settlement_day, after_batch_id=last)
            if not candidates:
                break
            batch = candidates[0]
            try:
                plan = self._build_plan(batch)
            except DomainError as exc:
                return {
                    "settlement_day": settlement_day,
                    "status": "blocked",
                    "checkpoint": last,
                    "processed": processed,
                    "blocked_batch": {"id": batch["id"], "reference": batch["reference"], "reason": str(exc)},
                }
            batch = self.repository.get_batch(batch["id"])
            confirmed = self.repository.confirm_batch(
                batch["id"], batch["version"], plan, actor.user_id, settlement_day=settlement_day
            )
            last = confirmed["id"]
            processed.append({"id": confirmed["id"], "reference": confirmed["reference"], "state": confirmed["state"]})
        return {"settlement_day": settlement_day, "status": "complete", "checkpoint": last, "processed": processed, "blocked_batch": None}

    # ------------------------------------------------------------------
    # 公司行动权益版本
    # ------------------------------------------------------------------
    def publish_entitlement(self, actor: Actor, instrument: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, ENTITLEMENT_ROLES)
        instrument = text({"instrument": instrument}, "instrument")
        terms = self.rules.validate_entitlement(data or {})
        return self.repository.publish_entitlement(instrument, terms, actor.user_id)

    def list_entitlements(self, actor: Actor, instrument: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_entitlements(instrument=instrument)

    # ------------------------------------------------------------------
    # 交收回执
    # ------------------------------------------------------------------
    def ingest_receipt(self, actor: Actor, batch_ref: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """登记回执：开放/已确认批次参与交收（按批次+券号去重）；关账批次转待对账。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, RECEIPT_ROLES)
        batch_ref = text({"batch_ref": batch_ref}, "batch_ref")
        receipt = self.rules.validate_receipt(data or {})
        batch = self.repository.get_batch_by_ref(batch_ref)
        if batch["state"] == BATCH_CLOSED:
            expected = self._expected_for_reconciliation(batch["id"], receipt["instrument"])
            return self.repository.ingest_reconciliation(batch["id"], receipt, expected, actor.user_id)
        return self.repository.ingest_receipt(batch["id"], receipt, actor.user_id, mark_stale=True)

    def _expected_for_reconciliation(self, batch_id: int, instrument: str) -> Dict[str, float]:
        instructions = self.repository.batch_instructions(batch_id)
        quantity = 0
        cash = 0.0
        for record in instructions:
            payload = record["payload"]
            if payload.get("instrument") != instrument:
                continue
            quantity += int(payload.get("effective_quantity", payload["quantity"]))
            cash += float(payload["net_amount"])
        return {"quantity": quantity, "cash": round(cash, 2)}

    # ------------------------------------------------------------------
    # 待对账
    # ------------------------------------------------------------------
    def list_reconciliation(self, actor: Actor, status: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_reconciliation(status=status)

    def complete_reconciliation(self, actor: Actor, item_id: int, adjustment_ref: Optional[str] = None) -> Dict[str, Any]:
        """补全关账后到达的回执：生成新的调整结果，不倒改已确认的权益和账务。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, BATCH_ROLES)
        item = self.repository.get_reconciliation_item(int(item_id))
        source = self.repository.get_batch(item["batch_id"])
        adjustment_ref = adjustment_ref or ("ADJ-%s-ITEM%s" % (source["reference"], item_id))
        return self.repository.complete_reconciliation(int(item_id), actor.user_id, adjustment_ref)

    def ledger(self, actor: Actor, batch_id: Optional[int] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.ledger_entries(batch_id=batch_id)
