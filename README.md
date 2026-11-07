# Moa — OrderOps AI Auto Healing

단톡방 대화를 붙여넣으면 누가 무엇을 시켰는지 사람별로 정리해 주는 서비스입니다.
FastAPI 에이전트 서버가 분석을 하고, Streamlit 화면이 입력 · 정리 · 주문서 · 운영 대시보드를 보여줍니다.

## 폴더 구조

```
2026_aio2_moa/
├── backend/                 # FastAPI (API 서버, 8000 포트)
│   ├── app/
│   │   ├── main.py          # 앱 생성 · trace_id · 오류 포장 · 라우터 등록
│   │   ├── api/             # HTTP 층 — D-02 엔드포인트 31개 (인증 · 입력 검사 · 포장)
│   │   └── services/        # 업무 로직 — 매장 판별 · 해석 · 메뉴 매칭 · 검증 · 정산 · 사건
│   ├── sql/                 # 스키마 · 시드 · run_sql.py (DB 초기화)
│   ├── tools/seed_demo.py   # 시연 세션 3건
│   ├── tests/
│   ├── requirements.txt     # 운영용
│   ├── requirements-dev.txt # 테스트용 (pytest 등)
│   └── Dockerfile
├── frontend/                # Streamlit (화면, 8501 포트)
│   ├── app.py               # 페이지 등록만 한다 (st.navigation)
│   ├── api_client.py        # 에이전트 서버 호출 (D-02 API스펙 · BACKEND_URL 환경변수)
│   ├── screens/             # 화면 4종 — start · review · order · ops
│   ├── moa/                 # 토큰 · 문구 · 레이아웃 · 컴포넌트 · 대화상자 · 상태 · 모의 응답
│   ├── .streamlit/          # 테마 (D-01 토큰)
│   ├── tests/
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   └── Dockerfile
├── docker-compose.core.yml  # 공용 스택 — gateway(8501, 유일한 공개 포트) · db · 해석 서비스 2계통 · socket-proxy · healing
├── docker-compose.app.yml   # 앱 스택 — frontend · backend. -p moa-a · -p moa-b 로 두 번 띄운다 (PRD 5.4)
├── docker-compose.*.dev.yml # 로컬 개발용 덧씌우기 — 127.0.0.1 에만 포트를 연다
├── gateway/haproxy.cfg      # 게이트웨이 — A/B 분산 · 쿠키 고정 · backend /health 로 제외 · 재등록
├── socket-proxy/haproxy.cfg # Docker API 관문 — 조회 + 허용목록 컨테이너 재시작만
├── scripts/                 # init-networks.sh(네트워크) · deploy.sh(순차 배포) · db-backup.sh(DB 백업 · 복원)
│                            #   dep-test.sh(이중화 시험) · fault.sh(F-3 주입) · server-check.sh(서버 점검)
└── .github/workflows/ci.yml # CI: 테스트 + 이미지 빌드
```

프론트엔드는 백엔드 코드를 import하지 않고 HTTP로만 통신합니다. 호출 계약은 `03.구현문서/API스펙.md`(D-02)입니다.
`backend/` 폴더만 따로 받아도 실행 · 테스트 · DB 초기화가 됩니다 — 방법은 [backend/README.md](backend/README.md)에 있습니다.

## 화면 (P-06 화면설계서 v3.0)

정본 시안은 `Moa_화면설계_v3.0_간소화_변경.svg`(23케이스)입니다.

| URL | 화면 | 내용 |
|-----|------|------|
| `/` | SCR-001 주문 시작 | 서비스 소개 대화 · 예시 채우기 · `.txt` 올리기 · 대화 미리보기 · [정리하기] · 매장 되묻기 모달 |
| `/s?s={관리 토큰}` | SCR-002 주문 정리 | 왼쪽 주문서 패널(400px) + 오른쪽 채팅 · 진행 4단계 · 확인 질문(한 번에 하나) · 채팅 수정 · 행 편집 |
| `/order?s={관리 토큰}` | SCR-005 주문서 | 묶음 목록 · 이름 눌러 바꾸기 · 답장 반영 · 공유하기(= 완료 처리) |
| `/ops` | SCR-006 운영 대시보드 | O-0 비밀번호 · 승인 필요 · 지금 상태 · 사건 · 지표 · 주문 처리 기록 · 장애 주입 |

