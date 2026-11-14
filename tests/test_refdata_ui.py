import datetime
import gzip
import json
import os
import sys
import time

import pytest

from tzcheck import refdata
from tzcheck.updater import control, nightly

GOST_RECORDS = {
    "ГОСТ Р 53780-2010": {"designation": "ГОСТ Р 53780-2010", "title": "Лифты. Общие требования безопасности", "status": "Действует",
                          "via": "rst_api", "watch": True, "checked_at": "2026-11-13T17:57:34+00:00",
                          "check_url": "https://protect.gost.ru/gost?year=0&month=0&search=x"},
    "ГОСТ 28911-2015": {"designation": "ГОСТ 28911-2015", "title": "Лифты. Устройства управления", "status": "Заменён",
                        "replaced_by": "ГОСТ 28911-2024", "via": "protect_feed", "feed_month": "11.2026", "checked_at": "2026-11-13T17:57:34+00:00"},
}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    (d / "updates").mkdir(parents=True)
    with gzip.open(d / "gost.json.gz", "wt", encoding="utf-8") as f:
        json.dump({"source": "gost", "records": GOST_RECORDS}, f, ensure_ascii=False)
    (d / "gost_meta.json").write_text(json.dumps({"records_count": 2, "checked_at": "2026-11-13T17:58:16+00:00",
                                                  "via": "rst_api", "missing": []}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("TZCHECK_DATA_DIR", str(d))
    monkeypatch.setenv("TZCHECK_REFDATA_OFFLINE", "1")
    for k in ("TZCHECK_UPDATE_AT", "TZCHECK_UPDATE_FULL_WEEKDAY", "TZCHECK_UPDATE_ENABLED", "TZCHECK_UPDATE_RUNNER"):
        monkeypatch.delenv(k, raising=False)
    return d


def test_settings_defaults_from_env(data_dir, monkeypatch):
    monkeypatch.setenv("TZCHECK_UPDATE_AT", "02:15")
    monkeypatch.setenv("TZCHECK_UPDATE_FULL_WEEKDAY", "5")
    s = control.load_settings()
    assert (s["enabled"], s["at"], s["full_weekday"], s["saved_at"]) == (True, "02:15", 5, None)
    assert s["sources"] == control.SOURCE_ORDER


def test_settings_save_survives_reload(data_dir):
    control.save_settings({"enabled": False, "at": "23:05", "full_weekday": -1, "sources": ["gost", "laws"]})
    s = control.load_settings()
    assert (s["enabled"], s["at"], s["full_weekday"], s["sources"]) == (False, "23:05", -1, ["laws", "gost"])
    assert s["saved_at"]
    assert os.path.exists(data_dir / "control" / "schedule.json")
    assert control.plan(s) is None


@pytest.mark.parametrize("bad", [{"at": "25:00"}, {"at": "4:30"}, {"full_weekday": 9}, {"sources": []}, {"sources": ["nope"]}])
def test_settings_validation(data_dir, bad):
    with pytest.raises(ValueError):
        control.save_settings(bad)


def test_plan_marks_full_weekday():
    s = {"enabled": True, "at": "04:30", "full_weekday": 6}
    p = control.plan(s, datetime.datetime(2026, 11, 14, 12, 0))
    assert p == {"at": "2026-11-15T04:30:00", "full": True}
    p = control.plan(s, datetime.datetime(2026, 11, 13, 1, 0))
    assert p == {"at": "2026-11-13T04:30:00", "full": False}


def test_scheduler_takes_request_and_runs_on_time(data_dir, monkeypatch):
    ran = []
    monkeypatch.setattr(control, "execute", lambda req, runner, echo=False: ran.append((req, runner)) or {"code": 0, "outcome": "ok"})
    monkeypatch.setattr(control, "write_heartbeat", lambda info: None)
    control.save_settings({"at": "04:30", "full_weekday": 6, "sources": ["gost"]})
    s = nightly.Scheduler()
    control.queue_request(control.make_request(["gost"], False))
    assert s.tick(datetime.datetime(2026, 11, 14, 12, 0)) == "manual"
    assert ran[-1][0]["sources"] == ["gost"] and ran[-1][1] == "updater"
    assert control.pending_request() is None
    assert s.tick(datetime.datetime(2026, 11, 14, 12, 1)) is None
    assert s.tick(datetime.datetime(2026, 11, 15, 4, 31)) == "schedule"
    assert ran[-1][0]["trigger"] == "schedule" and ran[-1][0]["full"] is True and ran[-1][0]["sources"] == ["gost"]
    control.save_settings({"at": "05:00", "full_weekday": 6})
    s.tick(datetime.datetime(2026, 11, 15, 4, 40))
    assert s.planned == datetime.datetime(2026, 11, 15, 5, 0)


def test_execute_writes_state_log_history(data_dir, monkeypatch):
    monkeypatch.setattr(control, "command", lambda req: [sys.executable, "-c", "print('[gost] обновлено: 2 записи')"])
    st = control.execute(control.make_request(["gost"], False), "app")
    assert st["state"] == "done" and st["code"] == 0
    assert any("[gost] обновлено" in x for x in control.log_tail())
    assert control.history()[0]["code"] == 0
    assert control.busy() is None


def test_lock_means_busy(data_dir):
    (data_dir / "updates" / ".lock").write_text("1")
    assert "уже идёт" in control.busy()


def test_note_gost_feeds_targets(data_dir):
    from tzcheck.updater import gost
    refdata._noted.clear()
    assert refdata.note_gost("ГОСТ 1234-2020") is True
    assert refdata.note_gost("ГОСТ 1234-2020") is False
    refdata.note_gost("ГОСТ 5678-2021", by="manual")
    groups = gost.target_groups(GOST_RECORDS)
    assert "ГОСТ 1234-2020" in groups["tz_offline"] and "ГОСТ 5678-2021" in groups["manual"]
    assert "ГОСТ Р 53780-2010" in groups["watched_before"]


@pytest.fixture
def client(data_dir):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from tzcheck.api import app
    return TestClient(app)


def test_refdata_page_and_status(client):
    r = client.get("/refdata")
    assert r.status_code == 200
    for text in ("Справочники", "Каталог Росстандарта", "Обновить сейчас", "Как обновляется", "Расписание", "Словарь пуст"):
        assert text in r.text
    assert "—" not in r.text.split("<script>")[0]
    j = client.get("/api/refdata/status").json()
    assert [x["name"] for x in j["sources"]] == control.SOURCE_ORDER
    assert j["gost"]["known"] == 2 and j["mode"]["via"] == "app" and j["mode"]["offline"] is True
    assert '/refdata' in client.get("/").text


def test_schedule_api(client):
    assert client.post("/api/refdata/schedule", json={"at": "99:99"}).status_code == 400
    j = client.post("/api/refdata/schedule", json={"enabled": True, "at": "03:10", "full_weekday": 2, "sources": ["gost"]}).json()
    assert j["settings"]["at"] == "03:10" and j["next"]["at"].endswith("03:10:00")
    assert client.get("/api/refdata/schedule").json()["settings"]["sources"] == ["gost"]


def test_gost_search_and_watch(client):
    j = client.get("/api/refdata/gost", params={"q": "гост р 53780-2010"}).json()
    assert j["items"][0]["designation"] == "ГОСТ Р 53780-2010" and j["items"][0]["status"] == "Действует"
    j = client.get("/api/refdata/gost", params={"q": "28911"}).json()
    assert j["items"][0]["inactive"] is True and j["items"][0]["replaced_by"] == "ГОСТ 28911-2024"
    j = client.get("/api/refdata/gost", params={"q": "ГОСТ 9999-2001"}).json()
    assert j["items"] == [] and j["can_watch"] is True
    assert client.post("/api/refdata/gost/watch", json={"designation": "лифт"}).status_code == 400
    assert client.post("/api/refdata/gost/watch", json={"designation": "ГОСТ 9999-2001"}).json()["key"] == "ГОСТ 9999-2001"
    assert client.get("/api/refdata/gost", params={"q": "ГОСТ 9999-2001"}).json()["requested"] is True


def test_update_goes_to_updater_in_docker_mode(client, data_dir, monkeypatch):
    monkeypatch.setenv("TZCHECK_UPDATE_RUNNER", "updater")
    assert client.post("/api/refdata/update", json={"sources": ["nope"]}).status_code == 400
    r = client.post("/api/refdata/update", json={"sources": ["gost"], "full": False})
    assert r.status_code == 202 and "не отвечает" in r.json()["note"]
    req = json.loads((data_dir / "control" / "request.json").read_text(encoding="utf-8"))
    assert req["sources"] == ["gost"]
    control.write_heartbeat({"next_at": None, "settings": control.load_settings()})
    r = client.post("/api/refdata/update", json={"sources": ["laws"]})
    assert r.status_code == 202 and "передан сервису updater" in r.json()["note"]
    (data_dir / "updates" / ".lock").write_text("1")
    assert client.post("/api/refdata/update", json={}).status_code == 409


def test_update_runs_locally_without_updater(client, data_dir, monkeypatch):
    monkeypatch.setattr(control, "command", lambda req: [sys.executable, "-c", "print('готово')"])
    r = client.post("/api/refdata/update", json={"sources": ["gost"]})
    assert r.status_code == 202 and r.json()["state"]["runner"] == "app"
    for _ in range(100):
        st = client.get("/api/refdata/run").json()["state"]
        if st["state"] == "done":
            break
        time.sleep(0.1)
    assert st["state"] == "done" and st["code"] == 0
