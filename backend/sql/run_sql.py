"""sql/ 파일 실행기 — psql 대체 (D-07 8절 시드 파일을 순서대로 돌린다)

psql 클라이언트가 없는 환경에서 쓴다. psql 변수(:'app_pw')를 치환하고
파일을 순서대로 실행한 뒤 결과를 요약한다.

    python sql/run_sql.py                       # 01 ~ 13 전부 (backend/ 에서 실행)
    python sql/run_sql.py 04_views.sql          # 지정 파일만
    python sql/run_sql.py --check               # 실행하지 않고 현재 상태만 본다

접속 문자열은 backend/.env 의 DATABASE_URL 을 쓴다.
롤 비밀번호는 같은 파일의 APP_RW_PASSWORD · HEALING_RW_PASSWORD.
"""

import os
import pathlib
import sys

import psycopg
from dotenv import load_dotenv

# backend/ 폴더만 받아도 돌아가도록 이 파일 기준 경로만 쓴다
SQL_DIR = pathlib.Path(__file__).resolve().parent
ENV_PATH = SQL_DIR.parent / ".env"

# D-07 8절 순서. 05_vector.sql 은 U-15 도입 시에만 추가한다.
DEFAULT_ORDER = [
    "01_roles.sql",
    "02_schema_app.sql",
    "03_schema_healing.sql",
    "04_views.sql",
    "10_seed_brand.sql",
    "11_seed_menu_sbux.sql",
    "12_seed_menu_generic.sql",
    "13_seed_circuit.sql",
    "15_schema_workflows.sql",
]

# 시연 · 검수용. 기본 순서에 넣지 않는다 — 운영 배포에서 돌리지 않기 위해서다
DEMO = ["14_seed_demo.sql"]


def psql_vars() -> dict[str, str]:
    """psql -v 로 넘기던 변수들."""
    return {
        "app_pw": os.environ.get("APP_RW_PASSWORD", ""),
        "healing_pw": os.environ.get("HEALING_RW_PASSWORD", ""),
    }


def substitute(sql: str, variables: dict[str, str]) -> str:
    """psql 의 :'var' 치환. 값이 비어 있으면 그 파일을 돌리지 않는다."""
    for name, value in variables.items():
        token = f":'{name}'"
        if token in sql:
            if not value:
                raise SystemExit(f"[중단] 환경변수가 비어 있다: {name} -> .env 를 채운다")
            sql = sql.replace(token, "'" + value.replace("'", "''") + "'")
    return sql


def summarize(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_schema, COUNT(*)
              FROM information_schema.tables
             WHERE table_schema IN ('app','healing','langgraph') AND table_type = 'BASE TABLE'
             GROUP BY table_schema ORDER BY 1
            """
        )
        print("  테이블 :", dict(cur.fetchall()) or "없음")

        cur.execute(
            """
            SELECT table_schema, COUNT(*) FROM information_schema.views
             WHERE table_schema IN ('app','healing') GROUP BY table_schema ORDER BY 1
            """
        )
        print("  뷰     :", dict(cur.fetchall()) or "없음")

        cur.execute(
            """
            SELECT COUNT(*) FROM pg_indexes
             WHERE schemaname IN ('app','healing')
            """
        )
        print("  인덱스 :", cur.fetchone()[0], "개 (PK · UNIQUE 제약 포함)")

        cur.execute("SELECT rolname FROM pg_roles WHERE rolname IN ('app_rw','healing_rw') ORDER BY 1")
        print("  롤     :", [r[0] for r in cur.fetchall()] or "없음")


def main() -> None:
    load_dotenv(ENV_PATH)
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit(f"[중단] DATABASE_URL 이 비어 있다: {ENV_PATH}")

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    check_only = "--check" in sys.argv
    files = args or DEFAULT_ORDER

    with psycopg.connect(url, connect_timeout=15, autocommit=True) as conn:
        if check_only:
            print("현재 상태")
            summarize(conn)
            return

        variables = psql_vars()
        for name in files:
            path = SQL_DIR / name
            if not path.exists():
                print(f"[건너뜀] {name} — 파일이 없다")
                continue
            sql = substitute(path.read_text(encoding="utf-8"), variables)
            with conn.cursor() as cur:
                cur.execute(sql)
            print(f"[완료] {name}")

        print("\n실행 후 상태")
        summarize(conn)


if __name__ == "__main__":
    main()
