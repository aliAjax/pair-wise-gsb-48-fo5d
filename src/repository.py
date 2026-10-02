"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound
from .eod import (
    ITEM_PENDING,
    LEDGER_ADJUSTMENT,
    adjustment_idempotency_key,
    reconciliation_key,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        # 测试接缝：在批次确认事务内、提交前抛出异常，用于模拟写库失败后的重试。
        self.confirm_fault_inject: Optional[Exception] = None
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS entitlement_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    ratio REAL NOT NULL,
                    cash_rate REAL NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    published_by TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    UNIQUE(instrument, version)
                );
                CREATE TABLE IF NOT EXISTS eod_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_key TEXT NOT NULL UNIQUE,
                    settlement_day INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    entitlement_snapshot TEXT NOT NULL DEFAULT '{}',
                    origin TEXT NOT NULL DEFAULT 'settlement',
                    source_batch_key TEXT,
                    closed_at TEXT,
                    closed_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settlement_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_key TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    delivered_quantity INTEGER NOT NULL,
                    cash_paid REAL NOT NULL,
                    received_from TEXT NOT NULL DEFAULT '',
                    ingest_count INTEGER NOT NULL DEFAULT 1,
                    ingested_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_key, instrument)
                );
                CREATE TABLE IF NOT EXISTS ledger_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_key TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    entry_type TEXT NOT NULL,
                    securities_qty INTEGER NOT NULL,
                    cash_amount REAL NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reconciliation_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_batch_key TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    delivered_quantity INTEGER,
                    cash_paid REAL,
                    result_batch_key TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_batch_key, instrument)
                );
                CREATE TABLE IF NOT EXISTS eod_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    last_completed_batch TEXT,
                    processed_batches TEXT NOT NULL DEFAULT '[]',
                    started_by TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS eod_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_kind TEXT NOT NULL,
                    entity_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_entitlement_instrument ON entitlement_versions(instrument, version);
                CREATE INDEX IF NOT EXISTS idx_batches_day ON eod_batches(settlement_day, state);
                CREATE INDEX IF NOT EXISTS idx_receipts_batch ON settlement_receipts(batch_key);
                CREATE INDEX IF NOT EXISTS idx_ledger_batch ON ledger_entries(batch_key);
                CREATE INDEX IF NOT EXISTS idx_recon_status ON reconciliation_items(status, source_batch_key);
                CREATE INDEX IF NOT EXISTS idx_eod_events_entity ON eod_events(entity_kind, entity_key, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    # ------------------------------------------------------------------ 指令
    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, _dump(payload), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, _dump({"state": state}), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def list_instructions_for_batch(self, batch_key: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM records ORDER BY id").fetchall()
        instructions = [self._row(row) for row in rows]
        return [instruction for instruction in instructions if instruction["payload"].get("batch_key") == batch_key]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, _dump(payload), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, _dump(details), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), _dump(details), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------ 权益版本
    def active_entitlements(self) -> Dict[str, Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entitlement_versions WHERE is_active=1"
            ).fetchall()
        return {str(row["instrument"]): dict(row) for row in rows}

    def publish_entitlement(
        self,
        instrument: str,
        version: int,
        event_type: str,
        ratio: float,
        cash_rate: float,
        actor_id: str,
    ) -> Dict[str, Any]:
        """发布新版本：旧版本失活；未关账批次失效并清除其临时账务，已关账批次不受影响。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            open_batches = {
                str(row["batch_key"])
                for row in connection.execute("SELECT batch_key FROM eod_batches WHERE state!='closed'").fetchall()
            }
            affected = set()
            for record_row in connection.execute("SELECT payload FROM records").fetchall():
                payload = json.loads(record_row["payload"])
                if payload.get("instrument") == instrument and payload.get("batch_key") in open_batches:
                    affected.add(str(payload["batch_key"]))
            affected = sorted(affected)
            connection.execute("UPDATE entitlement_versions SET is_active=0 WHERE instrument=?", (instrument,))
            cursor = connection.execute(
                "INSERT INTO entitlement_versions(instrument,version,event_type,ratio,cash_rate,is_active,published_by,published_at) "
                "VALUES(?,?,?,?,?,1,?,?)",
                (instrument, version, event_type, ratio, cash_rate, actor_id, now),
            )
            entitlement_id = int(cursor.lastrowid)
            for batch_key in affected:
                connection.execute(
                    "UPDATE eod_batches SET state='open', version=version+1, entitlement_snapshot='{}', updated_at=? WHERE batch_key=? AND state!='closed'",
                    (now, batch_key),
                )
                connection.execute("DELETE FROM ledger_entries WHERE batch_key=? AND entry_type='settlement'", (batch_key,))
                connection.execute(
                    "INSERT INTO eod_events(entity_kind,entity_key,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                    ("batch", batch_key, "entitlement_invalidated", actor_id, _dump({"instrument": instrument, "new_version": version}), now),
                )
            connection.execute(
                "INSERT INTO eod_events(entity_kind,entity_key,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                ("entitlement", instrument, "published", actor_id, _dump({"version": version, "event_type": event_type}), now),
            )
            connection.commit()
        return self.get_entitlement(entitlement_id)

    def get_entitlement(self, entitlement_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM entitlement_versions WHERE id=?", (entitlement_id,)).fetchone()
        if row is None:
            raise NotFound("权益版本不存在")
        return dict(row)

    def next_entitlement_version(self, connection: sqlite3.Connection, instrument: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(version),0) AS max_version FROM entitlement_versions WHERE instrument=?",
            (instrument,),
        ).fetchone()
        return int(row["max_version"]) + 1

    # -------------------------------------------------------------- EOD批次
    def get_batch(self, batch_key: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM eod_batches WHERE batch_key=?", (batch_key,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        return self._batch_row(row)

    def list_batches(self, state: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM eod_batches WHERE state=? ORDER BY id", (state,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM eod_batches ORDER BY id").fetchall()
        return [self._batch_row(row) for row in rows]

    @staticmethod
    def _batch_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["entitlement_snapshot"] = json.loads(item["entitlement_snapshot"])
        return item

    def ensure_open_batch(self, batch_key: str, settlement_day: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM eod_batches WHERE batch_key=?", (batch_key,)).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO eod_batches(batch_key,settlement_day,state,version,entitlement_snapshot,origin,created_at,updated_at) "
                    "VALUES(?,?,'open',1,'{}','settlement',?,?)",
                    (batch_key, settlement_day, now, now),
                )
                connection.execute(
                    "INSERT INTO eod_events(entity_kind,entity_key,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                    ("batch", batch_key, "opened", actor_id, _dump({"settlement_day": settlement_day}), now),
                )
            elif str(row["state"]) != "open":
                connection.rollback()
                raise Conflict("批次%s已%s，不能继续接收回执" % (batch_key, row["state"]))
            result = connection.execute("SELECT * FROM eod_batches WHERE batch_key=?", (batch_key,)).fetchone()
            connection.commit()
        return self._batch_row(result)

    def receipts_for_batch(self, batch_key: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM settlement_receipts WHERE batch_key=? ORDER BY instrument", (batch_key,)
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_receipt(
        self,
        batch_key: str,
        instrument: str,
        delivered_quantity: int,
        cash_paid: float,
        received_from: str,
        actor_id: str,
    ) -> Dict[str, str]:
        """回执幂等落库：(批次,券号) 唯一，重复到达只算一次。

        - 内容一致的重复回执：忽略，只累加 ingest_count；
        - 内容不同且批次已确认（快照冻结）：冲突拒绝，不能倒改；
        - 内容不同但批次仍开放（如权益更新后重新交收）：允许更正首条回执。

        返回 outcome=inserted/duplicate/replaced/conflict；conflict 时回滚。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state_row = connection.execute("SELECT state FROM eod_batches WHERE batch_key=?", (batch_key,)).fetchone()
            locked = state_row is not None and str(state_row["state"]) != "open"
            existing = connection.execute(
                "SELECT * FROM settlement_receipts WHERE batch_key=? AND instrument=?",
                (batch_key, instrument),
            ).fetchone()
            if existing is not None:
                same = int(existing["delivered_quantity"]) == int(delivered_quantity) and round(float(existing["cash_paid"]), 2) == round(float(cash_paid), 2)
                if same:
                    connection.execute(
                        "UPDATE settlement_receipts SET ingest_count=ingest_count+1, updated_at=? WHERE id=?",
                        (now, int(existing["id"])),
                    )
                    connection.execute(
                        "INSERT INTO eod_events(entity_kind,entity_key,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                        ("receipt", "%s:%s" % (batch_key, instrument), "duplicate_ignored", actor_id,
                         _dump({"ingest_count": int(existing["ingest_count"]) + 1}), now),
                    )
                    outcome = "duplicate"
                elif locked:
                    connection.rollback()
                    return {"outcome": "conflict"}
                else:
                    connection.execute(
                        "UPDATE settlement_receipts SET delivered_quantity=?, cash_paid=?, received_from=?, updated_at=? WHERE id=?",
                        (int(delivered_quantity), round(float(cash_paid), 2), received_from, now, int(existing["id"])),
                    )
                    connection.execute(
                        "INSERT INTO eod_events(entity_kind,entity_key,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                        ("receipt", "%s:%s" % (batch_key, instrument), "receipt_replaced", actor_id,
                         _dump({"delivered_quantity": delivered_quantity, "cash_paid": cash_paid}), now),
                    )
                    outcome = "replaced"
            else:
                connection.execute(
                    "INSERT INTO settlement_receipts(batch_key,instrument,delivered_quantity,cash_paid,received_from,ingest_count,ingested_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,1,?,?,?)",
                    (batch_key, instrument, delivered_quantity, cash_paid, received_from, actor_id, now, now),
                )
                connection.execute(
                    "INSERT INTO eod_events(entity_kind,entity_key,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                    ("receipt", "%s:%s" % (batch_key, instrument), "received", actor_id,
                     _dump({"delivered_quantity": delivered_quantity, "cash_paid": cash_paid}), now),
                )
                outcome = "inserted"
            connection.commit()
        return {"outcome": outcome}

    def ledger_for_batch(self, batch_key: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ledger_entries WHERE batch_key=? ORDER BY instrument,id", (batch_key,)
            ).fetchall()
        return [dict(row) for row in rows]

    def confirm_batch(
        self,
        batch_key: str,
        expected_version: int,
        snapshot: Dict[str, Any],
        ledger: List[Dict[str, Any]],
        actor_id: str,
        run_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """确认交收（每批次一个原子事务）：

        - 条件更新 state='open' AND version=?，两个交收员同时提交只放行一位；
        - 冻结权益快照、按幂等键写账务，失败整体回滚不留半截账；
        - 顺手推进日终 run 的检查点，重试时从下一个未完成批次继续。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM eod_batches WHERE batch_key=?", (batch_key,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("批次不存在")
            if str(row["state"]) != "open" or int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("批次确认冲突：该批次可能已被其他交收员确认或权益已更新，请刷新后重试")
            for item in ledger:
                connection.execute(
                    "INSERT OR IGNORE INTO ledger_entries(batch_key,instrument,entry_type,securities_qty,cash_amount,idempotency_key,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (batch_key, item["instrument"], item.get("entry_type", "settlement"),
                     int(item["securities_qty"]), round(float(item["cash_amount"]), 2),
                     item["idempotency_key"], actor_id, now),
                )
            if self.confirm_fault_inject is not None:
                fault = self.confirm_fault_inject
                connection.rollback()
                raise fault
            updated = connection.execute(
                "UPDATE eod_batches SET state='confirmed', version=version+1, entitlement_snapshot=?, updated_at=? "
                "WHERE batch_key=? AND state='open' AND version=?",
                (_dump(snapshot), now, batch_key, int(expected_version)),
            )
            if updated.rowcount != 1:
                connection.rollback()
                raise Conflict("批次确认冲突：该批次可能已被其他交收员确认，请刷新后重试")
            connection.execute(
                "INSERT INTO eod_events(entity_kind,entity_key,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                ("batch", batch_key, "confirmed", actor_id,
                 _dump({"version": int(expected_version) + 1, "instruments": sorted(snapshot.keys())}), now),
            )
            self._advance_run_locked(connection, batch_key, actor_id, now, run_key)
            result = connection.execute("SELECT * FROM eod_batches WHERE batch_key=?", (batch_key,)).fetchone()
            connection.commit()
        return self._batch_row(result)

    def close_batch(self, batch_key: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM eod_batches WHERE batch_key=?", (batch_key,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("批次不存在")
            if str(row["state"]) != "confirmed":
                connection.rollback()
                raise Conflict("只有已确认的批次才能关账，当前状态：%s" % row["state"])
            connection.execute(
                "UPDATE eod_batches SET state='closed', version=version+1, closed_at=?, closed_by=?, updated_at=? WHERE batch_key=?",
                (now, actor_id, now, batch_key),
            )
            connection.execute(
                "INSERT INTO eod_events(entity_kind,entity_key,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                ("batch", batch_key, "closed", actor_id, _dump({"closed_at": now}), now),
            )
            result = connection.execute("SELECT * FROM eod_batches WHERE batch_key=?", (batch_key,)).fetchone()
            connection.commit()
        return self._batch_row(result)

    def upsert_pending_reconciliation(self, source_batch_key: str, instrument: str, reason: str) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO reconciliation_items(source_batch_key,instrument,reason,status,created_at,updated_at) "
                "VALUES(?,?,?,'pending',?,?) ON CONFLICT(source_batch_key,instrument) DO UPDATE SET status='pending', updated_at=excluded.updated_at",
                (source_batch_key, instrument, reason, now, now),
            )

    def list_reconciliation_items(self, status: Optional[str] = None, source_batch_key: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM reconciliation_items WHERE 1=1"
        params: List[Any] = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if source_batch_key:
            sql += " AND source_batch_key=?"
            params.append(source_batch_key)
        sql += " ORDER BY id"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def resolve_reconciliation(
        self,
        source_batch_key: str,
        instrument: str,
        delivered_quantity: int,
        cash_paid: float,
        actor_id: str,
    ) -> Dict[str, Any]:
        """关账后补全：生成 #RCn 新结果批次，只把差额作为调整账务记账。

        冻结原批次权益快照的副本，原批次、原账务一律不修改。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                "SELECT * FROM reconciliation_items WHERE source_batch_key=? AND instrument=?",
                (source_batch_key, instrument),
            ).fetchone()
            if item is None:
                connection.rollback()
                raise NotFound("待对账事项不存在")
            if str(item["status"]) != ITEM_PENDING:
                connection.rollback()
                raise Conflict("该对账事项已补全，结果批次：%s" % item["result_batch_key"])
            source = connection.execute("SELECT * FROM eod_batches WHERE batch_key=?", (source_batch_key,)).fetchone()
            if source is None or str(source["state"]) != "closed":
                connection.rollback()
                raise Conflict("只能对已关账批次执行对账补全")
            existing_rc = connection.execute(
                "SELECT COUNT(*) AS total FROM eod_batches WHERE source_batch_key=? AND origin='reconciliation'",
                (source_batch_key,),
            ).fetchone()
            sequence = int(existing_rc["total"]) + 1
            result_key = reconciliation_key(source_batch_key, sequence)
            snapshot = json.loads(source["entitlement_snapshot"])
            connection.execute(
                "INSERT INTO eod_batches(batch_key,settlement_day,state,version,entitlement_snapshot,origin,source_batch_key,closed_at,closed_by,created_at,updated_at) "
                "VALUES(?,?, 'closed',1,?, 'reconciliation',?, ?,?, ?,?)",
                (result_key, int(source["settlement_day"]), source["entitlement_snapshot"], source_batch_key, now, actor_id, now, now),
            )
            booked = connection.execute(
                "SELECT securities_qty, cash_amount FROM ledger_entries WHERE batch_key=? AND instrument=? ORDER BY id",
                (source_batch_key, instrument),
            ).fetchall()
            booked_qty = sum(int(r["securities_qty"]) for r in booked)
            booked_cash = round(sum(float(r["cash_amount"]) for r in booked), 2)
            delta_qty = int(delivered_quantity) - booked_qty
            delta_cash = round(float(cash_paid) - booked_cash, 2)
            connection.execute(
                "INSERT OR IGNORE INTO ledger_entries(batch_key,instrument,entry_type,securities_qty,cash_amount,idempotency_key,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (result_key, instrument, LEDGER_ADJUSTMENT, delta_qty, delta_cash,
                 adjustment_idempotency_key(source_batch_key, instrument, sequence), actor_id, now),
            )
            connection.execute(
                "UPDATE reconciliation_items SET status='resolved', delivered_quantity=?, cash_paid=?, result_batch_key=?, updated_at=? WHERE id=?",
                (int(delivered_quantity), round(float(cash_paid), 2), result_key, now, int(item["id"])),
            )
            connection.execute(
                "INSERT INTO eod_events(entity_kind,entity_key,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                ("batch", result_key, "reconciliation_result", actor_id,
                 _dump({"source_batch_key": source_batch_key, "instrument": instrument,
                        "delta_securities_qty": delta_qty, "delta_cash_amount": delta_cash,
                        "frozen_snapshot": snapshot.get(instrument, {})}), now),
            )
            result = connection.execute("SELECT * FROM eod_batches WHERE batch_key=?", (result_key,)).fetchone()
            connection.commit()
        return self._batch_row(result)

    # -------------------------------------------------------------- 日终run
    def ensure_run(self, run_key: str, actor_id: str) -> Dict[str, Any]:
        """幂等创建run；已存在的失败run重新打开续跑（检查点保留），运行中的run直接复用。"""
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO eod_runs(run_key,state,last_completed_batch,processed_batches,started_by,started_at) "
                "VALUES(?,'running',NULL,'[]',?,?)",
                (run_key, actor_id, now),
            )
            connection.execute(
                "UPDATE eod_runs SET state='running', finished_at=NULL WHERE run_key=? AND state='failed'",
                (run_key,),
            )
            row = connection.execute("SELECT * FROM eod_runs WHERE run_key=?", (run_key,)).fetchone()
            connection.commit()
        return self._run_row(row)

    def get_run(self, run_key: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM eod_runs WHERE run_key=?", (run_key,)).fetchone()
        if row is None:
            raise NotFound("日终处理批次不存在")
        return self._run_row(row)

    @staticmethod
    def _run_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["processed_batches"] = json.loads(item["processed_batches"])
        return item

    def _advance_run_locked(self, connection: sqlite3.Connection, batch_key: str, actor_id: str, now: str, run_key: Optional[str] = None) -> None:
        if run_key:
            rows = connection.execute(
                "SELECT id, processed_batches FROM eod_runs WHERE run_key=?", (run_key,)
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT id, processed_batches FROM eod_runs WHERE state='running' ORDER BY id DESC LIMIT 1"
            ).fetchall()
        for run in rows:
            processed = json.loads(run["processed_batches"])
            if batch_key not in processed:
                processed.append(batch_key)
            connection.execute(
                "UPDATE eod_runs SET state='running', last_completed_batch=?, processed_batches=? WHERE id=?",
                (batch_key, _dump(processed), int(run["id"])),
            )

    def mark_run_finished(self, run_key: str, state: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE eod_runs SET state=?, finished_at=? WHERE run_key=?",
                (state, _now(), run_key),
            )

    # -------------------------------------------------------------- EOD事件
    def add_eod_event(self, entity_kind: str, entity_key: str, action: str, actor_id: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO eod_events(entity_kind,entity_key,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                (entity_kind, entity_key, action, actor_id, _dump(details), _now()),
            )

    def eod_timeline(self, entity_kind: str, entity_key: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if entity_key is None:
                rows = connection.execute(
                    "SELECT * FROM eod_events WHERE entity_kind=? ORDER BY id", (entity_kind,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM eod_events WHERE entity_kind=? AND entity_key=? ORDER BY id",
                    (entity_kind, entity_key),
                ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result
