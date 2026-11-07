"""환경 설정 — .env 를 한 곳에서 읽는다.

load_dotenv 의 경로를 명시한다. 인자 없이 쓰면 **실행 위치**를 기준으로 찾아서
backend/ 에서 띄울 때는 되고 루트에서 띄울 때는 조용히 빈 값이 된다.
"""

import os
import pathlib
import re
import urllib.parse

from dotenv import load_dotenv

BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").lower() in ("1", "true", "yes")


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

APP_RW_PASSWORD = os.getenv("APP_RW_PASSWORD", "").strip()
HEALING_RW_PASSWORD = os.getenv("HEALING_RW_PASSWORD", "").strip()

# 생성 LLM — provider 분기는 app/llm.py 한 곳에만 둔다 (CLAUDE.md 원칙 ③ · D-03 3.1)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").strip().lower()
LLM_MODEL = (os.getenv("LLM_MODEL") or "gpt-4o-mini-2024-07-18").strip()
# 대체 주문 해석 서비스가 쓰는 모델 (FR-016 2계통 — 1차와 다른 계열로 둔다)
LLM_MODEL_BACKUP = os.getenv("LLM_MODEL_BACKUP", "").strip() or LLM_MODEL
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "2048"))
# 추론형 모델(gpt-5 · o 계열)의 추론 강도. 실측(2026-11-03): none 0.9초 · low 1.1초 — 높이면 5초 제한(DEP_TIMEOUT)에 다가간다
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "none").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")

# 주문 해석 서비스 2계통 (D-02 DEP-001 · D-03 4절) — 비우면 /health 가 "unset" 으로 본다
ORDER_INTERPRETER_PRIMARY_URL = os.getenv("ORDER_INTERPRETER_PRIMARY_URL", "").strip().rstrip("/")
ORDER_INTERPRETER_BACKUP_URL = os.getenv("ORDER_INTERPRETER_BACKUP_URL", "").strip().rstrip("/")
ORDER_INTERPRETER_TIMEOUT_SEC = float(os.getenv("ORDER_INTERPRETER_TIMEOUT_SEC") or "5")  # PRD 5.6

# 로그 파일 위치 (D-03 1.5 — service.log 전체 · errors.log 는 ERROR 만)
LOG_DIR = pathlib.Path(os.getenv("LOG_DIR") or str(BACKEND_DIR / "logs"))   # 빈 값도 기본값

# 주문 해석이 LLM 을 실제로 부르는가. 지금은 규칙 기반(intake.extract_events)이라 False 다.
# D-04 에서 llm.py 를 붙일 때 True 로 바꾼다 — 그때부터 키가 없으면 /health 가 down(503)이다
LLM_REQUIRED = _bool("LLM_REQUIRED")


def llm_key_present() -> bool:
    """provider 에 필요한 키가 있는가. 로컬(ollama)은 키가 없다."""
    return {"openai": bool(OPENAI_API_KEY),
            "anthropic": bool(ANTHROPIC_API_KEY)}.get(LLM_PROVIDER, True)

ENABLE_DEBUG_API = _bool("ENABLE_DEBUG_API")
ENVIRONMENT = os.getenv("ENVIRONMENT", "lab").strip()

# 빈 값(`OPS_DASHBOARD_PASSWORD=`)도 기본값으로 본다 — 빈 문자열이면 아무도 로그인하지 못한다.
# 기본값은 **실습 환경(lab)에서만** 쓴다(PRD R-17 · v1.9). 그 밖의 환경에서 값이 없으면 로그인을 막는다.
# 기본값을 쓰는 중이면 기동 때 경고를 남긴다(app/main.py)
_OPS_PASSWORD_ENV = (os.getenv("OPS_DASHBOARD_PASSWORD") or "").strip()
# 코드에 대체 기본값을 두지 않는다 — PRD R-17 이 "배포 전 제거하거나 .env 설정을 필수로 한다"로
# 정해 두었고, 2026-11-07 QA Q-07 이 lab 에서 짧은 고정 문자열로 로그인되는 것을 확인했다.
# 운영 대시보드는 아이디 없이 비밀번호 하나로 들어가므로 그 문자열이 유일한 문이었다
OPS_PASSWORD_MISSING = not _OPS_PASSWORD_ENV
OPS_DASHBOARD_PASSWORD = _OPS_PASSWORD_ENV

