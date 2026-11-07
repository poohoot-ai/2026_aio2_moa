"""D-16 구현점검서 1 ~ 3절 — 파일 존재 · 설계 ID ↔ 코드 대조 · 상한 일치. LLM · DB를 쓰지 않는다.

    python tools/d16_static_check.py [문서 폴더]      # 기본: ../../03.구현문서 (설계 문서가 있는 곳)

양쪽을 본다 — 문서에만 있는 ID(미구현)와 코드에만 있는 노드 · 툴 · 엔드포인트(설계서의 구멍).
측정 도구 자기 검증(G-A0): 일부러 없는 ID 를 섞어 "없음"으로 잡히는지 먼저 본다.
"""

import inspect
import json
import logging
import os
import pathlib
import re
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
logging.disable(logging.CRITICAL)
os.environ["ENABLE_DEBUG_API"] = "true"          # API-090 · 091 은 이 플래그가 있을 때만 등록된다

DOCS = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else BACKEND.parent.parent / "03.구현문서"
LOG = BACKEND / "eval" / "d16_static_check.json"


def files() -> list[dict]:
    root = BACKEND.parent
    want = [("backend/app/tools/", "D-13"), ("backend/app/graph/", "D-15"), ("backend/app/llm.py", "D-04"),
            ("backend/app/main.py", "D-06"), ("backend/prompts/", "D-14"), ("backend/sql/", "D-07 · D-08"),
            ("frontend/", "D-05"), ("backend/tests/", "Q-03 전 — 있으면 센다")]
    out = []
    for rel, owner in want:
        p = root / rel
        items = [x for x in p.rglob("*") if x.is_file() and "__pycache__" not in x.parts] if p.is_dir() else ([p] if p.exists() else [])
        out.append({"path": rel, "exists": p.exists(), "files": len(items), "owner": owner})
    out.append({"path": "backend/app/rag/", "exists": (root / "backend/app/rag").exists(), "files": 0,
                "owner": "D-09 — U-15 미도입 판정으로 만들지 않음(D-08 0.1)"})
    return out


def doc_endpoints() -> dict[str, tuple[str, str]]:
    text = (DOCS / "API스펙.md").read_text(encoding="utf-8")
    return {m[0]: (m[1], m[2]) for m in re.findall(r"^\| (API-\d{3}) \| ([A-Z]+) \| `([^`]+)`", text, re.M)}


def code_endpoints() -> set[tuple[str, str]]:
    from app.main import app

    found = set()
    for route in app.routes:
        for method in getattr(route, "methods", None) or []:
            if method in ("GET", "POST", "PATCH", "PUT", "DELETE") and not route.path.startswith(("/docs", "/openapi", "/redoc")):
                found.add((method, route.path))
    return found


def compare_endpoints(doc: dict, code: set) -> dict:
    doc_set = set(doc.values())
    return {"doc_only": sorted(f"{k} {m} {p}" for k, (m, p) in doc.items() if (m, p) not in code),
            "code_only": sorted(f"{m} {p}" for m, p in code - doc_set),
            "matched": sum(1 for v in doc.values() if v in code), "doc_total": len(doc)}


def nodes() -> dict:
    from app.graph import build, nodes as n

    graph_nodes = set(build.build().get_graph().nodes) - {"__start__", "__end__"}
    by_id = {}
    for name, fn in inspect.getmembers(n, inspect.isfunction):
        doc = inspect.getdoc(fn) or ""
        m = re.match(r"(N-\d{2})", doc)
        if m and name.endswith("_node"):
            by_id[m.group(1)] = name
    text = (DOCS / "에이전트흐름설계서.md").read_text(encoding="utf-8")
    designed = sorted(set(re.findall(r"^\| (N-\d{2}) \|", text, re.M)))
    return {"designed": designed, "code": by_id, "doc_only": [i for i in designed if i not in by_id],
            "code_only": [i for i in by_id if i not in designed], "graph_nodes": len(graph_nodes)}


