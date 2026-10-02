"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(value: str) -> Any:
    return json.loads(value) if value else None


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)")}
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    batch_id INTEGER REFERENCES batches(id),
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
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    settlement_day INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    frozen_versions TEXT NOT NULL DEFAULT '{}',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS entitlements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    terms TEXT NOT NULL,
                    published_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(instrument, version)
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id),
                    instrument TEXT NOT NULL,
                    delivered_quantity INTEGER NOT NULL,
                    cash_paid REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'received',
                    received_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ledger_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    posting_key TEXT NOT NULL UNIQUE,
                    batch_id INTEGER NOT NULL REFERENCES batches(id),
                    record_id INTEGER REFERENCES records(id),
                    instrument TEXT NOT NULL,
                    entry_type TEXT NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 0,
                    amount REAL NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reconciliation_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id),
                    instrument TEXT NOT NULL,
                    delivered_quantity INTEGER NOT NULL,
                    cash_paid REAL NOT NULL,
                    expected_quantity INTEGER NOT NULL,
                    expected_cash REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    received_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reconciliation_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES reconciliation_items(id),
                    adjustment_batch_id INTEGER NOT NULL REFERENCES batches(id),
                    delta_quantity INTEGER NOT NULL,
                    delta_cash REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS eod_checkpoints (
                    settlement_day INTEGER PRIMARY KEY,
                    last_completed_batch_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_records_batch ON records(batch_id);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_batch_events ON batch_events(batch_id, id);
                CREATE INDEX IF NOT EXISTS idx_entitlement_instrument ON entitlements(instrument, version);
                CREATE INDEX IF NOT EXISTS idx_receipts_batch ON receipts(batch_id);
                CREATE INDEX IF NOT EXISTS idx_recon_batch ON reconciliation_items(batch_id, status);
                CREATE INDEX IF NOT EXISTS idx_ledger_batch ON ledger_entries(batch_id);
                """
            )
            if columns and "batch_id" not in columns:
                connection.execute("ALTER TABLE records ADD COLUMN batch_id INTEGER REFERENCES batches(id)")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_dedup ON receipts(batch_id, instrument) WHERE status != 'duplicate'"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_recon_dedup ON reconciliation_items(batch_id, instrument) WHERE status = 'pending'"
            )

    # ------------------------------------------------------------------
    # 基础映射
    # ------------------------------------------------------------------
    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _batch_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["frozen_versions"] = _loads(item["frozen_versions"]) or {}
        return item

    @staticmethod
    def _json_row(row: sqlite3.Row, fields: List[str]) -> Dict[str, Any]:
        item = dict(row)
        for field in fields:
            item[field] = _loads(item[field])
        return item

    # ------------------------------------------------------------------
    # 结算指令（records）
    # ------------------------------------------------------------------
    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, batch_id: Optional[int] = None) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,batch_id,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), batch_id, actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
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

    def list_records(self, state: Optional[str] = None, limit: int = 100, batch_id: Optional[int] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        sql = "SELECT * FROM records WHERE 1=1"
        params: List[Any] = []
        if state:
            sql += " AND state=?"
            params.append(state)
        if batch_id is not None:
            sql += " AND batch_id=?"
            params.append(batch_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]

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
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
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
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
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

    # ------------------------------------------------------------------
    # 日终批次
    # ------------------------------------------------------------------
    def create_batch(self, reference: str, settlement_day: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO batches(reference,settlement_day,state,version,frozen_versions,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, int(settlement_day), "open", 1, "{}", actor_id, now, now),
                )
                batch_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO batch_events(batch_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
                    (batch_id, "created", actor_id, json.dumps({"settlement_day": int(settlement_day)}, ensure_ascii=False), now),
                )
                row = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("批次reference已存在") from exc
        return self._batch_row(row)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        return self._batch_row(row)

    def get_batch_by_ref(self, reference: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM batches WHERE reference=?", (reference,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        return self._batch_row(row)

    def list_batches(self, state: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM batches WHERE state=? ORDER BY settlement_day, id", (state,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM batches ORDER BY settlement_day, id").fetchall()
        return [self._batch_row(row) for row in rows]

    def batch_instructions(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM records WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        return [self._row(row) for row in rows]

    def batch_receipts(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM receipts WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        return [dict(row) for row in rows]

    def batch_events(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM batch_events WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def batch_detail(self, batch_id: int) -> Dict[str, Any]:
        batch = self.get_batch(batch_id)
        batch["instructions"] = self.batch_instructions(batch_id)
        batch["receipts"] = self.batch_receipts(batch_id)
        batch["events"] = self.batch_events(batch_id)
        batch["ledger"] = self.ledger_entries(batch_id)
        return batch

    def _confirm_fault(self, connection: sqlite3.Connection) -> None:
        """测试故障注入点：确认提交前的钩子，默认空实现。"""
        return None

    def confirm_batch(self, batch_id: int, expected_version: int, plan: Dict[str, Any], actor_id: str, settlement_day: Optional[int] = None) -> Dict[str, Any]:
        """按确认计划一次性记账。整个批次在单事务内完成，所有写键幂等，可安全重试。"""
        now = _now()
        frozen = json.dumps(plan["frozen_versions"], ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state, version FROM batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                raise NotFound("批次不存在")
            if int(row["version"]) != int(expected_version):
                raise Conflict("批次版本冲突，其他交收员可能已提交，请刷新")
            if row["state"] not in ("open", "stale"):
                # 并发提交：只有一位交收员能把批次从 open/stale 改成 confirmed。
                raise Conflict("批次已被其他交收员确认，后到请求冲突")
            reconfirm = row["state"] == "stale"
            for item in plan["plans"]:
                record = connection.execute(
                    "SELECT id, version, state FROM records WHERE id=?", (item["record_id"],)
                ).fetchone()
                if record is None:
                    raise NotFound("结算指令不存在")
                # 幂等记账键：重复执行不会产生第二条分录。
                connection.execute(
                    "INSERT OR IGNORE INTO ledger_entries(posting_key,batch_id,record_id,instrument,entry_type,quantity,amount,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (item["posting_key"], batch_id, item["record_id"], item["instrument"], "settlement",
                     int(item["delivered_quantity"]), float(item["cash_paid"]), actor_id, now),
                )
                updated = connection.execute(
                    "UPDATE records SET state='settled', version=version+1, "
                    "payload=json_set(COALESCE(payload,'{}'), '$.delivered_quantity', ?, '$.cash_paid', ?), "
                    "updated_by=?, updated_at=? WHERE id=? AND state='approved'",
                    (int(item["delivered_quantity"]), float(item["cash_paid"]), actor_id, now, item["record_id"]),
                )
                if updated.rowcount == 1:
                    connection.execute(
                        "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                        (item["record_id"], "settle", actor_id, int(record["version"]) + 1,
                         json.dumps({"summary": "批次交收完成", "batch_id": batch_id, "delivered_quantity": item["delivered_quantity"], "cash_paid": item["cash_paid"]}, ensure_ascii=False, sort_keys=True),
                         now),
                    )
                # 已应用的回执打标；重跑时状态已为 applied，不受影响。
                connection.execute(
                    "UPDATE receipts SET status='applied' WHERE id=? AND status='received'",
                    (item["receipt_id"],),
                )
            connection.execute(
                "UPDATE batches SET state='confirmed', version=version+1, frozen_versions=?, updated_at=? WHERE id=?",
                (frozen, now, batch_id),
            )
            connection.execute(
                "INSERT INTO batch_events(batch_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
                (batch_id, "reconfirmed" if reconfirm else "confirmed", actor_id,
                 json.dumps({"frozen_versions": plan["frozen_versions"], "records": len(plan["plans"])}, ensure_ascii=False, sort_keys=True), now),
            )
            if settlement_day is not None:
                connection.execute(
                    "INSERT INTO eod_checkpoints(settlement_day,last_completed_batch_id,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(settlement_day) DO UPDATE SET last_completed_batch_id=excluded.last_completed_batch_id, updated_at=excluded.updated_at",
                    (int(settlement_day), batch_id, now),
                )
            self._confirm_fault(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_batch(batch_id)

    def close_batch(self, batch_id: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                raise NotFound("批次不存在")
            if row["state"] != "confirmed":
                raise Conflict("只有已确认且版本最新的批次才能关账")
            connection.execute("UPDATE batches SET state='closed', updated_at=? WHERE id=?", (now, batch_id))
            connection.execute(
                "INSERT INTO batch_events(batch_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
                (batch_id, "closed", actor_id, json.dumps({"frozen": True}, ensure_ascii=False), now),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_batch(batch_id)

    def eod_candidates(self, settlement_day: int, after_batch_id: int = 0) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM batches WHERE settlement_day=? AND state IN ('open','stale') AND id>? ORDER BY id",
                (int(settlement_day), int(after_batch_id)),
            ).fetchall()
        return [self._batch_row(row) for row in rows]

    def get_checkpoint(self, settlement_day: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_completed_batch_id FROM eod_checkpoints WHERE settlement_day=?",
                (int(settlement_day),),
            ).fetchone()
        return int(row["last_completed_batch_id"]) if row else 0

    # ------------------------------------------------------------------
    # 公司行动权益版本
    # ------------------------------------------------------------------
    def publish_entitlement(self, instrument: str, terms: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        """发布新版本；同一事务内把引用该券号、已确认未关账且冻结版本落后的批次置为 stale。"""
        now = _now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute(
                "SELECT MAX(version) AS v FROM entitlements WHERE instrument=?", (instrument,)
            ).fetchone()
            version = int(latest["v"] or 0) + 1
            cursor = connection.execute(
                "INSERT INTO entitlements(instrument,version,terms,published_by,created_at) VALUES(?,?,?,?,?)",
                (instrument, version, json.dumps(terms, ensure_ascii=False, sort_keys=True), actor_id, now),
            )
            entitlement_id = int(cursor.lastrowid)
            # records 的券号保存在 payload JSON 中，用 Python 解析冻结版本后决定哪些批次需要重确认。
            batch_rows = connection.execute("SELECT id, frozen_versions FROM batches WHERE state='confirmed'").fetchall()
            stale_ids = []
            for brow in batch_rows:
                instr_rows = connection.execute(
                    "SELECT 1 FROM records WHERE batch_id=? AND json_extract(payload, '$.instrument')=? LIMIT 1",
                    (brow["id"], instrument),
                ).fetchall()
                if not instr_rows:
                    continue
                frozen = _loads(brow["frozen_versions"]) or {}
                if int(frozen.get(instrument, 0)) < version:
                    stale_ids.append(int(brow["id"]))
            for stale_id in stale_ids:
                connection.execute("UPDATE batches SET state='stale', version=version+1, updated_at=? WHERE id=?", (now, stale_id))
                connection.execute(
                    "INSERT INTO batch_events(batch_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
                    (stale_id, "entitlement_changed", actor_id,
                     json.dumps({"instrument": instrument, "new_version": version}, ensure_ascii=False), now),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entitlement(entitlement_id)

    def get_entitlement(self, entitlement_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM entitlements WHERE id=?", (entitlement_id,)).fetchone()
        if row is None:
            raise NotFound("权益版本不存在")
        item = dict(row)
        item["terms"] = _loads(item["terms"])
        return item

    def current_entitlement_versions(self, instruments: List[str]) -> Dict[str, int]:
        if not instruments:
            return {}
        result = {}
        with self._connect() as connection:
            for instrument in sorted(set(instruments)):
                row = connection.execute(
                    "SELECT MAX(version) AS v FROM entitlements WHERE instrument=?", (instrument,)
                ).fetchone()
                result[instrument] = int(row["v"] or 0)
        return result

    def list_entitlements(self, instrument: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if instrument:
                rows = connection.execute(
                    "SELECT * FROM entitlements WHERE instrument=? ORDER BY version DESC", (instrument,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM entitlements ORDER BY instrument, version DESC"
                ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["terms"] = _loads(item["terms"])
            items.append(item)
        return items

    # ------------------------------------------------------------------
    # 交收回执
    # ------------------------------------------------------------------
    def ingest_receipt(self, batch_id: int, receipt: Dict[str, Any], actor_id: str, mark_stale: bool) -> Dict[str, Any]:
        """回执登记入库。(批次, 券号) 上只允许一张有效回执，重复到达登记为 duplicate 且不参与交收。"""
        now = _now()
        connection = self._connect()
        duplicate = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            brow = connection.execute("SELECT state, frozen_versions FROM batches WHERE id=?", (batch_id,)).fetchone()
            if brow is None:
                raise NotFound("批次不存在")
            existing = connection.execute(
                "SELECT id FROM receipts WHERE batch_id=? AND instrument=? AND status!='duplicate'",
                (batch_id, receipt["instrument"]),
            ).fetchone()
            if existing is not None:
                # 重复到达的回执按批次和券号只算一次：留痕但绝不参与记账。
                connection.execute(
                    "INSERT INTO receipts(batch_id,instrument,delivered_quantity,cash_paid,source,status,received_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (batch_id, receipt["instrument"], int(receipt["delivered_quantity"]), float(receipt["cash_paid"]),
                     receipt["source"], "duplicate", actor_id, now),
                )
                duplicate = True
            else:
                cursor = connection.execute(
                    "INSERT INTO receipts(batch_id,instrument,delivered_quantity,cash_paid,source,status,received_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (batch_id, receipt["instrument"], int(receipt["delivered_quantity"]), float(receipt["cash_paid"]),
                     receipt["source"], "received", actor_id, now),
                )
                receipt_id = int(cursor.lastrowid)
                if mark_stale and brow["state"] == "confirmed":
                    connection.execute("UPDATE batches SET state='stale', version=version+1, updated_at=? WHERE id=?", (now, batch_id))
                    connection.execute(
                        "INSERT INTO batch_events(batch_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
                        (batch_id, "receipt_changed", actor_id,
                         json.dumps({"instrument": receipt["instrument"], "receipt_id": receipt_id}, ensure_ascii=False), now),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM receipts WHERE batch_id=? AND instrument=? ORDER BY id DESC LIMIT 1",
                (batch_id, receipt["instrument"]),
            ).fetchone()
        result = dict(row)
        result["duplicate"] = duplicate
        return result

    def mark_receipt_applied(self, receipt_id: int) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE receipts SET status='applied' WHERE id=?", (receipt_id,))

    # ------------------------------------------------------------------
    # 关账后待对账
    # ------------------------------------------------------------------
    def ingest_reconciliation(self, batch_id: int, receipt: Dict[str, Any], expected: Dict[str, float], actor_id: str) -> Dict[str, Any]:
        """关账后到达的回执进入待对账：不动已确认的权益快照和账务。重复到达同样只算一次。"""
        now = _now()
        connection = self._connect()
        duplicate = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            brow = connection.execute("SELECT state FROM batches WHERE id=?", (batch_id,)).fetchone()
            if brow is None:
                raise NotFound("批次不存在")
            if brow["state"] != "closed":
                raise Conflict("仅关账批次的回执进入待对账")
            existing = connection.execute(
                "SELECT id FROM reconciliation_items WHERE batch_id=? AND instrument=? AND status='pending'",
                (batch_id, receipt["instrument"]),
            ).fetchone()
            if existing is not None:
                duplicate = True
            else:
                cursor = connection.execute(
                    "INSERT INTO reconciliation_items(batch_id,instrument,delivered_quantity,cash_paid,expected_quantity,expected_cash,source,status,received_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (batch_id, receipt["instrument"], int(receipt["delivered_quantity"]), float(receipt["cash_paid"]),
                     int(expected["quantity"]), float(expected["cash"]), receipt["source"], "pending", actor_id, now),
                )
                connection.execute(
                    "INSERT INTO batch_events(batch_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
                    (batch_id, "reconciliation_received", actor_id,
                     json.dumps({"instrument": receipt["instrument"], "duplicate": False}, ensure_ascii=False), now),
                )
                item_id = int(cursor.lastrowid)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if duplicate:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM reconciliation_items WHERE batch_id=? AND instrument=? AND status='pending'",
                    (batch_id, receipt["instrument"]),
                ).fetchone()
            result = dict(row)
        else:
            result = self.get_reconciliation_item(item_id)
        result["duplicate"] = duplicate
        return result

    def get_reconciliation_item(self, item_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM reconciliation_items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFound("待对账项不存在")
        return dict(row)

    def list_reconciliation(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM reconciliation_items WHERE status=? ORDER BY id", (status,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM reconciliation_items ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def complete_reconciliation(self, item_id: int, actor_id: str, adjustment_ref: str) -> Dict[str, Any]:
        """补全待对账项：生成独立的新结果批次和调整分录，原关账批次的权益与账务保持不变。"""
        now = _now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM reconciliation_items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFound("待对账项不存在")
            if row["status"] != "pending":
                raise Conflict("待对账项已补全，不能重复处理")
            source = connection.execute("SELECT state, settlement_day FROM batches WHERE id=?", (row["batch_id"],)).fetchone()
            if source["state"] != "closed":
                raise Conflict("原批次未关账，不能按对账补全处理")
            delta_qty = int(row["delivered_quantity"]) - int(row["expected_quantity"])
            delta_cash = round(float(row["cash_paid"]) - float(row["expected_cash"]), 2)
            try:
                cursor = connection.execute(
                    "INSERT INTO batches(reference,settlement_day,state,version,frozen_versions,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (adjustment_ref, int(source["settlement_day"]), "closed", 1, "{}", actor_id, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("调整批次reference已存在") from exc
            adjustment_batch_id = int(cursor.lastrowid)
            posting_key = "recon:item-%s" % item_id
            connection.execute(
                "INSERT OR IGNORE INTO ledger_entries(posting_key,batch_id,record_id,instrument,entry_type,quantity,amount,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (posting_key, adjustment_batch_id, None, row["instrument"], "reconciliation_adjustment",
                 delta_qty, delta_cash, actor_id, now),
            )
            connection.execute(
                "INSERT INTO reconciliation_results(item_id,adjustment_batch_id,delta_quantity,delta_cash,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, adjustment_batch_id, delta_qty, delta_cash, actor_id, now),
            )
            connection.execute(
                "UPDATE reconciliation_items SET status='resolved' WHERE id=?", (item_id,),
            )
            connection.execute(
                "INSERT INTO batch_events(batch_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
                (adjustment_batch_id, "reconciliation_adjusted", actor_id,
                 json.dumps({"source_batch_id": row["batch_id"], "instrument": row["instrument"], "delta_quantity": delta_qty, "delta_cash": delta_cash}, ensure_ascii=False), now),
            )
            connection.execute(
                "INSERT INTO batch_events(batch_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
                (row["batch_id"], "reconciliation_resolved", actor_id,
                 json.dumps({"item_id": item_id, "adjustment_batch_id": adjustment_batch_id}, ensure_ascii=False), now),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        item = self.get_reconciliation_item(item_id)
        with self._connect() as connection:
            result_row = connection.execute(
                "SELECT * FROM reconciliation_results WHERE item_id=?", (item_id,)
            ).fetchone()
        return {"item": item, "result": dict(result_row)}

    # ------------------------------------------------------------------
    # 账务分录
    # ------------------------------------------------------------------
    def ledger_entries(self, batch_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if batch_id is not None:
                rows = connection.execute("SELECT * FROM ledger_entries WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM ledger_entries ORDER BY id").fetchall()
        return [dict(row) for row in rows]
