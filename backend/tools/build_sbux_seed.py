"""스타벅스 메뉴 시드 생성기 — 스타벅스_메뉴별_옵션정리.xlsx → sql/11_seed_menu_sbux.sql

    python tools/build_sbux_seed.py ../../스타벅스_메뉴별_옵션정리.xlsx      (backend/ 에서 실행)

규칙 (2026-11-06 개정 — 엑셀이 유일한 출처)
  - 엑셀에서 가격이 있는 메뉴만 넣는다. 엑셀에 없거나 가격이 없는 메뉴는 넣지 않는다(가안 가격 · 수기 메뉴 없음).
  - 기존 스타벅스 시드는 지우고 다시 넣는다. 별칭 · 옵션은 전부 지운 뒤 다시 넣고, 메뉴는 엑셀에 없는 것을
    지운다. 단 주문 이력(order_item, FK ON DELETE RESTRICT)이 참조하는 메뉴는 지울 수 없으므로 비활성으로 돌린다.
  - 사이즈 가격 (2026-11-06 사용자 결정): Tall 가격이 있으면 Tall 기준으로 만든다 —
        Short = Tall - 800 · Grande = Tall + 800 · Venti = Tall + 1,600
    어느 사이즈가 있는지는 '사이즈' 열을 따른다('Short는 HOT만' 이면 아이스 메뉴에 Short 없음).
    Tall 가격이 없으면(그란데 단일 등) 엑셀 가격을 그대로 쓴다.
  - base_price 는 기본 사이즈 가격, 다른 사이즈는 그 차액이 size 옵션 extra_price 다(Short 는 음수).
  - 가격 출처 순서: 'Tall 가격(원)' 열(D열 — 입력값) → '단일 상품 가격(원)' → 사이즈별 가격 열 → '가격·메모' 글자.
    열은 머리글 이름으로 찾는다. 빈칸 · '#REF!' 는 가격 없음이다.
    '사이즈' 열에 Short/Tall/Grande/Venti 가 없으면(Solo·Doppio · 선택 없음 · 단일 용량) 사이즈 옵션 없이 한 가격이다.
    메뉴로 넣는 기준은 'Tall 가격(원)' 또는 '단일 상품 가격(원)' 값이 있는 행이다 — 메모에만 가격이 있는
    행(제주 그란데 전용 5종)은 넣지 않는다(2026-11-06 사용자 결정). 엑셀 값 보정은 OVERRIDES 에 둔다.
  - 옵션은 5종만 DB 로 관리한다: size · ice · shot · bean · cup. 그 밖의 요청은 기타 요청이다.
  - 메뉴 ID: 기존 ID(sb_0NN)가 있는 상품코드는 그 ID 를 유지한다(LEGACY — 평가셋 · 별칭이 쓴다).
    새 메뉴는 sb_c{상품코드}, 상품코드가 없으면 sb_n{이름 해시}. 다시 돌려도 같은 ID 가 나온다.
  - 같은 이름이 두 행이면(기본 사이즈만 다른 변형) 기존 ID 행 > 기본 사이즈가 Tall 인 행을 쓴다.
  - 별칭은 sql/seed_src/sbux_alias.csv 에서 읽는다. 시드에 없는 메뉴를 가리키는 별칭은 빼고 경고한다.
"""

import csv
import hashlib
import pathlib
import re
import sys

import openpyxl

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "sql" / "11_seed_menu_sbux.sql"
ALIAS_CSV = ROOT / "sql" / "seed_src" / "sbux_alias.csv"
MENU_VERSION = "mv_20261029"          # app/config.py MENU_VERSION 과 같은 값 — ID 를 유지하므로 판을 올리지 않는다

SIZES = [("short", "Short", "숏"), ("tall", "Tall", "톨"), ("grande", "Grande", "그란데"),
         ("venti", "Venti", "벤티")]
# Tall 기준 사이즈 차액 (2026-11-06 사용자 결정)
SIZE_DELTA = {"short": -800, "tall": 0, "grande": 800, "venti": 1600}
SHOT_EXTRA, DECAF_EXTRA, CUP_DISCOUNT = 600, 300, -400       # 엑셀 가격·메모 열의 값

