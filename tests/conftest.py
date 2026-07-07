"""Test configuration — uses in-memory SQLite so it never touches the real DB."""
import os
import pytest

# Must be set before importing app so SQLAlchemy picks it up
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
os.environ.setdefault('FLASK_TESTING', '1')

from app import app as flask_app, db
from models import User, Account, AnalyticsSnapshot
from datetime import datetime


@pytest.fixture(scope='session')
def app():
    flask_app.config.update({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'WTF_CSRF_ENABLED': False,
    })
    with flask_app.app_context():
        db.create_all()
        # Seed a test user
        if not User.query.filter_by(username='testuser').first():
            u = User(username='testuser', email='test@test.de', role='admin')
            u.set_password('testpass')
            db.session.add(u)
            db.session.commit()
    yield flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_client(app, client):
    """Test client with session pre-set to bypass login_required."""
    with app.app_context():
        user = User.query.filter_by(username='testuser').first()
        user_id = user.id if user else 1
    with client.session_transaction() as sess:
        sess['user_id'] = user_id
        sess['user_role'] = 'admin'
        sess['user_name'] = 'testuser'
    return client
