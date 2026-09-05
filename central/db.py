from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core import ports
from core.spec import Spec

from . import auth


def _default_db_path() -> Path:
    """Prefer a native-Linux location (works reliably on WSL/drvfs where
    SQLite journal deletes are not propagated by the 9p cache). Falls back
    to the project dir for dev setups where XDG is not set."""
    root = os.environ.get("SURSUMAI_DB_DIR")
    if root:
        p = Path(root) / "sursumai.db"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    try:
        p = Path(xdg) / "sursumai" / "sursumai.db"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    except OSError:
        pass
    return Path(__file__).resolve().parent.parent / "sursumai.db"


DB_PATH = _default_db_path()


class DeployState:
    PENDING = "pending"
    CHECKING = "checking"
    PROVISIONING = "provisioning"
    HEALTHY = "healthy"
    FAILED = "failed"
    DESTROYING = "destroying"
    REDEPLOYING = "redeploying"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class User:
    def __init__(self, id: str, email: str, name: str, password_hash: str,
                 created_at: str | None = None):
        self.id = id
        self.email = email
        self.name = name
        self.password_hash = password_hash
        self.created_at = created_at or _now()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "created_at": self.created_at,
        }


class Session:
    def __init__(self, token_hash: str, user_id: str, expires_at: float,
                 created_at: str | None = None):
        self.token_hash = token_hash
        self.user_id = user_id
        self.expires_at = expires_at
        self.created_at = created_at or _now()


class Deploy:
    def __init__(self, spec: Spec, user_id: str, id: str | None = None,
                 status: str = DeployState.PENDING, endpoint: str | None = None,
                 preflight: list[dict] | None = None,
                 created_at: str | None = None, updated_at: str | None = None,
                 error: str | None = None):
        self.id = id or uuid.uuid4().hex
        self.spec = spec
        self.user_id = user_id
        self.status = status
        self.endpoint = endpoint
        self.preflight = preflight
        self.created_at = created_at or _now()
        self.updated_at = updated_at or self.created_at
        self.error = error

    def to_dict(self) -> dict:
        spec = self.spec.to_dict()
        if spec.get("hf_token"):
            spec["hf_token"] = "***"
        return {
            "id": self.id,
            "user_id": self.user_id,
            "spec": spec,
            "status": self.status,
            "endpoint": self.endpoint,
            "preflight": self.preflight,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
        }


class Pool:
    def __init__(self, user_id: str, name: str, weak_id: str, strong_id: str,
                 judge_id: str | None = None, mode: str = "escalation",
                 id: str | None = None, created_at: str | None = None):
        self.id = id or uuid.uuid4().hex
        self.user_id = user_id
        self.name = name
        self.weak_id = weak_id
        self.strong_id = strong_id
        self.judge_id = judge_id
        self.mode = mode
        self.created_at = created_at or _now()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "weak_id": self.weak_id,
            "strong_id": self.strong_id,
            "judge_id": self.judge_id,
            "mode": self.mode,
            "created_at": self.created_at,
        }