세션 관리 토큰은 **쿼리 파라미터로 받아 `X-Session-Token` 헤더로** 보냅니다. API 경로에 넣지 않습니다(D-02 1.1).

### 화면 미리보기 모드 (`MOA_UI_PREVIEW`)

**기본은 꺼짐입니다.** 켜면 에이전트 서버가 응답하지 않는 구간을 예시 데이터로 채워 서버 없이 화면을 볼 수 있습니다.
예시로 채운 구간은 화면 맨 아래 한 줄로 알립니다. 서버와 함께 쓸 때는 켜지 않습니다 — 켜 두면 세션을 잃었을 때(새로고침 ·
화면 재시작) 오류 대신 예시 주문서가 그려져 가짜 데이터로 답하고 공유하게 됩니다(2026-11-04).

```bash
MOA_UI_PREVIEW=1   # 켜기 — 서버 없이 화면만 볼 때. 화면 테스트(pytest)는 frontend/conftest.py 가 켠다
```

예시 데이터는 `frontend/moa/mock.py` 한 곳에 있습니다. 값은 전부 정본 시안의 예시값입니다.

## 실행 방법

### 방법 1: Docker Compose (권장)

Docker Desktop을 켠 뒤 프로젝트 루트에서 실행합니다. 공용 스택 1벌 + 앱 스택 2벌(A · B)이고, 게이트웨이가 8501 하나로 A · B 에 나눠 보냅니다(PRD 5.4).
루트 `.env` 에 `POSTGRES_PASSWORD` · `HEALING_RW_PASSWORD`, `backend/.env` 에 LLM 키 등 앱 비밀값을 둡니다(각각 `.env.example` · `backend/.env.example` 을 복사해 채운다).

```bash
sh scripts/init-networks.sh                                                         # 처음 한 번: 네트워크 3개
docker compose -f docker-compose.core.yml -p moa-core up -d --build                 # 공용 스택을 먼저
docker compose -f docker-compose.app.yml  -p moa-a    run --rm backend python sql/run_sql.py   # 처음 한 번: 롤 · 스키마 · 시드
docker compose -f docker-compose.app.yml  -p moa-a    up -d                         # 앱 복제본 A
docker compose -f docker-compose.app.yml  -p moa-b    up -d                         # 앱 복제본 B
```

- 화면: http://localhost:8501 (게이트웨이)
- 개발용으로 각 스택 · DB · 게이트웨이 상태 페이지를 직접 열려면 덧씌우기를 더한다 (127.0.0.1 에만 열린다)
  - `docker compose -f docker-compose.core.yml -f docker-compose.core.dev.yml -p moa-core up -d` → DB `5434` · 상태 페이지 http://localhost:8404/stats
  - `DEV_FRONT_PORT=8511 DEV_BACK_PORT=8001 docker compose -f docker-compose.app.yml -f docker-compose.app.dev.yml -p moa-a up -d` → A 화면 `8511` · API http://localhost:8001/docs (B 는 8512 · 8002)
- 주문 해석 서비스는 기본이 **LLM 해석**(`INTERPRETER_SOURCE=llm`)이다. 같은 입력에 같은 결과가 필요하면(장애 실험 반복) 공용 스택을 `INTERPRETER_SOURCE=rules` 로 띄운다
- 공용 스택을 먼저 띄운다 — compose 의 `depends_on` 은 프로젝트를 넘지 못한다. 정리는 앱 스택 · 공용 스택 순서로 `down` (**`-v` 금지** — DB 볼륨 `moa_pgdata` 가 지워진다)

### 방법 2: 직접 실행

