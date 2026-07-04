import asyncio
import os

import structlog
from psycopg_pool import ConnectionPool

LEASE_DURATION_SECONDS = 60
HEARTBEAT_INTERVAL_SECONDS = 20

logger = structlog.get_logger(__name__)

_registration_complete = False


def is_registration_complete() -> bool:
    return _registration_complete


class SandboxRegistry:
    def __init__(
        self, pool: ConnectionPool, session_id: str, sandbox_id: str, sandbox_url: str
    ) -> None:
        self._pool = pool
        self._session_id = session_id
        self._sandbox_id = sandbox_id
        self._sandbox_url = sandbox_url
        self._heartbeat_task: asyncio.Task | None = None

    @staticmethod
    def create_from_env() -> "SandboxRegistry | None":
        session_id = os.environ.get("SESSION_ID")
        sandbox_id = os.environ.get("SANDBOX_ID")
        sandbox_url = os.environ.get("SANDBOX_URL")
        database_url = os.environ.get("DATABASE_URL")

        if not session_id or not sandbox_id or not sandbox_url or not database_url:
            logger.warning(
                "sandbox_registry_env_missing — skipping registration",
                has_session_id=bool(session_id),
                has_sandbox_id=bool(sandbox_id),
                has_sandbox_url=bool(sandbox_url),
                has_database_url=bool(database_url),
            )
            return None

        pool = ConnectionPool(database_url, min_size=1, max_size=2)
        return SandboxRegistry(pool, session_id, sandbox_id, sandbox_url)

    def register(self) -> None:
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO sandbox_registry
                        (session_id, sandbox_id, sandbox_url, status, lease_expires_at)
                    VALUES (%s, %s, %s, 'active', now() + make_interval(secs => %s))
                    ON CONFLICT (session_id) WHERE status = 'active'
                    DO UPDATE SET
                        sandbox_id = EXCLUDED.sandbox_id,
                        sandbox_url = EXCLUDED.sandbox_url,
                        lease_expires_at = EXCLUDED.lease_expires_at
                    """,
                    (self._session_id, self._sandbox_id, self._sandbox_url, LEASE_DURATION_SECONDS),
                )
            global _registration_complete
            _registration_complete = True
            logger.info(
                "sandbox_registered",
                session_id=self._session_id,
                sandbox_id=self._sandbox_id,
            )
        except Exception:
            logger.exception("sandbox_register_failed", session_id=self._session_id)
            raise

    def start_heartbeat(self) -> None:
        if self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            try:
                with self._pool.connection() as conn:
                    conn.execute(
                        """
                        UPDATE sandbox_registry
                        SET last_heartbeat = now(),
                            lease_expires_at = now() + make_interval(secs => %s)
                        WHERE session_id = %s AND status = 'active'
                        """,
                        (LEASE_DURATION_SECONDS, self._session_id),
                    )
            except Exception:
                logger.exception("sandbox_heartbeat_failed", session_id=self._session_id)

    def unregister(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    UPDATE sandbox_registry
                    SET status = 'shutdown'
                    WHERE session_id = %s AND status = 'active'
                    """,
                    (self._session_id,),
                )
            global _registration_complete
            _registration_complete = False
            logger.info("sandbox_unregistered", session_id=self._session_id)
        except Exception:
            logger.exception("sandbox_unregister_failed", session_id=self._session_id)

    def close(self) -> None:
        self.unregister()
        self._pool.close()
