import time

import app


def _insert(created_at, marker):
    with app.connect_db() as db:
        return app.execute_db(
            db,
            """INSERT INTO requests (service, address, preferred_time, customer_contact, notes, status, created_at, source)
               VALUES ('Тест', ?, 'завтра', 'retention-test', '', 'Новая', ?, 'site')""",
            (marker, created_at),
            returning_id=True,
        )


def _exists(request_id):
    with app.connect_db() as db:
        return app.execute_db(db, "SELECT id FROM requests WHERE id = ?", (request_id,)).fetchone() is not None


def test_old_requests_are_deleted_and_fresh_ones_kept(monkeypatch):
    monkeypatch.setattr(app, "REQUEST_RETENTION_DAYS", 365)
    now = time.time()
    old = _insert(int(now) - 366 * 86400, "retention-old")
    fresh = _insert(int(now) - 10 * 86400, "retention-fresh")
    assert app.purge_old_requests(now, force=True) >= 1
    assert not _exists(old) and _exists(fresh)


def test_retention_zero_keeps_everything(monkeypatch):
    monkeypatch.setattr(app, "REQUEST_RETENTION_DAYS", 0)
    ancient = _insert(1, "retention-ancient")
    assert app.purge_old_requests(time.time(), force=True) == 0
    assert _exists(ancient)


def test_purge_runs_at_most_once_an_hour(monkeypatch):
    monkeypatch.setattr(app, "REQUEST_RETENTION_DAYS", 365)
    now = time.time()
    app.purge_old_requests(now, force=True)
    old = _insert(int(now) - 400 * 86400, "retention-throttle")
    assert app.purge_old_requests(now + 60) == 0 and _exists(old)
    assert app.purge_old_requests(now + 3700) >= 1 and not _exists(old)