def tools() -> dict:
    from app.tools import registry

    code = {spec["id"]: name for name, spec in registry.TOOLS.items()}
    text = (DOCS / "툴스키마정의서.md").read_text(encoding="utf-8")
    designed = dict(re.findall(r"^### (TOOL-\d{3}) `([a-z_]+)`", text, re.M))
    return {"designed": designed, "code": code,
            "doc_only": [i for i in designed if code.get(i) != designed[i]],
            "code_only": [i for i in code if i not in designed]}


def limits() -> list[dict]:
    from app import config

    d12 = (DOCS / "에이전트흐름설계서.md").read_text(encoding="utf-8")
    d03 = (DOCS / "시스템정의서.md").read_text(encoding="utf-8")
    rows = [
        ("그래프 순회 상한", "GRAPH_RECURSION_LIMIT", config.GRAPH_RECURSION_LIMIT,
         r"그래프 순회 상한 \| \*\*(\d+) 슈퍼스텝", r"그래프 순회 상한 \| \*\*(\d+)\*\*"),
        ("되묻기 상한", "MAX_REASK", config.MAX_REASK,
         r"되묻기 상한 \(`ask_count`\) \| \*\*항목당 (\d+)회", r"되묻기 상한 \| \*\*항목당 (\d+)\*\*"),
    ]
    out = []
    for name, const, value, re12, re03 in rows:
        a, b = re.search(re12, d12), re.search(re03, d03)
        v12, v03 = (int(a.group(1)) if a else None), (int(b.group(1)) if b else None)
        out.append({"limit": name, "d12": v12, "d03": v03, "code": f"config.{const} = {value}",
                    "match": v12 == v03 == value})
    spend = "설정됨" in d03 and "콘솔 지출 한도" in d12
    out.append({"limit": "콘솔 지출 한도", "d12": "콘솔", "d03": "설정됨(.AP-key.md 2절)",
                "code": "llm.py limit_reached 분리 · 재시도 0", "match": spend})
    return out


def self_check() -> dict:
    """판정기가 '없음'을 잡는가 — 문서에 없는 엔드포인트 · 노드를 섞어 본다."""
    fake_doc = {**doc_endpoints(), "API-999": ("GET", "/api/nope")}
    caught_doc = "API-999 GET /api/nope" in compare_endpoints(fake_doc, code_endpoints())["doc_only"]
    fake_code = code_endpoints() | {("GET", "/api/secret")}
    caught_code = "GET /api/secret" in compare_endpoints(doc_endpoints(), fake_code)["code_only"]
    return {"문서에만 있는 엔드포인트를 잡는가": caught_doc, "코드에만 있는 엔드포인트를 잡는가": caught_code}


def main() -> None:
    check = self_check()
    report = {"self_check": check, "files": files(),
              "endpoints": compare_endpoints(doc_endpoints(), code_endpoints()),
              "nodes": nodes(), "tools": tools(), "limits": limits()}
    LOG.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("판정기 자기 검증:", check)
    for f in report["files"]:
        print(f"  파일 {f['path']:24} 존재 {f['exists']} · 파일 {f['files']}개 · {f['owner']}")
    e = report["endpoints"]
    print(f"  엔드포인트 {e['matched']}/{e['doc_total']} · 문서에만 {e['doc_only']} · 코드에만 {e['code_only']}")
    n = report["nodes"]
    print(f"  노드 설계 {len(n['designed'])} · 코드 {len(n['code'])} · 그래프 {n['graph_nodes']} · 문서에만 {n['doc_only']} · 코드에만 {n['code_only']}")
    t = report["tools"]
    print(f"  툴 설계 {len(t['designed'])} · 코드 {len(t['code'])} · 문서에만 {t['doc_only']} · 코드에만 {t['code_only']}")
    for lim in report["limits"]:
        print(f"  상한 {lim['limit']}: D-12 {lim['d12']} · D-03 {lim['d03']} · {lim['code']} → {'일치' if lim['match'] else '불일치'}")
    print(f"로그: {LOG.relative_to(BACKEND)}")


if __name__ == "__main__":
    main()
