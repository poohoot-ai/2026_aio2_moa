"""게이트웨이 상태 읽기 — HAProxy CSV 의 상태 칸을 쓴다 (F-3 검증의 전제).

실측 결함 2026-11-06: 컨테이너가 사라지면 HAProxy 는 DOWN 이 아니라 **MAINT (resolution)** 으로 간다
(`resolvers docker` · `init-addr libc,none` — DNS 레코드가 없어진다).
코드가 "DOWN" 문자열만 찾아 60초 동안 한 번도 못 찾았고, F-3 이 "예상과 다름"으로 늘 FAIL 이었다.
"""
from healing import __main__ as worker

HEAD = ("# pxname,svname,qcur,qmax,scur,smax,slim,stot,bin,bout,dreq,dresp,ereq,econ,eresp,"
        "wretr,wredis,status,weight,act,bck,chkfail,chkdown,lastchg,downtime,qlimit,pid")


def _csv(status: str) -> str:
    cells = ["chk_a", "backend"] + ["0"] * 15 + [status] + ["1"] * 8
    return HEAD + "\n" + ",".join(cells) + "\nchk_b,backend," + ",".join(["0"] * 15 + ["UP"] + ["1"] * 8)


def test_up_is_read_as_up():
    assert worker._gateway_status_from_csv(_csv("UP"), "a") == "UP"


def test_a_vanished_container_is_excluded_even_though_it_is_not_down():
    assert worker._gateway_excluded_from_csv(_csv("MAINT (resolution)"), "a") is True


def test_plain_down_is_excluded():
    assert worker._gateway_excluded_from_csv(_csv("DOWN"), "a") is True


def test_down_in_transition_is_excluded():
    assert worker._gateway_excluded_from_csv(_csv("DOWN 1/2"), "a") is True


def test_up_is_not_excluded():
    assert worker._gateway_excluded_from_csv(_csv("UP"), "a") is False
    # UP 2/3 (재등록 중)도 아직 올라오는 중이지만 제외 상태는 아니다
    assert worker._gateway_excluded_from_csv(_csv("UP 2/3"), "a") is False


def test_the_other_stack_is_not_confused():
    assert worker._gateway_status_from_csv(_csv("DOWN"), "b") == "UP"


def test_a_missing_row_is_not_claimed_as_excluded():
    # 줄을 못 찾았으면 "모른다"이지 "제외됐다"가 아니다 — 모르는 것을 근거로 통과시키지 않는다
    assert worker._gateway_status_from_csv(HEAD, "a") is None
    assert worker._gateway_excluded_from_csv(HEAD, "a") is False