class RouterSession:
    def __init__(self, id: str, pool_id: str, user_id: str, latched: bool = False,
                 streak: int = 0, created_at: str | None = None,
                 updated_at: str | None = None):
        self.id = id
        self.pool_id = pool_id
        self.user_id = user_id
        self.latched = latched
        self.streak = streak
        self.created_at = created_at or _now()
        self.updated_at = updated_at or self.created_at

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pool_id": self.pool_id,
            "user_id": self.user_id,
            "latched": self.latched,
            "streak": self.streak,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class Store:
    def __init__(self, path: Path = DB_PATH):
        self._port_lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        # sessions are keyed by sha256(token) — the plaintext bearer never
        # touches the disk. A pre-hash database has a `token` column: drop it
        # (those sessions cannot be migrated, everyone simply logs in again).
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(sessions)")]
        if cols and "token_hash" not in cols:
            self._conn.execute("DROP TABLE sessions")
            cols = []
        if not cols:
            self._conn.execute(
                """
                CREATE TABLE sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(deploys)")]
        if not cols:
            self._conn.execute(
                """
                CREATE TABLE deploys (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    spec TEXT NOT NULL,
                    status TEXT NOT NULL,
                    endpoint TEXT,
                    preflight TEXT,
                    port INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT
                )
                """
            )
        else:
            if "user_id" not in cols:
                self._conn.execute("ALTER TABLE deploys ADD COLUMN user_id TEXT")
            if "preflight" not in cols:
                self._conn.execute("ALTER TABLE deploys ADD COLUMN preflight TEXT")
            if "port" not in cols:
                self._conn.execute("ALTER TABLE deploys ADD COLUMN port INTEGER")
                self._backfill_ports()
        # One deploy per port, enforced by the database rather than by whoever
        # remembers to check. Partial index: legacy rows (port NULL) are exempt.
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_deploys_port "
            "ON deploys(port) WHERE port IS NOT NULL"
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metrics (
                deploy_id TEXT NOT NULL,
                ts REAL NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        # every metrics read is "this deploy, newest first"
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_metrics_deploy_ts ON metrics(deploy_id, ts DESC)"
        )
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(pools)")]
        if not cols:
            self._conn.execute(
                """
                CREATE TABLE pools (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    weak_id TEXT NOT NULL,
                    strong_id TEXT NOT NULL,
                    judge_id TEXT,
                    mode TEXT NOT NULL DEFAULT 'escalation',
                    created_at TEXT NOT NULL
                )
                """
            )
        elif "mode" not in cols:
            self._conn.execute("ALTER TABLE pools ADD COLUMN mode TEXT NOT NULL DEFAULT 'escalation'")

        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pool_models (
                pool_id TEXT NOT NULL,
                deploy_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY (pool_id, deploy_id)
            )
            """
        )
        # backfill: pools criados antes do pool_models ganham weak/strong como entradas
        existing = self._conn.execute(
            "SELECT p.id, p.weak_id, p.strong_id FROM pools p "
            "WHERE NOT EXISTS (SELECT 1 FROM pool_models pm WHERE pm.pool_id = p.id) "
            "AND p.weak_id IS NOT NULL AND p.weak_id != ''"
        ).fetchall()
        for row in existing:
            self._conn.execute(
                "INSERT OR IGNORE INTO pool_models (pool_id, deploy_id, position) VALUES (?, ?, 0)",
                (row["id"], row["weak_id"]),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO pool_models (pool_id, deploy_id, position) VALUES (?, ?, 1)",
                (row["id"], row["strong_id"]),
            )
        if existing:
            self._conn.commit()
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(router_sessions)")]
        if not cols:
            self._conn.execute(
                """
                CREATE TABLE router_sessions (
                    id TEXT PRIMARY KEY,
                    pool_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    latched INTEGER NOT NULL DEFAULT 0,
                    streak INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS router_log (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                pool_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                model_served TEXT NOT NULL,
                decision TEXT NOT NULL,
                tokens INTEGER NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )

    # ---- users ----

    def create_user(self, email: str, name: str, password_hash: str) -> User:
        user = User(id=uuid.uuid4().hex, email=email, name=name, password_hash=password_hash)
        self._conn.execute(
            "INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (user.id, user.email, user.name, user.password_hash, user.created_at),
        )
        self._conn.commit()
        return user

    def get_user_by_email(self, email: str) -> User | None:
        row = self._conn.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()
        return self._user_from_row(row) if row else None

    def get_user_by_id(self, id: str) -> User | None:
        row = self._conn.execute("SELECT * FROM users WHERE id = ?", (id,)).fetchone()
        return self._user_from_row(row) if row else None

    def _user_from_row(self, row: sqlite3.Row) -> User:
        return User(
            id=row["id"], email=row["email"], name=row["name"],
            password_hash=row["password_hash"], created_at=row["created_at"],
        )

    # ---- sessions ----

    def create_session(self, token: str, user_id: str, expires_at: float) -> None:
        self._conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (auth.hash_token(token), user_id, _now(), expires_at),
        )
        self._conn.commit()

    def get_user_by_token(self, token: str) -> User | None:
        token_hash = auth.hash_token(token)
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < time.time():
            self._conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            self._conn.commit()
            return None
        return self.get_user_by_id(row["user_id"])

    def delete_session(self, token: str) -> None:
        self._conn.execute(
            "DELETE FROM sessions WHERE token_hash = ?", (auth.hash_token(token),)
        )
        self._conn.commit()

    def purge_expired_sessions(self) -> None:
        self._conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        self._conn.commit()

    # ---- deploys ----

    def _backfill_ports(self) -> None:
        """Move the port out of the spec JSON and into its own column.

        Deploys created before ports were allocated have no port at all: they
        are left NULL and keep answering on their hashed port. Two of those can
        already share a port — that is the bug — but rewriting a running
        deploy's port here would only break it further; they get a real port
        the next time they are redeployed.
        """
        taken: set[int] = set()
        for row in self._conn.execute("SELECT id, spec FROM deploys").fetchall():
            try:
                port = json.loads(row["spec"]).get("port")
            except (ValueError, TypeError):
                continue
            if not ports.in_range(port) or port in taken:
                continue
            taken.add(port)
            self._conn.execute("UPDATE deploys SET port = ? WHERE id = ?", (port, row["id"]))
        self._conn.commit()

    def taken_ports(self, exclude_id: str | None = None) -> set[int]:
        """Every port currently spoken for, allocated or legacy-hashed."""
        taken: set[int] = set()
        for row in self._conn.execute("SELECT id, port FROM deploys").fetchall():
            if exclude_id is not None and row["id"] == exclude_id:
                continue
            if row["port"] is not None:
                taken.add(row["port"])
            else:
                # no allocated port: it is listening on its hashed one
                taken.add(ports.legacy_port(row["id"]))
        return taken

    def allocate_port(self, exclude_id: str | None = None) -> int:
        """Reserve the lowest free deploy port.

        Held under a lock and re-checked against the UNIQUE index on write, so
        two deploys created at the same moment cannot land on the same port.
        """
        with self._port_lock:
            return ports.first_free(self.taken_ports(exclude_id))

    def create(self, spec: Spec, user_id: str) -> Deploy:
        """Create a deploy, allocating its port if it does not have one."""
        with self._port_lock:
            for _ in range(len(ports.PORT_RANGE)):
                if not ports.in_range(spec.port):
                    spec.port = ports.first_free(self.taken_ports())
                deploy = Deploy(spec, user_id)
                try:
                    self._upsert(deploy)
                    return deploy
                except sqlite3.IntegrityError:
                    # someone took that port between the read and the write
                    spec.port = None
            raise ports.NoPortAvailable("could not reserve a deploy port")

    def get(self, id: str) -> Deploy | None:
        row = self._conn.execute("SELECT * FROM deploys WHERE id = ?", (id,)).fetchone()
        return self._from_row(row) if row else None

    def list(self, user_id: str | None = None) -> list[Deploy]:
        if user_id is None:
            rows = self._conn.execute("SELECT * FROM deploys ORDER BY created_at DESC").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM deploys WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
            ).fetchall()
        return [self._from_row(r) for r in rows]

    def update(self, deploy: Deploy) -> None:
        deploy.updated_at = _now()
        self._upsert(deploy)

    def delete(self, id: str) -> None:
        self._conn.execute("DELETE FROM deploys WHERE id = ?", (id,))
        self._conn.execute("DELETE FROM metrics WHERE deploy_id = ?", (id,))
        self._conn.commit()

    def clear_metrics(self, deploy_id: str) -> None:
        self._conn.execute("DELETE FROM metrics WHERE deploy_id = ?", (deploy_id,))
        self._conn.commit()

    # One snapshot every 10s per deploy adds up to ~8600 rows a day, forever.
    # The UI only ever reads the last few hundred, so older rows are dead
    # weight that slows every dashboard poll down.
    METRICS_KEEP = 1080  # ~3h of history at one snapshot per 10s

    def save_metrics(self, deploy_id: str, payload: dict) -> None:
        self._conn.execute(
            "INSERT INTO metrics (deploy_id, ts, payload) VALUES (?, ?, ?)",
            (deploy_id, payload.get("ts", time.time()), json.dumps(payload)),
        )
        self._conn.commit()
        self._prune_metrics(deploy_id)

    def _prune_metrics(self, deploy_id: str) -> None:
        """Drop everything older than the newest METRICS_KEEP snapshots."""
        self._conn.execute(
            """
            DELETE FROM metrics
            WHERE deploy_id = ? AND ts < (
                SELECT MIN(ts) FROM (
                    SELECT ts FROM metrics WHERE deploy_id = ?
                    ORDER BY ts DESC LIMIT ?
                )
            )
            """,
            (deploy_id, deploy_id, self.METRICS_KEEP),
        )
        self._conn.commit()

    def latest_metrics(self, deploy_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT payload FROM metrics WHERE deploy_id = ? ORDER BY ts DESC LIMIT 1",
            (deploy_id,),
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_metrics(self, deploy_id: str, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT ts, payload FROM metrics WHERE deploy_id = ? ORDER BY ts DESC LIMIT ?",
            (deploy_id, limit),
        ).fetchall()
        return [json.loads(r["payload"]) for r in reversed(rows)]

    def _upsert(self, deploy: Deploy) -> None:
        self._conn.execute(
            """
            INSERT INTO deploys (id, user_id, spec, status, endpoint, preflight, port, created_at, updated_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                spec=excluded.spec,
                status=excluded.status,
                endpoint=excluded.endpoint,
                preflight=excluded.preflight,
                port=excluded.port,
                updated_at=excluded.updated_at,
                error=excluded.error
            """,
            (
                deploy.id,
                deploy.user_id,
                json.dumps(deploy.spec.to_dict()),
                deploy.status,
                deploy.endpoint,
                json.dumps(deploy.preflight) if deploy.preflight is not None else None,
                deploy.spec.port if ports.in_range(deploy.spec.port) else None,
                deploy.created_at,
                deploy.updated_at,
                deploy.error,
            ),
        )
        self._conn.commit()

    def _from_row(self, row: sqlite3.Row) -> Deploy:
        return Deploy(
            id=row["id"],
            user_id=row["user_id"] or "",
            spec=Spec.from_dict(json.loads(row["spec"])),
            status=row["status"],
            endpoint=row["endpoint"],
            preflight=json.loads(row["preflight"]) if row["preflight"] else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            error=row["error"],
        )

    # ---- pools ----

    def create_pool(self, user_id: str, name: str, weak_id: str, strong_id: str,
                    judge_id: str | None = None, mode: str = "escalation") -> Pool:
        pool = Pool(user_id=user_id, name=name, weak_id=weak_id, strong_id=strong_id,
                    judge_id=judge_id, mode=mode)
        self._conn.execute(
            "INSERT INTO pools (id, user_id, name, weak_id, strong_id, judge_id, mode, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pool.id, pool.user_id, pool.name, pool.weak_id, pool.strong_id, pool.judge_id,
             pool.mode, pool.created_at),
        )
        self._conn.commit()
        return pool

    def get_pool(self, id: str) -> Pool | None:
        row = self._conn.execute("SELECT * FROM pools WHERE id = ?", (id,)).fetchone()
        return self._pool_from_row(row) if row else None

    def list_pools(self, user_id: str) -> list[Pool]:
        rows = self._conn.execute(
            "SELECT * FROM pools WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
        ).fetchall()
        return [self._pool_from_row(r) for r in rows]

    def delete_pool(self, id: str) -> None:
        self._conn.execute("DELETE FROM pools WHERE id = ?", (id,))
        self._conn.execute("DELETE FROM pool_models WHERE pool_id = ?", (id,))
        self._conn.commit()

    def update_pool(self, pool: Pool) -> None:
        self._conn.execute(
            "UPDATE pools SET name = ?, weak_id = ?, strong_id = ?, judge_id = ?, mode = ? WHERE id = ?",
            (pool.name, pool.weak_id, pool.strong_id, pool.judge_id, pool.mode, pool.id),
        )
        self._conn.commit()

    def get_pool_models(self, pool_id: str) -> list[str]:
        """Ordered deploy ids of a pool (position asc). Empty for legacy pools
        that only set weak_id/strong_id."""
        rows = self._conn.execute(
            "SELECT deploy_id FROM pool_models WHERE pool_id = ? ORDER BY position ASC",
            (pool_id,),
        ).fetchall()
        return [r["deploy_id"] for r in rows]

    def replace_pool_models(self, pool_id: str, deploy_ids: list[str]) -> None:
        self._conn.execute("DELETE FROM pool_models WHERE pool_id = ?", (pool_id,))
        for i, deploy_id in enumerate(deploy_ids):
            self._conn.execute(
                "INSERT INTO pool_models (pool_id, deploy_id, position) VALUES (?, ?, ?)",
                (pool_id, deploy_id, i),
            )
        self._conn.commit()

    def _pool_from_row(self, row: sqlite3.Row) -> Pool:
        return Pool(
            id=row["id"], user_id=row["user_id"], name=row["name"],
            weak_id=row["weak_id"], strong_id=row["strong_id"], judge_id=row["judge_id"],
            mode=row["mode"] if "mode" in row.keys() else "escalation",
            created_at=row["created_at"],
        )

    # ---- router sessions ----

    def get_router_session(self, id: str) -> RouterSession | None:
        row = self._conn.execute("SELECT * FROM router_sessions WHERE id = ?", (id,)).fetchone()
        return self._router_session_from_row(row) if row else None

    def upsert_router_session(self, session: RouterSession) -> None:
        session.updated_at = _now()
        self._conn.execute(
            """
            INSERT INTO router_sessions (id, pool_id, user_id, latched, streak, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                latched=excluded.latched,
                streak=excluded.streak,
                updated_at=excluded.updated_at
            """,
            (session.id, session.pool_id, session.user_id, int(session.latched),
             session.streak, session.created_at, session.updated_at),
        )
        self._conn.commit()

    def apply_judge_verdict(self, session_id: str, escalate: bool, confirmations: int = 2) -> None:
        """Atomically update streak/latch from an async judge verdict."""
        self._conn.execute(
            """
            UPDATE router_sessions
            SET streak = CASE WHEN ? THEN streak + 1 ELSE 0 END,
                latched = CASE WHEN ? AND streak + 1 >= ? THEN 1 ELSE latched END,
                updated_at = ?
            WHERE id = ?
            """,
            (int(escalate), int(escalate), confirmations, _now(), session_id),
        )
        self._conn.commit()

    def _router_session_from_row(self, row: sqlite3.Row) -> RouterSession:
        return RouterSession(
            id=row["id"], pool_id=row["pool_id"], user_id=row["user_id"],
            latched=bool(row["latched"]), streak=row["streak"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def touch_router_session(self, id: str) -> None:
        self._conn.execute(
            "UPDATE router_sessions SET updated_at = ? WHERE id = ?", (_now(), id)
        )
        self._conn.commit()

    def reset_router_session(self, id: str) -> None:
        self._conn.execute(
            "UPDATE router_sessions SET streak = 0, latched = 0, updated_at = ? WHERE id = ?",
            (_now(), id),
        )
        self._conn.commit()

    # ---- router log ----

    def log_router(self, session_id: str, pool_id: str, user_id: str, model_served: str,
                   decision: str, tokens: int = 0, latency_ms: int = 0) -> None:
        self._conn.execute(
            "INSERT INTO router_log (id, session_id, pool_id, user_id, model_served, decision, tokens, latency_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, session_id, pool_id, user_id, model_served, decision,
             tokens, latency_ms, _now()),
        )
        self._conn.commit()

    def list_router_log(self, pool_id: str | None = None, limit: int = 50) -> list[dict]:
        if pool_id:
            rows = self._conn.execute(
                "SELECT * FROM router_log WHERE pool_id = ? ORDER BY created_at DESC LIMIT ?",
                (pool_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM router_log ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]
