"""Versioned, structured runtime observations for UI and future advisors.

This store deliberately keeps machine data separate from human-facing logs.  It
stores no screenshots and never raises into a game flow: telemetry may be lost,
but it must never make automation fail.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .runtime_paths import JP_DATA_DIR, LOG_DIR


TELEMETRY_SCHEMA_VERSION = 15
DEFAULT_RETENTION_DAYS = 90

# ── 资源总账（resource_ledger）契约常量 ──
# v2：缺口按波及资源降置信度（v1 是窗口内任何缺口把所有资源打成 low）
# v3：新增 balance_series（余额观察序列，供账房余额折线图取点）
LEDGER_SCHEMA_VERSION = 3
# 资源全集，顺序固定：顶栏四资源 + 真小判 + 甲州金 + 右栏两符
LEDGER_RESOURCES = ("木炭", "玉钢", "冷却材", "砥石", "小判", "甲州金", "委托符", "加速符")


def _resource_reading(reading):
    """旧客户端名称与资源字段对应；只在读取时转换，不改写历史。"""
    result = dict(reading or {})
    if "加速符·极" in result:
        result.setdefault("加速符", result.pop("加速符·极"))
    return result

# peek 只有顶栏五资源（契约：永远不含小判/委托符/加速符），
# 白名单过滤防脏 payload 污染小判观察链
_LEDGER_PEEK_RESOURCES = frozenset(("木炭", "玉钢", "冷却材", "砥石", "甲州金"))
_LEDGER_OBS_TYPES = ("inventory.captured", "inventory.peek", "osaka.koban_session")
_LEDGER_MERGE_SECONDS = 5.0  # 同一时刻多来源同值观察的去重窗口
_MANUAL_INVENTORY_SOURCES = frozenset(("manual_entry", "manual_import"))

try:
    from zoneinfo import ZoneInfo
    _LEDGER_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # Windows 无 tzdata 时兜底：上海 1991 年后无夏令时，固定 +8 够用
    _LEDGER_TZ = timezone(timedelta(hours=8))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, fallback):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


# 一圈出阵的结束事件类型 → 兜底结局（payload 没写 outcome 的老数据用）
SORTIE_LOOP_END_OUTCOMES = {
    "sortie.completed": "completed",
    "sortie.retreated_before_boss": "retreated_before_boss",
    "sortie.interrupted": "interrupted",
}


def _loop_pace_key(event_type: str, payload: dict):
    """圈事件的计时口径标识：同一玩法、同一张图才算同一口径。

    混合 workflow（异去+秘宝之里+锻刀…）的相邻完成事件间隔会把玩法
    切换时间摊进"平均圈速"，那种平均值不许再当统一圈速展示。
    """
    if event_type == "osaka.floor_completed":
        return ("osaka",)
    if event_type in ("sortie.completed", "sortie.retreated_before_boss"):
        return ("sortie", payload.get("mode"), payload.get("chapter"),
                payload.get("map_no"))
    if event_type == "raid.round_completed":
        return ("raid", payload.get("difficulty"))
    if event_type == "edocastle.run_completed":
        return ("edocastle", payload.get("difficulty"))
    if event_type == "hanafuda.run_completed":
        return ("hanafuda", payload.get("difficulty"))
    return (event_type,)


def pair_loop_records(events: list[dict]) -> list[dict]:
    """把 sortie.loop_started 和它的结束事件配成一条条逐圈事实。

    配对键 = (mode, chapter, map_no, sequence, attempt)：同 sequence 的
    重试靠 attempt 区分。没有结束事件的出发如实记「结果未知」
    (end_type=None)，绝不假定成功。老数据只有完成事件、没有出发事件的，
    照常出一条明细，attempt 记 None。一圈的耗时只认这一圈自己的两个
    事实：优先结束事件 payload 的 duration_seconds，缺了才用
    出发→结束的时间差；绝不拿整个任务的耗时当成圈的耗时。
    """

    def _key(payload: dict):
        return (payload.get("mode"), payload.get("chapter"),
                payload.get("map_no"), payload.get("sequence"),
                payload.get("attempt"))

    def _record(start: dict | None, end: dict | None) -> dict:
        end_payload = (end or {}).get("payload") or {}
        start_payload = (start or {}).get("payload") or {}
        payload = {**start_payload, **end_payload}  # 结束事实覆盖出发事实
        end_type = (end or {}).get("event_type")
        if end_type:
            outcome = payload.get("outcome") or SORTIE_LOOP_END_OUTCOMES[end_type]
        else:
            outcome = "unknown"  # 出发了但没等到结束事件
        duration = None
        if end is not None:
            value = end_payload.get("duration_seconds")
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and 0 < value:
                duration = round(float(value), 1)
            elif start is not None:
                duration = round(end["ts"] - start["ts"], 1)
        battle_count = payload.get("battle_count")
        if isinstance(battle_count, bool) or not isinstance(battle_count, int) \
                or battle_count < 0:
            battle_count = None
        drops = payload.get("drops_recognized")
        if isinstance(drops, bool) or not isinstance(drops, int) or drops < 0:
            drops = None
        return {
            "mode": payload.get("mode"),
            "chapter": payload.get("chapter"),
            "map_no": payload.get("map_no"),
            "team_no": payload.get("team_no"),
            "sequence": payload.get("sequence"),
            "attempt": payload.get("attempt"),
            "end_type": end_type,
            "outcome": outcome,
            "interrupt_reason": payload.get("interrupt_reason"),
            "duration_seconds": duration,
            "battle_count": battle_count,
            "battle_count_note": payload.get("battle_count_note"),
            "march_mode": payload.get("march_mode"),
            "drop_observation": payload.get("drop_observation"),
            "drops_recognized": drops,
            "drop_observation_reason": payload.get("drop_observation_reason"),
            "started_at": start["ts"] if start else None,
            "ended_at": end["ts"] if end else None,
        }

    open_starts: dict[tuple, list[dict]] = {}
    records: list[dict] = []
    for event in events:
        event_type = event.get("event_type")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event_type == "sortie.loop_started":
            open_starts.setdefault(_key(payload), []).append(event)
        elif event_type in SORTIE_LOOP_END_OUTCOMES:
            bucket = open_starts.get(_key(payload))
            start = bucket.pop(0) if bucket else None
            records.append(_record(start, event))
    for bucket in open_starts.values():  # 没闭合的出发：结果未知
        records.extend(_record(start, None) for start in bucket)
    records.sort(key=lambda r: (r.get("ended_at") or r.get("started_at") or 0))
    return records


class TelemetryStore:
    """Small WAL-backed event store safe for panel and worker processes."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or (LOG_DIR / "telemetry.db"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='sword_annotations'").fetchone()
        if exists and "serial_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(sword_annotations)")}:
            backup_path = Path(str(self.db_path) + ".pre-serial.bak")
            if not backup_path.exists():
                with sqlite3.connect(backup_path) as backup:
                    conn.backup(backup)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                script TEXT NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL,
                status TEXT NOT NULL DEFAULT 'running'
            );
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                run_id TEXT,
                script TEXT,
                kind TEXT NOT NULL,
                expected TEXT,
                match_mode TEXT,
                matched INTEGER,
                roi TEXT NOT NULL,
                tokens TEXT NOT NULL,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                run_id TEXT,
                script TEXT,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS human_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                occurred_at REAL NOT NULL,
                source TEXT NOT NULL,
                gap_key TEXT,
                activities TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS manual_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                script TEXT NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL NOT NULL,
                loops INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS sword_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at REAL NOT NULL,
                owned INTEGER,
                capacity INTEGER,
                sword_count INTEGER NOT NULL DEFAULT 0,
                missing INTEGER,
                source TEXT,
                completeness TEXT
            );
            CREATE TABLE IF NOT EXISTS sword_snapshot_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL,
                sword_id TEXT NOT NULL,
                name_zh TEXT NOT NULL,
                level INTEGER,
                tou_level INTEGER,
                survival INTEGER,
                survival_max INTEGER,
                fatigue INTEGER,
                fatigue_max INTEGER,
                stats TEXT NOT NULL DEFAULT '{}',
                kiwame_date TEXT,
                locked INTEGER,
                page_no INTEGER
            );
            -- v12 新表：刀帐人工标注（形态确认/要练的刀）。原地 CREATE 即可，
            -- 老库打开自动补表；标注是人工事实，不做历史回填。
            CREATE TABLE IF NOT EXISTS sword_annotations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sword_catalog_id TEXT NOT NULL,
                kiwame_date TEXT NOT NULL,
                level_at_mark INTEGER,
                level_confirmed INTEGER,
                form_confirmed TEXT,
                keeper INTEGER NOT NULL DEFAULT 0,
                favorite INTEGER NOT NULL DEFAULT 0,
                watch INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                created_at REAL,
                updated_at REAL,
                revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_observations_ts ON observations(ts DESC);
            CREATE INDEX IF NOT EXISTS idx_observations_run ON observations(run_id);
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
            CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_human_reports_occurred
                ON human_reports(occurred_at DESC);
            CREATE INDEX IF NOT EXISTS idx_human_reports_gap
                ON human_reports(gap_key);
            CREATE INDEX IF NOT EXISTS idx_manual_sessions_started
                ON manual_sessions(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sword_snapshots_ts
                ON sword_snapshots(captured_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sword_rows_snapshot
                ON sword_snapshot_rows(snapshot_id);
            CREATE INDEX IF NOT EXISTS idx_sword_annotations_fp
                ON sword_annotations(sword_catalog_id, kiwame_date);
        """)
        conn.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
            (str(TELEMETRY_SCHEMA_VERSION),),
        )
        # v5 原地补列：人工认领的精确数额（旧记录保持 NULL，不回填不猜测）
        report_cols = {row["name"] for row in conn.execute(
            "PRAGMA table_info(human_reports)").fetchall()}
        if "resource" not in report_cols:
            conn.execute("ALTER TABLE human_reports ADD COLUMN resource TEXT")
        if "claimed_delta" not in report_cols:
            conn.execute("ALTER TABLE human_reports ADD COLUMN claimed_delta REAL")
        if "group_id" not in report_cols:
            conn.execute("ALTER TABLE human_reports ADD COLUMN group_id TEXT")
        # v8 原地补列：自定义工作流的预设名（旧记录保持 NULL，显示回落到「自定义工作流」）
        run_cols = {row["name"] for row in conn.execute(
            "PRAGMA table_info(runs)").fetchall()}
        if "label" not in run_cols:
            conn.execute("ALTER TABLE runs ADD COLUMN label TEXT")
        # v10 原地补列 + 历史回填：刀帐快照的来源与完整度。
        # 老库两类扫描（所持刀剑盘点/刀帐图鉴）共用一表没有 source，不能直接
        # 拿最新快照当本丸候选池（图鉴会冒充具体刀）。
        # 回填依据（确定证据，非猜测）：图鉴扫描器写的行 sword_id 恒为
        # 「album_NNN」格式（代码固定生成）；一览盘点写的行恒为名册目录 id。
        # 全 album 行 → album；零 album 行 → owned_inventory；混排/空快照
        # 无法可靠判断 → unknown。completeness 同理按 owned/missing 对账回填，
        # 证据不足 → unknown。
        snap_cols = {row["name"] for row in conn.execute(
            "PRAGMA table_info(sword_snapshots)").fetchall()}
        if "source" not in snap_cols:
            conn.execute("ALTER TABLE sword_snapshots ADD COLUMN source TEXT")
        if "completeness" not in snap_cols:
            conn.execute(
                "ALTER TABLE sword_snapshots ADD COLUMN completeness TEXT")
        conn.execute("""
            UPDATE sword_snapshots SET source = (
                CASE
                    WHEN (SELECT COUNT(*) FROM sword_snapshot_rows r
                          WHERE r.snapshot_id = sword_snapshots.id) = 0
                        THEN 'unknown'
                    WHEN (SELECT COUNT(*) FROM sword_snapshot_rows r
                          WHERE r.snapshot_id = sword_snapshots.id
                            AND r.sword_id LIKE 'album\\_%' ESCAPE '\\')
                        = (SELECT COUNT(*) FROM sword_snapshot_rows r
                           WHERE r.snapshot_id = sword_snapshots.id)
                        THEN 'album'
                    WHEN (SELECT COUNT(*) FROM sword_snapshot_rows r
                          WHERE r.snapshot_id = sword_snapshots.id
                            AND r.sword_id LIKE 'album\\_%' ESCAPE '\\') = 0
                        THEN 'owned_inventory'
                    ELSE 'unknown'
                END
            ) WHERE source IS NULL
        """)
        conn.execute("""
            UPDATE sword_snapshots SET completeness = (
                CASE
                    WHEN owned IS NOT NULL AND missing IS NOT NULL
                         AND missing = 0 AND sword_count > 0
                        THEN 'complete'
                    WHEN missing IS NOT NULL AND missing > 0
                        THEN 'partial'
                    ELSE 'unknown'
                END
            ) WHERE completeness IS NULL
        """)
        # v11 原地补列：一览盘点的行级形态事实（徽章刀种+花数观测与结论，
        # JSON）。旧记录保持 NULL = 没有形态事实，不回填不猜测——形态结论
        # 只能来自当次扫描的同帧观测，历史快照没看过徽章就是没有。
        row_cols = {row["name"] for row in conn.execute(
            "PRAGMA table_info(sword_snapshot_rows)").fetchall()}
        if "form_fact" not in row_cols:
            conn.execute(
                "ALTER TABLE sword_snapshot_rows ADD COLUMN form_fact TEXT")
        # v13 原地补列：人工确认的等级。旧标注保持 NULL，不回填不猜测。
        ann_cols = {row["name"] for row in conn.execute(
            "PRAGMA table_info(sword_annotations)").fetchall()}
        if "serial_id" not in ann_cols:
            conn.execute("ALTER TABLE sword_annotations ADD COLUMN serial_id INTEGER")
        if "level_confirmed" not in ann_cols:
            conn.execute(
                "ALTER TABLE sword_annotations ADD COLUMN level_confirmed INTEGER")
        # v14 原地补列：常用（favorite）/特别关心（watch）的玩家偏好标记。
        # 与 keeper 一样是玩家偏好契约，旧标注保持 0，不回填不猜测。
        if "favorite" not in ann_cols:
            conn.execute(
                "ALTER TABLE sword_annotations ADD COLUMN favorite "
                "INTEGER NOT NULL DEFAULT 0")
        if "watch" not in ann_cols:
            conn.execute(
                "ALTER TABLE sword_annotations ADD COLUMN watch "
                "INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    def close(self) -> None:
        """Close this thread's connection (primarily for tests and clean shutdowns)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @staticmethod
    def runtime_context() -> tuple[str | None, str | None]:
        return (os.environ.get("MAAMARU_RUN_ID") or None,
                os.environ.get("MAAMARU_SCRIPT") or None)

    def start_run(self, run_id: str, script: str, started_at: float | None = None,
                  label: str | None = None) -> None:
        try:
            self.prune()
            self._conn().execute(
                "INSERT OR REPLACE INTO runs(run_id, script, started_at, ended_at, status, label) "
                "VALUES (?, ?, ?, NULL, 'running', ?)",
                (run_id, script, started_at or time.time(), label),
            )
            self._conn().commit()
        except Exception:
            pass

    def delete_run(self, run_id: str) -> bool:
        """删掉一轮任务记录，连同它名下的事件和识别观测，成绩单不再显示。"""
        conn = self._conn()
        cursor = conn.execute("DELETE FROM runs WHERE run_id = ?", (str(run_id),))
        if cursor.rowcount <= 0:
            conn.commit()
            return False
        conn.execute("DELETE FROM events WHERE run_id = ?", (str(run_id),))
        conn.execute("DELETE FROM observations WHERE run_id = ?", (str(run_id),))
        conn.commit()
        return True

    def finish_run(self, run_id: str, status: str, ended_at: float | None = None) -> None:
        try:
            self._conn().execute(
                "UPDATE runs SET ended_at = ?, status = ? WHERE run_id = ?",
                (ended_at or time.time(), status, run_id),
            )
            self._conn().commit()
        except Exception:
            pass

    def runs_between(self, start_ts: float, end_ts: float) -> list[dict[str, Any]]:
        """返回与 [start_ts, end_ts) 时间窗有重叠的运行记录（只读，供仪表盘时间轴用）。"""
        try:
            rows = self._conn().execute(
                "SELECT run_id, script, started_at, ended_at, status, label FROM runs"
                " WHERE started_at < ? AND (ended_at IS NULL OR ended_at >= ?)"
                " ORDER BY started_at",
                (end_ts, start_ts),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def record_ocr(self, *, kind: str, roi: list[int], tokens: list[dict],
                   expected: str | None = None, match_mode: str | None = None,
                   matched: bool | None = None, error: str | None = None) -> None:
        try:
            run_id, script = self.runtime_context()
            self._conn().execute(
                "INSERT INTO observations(ts, run_id, script, kind, expected, match_mode, "
                "matched, roi, tokens, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), run_id, script, kind, expected, match_mode,
                 None if matched is None else int(matched), _json(roi), _json(tokens), error),
            )
            self._conn().commit()
        except Exception:
            pass

    def record_event(self, event_type: str, payload: dict | None = None) -> int | None:
        """写入一条事件并返回事件 id（供 resource.change 的 source_event_id 关联）。

        写失败返回 None——telemetry 丢了不许拖垮玩法流程。
        """
        try:
            run_id, script = self.runtime_context()
            cursor = self._conn().execute(
                "INSERT INTO events(ts, run_id, script, event_type, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (time.time(), run_id, script, event_type, _json(payload or {})),
            )
            self._conn().commit()
            return cursor.lastrowid
        except Exception:
            return None

    def _prepare_manual_inventory(self, resources: dict,
                                  observed_at: float | None = None) -> tuple[float, dict]:
        if not isinstance(resources, dict):
            raise ValueError("家底格式不对")
        unknown = set(resources) - set(LEDGER_RESOURCES)
        if unknown:
            raise ValueError(f"不认识的资源：{next(iter(sorted(unknown)))}")
        clean = {}
        for name, value in resources.items():
            if value in (None, ""):
                continue
            if isinstance(value, bool):
                raise ValueError(f"{name}数量要填非负整数")
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{name}数量要填非负整数") from None
            if number < 0 or number != float(value):
                raise ValueError(f"{name}数量要填非负整数")
            clean[name] = number
        if not clean:
            raise ValueError("至少填一项家底")
        ts = float(observed_at or time.time())
        if not math.isfinite(ts) or ts <= 0 or ts > time.time() + 300:
            raise ValueError("记录时间不正确")
        payload = {
            "captured_at": datetime.fromtimestamp(ts, _LEDGER_TZ).strftime(
                "%Y-%m-%d %H:%M:%S"),
            "source": "manual_entry",
            "resources": clean,
        }
        return ts, payload

    def add_manual_inventory(self, resources: dict,
                             observed_at: float | None = None) -> dict:
        """记录一份不依赖游戏截图的库存观察；允许只填写部分资源。"""
        ts, payload = self._prepare_manual_inventory(resources, observed_at)
        cursor = self._conn().execute(
            "INSERT INTO events(ts, run_id, script, event_type, payload) "
            "VALUES (?, NULL, 'manual', 'inventory.captured', ?)",
            (ts, _json(payload)),
        )
        self._conn().commit()
        return {"id": cursor.lastrowid, "ts": ts, **payload}

    def manual_inventory(self, limit: int = 200) -> list[dict]:
        """只列玩家手动抄入的家底，不把游戏截图或任务快照混进来。"""
        rows = self._conn().execute(
            "SELECT id, ts, payload FROM events "
            "WHERE run_id IS NULL AND script = 'manual' "
            "AND event_type = 'inventory.captured' "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
        items = []
        for row in rows:
            payload = _loads(row["payload"], {})
            if payload.get("source") not in _MANUAL_INVENTORY_SOURCES:
                continue
            items.append({"id": row["id"], "ts": row["ts"], **payload})
        return items

    def update_manual_inventory(self, event_id: int, resources: dict,
                                observed_at: float | None = None) -> dict:
        ts, payload = self._prepare_manual_inventory(resources, observed_at)
        conn = self._conn()
        row = conn.execute(
            "SELECT run_id, script, event_type, payload FROM events WHERE id = ?",
            (int(event_id),),
        ).fetchone()
        old_payload = _loads(row["payload"], {}) if row else {}
        if (not row or row["run_id"] is not None or row["script"] != "manual"
                or row["event_type"] != "inventory.captured"
                or old_payload.get("source") not in _MANUAL_INVENTORY_SOURCES):
            raise ValueError("找不到这条手动家底记录")
        conn.execute(
            "UPDATE events SET ts = ?, payload = ? WHERE id = ?",
            (ts, _json(payload), int(event_id)),
        )
        conn.commit()
        return {"id": int(event_id), "ts": ts, **payload}

    def delete_manual_inventory(self, event_id: int) -> bool:
        conn = self._conn()
        row = conn.execute(
            "SELECT run_id, script, event_type, payload FROM events WHERE id = ?",
            (int(event_id),),
        ).fetchone()
        payload = _loads(row["payload"], {}) if row else {}
        if (not row or row["run_id"] is not None or row["script"] != "manual"
                or row["event_type"] != "inventory.captured"
                or payload.get("source") not in _MANUAL_INVENTORY_SOURCES):
            return False
        cursor = conn.execute("DELETE FROM events WHERE id = ?", (int(event_id),))
        conn.commit()
        return cursor.rowcount > 0

    def attach_inventory_snapshot(self, run_id: str, snapshot: dict,
                                  captured_ts: float | None = None) -> dict:
        """Attach a later standalone inventory snapshot as a run's closing snapshot.

        Unlike passive telemetry writes, this is a user-requested correction and must
        fail loudly when the association would be ambiguous or misleading.
        """
        conn = self._conn()
        run = conn.execute(
            "SELECT run_id, script, ended_at FROM runs WHERE run_id = ?", (run_id,),
        ).fetchone()
        if not run:
            raise ValueError("找不到这轮任务记录")
        if run["ended_at"] is None:
            raise ValueError("任务还在运行，不能补收工盘点")
        resources = snapshot.get("resources")
        if not isinstance(resources, dict) or not resources:
            raise ValueError("最近的库存快照没有可用资源数据")
        captured_ts = float(captured_ts or time.time())
        if captured_ts < float(run["ended_at"]):
            raise ValueError("最近的库存快照早于这轮收工，请先重新运行“库存快照”")
        snapshot_rows = conn.execute(
            "SELECT e.run_id, e.payload, r.started_at FROM events e "
            "JOIN runs r ON r.run_id = e.run_id "
            "WHERE e.event_type = 'inventory.captured' ORDER BY r.started_at DESC, e.id DESC",
        ).fetchall()
        latest_started_run = next((row["run_id"] for row in snapshot_rows
                                   if _loads(row["payload"], {}).get("phase") == "before"), None)
        if latest_started_run and latest_started_run != run_id:
            raise ValueError("只能给最近一条带开工盘点的挂机记录补录，不能把新库存补到旧轮次")
        existing = conn.execute(
            "SELECT payload FROM events WHERE run_id = ? AND event_type = 'inventory.captured'",
            (run_id,),
        ).fetchall()
        payloads = [_loads(row["payload"], {}) for row in existing]
        if any(payload.get("phase") == "after" for payload in payloads):
            raise ValueError("这轮已经有收工盘点，无需重复补录")
        if not any(payload.get("phase") == "before" for payload in payloads):
            raise ValueError("这轮没有开工盘点，单独补收工数据也无法计算变化")
        payload = dict(snapshot)
        payload.update({"phase": "after", "source": "manual_attach"})
        conn.execute(
            "INSERT INTO events(ts, run_id, script, event_type, payload) VALUES (?, ?, ?, ?, ?)",
            (captured_ts, run_id, run["script"], "inventory.captured", _json(payload)),
        )
        conn.commit()
        return self.run_summary(run_id)

    def _prepare_human_report(self, *, occurred_at: float, activities: list[str],
                              note: str = "", source: str = "proactive",
                              gap_key: str | None = None,
                              resource: str | None = None,
                              claimed_delta: float | None = None,
                              group_id: str | None = None) -> dict:
        activities = [str(value).strip()[:40] for value in activities
                      if str(value).strip()][:20]
        note = str(note or "").strip()[:300]
        source = source if source in {"proactive", "gap"} else "proactive"
        gap_key = str(gap_key or "").strip()[:80] or None
        # 认领数额必须资源+数额成对出现；缺口报备的金额以缺口快照差为准，
        # 不允许再自带数额（两种语义不许混在一条记录里）
        if gap_key and (resource is not None or claimed_delta is not None):
            raise ValueError("缺口报备的金额以库存缺口为准，不用填资源和数额")
        if (resource is None) != (claimed_delta is None):
            raise ValueError("认领需要同时填写资源和数额")
        if resource is not None:
            resource = str(resource).strip()
            if resource not in LEDGER_RESOURCES:
                raise ValueError(f"不认识这种资源：{resource}")
            if (isinstance(claimed_delta, bool)
                    or not isinstance(claimed_delta, (int, float))):
                raise ValueError("认领数额必须是非零整数")
            if (isinstance(claimed_delta, float)
                    and (not math.isfinite(claimed_delta)
                         or not claimed_delta.is_integer())):
                raise ValueError("认领数额必须是非零整数")
            claimed_delta = int(claimed_delta)
            if not claimed_delta or not -(2 ** 63) <= claimed_delta <= 2 ** 63 - 1:
                raise ValueError("认领数额超出可记录范围")
        if not activities and not note and resource is None:
            raise ValueError("请至少选一项，或留一句说明")
        group_id = str(group_id or "").strip()[:80] or None
        occurred_at = float(occurred_at)
        if not math.isfinite(occurred_at) or occurred_at <= 0 or occurred_at > time.time() + 300:
            raise ValueError("记录时间不正确")
        return {
            "occurred_at": occurred_at, "source": source,
            "gap_key": gap_key, "activities": activities, "note": note,
            "resource": resource, "claimed_delta": claimed_delta,
            "group_id": group_id,
        }

    def add_human_report(self, *, occurred_at: float, activities: list[str],
                         note: str = "", source: str = "proactive",
                         gap_key: str | None = None,
                         resource: str | None = None,
                         claimed_delta: float | None = None,
                         group_id: str | None = None) -> dict:
        clean = self._prepare_human_report(
            occurred_at=occurred_at, activities=activities, note=note,
            source=source, gap_key=gap_key, resource=resource,
            claimed_delta=claimed_delta, group_id=group_id)
        created_at = time.time()
        cursor = self._conn().execute(
            "INSERT INTO human_reports(created_at, occurred_at, source, gap_key, "
            "activities, note, resource, claimed_delta, group_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (created_at, clean["occurred_at"], clean["source"], clean["gap_key"],
             _json(clean["activities"]), clean["note"], clean["resource"],
             clean["claimed_delta"], clean["group_id"]),
        )
        self._conn().commit()
        return {"id": cursor.lastrowid, "created_at": created_at,
                **clean}

    def add_human_report_group(self, *, occurred_at: float, activities: list[str],
                               entries: dict[str, float], note: str = "") -> list[dict]:
        clean = {str(resource): delta for resource, delta in entries.items()
                 if delta is not None}
        if not clean:
            raise ValueError("请至少填写一种资源的收支")
        group_id = uuid.uuid4().hex
        prepared = [self._prepare_human_report(
            occurred_at=occurred_at, activities=activities, note=note,
            source="proactive", resource=resource, claimed_delta=delta,
            group_id=group_id) for resource, delta in clean.items()]
        conn = self._conn()
        created_at = time.time()
        items = []
        try:
            for item in prepared:
                cursor = conn.execute(
                    "INSERT INTO human_reports(created_at, occurred_at, source, gap_key, "
                    "activities, note, resource, claimed_delta, group_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (created_at, item["occurred_at"], item["source"], item["gap_key"],
                     _json(item["activities"]), item["note"], item["resource"],
                     item["claimed_delta"], item["group_id"]),
                )
                items.append({"id": cursor.lastrowid, "created_at": created_at, **item})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return items

    def update_human_report(self, report_id: int, *, occurred_at: float,
                            activities: list[str], note: str, resource: str,
                            claimed_delta: float) -> dict:
        conn = self._conn()
        row = conn.execute(
            "SELECT id, created_at, source, gap_key, group_id FROM human_reports WHERE id = ?",
            (int(report_id),),
        ).fetchone()
        if not row or row["source"] != "proactive" or row["gap_key"] or row["group_id"]:
            raise ValueError("找不到这笔可修改的手动收支")
        clean = self._prepare_human_report(
            occurred_at=occurred_at, activities=activities, note=note,
            source="proactive", resource=resource, claimed_delta=claimed_delta)
        conn.execute(
            "UPDATE human_reports SET occurred_at = ?, activities = ?, note = ?, "
            "resource = ?, claimed_delta = ? WHERE id = ?",
            (clean["occurred_at"], _json(clean["activities"]), clean["note"],
             clean["resource"], clean["claimed_delta"], int(report_id)),
        )
        conn.commit()
        return {"id": row["id"], "created_at": row["created_at"], **clean}

    def update_human_report_group(self, group_id: str, *, occurred_at: float,
                                  activities: list[str], entries: dict[str, float],
                                  note: str = "") -> list[dict]:
        group_id = str(group_id or "").strip()
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, created_at, source, gap_key FROM human_reports WHERE group_id = ?",
            (group_id,),
        ).fetchall()
        if not rows or any(row["source"] != "proactive" or row["gap_key"] for row in rows):
            raise ValueError("找不到这组可修改的手动收支")
        clean_entries = {str(resource): delta for resource, delta in entries.items()
                         if delta is not None}
        if not clean_entries:
            raise ValueError("请至少填写一种资源的收支")
        prepared = [self._prepare_human_report(
            occurred_at=occurred_at, activities=activities, note=note,
            source="proactive", resource=resource, claimed_delta=delta,
            group_id=group_id) for resource, delta in clean_entries.items()]
        created_at = min(float(row["created_at"]) for row in rows)
        items = []
        try:
            conn.execute("DELETE FROM human_reports WHERE group_id = ?", (group_id,))
            for item in prepared:
                cursor = conn.execute(
                    "INSERT INTO human_reports(created_at, occurred_at, source, gap_key, "
                    "activities, note, resource, claimed_delta, group_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (created_at, item["occurred_at"], item["source"], item["gap_key"],
                     _json(item["activities"]), item["note"], item["resource"],
                     item["claimed_delta"], item["group_id"]),
                )
                items.append({"id": cursor.lastrowid, "created_at": created_at, **item})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return items

    def human_reports(self, limit: int = 200) -> list[dict]:
        rows = self._conn().execute(
            "SELECT id, created_at, occurred_at, source, gap_key, activities, note, "
            "resource, claimed_delta, group_id "
            "FROM human_reports ORDER BY occurred_at DESC, id DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
        return [{**dict(row), "activities": _loads(row["activities"], [])}
                for row in rows]

    def delete_human_report(self, report_id: int) -> bool:
        cursor = self._conn().execute(
            "DELETE FROM human_reports WHERE id = ?", (int(report_id),),
        )
        self._conn().commit()
        return cursor.rowcount > 0

    def delete_human_report_group(self, group_id: str) -> bool:
        cursor = self._conn().execute(
            "DELETE FROM human_reports WHERE group_id = ?", (str(group_id),),
        )
        self._conn().commit()
        return cursor.rowcount > 0

    def _prepare_manual_session(self, *, script: str, started_at: float,
                                ended_at: float, loops: int,
                                note: str = "") -> dict:
        labels = {
            "osaka": "大阪城", "raid": "联队战", "edocastle": "江户城",
            "sortie": "合战场", "yosari": "异去", "pumpkin": "季节活动",
        }
        script = str(script or "").strip()
        if script not in labels:
            raise ValueError("请选择一种支持的玩法")
        if isinstance(loops, bool) or not isinstance(loops, int) or not 1 <= loops <= 100000:
            raise ValueError("圈数必须是 1 到 100000 的整数")
        started_at, ended_at = float(started_at), float(ended_at)
        if not math.isfinite(started_at) or not math.isfinite(ended_at):
            raise ValueError("开始和结束时间不正确")
        if ended_at <= started_at:
            raise ValueError("结束时间必须晚于开始时间")
        if ended_at - started_at > 31 * 86400:
            raise ValueError("一段活动最多记录 31 天")
        if ended_at > time.time() + 300:
            raise ValueError("结束时间不能在未来")
        note = str(note or "").strip()[:200]
        return {
            "script": script, "activity": labels[script],
            "started_at": started_at, "ended_at": ended_at, "loops": loops,
            "duration_seconds": round(ended_at - started_at, 1),
            "average_loop_seconds": round((ended_at - started_at) / loops, 1),
            "note": note, "source": "manual",
        }

    def add_manual_session(self, *, script: str, started_at: float,
                           ended_at: float, loops: int,
                           note: str = "") -> dict:
        """Record player-run activity without inserting a machine ``run``.

        Manual sessions intentionally live in their own table: scorecard totals and
        automation evidence must never silently absorb player-entered work.
        """
        clean = self._prepare_manual_session(
            script=script, started_at=started_at, ended_at=ended_at,
            loops=loops, note=note)
        created_at = time.time()
        cursor = self._conn().execute(
            "INSERT INTO manual_sessions(created_at, script, started_at, ended_at, loops, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (created_at, clean["script"], clean["started_at"], clean["ended_at"],
             clean["loops"], clean["note"]),
        )
        self._conn().commit()
        return {"id": cursor.lastrowid, "created_at": created_at, **clean}

    def update_manual_session(self, session_id: int, *, script: str,
                              started_at: float, ended_at: float, loops: int,
                              note: str = "") -> dict:
        clean = self._prepare_manual_session(
            script=script, started_at=started_at, ended_at=ended_at,
            loops=loops, note=note)
        conn = self._conn()
        row = conn.execute(
            "SELECT id, created_at FROM manual_sessions WHERE id = ?",
            (int(session_id),),
        ).fetchone()
        if not row:
            raise ValueError("找不到这条手动活动记录")
        conn.execute(
            "UPDATE manual_sessions SET script = ?, started_at = ?, ended_at = ?, "
            "loops = ?, note = ? WHERE id = ?",
            (clean["script"], clean["started_at"], clean["ended_at"],
             clean["loops"], clean["note"], int(session_id)),
        )
        conn.commit()
        return {"id": row["id"], "created_at": row["created_at"], **clean}

    def manual_sessions(self, limit: int = 200, *, from_ts: float | None = None,
                        to_ts: float | None = None) -> list[dict]:
        clauses, args = [], []
        if from_ts is not None:
            clauses.append("started_at >= ?")
            args.append(float(from_ts))
        if to_ts is not None:
            clauses.append("started_at < ?")
            args.append(float(to_ts))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        args.append(max(1, min(int(limit), 1000)))
        rows = self._conn().execute(
            "SELECT id, created_at, script, started_at, ended_at, loops, note "
            f"FROM manual_sessions{where} ORDER BY started_at DESC, id DESC LIMIT ?",
            args,
        ).fetchall()
        labels = {
            "osaka": "大阪城", "raid": "联队战", "edocastle": "江户城",
            "sortie": "合战场", "yosari": "异去", "pumpkin": "季节活动",
        }
        return [{
            **dict(row), "activity": labels.get(row["script"], row["script"]),
            "duration_seconds": round(row["ended_at"] - row["started_at"], 1),
            "average_loop_seconds": round(
                (row["ended_at"] - row["started_at"]) / row["loops"], 1),
            "source": "manual",
        } for row in rows]

    def delete_manual_session(self, session_id: int) -> bool:
        cursor = self._conn().execute(
            "DELETE FROM manual_sessions WHERE id = ?", (int(session_id),),
        )
        self._conn().commit()
        return cursor.rowcount > 0

    # ---------- 刀帐快照（刀剑男士一览逐页扫描的落库） ----------

    def save_sword_snapshot(self, rows: list[dict], *, owned: int | None = None,
                            capacity: int | None = None,
                            captured_at: float | None = None,
                            missing: int | None = None,
                            source: str | None = None,
                            completeness: str | None = None) -> int:
        """写一份刀帐快照（头 + 每刀一行），返回快照 id。

        rows 的每项：sword_id/name_zh 必填，其余字段缺省 None；
        stats 是九属性 dict（键=属性名），按 JSON 存。

        source：owned_inventory（所持刀剑一览盘点）/ album（刀帐图鉴）/
        unknown（调用方没说清，不允许冒充盘点）。
        completeness：complete（对账平）/ partial（有缺口）/ unknown；
        缺省按 owned/missing/行数推断，证据不足落 unknown。
        """
        ts = float(captured_at or time.time())
        if source not in ("owned_inventory", "album"):
            source = "unknown"
        if completeness not in ("complete", "partial"):
            if owned and missing == 0 and rows:
                completeness = "complete"
            elif missing:
                completeness = "partial"
            else:
                completeness = "unknown"
        conn = self._conn()
        cursor = conn.execute(
            "INSERT INTO sword_snapshots(captured_at, owned, capacity, sword_count, "
            "missing, source, completeness) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, owned, capacity, len(rows), missing, source, completeness),
        )
        snapshot_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO sword_snapshot_rows(snapshot_id, sword_id, name_zh, level, "
            "tou_level, survival, survival_max, fatigue, fatigue_max, stats, "
            "kiwame_date, locked, page_no, form_fact) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(snapshot_id, row["sword_id"], row["name_zh"],
              row.get("level"), row.get("tou_level"),
              row.get("survival"), row.get("survival_max"),
              row.get("fatigue"), row.get("fatigue_max"),
              _json(row.get("stats") or {}),
              row.get("kiwame_date"), row.get("locked"), row.get("page_no"),
              _json(row["form_fact"]) if row.get("form_fact") else None)
             for row in rows],
        )
        conn.commit()
        return int(snapshot_id)

    def recent_sword_snapshots(self, limit: int = 20) -> list[dict]:
        rows = self._conn().execute(
            "SELECT id, captured_at, owned, capacity, sword_count, missing, "
            "source, completeness "
            "FROM sword_snapshots ORDER BY captured_at DESC, id DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def latest_sword_snapshot(self, *, source: str | None = None,
                              completeness: str | None = None) -> dict | None:
        """按来源/完整度直接查最新匹配快照（SQL 无窗口）。

        recent_sword_snapshots 有行数窗口，较新的 album/partial/unknown
        攒多了会把旧的可信完整盘点挤出窗口——候选池晋升和盘点展示
        必须走这个无窗口入口，无效快照再多也盖不住可信档案。
        """
        clauses, args = [], []
        if source:
            clauses.append("source = ?")
            args.append(source)
        if completeness:
            clauses.append("completeness = ?")
            args.append(completeness)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        row = self._conn().execute(
            "SELECT id, captured_at, owned, capacity, sword_count, missing, "
            "source, completeness FROM sword_snapshots" + where +
            " ORDER BY captured_at DESC, id DESC LIMIT 1", args,
        ).fetchone()
        return dict(row) if row else None

    def previously_owned_sword(self, catalog_id: str, acquired_date: str,
                               before: float) -> bool:
        """旧完整所持盘点证明曾持有；图鉴、局部读取不能作为离开依据。"""
        return self._conn().execute(
            "SELECT 1 FROM sword_snapshot_rows r JOIN sword_snapshots s "
            "ON s.id = r.snapshot_id WHERE r.sword_id = ? AND r.kiwame_date = ? "
            "AND s.source = 'owned_inventory' AND s.completeness = 'complete' "
            "AND s.captured_at < ? LIMIT 1",
            (catalog_id, acquired_date, before),
        ).fetchone() is not None

    def sword_snapshot_detail(self, snapshot_id: int) -> dict | None:
        head = self._conn().execute(
            "SELECT id, captured_at, owned, capacity, sword_count, missing, "
            "source, completeness "
            "FROM sword_snapshots WHERE id = ?", (int(snapshot_id),),
        ).fetchone()
        if not head:
            return None
        rows = self._conn().execute(
            "SELECT id AS row_id, sword_id, name_zh, level, tou_level, "
            "survival, survival_max, fatigue, fatigue_max, stats, kiwame_date, "
            "locked, page_no, form_fact "
            "FROM sword_snapshot_rows WHERE snapshot_id = ? ORDER BY id",
            (int(snapshot_id),),
        ).fetchall()
        out = dict(head)
        out["swords"] = [{**dict(row), "stats": _loads(row["stats"], {}),
                          "form_fact": _loads(row["form_fact"], None)}
                         for row in rows]
        return out

    # ---------- 刀帐人工标注（sword_annotations，v12 建表、v13/v14 补列） ----------

    def save_sword_annotation(self, sword_catalog_id, kiwame_date,
                              level_at_mark=None, form_confirmed=None,
                              keeper=None, note=None, level_confirmed=None,
                              favorite=None, watch=None, serial_id=None) -> dict:
        """保存一条人工标注；同指纹（目录 id + 显现日期）已存在有效标注时更新。

        更新只覆盖传入的非 None 字段（updated_at 随刷新），软删的指纹视为
        不存在、直接新建。指纹是标注挂到「具体某一振」的唯一依据：
        显现日期每振终身不变，同名多振靠它区分。
        """
        sword_catalog_id = str(sword_catalog_id or "").strip()
        kiwame_date = str(kiwame_date or "").strip()
        if not sword_catalog_id:
            raise ValueError("刀剑目录 id 不能为空")
        if not kiwame_date and serial_id is None:
            raise ValueError("显现日期不能为空")
        # 有独立编号时不依赖日期区分同名多振；未同步日期保持空缺。
        if form_confirmed not in (None, "kiwame", "normal"):
            raise ValueError("形态确认只能是 kiwame 或 normal")
        if level_at_mark is not None:
            if (isinstance(level_at_mark, bool)
                    or not isinstance(level_at_mark, (int, float))
                    or int(level_at_mark) != level_at_mark):
                raise ValueError("标记等级要填整数")
            level_at_mark = int(level_at_mark)
        if level_confirmed is not None:
            if (isinstance(level_confirmed, bool)
                    or not isinstance(level_confirmed, (int, float))
                    or int(level_confirmed) != level_confirmed
                    or not 1 <= int(level_confirmed) <= 99):
                raise ValueError("确认等级必须是 1 到 99 的整数")
            level_confirmed = int(level_confirmed)
        if serial_id is not None:
            if isinstance(serial_id, bool) or not isinstance(serial_id, int) or serial_id <= 0:
                raise ValueError("刀剑独立编号必须是正整数")
        keeper_value = None if keeper is None else int(bool(keeper))
        favorite_value = None if favorite is None else int(bool(favorite))
        watch_value = None if watch is None else int(bool(watch))
        note = str(note).strip()[:300] if note is not None else None
        conn = self._conn()
        now = time.time()
        if serial_id is not None:
            row = conn.execute("SELECT id FROM sword_annotations WHERE serial_id = ? AND revoked = 0", (serial_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM sword_annotations WHERE sword_catalog_id = ? AND kiwame_date = ? "
                "AND serial_id IS NULL AND revoked = 0", (sword_catalog_id, kiwame_date),
            ).fetchone()
        if row:
            sets, args = [], []
            if level_at_mark is not None:
                sets.append("level_at_mark = ?")
                args.append(level_at_mark)
            if level_confirmed is not None:
                sets.append("level_confirmed = ?")
                args.append(level_confirmed)
            if form_confirmed is not None:
                sets.append("form_confirmed = ?")
                args.append(form_confirmed)
            if keeper_value is not None:
                sets.append("keeper = ?")
                args.append(keeper_value)
            if favorite_value is not None:
                sets.append("favorite = ?")
                args.append(favorite_value)
            if watch_value is not None:
                sets.append("watch = ?")
                args.append(watch_value)
            if note is not None:
                sets.append("note = ?")
                args.append(note)
            sets.append("updated_at = ?")
            args.append(now)
            args.append(row["id"])
            conn.execute(
                f"UPDATE sword_annotations SET {', '.join(sets)} WHERE id = ?",
                args)
            conn.commit()
            return self._sword_annotation_dict(row["id"])
        cursor = conn.execute(
            "INSERT INTO sword_annotations(serial_id, sword_catalog_id, kiwame_date, "
            "level_at_mark, level_confirmed, form_confirmed, keeper, "
            "favorite, watch, note, created_at, updated_at, revoked) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (serial_id, sword_catalog_id, kiwame_date, level_at_mark, level_confirmed,
             form_confirmed, keeper_value or 0, favorite_value or 0,
             watch_value or 0, note, now, now),
        )
        conn.commit()
        return self._sword_annotation_dict(cursor.lastrowid)

    def bind_sword_annotation_serials(self, entries: list[dict]) -> None:
        """只把唯一旧指纹绑定到唯一游戏编号；绑定之后不再回退日期匹配。"""
        from collections import Counter
        counts = Counter((e.get("sword_catalog_id"), e.get("kiwame_date")) for e in entries)
        annotations = self.sword_annotations()
        ann_counts = Counter((a["sword_catalog_id"], a["kiwame_date"]) for a in annotations if a["serial_id"] is None)
        occupied = {a["serial_id"] for a in annotations if a["serial_id"] is not None}
        unique = {(e.get("sword_catalog_id"), e.get("kiwame_date")): e.get("serial_id") for e in entries if e.get("serial_id")}
        for ann in annotations:
            key = (ann["sword_catalog_id"], ann["kiwame_date"])
            serial = unique.get(key)
            if ann["serial_id"] is None and counts[key] == 1 and ann_counts[key] == 1 and serial and serial not in occupied:
                self._conn().execute("UPDATE sword_annotations SET serial_id = ? WHERE id = ? AND serial_id IS NULL", (serial, ann["id"]))
                occupied.add(serial)
        self._conn().commit()

    def _sword_annotation_dict(self, annotation_id: int) -> dict:
        row = self._conn().execute(
            "SELECT id, serial_id, sword_catalog_id, kiwame_date, level_at_mark, "
            "level_confirmed, form_confirmed, keeper, favorite, watch, note, "
            "created_at, updated_at, revoked "
            "FROM sword_annotations WHERE id = ?", (int(annotation_id),),
        ).fetchone()
        if not row:
            raise ValueError("找不到这条人工标注")
        return {**dict(row), "keeper": bool(row["keeper"]),
                "favorite": bool(row["favorite"]),
                "watch": bool(row["watch"]),
                "revoked": bool(row["revoked"])}

    def sword_annotations(self, include_revoked: bool = False) -> list[dict]:
        where = "" if include_revoked else " WHERE revoked = 0"
        rows = self._conn().execute(
            "SELECT id, serial_id, sword_catalog_id, kiwame_date, level_at_mark, "
            "level_confirmed, form_confirmed, keeper, favorite, watch, note, "
            "created_at, updated_at, revoked "
            f"FROM sword_annotations{where} ORDER BY updated_at DESC, id DESC",
        ).fetchall()
        return [{**dict(row), "keeper": bool(row["keeper"]),
                 "favorite": bool(row["favorite"]),
                 "watch": bool(row["watch"]),
                 "revoked": bool(row["revoked"])} for row in rows]

    def revoke_sword_annotation(self, annotation_id) -> dict:
        """软删一条标注（revoked=1），历史保留不丢；不存在或已撤销抛
        ValueError——重复撤销说明界面状态和库已经不一致，如实报错。"""
        conn = self._conn()
        row = conn.execute(
            "SELECT id FROM sword_annotations WHERE id = ? AND revoked = 0",
            (int(annotation_id),),
        ).fetchone()
        if not row:
            raise ValueError("找不到这条人工标注")
        conn.execute(
            "UPDATE sword_annotations SET revoked = 1, updated_at = ? "
            "WHERE id = ?", (time.time(), row["id"]))
        conn.commit()
        return self._sword_annotation_dict(row["id"])

    def inventory_gaps(self, limit: int = 50) -> list[dict]:
        """Return resource changes between a prior closing snapshot and next run start."""
        # 只看最近 500 条快照（返回上限 200 个缺口，500 条配对绰绰有余），
        # 快照事件永久保留，全表扫描会随年份线性变慢
        rows = self._conn().execute(
            "SELECT id, ts, run_id, payload FROM events "
            "WHERE event_type = 'inventory.captured' "
            "ORDER BY ts DESC, id DESC LIMIT 500",
        ).fetchall()
        rows = list(reversed(rows))
        snapshots = [{**dict(row), "payload": _loads(row["payload"], {})}
                     for row in rows]
        reported = {row["gap_key"] for row in self._conn().execute(
            "SELECT gap_key FROM human_reports WHERE gap_key IS NOT NULL",
        ).fetchall()}
        gaps = []
        for previous, current in zip(snapshots, snapshots[1:]):
            previous_phase = previous["payload"].get("phase")
            current_phase = current["payload"].get("phase")
            if current_phase != "before" or previous_phase not in {"after", None}:
                continue
            if previous["run_id"] == current["run_id"]:
                continue
            left = previous["payload"].get("resources") or {}
            right = current["payload"].get("resources") or {}
            delta = {name: right[name] - left[name] for name in left.keys() & right.keys()
                     if isinstance(left.get(name), (int, float))
                     and isinstance(right.get(name), (int, float))
                     and right[name] != left[name]}
            if not delta:
                continue
            gap_key = f'{previous["id"]}:{current["id"]}'
            gaps.append({"gap_key": gap_key, "started_at": previous["ts"],
                         "ended_at": current["ts"], "resource_delta": delta,
                         "reported": gap_key in reported})
        return list(reversed(gaps[-max(1, min(int(limit), 200)):]))

    def client_item_inventory(self, to_ts: float | None = None) -> dict:
        """Latest actual client read per named item; never treat absence as zero."""
        from .youzu_log import ITEM_NAMES
        remaining = set(ITEM_NAMES.values())
        items = {}
        resources = {}
        for row in self._conn().execute(
                "SELECT ts, payload FROM events WHERE script = 'youzu_log' "
                "AND event_type = 'inventory.captured' AND ts <= ? ORDER BY ts DESC, id DESC",
                (time.time() if to_ts is None else float(to_ts),)):
            reading = _resource_reading(_loads(row["payload"], {}).get("resources"))
            for name in LEDGER_RESOURCES:
                value = reading.get(name)
                if name not in resources and isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                    resources[name] = {"count": int(value), "observed_at": row["ts"]}
            for name in remaining.intersection(reading):
                value = reading[name]
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                    items[name] = {"count": int(value), "observed_at": row["ts"]}
            remaining.difference_update(items)
            if not remaining and len(resources) == len(LEDGER_RESOURCES):
                break
        box_values = {"小判箱·小": 200, "小判箱·中": 400, "小判箱·大": 700}
        boxes = {name: {**items[name], "value_each": value} for name, value in box_values.items() if name in items}
        complete = len(boxes) == len(box_values)
        asset_row = self._conn().execute(
            "SELECT ts, payload FROM events WHERE script = 'youzu_log' AND event_type = 'game_assets.captured' AND ts <= ? ORDER BY ts DESC, id DESC LIMIT 1",
            (time.time() if to_ts is None else float(to_ts),)).fetchone()
        from .client_equipment import name_client_assets
        assets = name_client_assets({**_loads(asset_row["payload"], {}), "observed_at": asset_row["ts"]}) if asset_row else None
        missing = set(LEDGER_RESOURCES) - resources.keys()
        if missing:
            for row in self._conn().execute(
                    "SELECT ts, payload FROM events WHERE event_type='inventory.captured' AND script NOT IN ('youzu_log','manual') AND ts <= ? ORDER BY ts DESC, id DESC",
                    (time.time() if to_ts is None else float(to_ts),)):
                reading = _resource_reading(_loads(row["payload"], {}).get("resources"))
                for name in list(missing):
                    value = reading.get(name)
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                        resources[name] = {"count": int(value), "observed_at": row["ts"], "source": "screen"}
                        missing.remove(name)
                if not missing:
                    break
        return {"assets": assets, "items": items, "resources": resources, "koban_boxes": boxes,
                "koban_reserve": sum(v["count"] * v["value_each"] for v in boxes.values()) if complete else None,
                "koban_reserve_known": sum(v["count"] * v["value_each"] for v in boxes.values())}

    def resource_ledger(self, from_ts: float, to_ts: float) -> dict:
        """聚合时间窗口内八种资源的总账：观察链、已确认归因、缺口。

        SQL 层按 ts 过滤，不走 recent_events 的 1000 条上限。
        total_delta 保留符号、恒等于 attributed + unattributed（负残差不截断）；
        观察不足形成不了 opening/closing 时 total_delta 为 None，
        但 confirmed 明细仍保留在 attributions 里。
        """
        from_ts, to_ts = float(from_ts), float(to_ts)
        if to_ts < from_ts:
            from_ts, to_ts = to_ts, from_ts
        conn = self._conn()
        event_types = ("inventory.captured", "inventory.peek", "osaka.koban_session",
                       "repair.session_completed", "resource.change",
                       "ticket.refilled")
        marks = ",".join("?" * len(event_types))
        rows = conn.execute(
            "SELECT id, ts, run_id, script, event_type, payload FROM events "
            f"WHERE ts >= ? AND ts <= ? AND event_type IN ({marks}) ORDER BY ts, id",
            (from_ts, to_ts, *event_types)).fetchall()
        # Sparse client responses must retain a separate baseline per resource.
        baseline_by_id = {}
        for event_type in _LEDGER_OBS_TYPES:
            names = (_LEDGER_PEEK_RESOURCES if event_type == "inventory.peek"
                     else ("小判",) if event_type == "osaka.koban_session"
                     else LEDGER_RESOURCES)
            for name in names:
                path = ('$.resources."' + name + '"' if event_type == "inventory.captured"
                        else '$."' + name + '"' if event_type == "inventory.peek"
                        else '$.after')
                row = conn.execute(
                    "SELECT id, ts, run_id, script, event_type, payload FROM events "
                    "WHERE ts < ? AND event_type = ? "
                    "AND (json_type(payload, ?) IN ('integer', 'real') OR json_type(payload, ?) IN ('integer', 'real')) "
                    "ORDER BY ts DESC, id DESC LIMIT 1",
                    (from_ts, event_type, path, '$.resources."加速符·极"' if name == '加速符' and event_type == 'inventory.captured' else path)).fetchone()
                if row:
                    baseline_by_id[row["id"]] = row
        for name in LEDGER_RESOURCES:
            row = conn.execute(
                "SELECT id, ts, run_id, script, event_type, payload FROM events "
                "WHERE ts < ? AND event_type = 'resource.change' "
                "AND json_extract(payload, '$.resource') IN (?, ?) "
                "AND json_type(payload, '$.before') IN ('integer', 'real') "
                "AND json_type(payload, '$.after') IN ('integer', 'real') "
                "ORDER BY ts DESC, id DESC LIMIT 1", (from_ts, name, '加速符·极' if name == '加速符' else name)).fetchone()
            if row:
                baseline_by_id[row["id"]] = row
        baseline_rows = list(baseline_by_id.values())
        reports = conn.execute(
            "SELECT id, occurred_at, gap_key, resource, claimed_delta FROM human_reports "
            "WHERE occurred_at <= ? ORDER BY occurred_at, id", (to_ts,)).fetchall()

        # ── 观察流：优先级 直读 before/after(3) > captured(2) > peek(1) ──
        raw: list[dict] = []
        for row in [*baseline_rows, *rows]:
            payload = _loads(row["payload"], {})
            event_type = row["event_type"]
            if event_type == "osaka.koban_session":
                # sub 区分同事件内 before/after 的先后顺序（事件只有一个 ts）
                for sub, key in ((0, "before"), (1, "after")):
                    value = payload.get(key)
                    if isinstance(value, (int, float)):
                        raw.append({"ts": row["ts"], "sub": sub, "resource": "小判",
                                    "value": value, "priority": 3, "source": event_type,
                                    "event_id": row["id"], "evidence": [row["id"]]})
            elif event_type == "inventory.captured":
                for name, value in _resource_reading(payload.get("resources")).items():
                    if isinstance(value, (int, float)):
                        raw.append({"ts": row["ts"], "sub": 0, "resource": name,
                                    "value": value, "priority": 2, "source": event_type,
                                    "event_id": row["id"], "evidence": [row["id"]]})
            elif event_type == "inventory.peek":
                for name in _LEDGER_PEEK_RESOURCES:
                    value = payload.get(name)
                    if isinstance(value, (int, float)):
                        raw.append({"ts": row["ts"], "sub": 0, "resource": name,
                                    "value": value, "priority": 1, "source": event_type,
                                    "event_id": row["id"], "evidence": [row["id"]]})
            elif event_type == "resource.change":
                # 带 before/after 的 resource.change（如异去补充提灯）是直读观察，
                # 否则跨日分桶的 opening 会跳过这条消费链，把支出漏成次日未归因
                name = payload.get("resource")
                if name == "加速符·极":
                    name = "加速符"
                if name:
                    for sub, key in ((0, "before"), (1, "after")):
                        value = payload.get(key)
                        if isinstance(value, (int, float)):
                            raw.append({"ts": row["ts"], "sub": sub, "resource": name,
                                        "value": value, "priority": 3,
                                        "source": event_type,
                                        "event_id": row["id"], "evidence": [row["id"]]})

        # ── 归因：resource.change 双写去重（source_event_id 指向旧事件时跳过旧的那份）──
        shadowed = set()
        # 加速符去重：同 run 已有 repair.confirm_screen 的逐笔记账时，
        # repair.session_completed 的 speedups 汇总让位（逐笔粒度更细更准）；
        # 没有逐笔记录的老数据照常靠 session_completed 归因
        per_repair_runs: set = set()
        per_repair_any = False
        reward_change_groups: dict[tuple, list[int]] = {}
        for row in rows:
            if row["event_type"] == "resource.change":
                rc_payload = _loads(row["payload"], {})
                source_event_id = rc_payload.get("source_event_id")
                if isinstance(source_event_id, (int, float)):
                    shadowed.add(int(source_event_id))
                if (rc_payload.get("source") == "repair.confirm_screen"
                        and rc_payload.get("resource") == "加速符"):
                    per_repair_any = True
                    if row["run_id"]:
                        per_repair_runs.add(row["run_id"])
                if (rc_payload.get("source") == "task_rewards.reward_popup"
                        and isinstance(source_event_id, (int, float))
                        and rc_payload.get("resource")):
                    key = (int(source_event_id), str(rc_payload["resource"]))
                    reward_change_groups.setdefault(key, []).append(row["id"])
        # 2026-08-29 实测：加速符会撞上委托符模板，导致同一奖励弹窗
        # 同一资源出现两条。弹窗会合并同类奖励，因此这种重复代表类别冲突；
        # 两条都不归因，保留原始事件等待同源模板补齐，不能挑一条猜。
        ambiguous_reward_changes = {
            event_id for event_ids in reward_change_groups.values()
            if len(event_ids) > 1 for event_id in event_ids
        }
        attributions: list[dict] = []
        unresolved_changes: list[dict] = []
        for row in rows:
            payload = _loads(row["payload"], {})
            event_type = row["event_type"]
            item = None
            if event_type == "osaka.koban_session" and row["id"] not in shadowed:
                delta = payload.get("delta")
                if isinstance(delta, (int, float)) and delta:
                    item = {"resource": "小判", "delta": delta, "source": event_type,
                            "label": f"挖地小判 {int(delta):+d}", "confidence": "confirmed"}
            elif event_type == "repair.session_completed" and row["id"] not in shadowed:
                # run_id 优先配对；run_id 缺失时按窗内是否有逐笔记录兜底
                has_per_repair = (row["run_id"] in per_repair_runs
                                  if row["run_id"] else per_repair_any)
                speedups = payload.get("speedups")
                if (not has_per_repair
                        and isinstance(speedups, (int, float)) and speedups):
                    item = {"resource": "加速符", "delta": -int(speedups),
                            "source": event_type,
                            "label": f"手入加速符 {-int(speedups):+d}",
                            "confidence": "confirmed"}
            elif (event_type == "resource.change"
                  and row["id"] not in ambiguous_reward_changes):
                delta = payload.get("delta")
                resource = str(payload.get("resource") or "")
                if resource == "加速符·极":
                    resource = "加速符"
                if resource and isinstance(delta, (int, float)) and delta:
                    item = {"resource": resource, "delta": delta,
                            "source": str(payload.get("source") or event_type),
                            "label": str(payload.get("note") or f"{resource} {int(delta):+d}"),
                            "confidence": str(payload.get("attribution") or "confirmed")}
                    if row["script"] == "youzu_log":
                        from .youzu_log import translate_ledger_source
                        original = item["source"]
                        item["source"], item["label"] = translate_ledger_source(original, item["label"])
                        item["raw_source"] = original
                        if item["source"].startswith("unknown."):
                            unresolved_changes.append({**item, "id": f"u{row['id']}",
                                "ts": row["ts"], "script": row["script"],
                                "run_id": row["run_id"], "event_id": row["id"], "confidence": "inferred"})
                            item = None
            elif event_type == "ticket.refilled" and row["id"] not in shadowed:
                # v0.4.1 的江户城已经稳定记录“补过一张”，但没把固定的
                # 300 小判写进 payload。兼容这些旧事实，让历史统计即时补账；
                # 新事件优先使用自身携带的 resource/delta，不猜其他活动票价。
                resource = str(payload.get("resource") or "")
                if resource == "加速符·极":
                    resource = "加速符"
                delta = payload.get("delta")
                if (not resource and row["script"] == "edocastle"
                        and payload.get("source") == "江户城"):
                    resource, delta = "小判", -300
                if resource and isinstance(delta, (int, float)) and delta:
                    item = {"resource": resource, "delta": delta,
                            "source": "ticket.refilled",
                            "label": f"{payload.get('source') or '活动'}补手形 {int(delta):+d}",
                            "confidence": "confirmed"}
            if item:
                item.update({"id": f"a{len(attributions) + 1}", "ts": row["ts"],
                             "script": row["script"], "run_id": row["run_id"],
                             "event_id": row["id"]})
                attributions.append(item)

        # 游戏原文与脚本若记录了同一笔变化，保留原文并关联执行记录。
        # 只合并唯一匹配：精确余额一致，或同来源且五秒内相同金额。
        shadowed_attrs = set()
        payload_by_id = {r["id"]: _loads(r["payload"], {}) for r in rows}
        scripts_by_amount = {}
        for a in attributions:
            if a["script"] != "youzu_log":
                scripts_by_amount.setdefault((a["resource"], a["delta"]), []).append(a)
        matches = []
        for game in [a for a in attributions if a["script"] == "youzu_log"]:
            candidates = []
            game_payload = payload_by_id.get(game["event_id"], {})
            for script in scripts_by_amount.get((game["resource"], game["delta"]), []):
                if script["script"] == "youzu_log" or script["event_id"] in shadowed_attrs:
                    continue
                if script["resource"] != game["resource"] or script["delta"] != game["delta"]:
                    continue
                other = payload_by_id.get(script["event_id"], {})
                balances_match = all(isinstance(game_payload.get(key), (int, float))
                                     and game_payload[key] == other.get(key)
                                     for key in ("before", "after"))
                same_source = script["source"].split(".")[0] == game["source"].split(".")[0]
                distance = abs(script["ts"] - game["ts"])
                has_balances = all(isinstance(p.get(key), (int, float))
                                   for p in (game_payload, other) for key in ("before", "after"))
                if ((balances_match and distance <= 30)
                        or (not has_balances and same_source and distance <= 5)):
                    candidates.append(script)
            matches.append((game, candidates))
        matching_games = {}
        for game, candidates in matches:
            for script in candidates:
                matching_games.setdefault(script["event_id"], []).append(game)
        for game, candidates in matches:
            if len(candidates) == 1 and len(matching_games[candidates[0]["event_id"]]) == 1:
                script = candidates[0]
                shadowed_attrs.add(script["event_id"])
                game["run_id"] = script["run_id"]
                game["execution_script"] = script["script"]
                game["evidence_ids"] = [game["event_id"], script["event_id"]]
        attributions = [a for a in attributions if a["event_id"] not in shadowed_attrs]

        # 去重：时间贴脸（5 秒内）且数值一致 = 同一观察点，合并证据 event id，
        # 来源升到最高优先级；数值不同 = 证据冲突，各算各的观察并记冲突缺口
        observations: dict[str, list[dict]] = {}
        conflicts: list[tuple] = []
        by_resource: dict[str, list[dict]] = {}
        for entry in raw:
            if entry["event_id"] in shadowed_attrs:
                continue
            by_resource.setdefault(entry["resource"], []).append(entry)
        for name, entries in by_resource.items():
            entries.sort(key=lambda e: (e["ts"], e["sub"], -e["priority"], e["event_id"]))
            merged: list[dict] = []
            for entry in entries:
                if merged and entry["ts"] - merged[-1]["ts"] <= _LEDGER_MERGE_SECONDS:
                    last = merged[-1]
                    if entry["value"] == last["value"]:
                        last["evidence"].append(entry["event_id"])
                        if entry["priority"] > last["priority"]:
                            last.update(priority=entry["priority"], source=entry["source"])
                        continue
                    # 同事件的 before/after 本来就不同值，不算冲突；
                    # 不同来源贴脸读数不一致才是证据冲突
                    if entry["event_id"] != last["event_id"]:
                        conflicts.append((name, last["ts"], entry["ts"],
                                          last["value"], entry["value"]))
                merged.append(entry)
            observations[name] = merged

        # ── 余额观察序列：余额折线图的取点来源，与 opening/closing 同一条链 ──
        # 同一时刻多笔读数（大阪城 before/after 这类同 ts 的成对读数）留最新一笔，
        # 保证一个时刻只对应图上的一个点；缺的资源不补零，前端画成断点
        balance_by_ts: dict[float, dict] = {}
        for name in LEDGER_RESOURCES:
            for obs in observations.get(name, []):
                ts = obs["ts"]
                if ts < from_ts or ts > to_ts:
                    continue
                point = balance_by_ts.get(ts)
                if point is None:
                    point = {"ts": ts,
                             "date": datetime.fromtimestamp(
                                 ts, _LEDGER_TZ).date().isoformat(),
                             "values": {}}
                    balance_by_ts[ts] = point
                point["values"][name] = obs["value"]
        balance_series = sorted(balance_by_ts.values(), key=lambda p: p["ts"])

        # ── 同 run 前后盘点残差：收杂物箱等没有逐笔记账的流程，
        # 用 run 自带的 before/after 快照净差认账（扣除该 run 已逐笔确认的部分），
        # 否则收邮箱这类收益全落进「不知道谁干的」。confidence 用 inferred：
        # 净差是快照夹出来的推断，不是逐笔实证，不够格把当天置信度顶到 high
        by_run: dict[str, dict] = {}
        for row in rows:
            if row["event_type"] != "inventory.captured" or not row["run_id"]:
                continue
            slot = by_run.setdefault(row["run_id"], {"before": None, "after": None,
                                                     "script": row["script"]})
            phase = _loads(row["payload"], {}).get("phase")
            if phase == "before" and slot["before"] is None:
                slot["before"] = row
            elif phase == "after":
                slot["after"] = row
        for run_id, slot in by_run.items():
            before_row, after_row = slot["before"], slot["after"]
            if (before_row is None or after_row is None
                    or after_row["ts"] <= before_row["ts"]):
                continue
            left = _loads(before_row["payload"], {}).get("resources") or {}
            right = _loads(after_row["payload"], {}).get("resources") or {}
            run_delta: dict[str, float] = {}
            for a in attributions:
                if (a["run_id"] == run_id or (a["script"] == "youzu_log"
                        and before_row["ts"] <= a["ts"] <= after_row["ts"])):
                    run_delta[a["resource"]] = run_delta.get(a["resource"], 0) + a["delta"]
            for name in left.keys() & right.keys():
                if not (isinstance(left.get(name), (int, float))
                        and isinstance(right.get(name), (int, float))):
                    continue
                residual = right[name] - left[name] - run_delta.get(name, 0)
                if not residual:
                    continue
                attributions.append({
                    "id": f"a{len(attributions) + 1}", "ts": after_row["ts"],
                    "resource": name, "delta": residual,
                    "source": "inventory.run_delta",
                    "label": f"盘点净差 {int(residual):+d}",
                    "confidence": "inferred",
                    "script": slot["script"], "run_id": run_id,
                    "event_id": after_row["id"]})

        for index, item in enumerate(attributions, 1):
            item["id"] = f"a{index}"

        # ── 缺口：跨 run 快照差值 + 人工报备 + 证据冲突 ──
        gaps: list[dict] = []
        report_list = [{"id": r["id"], "occurred_at": r["occurred_at"],
                        "gap_key": r["gap_key"], "resource": r["resource"],
                        "claimed_delta": r["claimed_delta"]} for r in reports]
        captured = sorted((r for r in [*baseline_rows, *rows]
                           if r["event_type"] == "inventory.captured"),
                          key=lambda r: (r["ts"], r["id"]))
        for prev, cur in zip(captured, captured[1:]):
            prev_payload = _loads(prev["payload"], {})
            cur_payload = _loads(cur["payload"], {})
            if (cur_payload.get("phase") != "before"
                    or prev_payload.get("phase") not in ("after", None)):
                continue
            if prev["run_id"] == cur["run_id"]:
                continue
            if cur["ts"] < from_ts or prev["ts"] > to_ts:
                continue
            left = prev_payload.get("resources") or {}
            right = cur_payload.get("resources") or {}
            delta = {name: right[name] - left[name] for name in left.keys() & right.keys()
                     if isinstance(left.get(name), (int, float))
                     and isinstance(right.get(name), (int, float))
                     and right[name] != left[name]}
            if not delta:
                continue
            gap_key = f'{prev["id"]}:{cur["id"]}'
            linked = [r["id"] for r in report_list
                      if r["gap_key"] == gap_key
                      or (prev["ts"] < r["occurred_at"] <= cur["ts"]
                          and (not r["resource"] or r["resource"] in delta))]
            gaps.append({"id": f'gap-{int(prev["ts"])}-{int(cur["ts"])}',
                         "from": prev["ts"], "to": cur["ts"], "resources": delta,
                         "reason": "no_observation", "human_report_ids": linked})
        linked_report_ids = {rid for gap in gaps for rid in gap["human_report_ids"]}
        for report in report_list:
            # 没挂上任何缺口的窗口内人工报备单独成条：不改写库存；
            # 带了 resource 的认领只波及该资源的置信度，
            # 旧的无资源报备只留档，不再代表当天所有资源都已认领
            if (report["id"] in linked_report_ids
                    or not from_ts <= report["occurred_at"] <= to_ts):
                continue
            resources = ({report["resource"]: report["claimed_delta"]}
                         if report["resource"] else {})
            gaps.append({"id": f'gap-hr-{report["id"]}',
                         "from": report["occurred_at"], "to": report["occurred_at"],
                         "resources": resources, "reason": "human_reported",
                         "human_report_ids": [report["id"]]})
        for name, ts_a, ts_b, value_a, value_b in conflicts:
            gaps.append({"id": f"gap-conflict-{int(ts_a)}-{int(ts_b)}",
                         "from": ts_a, "to": ts_b,
                         "resources": {name: value_b - value_a},
                         "reason": "conflicting_evidence", "human_report_ids": []})
        gaps.sort(key=lambda g: (g["from"], g["id"]))

        def _gap_ids(start: float, end: float,
                     resource: str | None = None) -> list[str]:
            ids = []
            for g in gaps:
                if not (g["from"] <= end and g["to"] >= start):
                    continue
                # 缺口必须点名波及资源才影响该资源置信度；
                # 旧版无资源的人工报备（resources 为空）只留档，不波及任何资源
                if resource is not None and resource not in g["resources"]:
                    continue
                ids.append(g["id"])
            return ids

        def _confidence(obs_count: int, attrs: list[dict], paired: bool,
                        start: float, end: float,
                        resource: str | None = None) -> str:
            # 观察缺失 / 有波及该资源的人工报备或缺口 / 证据冲突 = low；
            # 观察链完整且有 confirmed 覆盖 = high；只有观察差值无归因 = medium
            if obs_count == 0 or _gap_ids(start, end, resource):
                return "low"
            if paired and any(a["confidence"] == "confirmed" for a in attrs):
                return "high"
            return "medium"

        def _pair(all_obs: list[dict], start: float, end: float):
            """opening = 窗前基线（没有则窗内首观察），closing = 窗内末观察。

            只有孤零零一条观察时不构成 opening/closing 对，返回 paired=False。
            """
            before = [o for o in all_obs if o["ts"] < start]
            within = [o for o in all_obs if start <= o["ts"] <= end]
            opening = before[-1] if before else (within[0] if within else None)
            closing = within[-1] if within else None
            paired = bool(opening is not None and closing is not None
                          and opening is not closing)
            return opening, closing, within, paired

        # Keep the recorded workflow name with its receipts, including historical runs.
        run_ids = {a.get("run_id") for a in [*attributions, *unresolved_changes] if a.get("run_id")}
        run_labels = {}
        for run_id in run_ids:
            run = conn.execute("SELECT label FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run and run["label"]:
                run_labels[run_id] = run["label"]
        for item in [*attributions, *unresolved_changes]:
            if item.get("run_id") in run_labels:
                item["run_label"] = run_labels[item["run_id"]]

        per_resource = []
        for name in LEDGER_RESOURCES:
            opening, closing, within, paired = _pair(
                observations.get(name, []), from_ts, to_ts)
            attrs = [a for a in attributions if a["resource"] == name]
            attributed = sum(a["delta"] for a in attrs)
            total = closing["value"] - opening["value"] if paired else None
            per_resource.append({
                "resource": name,
                "opening": opening["value"] if opening else None,
                "closing": closing["value"] if closing else None,
                "total_delta": total,
                "attributed_delta": attributed,
                "unattributed_delta": (total - attributed) if total is not None else None,
                "observation_count": len(within),
                "confidence": _confidence(len(within), attrs, paired,
                                          from_ts, to_ts, name),
            })

        # ── 按 Asia/Shanghai 日期分桶：跨日 run 按观察发生日记账 ──
        daily_series = []
        day = datetime.fromtimestamp(from_ts, _LEDGER_TZ).replace(
            hour=0, minute=0, second=0, microsecond=0)
        last_day = datetime.fromtimestamp(to_ts, _LEDGER_TZ).replace(
            hour=0, minute=0, second=0, microsecond=0)
        while day <= last_day:
            next_day = day + timedelta(days=1)
            start_ts, end_ts = day.timestamp(), next_day.timestamp() - 1e-6
            for name in LEDGER_RESOURCES:
                opening, closing, within, paired = _pair(
                    observations.get(name, []), start_ts, end_ts)
                day_attrs = [a for a in attributions
                             if a["resource"] == name and start_ts <= a["ts"] <= end_ts]
                if not within and not day_attrs:
                    continue
                attributed = sum(a["delta"] for a in day_attrs)
                total = closing["value"] - opening["value"] if paired else None
                daily_series.append({
                    "date": day.date().isoformat(), "resource": name,
                    "opening": opening["value"] if opening else None,
                    "closing": closing["value"] if closing else None,
                    "total_delta": total,
                    "attributed_delta": attributed,
                    "unattributed_delta": (total - attributed) if total is not None else None,
                    "observation_count": len(within),
                    "confidence": _confidence(len(within), day_attrs, paired,
                                              start_ts, end_ts, name),
                    "gap_ids": _gap_ids(start_ts, end_ts, name),
                    "attribution_ids": [a["id"] for a in day_attrs],
                })
            day = next_day

        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "generated_at": time.time(),
            "window": {"from": from_ts, "to": to_ts, "timezone": "Asia/Shanghai",
                       "days": round((to_ts - from_ts) / 86400, 2)},
            "per_resource": per_resource,
            "daily_series": daily_series,
            "balance_series": balance_series,
            "gaps": gaps,
            "attributions": attributions,
            "unresolved_changes": unresolved_changes,
        }

    def recent_events(self, limit: int = 100, event_type: str | None = None,
                      script: str | None = None,
                      before_id: int | None = None,
                      from_ts: float | None = None,
                      to_ts: float | None = None) -> list[dict]:
        clauses, args = [], []
        if event_type:
            clauses.append("event_type = ?")
            args.append(event_type)
        if script:
            clauses.append("script = ?")
            args.append(script)
        if before_id is not None:
            clauses.append("id < ?")
            args.append(max(1, int(before_id)))
        if from_ts is not None:
            clauses.append("ts >= ?")
            args.append(float(from_ts))
        if to_ts is not None:
            clauses.append("ts < ?")
            args.append(float(to_ts))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        args.append(max(1, min(int(limit), 1001)))
        rows = self._conn().execute(
            "SELECT id, ts, run_id, script, event_type, payload FROM events" +
            where + " ORDER BY id DESC LIMIT ?", args,
        ).fetchall()
        return [{"id": r["id"], "ts": r["ts"], "run_id": r["run_id"],
                 "script": r["script"], "event_type": r["event_type"],
                 "payload": _loads(r["payload"], {})} for r in rows]

    def run_summary(self, run_id: str) -> dict | None:
        """Build one human-facing task result from structured events only."""
        run = self._conn().execute(
            "SELECT run_id, script, started_at, ended_at, status, label FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if not run:
            return None
        rows = self._conn().execute(
            "SELECT ts, event_type, payload FROM events WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        events = [{"ts": r["ts"], "event_type": r["event_type"],
                   "payload": _loads(r["payload"], {})} for r in rows]
        loop_events = [e for e in events if e["event_type"] in {
            "osaka.floor_completed", "sortie.completed", "raid.round_completed",
            "pumpkin.sortie_completed", "sortie.retreated_before_boss",
            "edocastle.run_completed", "hanafuda.run_completed",
        }]
        osaka = [e for e in loop_events if e["event_type"] == "osaka.floor_completed"]
        pace_keys = {_loop_pace_key(e["event_type"], e["payload"])
                     for e in loop_events}
        intervals = [b["ts"] - a["ts"] for a, b in zip(loop_events, loop_events[1:])
                     if b["ts"] > a["ts"]]
        average = sum(intervals) / len(intervals) if intervals else None
        play_duration = ((loop_events[-1]["ts"] - run["started_at"])
                         if loop_events and loop_events[-1]["ts"] >= run["started_at"]
                         else None)
        repairs = [e for e in events if e["event_type"] == "repair.session_completed"]
        completed_repairs = [e for e in repairs
                             if int(e["payload"].get("repaired") or
                                    e["payload"].get("count") or 0) > 0]
        snapshots = [e for e in events if e["event_type"] == "inventory.captured"]
        peeks = [e for e in events if e["event_type"] == "inventory.peek"]
        attributed_deltas: dict[str, int | float] = {}
        resource_change_count = 0
        for event in events:
            if event["event_type"] != "resource.change":
                continue
            payload = event["payload"]
            delta = payload.get("delta")
            resource = payload.get("resource")
            if not resource or not isinstance(delta, (int, float)) or not delta:
                continue
            resource_change_count += 1
            attributed_deltas[resource] = attributed_deltas.get(resource, 0) + delta
        before = next((e for e in snapshots if e["payload"].get("phase") == "before"), None)
        after = next((e for e in reversed(snapshots)
                      if e["payload"].get("phase") == "after"), None)
        deltas = {}
        if before and after:
            left = before["payload"].get("resources") or {}
            right = after["payload"].get("resources") or {}
            for name in left.keys() | right.keys():
                if isinstance(left.get(name), (int, float)) and isinstance(right.get(name), (int, float)):
                    deltas[name] = right[name] - left[name]
        # 挖地小判掉落率实验：没有前后盘点时，用实验自带的开工/收场小判顶上
        # （run 级盘点 2026-08 退役后，这是挖地成绩单小判差值的主要来源）
        koban_science = None
        if not (before and after):
            sci = [e["payload"] for e in events
                   if e["event_type"] == "osaka.koban_session"
                   and isinstance(e["payload"].get("before"), (int, float))
                   and isinstance(e["payload"].get("after"), (int, float))]
            if sci:
                koban_science = sci[-1]
                deltas["小判"] = (int(koban_science["after"])
                                  - int(koban_science["before"]))
        selected = [e["payload"].get("selected_floor") for e in osaka
                    if e["payload"].get("selected_floor") is not None]
        return {
            "run_id": run["run_id"], "script": run["script"],
            "label": run["label"],
            "started_at": run["started_at"], "ended_at": run["ended_at"],
            "status": run["status"],
            "duration_seconds": ((run["ended_at"] - run["started_at"])
                                 if run["ended_at"] else None),
            "play_duration_seconds": round(play_duration, 1) if play_duration is not None else None,
            "loops": len(loop_events),
            "selected_floor": selected[-1] if selected else None,
            "average_loop_seconds": round(average, 1) if average else None,
            "estimated_6h_loops": int(21600 // average) if average else None,
            "repair_sessions": len(completed_repairs),
            "repaired_swords": sum(int(e["payload"].get("repaired") or 0) for e in repairs),
            "speedups": sum(int(e["payload"].get("speedups") or 0) for e in repairs),
            "equipment_restores": sum(1 for e in events
                                      if e["event_type"] == "equipment.restored"),
            "resource_delta": deltas,
            "attributed_resource_delta": attributed_deltas,
            "resource_change_count": resource_change_count,
            "inventory_observation": (peeks[-1]["payload"] if peeks else None),
            "inventory_observation_count": len(peeks),
            "has_resource_comparison": bool(before and after) or bool(koban_science),
            "has_before_snapshot": bool(before) or bool(koban_science),
            "has_after_snapshot": bool(after) or bool(koban_science),
            "after_snapshot_source": (after["payload"].get("source") if after
                                      else ("auto_science" if koban_science else None)),
            "koban_session": koban_science,
            # 逐圈事实：loop_started × 结束事件配对（含未闭合 → 结果未知）
            "loop_records": pair_loop_records(events),
            # 圈速口径统一（同玩法同图）才允许把 average_loop_seconds 当圈速展示
            "loop_pace_unified": len(pace_keys) <= 1,
        }

    def recent_run_summaries(self, limit: int = 20, script: str | None = None,
                             before_started_at: float | None = None,
                             from_ts: float | None = None,
                             to_ts: float | None = None,
                             status: str | None = None) -> list[dict]:
        clauses, args = ["status != 'running'"], []
        if script:
            clauses.append("script = ?")
            args.append(script)
        if status:
            clauses.append("status = ?")
            args.append(status)
        if before_started_at is not None:
            clauses.append("started_at < ?")
            args.append(float(before_started_at))
        if from_ts is not None:
            clauses.append("started_at >= ?")
            args.append(float(from_ts))
        if to_ts is not None:
            clauses.append("started_at < ?")
            args.append(float(to_ts))
        args.append(max(1, min(int(limit), 101)))
        rows = self._conn().execute(
            "SELECT run_id FROM runs WHERE " + " AND ".join(clauses) +
            " ORDER BY started_at DESC LIMIT ?", args,
        ).fetchall()
        return [summary for row in rows
                if (summary := self.run_summary(row["run_id"])) is not None]

    def recent_observations(self, limit: int = 100, script: str | None = None,
                            matched: bool | None = None) -> list[dict]:
        clauses, args = [], []
        if script:
            clauses.append("script = ?")
            args.append(script)
        if matched is not None:
            clauses.append("matched = ?")
            args.append(int(matched))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        args.append(max(1, min(int(limit), 1000)))
        rows = self._conn().execute(
            "SELECT id, ts, run_id, script, kind, expected, match_mode, matched, "
            "roi, tokens, error FROM observations" + where +
            " ORDER BY id DESC LIMIT ?", args,
        ).fetchall()
        return [{"id": r["id"], "ts": r["ts"], "run_id": r["run_id"],
                 "script": r["script"], "kind": r["kind"],
                 "expected": r["expected"], "match_mode": r["match_mode"],
                 "matched": None if r["matched"] is None else bool(r["matched"]),
                 "roi": _loads(r["roi"], []), "tokens": _loads(r["tokens"], []),
                 "error": r["error"]} for r in rows]

    def summary(self, days: int = 30) -> dict:
        days = max(1, min(int(days), 3650))
        since = time.time() - days * 86400
        conn = self._conn()
        run_rows = conn.execute(
            "SELECT script, status, COUNT(*) count FROM runs WHERE started_at >= ? "
            "GROUP BY script, status", (since,),
        ).fetchall()
        run_by_script, run_by_status = {}, {}
        for row in run_rows:
            run_by_script[row["script"]] = run_by_script.get(row["script"], 0) + row["count"]
            run_by_status[row["status"]] = run_by_status.get(row["status"], 0) + row["count"]

        ocr = conn.execute(
            "SELECT COUNT(*) total, SUM(CASE WHEN matched = 1 THEN 1 ELSE 0 END) hits, "
            "SUM(CASE WHEN matched = 0 THEN 1 ELSE 0 END) misses "
            "FROM observations WHERE ts >= ?", (since,),
        ).fetchone()
        expected_rows = conn.execute(
            "SELECT expected, COUNT(*) total, "
            "SUM(CASE WHEN matched = 1 THEN 1 ELSE 0 END) hits "
            "FROM observations WHERE ts >= ? AND expected IS NOT NULL "
            "GROUP BY expected ORDER BY total DESC LIMIT 100", (since,),
        ).fetchall()
        event_rows = conn.execute(
            "SELECT event_type, COUNT(*) count FROM events WHERE ts >= ? "
            "GROUP BY event_type ORDER BY count DESC", (since,),
        ).fetchall()
        activity_rows = conn.execute(
            "SELECT event_type, payload FROM events WHERE ts >= ? AND event_type IN "
            "('sortie.completed', 'sortie.retreated_before_boss', "
            "'osaka.floor_completed', 'edocastle.run_completed', 'raid.round_completed', "
            "'pumpkin.sortie_completed', 'practice.result')",
            (since,),
        ).fetchall()
        activity = {"sorties": 0, "practice": {"total": 0, "wins": 0,
                                                "losses": 0, "unknown": 0},
                    "sortie_groups": []}
        sortie_groups: dict[tuple, dict] = {}
        for row in activity_rows:
            event_type = row["event_type"]
            payload = _loads(row["payload"], {})
            if event_type == "practice.result":
                activity["practice"]["total"] += 1
                result = str(payload.get("result") or payload.get("outcome") or "").lower()
                if "胜" in result or result.startswith("win") or result == "won":
                    activity["practice"]["wins"] += 1
                elif "败" in result or result.startswith("lose") or result == "lost":
                    activity["practice"]["losses"] += 1
                else:
                    activity["practice"]["unknown"] += 1
                continue
            activity["sorties"] += 1
            if event_type == "osaka.floor_completed":
                key = ("osaka", payload.get("selected_floor"))
            elif event_type == "edocastle.run_completed":
                key = ("edocastle", payload.get("difficulty"))
            elif event_type in {"sortie.completed", "sortie.retreated_before_boss"}:
                key = (event_type, payload.get("mode"), payload.get("chapter"),
                       payload.get("map_no"))
            elif event_type == "raid.round_completed":
                key = ("raid", payload.get("difficulty"), bool(payload.get("triple")))
            else:
                key = ("pumpkin",)
            group = sortie_groups.setdefault(key, {
                "event_type": event_type, "payload": payload, "count": 0,
            })
            group["count"] += 1
        activity["sortie_groups"] = sorted(
            sortie_groups.values(), key=lambda item: item["count"], reverse=True)
        matched_total = int(ocr["hits"] or 0) + int(ocr["misses"] or 0)
        return {
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "generated_at": time.time(),
            "window": {"days": days, "since": since,
                       "retention_days": DEFAULT_RETENTION_DAYS,
                       "detail_retention_days": DEFAULT_RETENTION_DAYS,
                       "history_retention_days": None},
            "runs": {"total": sum(run_by_status.values()),
                     "by_script": run_by_script, "by_status": run_by_status},
            "ocr": {
                "total": int(ocr["total"] or 0),
                "matched": int(ocr["hits"] or 0),
                "missed": int(ocr["misses"] or 0),
                "match_rate": round(int(ocr["hits"] or 0) / matched_total, 4)
                if matched_total else None,
                "by_expected": [
                    {"expected": r["expected"], "total": r["total"],
                     "matched": int(r["hits"] or 0),
                     "match_rate": round(int(r["hits"] or 0) / r["total"], 4)}
                    for r in expected_rows
                ],
            },
            "events": {"total": sum(r["count"] for r in event_rows),
                       "by_type": {r["event_type"]: r["count"] for r in event_rows}},
            "activity": activity,
        }

    def prune(self, retention_days: int | None = None) -> None:
        """Trim bulky OCR detail; explicit retention keeps the legacy full trim."""
        days = DEFAULT_RETENTION_DAYS if retention_days is None else retention_days
        cutoff = time.time() - max(1, int(days)) * 86400
        try:
            conn = self._conn()
            conn.execute("DELETE FROM observations WHERE ts < ?", (cutoff,))
            if retention_days is not None:
                conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
                conn.execute("DELETE FROM human_reports WHERE occurred_at < ?", (cutoff,))
            conn.commit()
        except Exception:
            pass


_store: TelemetryStore | None = None
_jp_store: TelemetryStore | None = None
_lock = threading.Lock()


def get_telemetry_store() -> TelemetryStore:
    global _store
    if _store is None:
        with _lock:
            if _store is None:
                _store = TelemetryStore()
    return _store


def get_jp_telemetry_store() -> TelemetryStore:
    """日服账房库：独立文件（jp/telemetry.db），与国服互不可见。"""
    global _jp_store
    if _jp_store is None:
        with _lock:
            if _jp_store is None:
                candidate = TelemetryStore(JP_DATA_DIR / "telemetry.db")
                from .jp_resource_repair import repair_legacy_resources
                try:
                    repair_legacy_resources(candidate)
                except Exception:
                    candidate.close()
                    raise
                _jp_store = candidate
    return _jp_store


def record_event(event_type: str, payload: dict | None = None) -> int | None:
    """记录事件，返回事件 id（失败返回 None）。"""
    return get_telemetry_store().record_event(event_type, payload)