# 기존 ID → 엑셀 상품코드. name 이 있으면 그 이름을 쓴다(사용자가 부르는 이름 · 온도 짝 유지).
# 엑셀에서 가격이 없는 메뉴(sb_017 · sb_018 · sb_024 · sb_027)는 시드에 들어가지 않지만,
# 나중에 엑셀에 가격이 채워지면 같은 ID 로 돌아오도록 대응은 남겨 둔다.
LEGACY = {
    "sb_004": {"code": "110563", "name": "아이스 아메리카노"},   # 엑셀 '아이스 카페 아메리카노'
    "sb_011": {"code": "94"}, "sb_003": {"code": "110569"}, "sb_012": {"code": "41"},
    "sb_006": {"code": "9200000006346"}, "sb_013": {"code": "9200000006343"},
    "sb_014": {"code": "110566"}, "sb_015": {"code": "110582"},
    "sb_016": {"code": "9200000000038"}, "sb_017": {"code": "9200000002081"},
    "sb_018": {"code": "20"}, "sb_002": {"code": "168016"}, "sb_019": {"code": "168010"},
    "sb_020": {"code": "168066"}, "sb_021": {"code": "9200000003276"},
    "sb_005": {"code": "9200000002956"}, "sb_023": {"code": "4004000000039"},
    "sb_024": {"code": "135612"}, "sb_025": {"code": "110621"},     # 아이스 초콜릿 → 아이스 시그니처 초콜릿
    "sb_026": {"code": "9200000002496"},                          # 그린 티 라떼 → 제주 말차 라떼
    "sb_027": {"code": "18"},
}
# 엑셀 값을 바로잡는 보정(상품코드 → 덮어쓸 값). 엑셀이 고쳐지면 이 항목을 지운다 — 같아지면 경고가 뜬다.
#   169001 망고 바나나 블렌디드 — 벤티 단일 6,600원 (2026-11-06 사용자 확인). 엑셀은 Grande · Tall 칸 6,600
#          → Tall 기준 규칙(Venti = Tall + 1,600)에 맞춰 Tall 5,000 으로 넣는다
OVERRIDES = {
    "169001": {"size_text": "Venti(591ml)", "tall": 6600 - SIZE_DELTA["venti"]},
}
# 이름이 같은 변형 중 버리는 행(상품코드) — 가격이 같고 기본 사이즈만 다르다
SKIP_CODES = {"9200000004770", "9200000004753", "9200000005310", "9200000004769",
              "9200000005516"}   # 마지막: 리저브 '아이스 아메리카노' — sb_004 와 이름 · 가격이 같다

# 기존 메뉴의 특성 태그는 그대로 둔다
LEGACY_TAGS = {
    "sb_004": [], "sb_011": ["따뜻한"], "sb_003": ["고소한"], "sb_012": ["고소한", "따뜻한"],
    "sb_006": ["단맛"], "sb_013": ["단맛", "따뜻한"], "sb_014": ["단맛"], "sb_015": ["단맛"],
    "sb_016": [], "sb_017": ["단맛"], "sb_018": [],
    "sb_002": ["단맛", "초콜릿"], "sb_019": ["단맛"], "sb_020": ["단맛", "초콜릿", "카페인없음"],
    "sb_021": ["단맛", "과일", "카페인없음"], "sb_005": ["티", "과일", "카페인없음"],
    "sb_023": ["티"], "sb_024": ["티", "단맛"], "sb_025": ["단맛", "초콜릿", "카페인없음"],
    "sb_026": ["티", "단맛"], "sb_027": ["카페인없음"],
}
_SWEET = ("초콜릿", "카라멜", "바닐라", "모카", "돌체", "크림", "꿀", "프라푸치노", "블렌디드", "요거트",
          "밀크", "스위트")
_FRUIT = ("망고", "딸기", "자몽", "유자", "라임", "피치", "복숭아", "체리", "레몬", "아사이", "패션")
_NO_CAFFEINE = ("캐모마일", "히비스커스", "민트 블렌드", "유스베리", "요거트", "망고", "초콜릿 크림 칩")
_HOT_CATEGORIES = ("에스프레소", "브루드 커피", "티", "기타 제조 음료")


def tags_for(name: str, category: str) -> list[str]:
    tags = []
    if any(w in name for w in _SWEET):
        tags.append("단맛")
    if "초콜릿" in name or "모카" in name or "자바 칩" in name:
        tags.append("초콜릿")
    if any(w in name for w in _FRUIT):
        tags.append("과일")
    if category == "티":
        tags.append("티")
    if any(w in name for w in _NO_CAFFEINE):
        tags.append("카페인없음")
    if category in _HOT_CATEGORIES and "아이스" not in name and "콜드" not in name:
        tags.append("따뜻한")
    return tags