# D-07 7.2 연결 규약 — 값이 코드와 PRD 5.6.1 판정에서 같아야 한다
CONNECT_TIMEOUT_SEC = 2
STATEMENT_TIMEOUT_MS = 2000

# D-07 8절 · PRD U-13 — 메뉴 기준일. 시드(sql/11 · 12)의 menu_version 과 같은 값이어야 한다
MENU_VERSION = os.getenv("MENU_VERSION", "mv_20261029")
MENU_AS_OF = os.getenv("MENU_AS_OF", "2026-10-29")

# 비용·폭주 방어 — 협상 불가 3종 (CLAUDE.md · D-12 7절). 세 번째(지출 한도)는 OpenAI 콘솔에 있다
GRAPH_RECURSION_LIMIT = 25    # 그래프 순회 상한 (LangGraph recursion_limit · D-12 7절)
MAX_REASK = 3                 # 되묻기 상한 — 항목당 확인 질문 수 (edits._follow_up)

# D-12 7절 보조 상한 · 3.1 묶음 결정
MAX_TOOL_ROUNDS = 3           # 전문가 루프 툴 왕복 상한 (D-10 5절 · 교안코드규약 4.7)
MAX_REGENERATE = 2            # 계약 위반 · 누락 시 묶음 재생성 (PRD 5.6 F-5)
MAX_LLM_CALLS_PER_RUN = 300   # 분석 1회 LLM 호출 상한 — 넘는 묶음은 규칙 해석
BATCH_SIZE = 4                # 묶음 메시지 수 — 1차 서비스 (D-12 3.1 · 7.1 실측)
BATCH_SIZE_BACKUP = 2         # 대체 서비스(gpt-4o-mini)는 4건 묶음이 3.6 ~ 4.7초라 5초 제한에 걸린다 (D-12 10.2)
BATCH_CONTEXT = 4             # 묶음마다 붙이는 앞 메시지 수
PERSON_CONTEXT = 12           # 묶음마다 붙이는 "그 사람이 한 다른 말" 상한 (2026-11-05 보고 7번)
BATCH_CONCURRENCY = 4         # 묶음 병렬 수 (429 회피)
MENU_AGENT_MAX_ITEMS = 20      # 분석 1회에 Menu Agent 로 보내는 add 이벤트 상한 (비용 · 지연)
REVIEW_AGENT_MAX_ITEMS = 10    # 분석 1회에 보조 검토로 보내는 항목 상한
SLOW_NOTICE_SEC = 15          # 진행이 이만큼 멈추면 "조금 더 걸리고 있어요" (P-06 상태 표)
MAX_ITEM_QUANTITY = 20        # 한 사람 한 줄의 잔 수가 이보다 크면 확인 질문 (0 · 음수도)
MAX_MESSAGES = 500            # 한 번에 받는 메시지 수 (API-002 limits)
MAX_INPUT_BYTES = 1_048_576   # 1MB


def role_url(role: str) -> str:
    """app_rw · healing_rw 접속 문자열을 소유자 URL 에서 파생한다 (D-07 2.1).

    Supabase pooler 는 사용자명에 프로젝트 ref 를 붙인다: <롤>.<ref>
    롤 비밀번호가 없으면 소유자 URL 을 그대로 쓴다 — 권한 분리는 약해지지만
    기동은 된다(D-16 에 적는다).
    """
    password = {"app_rw": APP_RW_PASSWORD, "healing_rw": HEALING_RW_PASSWORD}.get(role, "")
    if not DATABASE_URL or not password:
        return DATABASE_URL

    match = re.match(r"^(postgresql|postgres)://([^:]+):[^@]*@(.+)$", DATABASE_URL)
    if not match:
        return DATABASE_URL
    scheme, owner_user, host_part = match.groups()

    # postgres.<ref> -> <role>.<ref> / 평범한 postgres -> <role>
    suffix = owner_user.split(".", 1)[1] if "." in owner_user else ""
    user = f"{role}.{suffix}" if suffix else role
    return f"{scheme}://{user}:{urllib.parse.quote(password, safe='')}@{host_part}"


def db_configured() -> bool:
    return bool(DATABASE_URL)
