"""Tests for app-scoped artifacts, knowledge, and local Chroma RAG."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


ORANGE_URL = (
    "https://opensource-demo.orangehrmlive.com/web/index.php/auth/login"
)
ORANGE_APP = "opensource-demo.orangehrmlive.com"


def test_app_id_from_url_strips_www_and_port():
    from src.utils.app_registry import app_id_from_url

    assert app_id_from_url(ORANGE_URL) == ORANGE_APP
    assert (
        app_id_from_url("https://www.Example.com:8443/path") == "example.com"
    )
    assert app_id_from_url("https://opensource-demo.orangehrmlive.com") == ORANGE_APP
    assert app_id_from_url("") == ""


def test_slug_flow_and_resolve():
    from src.utils.app_registry import resolve_app_and_flow, slug_flow

    assert slug_flow("Create Claim") == "create-claim"
    assert slug_flow("create-claim.json") == "create-claim"
    app, flow = resolve_app_and_flow(
        target_url=ORANGE_URL,
        label="Create Claim",
    )
    assert app == ORANGE_APP
    assert flow == "create-claim"


def test_ensure_workspace_idempotent(tmp_path, monkeypatch):
    from src.utils import app_registry, workspace
    from src.utils import rag_store

    monkeypatch.setattr(app_registry, "artifacts_root", lambda: tmp_path / "artifacts")
    monkeypatch.setattr(rag_store, "artifacts_root", lambda: tmp_path / "artifacts")
    monkeypatch.setattr(rag_store, "_rag_enabled", lambda: False)
    rag_store.reset_client_for_tests()
    workspace._INITIALIZED = False

    root1 = workspace.ensure_workspace()
    root2 = workspace.ensure_workspace()
    assert root1 == root2
    for rel in ("k6", "recordings", "knowledge", "rag/chroma"):
        assert (root1 / rel).is_dir()


def test_recording_lands_under_domain(tmp_path, monkeypatch):
    from src.utils import app_registry, recording_store

    rec_root = tmp_path / "recordings"
    monkeypatch.setenv("NFE_RECORDINGS_DIR", str(rec_root))
    monkeypatch.setattr(app_registry, "artifacts_root", lambda: tmp_path / "artifacts")
    # Avoid writing knowledge/RAG during ensure_app_dirs
    monkeypatch.setattr(
        app_registry,
        "ensure_app_dirs",
        lambda app_id: (tmp_path / "artifacts" / "knowledge" / app_id),
    )
    (tmp_path / "artifacts" / "knowledge" / ORANGE_APP / "flows").mkdir(
        parents=True, exist_ok=True
    )
    (rec_root / ORANGE_APP).mkdir(parents=True, exist_ok=True)

    meta = recording_store.save_watch_me_recording(
        target_url=ORANGE_URL,
        user_journey_steps=[{"type": "click", "selectors": ["button"]}],
        run_records=[{"run_id": 1, "network_requests": []}],
        label="Create Claim",
    )
    expected = rec_root / ORANGE_APP / "create-claim.json"
    assert Path(meta["path"]) == expected.resolve()
    assert expected.is_file()
    data = json.loads(expected.read_text(encoding="utf-8"))
    assert data["app"] == ORANGE_APP
    assert data["flow"] == "create-claim"


def test_legacy_flat_recording_fallback(tmp_path, monkeypatch):
    from src.utils import recording_store

    rec_root = tmp_path / "recordings"
    monkeypatch.setenv("NFE_RECORDINGS_DIR", str(rec_root))
    rec_root.mkdir(parents=True)
    legacy = rec_root / f"{ORANGE_APP}.json"
    legacy.write_text(
        json.dumps(
            {
                "target_url": ORANGE_URL,
                "user_journey_steps": [{"type": "click"}],
                "run_records": [{"run_id": 1}],
            }
        ),
        encoding="utf-8",
    )
    path = recording_store.resolve_recording_path(ORANGE_URL)
    assert path is not None
    assert path.resolve() == legacy.resolve()


def test_k6_save_under_app_flow(tmp_path, monkeypatch):
    from src.utils import app_registry, artifacts

    k6_root = tmp_path / "k6"
    monkeypatch.setenv("NFE_ARTIFACTS_DIR", str(k6_root))
    monkeypatch.setattr(app_registry, "artifacts_root", lambda: tmp_path / "artifacts")
    monkeypatch.setattr(
        app_registry,
        "ensure_app_dirs",
        lambda app_id: (tmp_path / "artifacts" / "knowledge" / app_id),
    )
    (k6_root / ORANGE_APP).mkdir(parents=True, exist_ok=True)

    meta = artifacts.save_k6_script(
        "export default function () {}",
        target_url=ORANGE_URL,
        flow="create-claim",
    )
    expected = k6_root / ORANGE_APP / "create-claim.js"
    assert Path(meta["path"]) == expected.resolve()
    assert meta["app"] == ORANGE_APP
    assert meta["flow"] == "create-claim"


def test_knowledge_upsert_writes_markdown(tmp_path, monkeypatch):
    from src.utils import app_registry, knowledge_store, rag_store

    monkeypatch.setattr(app_registry, "artifacts_root", lambda: tmp_path / "artifacts")
    monkeypatch.setattr(knowledge_store, "artifacts_root", lambda: tmp_path / "artifacts")
    monkeypatch.setattr(rag_store, "artifacts_root", lambda: tmp_path / "artifacts")
    monkeypatch.setattr(rag_store, "_rag_enabled", lambda: False)
    rag_store.reset_client_for_tests()

    path = knowledge_store.upsert_flow_card(
        ORANGE_APP,
        "create-claim",
        target_url=ORANGE_URL,
        recording_path=f"artifacts/recordings/{ORANGE_APP}/create-claim.json",
        k6_path=f"artifacts/k6/{ORANGE_APP}/create-claim.js",
        txn_names=["Login", "Create Claim"],
        workload_source="analyse_smoke",
        smoke_status="passed",
        step_count=5,
    )
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert ORANGE_APP in text
    assert "create-claim" in text
    assert "Login" in text
    assert knowledge_store.read_flow(ORANGE_APP, "create-claim")
    assert "create-claim" in knowledge_store.list_flows(ORANGE_APP)


def test_chroma_upsert_and_query(tmp_path, monkeypatch):
    pytest.importorskip("chromadb")
    from src.utils import app_registry, rag_store

    monkeypatch.setattr(app_registry, "artifacts_root", lambda: tmp_path / "artifacts")
    monkeypatch.setattr(rag_store, "artifacts_root", lambda: tmp_path / "artifacts")
    monkeypatch.setenv("NFE_RAG_ENABLED", "true")
    monkeypatch.setenv("NFE_RAG_FAKE_EMBEDDINGS", "true")
    monkeypatch.setattr(rag_store, "_rag_enabled", lambda: True)
    rag_store.reset_client_for_tests()

    knowledge = tmp_path / "artifacts" / "knowledge" / ORANGE_APP / "flows"
    knowledge.mkdir(parents=True)
    md_path = knowledge / "create-claim.md"
    md_path.write_text(
        "# Flow: create-claim\n\n## Artifacts\n\nOrangeHRM create claim flow for claims.\n",
        encoding="utf-8",
    )

    n = rag_store.upsert_markdown(
        ORANGE_APP,
        flow="create-claim",
        kind="flow",
        text=md_path.read_text(encoding="utf-8"),
        path=str(md_path),
    )
    assert n >= 1

    hits = rag_store.query("create claim OrangeHRM", app=ORANGE_APP, top_k=2)
    assert hits
    assert any(
        "create-claim" in (h.get("text") or "").lower()
        or "claim" in (h.get("text") or "").lower()
        for h in hits
    )
    assert hits[0]["metadata"].get("app") == ORANGE_APP


def test_rag_soft_fail_when_disabled(tmp_path, monkeypatch):
    from src.utils import app_registry, rag_store

    monkeypatch.setattr(app_registry, "artifacts_root", lambda: tmp_path / "artifacts")
    monkeypatch.setattr(rag_store, "artifacts_root", lambda: tmp_path / "artifacts")
    monkeypatch.setattr(rag_store, "_rag_enabled", lambda: False)
    rag_store.reset_client_for_tests()

    n = rag_store.upsert_markdown(
        ORANGE_APP,
        flow="x",
        kind="flow",
        text="# hello",
        path=str(tmp_path / "artifacts" / "knowledge" / ORANGE_APP / "flows" / "x.md"),
    )
    assert n == 0
    assert rag_store.query("hello", app=ORANGE_APP) == []


def test_extract_watch_me_label():
    from src.utils.app_registry import extract_watch_me_label

    assert (
        extract_watch_me_label(
            f"watch me create-claim {ORANGE_URL}"
        )
        == "create-claim"
    )
    assert (
        extract_watch_me_label(f'watch me "Create Claim" {ORANGE_URL}')
        == "Create Claim"
    )