def default_size(size_text: str, available: list[str]) -> str:
    for value, word, _ in SIZES:
        if value in available and re.search(rf"{word}\([^)]*기본\)", size_text or ""):
            return value
    return "tall" if "tall" in available else available[0]


def _won(value) -> int | None:
    """가격 셀 값 → 정수. 빈칸 · '#REF!' 같은 수식 오류 · 문자열은 가격 없음(None)이다."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").replace(",", "").replace("원", "").strip()
    return int(text) if text.isdigit() else None


_MEMO_SIZE = re.compile(r"\b(Short|Tall|Grande|Venti)\s+(\d{1,2},\d{3}|\d{4,5})원")
_MEMO_SINGLE = re.compile(r"(?:사진 가격|단일 가격|가격)\s+(\d{1,2},\d{3}|\d{4,5})원")


def memo_prices(memo: str) -> tuple[dict[str, int], int | None]:
    """'가격·메모' 열의 글자에서 가격을 읽는다.

    'Tall 5,200원 / Grande 5,800원' → 사이즈별 가격.  '사진 가격 4,700원' → 단일 상품 가격.
    '메뉴 가격 미확인' · 'Short = Tall - 800원' 같은 계산식 메모는 가격으로 보지 않는다.
    """
    words = {w: v for v, w, _ in SIZES}
    sizes = {words[w]: int(p.replace(",", "")) for w, p in _MEMO_SIZE.findall(memo)}
    single = None
    if not sizes:
        m = _MEMO_SINGLE.search(memo)
        single = int(m.group(1).replace(",", "")) if m else None
    return sizes, single


def offered_sizes(name: str, size_text: str) -> list[str]:
    """'사이즈' 열에 적힌 사이즈. 'Short는 HOT만' 이면 아이스 메뉴에서 Short 를 뺀다."""
    found = [v for v, word, _ in SIZES if re.search(rf"\b{word}\b", size_text or "")]
    if "short" in found and "HOT만" in (size_text or "") and "아이스" in name:
        found.remove("short")
    return found


def tall_based(tall: int, sizes: list[str]) -> dict[str, int]:
    """Tall 가격 기준으로 '사이즈' 열에 있는 사이즈 가격만 만든다 (Short -800 · Grande +800 · Venti +1,600).

    Tall 이 없는 메뉴(코르타도 = Short 전용 · 망고 바나나 블렌디드 = Grande 전용)도 Tall 가격 기준으로 계산한다.
    """
    return {v: tall + SIZE_DELTA[v] for v, _, _ in SIZES if v in sizes}


# 엑셀 열은 번호가 아니라 머리글로 찾는다 (2026-11-06 12:17 판에서 D열 'Tall 가격(원)' 이 끼어들어 열이 밀렸다)
COLS = {"category": "카테고리", "code": "상품코드", "name": "메뉴명", "size_text": "사이즈",
        "bean_shot": "원두·샷", "ice_cup": "얼음·컵·기타", "memo": "가격·메모",
        "single": "단일 상품 가격(원)"}


def _header_index(header: list) -> tuple[dict[str, int], list[int], dict[str, int]]:
    names = [str(h or "").strip() for h in header]
    missing = [h for h in COLS.values() if h not in names]
    if missing:
        raise SystemExit(f"[중단] 엑셀 머리글이 없다: {missing}")
    idx = {k: names.index(h) for k, h in COLS.items()}
    tall_cols = [i for i, h in enumerate(names) if h == "Tall 가격(원)"]      # 첫 열(D)이 입력, 뒤 열은 수식
    size_cols = {v: names.index(f"{w} 가격(원)") for v, w, _ in SIZES if f"{w} 가격(원)" in names}
    return idx, tall_cols, size_cols


def read_sheet(path: str) -> dict[str, dict]:
    ws = openpyxl.load_workbook(path, data_only=True).active
    idx, tall_cols, size_cols = _header_index([c.value for c in ws[1]])
    rows = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        cell = {k: r[i] if i < len(r) else None for k, i in idx.items()}
        if not cell["name"]:
            continue
        name = str(cell["name"]).strip()
        code = str(cell["code"]).strip() if cell["code"] not in (None, "") else ""
        # 가격 출처 순서: Tall 가격 열 → 단일 상품 가격 열 → 사이즈별 가격 열 → '가격·메모' 글자
        tall = next((v for v in (_won(r[i]) for i in tall_cols if i < len(r)) if v is not None), None)
        single = _won(cell["single"])
        prices = {}
        if tall is None and single is None:
            prices = {v: _won(r[i]) for v, i in size_cols.items() if i < len(r)}
            prices = {k: v for k, v in prices.items() if v is not None}
            if not prices:
                prices, single = memo_prices(str(cell["memo"] or ""))
            tall = prices.get("tall")
        key = code or "name:" + name          # 상품코드가 없는 행(에비앙 등)은 이름으로 구분한다
        rows[key] = {
            "category": cell["category"], "code": code, "name": name,
            "size_text": str(cell["size_text"] or ""), "bean_shot": str(cell["bean_shot"] or ""),
            "ice_cup": str(cell["ice_cup"] or ""), "tall": tall, "prices": prices, "single": single,
        }
    return rows


def new_menu_id(row: dict) -> str:
    """새 메뉴 ID — 상품코드가 있으면 sb_c{상품코드}, 없으면 이름 해시 sb_n{10자}. 다시 돌려도 같은 ID 다."""
    if row["code"]:
        return f"sb_c{row['code']}"
    return "sb_n" + hashlib.md5(row["name"].encode("utf-8")).hexdigest()[:10]


def groups_for(row: dict, prices: dict[str, int] | None = None) -> list[str]:
    prices = row["prices"] if prices is None else prices
    groups = []
    if prices and row["size_text"].strip() != "선택 없음":
        groups.append("size")
    # '얼음: 없이·적게·…' 형식만 얼음 옵션이다. 병음료의 '컵: 매장컵+얼음' 은 얼음 양 선택이 아니다
    if re.search(r"얼음\s*:", row["ice_cup"]):
        groups.append("ice")
    if "샷" in row["bean_shot"]:
        groups.append("shot")
    if "시그니처" in row["bean_shot"]:
        groups.append("bean")
    if "개인컵" in row["ice_cup"]:
        groups.append("cup")
    return groups


def build(path: str) -> tuple[list[dict], list[dict]]:
    sheet = read_sheet(path)
    legacy_codes = {v["code"]: mid for mid, v in LEGACY.items()}
    menus, skipped = [], []
    for row in sheet.values():
        code = row["code"]
        menu_id = legacy_codes.get(code)
        if code and code in SKIP_CODES and not menu_id:
            skipped.append({**row, "why": "같은 이름 변형"})
            continue
        if code in OVERRIDES:
            fix = OVERRIDES[code]
            if all(row.get(k) == v for k, v in fix.items()):
                print(f"[알림] 엑셀이 보정값과 같아졌다 — OVERRIDES['{code}'] 를 지운다")
            row = {**row, **fix}
        # 가격은 'Tall 가격(원)' · '단일 상품 가격(원)' 열에 있는 것만 인정한다 (2026-11-06 사용자 결정 —
        # 메모에만 가격이 있는 제주 그란데 전용 5종은 넣지 않는다)
        if row["tall"] is None and row["single"] is None:
            skipped.append({**row, "why": "가격 없음"})
            continue
        menu_id = menu_id or new_menu_id(row)
        name = LEGACY.get(menu_id, {}).get("name") or row["name"]
        prices, single = dict(row["prices"]), row["single"]
        if row["tall"] is not None:
            sizes = offered_sizes(name, row["size_text"])
            if sizes:
                prices = tall_based(row["tall"], sizes)
            else:                               # Solo/Doppio · 선택 없음 · 단일 용량 — 사이즈 옵션 없이 한 가격
                prices, single = {}, row["tall"]
        if prices:
            available = [v for v, _, _ in SIZES if v in prices]
            chosen = default_size(row["size_text"], available)
            base = prices[chosen]
        else:                                   # 단일 상품(병음료 · 생수) — 사이즈 옵션이 없다
            available, chosen, base = [], "", single
        menus.append({
            "menu_id": menu_id, "code": code, "name": name, "category": row["category"],
            "prices": prices, "base": base, "sizes": available,
            "default_size": chosen, "groups": groups_for(row, prices),
            "tags": LEGACY_TAGS.get(menu_id, tags_for(name, row["category"])),
            "_legacy": code in legacy_codes, "_tall_default": chosen in ("tall", ""),
        })
    # 이름이 같은 행이 둘 이상이면 하나만 남긴다 — 기존 ID > 기본 사이즈 Tall > 엑셀 앞 행
    by_name: dict[str, dict] = {}
    for m in menus:
        keep = by_name.get(m["name"])
        if keep is None:
            by_name[m["name"]] = m
            continue
        loser = m
        if (m["_legacy"], m["_tall_default"]) > (keep["_legacy"], keep["_tall_default"]):
            by_name[m["name"]], loser = m, keep
        skipped.append({"name": loser["name"], "code": loser["code"], "why": "같은 이름 변형"})
    menus = [m for m in menus if by_name.get(m["name"]) is m]
    for m in menus:
        m.pop("_legacy"), m.pop("_tall_default")
    return menus, skipped


def option_rows(menu: dict) -> list[tuple]:
    mid, rows = menu["menu_id"], []

    def add(group, value, label, price, default):
        rows.append((f"sb_opt_{mid}_{group}_{value}", mid, group, value, label, price, default))

    if "size" in menu["groups"]:
        for value, _, label in SIZES:
            if value in menu["sizes"]:
                add("size", value, label, menu["prices"][value] - menu["base"], value == menu["default_size"])
    if "ice" in menu["groups"]:
        for value, label, default in [("none", "없이", False), ("less", "적게", False),
                                      ("normal", "보통", True), ("more", "많이", False)]:
            add("ice", value, label, 0, default)
    if "shot" in menu["groups"]:
        add("shot", "base", "기본", 0, True)
        add("shot", "extra", "샷 추가", SHOT_EXTRA, False)
    if "bean" in menu["groups"]:
        add("bean", "signature", "시그니처", 0, True)
        add("bean", "blonde", "블론드", 0, False)
        add("bean", "decaf", "디카페인", DECAF_EXTRA, False)
    if "cup" in menu["groups"]:
        add("cup", "none", "미사용", 0, True)
        add("cup", "personal", "사용", CUP_DISCOUNT, False)
    return rows


def q(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def load_aliases(known: set[str]) -> tuple[list[dict], list[dict]]:
    aliases = list(csv.DictReader(ALIAS_CSV.open(encoding="utf-8")))
    return ([a for a in aliases if a["menu_id"] in known],
            [a for a in aliases if a["menu_id"] not in known])


def render(menus: list[dict], source: str) -> str:
    aliases, dropped = load_aliases({m["menu_id"] for m in menus})
    id_list = ", ".join(q(m["menu_id"]) for m in menus)

    out = [
        "-- =====================================================================",
        "-- 11_seed_menu_sbux.sql — 스타벅스 기준 메뉴 · 별칭 · 옵션 (D-07 4.11 · 8절)",
        "--",
        "-- ★ 생성 파일이다. 손으로 고치지 말고 생성기를 다시 돌린다:",
        "--     python tools/build_sbux_seed.py <스타벅스_메뉴별_옵션정리.xlsx>",
        f"--   출처: {source} (유일한 출처 — 가격이 있는 메뉴만) · 별칭: sql/seed_src/sbux_alias.csv",
        "--   공개 메뉴 기준 시연 데이터 · 로고 미사용 (PRD 2.4 · R-13)",
        "--",
        "--   기존 스타벅스 시드를 지우고 다시 넣는다. 주문 이력이 참조하는 메뉴는 지울 수 없어 비활성으로 돌린다.",
        "--   사이즈 가격: Tall 기준 Short -800 · Grande +800 · Venti +1,600 (사이즈 열에 있는 것만).",
        "--   base_price 는 기본 사이즈 가격, 다른 사이즈는 차액이 size 옵션 추가금이다(Short 는 음수).",
        f"--   옵션 5종만 DB 로 관리한다 — size · ice · shot(+{SHOT_EXTRA}) · bean(디카페인 +{DECAF_EXTRA}) ·",
        f"--   cup(개인컵 {CUP_DISCOUNT}). 시럽 · 우유 · 휘핑 등 그 밖의 요청은 기타 요청(custom_request)이다.",
        f"--   기준 menu_version = '{MENU_VERSION}' (app/config.py MENU_VERSION 과 같은 값)",
        "-- =====================================================================",
        "",
        "-- --------------------------------------------------------------------",
        "-- 0. 기존 스타벅스 시드 정리",
        "--    별칭 · 옵션은 전부 지운다. 메뉴는 이번 시드에 없는 것만 지운다(있는 것은 아래 INSERT 가 덮어쓴다).",
        "--    order_item.menu_id 는 ON DELETE RESTRICT — 주문 이력이 참조하는 메뉴는 지우지 않고 비활성으로 둔다.",
        "-- --------------------------------------------------------------------",
        "DELETE FROM app.menu_alias WHERE brand_id = 'starbucks';",
        "DELETE FROM app.menu_option WHERE brand_id = 'starbucks'",
        "   OR menu_id IN (SELECT menu_id FROM app.menu WHERE brand_id = 'starbucks');",
        "DELETE FROM app.menu m",
        f" WHERE m.brand_id = 'starbucks' AND m.menu_id NOT IN ({id_list})",
        "   AND NOT EXISTS (SELECT 1 FROM app.order_item oi WHERE oi.menu_id = m.menu_id);",
        "UPDATE app.menu SET is_active = false",
        f" WHERE brand_id = 'starbucks' AND menu_id NOT IN ({id_list});",
        "",
        "-- --------------------------------------------------------------------",
        "-- 1. 메뉴",
        "-- --------------------------------------------------------------------",
        "INSERT INTO app.menu (menu_id, brand_id, menu_version, category, name, base_price, trait_tags, is_active) VALUES",
    ]
    values = []
    for m in menus:
        tags = "{" + ",".join(f'"{t}"' for t in m["tags"]) + "}"
        values.append(f"  ({q(m['menu_id'])}, 'starbucks', {q(MENU_VERSION)}, {q(m['category'])}, "
                      f"{q(m['name'])}, {m['base']}, {q(tags)}, true)")
    out.append(",\n".join(values))
    out += [
        "ON CONFLICT (menu_id) DO UPDATE",
        "   SET menu_version = EXCLUDED.menu_version, category = EXCLUDED.category, name = EXCLUDED.name,",
        "       base_price = EXCLUDED.base_price, trait_tags = EXCLUDED.trait_tags, is_active = EXCLUDED.is_active;",
        "",
        "-- --------------------------------------------------------------------",
        "-- 2. 별칭 — 정규화 키로 저장한다 (소문자 · 공백 제거 · D-07 4.11). 메뉴 이름 자체는 적지 않는다",
    ]
    if dropped:
        out.append("--    시드에 없는 메뉴를 가리켜 뺀 별칭: "
                   + ", ".join(f"{a['alias']}({a['menu_id']})" for a in dropped))
    out += [
        "-- --------------------------------------------------------------------",
        "INSERT INTO app.menu_alias (alias, menu_id, brand_id) VALUES",
        ",\n".join(f"  ({q(a['alias'])}, {q(a['menu_id'])}, 'starbucks')" for a in aliases),
        "ON CONFLICT (alias, menu_id) DO NOTHING;",
        "",
        "-- --------------------------------------------------------------------",
        "-- 3. 옵션 — 메뉴마다 따로 적는다. 매장 공통 행을 두지 않는다 — 공통 행은 그 종류를 모든 메뉴에 붙인다.",
        "-- 메뉴가 아직 안 정해진 항목은 매장 전체 값의 합집합으로 검증한다(menu.applicable_options)",
        "-- --------------------------------------------------------------------",
        "INSERT INTO app.menu_option (option_id, menu_id, category, brand_id, option_group, option_value, value_label, extra_price, is_default) VALUES",
    ]
    rows = [r for m in menus for r in option_rows(m)]
    out.append(",\n".join(
        f"  ({q(oid)}, {q(mid)}, NULL, 'starbucks', {q(g)}, {q(v)}, {q(label)}, {price}, "
        f"{'true' if default else 'false'})" for oid, mid, g, v, label, price, default in rows))
    out += ["ON CONFLICT (option_id) DO UPDATE",
            "   SET extra_price = EXCLUDED.extra_price, value_label = EXCLUDED.value_label,",
            "       is_default = EXCLUDED.is_default;", ""]
    return "\n".join(out)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    path = sys.argv[1]
    menus, skipped = build(path)
    OUT.write_text(render(menus, pathlib.Path(path).name), encoding="utf-8")
    _, dropped = load_aliases({m["menu_id"] for m in menus})
    print(f"메뉴 {len(menus)}종 · 옵션 {sum(len(option_rows(m)) for m in menus)}행 -> {OUT.relative_to(ROOT)}")
    print(f"제외 {len(skipped)}행 (가격 없음 {sum(s['why'] == '가격 없음' for s in skipped)} · "
          f"같은 이름 변형 {sum(s['why'] != '가격 없음' for s in skipped)})")
    if dropped:
        print(f"[경고] 시드에 없는 메뉴를 가리켜 뺀 별칭 {len(dropped)}개: "
              + ", ".join(f"{a['alias']}({a['menu_id']})" for a in dropped))


if __name__ == "__main__":
    main()
