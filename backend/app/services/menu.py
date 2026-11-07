"""메뉴 조회 · 정규화 — 별칭 사전 + 규칙이 먼저다 (D-03 벡터 검색 조건부).

G-A3 ①: brand_id · menu_version 을 **LLM 인자로 받지 않는다.** 세션에서 주입한다.
이 모듈의 공개 함수는 모두 첫 인자로 세션 맥락을 받고, 호출자가 세션에서 꺼내 넘긴다.

F-6 폴백: 메뉴 캐시는 **프로세스 메모리**다 (D-07 D-8). DB 가 끊겼을 때 쓰는 캐시를
DB 에 두면 폴백이 성립하지 않는다. 캐시는 조회 전용이고 DB 에 다시 쓰지 않는다.
"""

import hashlib
import json
import re
import time

from app import config, db
from app.services.text_norm import normalize

# --- 메뉴 캐시 (메모리 · 조회 전용) -------------------------------------------
_CACHE: dict[tuple[str, str], dict] = {}
_CACHE_TTL_SEC = 300


def _load(brand_id: str, menu_version: str) -> dict:
    """메뉴 · 별칭 · 옵션을 한 번에 읽어 캐시한다."""
    menus = db.fetch_all(
        "SELECT menu_id, brand_id, menu_version, category, name, base_price, trait_tags "
        "  FROM app.menu "
        " WHERE brand_id = %s AND menu_version = %s AND is_active = true "
        " ORDER BY category, name",
        (brand_id, menu_version),
    )
    # 비활성 메뉴의 별칭은 읽지 않는다 — by_id 에 없는 메뉴를 가리켜 매칭이 KeyError 로 죽는다
    aliases = db.fetch_all(
        "SELECT a.alias, a.menu_id FROM app.menu_alias a "
        "  JOIN app.menu m ON m.menu_id = a.menu_id "
        " WHERE a.brand_id = %s AND m.menu_version = %s AND m.is_active = true",
        (brand_id, menu_version),
    )
    options = db.fetch_all(
        "SELECT option_id, menu_id, category, option_group, option_value, value_label, "
        "       extra_price, is_default "
        "  FROM app.menu_option "
        " WHERE brand_id = %s OR brand_id IS NULL "
        " ORDER BY option_group, extra_price",
        (brand_id,),
    )

    # 매장 이름("스타벅스 아아" · "메가커피에서") — 엄격 매칭이 남은 글자를 수식어로 오인하지 않게 (D-09)
    try:
        store_words = sorted({normalize(r["alias"]) for r in db.fetch_all(
            "SELECT alias FROM app.brand_alias")} - {""}, key=len, reverse=True)
    except db.DbUnavailable:
        store_words = []

    by_id = {m["menu_id"]: m for m in menus}
    alias_index: dict[str, list[str]] = {}
    # 메뉴 이름 자체도 별칭으로 쓴다 — 시드에 모든 이름을 중복 적지 않는다
    for menu in menus:
        alias_index.setdefault(normalize(menu["name"]), []).append(menu["menu_id"])
    for row in aliases:
        alias_index.setdefault(normalize(row["alias"]), []).append(row["menu_id"])

    return {
        "menus": menus, "by_id": by_id, "alias_index": alias_index,
        "options": options, "store_words": store_words, "loaded_at": time.monotonic(),
        "checksum": hashlib.sha256(json.dumps(
            {"menus": menus, "aliases": aliases, "options": options},
            ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest(),
    }


def catalog(brand_id: str, menu_version: str | None = None, *, allow_stale: bool = True) -> dict:
    """메뉴 묶음. DB 가 끊기면 캐시로 폴백한다 (F-6)."""
    menu_version = menu_version or config.MENU_VERSION
    key = (brand_id, menu_version)
    cached = _CACHE.get(key)
    try:
        from app.services import incidents
        injected = any(row.get("target") == "menu_store" and row.get("mode") == "block"
                       for row in incidents.active_injections())
    except db.DbUnavailable:
        injected = False
    if injected:
        if cached and allow_stale:
            return {**cached, "stale": True}
        raise db.DbUnavailable("F-6 주입: 메뉴 카탈로그 읽기 불가, 일치 캐시 없음")
    if cached and time.monotonic() - cached["loaded_at"] < _CACHE_TTL_SEC:
        return cached
    try:
        loaded = _load(brand_id, menu_version)
    except db.DbUnavailable:
        # 메뉴 조회 한정 장애와 전체 DB 단절을 구분한다.
        try:
            db.fetch_one("SELECT 1")
            database_reachable = True
        except db.DbUnavailable:
            database_reachable = False
        if cached and allow_stale and database_reachable:
            return {**cached, "stale": True}
        raise
    _CACHE[key] = loaded
    return loaded


def preload_active() -> dict:
    """Warm every active brand/version before an instance is marked ready."""
    rows = db.fetch_all("SELECT DISTINCT brand_id,menu_version FROM app.menu "
                        "WHERE is_active=true ORDER BY brand_id,menu_version")
    if not rows:
        raise db.DbUnavailable("active menu catalog is empty")
    checksums = {}
    for row in rows:
        key = (row["brand_id"], row["menu_version"])
        cached = _CACHE.get(key)
        if cached and time.monotonic() - cached["loaded_at"] < _CACHE_TTL_SEC:
            loaded = cached
        else:
            loaded = _load(*key)
            _CACHE[key] = loaded
        checksum = loaded.get("checksum") or ""
        if not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise db.DbUnavailable("menu cache checksum validation failed")
        checksums[f"{key[0]}:{key[1]}"] = checksum
    return {"catalogs": len(checksums), "checksums": checksums}


def cache_state() -> dict:
    """/health · API-045 가 읽는 폴백 상태."""
    return {
        "entries": len(_CACHE),
        "brands": sorted({k[0] for k in _CACHE}),
        "stale": any(v.get("stale") for v in _CACHE.values()),
    }


# --- 메뉴 매칭 ----------------------------------------------------------------
# 포함 규칙으로 별칭을 찾은 뒤 남은 글자 중 "설명되는" 것 — 온도 · 사이즈 표현 · 수량 · 조사 · 군말.
# 이것을 지우고도 2글자 이상 남으면 **메뉴판에 없는 수식어**가 붙은 것이다("헤이즐넛" + 라떼).
# 그때 짧은 별칭으로 자동 확정하면 미등록 메뉴가 조용히 카페 라떼가 된다(D-08 0.1 · SC-8 위반 5건)
_EXPLAINED = sorted({
    "아이스", "따뜻한", "따뜻하게", "뜨거운", "뜨겁게", "차가운", "차갑게", "시원한", "시원하게", "핫", "hot", "ice",
    "큰", "작은", "중간", "보통", "제일", "미디엄", "스몰", "숏", "사이즈", "얼음", "적게", "많이", "조금", "추가",
    "하나", "한잔", "잔", "한", "개", "컵", "거", "것", "걸로", "걸", "그걸로", "같은", "나도", "저도",
    "나는", "난", "저는", "전", "그럼", "그냥", "주세요", "부탁", "할게", "할래", "마실게", "먹을게",
    # 주문 어미 — 메뉴 표현에 붙어 와도 메뉴판에 없는 수식어가 아니다 (2026-11-05 보고 4번)
    "먹을래", "마실래", "먹겠", "마시겠", "시킬게", "시킬래", "주문", "원해", "싶어",
    "요", "도", "만", "은", "는", "이", "가", "을", "를", "로", "으로", "나", "저", "에서",
}, key=len, reverse=True)


def _unexplained(rest: str, store_words: list[str] = ()) -> str:
    for token in store_words:            # 매장 이름을 먼저 — "더벤티"가 사이즈 "벤티"로 쪼개지지 않게
        rest = rest.replace(token, "")
    for token in _EXPLAINED:
        rest = rest.replace(token, "")
    # 한글 낱자(ㅎㅎ · ㅋㅋ)는 메뉴 이름에 없다 — 웃음이 "메뉴판에 없는 수식어"로 세어지지 않게
    return "".join(ch for ch in rest if not ch.isdigit() and not ("ㄱ" <= ch <= "ㅣ"))


def match(brand_id: str, menu_version: str, menu_text: str, *, strict: bool = False) -> dict:
    """원문 메뉴 표현 → 메뉴 후보.

    돌려주는 값
      {"menu_id", "name", "base_price"}                 하나로 정해졌다
      {"menu_id": None, "candidates": [...]}            후보 복수 · 미등록

    strict=True — 분석 경로(sessions.match_item). 포함 규칙에서 설명되지 않는 글자가 남으면
    자동 확정하지 않고 후보로 넘긴다. 채팅 수정 · 답장 제안은 담당자가 직접 지시하고 되돌리기가 있어 기본값이다.
    """
    cat = catalog(brand_id, menu_version)
    key = normalize(menu_text)
    if not key:
        return {"menu_id": None, "candidates": [], "reason": "unregistered_menu"}

    # 1 완전 일치 — 중복을 먼저 지운다.
    #   메뉴 이름과 별칭이 **같은 메뉴를 두 번** 가리키는 경우가 있다('아이스티'는
    #   이름이면서 별칭이다). 지우지 않으면 후보 2개로 보여 모호 판정이 난다.
    exact = list(dict.fromkeys(cat["alias_index"].get(key, [])))
    if len(exact) == 1:
        return _hit(cat, exact[0])
    if len(exact) > 1:
        return {"menu_id": None, "candidates": [_hit(cat, mid) for mid in exact],
                "reason": "ambiguous_spec"}

    # 2 문장이 별칭을 품은 경우 — **가장 긴 별칭만** 쓴다.
    #   짧은 별칭까지 모으면 "아이스 아메리카노"가 `아이스아메리카노`(정답) ·
    #   `아메리카노` · `아메` 셋에 걸려 후보 3개가 되고, 정확히 맞는 입력이
    #   모호 판정을 받는다. 긴 쪽이 더 구체적인 지정이다.
    contained = [k for k in cat["alias_index"] if k and k in key]
    if contained:
        longest = max(len(k) for k in contained)
        winners = list(dict.fromkeys(
            mid for k in contained if len(k) == longest for mid in cat["alias_index"][k]))
        alias = next(k for k in contained if len(k) == longest)
        if strict and len(_unexplained(key.replace(alias, "", 1), cat.get("store_words", []))) >= 2:
            # 후보는 준다 — 담당자가 "이 메뉴가 맞다"면 한 번 누르면 된다. 맞는 게 없으면 메뉴 찾기로 간다
            return {"menu_id": None, "candidates": [_hit(cat, mid) for mid in winners[:5]],
                    "reason": "unregistered_menu"}
        if len(winners) == 1:
            return _hit(cat, winners[0])
        return {"menu_id": None, "candidates": [_hit(cat, mid) for mid in winners[:5]],
                "reason": "ambiguous_spec"}

    # 3 입력이 별칭의 일부인 경우(타이핑 중 · 줄임말) — 후보를 모아 담당자에게 넘긴다
    #   한 글자는 보지 않는다. "네~"가 레모"네"이드에 걸려 딸기 아사이 레모네이드로 **자동 확정**됐다
    #   (2026-11-04 메뉴판 71종 교체 뒤 Q-02 EV-045). 메뉴가 늘수록 한 글자 대답이 어딘가에 걸린다
    if len(key) < 2:
        return {"menu_id": None, "candidates": [], "reason": "unregistered_menu"}
    prefix = list(dict.fromkeys(
        mid for k, ids_ in cat["alias_index"].items() if k and key in k for mid in ids_))
    if len(prefix) == 1:
        return _hit(cat, prefix[0])
    if prefix:
        return {"menu_id": None, "candidates": [_hit(cat, mid) for mid in prefix[:5]],
                "reason": "ambiguous_spec"}

    return {"menu_id": None, "candidates": [], "reason": "unregistered_menu"}


def by_name(brand_id: str, menu_version: str, name: str) -> dict | None:
    """메뉴 **이름**으로 하나를 찾는다. 별칭은 보지 않는다 (2026-11-05 2차 보고 2번).

    왜 필요한가: 확인 질문의 버튼 라벨은 메뉴 **이름**이다. 그런데 이름이 다른 메뉴의 별칭이기도 한 경우가 있다
    — 시드에서 "아이스 카라멜 마키아또"가 별칭 `카라멜마키아또`를 함께 가진다. 그래서 담당자가 버튼
    [카라멜 마키아또]를 눌러도 `match()` 는 후보 2개로 보고 확정하지 못했고, 화면에는 "그 내용은 반영하지
    못했어요"가 떴다. **이름은 메뉴판에서 유일하므로** 이름 일치를 별칭보다 먼저 본다.
    """
    cat = catalog(brand_id, menu_version)
    key = normalize(name)
    hit = next((m for m in cat["menus"] if normalize(m["name"]) == key), None)
    return _hit(cat, hit["menu_id"]) if hit else None


def mentions(brand_id: str, menu_version: str, text: str) -> list[str]:
    """이 문장이 가리키는 **서로 다른 메뉴** 목록 (2026-11-05 2차 보고 3번).

    겹치지 않게 **긴 별칭부터** 집는다 — "아아 톨 하나랑 카페라떼 하나"는 두 개, "아이스 유스베리 티
    어떠세요"는 하나다. 참조 주문("그걸로")을 자동 확정해도 되는지 가리는 데 쓴다.
    """
    cat = catalog(brand_id, menu_version)
    key = normalize(text)
    found: list[str] = []
    taken: list[tuple[int, int]] = []
    for alias in sorted((k for k in cat["alias_index"] if k), key=len, reverse=True):
        start = key.find(alias)
        while start >= 0:
            end = start + len(alias)
            if not any(s < end and start < e for s, e in taken):
                taken.append((start, end))
                for menu_id in cat["alias_index"][alias]:
                    if menu_id not in found:
                        found.append(menu_id)
                break
            start = key.find(alias, start + 1)
    return found


# --- 지금 못 내는 메뉴를 가리키는 말 찾기 --------------------------------------
#
# 참조 주문("그걸로")이 가리킨 것이 **메뉴 이름이긴 한데 이 매장 기준에는 없을** 때가 있다.
# 그것을 모르면 참조 해소가 그 말을 그냥 지나쳐 더 오래된 주문을 집는다 — 2026-11-07 실측:
#
#   [정하준] 민트 초코가 유명하대        <- 가리킨 것. 스타벅스 기준에는 없다
#   [최유나] 그럼 난 그걸로 하나~        -> 아이스 아메리카노 로 **확정**됐다(확인 필요도 안 붙음)
#
# 맞는 결론은 "민트 초코는 지금 메뉴에 없어요 — 확인해 주세요"다(미등록 → NEEDS_REVIEW).
_ALL_NAMES: dict = {"at": 0.0, "index": None}
_ALL_NAMES_TTL_SEC = 300

# 2글자 별칭(아아 · 라떼 · 모카 …)은 잡담에 우연히 들어갈 수 있다. 다른 매장 기준에만 있는
# 2글자가 걸리면 멀쩡한 참조를 막으므로 3글자부터 본다 — 놓치는 쪽이 틀리는 쪽보다 낫다
_MIN_UNAVAILABLE_LEN = 3


def _all_menu_names() -> dict[str, list[dict]]:
    """정규화한 이름 · 별칭 -> 그 이름을 쓰는 메뉴들. **모든 매장 기준 · 비활성 포함.**

    `catalog()` 은 지금 매장의 활성 메뉴만 담는다(그래야 매칭이 맞다). 이 사전은 반대로
    "시스템이 아는 메뉴 이름 전부"를 담아, 못 내는 메뉴를 **가리켰다는 사실**만 알아내는 데 쓴다.
    여기서 나온 menu_id 로 주문을 만들지 않는다 — 판정에만 쓴다.
    """
    now = time.monotonic()
    if _ALL_NAMES["index"] is not None and now - _ALL_NAMES["at"] < _ALL_NAMES_TTL_SEC:
        return _ALL_NAMES["index"]

    index: dict[str, list[dict]] = {}

    def add(label: str, row: dict) -> None:
        key = normalize(label or "")
        if len(key) < _MIN_UNAVAILABLE_LEN:
            return
        index.setdefault(key, []).append(row)

    for row in db.fetch_all(
            "SELECT menu_id, brand_id, menu_version, name, is_active FROM app.menu"):
        add(row["name"], row)
    for row in db.fetch_all(
            "SELECT a.alias, m.menu_id, m.brand_id, m.menu_version, m.name, m.is_active "
            "  FROM app.menu_alias a JOIN app.menu m ON m.menu_id = a.menu_id"):
        add(row["alias"], row)

    _ALL_NAMES.update({"at": now, "index": index})
    return index


def unavailable_mentions(brand_id: str, menu_version: str, text: str) -> list[dict]:
    """이 문장이 **지금 매장 기준으로는 낼 수 없는 메뉴**를 가리키는가.

    지금 낼 수 있는 메뉴를 가리키면 빈 목록이다 — 그건 `mentions()` 가 할 일이다.
    겹치지 않게 긴 이름부터 집는 것도 `mentions()` 와 같다.
    """
    if mentions(brand_id, menu_version, text):
        return []
    key = normalize(text or "")
    if not key:
        return []
    index = _all_menu_names()
    hits: list[dict] = []
    taken: list[tuple[int, int]] = []
    for alias in sorted(index, key=len, reverse=True):
        start = key.find(alias)
        if start < 0:
            continue
        end = start + len(alias)
        if any(s < end and start < e for s, e in taken):
            continue
        rows = index[alias]
        if any(r["brand_id"] == brand_id and r["menu_version"] == menu_version
               and r["is_active"] for r in rows):
            # 지금 낼 수 있는 메뉴다. `mentions()` 가 못 집었어도 여기서 막지 않는다
            continue
        taken.append((start, end))
        hits.append({"text": alias, "name": rows[0]["name"], "menu_id": rows[0]["menu_id"],
                     "brand_id": rows[0]["brand_id"], "is_active": rows[0]["is_active"]})
    return hits


def with_temperature(brand_id: str, menu_version: str, hit: dict, hint: str | None) -> dict:
    """온도 단서로 같은 메뉴의 아이스 · 따뜻한 판을 고른다. 메뉴 이름이 온도를 품는다("카페 라떼" / "아이스 카페 라떼").

    단서: 얼음 옵션 · 이름 뒤에 붙은 "아이스" · 차가운 · 따뜻한 등(intake.temperature_hint).
    다른 판이 메뉴 DB 에 없으면(콜드 브루처럼 한쪽만 있는 메뉴) 그대로 둔다.
    """
    if not hint or not hit.get("menu_id"):
        return hit
    name = hit["name"]
    if hint == "ice" and not name.startswith("아이스"):
        target = f"아이스 {name}"
    elif hint == "hot" and name.startswith("아이스 "):
        target = name[len("아이스 "):]
    else:
        return hit
    cat = catalog(brand_id, menu_version)
    key = normalize(target)
    other = next((m for m in cat["menus"] if normalize(m["name"]) == key), None)
    return _hit(cat, other["menu_id"]) if other else hit


def narrow_by_temperature(hit: dict, hint: str | None) -> dict:
    """후보가 아이스 판 · 따뜻한 판으로만 갈린 경우, 온도 단서로 하나를 고른다 (2026-11-05 보고 6번).

    "카라멜 마키아또 아이스로 주세요"는 같은 별칭에 두 메뉴가 걸려 후보 2개가 됐고,
    `with_temperature` 는 **이미 정해진** 메뉴만 바꾸므로 아무 일도 하지 않았다. 그 사이에 끼운다.
    고르는 조건은 좁다 — 온도로 갈라 남는 후보가 **정확히 하나**일 때만. 아니면 그대로 물어본다.
    """
    if not hint or hit.get("menu_id"):
        return hit
    if hit.get("reason") != "ambiguous_spec":
        # 미등록(메뉴판에 없는 수식어가 붙었다)은 온도로 좁히지 않는다 — "헤이즐넛 카라멜 마키아또 아이스"가
        # 조용히 아이스 카라멜 마키아또로 확정된다. 그것이 SC-8 "오답 확정 0건"이 막는 자리다
        return hit
    candidates = hit.get("candidates") or []
    if len(candidates) < 2:
        return hit
    iced = [c for c in candidates if (c.get("name") or "").startswith("아이스")]
    hot = [c for c in candidates if not (c.get("name") or "").startswith("아이스")]
    picked = iced if hint == "ice" else hot
    if len(picked) != 1 or not (iced and hot):
        return hit
    return {**picked[0], "candidates": [], "reason": None}


def temperature_variants(brand_id: str, menu_version: str, base_text: str) -> list[dict]:
    """온도를 뺀 메뉴 표현 → 따뜻한 메뉴 · 아이스 메뉴 후보 (온도 확인 질문).

    메뉴 이름이 온도를 품는다("카페 라떼" / "아이스 카페 라떼") — 두 이름을 각각 맞춰 본다.
    """
    found: list[dict] = []
    for text in (base_text, f"아이스 {base_text}"):
        if not normalize(text):
            continue
        hit = match(brand_id, menu_version, text)
        for one in ([hit] if hit.get("menu_id") else hit.get("candidates") or []):
            if one["menu_id"] not in {f["menu_id"] for f in found}:
                found.append(one)
    return found[:4]


def _hit(cat: dict, menu_id: str) -> dict:
    menu = cat["by_id"][menu_id]
    return {"menu_id": menu_id, "name": menu["name"], "base_price": menu["base_price"],
            "category": menu["category"], "trait_tags": list(menu["trait_tags"] or [])}


def by_traits(brand_id: str, menu_version: str, traits: list[str]) -> list[dict]:
    """특성 표현("달달한 걸로") → 정렬된 후보 + 정렬 근거 태그 (FR-004).

    추천을 암시하지 않는다 — 모든 후보에 태그를 붙이고 순위 필드를 두지 않는다.
    """
    cat = catalog(brand_id, menu_version)
    wanted = set(traits)
    scored = []
    for menu in cat["menus"]:
        tags = set(menu["trait_tags"] or [])
        overlap = wanted & tags
        if overlap:
            scored.append((len(overlap), menu["name"], menu))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [{**_hit(cat, m["menu_id"]), "reason_tags": sorted(set(m["trait_tags"] or []) & wanted)}
            for _, _, m in scored]


# --- 옵션 --------------------------------------------------------------------
def applicable_options(brand_id: str, menu_version: str, menu_id: str | None = None) -> list[dict]:
    """이 메뉴에 적용되는 옵션 행 — 화면 버튼 · 허용 검증 · 추가금 · 기본값이 **모두 이 함수**를 쓴다.

    범위는 세 단계다: 매장 공통(menu_id · category 비움) < 분류(category) < 메뉴(menu_id).
    **옵션 종류(option_group)마다 가장 구체적인 범위의 행만 쓴다** — 더 넓은 범위의 같은 종류 행은 버린다.
      - 메뉴별 추가금: 그 메뉴 범위에 같은 값을 다른 extra_price 로 적는다
      - 메뉴별 불가: 그 범위에 그 값을 빼고 적는다 (티 분류의 shot 을 base 하나만 → 샷 추가 불가)
      - 종류 자체가 없는 메뉴: 그 범위에 그 종류의 행을 base 하나로만 둔다
    menu_id 가 없거나(메뉴 미확정) 메뉴판에 없으면 매장 공통 행만 본다. 공통 행이 없는 종류는
    (스타벅스처럼 옵션을 메뉴마다 적은 매장) 그 매장 전체 값의 합집합으로 본다 — 메뉴를 고르기 전에
    "벤티"를 옵션 오류로 떨어뜨리지 않기 위해서다. 메뉴가 정해지면 그 메뉴 범위로 다시 검증된다.
    """
    cat = catalog(brand_id, menu_version)
    menu = cat["by_id"].get(menu_id) if menu_id else None
    levels: dict[str, dict[int, list[dict]]] = {}
    for opt in cat["options"]:
        if opt["menu_id"]:
            if not menu or opt["menu_id"] != menu_id:
                continue
            level = 2
        elif opt["category"]:
            if not menu or opt["category"] != menu["category"]:
                continue
            level = 1
        else:
            level = 0
        levels.setdefault(opt["option_group"], {}).setdefault(level, []).append(opt)
    picked = [opt for by_level in levels.values() for opt in by_level[max(by_level)]]
    if menu:
        return picked
    seen = {(o["option_group"], o["option_value"]) for o in picked}
    for opt in cat["options"]:
        if opt["option_group"] in levels:
            continue
        key = (opt["option_group"], opt["option_value"])
        if key not in seen:
            seen.add(key)
            picked.append(opt)
    return picked


def split_unoffered(brand_id: str, menu_version: str, options: dict,
                    menu_id: str | None) -> tuple[dict, dict]:
    """(남길 옵션, 옮길 옵션). 메뉴에 **그 종류 자체가 없는** 옵션은 기타 요청으로 옮길 몫이다.

    스타벅스는 size · ice · shot · bean · cup 다섯 종류만 메뉴판 옵션이다. "두유로" · "시럽 추가"처럼
    종류가 없는 요청을 옵션 오류로 떨어뜨리면 담당자는 고를 버튼이 없는 "옵션을 다시 골라 주세요"를 본다.
    종류는 있는데 값이 막힌 경우(티의 샷 추가처럼 범위가 막은 값)는 옮기지 않는다 — 검증이 잡는다.
    메뉴가 안 정해졌으면 아무것도 옮기지 않는다(메뉴를 고른 뒤 다시 나눈다).
    """
    if not menu_id or not options:
        return dict(options or {}), {}
    offered = {o["option_group"] for o in applicable_options(brand_id, menu_version, menu_id)}
    kept = {g: v for g, v in options.items() if g in offered}
    moved = {g: v for g, v in options.items() if g not in offered}
    return kept, moved


def option_groups(brand_id: str, menu_version: str, menu_id: str | None = None) -> list[dict]:
    """화면 버튼 묶음. 해당 메뉴에 적용되는 그룹만 돌려준다 (P-06 행 편집 규칙)."""
    grouped: dict[str, list[dict]] = {}
    for opt in applicable_options(brand_id, menu_version, menu_id):
        grouped.setdefault(opt["option_group"], []).append(opt)
    return [{"option_group": group, "values": values} for group, values in grouped.items()]


def _find(brand_id: str, menu_version: str, group: str, value: str,
          menu_id: str | None) -> dict | None:
    return next((o for o in applicable_options(brand_id, menu_version, menu_id)
                 if o["option_group"] == group and o["option_value"] == value), None)


def is_valid_option(brand_id: str, menu_version: str, group: str, value: str,
                    menu_id: str | None = None) -> bool:
    """이 메뉴에 허용된 옵션만 받는다 — 없는 키는 호출자가 400 으로 거른다 (API-023)."""
    return _find(brand_id, menu_version, group, value, menu_id) is not None


def option_extra_price(brand_id: str, menu_version: str, options: dict,
                       menu_id: str | None = None) -> int:
    """옵션 추가금 합계 — 이 메뉴 범위의 가격을 쓴다. 모르는 키는 0 으로 세지 않고 호출자가 먼저 검증한다."""
    total = 0
    for group, value in (options or {}).items():
        opt = _find(brand_id, menu_version, group, value, menu_id)
        if opt:
            total += opt["extra_price"]
    return total


def option_label(brand_id: str, menu_version: str, group: str, value: str) -> str:
    cat = catalog(brand_id, menu_version)
    for opt in cat["options"]:
        if opt["option_group"] == group and opt["option_value"] == value:
            return opt["value_label"]
    return str(value)


def search(brand_id: str, menu_version: str, query: str, limit: int | None = None) -> list[dict]:
    """메뉴 찾기 모달 (API-026). 세션 기준 메뉴만 보인다 (FR-004).

    기본은 **전부** 돌려준다 — 20개로 자르니 [다른 메뉴]에서 스타벅스 72종 중 20종만 보였다(2026-11-05 5차 보고).
    모달 목록은 고정 높이 안에서 스크롤된다. 툴 호출(menu_tools)처럼 LLM 에 넘기는 곳만 limit 을 준다
    """
    cat = catalog(brand_id, menu_version)
    key = normalize(query)
    hits = []
    for menu in cat["menus"]:
        if not key or key in normalize(menu["name"]):
            hits.append(menu)
            continue
        if any(key in alias_key and menu["menu_id"] in ids
               for alias_key, ids in cat["alias_index"].items()):
            hits.append(menu)
    return [_hit(cat, m["menu_id"]) for m in (hits if limit is None else hits[:limit])]


def search_view(brand_id: str, menu_version: str, query: str) -> dict:
    """API-026 응답 — 목록과 함께 **범위 고지**를 준다.

    고지가 없으면 담당자는 "내 메뉴가 왜 없지"를 알 수 없다. 다른 매장 기준 메뉴가
    조회되지 않는 것은 결함이 아니라 규칙이므로(FR-004) 그 사실을 문장으로 돌려준다.
    """
    from app.services import brand as brand_service

    label = brand_service.BRAND_LABEL.get(brand_id, brand_service.BRAND_LABEL["generic"])
    hits = search(brand_id, menu_version, query)
    return {
        "menus": [{"menu_id": h["menu_id"], "name": h["name"],
                   "spec_text": option_label(brand_id, menu_version, "size", _default_size(
                       brand_id, menu_version)),
                   "estimated_price": h["base_price"],
                   "trait_tags": h.get("trait_tags", [])} for h in hits],
        "scope_notice": f"{label} 메뉴만 보여줘요",
        "empty_text": None if hits else "찾는 메뉴가 없어요. 다른 이름으로 찾아보세요",
    }


def default_option(brand_id: str, menu_version: str, group: str, menu_id: str | None = None) -> str:
    """그룹의 기본값 — 매장 기준에 따라 tall · regular 로 갈리고, 메뉴 범위에 기본값이 있으면 그것."""
    for opt in applicable_options(brand_id, menu_version, menu_id):
        if opt["option_group"] == group and opt["is_default"]:
            return opt["option_value"]
    return ""


def _default_size(brand_id: str, menu_version: str) -> str:
    return default_option(brand_id, menu_version, "size")
