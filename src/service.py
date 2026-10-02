"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from . import eod
from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, choice, integer, number, text
from .repository import Repository
from .rules import DomainRules


EOD_ROLE = "settlement_officer"
ENTITLEMENT_ROLE = "corporate_actions"
RECONCILIATION_ROLES = {"settlement_officer", "reconciliation_clerk"}


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
        if not (self.rules.known_role(actor.role) or actor.role == "reconciliation_clerk"):
            raise PermissionDenied("角色无权访问该服务")

    def _require_role(self, actor: Actor, roles: set) -> None:
        if actor.role != "admin" and actor.role not in roles:
            raise PermissionDenied("角色无权执行该操作")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

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

    # --------------------------------------------------------- 公司行动权益
    def publish_entitlement(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        """发布权益新版本。未关账批次的冻结快照随之失效，必须重新确认。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, {ENTITLEMENT_ROLE})
        instrument = text(payload or {}, "instrument")
        event_type = choice(payload, "event_type", ["split", "dividend", "merger"])
        ratio = number(payload, "ratio", 0.01)
        cash_rate = number(payload, "cash_rate", 0.0) if event_type == "dividend" else 0.0
        with self.repository._connect() as connection:
            version = self.repository.next_entitlement_version(connection, instrument)
        return self.repository.publish_entitlement(instrument, version, event_type, ratio, cash_rate, actor.user_id)

    def list_entitlements(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return list(self.repository.active_entitlements().values())

    # --------------------------------------------------------------- EOD批次
    def open_batch(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, {EOD_ROLE})
        batch_key = text(payload or {}, "batch_key")
        settlement_day = int(payload["settlement_day"]) if isinstance(payload.get("settlement_day"), int) and not isinstance(payload.get("settlement_day"), bool) else 0
        return self.repository.ensure_open_batch(batch_key, settlement_day, actor.user_id)

    def get_batch(self, actor: Actor, batch_key: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch = self.repository.get_batch(batch_key)
        batch["ledger"] = self.repository.ledger_for_batch(batch_key)
        batch["receipts"] = self.repository.receipts_for_batch(batch_key)
        return batch

    def list_batches(self, actor: Actor, state: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_batches(state=state)

    # --------------------------------------------------------------- 交收回执
    def ingest_receipt(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        """登记交收回执。

        - (批次,券号) 重复到达且内容一致：幂等忽略，只计一次；
        - 内容与首条不一致：冲突，拒绝覆盖；
        - 批次已确认未关账：回到开放态，确认时按已冻结版本重算；
        - 批次已关账：不改已确认权益与账务，直接进待对账队列。
        """
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, {EOD_ROLE})
        batch_key = text(payload or {}, "batch_key")
        instrument = text(payload, "instrument")
        delivered_quantity = integer(payload, "delivered_quantity", 0)
        cash_paid = number(payload, "cash_paid", 0)
        received_from = text(payload, "received_from")
        settlement_day = int(payload["settlement_day"]) if isinstance(payload.get("settlement_day"), int) and not isinstance(payload.get("settlement_day"), bool) else 0

        try:
            batch = self.repository.get_batch(batch_key)
        except Exception:
            batch = self.repository.ensure_open_batch(batch_key, settlement_day, actor.user_id)
        if batch["state"] == eod.BATCH_CLOSED:
            self.repository.upsert_pending_reconciliation(batch_key, instrument, "关账后到达的交收回执")
            self.repository.add_eod_event(
                "receipt", "%s:%s" % (batch_key, instrument), "post_close_queued", actor.user_id,
                {"delivered_quantity": delivered_quantity, "cash_paid": cash_paid},
            )
            return {"outcome": "queued_for_reconciliation", "batch_key": batch_key, "instrument": instrument}

        result = self.repository.upsert_receipt(
            batch_key, instrument, delivered_quantity, cash_paid, received_from, actor.user_id
        )
        result["batch_key"] = batch_key
        result["instrument"] = instrument
        if result["outcome"] == "conflict":
            raise Conflict("券号%s已有内容不同的回执，不能用后到回执覆盖" % instrument)
        return result

    def list_receipts(self, actor: Actor, batch_key: str) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.receipts_for_batch(batch_key)

    # --------------------------------------------------------- 确认交收/关账
    def _settlement_inputs(self, batch_key: str):
        instructions = self.repository.list_instructions_for_batch(batch_key)
        if not instructions:
            raise ValidationError("批次%s没有可交收的结算指令" % batch_key)
        receipts = {item["instrument"]: item for item in self.repository.receipts_for_batch(batch_key)}
        return instructions, receipts

    def confirm_settlement(self, actor: Actor, batch_key: str, expected_version: int, run_key: Optional[str] = None) -> Dict[str, Any]:
        """确认交收：冻结当时权益版本，按回执(批次,券号)去重结果记账。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, {EOD_ROLE})
        batch_key = text({"batch_key": batch_key}, "batch_key")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValidationError("expected_version必须是正整数")
        self.repository.get_batch(batch_key)
        instructions, receipts = self._settlement_inputs(batch_key)
        instruments = [item["payload"]["instrument"] for item in instructions]
        active = self.repository.active_entitlements()
        entitlements = {
            key: eod.Entitlement(
                instrument=str(value["instrument"]),
                version=int(value["version"]),
                event_type=str(value["event_type"]),
                ratio=float(value["ratio"]),
                cash_rate=float(value["cash_rate"]),
            )
            for key, value in active.items()
        }
        snapshot = eod.build_snapshot(instruments, entitlements)
        ledger = eod.summarize_batch(instructions, receipts, snapshot)
        for item in ledger:
            item["idempotency_key"] = eod.settlement_idempotency_key(batch_key, item["instrument"])
        return self.repository.confirm_batch(batch_key, expected_version, snapshot, ledger, actor.user_id, run_key=run_key)

    def close_batch(self, actor: Actor, batch_key: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, {EOD_ROLE})
        return self.repository.close_batch(batch_key, actor.user_id)

    def run_eod(self, actor: Actor, run_key: Optional[str] = None) -> Dict[str, Any]:
        """日终处理：按批次顺序提交，已完成批次在检查点中；失败后重跑从下个未完成批次继续。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, {EOD_ROLE})
        run_key = run_key or ("EOD-" + _timestamp_key())
        run = self.repository.ensure_run(run_key, actor.user_id)
        completed = set(run["processed_batches"])
        open_batches = [batch for batch in self.repository.list_batches(eod.BATCH_OPEN) if batch["batch_key"] not in completed]
        failed_batch: Optional[str] = None
        for batch in open_batches:
            try:
                self.confirm_settlement(actor, batch["batch_key"], int(batch["version"]), run_key=run_key)
                completed.add(batch["batch_key"])
            except (ValidationError, Conflict) as exc:
                # 业务性失败：记录本批次原因，日终停在该批次，下一个批次不动。
                failed_batch = batch["batch_key"]
                self.repository.add_eod_event("run", run_key, "batch_failed", actor.user_id,
                                              {"batch_key": failed_batch, "reason": str(exc)})
                self.repository.mark_run_finished(run_key, "failed")
                run = self.repository.get_run(run_key)
                run["failed_batch"] = failed_batch
                run["reason"] = str(exc)
                return run
            except Exception:
                # 写库失败：该批次事务已整体回滚；标记run失败并抛出，调用方按检查点重试，不重复记账。
                self.repository.mark_run_finished(run_key, "failed")
                raise
        state = "completed" if not self.repository.list_batches(eod.BATCH_OPEN) else "completed_with_pending"
        self.repository.mark_run_finished(run_key, state)
        run = self.repository.get_run(run_key)
        run["failed_batch"] = None
        return run

    # ----------------------------------------------------------------- 对账
    def list_reconciliation(self, actor: Actor, status: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_reconciliation_items(status=status)

    def complete_reconciliation(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        """关账后回执补全：生成新结果批次，只记差额，不倒改原批次权益与账务。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, RECONCILIATION_ROLES)
        source_batch_key = text(payload or {}, "source_batch_key")
        instrument = text(payload, "instrument")
        delivered_quantity = integer(payload, "delivered_quantity", 0)
        cash_paid = number(payload, "cash_paid", 0)
        return self.repository.resolve_reconciliation(
            source_batch_key, instrument, delivered_quantity, cash_paid, actor.user_id
        )

    def ledger(self, actor: Actor, batch_key: str) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get_batch(batch_key)
        return self.repository.ledger_for_batch(batch_key)

    def get_run(self, actor: Actor, run_key: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_run(run_key)

    def eod_timeline(self, actor: Actor, entity_kind: str, entity_key: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.eod_timeline(entity_kind, entity_key)


def _timestamp_key() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