[uv](https://docs.astral.sh/uv/)로 가상환경을 만들고 패키지를 설치합니다.

**1. 가상환경 만들기** — 프로젝트 루트에서 한 번만 실행합니다. **환경은 두 개입니다.**

```bash
uv venv -p 3.11                      # .venv          — backend (FastAPI)
uv venv -p 3.11 .venv-frontend       # .venv-frontend — frontend (Streamlit)
```

**왜 하나가 아닌가** — Streamlit 1.66 은 `starlette>=0.46` 을, FastAPI 0.115.6 은 `starlette<0.42` 를 요구합니다.
한 환경에 함께 설치하면 **나중에 설치한 쪽이 다른 쪽을 깨뜨립니다**(backend 는 `Router.__init__()` TypeError,
frontend 는 `DEFAULT_EXCLUDED_CONTENT_TYPES` ImportError). 둘을 한 환경에 넣으려면 FastAPI 를 올려야 하고,
그것은 D-03 1절 확정 스택 변경입니다.

**2. 패키지 설치** — 각 폴더에서, 그 폴더의 환경을 지정해 실행합니다.

```bash
cd backend
uv pip install --python ../.venv/Scripts/python.exe -r requirements-dev.txt
```

```bash
cd frontend
uv pip install --python ../.venv-frontend/Scripts/python.exe -r requirements-dev.txt
```

macOS · Linux 는 `../.venv/bin/python` 입니다.

`requirements-dev.txt`가 첫 줄(`-r requirements.txt`)에서 운영용 패키지까지 함께 설치하므로 `requirements.txt`는 따로 설치하지 않습니다.

**3. 서버 실행** — 터미널 두 개에서 각각 실행합니다.

```bash
cd backend
../.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
```

```bash
cd frontend
../.venv-frontend/Scripts/python.exe -m streamlit run app.py
```

`uv run` 은 상위 폴더의 `.venv` 를 집으므로 frontend 에서는 쓰지 않습니다 — 위처럼 환경의 파이썬을 직접 부릅니다.

프론트엔드는 기본으로 `http://localhost:8000`에 접속합니다. 다른 주소를 쓰려면 `BACKEND_URL` 환경변수를 지정합니다.

### 방법 3: EC2 서버 (ECR 이미지)

서버는 빌드하지 않고 ECR 에서 이미지를 받는다. 로컬과 같은 compose 파일을 쓰고, 이미지 주소만 `.env` 의 `REGISTRY` 로 바꾼다.

**이미지 올리기** (내 PC, PowerShell) — `docker compose -f docker-compose.app.yml -p moa-a build` 후 `moa-backend:latest` · `moa-frontend:latest` 를 ECR 에 push 한다.

```powershell
$AWS_REGION = "ap-northeast-2"
$AWS_ACCOUNT_ID = "<계정 ID>"
$ECR = "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR

foreach ($REPO in "moa-backend", "moa-frontend") {
    aws ecr describe-repositories --repository-names $REPO --region $AWS_REGION *> $null
    if ($LASTEXITCODE -ne 0) {
        aws ecr create-repository --repository-name $REPO --region $AWS_REGION
    }
    docker tag "${REPO}:latest" "$ECR/${REPO}:latest"
    docker push "$ECR/${REPO}:latest"
}
```

**서버에 둘 파일** (`~/moa`) — 소스 코드는 두지 않는다. compose · 설정 · 스크립트는 CI 가 매 배포마다 복사한다.

```
~/moa/
├── docker-compose.core.yml · docker-compose.app.yml
├── gateway/haproxy.cfg · socket-proxy/haproxy.cfg
├── scripts/                 # CI 가 배포마다 복사한다
├── logs/<컨테이너>/        # 앱 · 해석 서비스 · healing 로그 (14일 보관)
├── backups/                # DB 백업 (배포마다 자동 · 최근 14개)
├── .env            # POSTGRES_PASSWORD · HEALING_RW_PASSWORD · REGISTRY=<계정 ID>.dkr.ecr.ap-northeast-2.amazonaws.com/ (끝의 / 포함)
└── backend/.env    # backend/.env.example 을 채운 것
```

**서버에서 실행** — EC2 에 ECR 읽기 IAM 역할이 붙어 있어야 한다(액세스 키를 두지 않는다).

```bash
cd ~/moa
aws ecr get-login-password --region ap-northeast-2 | docker login --username AWS --password-stdin <계정 ID>.dkr.ecr.ap-northeast-2.amazonaws.com
bash scripts/deploy.sh                                                        # 공용 → 해석 서비스 → 앱 A → (A UP 확인) → 앱 B
```

- 새 서버(빈 DB)라면 먼저 DB 를 초기화한 뒤 `deploy.sh` 를 돌린다 — 초기화 전에는 backend 가 `unhealthy` 라 A 교체에서 멈춘다.
  **이미지를 먼저 받는다** — 이미지가 없을 때 `run` 은 빌드를 시도하는데 서버에는 소스(`./backend`)가 없어 실패한다 (D-03 7.4)

```bash
sh scripts/init-networks.sh
docker compose -f docker-compose.core.yml -p moa-core pull
docker compose -f docker-compose.core.yml -p moa-core up -d db
docker compose -f docker-compose.app.yml -p moa-a pull
docker compose -f docker-compose.app.yml -p moa-a run --rm backend python sql/run_sql.py
bash scripts/deploy.sh
```

- 처음 배포 전 점검: `bash scripts/server-check.sh` (읽기만 한다 — 메모리 · compose 기능 · `.env` 키)
- **호스트를 재부팅했으면 `bash scripts/deploy.sh` 를 한 번** 돌린다 — `restart: "no"` 인 backend · interpreter-primary 는 자동으로 뜨지 않는다
- DB 백업: `deploy.sh` 가 배포마다 먼저 `~/moa/backups/` 에 남긴다(최근 14개). 직접: `bash scripts/db-backup.sh` · 목록 `list` · 복원 `restore <파일>`

- `deploy.sh` 는 두 앱 스택을 동시에 내리지 않는다. 교체할 스택을 게이트웨이에서 먼저 빼고, 교체 뒤 UP 이 확인돼야 다음 스택으로 간다. 배포 동안 healing 은 멈춘다
- 옛 단일 스택(프로젝트 `moa`)이 떠 있으면 `deploy.sh` 가 처음 한 번 컨테이너만 지운다 — DB 볼륨 `moa_pgdata` 는 그대로 이어 쓴다
- 이미지는 x86(amd64) 로 빌드된다. Arm(Graviton) 인스턴스에서는 뜨지 않는다

## 테스트

위의 직접 실행 1~2단계(가상환경, 패키지 설치)를 마친 뒤 각 폴더에서 실행합니다. CI도 같은 `pytest` 명령을 사용합니다.

```bash
cd backend
../.venv/Scripts/python.exe -m pytest -q                   # 계약 테스트 (DB 없이)
MOA_LIVE_DB=1 ../.venv/Scripts/python.exe -m pytest -q --cov=app --cov-fail-under=75   # .env 의 DB 로 전체 흐름까지
```

```bash
cd frontend
../.venv-frontend/Scripts/python.exe -m pytest -q --cov=api_client --cov-fail-under=85
```

## CI

`main`, `dev` 브랜치에 push하거나 이 브랜치로 PR을 만들면 GitHub Actions가 실행됩니다.

1. `test-backend`, `test-frontend`: 각 폴더의 테스트 (동시에 실행). 백엔드는 일회용 PostgreSQL에 스키마를 올려 전체 흐름까지 돌린다
2. `build`: 두 테스트가 통과하면 백엔드·프론트엔드 Docker 이미지를 빌드
3. `deploy`: **`main` 에 push 될 때만** 실행된다(dev push · PR 은 2단계에서 끝난다). 이미지를 커밋 sha 태그 + `latest` 로 ECR 에 올리고, SSH 로 EC2 의 `~/moa` 에 compose · 게이트웨이 · 소켓 프록시 설정 · 스크립트를 복사한 뒤 `scripts/deploy.sh` 로 순차 교체한다(A → 게이트웨이에서 A UP 확인 → B). 마지막에 게이트웨이(8501) 응답을 확인하고, 실패하면 컨테이너 상태 · 로그를 남긴다

`deploy` 에 필요한 GitHub Secrets:

| 이름 | 값 |
| --- | --- |
| `AWS_ACCESS_KEY_ID` · `AWS_SECRET_ACCESS_KEY` | ECR push 권한이 있는 IAM 사용자 키 |
| `EC2_HOST` | EC2 퍼블릭 IP 또는 DNS |
| `ECR_REGISTRY` | `<계정 ID>.dkr.ecr.ap-northeast-2.amazonaws.com` (끝에 / 없음) |
| `EC2_SSH_KEY` | 키 페어 `.pem` 파일 내용 전체 |
| `EC2_SG_ID` | EC2 보안 그룹 ID (`sg-...`) — 배포 동안만 러너 IP 를 22번에 허용하고 끝나면 지운다 |

SSH 사용자는 기본 `ubuntu` 다. Amazon Linux 등 다른 사용자면 Secrets 가 아니라 **Variables** 에 `EC2_USER` 를 둔다.

보안 그룹의 22번은 평소처럼 내 IP 만 열어 두면 된다 — 러너 IP 는 워크플로가 열고 닫는다. 그래서 위 IAM 사용자에 `ec2:AuthorizeSecurityGroupIngress` · `ec2:RevokeSecurityGroupIngress` 권한이 있어야 한다. 서버 준비(`.env` · `backend/.env` · DB 초기화)는 위 "방법 3" 을 처음 한 번 해 둔다.
