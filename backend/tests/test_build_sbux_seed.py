# 스타벅스 시드 생성기 — 엑셀 한 행이 메뉴 · 사이즈 가격 · 옵션 5종으로 어떻게 바뀌는가
# 엑셀 파일 없이 행 모양만으로 본다(openpyxl 이 없는 환경에서도 돈다).
import pytest

pytest.importorskip("openpyxl")
from tools import build_sbux_seed as B  # noqa: E402


def row(size_text, prices, bean_shot="-", ice_cup="얼음: 없이·적게·보통(기본)·많이 / 개인컵 여부"):
    return {"size_text": size_text, "bean_shot": bean_shot, "ice_cup": ice_cup,
            "prices": {k: v for k, v in zip(("short", "tall", "grande", "venti"), prices) if v}}


def test_groups_follow_the_sheet_columns():
    latte = row("Tall(355ml, 기본)", (None, 5200, 5800, 6600),
                bean_shot="시그니처(기본), 블론드, 디카페인(+300원) / 에스프레소 샷 n개 추가")
    assert B.groups_for(latte) == ["size", "ice", "shot", "bean", "cup"]
    hot_tea = row("Tall(355ml, 기본), Venti(591ml)", (None, 4500, None, 6100), ice_cup="개인컵 여부")
    assert B.groups_for(hot_tea) == ["size", "cup"]                        # 얼음 · 샷 · 원두 없음
    bottle = row("선택 없음", (None, None, None, None), ice_cup="컵: 선택안함(기본)·매장컵")
    assert B.groups_for(bottle) == []                                      # 매장컵 선택은 기타 요청


def test_default_size_is_the_marked_one_and_base_price_is_its_price():
    assert B.default_size("Tall(355ml), Grande(473ml, 기본), Venti(591ml)",
                          ["tall", "grande", "venti"]) == "grande"
    assert B.default_size("Grande(473ml)", ["grande"]) == "grande"         # 망고 바나나 블렌디드
    assert B.default_size("Tall(355ml), Grande(473ml)", ["tall", "grande"]) == "tall"   # 표시 없으면 첫 사이즈


def test_size_extra_is_the_difference_from_the_default_size():
    menu = {"menu_id": "m", "groups": ["size", "cup"], "sizes": ["short", "tall", "grande"],
            "prices": {"short": 5100, "tall": 5900, "grande": 6500}, "base": 5900, "default_size": "tall"}
    rows = {(g, v): (price, default) for _, _, g, v, _, price, default in B.option_rows(menu)}
    assert rows[("size", "short")] == (-800, False)
    assert rows[("size", "tall")] == (0, True)
    assert rows[("size", "grande")] == (600, False)
    assert rows[("cup", "personal")] == (-400, False)


def test_sizes_are_built_from_tall_price():
    # 2026-11-06 사용자 결정: Short -800 · Grande +800 · Venti +1,600 — '사이즈' 열에 있는 것만
    assert B.tall_based(5200, ["tall", "grande", "venti"]) == {"tall": 5200, "grande": 6000, "venti": 6800}
    assert B.tall_based(5900, ["short", "tall", "grande", "venti"])["short"] == 5100
    assert B.tall_based(4500, ["tall", "venti"]) == {"tall": 4500, "venti": 6100}      # 그란데 없는 티
    assert B.tall_based(5800, ["short"]) == {"short": 5000}                         # 코르타도 — Tall 을 만들지 않는다


def test_offered_sizes_follow_the_size_column():
    text = "Short(237ml), Tall(355ml, 기본), Grande(473ml), Venti(591ml) / Short는 HOT만"
    assert B.offered_sizes("차이 티 라떼", text) == ["short", "tall", "grande", "venti"]
    assert B.offered_sizes("아이스 차이 티 라떼", text) == ["tall", "grande", "venti"]   # 아이스는 Short 없음
    assert B.offered_sizes("에스프레소", "Solo, Doppio") == []                          # 사이즈 옵션 없이 한 가격
    assert B.offered_sizes("ABC 클렌즈 190ML", "단일 용량 (제품명에 표기)") == []


def test_memo_price_is_read_only_when_written_as_price():
    assert B.memo_prices("Tall 5,200원 / Grande 5,800원 / 샷 추가 +600원") == ({"tall": 5200, "grande": 5800}, None)
    assert B.memo_prices("사진 가격 4,700원 / 주스 가격표") == ({}, 4700)
    assert B.memo_prices("메뉴 가격 미확인 · Short = Tall - 800원") == ({}, None)
    assert B._won("#REF!") is None and B._won(None) is None and B._won(4100) == 4100


def test_unpriced_size_is_not_offered():
    menu = {"menu_id": "m", "groups": ["size"], "sizes": ["tall", "venti"],
            "prices": {"tall": 4500, "venti": 6100}, "base": 4500, "default_size": "tall"}
    assert {v for _, _, g, v, *_ in B.option_rows(menu) if g == "size"} == {"tall", "venti"}
