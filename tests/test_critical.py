"""Critical endpoint tests — analytics growth, watchlist CRUD, city meta toggle."""
import json
import pytest
from app import db
from models import Account, AnalyticsSnapshot, WatchlistSeite, WatchlistCityMeta, WatchlistFollowerSnapshot
from datetime import datetime, timedelta


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_db(app):
    """Isolate each test with a fresh DB state."""
    with app.app_context():
        db.session.query(WatchlistFollowerSnapshot).delete()
        db.session.query(WatchlistCityMeta).delete()
        db.session.query(WatchlistSeite).delete()
        db.session.query(AnalyticsSnapshot).delete()
        db.session.query(Account).delete()
        db.session.commit()
    yield
    with app.app_context():
        db.session.rollback()


@pytest.fixture
def test_account(app):
    with app.app_context():
        acc = Account(
            name='Testcity', handle='testcity',
            platform='Instagram', status='active',
            follower_count=1000,
        )
        db.session.add(acc)
        db.session.commit()
        return acc.id


@pytest.fixture
def test_entry(app):
    with app.app_context():
        e = WatchlistSeite(
            stadt='Frankfurt', ziel_typ='stadtseite',
            ziel_name='Frankfurt Main Page',
            platform='Instagram',
            seiten_status='nicht_gesucht',
        )
        db.session.add(e)
        db.session.commit()
        return e.id


# ── Analytics Growth ──────────────────────────────────────────────────────────

class TestAnalyticsGrowth:
    def test_returns_json_with_correct_shape(self, auth_client):
        r = auth_client.get('/api/analytics/growth?days=7')
        assert r.status_code == 200
        data = r.get_json()
        assert 'labels' in data and 'data' in data
        assert len(data['labels']) == 7
        assert len(data['data']) == 7

    def test_data_never_goes_negative(self, auth_client, app, test_account):
        """Growth totals must be non-negative even when snapshots are sparse."""
        with app.app_context():
            acc_id = test_account
            today = datetime.utcnow().date()
            # Only snapshot for today, not yesterday → yesterday should carry 0 not go negative
            db.session.add(AnalyticsSnapshot(
                account_id=acc_id,
                followers=1500,
                recorded_at=datetime.utcnow(),
            ))
            db.session.commit()

        r = auth_client.get('/api/analytics/growth?days=7')
        assert r.status_code == 200
        data = r.get_json()['data']
        assert all(v >= 0 for v in data), f"Negative value found: {data}"

    def test_data_does_not_dip_when_followers_only_increase(self, auth_client, app, test_account):
        """Per-account last-known-value fill must prevent artificial dips."""
        with app.app_context():
            acc_id = test_account
            today = datetime.utcnow().date()
            day1 = today - timedelta(days=3)
            day2 = today - timedelta(days=1)
            db.session.add(AnalyticsSnapshot(
                account_id=acc_id, followers=1000,
                recorded_at=datetime.combine(day1, datetime.min.time()),
            ))
            db.session.add(AnalyticsSnapshot(
                account_id=acc_id, followers=1200,
                recorded_at=datetime.combine(day2, datetime.min.time()),
            ))
            db.session.commit()

        r = auth_client.get('/api/analytics/growth?days=5')
        assert r.status_code == 200
        data = r.get_json()['data']
        # Check no dip: once a value rises it should not go back down
        for i in range(1, len(data)):
            if data[i - 1] > 0:
                assert data[i] >= data[i - 1] or data[i] == 0, (
                    f"Dip at index {i}: {data[i-1]} → {data[i]}"
                )

    def test_unauthenticated_returns_401(self, client):
        r = client.get('/api/analytics/growth?days=7')
        assert r.status_code == 401


# ── Watchlist CRUD ────────────────────────────────────────────────────────────

