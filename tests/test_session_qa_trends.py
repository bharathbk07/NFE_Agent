"""Tests for cache-first session QA, run history, and trend helpers."""

from __future__ import annotations

import json


def _patch_artifacts(monkeypatch, tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    import src.utils.app_registry as app_registry
    import src.utils.knowledge_store as knowledge_store
    import src.utils.perf_trend as perf_trend
    import src.utils.rag_store as rag_store

    monkeypatch.setattr(app_registry, "artifacts_root", lambda: root)
    monkeypatch.setattr(knowledge_store, "artifacts_root", lambda: root)
    monkeypatch.setattr(rag_store, "artifacts_root", lambda: root)
    monkeypatch.setattr(perf_trend, "artifacts_root", lambda: root)
    monkeypatch.setenv("NFE_RAG_ENABLED", "0")
    return root


def test_summarize_includes_k6_smoke(monkeypatch, tmp_path):
    _patch_artifacts(monkeypatch, tmp_path)
    from src.agents.analysis_qa_agent import _summarize_analysis_context

    state = {
        "target_url": "https://example.com/",
        "app": "example.com",
        "flow": "default",
        "performance_test_output": {
            "k6_smoke": {
                "ok": False,
                "summary": "checks failed",
                "failed_checks": ["Login POST expect status"],
                "status_counts": {"401": 2},
                "summary_json": "",
            },
            "confluence": {"published": True, "run_url": "https://wiki.example/run"},
            "load_test_ir": {"workload": {"vus": 5, "pacing_s": 30}},
            "artifacts": {"k6_script": "x", "k6_file": {"path": "/tmp/x.js"}},
        },
    }
    text = _summarize_analysis_context(state)
    assert "k6_smoke" in text
    assert "checks failed" in text
    assert "Login POST expect status" in text
    assert "pacing_s" in text
    assert "run_url" in text


def test_ingest_and_list_run_history(monkeypatch, tmp_path):
    _patch_artifacts(monkeypatch, tmp_path)
    from src.utils.app_registry import ensure_app_dirs
    from src.utils.knowledge_store import ingest_run_history, list_run_history

    ensure_app_dirs("example.com")
    path = ingest_run_history(
        "example.com",
        "default",
        kpis={
            "smoke_ok": True,
            "p95_ms": 120.5,
            "fail_rate": 0.01,
            "checks_rate": 0.99,
            "source": "test",
        },
        workload_source="unit",
        target_url="https://example.com/",
    )
    assert path.is_file()
    assert "p95_ms" in path.read_text(encoding="utf-8")

    ingest_run_history(
        "example.com",
        "default",
        kpis={
            "smoke_ok": False,
            "p95_ms": 200.0,
            "fail_rate": 0.05,
            "source": "test",
        },
        workload_source="unit",
    )
    rows = list_run_history("example.com", "default", limit=5)
    assert len(rows) >= 2
    assert float(rows[0].get("p95_ms")) == 200.0


def test_build_trend_table_deltas():
    from src.utils.perf_trend import build_trend_table

    md = build_trend_table(
        [
            {
                "run_id": "r2",
                "timestamp": "2026-01-02",
                "smoke_ok": True,
                "p95_ms": 150,
                "fail_rate": 0.02,
                "source": "local",
            },
            {
                "run_id": "r1",
                "timestamp": "2026-01-01",
                "smoke_ok": True,
                "p95_ms": 100,
                "fail_rate": 0.01,
                "source": "local",
            },
        ]
    )
    assert "r2" in md and "r1" in md
    assert "Latest vs previous" in md


def test_gather_evidence_local_without_confluence(monkeypatch, tmp_path):
    _patch_artifacts(monkeypatch, tmp_path)
    from src.utils.app_registry import ensure_app_dirs
    from src.utils.knowledge_store import ingest_run_history
    from src.utils.perf_evidence import gather_evidence_for_question

    ensure_app_dirs("example.com")
    for p95 in (100, 140, 180):
        ingest_run_history(
            "example.com",
            "checkout",
            kpis={"smoke_ok": True, "p95_ms": p95, "fail_rate": 0.0, "source": "test"},
        )

    # Avoid live Confluence even on thin-miss paths
    monkeypatch.setattr(
        "src.utils.perf_evidence.ConfluenceEvidenceSource.sync",
        lambda self, *a, **k: [],
    )

    evidence = gather_evidence_for_question(
        "show me the p95 trend",
        app="example.com",
        flow="checkout",
        target_url="https://example.com/",
    )
    assert "knowledge_markdown" in evidence["sources"]
    assert "confluence_sync" not in evidence["sources"]
    assert "180" in evidence["trend_markdown"] or "140" in evidence["trend_markdown"]


def test_monitoring_stub_note(monkeypatch, tmp_path):
    _patch_artifacts(monkeypatch, tmp_path)
    from src.utils.app_registry import ensure_app_dirs
    from src.utils.perf_evidence import gather_evidence_for_question

    ensure_app_dirs("example.com")
    monkeypatch.setattr(
        "src.utils.perf_evidence.ConfluenceEvidenceSource.sync",
        lambda self, *a, **k: [],
    )
    evidence = gather_evidence_for_question(
        "get trend from monitoring grafana",
        app="example.com",
        flow="default",
    )
    assert any("onitoring" in n for n in evidence["notes"])


def test_extract_kpis_from_summary_json(monkeypatch, tmp_path):
    root = _patch_artifacts(monkeypatch, tmp_path)
    from src.utils.perf_trend import extract_kpis_from_summary_json

    summary = {
        "metrics": {
            "http_req_failed": {"values": {"rate": 0.02}},
            "http_req_duration": {"values": {"p(95)": 321.5}},
            "http_reqs": {"values": {"count": 40}},
            "checks": {"values": {"rate": 0.98}},
        }
    }
    path = root / "k6" / "summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary), encoding="utf-8")
    kpis = extract_kpis_from_summary_json(str(path))
    assert kpis.get("p95_ms") == 321.5
    assert kpis.get("fail_rate") == 0.02
