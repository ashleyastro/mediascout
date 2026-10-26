from __future__ import annotations

import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import db, demo, main
from app.results import journalists_for_scan


@pytest.fixture(scope="module")
def demo_client(tmp_path_factory):
    # The MCP session manager can start only once per process, so the app runs once per module.
    db.set_db_path(str(tmp_path_factory.mktemp("demo") / "demo.db"))
    db.init_db()
    demo_settings = replace(main.settings, demo_mode=True, app_password=None)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(main, "settings", demo_settings)
        mp.setattr("app.scanner.default_settings", demo_settings)
        mp.setattr(demo, "STEP_DELAY", 0)
        with TestClient(main.app) as client:
            yield client


def test_dataset_is_fictional():
    for art in demo.build_dataset():
        assert art["domain"].endswith(".example")
        assert art["url"].startswith("https://www.") and ".example/" in art["url"]


def test_sample_scan_is_ready(demo_client):
    cfg = demo_client.get("/api/config").json()
    assert cfg["demo_mode"] is True and cfg["email_enabled"] is False
    data = demo_client.get(f"/api/scans/{cfg['sample_scan_id']}/journalists").json()
    assert data["scan"]["status"] == "done"
    names = [j["name"] for j in data["journalists"]]
    assert "Avery Collins" in names and "Jordan Ellis" in names
    assert "Ben Adler" not in names  # only 2 stories, below the default of 5
    for j in data["journalists"]:
        assert j["article_count"] >= 5
        assert j["contact"]["email"].endswith(".example")
        assert not {"twitter", "linkedin", "bluesky"} & set(j["contact"])


def test_demo_scan_runs_without_network(demo_client):
    scan_id = demo_client.post("/api/scans", json={"industries": ["cybersecurity"], "regions": ["NA"]}).json()["id"]
    for _ in range(50):
        scan = demo_client.get(f"/api/scans/{scan_id}").json()
        if scan["status"] == "done":
            break
        time.sleep(0.05)
    assert scan["status"] == "done"
    assert scan["outlets_total"] == len([o for o in demo.DEMO_OUTLETS if o[2] == "NA"])
    names = [j["name"] for j in journalists_for_scan(scan_id)]
    assert names[0] == "Riley Chen"
    assert "Elena Fischer" not in names  # EU outlet, excluded by region


def test_risky_actions_are_blocked(demo_client):
    assert demo_client.post("/api/scans/sample/email", json={"to": ["a@b.com"]}).status_code == 403
    assert demo_client.post("/api/suppressions", json={"value": "Avery Collins"}).status_code == 403
    files = {"file": ("o.csv", "domain,name,region\nx.com,X,NA\n", "text/csv")}
    assert demo_client.post("/api/outlets/import", files=files).status_code == 403


def test_prune_keeps_protected_scan(tmp_path):
    db.set_db_path(str(tmp_path / "prune.db"))
    db.init_db()
    for i in range(5):
        db.create_scan(f"s{i}", {"min_articles": 5})
    db.prune_scans(2, protect=["s0"])
    ids = {s["id"] for s in db.list_scans()}
    assert len(ids) == 3 and "s0" in ids