class TestWatchlistCRUD:
    def test_create_entry(self, auth_client):
        r = auth_client.post('/api/watchlist/stadtseiten',
            data=json.dumps({'stadt': 'München', 'ziel_name': 'München Seite', 'ziel_typ': 'stadtseite'}),
            content_type='application/json')
        assert r.status_code == 200
        body = r.get_json()
        assert body['ok'] is True
        assert isinstance(body['id'], int)

    def test_create_missing_required_fields_returns_400(self, auth_client):
        r = auth_client.post('/api/watchlist/stadtseiten',
            data=json.dumps({'ziel_name': 'Kein Stadt'}),
            content_type='application/json')
        assert r.status_code == 400

    def test_list_entries_by_city(self, auth_client, app, test_entry):
        r = auth_client.get('/api/watchlist/stadtseiten?stadt=Frankfurt')
        assert r.status_code == 200
        items = r.get_json()
        assert len(items) >= 1
        assert all(i['stadt'] == 'Frankfurt' for i in items)

    def test_update_entry_status(self, auth_client, app, test_entry):
        r = auth_client.put(f'/api/watchlist/stadtseiten/{test_entry}',
            data=json.dumps({'seiten_status': 'kontaktiert'}),
            content_type='application/json')
        assert r.status_code == 200
        assert r.get_json()['ok'] is True

    def test_soft_delete(self, auth_client, app, test_entry):
        r = auth_client.delete(f'/api/watchlist/stadtseiten/{test_entry}')
        assert r.status_code == 200
        # Entry should no longer appear in list
        r2 = auth_client.get('/api/watchlist/stadtseiten?stadt=Frankfurt')
        ids = [i['id'] for i in r2.get_json()]
        assert test_entry not in ids


# ── CityMeta Toggles ─────────────────────────────────────────────────────────

class TestCityMeta:
    def test_toggle_haben_creates_meta_row(self, auth_client, app, test_entry):
        r = auth_client.put('/api/watchlist/staedte/Frankfurt/haben',
            data=json.dumps({'haben': True}),
            content_type='application/json')
        assert r.status_code == 200
        assert r.get_json()['haben'] is True

        with app.app_context():
            meta = WatchlistCityMeta.query.filter_by(stadt='Frankfurt').first()
            assert meta is not None
            assert meta.haben_seite is True

    def test_toggle_haben_off(self, auth_client, app, test_entry):
        auth_client.put('/api/watchlist/staedte/Frankfurt/haben',
            data=json.dumps({'haben': True}), content_type='application/json')
        r = auth_client.put('/api/watchlist/staedte/Frankfurt/haben',
            data=json.dumps({'haben': False}), content_type='application/json')
        assert r.status_code == 200
        with app.app_context():
            meta = WatchlistCityMeta.query.filter_by(stadt='Frankfurt').first()
            assert meta.haben_seite is False

    def test_toggle_geplant(self, auth_client, app, test_entry):
        r = auth_client.put('/api/watchlist/staedte/Frankfurt/geplant',
            data=json.dumps({'geplant': True}),
            content_type='application/json')
        assert r.status_code == 200
        with app.app_context():
            meta = WatchlistCityMeta.query.filter_by(stadt='Frankfurt').first()
            assert meta.seite_geplant is True

    def test_staedte_returns_haben_from_city_meta(self, auth_client, app, test_entry):
        auth_client.put('/api/watchlist/staedte/Frankfurt/haben',
            data=json.dumps({'haben': True}), content_type='application/json')
        r = auth_client.get('/api/watchlist/staedte')
        assert r.status_code == 200
        cities = r.get_json()
        ffm = next((c for c in cities if c['stadt'] == 'Frankfurt'), None)
        assert ffm is not None
        assert ffm['haben_seite'] is True

    def test_toggle_unknown_city_returns_404(self, auth_client):
        r = auth_client.put('/api/watchlist/staedte/Unbekannt/haben',
            data=json.dumps({'haben': True}), content_type='application/json')
        assert r.status_code == 404
