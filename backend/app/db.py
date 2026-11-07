"""DB 연결 — 롤별 작은 연결 풀. 끊긴 연결은 버리고 다시 붙는다 (D-07 7.2).

왜 풀인가 (실측 2026-11-03): 원격 pooler 에 새로 붙는 데 1.8초, 질의 1건은 0.3초다.
질의마다 새로 붙으면 진행 상태 조회(질의 4건) 한 번이 8초가 걸려 1초 폴링이 성립하지 않는다.

D-07 7.2 가 "붙잡지 않고 새로 연다"로 지키려던 것 — DB 가 잠깐 꺼졌다 돌아오면 다음 호출부터
저절로 다시 붙는다(F-6 reconnect_cache 의 토대) — 은 그대로 지킨다.
  - 깨진 연결은 풀이 버린다(psycopg_pool 이 반납 시 상태를 본다)
  - 읽기(fetch_*)는 깨진 연결을 만나면 **새 연결로 한 번만** 다시 한다. 읽기라 두 번 해도 안전하다
  - 쓰기(transaction)는 다시 하지 않는다 — 실패는 DB_UNAVAILABLE(retryable)로 화면에 간다
  - 풀에서 연결을 기다리는 시간도 연결 제한시간(2초)으로 묶는다 — /health 가 3초 안에 답해야 한다

롤을 2개 쓴다 (D-07 2.1):
  app      업무 데이터 R/W · healing 읽기만
  healing  사건 R/W · 주문 읽기만   <- API-044 승인 · API-049 주입이 이 롤로 쓴다
"""

import threading
from contextlib import contextmanager
import time

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from app import config

POOL_MAX = 4            # 롤당. Supabase Session pooler 의 연결 수 한도를 넘지 않게 작게 둔다
POOL_MAX_IDLE_SEC = 240  # pooler 가 유휴 연결을 끊기 전에 먼저 정리한다

_pools: dict[str, ConnectionPool] = {}
_lock = threading.Lock()


class DbUnavailable(RuntimeError):
    """DB 에 닿지 못했다 — F-6 의 입구다. 호출자가 캐시 폴백을 고른다."""


def _role_name(role: str) -> str:
    return "app_rw" if role == "app" else "healing_rw"


def _pool(role: str) -> ConnectionPool:
    if not config.db_configured():
        raise DbUnavailable("DATABASE_URL 미설정")
    with _lock:
        pool = _pools.get(role)
        if pool is None:
            pool = ConnectionPool(
                config.role_url(_role_name(role)),
                min_size=0, max_size=POOL_MAX, max_idle=POOL_MAX_IDLE_SEC,
                timeout=config.CONNECT_TIMEOUT_SEC,
                kwargs={"connect_timeout": config.CONNECT_TIMEOUT_SEC,
                        "options": f"-c statement_timeout={config.STATEMENT_TIMEOUT_MS}",
                        "row_factory": dict_row, "autocommit": True},
                open=True, name=f"moa-{role}")
            _pools[role] = pool
        return pool


@contextmanager
def connect(role: str = "app"):
    """psycopg3 연결(autocommit). role 은 'app' 또는 'healing'."""
    try:
        conn_cm = _pool(role).connection()
        conn = conn_cm.__enter__()
    except PoolTimeout as exc:
        raise DbUnavailable(f"연결 대기 초과 ({config.CONNECT_TIMEOUT_SEC}초)") from exc
    except psycopg.OperationalError as exc:
        raise DbUnavailable(str(exc).strip().splitlines()[0]) from exc
    try:
        yield conn
    except BaseException as exc:
        conn_cm.__exit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        conn_cm.__exit__(None, None, None)


@contextmanager
def cursor(role: str = "app"):
    with connect(role) as conn:
        with conn.cursor() as cur:
            yield cur


SLOW_QUERY_SEC = 2.0        # PRD 5.6.1 DB 판정 조건 "쿼리 2초 초과" (F-6 · FT-03)


def _watch_query(sql: str, started: float) -> None:
    """2초를 넘긴 쿼리를 감지 메트릭으로 남긴다.

    메트릭 기록 자체가 DB 쓰기이므로 **메트릭 표를 건드리는 SQL 은 세지 않는다** — 세면 무한 재귀한다.
    연결 오류(db_connection_error)는 여기서 적지 않는다: 연결이 실패한 상황에서 기록을 시도하면
    connect -> 기록 -> connect 로 되돌아간다. 그쪽은 main.py 의 DbUnavailable 처리기가 적는다.
    """
    if "healing.runtime_metric" in sql:
        return
    if time.monotonic() - started < SLOW_QUERY_SEC:
        return
    from app.services import metrics

    metrics.record("slow_query_count", "menu_store", 1, threshold=1, breached=True)


def _read(sql: str, params, role: str, one: bool):
    """읽기 1건. 풀에 있던 연결이 끊겨 있었으면 새 연결로 한 번 더 한다."""
    for attempt in (1, 2):
        started = time.monotonic()
        try:
            with cursor(role) as cur:
                cur.execute(sql, params)
                rows = cur.fetchone() if one else cur.fetchall()
            _watch_query(sql, started)
            return rows
        except psycopg.OperationalError as exc:
            # statement_timeout(QueryCanceled)은 연결 문제가 아니다 — 다시 하지 않는다
            if attempt == 2 or isinstance(exc, psycopg.errors.QueryCanceled):
                raise
            continue


def fetch_all(sql: str, params=None, *, role: str = "app") -> list[dict]:
    return _read(sql, params, role, one=False)


def fetch_one(sql: str, params=None, *, role: str = "app") -> dict | None:
    return _read(sql, params, role, one=True)


def execute(sql: str, params=None, *, role: str = "app") -> int:
    started = time.monotonic()
    with cursor(role) as cur:
        cur.execute(sql, params)
        rows = cur.rowcount
    _watch_query(sql, started)
    return rows


@contextmanager
def transaction(role: str = "app"):
    """여러 쓰기를 한 단위로 묶는다 — 주문 항목과 변경 이력은 함께 커밋되어야 한다."""
    with connect(role) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                yield cur


def close_pools() -> None:
    """프로세스 종료 · 테스트 정리용."""
    with _lock:
        for pool in _pools.values():
            pool.close()
        _pools.clear()


def health() -> dict:
    """/health 의 db 칸 (API-001). 세 분기를 구분해 돌려준다."""
    if not config.db_configured():
        return {"state": "unset", "text": "DATABASE_URL 미설정"}
    try:
        row = fetch_one("SELECT COUNT(*) AS n FROM app.menu WHERE menu_version = %s",
                        (config.MENU_VERSION,))
        return {"state": "connected", "text": "connected", "menu_rows": row["n"]}
    except DbUnavailable as exc:
        return {"state": "down", "text": str(exc)}
    except psycopg.Error as exc:
        return {"state": "error", "text": str(exc).strip().splitlines()[0]}
