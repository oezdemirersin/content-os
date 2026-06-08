import os
import json
import csv
import io
import threading
import difflib
import mimetypes
import uuid
from datetime import datetime, timedelta
from flask import (Flask, render_template, request, redirect, url_for,
                   jsonify, flash, send_from_directory)
from werkzeug.utils import secure_filename
from functools import wraps
from flask import session
from models import (db, Platform, Category, Label, TeamMember, Account, AIConfig,
                    ContentItem, MediaItem, ScheduledPost, AnalyticsSnapshot,
                    AutomationRule, AutomationRunLog, SystemAlert, User, ActivityLog,
                    AccountGroup, ContentTemplate, ContentComment,
                    HashtagSet, NotificationSettings, AppNotification, RecurringPost,
                    AccountAutomationProfile, AppSettings)
import smtplib
from email.mime.text import MIMEText
import calendar as cal_mod_global
from sqlalchemy import func
from sqlalchemy.orm import joinedload, selectinload

app = Flask(__name__, template_folder='templates/cms')

# ── Emergency-Pause Cache ─────────────────────────────────────────────────────
# Shared by inject_globals() (every request) AND the scheduler (every 60 s).
# Avoids one DB round-trip per request; refreshes every 10 s automatically.
import time as _time_mod
_ep_cache: dict = {'paused': False, 'expires': 0.0}

def _is_emergency_paused() -> bool:
    """Return True if Notfall-Pause is active. Result cached for 10 s."""
    now = _time_mod.monotonic()
    if now > _ep_cache['expires']:
        try:
            ep = AppSettings.query.filter_by(key='emergency_pause').first()
            _ep_cache['paused']  = bool(ep and ep.value == '1')
            _ep_cache['expires'] = now + 10
        except Exception as e:
            app.logger.warning('_is_emergency_paused: DB error — %s', e)
    return _ep_cache['paused']

def _invalidate_ep_cache() -> None:
    """Force the next call to _is_emergency_paused() to re-read the DB."""
    _ep_cache['expires'] = 0.0

@app.context_processor
def inject_globals():
    return {'now': datetime.utcnow, 'emergency_pause_active': _is_emergency_paused()}

@app.template_filter('fmt_followers')
def fmt_followers(n):
    """Zeigt Follower-Zahlen exakt mit Punkt-Trennung, ab 1M abgekürzt."""
    try:
        n = int(n or 0)
    except (ValueError, TypeError):
        return '0'
    if n >= 1_000_000:
        return f'{n/1_000_000:.1f}M'.replace('.', ',')
    # Exakte Zahl mit Tausender-Punkt: 1600 → 1.600
    return f'{n:,}'.replace(',', '.')
_secret = os.environ.get('SECRET_KEY') or 'content-os-secret-2024-v2'
app.config['SECRET_KEY'] = _secret
app.secret_key = _secret
# Render gibt postgres:// zurück, SQLAlchemy braucht postgresql://
_db_url = os.environ.get('DATABASE_URL', 'sqlite:///content_os.db')
if _db_url.startswith('postgres://'):
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    # pool_pre_ping entfernt: war +50-100ms auf JEDE Anfrage (Supabase-Roundtrip)
    # pool_recycle=300 reicht um stale connections zu verhindern
    'pool_recycle': 300,
    'pool_size': 5,
    'max_overflow': 10,
    'connect_args': {'sslmode': 'require'} if _db_url.startswith('postgresql://') else {},
}
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'mp4', 'mov', 'avi', 'webm', 'pdf'}

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db.init_app(app)

# ─────────────────────── CLOUDINARY ───────────────────────
import cloudinary
import cloudinary.uploader

_cloudinary_url = os.environ.get('CLOUDINARY_URL')
if _cloudinary_url:
    cloudinary.config(cloudinary_url=_cloudinary_url)

def _cloudinary_upload(file_obj, original_filename):
    """Upload file to Cloudinary (folder: content-os/).
    Returns Cloudinary result dict on success, None if Cloudinary not configured."""
    if not _cloudinary_url:
        return None
    ext = original_filename.rsplit('.', 1)[-1].lower() if '.' in original_filename else 'bin'
    resource_type = 'video' if ext in {'mp4', 'mov', 'avi', 'webm'} else 'image'
    try:
        result = cloudinary.uploader.upload(
            file_obj,
            folder='content-os',
            resource_type=resource_type,
            use_filename=False,
            unique_filename=True,
        )
        return result
    except Exception as e:
        app.logger.error(f'Cloudinary upload error: {e}')
        return None

def _cloudinary_delete(public_id, resource_type='image'):
    """Delete asset from Cloudinary by public_id."""
    if not _cloudinary_url or not public_id:
        return
    try:
        cloudinary.uploader.destroy(public_id, resource_type=resource_type)
    except Exception as e:
        app.logger.error(f'Cloudinary delete error: {e}')


# ─────────────────────── AUTH ───────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated


# ── Global auth guard — schützt ALLE Routen außer Login/Logout/Static ──
PUBLIC_ENDPOINTS = {'login', 'logout', 'static'}

@app.before_request
def global_auth_guard():
    if request.endpoint and request.endpoint not in PUBLIC_ENDPOINTS:
        if not session.get('user_id'):
            return redirect(url_for('login', next=request.path))


def current_user():
    uid = session.get('user_id')
    return User.query.get(uid) if uid else None


def log_activity(action, description, entity_type=None, entity_id=None):
    try:
        uid = session.get('user_id')
        db.session.add(ActivityLog(
            user_id=uid, action=action, description=description,
            entity_type=entity_type, entity_id=entity_id
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()


app.jinja_env.globals['current_user'] = current_user


# ─────────────────────── SEED ───────────────────────

def seed_data():
    if Platform.query.first():
        return

    # Default admin user
    admin = User(username='admin', email='admin@content-os.de', role='owner')
    admin.set_password('admin123')
    db.session.add(admin)
    db.session.flush()

    platforms = [
        Platform(name='Instagram', icon='instagram', color='#E1306C'),
        Platform(name='TikTok', icon='tiktok', color='#010101'),
        Platform(name='YouTube', icon='youtube', color='#FF0000'),
        Platform(name='Facebook', icon='facebook', color='#1877F2'),
    ]
    for p in platforms:
        db.session.add(p)

    categories = [
        Category(name='Stadt-News', color='#3b82f6', icon='newspaper'),
        Category(name='Stadt-Memes', color='#f59e0b', icon='face-laugh'),
        Category(name='Beichten', color='#8b5cf6', icon='heart'),
        Category(name='Lebensmittelwarnungen', color='#ef4444', icon='triangle-exclamation'),
        Category(name='Reels', color='#ec4899', icon='film'),
        Category(name='Krypto', color='#f97316', icon='bitcoin-sign'),
        Category(name='Deals', color='#10b981', icon='tag'),
        Category(name='Sport', color='#06b6d4', icon='futbol'),
        Category(name='Unterhaltung', color='#a855f7', icon='star'),
    ]
    for c in categories:
        db.session.add(c)

    db.session.flush()

    labels_data = ['Hessen', 'Bayern', 'NRW', 'Berlin', 'Frankfurt', 'München',
                   'Hamburg', 'Automatisiert', 'Manuell', 'Wichtig', 'Archiv',
                   'Deutschland', 'Rückruf', 'Breaking', 'Trending', 'Viral']
    for l in labels_data:
        db.session.add(Label(name=l, color='#6366f1'))

    owner = TeamMember(name='Admin', email='admin@content-os.de', role='owner')
    db.session.add(owner)
    db.session.flush()

    instagram = Platform.query.filter_by(name='Instagram').first()
    cats = {c.name: c for c in Category.query.all()}

    accounts_data = [
        {'name': 'Frankfurt News', 'handle': '@frankfurt.news', 'cat': 'Stadt-News', 'f': 45200, 'auto': 3, 'prio': 'high'},
        {'name': 'München News', 'handle': '@muenchen.news', 'cat': 'Stadt-News', 'f': 78300, 'auto': 2, 'prio': 'high'},
        {'name': 'Berlin Daily', 'handle': '@berlin.daily', 'cat': 'Stadt-News', 'f': 124500, 'auto': 3, 'prio': 'critical'},
        {'name': 'Hamburg Aktuell', 'handle': '@hamburg.aktuell', 'cat': 'Stadt-News', 'f': 31200, 'auto': 1, 'prio': 'medium'},
        {'name': 'Frankfurt Memes', 'handle': '@frankfurt.memes', 'cat': 'Stadt-Memes', 'f': 89400, 'auto': 2, 'prio': 'high'},
        {'name': 'München Memes', 'handle': '@muenchen.memes', 'cat': 'Stadt-Memes', 'f': 156700, 'auto': 2, 'prio': 'high'},
        {'name': 'Lebensmittel Warnung DE', 'handle': '@lebensmittel.warnung', 'cat': 'Lebensmittelwarnungen', 'f': 234100, 'auto': 4, 'prio': 'critical'},
        {'name': 'Beichten Frankfurt', 'handle': '@beichten.ffm', 'cat': 'Beichten', 'f': 67800, 'auto': 1, 'prio': 'medium'},
        {'name': 'Köln News', 'handle': '@koeln.news', 'cat': 'Stadt-News', 'f': 52300, 'auto': 2, 'prio': 'medium'},
        {'name': 'Stuttgart Aktuell', 'handle': '@stuttgart.aktuell', 'cat': 'Stadt-News', 'f': 28900, 'auto': 1, 'prio': 'low', 'status': 'paused'},
    ]

    for i, a in enumerate(accounts_data):
        acc = Account(
            name=a['name'], handle=a['handle'],
            platform_id=instagram.id,
            category_id=cats[a['cat']].id,
            follower_count=a['f'],
            automation_level=a['auto'],
            priority=a['prio'],
            status=a.get('status', 'active'),
            team_member_id=owner.id,
        )
        db.session.add(acc)
        db.session.flush()
        db.session.add(AIConfig(account_id=acc.id))

        base = a['f']
        for day in range(30, 0, -1):
            db.session.add(AnalyticsSnapshot(
                account_id=acc.id,
                followers=max(0, base - (day * int(base * 0.001))),
                recorded_at=datetime.utcnow() - timedelta(days=day),
                engagement_rate=round(3.5 + (i % 3) * 0.5, 2)
            ))

        # Vary post count so some accounts have low stock
        post_count = [10, 5, 14, 2, 8, 12, 7, 1, 9, 3][i]
        for day in range(1, post_count + 1):
            db.session.add(ScheduledPost(
                account_id=acc.id,
                caption=f'Geplanter Post #{day} für {a["name"]}',
                post_type='feed',
                status='scheduled',
                scheduled_at=datetime.utcnow() + timedelta(days=day),
                created_by_id=owner.id
            ))

    # Demo AutomationRules
    db.session.flush()
    all_accs = Account.query.all()
    rules = [
        AutomationRule(account_id=all_accs[0].id, name='Frankfurt RSS', rule_type='rss', active=True,
                       source_config=json.dumps({'url': 'https://www.faz.net/rss/aktuell/', 'keywords': ['Frankfurt']}),
                       run_interval_minutes=60),
        AutomationRule(account_id=all_accs[6].id, name='BVL Lebensmittelwarnungen', rule_type='food_warning', active=True,
                       source_config=json.dumps({'url': 'https://www.bvl.bund.de/rss', 'keywords': []}),
                       run_interval_minutes=30),
        AutomationRule(name='Deutschland News', rule_type='city_news', active=False,
                       source_config=json.dumps({'sources': ['dpa', 'apa']}),
                       run_interval_minutes=120),
    ]
    for r in rules:
        db.session.add(r)

    # Sample content
    sample = [
        ('Milka Schokolade Rückruf — Warnung für ganz Deutschland', 'Lebensmittelwarnungen', 'ready'),
        ('Stadtfest Frankfurt 2024 — Alle Infos und Programm', 'Stadt-News', 'draft'),
        ('München schönste Stadt Europas? Diese Studie sagt Ja', 'Stadt-Memes', 'scheduled'),
        ('Bahnstreik nächste Woche angekündigt — was du wissen musst', 'Stadt-News', 'in_progress'),
        ('Haribo Goldbären Rückruf wegen Fremdkörper', 'Lebensmittelwarnungen', 'ready'),
        ('Wetter Frankfurt: Hitzewelle bis 38 Grad', 'Stadt-News', 'ready'),
        ('Neue S-Bahn Linie für München geplant', 'Stadt-News', 'draft'),
    ]
    for title, cat_name, status in sample:
        c = ContentItem(title=title, category_id=cats.get(cat_name, list(cats.values())[0]).id,
                        status=status, author_id=owner.id, raw_text=f'Rohtext: {title}',
                        caption=f'📌 {title}\n\nMehr Infos in unserer Bio.\n\n#News #Deutschland')
        db.session.add(c)

    db.session.commit()
    print("✅ Seed data created")


def init_db():
    with app.app_context():
        db.create_all()
        from sqlalchemy import text, inspect

        is_postgres = 'postgresql' in str(db.engine.url)

        # Jede Migration läuft in ihrer eigenen Verbindung + Commit.
        # Auf PostgreSQL: bricht eine Anweisung ab, bleibt die nächste davon unberührt.
        def safe_alter(sql):
            try:
                with db.engine.connect() as _conn:
                    _conn.execute(text(sql))
                    _conn.commit()
            except Exception as e:
                app.logger.debug(f'Migration skipped ({e.__class__.__name__}): {sql[:60]}')

        if is_postgres:
            # ── account ──────────────────────────────────────────────────
            safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS growth_goal INTEGER')
            safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS growth_goal_date TIMESTAMP')
            safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS share_token VARCHAR(64)')
            safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS profile_url VARCHAR(500)')
            safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS posting_interval_days FLOAT DEFAULT 1.0')
            # ── ai_config ────────────────────────────────────────────────
            safe_alter("ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS posting_times TEXT DEFAULT '[\"09:00\", \"18:00\"]'")
            safe_alter('ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS auto_approve BOOLEAN DEFAULT FALSE')
            safe_alter("ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS ai_model VARCHAR(100) DEFAULT 'claude-sonnet-4-6'")
            safe_alter('ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS persona TEXT')
            # ── content_item ─────────────────────────────────────────────
            safe_alter('ALTER TABLE content_item ADD COLUMN IF NOT EXISTS caption_score_manual FLOAT')
            safe_alter('ALTER TABLE content_item ADD COLUMN IF NOT EXISTS source_url VARCHAR(1000)')
            safe_alter('ALTER TABLE content_item ADD COLUMN IF NOT EXISTS source_name VARCHAR(200)')
            safe_alter('ALTER TABLE content_item ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP')
            safe_alter('ALTER TABLE content_item ADD COLUMN IF NOT EXISTS published_at TIMESTAMP')
            safe_alter('ALTER TABLE content_item ADD COLUMN IF NOT EXISTS author_id INTEGER')
            safe_alter('ALTER TABLE content_item ADD COLUMN IF NOT EXISTS ai_headline VARCHAR(500)')
            safe_alter('ALTER TABLE content_item ADD COLUMN IF NOT EXISTS ai_caption TEXT')
            safe_alter('ALTER TABLE content_item ADD COLUMN IF NOT EXISTS ai_score FLOAT')
            safe_alter('ALTER TABLE content_item ADD COLUMN IF NOT EXISTS is_duplicate BOOLEAN DEFAULT FALSE')
            safe_alter("ALTER TABLE content_item ADD COLUMN IF NOT EXISTS content_type VARCHAR(30) DEFAULT 'feed'")
            safe_alter("ALTER TABLE content_item ADD COLUMN IF NOT EXISTS approval_status VARCHAR(20) DEFAULT 'none'")
            safe_alter('ALTER TABLE content_item ADD COLUMN IF NOT EXISTS reviewed_by_id INTEGER')
            safe_alter('ALTER TABLE content_item ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP')
            safe_alter('ALTER TABLE content_item ADD COLUMN IF NOT EXISTS review_note TEXT')
            # ── scheduled_post ───────────────────────────────────────────
            safe_alter("ALTER TABLE scheduled_post ADD COLUMN IF NOT EXISTS slot_type VARCHAR(20) DEFAULT 'fixed'")
            safe_alter("ALTER TABLE scheduled_post ADD COLUMN IF NOT EXISTS media_ids TEXT DEFAULT '[]'")
            safe_alter('ALTER TABLE scheduled_post ADD COLUMN IF NOT EXISTS likes INTEGER')
            safe_alter('ALTER TABLE scheduled_post ADD COLUMN IF NOT EXISTS comments INTEGER')
            safe_alter('ALTER TABLE scheduled_post ADD COLUMN IF NOT EXISTS reach INTEGER')
            safe_alter('ALTER TABLE scheduled_post ADD COLUMN IF NOT EXISTS impressions INTEGER')
            safe_alter('ALTER TABLE scheduled_post ADD COLUMN IF NOT EXISTS saves INTEGER')
            safe_alter('ALTER TABLE scheduled_post ADD COLUMN IF NOT EXISTS external_post_id VARCHAR(200)')
            # ── media_item ───────────────────────────────────────────────
            safe_alter('ALTER TABLE media_item ADD COLUMN IF NOT EXISTS original_filename VARCHAR(500)')
            safe_alter('ALTER TABLE media_item ADD COLUMN IF NOT EXISTS mime_type VARCHAR(100)')
            safe_alter('ALTER TABLE media_item ADD COLUMN IF NOT EXISTS file_size INTEGER')
            safe_alter('ALTER TABLE media_item ADD COLUMN IF NOT EXISTS width INTEGER')
            safe_alter('ALTER TABLE media_item ADD COLUMN IF NOT EXISTS height INTEGER')
            safe_alter('ALTER TABLE media_item ADD COLUMN IF NOT EXISTS duration FLOAT')
            safe_alter('ALTER TABLE media_item ADD COLUMN IF NOT EXISTS thumbnail_url VARCHAR(1000)')
            safe_alter("ALTER TABLE media_item ADD COLUMN IF NOT EXISTS storage_source VARCHAR(50) DEFAULT 'local'")
            safe_alter("ALTER TABLE media_item ADD COLUMN IF NOT EXISTS tags TEXT DEFAULT '[]'")
            safe_alter('ALTER TABLE media_item ADD COLUMN IF NOT EXISTS usage_count INTEGER DEFAULT 0')
            safe_alter('ALTER TABLE media_item ADD COLUMN IF NOT EXISTS url VARCHAR(1000)')
            safe_alter('ALTER TABLE media_item ADD COLUMN IF NOT EXISTS uploaded_by_id INTEGER')
            # ── content_template ─────────────────────────────────────────
            safe_alter('ALTER TABLE content_template ADD COLUMN IF NOT EXISTS cta_template TEXT')
            safe_alter('ALTER TABLE content_template ADD COLUMN IF NOT EXISTS preview_image VARCHAR(500)')
            safe_alter("ALTER TABLE content_template ADD COLUMN IF NOT EXISTS primary_color VARCHAR(20) DEFAULT ''")
            safe_alter("ALTER TABLE content_template ADD COLUMN IF NOT EXISTS secondary_color VARCHAR(20) DEFAULT ''")
            safe_alter("ALTER TABLE content_template ADD COLUMN IF NOT EXISTS image_ratio VARCHAR(10) DEFAULT '1:1'")
            safe_alter('ALTER TABLE content_template ADD COLUMN IF NOT EXISTS style_notes TEXT')
            safe_alter("ALTER TABLE content_template ADD COLUMN IF NOT EXISTS posting_days TEXT DEFAULT '[]'")
            safe_alter("ALTER TABLE content_template ADD COLUMN IF NOT EXISTS posting_time_pref VARCHAR(10) DEFAULT ''")

        else:
            # SQLite: kein IF NOT EXISTS → mit inspect prüfen
            inspector = inspect(db.engine)
            account_cols = [c['name'] for c in inspector.get_columns('account')]
            if 'growth_goal'           not in account_cols: safe_alter('ALTER TABLE account ADD COLUMN growth_goal INTEGER')
            if 'growth_goal_date'      not in account_cols: safe_alter('ALTER TABLE account ADD COLUMN growth_goal_date DATETIME')
            if 'share_token'           not in account_cols: safe_alter('ALTER TABLE account ADD COLUMN share_token VARCHAR(64)')
            if 'profile_url'           not in account_cols: safe_alter('ALTER TABLE account ADD COLUMN profile_url VARCHAR(500)')
            if 'posting_interval_days' not in account_cols: safe_alter('ALTER TABLE account ADD COLUMN posting_interval_days FLOAT DEFAULT 1.0')

            ci_cols = [c['name'] for c in inspector.get_columns('content_item')]
            if 'caption_score_manual' not in ci_cols: safe_alter('ALTER TABLE content_item ADD COLUMN caption_score_manual FLOAT')

            sp_cols = [c['name'] for c in inspector.get_columns('scheduled_post')]
            if 'slot_type' not in sp_cols: safe_alter("ALTER TABLE scheduled_post ADD COLUMN slot_type VARCHAR(20) DEFAULT 'fixed'")
            if 'media_ids' not in sp_cols: safe_alter("ALTER TABLE scheduled_post ADD COLUMN media_ids TEXT DEFAULT '[]'")

            try:
                ct_cols = [c['name'] for c in inspector.get_columns('content_template')]
                for col, ddl in [
                    ('cta_template',      'ALTER TABLE content_template ADD COLUMN cta_template TEXT'),
                    ('preview_image',     'ALTER TABLE content_template ADD COLUMN preview_image VARCHAR(500)'),
                    ('primary_color',     "ALTER TABLE content_template ADD COLUMN primary_color VARCHAR(20) DEFAULT ''"),
                    ('secondary_color',   "ALTER TABLE content_template ADD COLUMN secondary_color VARCHAR(20) DEFAULT ''"),
                    ('image_ratio',       "ALTER TABLE content_template ADD COLUMN image_ratio VARCHAR(10) DEFAULT '1:1'"),
                    ('style_notes',       'ALTER TABLE content_template ADD COLUMN style_notes TEXT'),
                    ('posting_days',      "ALTER TABLE content_template ADD COLUMN posting_days TEXT DEFAULT '[]'"),
                    ('posting_time_pref', "ALTER TABLE content_template ADD COLUMN posting_time_pref VARCHAR(10) DEFAULT ''"),
                ]:
                    if col not in ct_cols:
                        safe_alter(ddl)
            except Exception:
                pass

        # ── Performance-Indizes (CREATE INDEX IF NOT EXISTS läuft idempotent) ──
        if is_postgres:
            idx_stmts = [
                'CREATE INDEX IF NOT EXISTS ix_account_status          ON account(status)',
                'CREATE INDEX IF NOT EXISTS ix_content_item_status      ON content_item(status)',
                'CREATE INDEX IF NOT EXISTS ix_content_item_created_at  ON content_item(created_at DESC)',
                'CREATE INDEX IF NOT EXISTS ix_scheduled_post_sched_at  ON scheduled_post(scheduled_at)',
                'CREATE INDEX IF NOT EXISTS ix_scheduled_post_status    ON scheduled_post(status)',
                'CREATE INDEX IF NOT EXISTS ix_scheduled_post_acc_type  ON scheduled_post(account_id, post_type, status)',
                'CREATE INDEX IF NOT EXISTS ix_analytics_snap_rec_at    ON analytics_snapshot(recorded_at)',
                'CREATE INDEX IF NOT EXISTS ix_analytics_snap_acc_rec   ON analytics_snapshot(account_id, recorded_at)',
                'CREATE INDEX IF NOT EXISTS ix_system_alert_resolved    ON system_alert(resolved)',
                'CREATE INDEX IF NOT EXISTS ix_activity_log_created_at  ON activity_log(created_at DESC)',
            ]
            for stmt in idx_stmts:
                safe_alter(stmt)

        seed_data()

        # Memes-Kategorie anlegen falls nicht vorhanden
        if not Category.query.filter_by(name='Memes').first():
            db.session.add(Category(name='Memes', color='#f59e0b', icon='face-laugh'))
            db.session.commit()

init_db()


# ─────────────────────── ALERT ENGINE ───────────────────────

_email_sent_cache = set()  # verhindert doppelte Mails in einer Session

def _maybe_send_alert_email(account_name, stock_days):
    """Sendet E-Mail-Alert wenn aktiviert und noch nicht in dieser Session gesendet."""
    key = f'{account_name}:{round(stock_days, 0)}'
    if key in _email_sent_cache:
        return
    try:
        ns = NotificationSettings.query.first()
        if ns and ns.email_enabled and ns.email:
            threshold = ns.low_stock_days or 3
            if stock_days <= threshold:
                ok = send_low_stock_email(account_name, stock_days, ns.email)
                if ok:
                    _email_sent_cache.add(key)
    except Exception as e:
        app.logger.error(f'Alert-Email Fehler: {e}')


def generate_alerts():
    """Auto-generate system alerts based on current state."""
    # Clear old unresolved automated alerts
    SystemAlert.query.filter_by(resolved=False).filter(
        SystemAlert.alert_type.in_(['low_stock', 'no_posts', 'empty_stock'])
    ).delete()
    db.session.flush()  # sicherstellen dass deletes durch sind bevor neue eingefügt werden

    accounts = Account.query.filter_by(status='active').all()
    now = datetime.utcnow()

    for acc in accounts:
        days = acc.feed_stock_days()

        if days == 0:
            db.session.add(SystemAlert(
                account_id=acc.id, alert_type='empty_stock', severity='critical',
                message=f'"{acc.name}" hat NULL geplante Posts. Sofort Content hinzufügen!'
            ))
        elif days < acc.min_stock_days:
            db.session.add(SystemAlert(
                account_id=acc.id, alert_type='low_stock', severity='critical',
                message=f'"{acc.name}" hat nur {round(days, 1)} Tage Vorrat (Minimum: {acc.min_stock_days}T)'
            ))
            _maybe_send_alert_email(acc.name, days)
            _push_notification('low_stock',
                f'⚠️ Kritischer Vorrat: {acc.name}',
                f'Nur noch {round(days,1)} Tage Content-Vorrat!',
                link=f'/accounts/{acc.id}', account_id=acc.id)
        elif days < 7:
            db.session.add(SystemAlert(
                account_id=acc.id, alert_type='low_stock', severity='warning',
                message=f'"{acc.name}" hat nur {round(days, 1)} Tage Vorrat'
            ))
            _maybe_send_alert_email(acc.name, days)
            _push_notification('low_stock',
                f'Low Stock: {acc.name}',
                f'{round(days,1)} Tage Vorrat verbleibend.',
                link=f'/accounts/{acc.id}', account_id=acc.id)

        # No posts scheduled at all
        upcoming = ScheduledPost.query.filter_by(account_id=acc.id, status='scheduled')\
            .filter(ScheduledPost.scheduled_at >= now).count()
        if upcoming == 0 and acc.automation_level < 3:
            db.session.add(SystemAlert(
                account_id=acc.id, alert_type='no_posts', severity='warning',
                message=f'"{acc.name}" hat keine geplanten Posts'
            ))

        # Content-Gap-Alarm: kein Post in den nächsten 48h
        in_48h = now + timedelta(hours=48)
        gap_post = ScheduledPost.query.filter(
            ScheduledPost.account_id == acc.id,
            ScheduledPost.status == 'scheduled',
            ScheduledPost.scheduled_at >= now,
            ScheduledPost.scheduled_at <= in_48h,
        ).first()
        if not gap_post and upcoming > 0 and acc.automation_level < 3:
            _push_notification('info',
                f'⏰ Posting-Lücke: {acc.name}',
                'Kein Post in den nächsten 48 Stunden geplant.',
                link=f'/accounts/{acc.id}/planer', account_id=acc.id)

    # Automation errors
    broken_rules = AutomationRule.query.filter(AutomationRule.error_count > 3, AutomationRule.active == True).all()
    for rule in broken_rules:
        db.session.add(SystemAlert(
            alert_type='bot_error', severity='critical',
            message=f'Automatisierung "{rule.name}" hat {rule.error_count} Fehler'
        ))

    try:
        db.session.commit()
    except Exception as e:
        app.logger.error('generate_alerts: DB-Fehler beim Commit — %s', e)
        db.session.rollback()


# ─────────────────────── RSS ENGINE ───────────────────────

def fetch_rss_feed(url, keywords=None):
    """Fetch RSS feed and return list of entries."""
    try:
        import urllib.request
        import xml.etree.ElementTree as ET

        req = urllib.request.Request(url, headers={'User-Agent': 'ContentOS/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read()

        root = ET.fromstring(content)
        ns = {'atom': 'http://www.w3.org/2005/Atom'}
        items = []

        # RSS 2.0
        for item in root.findall('.//item'):
            title = item.findtext('title', '').strip()
            link = item.findtext('link', '').strip()
            desc = item.findtext('description', '').strip()
            pub = item.findtext('pubDate', '')

            if keywords:
                text = (title + ' ' + desc).lower()
                if not any(kw.lower() in text for kw in keywords):
                    continue

            items.append({'title': title, 'url': link, 'description': desc, 'published': pub})

        # Atom
        if not items:
            for entry in root.findall('atom:entry', ns):
                title = entry.findtext('atom:title', '', ns).strip()
                link_el = entry.find('atom:link', ns)
                link = link_el.get('href', '') if link_el is not None else ''
                summary = entry.findtext('atom:summary', '', ns).strip()
                items.append({'title': title, 'url': link, 'description': summary, 'published': ''})

        return items[:20]
    except Exception as e:
        return []


def is_duplicate(title, threshold=0.85):
    """Check if similar content already exists."""
    existing = ContentItem.query.filter(
        ContentItem.created_at >= datetime.utcnow() - timedelta(days=7)
    ).with_entities(ContentItem.title).all()

    for (existing_title,) in existing:
        ratio = difflib.SequenceMatcher(None, title.lower(), existing_title.lower()).ratio()
        if ratio >= threshold:
            return True
    return False


def run_automation_rule(rule_id):
    """Execute a single automation rule and log the result."""
    with app.app_context():
        rule = AutomationRule.query.get(rule_id)
        if not rule or not rule.active:
            return

        log = AutomationRunLog(rule_id=rule_id, status='running')
        db.session.add(log)
        db.session.commit()

        try:
            cfg = rule.get_source_config()
            url = cfg.get('url', '')
            keywords = cfg.get('keywords', [])

            if not url:
                log.status = 'error'
                log.error_message = 'Keine URL konfiguriert'
                log.finished_at = datetime.utcnow()
                db.session.commit()
                return

            entries = fetch_rss_feed(url, keywords)
            created = 0
            skipped = 0

            for entry in entries:
                if not entry['title']:
                    skipped += 1
                    continue
                if is_duplicate(entry['title']):
                    skipped += 1
                    continue

                cat = None
                if rule.rule_type == 'food_warning':
                    cat = Category.query.filter_by(name='Lebensmittelwarnungen').first()
                elif rule.rule_type == 'city_news':
                    cat = Category.query.filter_by(name='Stadt-News').first()

                item = ContentItem(
                    title=entry['title'],
                    raw_text=entry.get('description', ''),
                    source_url=entry.get('url', ''),
                    source_name=url.split('/')[2] if '/' in url else url,
                    category_id=cat.id if cat else None,
                    status='draft', content_type='feed',
                )
                if rule.account:
                    item.accounts.append(rule.account)
                db.session.add(item)
                created += 1

            rule.run_count += 1
            rule.last_run_at = datetime.utcnow()
            rule.next_run_at = datetime.utcnow() + timedelta(minutes=rule.run_interval_minutes)

            log.status = 'success'
            log.finished_at = datetime.utcnow()
            log.items_found = len(entries)
            log.items_created = created
            log.items_skipped = skipped
            db.session.commit()

        except Exception as e:
            rule.error_count += 1
            rule.last_error = str(e)
            rule.last_run_at = datetime.utcnow()
            log.status = 'error'
            log.error_message = str(e)
            log.finished_at = datetime.utcnow()
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()


def auto_archive_old_content():
    """Archive published content older than 30 days."""
    with app.app_context():
        cutoff = datetime.utcnow() - timedelta(days=30)
        old = ContentItem.query.filter(
            ContentItem.status == 'published',
            ContentItem.published_at <= cutoff
        ).all()
        count = 0
        for item in old:
            item.status = 'archived'
            count += 1
        if count:
            db.session.commit()


_last_daily_snap_date = None  # verhindert mehrfaches Laufen pro Tag

def _daily_follower_snapshot():
    """
    Erstellt täglich um Mitternacht einen AnalyticsSnapshot für jeden aktiven
    Account, falls noch keiner für heute existiert.
    Läuft automatisch — kein manueller Aufruf nötig.
    """
    global _last_daily_snap_date
    today = datetime.utcnow().date()
    if _last_daily_snap_date == today:
        return  # heute schon gelaufen
    _last_daily_snap_date = today
    try:
        with app.app_context():
            accounts = Account.query.filter_by(status='active').all()
            created = 0
            for acc in accounts:
                existing = AnalyticsSnapshot.query.filter_by(account_id=acc.id)\
                    .filter(func.date(AnalyticsSnapshot.recorded_at) == today).first()
                if not existing:
                    db.session.add(AnalyticsSnapshot(
                        account_id  = acc.id,
                        followers   = acc.follower_count or 0,
                        recorded_at = datetime.utcnow(),
                    ))
                    created += 1
            if created:
                db.session.commit()
                app.logger.info(f'[Daily Snapshot] {created} Snapshots für {today} angelegt')
    except Exception as e:
        app.logger.error(f'[Daily Snapshot] Fehler: {e}')


def schedule_automations():
    """Background thread that runs automation rules and housekeeping."""
    tick = 0
    while True:
        try:
            with app.app_context():
                now = datetime.utcnow()

                # ── Täglicher Follower-Sync + Snapshot um Mitternacht (00:00–00:59) ──
                if now.hour == 0:
                    auto_sync_row = AppSettings.query.filter_by(key='ig_auto_sync').first()
                    auto_sync_on  = (not auto_sync_row) or (auto_sync_row.value != '0')
                    if auto_sync_on and not _ig_sync_status['running']:
                        _ig_sync_status.update({'running': True, 'error': None,
                                                'result': None, 'progress': 0, 'current': ''})
                        threading.Thread(target=_run_ig_follower_sync, daemon=True).start()
                    _daily_follower_snapshot()

                # ── Notfall-Pause: alle Automationen sofort stoppen ──
                if not _is_emergency_paused():
                    due_rules = AutomationRule.query.filter(
                        AutomationRule.active == True,
                        (AutomationRule.next_run_at == None) |
                        (AutomationRule.next_run_at <= now)
                    ).all()
                    for rule in due_rules:
                        threading.Thread(target=run_automation_rule, args=(rule.id,), daemon=True).start()

            tick += 1
            # Alerts alle 5 Min. refreshen (statt bei jedem Dashboard-Load)
            if tick % 5 == 0:
                generate_alerts()
            # Housekeeping every 60 ticks (~1 hour)
            if tick % 60 == 0:
                auto_archive_old_content()

        except Exception:
            pass
        threading.Event().wait(60)


# Start automation engine in background
automation_thread = threading.Thread(target=schedule_automations, daemon=True)
automation_thread.start()


# ─────────────────────── HELPERS ───────────────────────

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ── App-Einstellungen ─────────────────────────────────────────
def get_setting(key, default=None):
    """Liest einen Wert aus AppSettings. Gibt default zurück wenn nicht gesetzt."""
    with app.app_context():
        s = AppSettings.query.filter_by(key=key).first()
        return s.value if s and s.value else default

def set_setting(key, value):
    """Speichert einen Wert in AppSettings (upsert)."""
    s = AppSettings.query.filter_by(key=key).first()
    if not s:
        s = AppSettings(key=key)
        db.session.add(s)
    s.value = value
    s.updated_at = datetime.utcnow()


def _set_follower_count(acc, new_count):
    """
    Zentrale Funktion für Follower-Updates.
    Aktualisiert Account.follower_count UND erstellt/aktualisiert den
    heutigen AnalyticsSnapshot — sodass alle Charts & KPIs konsistent sind.
    Kein db.session.commit() — muss vom Aufrufer gemacht werden.
    Gibt (old_count, delta) zurück.
    """
    old = acc.follower_count or 0
    acc.follower_count = new_count
    today = datetime.utcnow().date()
    snap = AnalyticsSnapshot.query.filter_by(account_id=acc.id)\
        .filter(func.date(AnalyticsSnapshot.recorded_at) == today).first()
    if snap:
        snap.followers   = new_count
        snap.recorded_at = datetime.utcnow()
    else:
        db.session.add(AnalyticsSnapshot(
            account_id  = acc.id,
            followers   = new_count,
            recorded_at = datetime.utcnow(),
        ))
    return old, new_count - old


def get_file_type(filename):
    ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
    if ext in {'mp4', 'mov', 'avi', 'webm'}:
        return 'video'
    elif ext in {'png', 'jpg', 'jpeg', 'gif', 'webp'}:
        return 'image'
    return 'other'


def _get_planned_days_batch(accounts):
    """Feed-stock-days für mehrere Accounts in EINER einzigen DB-Query.
    Gibt {account_id: days_float} zurück."""
    if not accounts:
        return {}
    now = datetime.utcnow()
    ids = [a.id for a in accounts]
    rows = db.session.query(
        ScheduledPost.account_id,
        func.count(ScheduledPost.id).label('cnt')
    ).filter(
        ScheduledPost.account_id.in_(ids),
        ScheduledPost.post_type == 'feed',
        ScheduledPost.status == 'scheduled',
        ScheduledPost.scheduled_at >= now
    ).group_by(ScheduledPost.account_id).all()
    cnt_map = {r.account_id: r.cnt for r in rows}
    return {
        a.id: (cnt_map.get(a.id, 0) / a.target_feed_per_day
               if a.target_feed_per_day else 0)
        for a in accounts
    }


def _days_to_status(days):
    """Gleiche Logik wie Account.stock_status(), aber ohne DB-Query."""
    if days >= 14: return 'green'
    if days >= 7:  return 'yellow'
    if days >= 3:  return 'orange'
    return 'red'


def get_dashboard_stats(active_accounts=None, days_map=None):
    """Wenn active_accounts + days_map übergeben werden, braucht die Funktion
    keine eigenen Account-Queries mehr (Dashboard-Route übergibt sie)."""
    if active_accounts is None:
        active_accounts = Account.query.filter_by(status='active').all()
    if days_map is None:
        days_map = _get_planned_days_batch(active_accounts)

    total_followers = db.session.query(func.sum(Account.follower_count)).scalar() or 0

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0)
    today_end = today_start + timedelta(days=1)
    posts_today = ScheduledPost.query.filter(
        ScheduledPost.scheduled_at >= today_start,
        ScheduledPost.scheduled_at < today_end
    ).count()

    content_ready = ContentItem.query.filter_by(status='ready').count()
    # Batch: kein DB-Query pro Account mehr
    critical_accounts = [a for a in active_accounts if _days_to_status(days_map[a.id]) == 'red']
    warning_accounts  = [a for a in active_accounts if _days_to_status(days_map[a.id]) in ('orange', 'yellow')]
    active_alerts = SystemAlert.query.filter_by(resolved=False).count()

    week_ago = datetime.utcnow() - timedelta(days=7)
    old_snap = db.session.query(func.sum(AnalyticsSnapshot.followers))\
        .filter(AnalyticsSnapshot.recorded_at <= week_ago).scalar() or 0
    growth_7d = total_followers - old_snap

    return {
        'total_accounts': len(active_accounts),
        'total_followers': total_followers,
        'posts_today': posts_today,
        'content_ready': content_ready,
        'critical_accounts': critical_accounts,
        'warning_accounts': warning_accounts,
        'active_alerts': active_alerts,
        'growth_7d': growth_7d,
    }


def linear_forecast(data_points, days_ahead=30):
    """Simple linear regression forecast."""
    if len(data_points) < 2:
        return []
    n = len(data_points)
    x_mean = (n - 1) / 2
    y_mean = sum(data_points) / n
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(data_points))
    den = sum((i - x_mean) ** 2 for i in range(n))
    slope = num / den if den else 0
    intercept = y_mean - slope * x_mean
    return [max(0, int(intercept + slope * (n + i))) for i in range(days_ahead)]


# ─────────────────────── DASHBOARD ───────────────────────

@app.route('/')
@login_required
def dashboard():
    # generate_alerts() wird jetzt vom Scheduler alle 5 Min. ausgeführt —
    # NICHT mehr bei jedem Seitenaufruf (war ~30 Extra-Queries pro Load).
    now = datetime.utcnow()

    # ── 1× Accounts laden + 1× Batch-Stock-Query (ersetzt N×6 Queries) ──
    all_active = Account.query.filter_by(status='active').all()
    days_map   = _get_planned_days_batch(all_active)

    # stats nutzt die bereits geladenen Daten (keine eigenen Account-Queries)
    stats = get_dashboard_stats(active_accounts=all_active, days_map=days_map)

    # stock_summary: kein einziger DB-Aufruf mehr (uses days_map)
    stock_summary = {'green': 0, 'yellow': 0, 'orange': 0, 'red': 0}
    for a in all_active:
        stock_summary[_days_to_status(days_map[a.id])] += 1

    # ── Chart: 1 GROUP-BY-Query statt 30 Einzel-Queries ──────────────
    chart_cutoff = now - timedelta(days=29)
    snap_rows = db.session.query(
        func.date(AnalyticsSnapshot.recorded_at).label('d'),
        func.sum(AnalyticsSnapshot.followers).label('total')
    ).filter(AnalyticsSnapshot.recorded_at >= chart_cutoff)\
     .group_by(func.date(AnalyticsSnapshot.recorded_at)).all()
    snap_dict = {str(r.d): int(r.total or 0) for r in snap_rows}
    chart_labels, chart_data = [], []
    for i in range(29, -1, -1):
        day = now - timedelta(days=i)
        chart_labels.append(day.strftime('%d.%m'))
        chart_data.append(snap_dict.get(day.date().isoformat(), 0))

    forecast = linear_forecast(chart_data, 14)

    # ── Restliche Queries (nicht weiter optimierbar) ──────────────────
    accounts       = sorted(all_active, key=lambda a: (
        {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}.get(a.priority, 2),
        -(a.follower_count or 0)
    ))[:10]
    recent_content = ContentItem.query.order_by(ContentItem.created_at.desc()).limit(8).all()
    alerts         = SystemAlert.query.filter_by(resolved=False)\
                         .order_by(SystemAlert.severity.desc()).limit(10).all()
    recent_activity = ActivityLog.query.order_by(ActivityLog.created_at.desc()).limit(15).all()

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    posts_today = ScheduledPost.query.filter(
        ScheduledPost.scheduled_at >= today_start,
        ScheduledPost.scheduled_at < today_start + timedelta(days=1),
        ScheduledPost.status.in_(['scheduled', 'published'])
    ).order_by(ScheduledPost.scheduled_at).all()

    categories = Category.query.order_by(Category.name).all()

    return render_template('dashboard.html',
        stats=stats, accounts=accounts, recent_content=recent_content, alerts=alerts,
        chart_labels=json.dumps(chart_labels), chart_data=json.dumps(chart_data),
        forecast=json.dumps(forecast), stock_summary=stock_summary,
        recent_activity=recent_activity, posts_today=posts_today,
        all_accounts=all_active, categories=categories,
        active_page='dashboard')


# ─────────────────────── ACCOUNTS ───────────────────────

@app.route('/accounts')
def accounts():
    q = request.args.get('q', '')
    category_id = request.args.get('category', type=int)
    platform_id = request.args.get('platform', type=int)
    status = request.args.get('status', '')
    automation = request.args.get('automation', '')
    priority = request.args.get('priority', '')
    sort = request.args.get('sort', 'followers')

    query = Account.query
    if q:
        query = query.filter(Account.name.ilike(f'%{q}%'))
    if category_id:
        query = query.filter_by(category_id=category_id)
    if platform_id:
        query = query.filter_by(platform_id=platform_id)
    if status:
        query = query.filter_by(status=status)
    if automation:
        query = query.filter_by(automation_level=int(automation))
    if priority:
        query = query.filter_by(priority=priority)

    if sort == 'name':
        query = query.order_by(Account.name)
    elif sort == 'created':
        query = query.order_by(Account.created_at.desc())
    else:
        query = query.order_by(Account.follower_count.desc())

    page = request.args.get('page', 1, type=int)
    per_page = 50
    # eager-load: verhindert N+1 für platform.name / category.name im Template
    pagination = query.options(
        joinedload(Account.platform),
        joinedload(Account.category),
    ).paginate(page=page, per_page=per_page, error_out=False)
    categories = Category.query.order_by(Category.name).all()
    platforms = Platform.query.all()

    _f = {'q': q, 'category': category_id, 'platform': platform_id,
          'status': status, 'automation': automation, 'priority': priority, 'sort': sort}
    return render_template('accounts.html',
        accounts=pagination.items, pagination=pagination,
        categories=categories, platforms=platforms,
        active_page='accounts',
        filters={k: v for k, v in _f.items() if v})


@app.route('/accounts/new', methods=['GET', 'POST'])
def account_new():
    if request.method == 'POST':
        d = request.form
        interval = float(d.get('posting_interval_days') or 1.0)
        acc = Account(
            name=d['name'], handle=d.get('handle', ''),
            profile_url=d.get('profile_url', ''),
            platform_id=int(d['platform_id']),
            category_id=int(d['category_id']) if d.get('category_id') else None,
            follower_count=int(d.get('follower_count') or 0),
            automation_level=int(d.get('automation_level', 0)),
            priority=d.get('priority', 'medium'),
            status=d.get('status', 'active'),
            notes=d.get('notes', ''),
            posting_interval_days=interval,
            target_feed_per_day=round(1.0 / interval, 3) if interval > 0 else 1.0,
            min_stock_days=int(d.get('min_stock_days') or 3),
            optimal_stock_days=int(d.get('optimal_stock_days') or 14),
        )
        db.session.add(acc)
        db.session.flush()
        db.session.add(AIConfig(account_id=acc.id))
        db.session.commit()
        flash(f'Account "{acc.name}" erstellt.', 'success')
        return redirect(url_for('account_detail', account_id=acc.id))

    categories = Category.query.order_by(Category.name).all()
    platforms = Platform.query.all()
    labels = Label.query.order_by(Label.name).all()
    return render_template('account_form.html',
        account=None, categories=categories, platforms=platforms, labels=labels,
        active_page='accounts')


@app.route('/accounts/<int:account_id>')
def account_detail(account_id):
    account = Account.query.get_or_404(account_id)
    upcoming = ScheduledPost.query.filter_by(account_id=account_id, status='scheduled')\
        .filter(ScheduledPost.scheduled_at >= datetime.utcnow())\
        .order_by(ScheduledPost.scheduled_at).limit(20).all()
    analytics = AnalyticsSnapshot.query.filter_by(account_id=account_id)\
        .order_by(AnalyticsSnapshot.recorded_at.desc()).limit(30).all()

    chart_labels = [a.recorded_at.strftime('%d.%m') for a in reversed(analytics)]
    chart_data = [a.followers for a in reversed(analytics)]

    # Stock per type
    now = datetime.utcnow()
    feed_count = ScheduledPost.query.filter_by(account_id=account_id, post_type='feed', status='scheduled')\
        .filter(ScheduledPost.scheduled_at >= now).count()
    story_count = ScheduledPost.query.filter_by(account_id=account_id, post_type='story', status='scheduled')\
        .filter(ScheduledPost.scheduled_at >= now).count()
    reel_count = ScheduledPost.query.filter_by(account_id=account_id, post_type='reel', status='scheduled')\
        .filter(ScheduledPost.scheduled_at >= now).count()

    feed_days = feed_count / account.target_feed_per_day if account.target_feed_per_day else 0
    story_days = story_count / account.target_story_per_day if account.target_story_per_day else 0

    account_alerts = SystemAlert.query.filter_by(account_id=account_id, resolved=False).all()

    # Vorrats-Posts mit Media für visuelle Galerie
    stock_posts = ScheduledPost.query.filter_by(account_id=account_id, status='scheduled')\
        .filter(ScheduledPost.scheduled_at >= now)\
        .order_by(ScheduledPost.scheduled_at).limit(24).all()

    # Content-Items die bereit oder in_progress sind (noch nicht geplant)
    ready_content = ContentItem.query\
        .filter(ContentItem.accounts.any(id=account_id))\
        .filter(ContentItem.status.in_(['ready', 'in_progress', 'draft']))\
        .order_by(ContentItem.updated_at.desc()).limit(20).all()

    return render_template('account_detail.html',
        account=account, upcoming=upcoming,
        chart_labels=json.dumps(chart_labels), chart_data=json.dumps(chart_data),
        feed_days=round(feed_days, 1), story_days=round(story_days, 1), reel_count=reel_count,
        account_alerts=account_alerts,
        stock_posts=stock_posts, ready_content=ready_content,
        active_page='accounts')


@app.route('/accounts/<int:account_id>/edit', methods=['GET', 'POST'])
def account_edit(account_id):
    account = Account.query.get_or_404(account_id)
    if request.method == 'POST':
        d = request.form
        interval = float(d.get('posting_interval_days') or 1.0)
        account.name = d['name']
        account.handle = d.get('handle', '')
        account.profile_url = d.get('profile_url', '')
        account.platform_id = int(d['platform_id'])
        account.category_id = int(d['category_id']) if d.get('category_id') else None
        _set_follower_count(account, int(d.get('follower_count') or 0))
        account.automation_level = int(d.get('automation_level', 0))
        account.priority = d.get('priority', 'medium')
        account.status = d.get('status', 'active')
        account.notes = d.get('notes', '')
        account.posting_interval_days = interval
        account.target_feed_per_day = round(1.0 / interval, 3) if interval > 0 else 1.0
        account.min_stock_days = int(d.get('min_stock_days') or 3)
        account.optimal_stock_days = int(d.get('optimal_stock_days') or 14)
        db.session.commit()
        flash('Account aktualisiert.', 'success')
        return redirect(url_for('account_detail', account_id=account_id))

    categories = Category.query.order_by(Category.name).all()
    platforms = Platform.query.all()
    labels = Label.query.order_by(Label.name).all()
    return render_template('account_form.html',
        account=account, categories=categories, platforms=platforms, labels=labels,
        active_page='accounts')


@app.route('/accounts/<int:account_id>/delete', methods=['POST'])
def account_delete(account_id):
    account = Account.query.get_or_404(account_id)
    name = account.name
    # PostgreSQL FK-Constraints: manuell auflösen bevor Account gelöscht wird.
    # (nullable FKs → NULL setzen, non-nullable → Zeilen löschen)
    from models import SystemAlert, AppNotification, HashtagSet, AccountAutomationProfile, RecurringPost
    SystemAlert.query.filter_by(account_id=account_id).update({'account_id': None})
    AppNotification.query.filter_by(account_id=account_id).update({'account_id': None})
    HashtagSet.query.filter_by(account_id=account_id).update({'account_id': None})
    AccountAutomationProfile.query.filter_by(account_id=account_id).delete()
    RecurringPost.query.filter_by(account_id=account_id).delete()
    db.session.flush()
    db.session.delete(account)
    db.session.commit()
    flash(f'Account "{name}" gelöscht.', 'info')
    return redirect(url_for('accounts'))


@app.route('/accounts/<int:account_id>/calendar')
def account_calendar(account_id):
    account = Account.query.get_or_404(account_id)
    all_accounts = Account.query.order_by(Account.name).all()
    return render_template('account_calendar.html',
        account=account, all_accounts=all_accounts, active_page='accounts')


# ─────────────────────── BATCH-PLANER ───────────────────────

@app.route('/accounts/<int:account_id>/planer')
def account_planer(account_id):
    account = Account.query.get_or_404(account_id)
    all_accounts = Account.query.filter_by(status='active').order_by(Account.name).all()
    labels = Label.query.order_by(Label.name).all()
    return render_template('planer.html',
        account=account, all_accounts=all_accounts, labels=labels, active_page='accounts')


@app.route('/api/accounts/<int:account_id>/stack')
def account_stack(account_id):
    """Unverplante ContentItems für den Batch-Planer."""
    label_id = request.args.get('label_id', type=int)
    q = request.args.get('q', '')

    # IDs die bereits für diesen Account eingeplant sind
    already = [r[0] for r in
        db.session.query(ScheduledPost.content_item_id)
        .filter(ScheduledPost.account_id == account_id,
                ScheduledPost.status.in_(['scheduled', 'draft']),
                ScheduledPost.content_item_id.isnot(None))
        .all()]

    query = ContentItem.query.filter(ContentItem.status.in_(['ready', 'draft', 'in_progress']))
    if already:
        query = query.filter(~ContentItem.id.in_(already))
    if label_id:
        query = query.filter(ContentItem.labels.any(Label.id == label_id))
    if q:
        query = query.filter(
            ContentItem.title.ilike(f'%{q}%') | ContentItem.caption.ilike(f'%{q}%'))

    # eager-load: verhindert N+1 für category/labels/media_items (war bis 900 Queries!)
    items = query.options(
        joinedload(ContentItem.category),
        selectinload(ContentItem.labels),
        selectinload(ContentItem.media_items),
    ).order_by(ContentItem.created_at.desc()).limit(300).all()
    return jsonify([{
        'id': c.id,
        'title': c.title,
        'caption': (c.caption or '')[:120],
        'status': c.status,
        'content_type': c.content_type,
        'category': c.category.name if c.category else '',
        'category_color': c.category.color if c.category else '#6366f1',
        'labels': [{'id': l.id, 'name': l.name, 'color': l.color} for l in c.labels],
        'thumb': c.media_items[0].url if c.media_items else None,
        'media_ids': [m.id for m in c.media_items],
        'media_count': len(c.media_items),
    } for c in items])


@app.route('/api/accounts/<int:account_id>/planer/events')
def planer_events(account_id):
    """Geplante Posts für einen Monat (YYYY-MM)."""
    month = request.args.get('month', '')  # z.B. 2025-06
    try:
        y, m = int(month[:4]), int(month[5:7])
    except Exception:
        from datetime import date
        today = date.today()
        y, m = today.year, today.month

    start = datetime(y, m, 1)
    import calendar as cal_mod
    last_day = cal_mod.monthrange(y, m)[1]
    end = datetime(y, m, last_day, 23, 59, 59)

    posts = ScheduledPost.query.filter(
        ScheduledPost.account_id == account_id,
        ScheduledPost.scheduled_at >= start,
        ScheduledPost.scheduled_at <= end,
    ).options(
        # eager-load: verhindert N+1 für p.content_item und ci.media_items
        joinedload(ScheduledPost.content_item).selectinload(ContentItem.media_items)
    ).order_by(ScheduledPost.scheduled_at).all()

    result = []
    for p in posts:
        ci = p.content_item
        result.append({
            'id': p.id,
            'date': p.scheduled_at.strftime('%Y-%m-%d'),
            'time': p.scheduled_at.strftime('%H:%M'),
            'slot_type': p.slot_type,
            'status': p.status,
            'post_type': p.post_type,
            'caption': (p.caption or (ci.title if ci else '') or '')[:80],
            'thumb': (ci.media_items[0].url if ci and ci.media_items else None),
            'content_item_id': p.content_item_id,
        })
    return jsonify(result)


@app.route('/api/accounts/<int:account_id>/planer/schedule', methods=['POST'])
def planer_schedule(account_id):
    """Einen ContentItem auf ein Datum legen (Planer-Drag&Drop)."""
    account = Account.query.get_or_404(account_id)
    d = request.get_json()
    content_item_id = d.get('content_item_id')
    date_str = d.get('date')       # YYYY-MM-DD
    time_str = d.get('time', '18:00')
    slot_type = d.get('slot_type', 'fixed')

    if not date_str:
        return jsonify({'ok': False, 'error': 'Datum fehlt'}), 400

    ci = ContentItem.query.get(content_item_id) if content_item_id else None
    scheduled_at = datetime.strptime(f'{date_str} {time_str}', '%Y-%m-%d %H:%M')

    post = ScheduledPost(
        account_id=account_id,
        content_item_id=content_item_id,
        caption=ci.caption or ci.title if ci else '',
        post_type=ci.content_type if ci else 'feed',
        slot_type=slot_type,
        status='scheduled' if slot_type != 'disabled' else 'disabled',
        scheduled_at=scheduled_at,
        media_item_id=ci.media_items[0].id if ci and ci.media_items else None,
        media_ids=json.dumps([m.id for m in ci.media_items]) if ci else '[]',
    )
    db.session.add(post)
    if ci:
        ci.status = 'scheduled'
    db.session.commit()
    log_activity('post_scheduled',
        f'Planer: {ci.title if ci else "Slot"} → {account.name} am {scheduled_at.strftime("%d.%m.%Y")}')
    return jsonify({'ok': True, 'post_id': post.id})


@app.route('/api/accounts/<int:account_id>/planer/auto-fill', methods=['POST'])
def planer_auto_fill(account_id):
    """Verteilt N ContentItems gleichmäßig ab einem Startdatum."""
    account = Account.query.get_or_404(account_id)
    d = request.get_json()
    content_ids = d.get('content_ids', [])   # geordnete Liste
    start_date = d.get('start_date')          # YYYY-MM-DD
    time_str = d.get('time', '18:00')
    interval = d.get('interval_days', account.posting_interval_days or 1)

    if not content_ids or not start_date:
        return jsonify({'ok': False, 'error': 'content_ids und start_date erforderlich'}), 400

    base = datetime.strptime(f'{start_date} {time_str}', '%Y-%m-%d %H:%M')
    # Bulk-load: 1 Query statt 1 Query pro ContentItem
    ci_map = {c.id: c for c in ContentItem.query.filter(
        ContentItem.id.in_(content_ids)
    ).options(selectinload(ContentItem.media_items)).all()}
    created = []
    for i, cid in enumerate(content_ids):
        ci = ci_map.get(cid)
        if not ci:
            continue
        scheduled_at = base + timedelta(days=i * interval)
        post = ScheduledPost(
            account_id=account_id,
            content_item_id=cid,
            caption=ci.caption or ci.title or '',
            post_type=ci.content_type or 'feed',
            slot_type='fixed',
            status='scheduled',
            scheduled_at=scheduled_at,
            media_item_id=ci.media_items[0].id if ci.media_items else None,
            media_ids=json.dumps([m.id for m in ci.media_items]),
        )
        db.session.add(post)
        ci.status = 'scheduled'
        created.append({'content_id': cid, 'date': scheduled_at.strftime('%Y-%m-%d')})
    db.session.commit()
    log_activity('batch_scheduled',
        f'Auto-Fill: {len(created)} Posts für {account.name} eingeplant')
    return jsonify({'ok': True, 'scheduled': created})


@app.route('/api/accounts/<int:account_id>/posts/new', methods=['POST'])
def account_post_new(account_id):
    """Create a new scheduled post via calendar drag or form."""
    account = Account.query.get_or_404(account_id)
    d = request.get_json()
    slot_type = d.get('slot_type', 'fixed')
    # disabled-Slot: kein echtes Post, nur Platzhalter
    status = 'disabled' if slot_type == 'disabled' else 'scheduled'

    # Media verarbeiten
    media_item_id = d.get('media_item_id')  # Einzelbild / Reel
    media_ids_list = d.get('media_ids', []) # Carousel
    if media_item_id and not media_ids_list:
        media_ids_list = [media_item_id]

    content_item_id = d.get('content_item_id')
    post = ScheduledPost(
        account_id=account_id,
        content_item_id=content_item_id,
        caption=d.get('caption', ''),
        post_type=d.get('post_type', 'feed'),
        slot_type=slot_type,
        status=status,
        scheduled_at=datetime.fromisoformat(d['scheduled_at']),
        media_item_id=media_ids_list[0] if media_ids_list else None,
        media_ids=json.dumps(media_ids_list),
    )
    db.session.add(post)

    # Media usage_count erhöhen
    for mid in media_ids_list:
        m = MediaItem.query.get(mid)
        if m: m.usage_count += 1

    db.session.commit()
    log_activity('post_scheduled', f'{slot_type.capitalize()}-Slot für {account.name} am {post.scheduled_at.strftime("%d.%m")} gesetzt')
    return jsonify({'id': post.id, 'ok': True})


@app.route('/api/content/picker')
def content_picker_list():
    """Gibt Content-Items für den Kalender-Picker zurück."""
    q = request.args.get('q', '')
    status = request.args.get('status', '')
    category_id = request.args.get('category', type=int)
    query = ContentItem.query.filter(ContentItem.status.in_(['draft','in_progress','ready']))
    if q:
        query = query.filter(ContentItem.title.ilike(f'%{q}%') | ContentItem.caption.ilike(f'%{q}%'))
    if status:
        query = query.filter_by(status=status)
    if category_id:
        query = query.filter_by(category_id=category_id)
    items = query.order_by(ContentItem.created_at.desc()).limit(100).all()
    return jsonify([{
        'id': c.id,
        'title': c.title,
        'caption': (c.caption or '')[:200],
        'status': c.status,
        'content_type': c.content_type,
        'category': c.category.name if c.category else '',
        'category_color': c.category.color if c.category else '#6366f1',
        'media_url': c.media_items[0].url if c.media_items else None,
        'media_count': len(c.media_items),
        'media_ids': [m.id for m in c.media_items],
    } for c in items])


@app.route('/api/media/picker')
def media_picker_list():
    """Gibt alle Medien für den Picker zurück."""
    file_type = request.args.get('type', '')
    q = request.args.get('q', '')
    query = MediaItem.query
    if file_type:
        query = query.filter_by(file_type=file_type)
    if q:
        query = query.filter(MediaItem.original_filename.ilike(f'%{q}%'))
    items = query.order_by(MediaItem.created_at.desc()).limit(200).all()
    return jsonify([{
        'id': m.id,
        'name': m.original_filename,
        'url': m.url,
        'file_type': m.file_type,
        'size_kb': round(m.file_size / 1024) if m.file_size else 0,
    } for m in items])


@app.route('/api/posts/<int:post_id>/media', methods=['POST'])
def post_update_media(post_id):
    """Media eines Posts aktualisieren."""
    post = ScheduledPost.query.get_or_404(post_id)
    d = request.get_json()
    media_ids = d.get('media_ids', [])
    post.media_ids = json.dumps(media_ids)
    post.media_item_id = media_ids[0] if media_ids else None
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/posts/<int:post_id>/slot-type', methods=['POST'])
def post_update_slot_type(post_id):
    """Slot-Typ eines Posts ändern."""
    post = ScheduledPost.query.get_or_404(post_id)
    d = request.get_json()
    new_type = d.get('slot_type')
    if new_type not in ('fixed', 'flexible', 'disabled'):
        return jsonify({'ok': False, 'error': 'Ungültiger Slot-Typ'}), 400
    post.slot_type = new_type
    post.status = 'disabled' if new_type == 'disabled' else ('scheduled' if post.status == 'disabled' else post.status)
    db.session.commit()
    return jsonify({'ok': True, 'slot_type': post.slot_type})


@app.route('/api/posts/<int:post_id>/reschedule', methods=['POST'])
def post_reschedule(post_id):
    """Move a post to a new datetime (drag & drop)."""
    post = ScheduledPost.query.get_or_404(post_id)
    d = request.get_json()
    post.scheduled_at = datetime.fromisoformat(d['scheduled_at'])
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/posts/<int:post_id>/delete', methods=['POST'])
def post_delete(post_id):
    post = ScheduledPost.query.get_or_404(post_id)
    db.session.delete(post)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/posts')
def api_posts():
    """Return scheduled posts as JSON for queue view."""
    account_id = request.args.get('account_id', type=int)
    status = request.args.get('status', '')
    limit = request.args.get('limit', 50, type=int)
    query = ScheduledPost.query
    if account_id:
        query = query.filter_by(account_id=account_id)
    if status:
        query = query.filter_by(status=status)
    posts = query.order_by(ScheduledPost.scheduled_at.asc()).limit(limit).all()
    result = []
    for p in posts:
        acc = Account.query.get(p.account_id)
        result.append({
            'id': p.id,
            'account_id': p.account_id,
            'account_name': acc.name if acc else '',
            'caption': (p.caption or '')[:200],
            'post_type': p.post_type,
            'status': p.status,
            'scheduled_at': p.scheduled_at.isoformat() if p.scheduled_at else None,
        })
    return jsonify(result)

# ─────────────────────── CONTENT HUB ───────────────────────

@app.route('/content')
def content_hub():
    q = request.args.get('q', '')
    status = request.args.get('status', '')
    category_id = request.args.get('category', type=int)
    content_type = request.args.get('type', '')

    query = ContentItem.query
    if q:
        query = query.filter(
            ContentItem.title.ilike(f'%{q}%') |
            ContentItem.caption.ilike(f'%{q}%') |
            ContentItem.raw_text.ilike(f'%{q}%')
        )
    if status:
        query = query.filter_by(status=status)
    if category_id:
        query = query.filter_by(category_id=category_id)
    if content_type:
        query = query.filter_by(content_type=content_type)

    page = request.args.get('page', 1, type=int)
    per_page = 40
    # eager-load: verhindert N+1 für category/labels/media_items im Template
    _opts = [joinedload(ContentItem.category),
             selectinload(ContentItem.labels),
             selectinload(ContentItem.media_items)]
    ordered = query.options(*_opts).order_by(ContentItem.created_at.desc())
    pagination = ordered.paginate(page=page, per_page=per_page, error_out=False)

    # Für Kanban alle Items (max 200) ohne Pagination
    kanban_items = query.options(*_opts).order_by(ContentItem.created_at.desc()).limit(200).all()

    categories = Category.query.order_by(Category.name).all()
    labels = Label.query.order_by(Label.name).all()

    # 1 GROUP-BY statt 7 einzelner COUNT-Queries
    _sc_rows = db.session.query(ContentItem.status, func.count(ContentItem.id))\
                         .group_by(ContentItem.status).all()
    status_counts = {s: 0 for s in ['draft', 'in_progress', 'ready', 'scheduled', 'published', 'archived', 'error']}
    for _s, _c in _sc_rows:
        if _s in status_counts:
            status_counts[_s] = _c

    _f = {'q': q, 'status': status, 'category': category_id, 'type': content_type}
    return render_template('content.html',
        items=pagination.items, pagination=pagination,
        kanban_items=kanban_items,
        categories=categories, labels=labels, status_counts=status_counts,
        active_page='content',
        filters={k: v for k, v in _f.items() if v})


@app.route('/content/new', methods=['GET', 'POST'])
def content_new():
    if request.method == 'POST':
        d = request.form
        item = ContentItem(
            title=d['title'],
            raw_text=d.get('raw_text', ''),
            caption=d.get('caption', ''),
            source_url=d.get('source_url', ''),
            source_name=d.get('source_name', ''),
            category_id=int(d['category_id']) if d.get('category_id') else None,
            status=d.get('status', 'draft'),
            content_type=d.get('content_type', 'feed'),
        )
        db.session.add(item)
        db.session.commit()
        flash('Content erstellt.', 'success')
        return redirect(url_for('content_hub'))

    categories = Category.query.order_by(Category.name).all()
    labels = Label.query.order_by(Label.name).all()
    all_accounts = Account.query.order_by(Account.name).all()
    return render_template('content_form.html',
        item=None, categories=categories, labels=labels, accounts=all_accounts,
        active_page='content')


@app.route('/content/<int:item_id>')
def content_detail(item_id):
    item = ContentItem.query.get_or_404(item_id)
    return render_template('content_detail.html', item=item, active_page='content')


@app.route('/content/<int:item_id>/edit', methods=['GET', 'POST'])
def content_edit(item_id):
    item = ContentItem.query.get_or_404(item_id)
    if request.method == 'POST':
        d = request.form
        item.title = d['title']
        item.raw_text = d.get('raw_text', '')
        item.caption = d.get('caption', '')
        item.source_url = d.get('source_url', '')
        item.source_name = d.get('source_name', '')
        item.category_id = int(d['category_id']) if d.get('category_id') else None
        item.status = d.get('status', item.status)
        item.content_type = d.get('content_type', 'feed')
        item.updated_at = datetime.utcnow()
        db.session.commit()
        flash('Content aktualisiert.', 'success')
        return redirect(url_for('content_detail', item_id=item_id))

    categories = Category.query.order_by(Category.name).all()
    labels = Label.query.order_by(Label.name).all()
    all_accounts = Account.query.order_by(Account.name).all()
    return render_template('content_form.html',
        item=item, categories=categories, labels=labels, accounts=all_accounts,
        active_page='content')


@app.route('/content/<int:item_id>/status', methods=['POST'])
def content_status_update(item_id):
    item = ContentItem.query.get_or_404(item_id)
    new_status = request.get_json().get('status')
    if new_status in ['draft', 'in_progress', 'ready', 'scheduled', 'published', 'archived', 'error']:
        item.status = new_status
        db.session.commit()
    return jsonify({'status': item.status})


# ─────────────────────── DISTRIBUTE ───────────────────────

@app.route('/distribute')
def distribute():
    """Verteilungssystem: Content zu Accounts zuweisen."""
    content_id = request.args.get('content_id', type=int)
    item = ContentItem.query.get(content_id) if content_id else None
    ready_items = ContentItem.query.filter_by(status='ready').order_by(ContentItem.created_at.desc()).all()
    all_accounts = Account.query.filter_by(status='active').order_by(Account.name).all()
    categories = Category.query.all()
    return render_template('distribute.html',
        item=item, ready_items=ready_items, all_accounts=all_accounts,
        categories=categories, active_page='content')


@app.route('/distribute/auto', methods=['POST'])
def distribute_auto():
    """Auto-distribute content to matching accounts by category."""
    content_id = request.form.get('content_id', type=int)
    item = ContentItem.query.get_or_404(content_id)

    matched = []
    if item.category_id:
        matching_accounts = Account.query.filter_by(
            category_id=item.category_id, status='active'
        ).all()
        for acc in matching_accounts:
            if acc not in item.accounts:
                item.accounts.append(acc)
                matched.append(acc.name)

    db.session.commit()
    if matched:
        flash(f'Automatisch zugewiesen an: {", ".join(matched)}', 'success')
    else:
        flash('Keine passenden Accounts gefunden (gleiche Kategorie)', 'info')
    return redirect(url_for('distribute', content_id=content_id))


@app.route('/distribute/manual', methods=['POST'])
def distribute_manual():
    """Manually assign content to selected accounts and create scheduled posts."""
    content_id = request.form.get('content_id', type=int)
    account_ids = request.form.getlist('account_ids', type=int)
    post_type = request.form.get('post_type', 'feed')
    schedule_time = request.form.get('schedule_time', '')

    item = ContentItem.query.get_or_404(content_id)
    created = 0

    for acc_id in account_ids:
        acc = Account.query.get(acc_id)
        if not acc:
            continue

        if acc not in item.accounts:
            item.accounts.append(acc)

        # Create scheduled post
        if schedule_time:
            try:
                sched_at = datetime.fromisoformat(schedule_time)
            except ValueError:
                sched_at = datetime.utcnow() + timedelta(hours=24)
        else:
            # Find next optimal posting slot
            cfg = acc.ai_config
            times = cfg.get_posting_times() if cfg else ['09:00', '18:00']
            sched_at = datetime.utcnow() + timedelta(days=1)
            for t in times:
                h, m = map(int, t.split(':'))
                candidate = datetime.utcnow().replace(hour=h, minute=m, second=0) + timedelta(days=1)
                existing = ScheduledPost.query.filter_by(account_id=acc_id, status='scheduled')\
                    .filter(ScheduledPost.scheduled_at == candidate).first()
                if not existing:
                    sched_at = candidate
                    break

        post = ScheduledPost(
            account_id=acc_id,
            content_item_id=content_id,
            caption=item.caption or item.title,
            post_type=post_type,
            status='scheduled',
            scheduled_at=sched_at,
        )
        db.session.add(post)
        created += 1

    item.status = 'scheduled' if created > 0 else item.status
    db.session.commit()
    flash(f'{created} Posts geplant für {len(account_ids)} Accounts.', 'success')
    return redirect(url_for('distribute', content_id=content_id))


# ─────────────────────── MEDIA LIBRARY ───────────────────────

@app.route('/media')
def media_library():
    q = request.args.get('q', '')
    file_type = request.args.get('type', '')
    category_id = request.args.get('category', type=int)

    query = MediaItem.query
    if q:
        query = query.filter(MediaItem.original_filename.ilike(f'%{q}%'))
    if file_type:
        query = query.filter_by(file_type=file_type)
    if category_id:
        query = query.filter_by(category_id=category_id)

    page = request.args.get('page', 1, type=int)
    per_page = 60
    pagination = query.order_by(MediaItem.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    categories = Category.query.order_by(Category.name).all()

    # 1 GROUP-BY statt 3 separater COUNT-Queries
    _tc_rows = db.session.query(MediaItem.file_type, func.count(MediaItem.id))\
                         .group_by(MediaItem.file_type).all()
    _tc_map = {ft: cnt for ft, cnt in _tc_rows}
    type_counts = {
        'image': _tc_map.get('image', 0),
        'video': _tc_map.get('video', 0),
        'other': sum(v for k, v in _tc_map.items() if k not in ('image', 'video')),
    }

    all_accounts = Account.query.filter_by(status='active').order_by(Account.name).all()
    return render_template('media.html',
        items=pagination.items, pagination=pagination,
        categories=categories, type_counts=type_counts,
        accounts=all_accounts,
        filters={'q': q, 'type': file_type, 'category': category_id},
        active_page='media')


@app.route('/media/upload', methods=['POST'])
def media_upload():
    if 'files' not in request.files:
        flash('Keine Dateien ausgewählt.', 'error')
        return redirect(url_for('media_library'))

    files = request.files.getlist('files')
    category_id = request.form.get('category_id', type=int)
    uploaded = 0

    for file in files:
        if file and file.filename and allowed_file(file.filename):
            original = secure_filename(file.filename)
            ext = original.rsplit('.', 1)[1].lower()
            ftype = get_file_type(original)
            mime = mimetypes.guess_type(original)[0] or 'application/octet-stream'

            # Datei in Bytes lesen (für Cloudinary + Fallback)
            file_bytes = file.read()
            cl = _cloudinary_upload(io.BytesIO(file_bytes), original)

            if cl:
                media = MediaItem(
                    filename=cl['public_id'],
                    original_filename=original,
                    file_type=ftype,
                    mime_type=mime,
                    file_size=cl.get('bytes', len(file_bytes)),
                    width=cl.get('width'),
                    height=cl.get('height'),
                    url=cl['secure_url'],
                    storage_source='cloudinary',
                    category_id=category_id,
                )
            else:
                # Fallback: lokaler Speicher
                unique_name = f"{uuid.uuid4().hex}.{ext}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_name)
                with open(filepath, 'wb') as f:
                    f.write(file_bytes)
                media = MediaItem(
                    filename=unique_name,
                    original_filename=original,
                    file_type=ftype,
                    mime_type=mime,
                    file_size=os.path.getsize(filepath),
                    url=f'/media/file/{unique_name}',
                    storage_source='local',
                    category_id=category_id,
                )
            db.session.add(media)
            uploaded += 1

    db.session.commit()
    flash(f'{uploaded} Datei(en) hochgeladen.', 'success')
    return redirect(url_for('media_library'))


@app.route('/media/file/<filename>')
def media_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/media/<int:media_id>/delete', methods=['POST'])
def media_delete(media_id):
    item = MediaItem.query.get_or_404(media_id)
    if item.storage_source == 'cloudinary':
        # Cloudinary public_ids have no file extension — use the stored file_type
        # column instead (values: 'image', 'video', 'reel', 'story', …)
        rtype = 'video' if item.file_type in {'video', 'reel'} else 'image'
        _cloudinary_delete(item.filename, resource_type=rtype)
    else:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], item.filename)
        if os.path.exists(filepath):
            os.remove(filepath)
    # FK-Constraint: ScheduledPost.media_item_id → NULL setzen vor dem Löschen
    ScheduledPost.query.filter_by(media_item_id=media_id).update({'media_item_id': None})
    db.session.flush()
    db.session.delete(item)
    db.session.commit()
    flash('Datei gelöscht.', 'info')
    return redirect(url_for('media_library'))


# ─────────────────────── CALENDAR ───────────────────────

@app.route('/calendar')
def calendar_view():
    all_accounts = Account.query.filter_by(status='active').order_by(Account.name).all()
    categories = Category.query.order_by(Category.name).all()
    return render_template('calendar.html',
        accounts=all_accounts, categories=categories, active_page='calendar')


@app.route('/api/calendar/events')
def calendar_events():
    account_id = request.args.get('account_id', type=int)
    start = request.args.get('start')
    end = request.args.get('end')

    query = ScheduledPost.query
    if account_id:
        query = query.filter_by(account_id=account_id)
    if start:
        query = query.filter(ScheduledPost.scheduled_at >= start)
    if end:
        query = query.filter(ScheduledPost.scheduled_at <= end)

    posts = query.options(joinedload(ScheduledPost.account)).all()
    type_icons = {'feed': '📸', 'reel': '🎬', 'story': '⭕', 'carousel': '🎠'}

    # Batch-load aller referenzierten MediaItems und ContentItems in je 1 Query
    media_ids_needed  = list({p.media_item_id for p in posts if p.media_item_id})
    content_ids_needed = list({p.content_item_id for p in posts if p.content_item_id})
    media_map   = {m.id: m for m in MediaItem.query.filter(MediaItem.id.in_(media_ids_needed)).all()} if media_ids_needed else {}
    content_map = {c.id: c for c in ContentItem.query.filter(ContentItem.id.in_(content_ids_needed)).all()} if content_ids_needed else {}

    # Farben: primär nach slot_type, sekundär nach Status
    def get_color(p):
        slot = getattr(p, 'slot_type', 'fixed') or 'fixed'
        if slot == 'disabled':  return '#374151'   # dunkelgrau — kein Post
        if slot == 'flexible':  return '#059669'   # grün — flexibel aus Vorrat
        # fixed: Farbe nach Status
        return {'scheduled': '#3b82f6', 'published': '#10b981',
                'failed': '#ef4444', 'draft': '#6b7280'}.get(p.status, '#3b82f6')

    def get_title(p, acc):
        slot = getattr(p, 'slot_type', 'fixed') or 'fixed'
        name = acc.name if acc else '?'
        if slot == 'disabled':  return f'🚫 {name} — Manuell'
        if slot == 'flexible':  return f'🔀 {name} — Flexibel'
        return f"{type_icons.get(p.post_type,'📌')} {name}"

    events = []
    for p in posts:
        acc  = p.account
        slot = getattr(p, 'slot_type', 'fixed') or 'fixed'
        mi   = media_map.get(p.media_item_id)
        ci   = content_map.get(p.content_item_id)
        events.append({
            'id': p.id,
            'title': get_title(p, acc),
            'start': p.scheduled_at.isoformat(),
            'color': get_color(p),
            'textColor': 'white',
            'borderColor': 'transparent',
            'editable': p.status in ('scheduled', 'disabled'),
            'extendedProps': {
                'account': acc.name if acc else '',
                'account_id': p.account_id,
                'type': p.post_type,
                'status': p.status,
                'slot_type': slot,
                'caption': (p.caption or '')[:150],
                'post_id': p.id,
                'media_ids': p.get_media_ids(),
                'media_count': len(p.get_media_ids()),
                'media_url': mi.url if mi else None,
                'content_item_id': p.content_item_id,
                'content_title': ci.title[:60] if ci else None,
            }
        })
    return jsonify(events)


# ─────────────────────── ANALYTICS ───────────────────────

@app.route('/analytics')
def analytics():
    all_accounts = Account.query.filter_by(status='active').order_by(Account.follower_count.desc()).all()
    categories = Category.query.all()
    total_followers = sum(a.follower_count for a in all_accounts)

    cat_stats = []
    for cat in categories:
        accs = Account.query.filter_by(category_id=cat.id, status='active').all()
        if accs:
            followers = sum(a.follower_count for a in accs)
            cat_stats.append({
                'name': cat.name, 'color': cat.color,
                'accounts': len(accs), 'followers': followers,
                'pct': round(followers / total_followers * 100, 1) if total_followers else 0
            })
    cat_stats.sort(key=lambda x: x['followers'], reverse=True)

    return render_template('analytics.html',
        accounts=all_accounts, cat_stats=cat_stats, total_followers=total_followers,
        active_page='analytics')


@app.route('/api/analytics/growth')
def analytics_growth():
    days = request.args.get('days', 30, type=int)
    account_id = request.args.get('account_id', type=int)
    include_forecast = request.args.get('forecast', '0') == '1'

    labels, data = [], []
    for i in range(days - 1, -1, -1):
        day = datetime.utcnow() - timedelta(days=i)
        labels.append(day.strftime('%d.%m'))
        total = db.session.query(func.sum(AnalyticsSnapshot.followers))\
            .filter(func.date(AnalyticsSnapshot.recorded_at) == day.date())
        if account_id:
            total = total.filter(AnalyticsSnapshot.account_id == account_id)
        data.append(total.scalar() or 0)

    result = {'labels': labels, 'data': data}
    if include_forecast:
        forecast_vals = linear_forecast(data, 14)
        forecast_labels = [(datetime.utcnow() + timedelta(days=i+1)).strftime('%d.%m') for i in range(14)]
        result['forecast_labels'] = forecast_labels
        result['forecast_data'] = forecast_vals

    return jsonify(result)


@app.route('/api/analytics/portfolio')
def analytics_portfolio():
    """
    Gesamt-Follower über alle Accounts pro Tag.
    Lücken werden mit dem zuletzt bekannten Wert aufgefüllt (forward-fill).
    Heute = max(letzter Snapshot, aktueller Account.follower_count).
    """
    days = request.args.get('days', 30, type=int)
    today = datetime.utcnow().date()

    # Aktuelles Portfolio-Total aus Account.follower_count
    current_total = db.session.query(func.sum(Account.follower_count))\
        .filter(Account.status == 'active').scalar() or 0

    # Pro Account + Tag: nur den NEUESTEN Snapshot nehmen, dann über alle Accounts summieren.
    # So werden mehrfache Updates am selben Tag nicht aufsummiert.
    start_date = today - timedelta(days=days - 1)

    # Subquery: spätester recorded_at pro (account_id, tag)
    latest_per_acc_day = db.session.query(
        AnalyticsSnapshot.account_id,
        func.date(AnalyticsSnapshot.recorded_at).label('snap_day'),
        func.max(AnalyticsSnapshot.recorded_at).label('latest_at')
    ).filter(
        func.date(AnalyticsSnapshot.recorded_at) >= start_date
    ).group_by(
        AnalyticsSnapshot.account_id,
        func.date(AnalyticsSnapshot.recorded_at)
    ).subquery()

    # Haupt-Query: followers des neuesten Snapshots summieren
    rows = db.session.query(
        latest_per_acc_day.c.snap_day.label('day'),
        func.sum(AnalyticsSnapshot.followers).label('total')
    ).join(
        AnalyticsSnapshot,
        db.and_(
            AnalyticsSnapshot.account_id == latest_per_acc_day.c.account_id,
            AnalyticsSnapshot.recorded_at == latest_per_acc_day.c.latest_at
        )
    ).group_by(latest_per_acc_day.c.snap_day)\
     .order_by(latest_per_acc_day.c.snap_day).all()

    # In dict umwandeln
    snap_by_day = {str(r.day): int(r.total) for r in rows}

    labels, data = [], []
    last_known = None
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        day_str = d.strftime('%d.%m')
        db_key  = str(d)
        labels.append(day_str)
        if db_key in snap_by_day:
            last_known = snap_by_day[db_key]
        val = last_known  # forward-fill
        # Für heute: nimm das Maximum aus Snapshot und aktuellem Stand
        if i == 0:
            val = max(val or 0, current_total)
        data.append(val)

    # Wachstum berechnen (erster bekannter Wert vs. letzter)
    first_val = next((v for v in data if v), 0)
    last_val  = data[-1] or 0
    delta     = last_val - first_val

    return jsonify({
        'labels':  labels,
        'data':    data,
        'current': current_total,
        'delta':   delta,
        'days':    days,
    })


@app.route('/analytics/export')
def analytics_export():
    """Export analytics data as CSV."""
    from io import StringIO
    import csv
    from flask import Response

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Account', 'Kategorie', 'Plattform', 'Follower', 'Automation', 'Priorität', 'Vorrat (Tage)', 'Status'])

    accounts = Account.query.order_by(Account.follower_count.desc()).all()
    for acc in accounts:
        writer.writerow([
            acc.name, acc.category.name if acc.category else '',
            acc.platform.name if acc.platform else '',
            acc.follower_count, acc.automation_level,
            acc.priority, acc.stock_days_display(), acc.status
        ])

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=content-os-analytics-{datetime.now().strftime("%Y%m%d")}.csv'}
    )


# ─────────────────────── AUTOMATION ───────────────────────

@app.route('/automation')
def automation():
    rules = AutomationRule.query.order_by(AutomationRule.active.desc(), AutomationRule.name).all()
    all_accounts = Account.query.order_by(Account.name).all()
    return render_template('automation.html',
        rules=rules, accounts=all_accounts, active_page='automation',
        emergency_pause=_is_emergency_paused())


@app.route('/api/automation/emergency-pause', methods=['POST'])
@login_required
def api_emergency_pause():
    data   = request.get_json(force=True) or {}
    action = data.get('action')   # 'pause' oder 'resume'
    val    = '1' if action == 'pause' else '0'
    _set_setting('emergency_pause', val)
    _invalidate_ep_cache()   # force cache refresh on next request/scheduler tick
    return jsonify({'ok': True, 'paused': val == '1'})


@app.route('/automation/new', methods=['GET', 'POST'])
def automation_new():
    if request.method == 'POST':
        d = request.form
        rule = AutomationRule(
            account_id=int(d['account_id']) if d.get('account_id') else None,
            name=d['name'],
            rule_type=d.get('rule_type', 'rss'),
            active=d.get('active') == 'on',
            source_config=d.get('source_config', '{}'),
            action_config=d.get('action_config', '{}'),
            run_interval_minutes=int(d.get('run_interval_minutes') or 60),
        )
        db.session.add(rule)
        db.session.commit()
        flash('Automatisierung erstellt.', 'success')
        return redirect(url_for('automation'))

    all_accounts = Account.query.order_by(Account.name).all()
    return render_template('automation_form.html',
        rule=None, accounts=all_accounts, active_page='automation')


@app.route('/automation/<int:rule_id>/toggle', methods=['POST'])
def automation_toggle(rule_id):
    rule = AutomationRule.query.get_or_404(rule_id)
    rule.active = not rule.active
    db.session.commit()
    return jsonify({'active': rule.active})


@app.route('/automation/<int:rule_id>/run', methods=['POST'])
def automation_run(rule_id):
    """Trigger manual run of an automation rule."""
    rule = AutomationRule.query.get_or_404(rule_id)
    threading.Thread(target=run_automation_rule, args=(rule_id,), daemon=True).start()
    # Kurz warten damit der Thread starten kann
    import time; time.sleep(0.3)
    last_log = AutomationRunLog.query.filter_by(rule_id=rule_id)\
        .order_by(AutomationRunLog.started_at.desc()).first()
    created = last_log.items_created if last_log else 0
    return jsonify({'ok': True, 'message': 'Gestartet', 'created': created})


# ─────────────────────── AI CONFIG ───────────────────────

@app.route('/ai')
def ai_config_list():
    all_accounts = Account.query.order_by(Account.name).all()
    return render_template('ai.html', accounts=all_accounts, active_page='ai')


@app.route('/ai/<int:account_id>', methods=['GET', 'POST'])
def ai_config_detail(account_id):
    account = Account.query.get_or_404(account_id)
    cfg = account.ai_config
    if not cfg:
        cfg = AIConfig(account_id=account_id)
        db.session.add(cfg)
        db.session.commit()

    if request.method == 'POST':
        d = request.form
        cfg.headline_min_words = int(d.get('headline_min_words') or 5)
        cfg.headline_max_words = int(d.get('headline_max_words') or 12)
        cfg.headline_style = d.get('headline_style', 'neutral')
        cfg.caption_min_words = int(d.get('caption_min_words') or 50)
        cfg.caption_max_words = int(d.get('caption_max_words') or 300)
        cfg.caption_tone = d.get('caption_tone', 'informativ')
        cfg.caption_hashtags = int(d.get('caption_hashtags') or 10)
        cfg.image_style = d.get('image_style', 'news')
        cfg.primary_color = d.get('primary_color', '#1a1a2e')
        cfg.accent_color = d.get('accent_color', '#e94560')
        cfg.auto_approve = d.get('auto_approve') == 'on'
        cfg.ai_model = d.get('ai_model', 'claude-sonnet-4-6')
        cfg.persona = d.get('persona', '')
        times = [t.strip() for t in d.get('posting_times', '').split(',') if t.strip()]
        cfg.posting_times = json.dumps(times or ['09:00', '18:00'])
        db.session.commit()
        flash('KI-Konfiguration gespeichert.', 'success')
        return redirect(url_for('ai_config_detail', account_id=account_id))

    all_accounts = Account.query.order_by(Account.name).all()
    return render_template('ai_config.html',
        account=account, cfg=cfg, accounts=all_accounts, active_page='ai')


# ─────────────────────── TEAM ───────────────────────

@app.route('/team')
def team():
    members = TeamMember.query.order_by(TeamMember.role, TeamMember.name).all()
    return render_template('team.html', members=members, active_page='team')


@app.route('/team/new', methods=['GET', 'POST'])
def team_new():
    if request.method == 'POST':
        d = request.form
        member = TeamMember(name=d['name'], email=d['email'], role=d.get('role', 'editor'))
        db.session.add(member)
        db.session.commit()
        flash(f'Teammitglied "{member.name}" hinzugefügt.', 'success')
        return redirect(url_for('team'))
    return render_template('team_form.html', member=None, active_page='team')


@app.route('/team/<int:member_id>/edit', methods=['GET', 'POST'])
def team_edit(member_id):
    member = TeamMember.query.get_or_404(member_id)
    if request.method == 'POST':
        d = request.form
        member.name = d.get('name', member.name)
        member.email = d.get('email', member.email)
        member.role = d.get('role', member.role)
        member.active = d.get('active') == 'on'
        db.session.commit()
        flash(f'"{member.name}" aktualisiert.', 'success')
        log_activity('team_updated', f'Teammitglied {member.name} bearbeitet')
        return redirect(url_for('team'))
    return render_template('team_form.html', member=member, active_page='team')


@app.route('/team/<int:member_id>/delete', methods=['POST'])
def team_delete(member_id):
    member = TeamMember.query.get_or_404(member_id)
    name = member.name
    # FK-Constraints auf PostgreSQL: alle referenzierenden Spalten nullen
    Account.query.filter_by(team_member_id=member_id).update({'team_member_id': None})
    ContentItem.query.filter_by(author_id=member_id).update({'author_id': None})
    ContentItem.query.filter_by(reviewed_by_id=member_id).update({'reviewed_by_id': None})
    ScheduledPost.query.filter_by(created_by_id=member_id).update({'created_by_id': None})
    MediaItem.query.filter_by(uploaded_by_id=member_id).update({'uploaded_by_id': None})
    db.session.flush()
    db.session.delete(member)
    db.session.commit()
    flash(f'"{name}" entfernt.', 'success')
    return redirect(url_for('team'))


# ─────────────────────── ALERTS ───────────────────────

@app.route('/alerts')
def alerts_center():
    alerts = SystemAlert.query.order_by(SystemAlert.resolved, SystemAlert.severity.desc(),
                                        SystemAlert.created_at.desc()).all()
    return render_template('alerts.html', alerts=alerts, active_page='dashboard')


@app.route('/alerts/refresh', methods=['POST'])
def alerts_refresh():
    generate_alerts()
    flash('Alerts neu generiert.', 'success')
    return redirect(url_for('alerts_center'))


# ─────────────────────── SETTINGS ───────────────────────

@app.route('/settings')
def settings():
    categories = Category.query.order_by(Category.name).all()
    labels = Label.query.order_by(Label.name).all()
    platforms = Platform.query.all()
    return render_template('settings.html',
        categories=categories, labels=labels, platforms=platforms, active_page='settings')


@app.route('/settings/category/new', methods=['POST'])
def category_new():
    d = request.form
    cat = Category(name=d['name'], color=d.get('color', '#6366f1'), icon=d.get('icon', 'folder'))
    db.session.add(cat)
    db.session.commit()
    flash(f'Kategorie "{cat.name}" erstellt.', 'success')
    return redirect(url_for('settings'))


@app.route('/settings/label/new', methods=['POST'])
def label_new():
    d = request.form
    label = Label(name=d['name'], color=d.get('color', '#6366f1'))
    db.session.add(label)
    db.session.commit()
    flash(f'Label "{label.name}" erstellt.', 'success')
    return redirect(url_for('settings'))


# ─────────────────────── API ───────────────────────

@app.route('/api/categories')
def api_categories():
    cats = Category.query.order_by(Category.name).all()
    return jsonify([{'id': c.id, 'name': c.name, 'color': c.color, 'icon': c.icon} for c in cats])


@app.route('/api/accounts')
def api_accounts():
    accounts = Account.query.filter_by(status='active')\
        .options(joinedload(Account.platform), joinedload(Account.category))\
        .order_by(Account.follower_count.desc()).all()
    # Batch-Stock-Query statt N×1 stock_status()-DB-Queries
    days_map = _get_planned_days_batch(accounts)
    return jsonify([{
        'id': a.id, 'name': a.name, 'handle': a.handle,
        'followers': a.follower_count, 'status': a.status,
        'category': a.category.name if a.category else '',
        'platform': a.platform.name if a.platform else '',
        'stock_status': _days_to_status(days_map[a.id]),
        'stock_days': round(days_map[a.id], 1),
    } for a in accounts])


@app.route('/api/stats')
def api_stats():
    stats = get_dashboard_stats()
    return jsonify({k: v for k, v in stats.items() if not isinstance(v, list)})


@app.route('/api/alerts/resolve/<int:alert_id>', methods=['POST'])
def resolve_alert(alert_id):
    alert = SystemAlert.query.get_or_404(alert_id)
    alert.resolved = True
    alert.resolved_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/content/duplicate-check', methods=['POST'])
def duplicate_check():
    title = request.get_json().get('title', '')
    dup = is_duplicate(title)
    return jsonify({'is_duplicate': dup})


# ─────────────────────── BULK CONTENT ACTIONS ───────────────────────

@app.route('/api/content/bulk', methods=['POST'])
def content_bulk():
    d = request.get_json()
    ids = d.get('ids', [])
    action = d.get('action')
    value = d.get('value')

    items = ContentItem.query.filter(ContentItem.id.in_(ids)).all()
    count = 0

    for item in items:
        if action == 'status' and value in ['draft','in_progress','ready','scheduled','published','archived','error']:
            item.status = value
            count += 1
        elif action == 'category' and value:
            item.category_id = int(value)
            count += 1
        elif action == 'delete':
            db.session.delete(item)
            count += 1
        elif action == 'assign_account' and value:
            acc = Account.query.get(int(value))
            if acc and acc not in item.accounts:
                item.accounts.append(acc)
            count += 1

    db.session.commit()
    return jsonify({'ok': True, 'affected': count})


# ─────────────────────── AUTOMATION RUN LOG ───────────────────────

@app.route('/api/automation/<int:rule_id>/logs')
def automation_logs(rule_id):
    logs = AutomationRunLog.query.filter_by(rule_id=rule_id)\
        .order_by(AutomationRunLog.started_at.desc()).limit(20).all()
    return jsonify([{
        'id': l.id,
        'started_at': l.started_at.isoformat(),
        'finished_at': l.finished_at.isoformat() if l.finished_at else None,
        'status': l.status,
        'items_found': l.items_found,
        'items_created': l.items_created,
        'items_skipped': l.items_skipped,
        'error': l.error_message,
    } for l in logs])


# ─────────────────────── MEDIA → POST ───────────────────────

@app.route('/api/media/<int:media_id>/create-post', methods=['POST'])
def media_create_post(media_id):
    d = request.get_json()
    account_id = d.get('account_id')
    caption = d.get('caption', '')
    post_type = d.get('post_type', 'feed')
    scheduled_at_str = d.get('scheduled_at')

    if not account_id:
        return jsonify({'ok': False, 'error': 'account_id fehlt'}), 400

    try:
        sched = datetime.fromisoformat(scheduled_at_str) if scheduled_at_str else datetime.utcnow() + timedelta(days=1)
    except (ValueError, TypeError):
        sched = datetime.utcnow() + timedelta(days=1)

    post = ScheduledPost(
        account_id=int(account_id),
        media_item_id=media_id,
        caption=caption,
        post_type=post_type,
        status='scheduled',
        scheduled_at=sched,
    )
    db.session.add(post)

    # increment usage count
    media = MediaItem.query.get(media_id)
    if media:
        media.usage_count = (media.usage_count or 0) + 1

    db.session.commit()
    return jsonify({'ok': True, 'post_id': post.id})


# ─────────────────────── ANALYTICS: DAILY DELTA ───────────────────────

@app.route('/api/analytics/daily-growth')
def analytics_daily_growth():
    days = request.args.get('days', 30, type=int)
    account_id = request.args.get('account_id', type=int)

    labels, deltas, eng_rates = [], [], []
    prev = None

    for i in range(days - 1, -1, -1):
        day = datetime.utcnow() - timedelta(days=i)
        q = db.session.query(func.sum(AnalyticsSnapshot.followers))\
            .filter(func.date(AnalyticsSnapshot.recorded_at) == day.date())
        eq = db.session.query(func.avg(AnalyticsSnapshot.engagement_rate))\
            .filter(func.date(AnalyticsSnapshot.recorded_at) == day.date())
        if account_id:
            q = q.filter(AnalyticsSnapshot.account_id == account_id)
            eq = eq.filter(AnalyticsSnapshot.account_id == account_id)

        total = q.scalar() or 0
        eng = round(eq.scalar() or 0, 2)
        delta = (total - prev) if prev is not None else 0
        prev = total

        labels.append(day.strftime('%d.%m'))
        deltas.append(delta)
        eng_rates.append(eng)

    return jsonify({'labels': labels, 'deltas': deltas, 'engagement': eng_rates})


# ─────────────────────── SETTINGS CRUD ───────────────────────

@app.route('/settings/category/<int:cat_id>/delete', methods=['POST'])
def category_delete(cat_id):
    cat = Category.query.get_or_404(cat_id)
    # FK-Constraints: alle referenzierenden Tabellen nullen
    Account.query.filter_by(category_id=cat_id).update({'category_id': None})
    ContentItem.query.filter_by(category_id=cat_id).update({'category_id': None})
    ContentTemplate.query.filter_by(category_id=cat_id).update({'category_id': None})
    MediaItem.query.filter_by(category_id=cat_id).update({'category_id': None})
    HashtagSet.query.filter_by(category_id=cat_id).update({'category_id': None})
    db.session.flush()
    db.session.delete(cat)
    db.session.commit()
    flash(f'Kategorie "{cat.name}" gelöscht.', 'info')
    return redirect(url_for('settings'))


@app.route('/settings/category/<int:cat_id>/edit', methods=['POST'])
def category_edit(cat_id):
    cat = Category.query.get_or_404(cat_id)
    cat.name = request.form.get('name', cat.name)
    cat.color = request.form.get('color', cat.color)
    cat.icon = request.form.get('icon', cat.icon)
    db.session.commit()
    flash('Kategorie aktualisiert.', 'success')
    return redirect(url_for('settings'))


@app.route('/settings/label/<int:label_id>/delete', methods=['POST'])
def label_delete(label_id):
    label = Label.query.get_or_404(label_id)
    name = label.name
    db.session.delete(label)
    db.session.commit()
    flash(f'Label "{name}" gelöscht.', 'info')
    return redirect(url_for('settings'))


@app.route('/settings/platform/<int:platform_id>/delete', methods=['POST'])
def platform_delete(platform_id):
    p = Platform.query.get_or_404(platform_id)
    if p.accounts:
        flash(f'Plattform "{p.name}" hat noch {len(p.accounts)} Accounts — zuerst umziehen.', 'error')
        return redirect(url_for('settings'))
    db.session.delete(p)
    db.session.commit()
    flash(f'Plattform "{p.name}" gelöscht.', 'success')
    return redirect(url_for('settings'))


@app.route('/settings/platform/new', methods=['POST'])
def platform_new():
    d = request.form
    p = Platform(name=d['name'], icon=d.get('icon', 'globe'), color=d.get('color', '#6366f1'))
    db.session.add(p)
    db.session.commit()
    flash(f'Plattform "{p.name}" erstellt.', 'success')
    return redirect(url_for('settings'))


# ─────────────────────── AUTH ROUTES ───────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('user_id'):
        return redirect(url_for('dashboard'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username, active=True).first()
        if user and user.check_password(password):
            session['user_id'] = user.id
            session['user_role'] = user.role
            session['user_name'] = user.username
            user.last_login = datetime.utcnow()
            db.session.commit()
            # Open-Redirect-Schutz: nur relative URLs erlaubt
            from urllib.parse import urlparse
            next_url = request.args.get('next', '')
            if next_url and urlparse(next_url).netloc:
                next_url = ''
            return redirect(next_url or url_for('dashboard'))
        error = 'Ungültige Zugangsdaten.'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    user = current_user()
    if request.method == 'POST':
        d = request.form
        if d.get('new_password'):
            if not user.check_password(d.get('current_password', '')):
                flash('Aktuelles Passwort falsch.', 'error')
                return redirect(url_for('profile'))
            user.set_password(d['new_password'])
        user.email = d.get('email', user.email)
        db.session.commit()
        flash('Profil gespeichert.', 'success')
        return redirect(url_for('profile'))
    recent_activity = ActivityLog.query.filter_by(user_id=user.id)\
        .order_by(ActivityLog.created_at.desc()).limit(20).all()
    return render_template('profile.html', user=user, activity=recent_activity,
                           active_page='profile')


# ─────────────────────── FOLLOWER UPDATE ───────────────────────

@app.route('/api/accounts/<int:account_id>/update-followers', methods=['POST'])
@login_required
def update_followers(account_id):
    account = Account.query.get_or_404(account_id)
    d = request.get_json()
    new_count = d.get('follower_count')
    if new_count is None:
        return jsonify({'ok': False, 'error': 'follower_count fehlt'}), 400

    old_count = account.follower_count
    account.follower_count = int(new_count)

    # Write snapshot
    snap = AnalyticsSnapshot(
        account_id=account_id,
        followers=int(new_count),
        recorded_at=datetime.utcnow(),
    )
    db.session.add(snap)
    db.session.commit()

    log_activity('followers_updated',
                 f'{account.name}: {old_count} → {new_count}',
                 'account', account_id)
    return jsonify({'ok': True, 'old': old_count, 'new': int(new_count)})


# ─────────────────────── ACTIVITY LOG ───────────────────────

@app.route('/api/activity')
@login_required
def api_activity():
    limit = request.args.get('limit', 30, type=int)
    logs = ActivityLog.query.order_by(ActivityLog.created_at.desc()).limit(limit).all()
    return jsonify([{
        'id': l.id,
        'action': l.action,
        'description': l.description,
        'entity_type': l.entity_type,
        'entity_id': l.entity_id,
        'user': l.user.username if l.user else 'System',
        'created_at': l.created_at.isoformat(),
    } for l in logs])


# ─────────────────────── ACCOUNT BULK ───────────────────────

@app.route('/api/accounts/bulk', methods=['POST'])
@login_required
def accounts_bulk():
    d = request.get_json()
    ids = d.get('ids', [])
    action = d.get('action')
    value = d.get('value')

    accs = Account.query.filter(Account.id.in_(ids)).all()
    count = 0
    for acc in accs:
        if action == 'status' and value in ['active', 'paused', 'error', 'inactive']:
            acc.status = value; count += 1
        elif action == 'automation' and value is not None:
            acc.automation_level = int(value); count += 1
        elif action == 'priority' and value in ['critical', 'high', 'medium', 'low']:
            acc.priority = value; count += 1
        elif action == 'category' and value:
            acc.category_id = int(value); count += 1

    db.session.commit()
    return jsonify({'ok': True, 'affected': count})


# ─────────────────────── GLOBAL SEARCH ───────────────────────

@app.route('/api/search')
@login_required
def global_search():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify({'accounts': [], 'content': [], 'media': []})

    accounts = Account.query.filter(
        Account.name.ilike(f'%{q}%') | Account.handle.ilike(f'%{q}%')
    ).limit(5).all()

    content = ContentItem.query.filter(
        ContentItem.title.ilike(f'%{q}%') | ContentItem.caption.ilike(f'%{q}%')
    ).limit(5).all()

    media = MediaItem.query.filter(
        MediaItem.original_filename.ilike(f'%{q}%')
    ).limit(5).all()

    return jsonify({
        'accounts': [{'id': a.id, 'name': a.name, 'handle': a.handle,
                      'url': url_for('account_detail', account_id=a.id)} for a in accounts],
        'content':  [{'id': c.id, 'title': c.title, 'status': c.status,
                      'url': url_for('content_detail', item_id=c.id)} for c in content],
        'media':    [{'id': m.id, 'name': m.original_filename, 'type': m.file_type,
                      'url': url_for('media_library')} for m in media],
    })


# ─────────────────────── GROWTH GOAL ───────────────────────

@app.route('/api/accounts/<int:account_id>/growth-goal', methods=['POST'])
def account_growth_goal_save(account_id):
    acc = Account.query.get_or_404(account_id)
    d = request.get_json()
    try:
        acc.growth_goal = int(d.get('growth_goal')) if d.get('growth_goal') else None
        date_str = d.get('growth_goal_date', '')
        acc.growth_goal_date = datetime.strptime(date_str, '%Y-%m-%d') if date_str else None
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400


# ─────────────────────── ACCOUNT NOTES ───────────────────────

@app.route('/api/accounts/<int:account_id>/notes', methods=['POST'])
@login_required
def account_notes_save(account_id):
    acc = Account.query.get_or_404(account_id)
    acc.notes = request.get_json().get('notes', '')
    db.session.commit()
    return jsonify({'ok': True})


# ─────────────────────── CSV IMPORT ───────────────────────

@app.route('/accounts/import-csv', methods=['POST'])
@login_required
def accounts_import_csv():
    f = request.files.get('csv_file')
    if not f:
        flash('Keine Datei ausgewählt.', 'error')
        return redirect(url_for('accounts'))

    content = f.read().decode('utf-8-sig', errors='replace')
    reader = csv.DictReader(io.StringIO(content))

    platforms_map = {p.name.lower(): p for p in Platform.query.all()}
    categories_map = {c.name.lower(): c for c in Category.query.all()}

    created = skipped = errors = 0
    error_rows = []

    for i, row in enumerate(reader, 1):
        name = (row.get('name') or row.get('Name') or '').strip()
        if not name:
            skipped += 1
            continue

        platform_name = (row.get('platform') or row.get('Plattform') or 'Instagram').strip().lower()
        platform = platforms_map.get(platform_name)
        if not platform:
            # try partial match
            for k, v in platforms_map.items():
                if platform_name in k or k in platform_name:
                    platform = v
                    break
        if not platform:
            error_rows.append(f'Zeile {i}: Plattform "{platform_name}" unbekannt')
            errors += 1
            continue

        category_name = (row.get('category') or row.get('Kategorie') or '').strip().lower()
        category = categories_map.get(category_name) if category_name else None

        try:
            followers = int((row.get('follower_count') or row.get('Follower') or '0').replace('.', '').replace(',', '').strip())
        except ValueError:
            followers = 0

        handle = (row.get('handle') or row.get('Handle') or '').strip()
        status = (row.get('status') or row.get('Status') or 'active').strip().lower()
        if status not in ('active', 'paused', 'inactive', 'error'):
            status = 'active'

        acc = Account(
            name=name,
            handle=handle,
            platform_id=platform.id,
            category_id=category.id if category else None,
            follower_count=followers,
            status=status,
        )
        db.session.add(acc)
        created += 1

    try:
        db.session.commit()
        msg = f'{created} Accounts importiert'
        if skipped:
            msg += f', {skipped} übersprungen'
        if errors:
            msg += f', {errors} Fehler'
        flash(msg, 'success' if not errors else 'info')
        if error_rows:
            flash(' · '.join(error_rows[:5]), 'error')
        log_activity('accounts_imported', f'{created} Accounts per CSV importiert')
    except Exception as e:
        db.session.rollback()
        flash(f'Import-Fehler: {e}', 'error')

    return redirect(url_for('accounts'))


# ─────────────────────── QUICK POST ───────────────────────

@app.route('/api/posts/quick', methods=['POST'])
@login_required
def quick_post_create():
    d = request.get_json()
    account_id = d.get('account_id')
    caption = (d.get('caption') or '').strip()
    scheduled_at_str = d.get('scheduled_at')
    post_type = d.get('post_type', 'feed')

    if not account_id or not caption or not scheduled_at_str:
        return jsonify({'ok': False, 'error': 'Fehlende Felder'}), 400

    acc = Account.query.get(account_id)
    if not acc:
        return jsonify({'ok': False, 'error': 'Account nicht gefunden'}), 404

    try:
        scheduled_at = datetime.strptime(scheduled_at_str, '%Y-%m-%dT%H:%M')
    except ValueError:
        return jsonify({'ok': False, 'error': 'Ungültiges Datum'}), 400

    # Create a minimal ContentItem so the post has a reference
    content = ContentItem(
        title=caption[:120] + ('…' if len(caption) > 120 else ''),
        caption=caption,
        status='scheduled',
        content_type=post_type,
    )
    db.session.add(content)
    db.session.flush()

    post = ScheduledPost(
        account_id=account_id,
        content_item_id=content.id,
        caption=caption,
        post_type=post_type,
        status='scheduled',
        scheduled_at=scheduled_at,
    )
    db.session.add(post)
    acc.last_post_at = scheduled_at

    db.session.commit()
    log_activity('post_scheduled', f'Schnell-Post für {acc.name} am {scheduled_at.strftime("%d.%m %H:%M")} geplant')
    return jsonify({'ok': True, 'post_id': post.id})


# ─────────────────────── CONTENT DUPLICATE ───────────────────────

@app.route('/content/<int:item_id>/duplicate', methods=['POST'])
@login_required
def content_duplicate(item_id):
    orig = ContentItem.query.get_or_404(item_id)
    copy = ContentItem(
        title='[Kopie] ' + orig.title,
        raw_text=orig.raw_text,
        caption=orig.caption,
        source_url=orig.source_url,
        source_name=orig.source_name,
        category_id=orig.category_id,
        status='draft',
        content_type=orig.content_type,
        ai_headline=orig.ai_headline,
        ai_caption=orig.ai_caption,
    )
    for acc in orig.accounts:
        copy.accounts.append(acc)
    for lbl in orig.labels:
        copy.labels.append(lbl)

    db.session.add(copy)
    db.session.commit()
    log_activity('content_created', f'Content "{copy.title}" dupliziert von #{orig.id}')
    flash(f'"{orig.title}" wurde dupliziert.', 'success')
    return redirect(url_for('content_edit', item_id=copy.id))


# ─────────────────────── ACCOUNT CSV EXPORT ───────────────────────

@app.route('/accounts/export-csv')
def accounts_export_csv():
    from io import StringIO
    import csv as _csv
    from flask import Response
    output = StringIO()
    w = _csv.writer(output)
    w.writerow(['name','handle','platform','category','follower_count','status','automation_level','priority','notes'])
    for acc in Account.query.order_by(Account.follower_count.desc()).all():
        w.writerow([acc.name, acc.handle or '', acc.platform.name if acc.platform else '',
                    acc.category.name if acc.category else '', acc.follower_count,
                    acc.status, acc.automation_level, acc.priority, acc.notes or ''])
    return Response(output.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=accounts-{datetime.now().strftime("%Y%m%d")}.csv'})


# ─────────────────────── POSTING STREAK ───────────────────────

@app.route('/api/stats/streak')
def posting_streak():
    today = datetime.utcnow().date()
    streak = 0
    for i in range(365):
        day = today - timedelta(days=i)
        count = ScheduledPost.query.filter(
            func.date(ScheduledPost.scheduled_at) == day,
            ScheduledPost.status.in_(['scheduled', 'published'])
        ).count()
        if count > 0:
            streak += 1
        elif i > 0:
            break
    return jsonify({'streak': streak, 'date': today.isoformat()})


# ─────────────────────── ICAL EXPORT ───────────────────────

@app.route('/calendar/export.ics')
def calendar_ical():
    from flask import Response
    account_id = request.args.get('account_id', type=int)
    query = ScheduledPost.query.filter(
        ScheduledPost.status.in_(['scheduled', 'published']),
        ScheduledPost.scheduled_at >= datetime.utcnow() - timedelta(days=30)
    )
    if account_id:
        query = query.filter_by(account_id=account_id)
    posts = query.order_by(ScheduledPost.scheduled_at).all()

    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Content OS//DE',
             'CALSCALE:GREGORIAN', 'METHOD:PUBLISH']
    for p in posts:
        dt = p.scheduled_at.strftime('%Y%m%dT%H%M%SZ')
        summary = f"{p.account.name} — {p.post_type.capitalize()}" if p.account else p.post_type
        desc = (p.caption or '')[:200].replace('\n', '\\n').replace(',', '\\,')
        uid = f"post-{p.id}@content-os"
        lines += ['BEGIN:VEVENT', f'UID:{uid}', f'DTSTART:{dt}', f'DTEND:{dt}',
                  f'SUMMARY:{summary}', f'DESCRIPTION:{desc}', 'END:VEVENT']
    lines.append('END:VCALENDAR')
    return Response('\r\n'.join(lines), mimetype='text/calendar',
        headers={'Content-Disposition': 'attachment; filename=posting-plan.ics'})


# ─────────────────────── BULK FOLLOWER UPDATE ───────────────────────

@app.route('/api/accounts/bulk-followers', methods=['POST'])
def bulk_followers_update():
    updates = request.get_json().get('updates', [])  # [{id, followers}]
    count = 0
    for u in updates:
        acc = Account.query.get(u.get('id'))
        if acc and u.get('followers') is not None:
            _set_follower_count(acc, int(u['followers']))
            count += 1
    db.session.commit()
    log_activity('bulk_followers_updated', f'{count} Accounts Follower aktualisiert')
    return jsonify({'ok': True, 'updated': count})


# ─────────────────────── BEICHTEN DASHBOARD ───────────────────────

@app.route('/beichten')
def beichten_dashboard():
    cat = Category.query.filter(Category.name.ilike('%beicht%')).first()
    items = ContentItem.query
    if cat:
        items = items.filter_by(category_id=cat.id)
    else:
        items = items.filter(ContentItem.source_name.ilike('%beicht%'))
    items = items.order_by(ContentItem.created_at.desc()).all()
    return render_template('beichten.html', items=items, active_page='beichten')


# ─────────────────────── ACCOUNT GROUPS ───────────────────────

@app.route('/groups')
def account_groups():
    groups = AccountGroup.query.order_by(AccountGroup.name).all()
    all_accounts = Account.query.order_by(Account.name).all()
    return render_template('groups.html', groups=groups, all_accounts=all_accounts, active_page='accounts')

@app.route('/groups/new', methods=['POST'])
def group_new():
    d = request.form
    g = AccountGroup(name=d['name'], color=d.get('color','#3b82f6'), description=d.get('description',''))
    db.session.add(g)
    db.session.commit()
    account_ids = request.form.getlist('account_ids')
    for aid in account_ids:
        acc = Account.query.get(int(aid))
        if acc: g.accounts.append(acc)
    db.session.commit()
    flash(f'Gruppe "{g.name}" erstellt.', 'success')
    return redirect(url_for('account_groups'))

@app.route('/groups/<int:group_id>/edit', methods=['POST'])
def group_edit(group_id):
    g = AccountGroup.query.get_or_404(group_id)
    g.name = request.form.get('name', g.name)
    g.color = request.form.get('color', g.color)
    g.description = request.form.get('description', g.description)
    account_ids = request.form.getlist('account_ids')
    g.accounts = Account.query.filter(Account.id.in_([int(i) for i in account_ids])).all()
    db.session.commit()
    flash(f'Gruppe "{g.name}" aktualisiert.', 'success')
    return redirect(url_for('account_groups'))


@app.route('/groups/<int:group_id>/delete', methods=['POST'])
def group_delete(group_id):
    g = AccountGroup.query.get_or_404(group_id)
    db.session.delete(g)
    db.session.commit()
    flash('Gruppe gelöscht.', 'success')
    return redirect(url_for('account_groups'))

@app.route('/api/groups/<int:group_id>/members', methods=['POST'])
def group_update_members(group_id):
    g = AccountGroup.query.get_or_404(group_id)
    ids = request.get_json().get('account_ids', [])
    g.accounts = Account.query.filter(Account.id.in_(ids)).all()
    db.session.commit()
    return jsonify({'ok': True})


# ─────────────────────── CONTENT TEMPLATES ───────────────────────

@app.route('/templates')
@login_required
def content_templates():
    templates = ContentTemplate.query.order_by(ContentTemplate.created_at.desc()).all()
    categories = Category.query.order_by(Category.name).all()
    accounts   = Account.query.filter_by(status='active').order_by(Account.name).all()
    return render_template('content_templates.html', templates=templates,
                           categories=categories, accounts=accounts, active_page='content')

def _save_template_from_form(t):
    """Liest Formular-Daten + Datei-Upload in ein ContentTemplate-Objekt."""
    d = request.form
    t.name             = d['name']
    t.category_id      = int(d['category_id']) if d.get('category_id') else None
    t.content_type     = d.get('content_type', 'feed')
    t.caption_template = d.get('caption_template', '')
    t.cta_template     = d.get('cta_template', '')
    t.hashtags         = d.get('hashtags', '')
    t.notes            = d.get('notes', '')
    t.primary_color    = d.get('primary_color', '')
    t.secondary_color  = d.get('secondary_color', '')
    t.image_ratio      = d.get('image_ratio', '1:1')
    t.style_notes      = d.get('style_notes', '')
    t.posting_days     = json.dumps(request.form.getlist('posting_days'))
    t.posting_time_pref = d.get('posting_time_pref', '')
    # Ziel-Accounts (M2M)
    acc_ids = [int(x) for x in request.form.getlist('target_accounts') if x.isdigit()]
    t.target_accounts  = Account.query.filter(Account.id.in_(acc_ids)).all() if acc_ids else []
    # Bild-Upload
    file = request.files.get('preview_image')
    if file and file.filename and allowed_file(file.filename):
        original = secure_filename(file.filename)
        ext = original.rsplit('.', 1)[1].lower()
        file_bytes = file.read()
        cl = _cloudinary_upload(io.BytesIO(file_bytes), original)
        if cl:
            # Vollständige Cloudinary-URL speichern
            t.preview_image = cl['secure_url']
        else:
            unique_name = f"tmpl_{uuid.uuid4().hex}.{ext}"
            with open(os.path.join(app.config['UPLOAD_FOLDER'], unique_name), 'wb') as f:
                f.write(file_bytes)
            t.preview_image = unique_name


@app.route('/templates/new', methods=['POST'])
@login_required
def template_new():
    t = ContentTemplate()
    _save_template_from_form(t)
    db.session.add(t)
    db.session.commit()
    flash(f'Template "{t.name}" gespeichert.', 'success')
    return redirect(url_for('content_templates'))


@app.route('/templates/<int:tmpl_id>/edit', methods=['POST'])
@login_required
def template_edit(tmpl_id):
    t = ContentTemplate.query.get_or_404(tmpl_id)
    # Altes Bild löschen wenn neues hochgeladen
    old_img = t.preview_image
    _save_template_from_form(t)
    if t.preview_image and old_img and old_img != t.preview_image:
        try:
            os.remove(os.path.join(app.config['UPLOAD_FOLDER'], old_img))
        except Exception:
            pass
    db.session.commit()
    flash(f'Template "{t.name}" aktualisiert.', 'success')
    return redirect(url_for('content_templates'))


@app.route('/api/templates/<int:tmpl_id>')
@login_required
def template_get(tmpl_id):
    t = ContentTemplate.query.get_or_404(tmpl_id)
    return jsonify({
        'id': t.id, 'name': t.name,
        'category_id': t.category_id,
        'content_type': t.content_type,
        'caption_template': t.caption_template or '',
        'cta_template': t.cta_template or '',
        'hashtags': t.hashtags or '',
        'notes': t.notes or '',
        'primary_color': t.primary_color or '',
        'secondary_color': t.secondary_color or '',
        'image_ratio': t.image_ratio or '1:1',
        'style_notes': t.style_notes or '',
        'posting_days': t.get_posting_days(),
        'posting_time_pref': t.posting_time_pref or '',
        'preview_image': t.preview_image or '',
        'target_account_ids': [a.id for a in t.target_accounts],
    })

@app.route('/templates/<int:tmpl_id>/apply')
def template_apply(tmpl_id):
    t = ContentTemplate.query.get_or_404(tmpl_id)
    t.use_count += 1
    db.session.commit()
    # Redirect to new content form with prefilled values
    return redirect(url_for('content_new',
        tmpl_caption=t.caption_template, tmpl_hashtags=t.hashtags,
        tmpl_type=t.content_type, tmpl_cat=t.category_id or ''))

@app.route('/templates/<int:tmpl_id>/delete', methods=['POST'])
def template_delete(tmpl_id):
    t = ContentTemplate.query.get_or_404(tmpl_id)
    db.session.delete(t)
    db.session.commit()
    flash('Template gelöscht.', 'success')
    return redirect(url_for('content_templates'))

@app.route('/api/content/<int:item_id>/save-as-template', methods=['POST'])
def content_save_as_template(item_id):
    item = ContentItem.query.get_or_404(item_id)
    d = request.get_json()
    name = d.get('name') or item.title[:60]
    t = ContentTemplate(
        name=name,
        category_id=item.category_id,
        content_type=item.content_type,
        caption_template=item.caption or '',
        notes=f'Erstellt aus Content #{item.id}',
    )
    db.session.add(t)
    db.session.commit()
    return jsonify({'ok': True, 'id': t.id, 'name': t.name})


# ─────────────────────── CONTENT COMMENTS ───────────────────────

@app.route('/content/<int:item_id>/comments', methods=['POST'])
def comment_add(item_id):
    ContentItem.query.get_or_404(item_id)
    text = (request.get_json() or {}).get('text', '').strip()
    if not text:
        return jsonify({'ok': False, 'error': 'Leer'}), 400
    c = ContentComment(content_item_id=item_id, user_id=session.get('user_id'), text=text)
    db.session.add(c)
    db.session.commit()
    return jsonify({'ok': True, 'id': c.id,
                    'text': c.text,
                    'user': c.user.username if c.user else 'System',
                    'created_at': c.created_at.strftime('%d.%m %H:%M')})

@app.route('/content/<int:item_id>/comments/<int:comment_id>/delete', methods=['POST'])
def comment_delete(item_id, comment_id):
    c = ContentComment.query.get_or_404(comment_id)
    db.session.delete(c)
    db.session.commit()
    return jsonify({'ok': True})


# ─────────────────────── CAPTION SCORING ───────────────────────

def score_caption(caption: str) -> float:
    """Heuristischer Caption-Score 0–10."""
    if not caption:
        return 0.0
    score = 0.0
    length = len(caption)

    # Länge (ideal 150–1500)
    if 150 <= length <= 1500: score += 2.5
    elif 80 <= length < 150 or 1500 < length <= 2200: score += 1.5
    elif length > 50: score += 0.5

    # Hashtags (ideal 8–20)
    import re
    tags = len(re.findall(r'#\w+', caption))
    if 8 <= tags <= 20: score += 2.0
    elif 3 <= tags < 8 or 20 < tags <= 30: score += 1.0
    elif tags > 0: score += 0.5

    # Emojis
    emojis = len([c for c in caption if ord(c) > 0x2600])
    if emojis >= 3: score += 1.5
    elif emojis >= 1: score += 0.75

    # Zeilenumbrüche / Struktur
    lines = caption.count('\n')
    if lines >= 3: score += 1.0
    elif lines >= 1: score += 0.5

    # CTA-Wörter
    cta_words = ['link in bio', 'kommentier', 'folg', 'teile', 'spar', 'schreib',
                 'klick', 'jetzt', 'sichern', 'meld', 'bewirb']
    lower = caption.lower()
    if any(w in lower for w in cta_words): score += 1.5
    elif any(w in lower for w in ['mehr', 'infos', 'hier', 'heute']): score += 0.75

    # Frage
    if '?' in caption: score += 0.5

    return min(round(score, 1), 10.0)

@app.route('/api/content/<int:item_id>/score')
def content_score(item_id):
    item = ContentItem.query.get_or_404(item_id)
    s = score_caption(item.caption or '')
    return jsonify({'score': s})

@app.route('/api/caption-score', methods=['POST'])
def caption_score_live():
    caption = (request.get_json() or {}).get('caption', '')
    return jsonify({'score': score_caption(caption)})


# ─────────────────────── BEICHTEN-FORMULAR ───────────────────────

PUBLIC_ENDPOINTS.add('submit_form')
PUBLIC_ENDPOINTS.add('submit_beichte')

@app.route('/submit')
@app.route('/submit/<handle>')
def submit_form(handle=None):
    account = None
    if handle:
        account = Account.query.filter(
            Account.handle.ilike(f'%{handle}%') | Account.name.ilike(f'%{handle}%')
        ).first()
    accounts = Account.query.filter_by(status='active').order_by(Account.name).all()
    return render_template('submit.html', account=account, accounts=accounts)

@app.route('/api/submit', methods=['POST'])
def submit_beichte():
    d = request.get_json() or {}
    text = d.get('text', '').strip()
    account_id = d.get('account_id')
    contact = d.get('contact', '').strip()[:100]  # max 100 Zeichen

    if not text or len(text) < 20:
        return jsonify({'ok': False, 'error': 'Text zu kurz (min. 20 Zeichen)'}), 400

    # Finde Beichten-Kategorie
    cat = Category.query.filter(Category.name.ilike('%beicht%')).first()
    account = Account.query.get(account_id) if account_id else None

    item = ContentItem(
        title=text[:80] + ('…' if len(text) > 80 else ''),
        raw_text=text,
        caption=text,
        source_name=f'Beichten-Formular{" · " + contact if contact else ""}',
        category_id=cat.id if cat else None,
        status='draft',
        content_type='feed',
    )
    if account:
        item.accounts.append(account)
    db.session.add(item)
    db.session.commit()
    return jsonify({'ok': True})


# ─────────────────────── KUNDEN-LINK ───────────────────────

PUBLIC_ENDPOINTS.add('share_view')

@app.route('/share/<token>')
def share_view(token):
    acc = Account.query.filter_by(share_token=token).first_or_404()
    snapshots = AnalyticsSnapshot.query.filter_by(account_id=acc.id)\
        .order_by(AnalyticsSnapshot.recorded_at.desc()).limit(30).all()
    posts = ScheduledPost.query.filter_by(account_id=acc.id)\
        .order_by(ScheduledPost.scheduled_at.desc()).limit(10).all()
    return render_template('share.html', account=acc, snapshots=snapshots, posts=posts)

@app.route('/api/accounts/<int:account_id>/share-token', methods=['POST'])
def generate_share_token(account_id):
    acc = Account.query.get_or_404(account_id)
    if not acc.share_token:
        acc.share_token = secrets.token_urlsafe(32)
        db.session.commit()
    return jsonify({'ok': True, 'token': acc.share_token,
                    'url': f'/share/{acc.share_token}'})

@app.route('/api/accounts/<int:account_id>/share-token/revoke', methods=['POST'])
def revoke_share_token(account_id):
    acc = Account.query.get_or_404(account_id)
    acc.share_token = None
    db.session.commit()
    return jsonify({'ok': True})


# ─────────────────────── BESTE POSTING-ZEIT ───────────────────────

@app.route('/api/accounts/<int:account_id>/best-times')
def account_best_times(account_id):
    Account.query.get_or_404(account_id)
    posts = ScheduledPost.query.filter_by(account_id=account_id, status='published').all()
    if not posts:
        # Fallback: alle geplanten Posts analysieren
        posts = ScheduledPost.query.filter_by(account_id=account_id).all()

    hour_counts = [0] * 24
    for p in posts:
        if p.scheduled_at:
            hour_counts[p.scheduled_at.hour] += 1

    total = sum(hour_counts)
    if not total:
        return jsonify({'hours': [], 'recommendation': 'Noch keine Daten'})

    # Top 3 Stunden
    top = sorted(range(24), key=lambda h: hour_counts[h], reverse=True)[:3]
    top.sort()
    rec = ', '.join(f'{h:02d}:00 Uhr' for h in top if hour_counts[h] > 0)

    return jsonify({
        'hours': [{'hour': h, 'count': hour_counts[h], 'pct': round(hour_counts[h]/total*100)} for h in range(24)],
        'top_hours': top,
        'recommendation': rec or 'Noch keine Daten',
        'total_posts': total,
    })


# ─────────────────────── VERGLEICHS-ANALYTICS ───────────────────────

@app.route('/api/analytics/compare')
def analytics_compare():
    id1 = request.args.get('a', type=int)
    id2 = request.args.get('b', type=int)
    days = request.args.get('days', 30, type=int)

    result = {}
    now = datetime.utcnow()
    cutoff = now - timedelta(days=days)

    for label, acc_id in [('a', id1), ('b', id2)]:
        if not acc_id:
            result[label] = None
            continue
        acc = Account.query.get(acc_id)
        if not acc:
            result[label] = None
            continue

        # 1 Query für alle Snapshots des Zeitraums statt days×1 Queries
        snaps = AnalyticsSnapshot.query.filter(
            AnalyticsSnapshot.account_id == acc_id,
            AnalyticsSnapshot.recorded_at >= cutoff,
        ).order_by(AnalyticsSnapshot.recorded_at.asc()).all()
        # Pro Tag: neuesten Wert nehmen
        snap_by_date = {}
        for s in snaps:
            snap_by_date[s.recorded_at.date()] = s.followers

        data, chart_labels = [], []
        for i in range(days - 1, -1, -1):
            day = now - timedelta(days=i)
            chart_labels.append(day.strftime('%d.%m'))
            data.append(snap_by_date.get(day.date()))

        result[label] = {
            'id': acc.id, 'name': acc.name,
            'followers': acc.follower_count,
            'labels': chart_labels, 'data': data,
            'stock': acc.stock_days_display(),
            'category': acc.category.name if acc.category else '—',
        }
    return jsonify(result)


# ─────────────────────── SETTINGS IMPORT / EXPORT ───────────────────────

@app.route('/settings/export')
def settings_export():
    from flask import Response
    data = {
        'exported_at': datetime.utcnow().isoformat(),
        'categories': [{'name': c.name, 'color': c.color, 'icon': c.icon} for c in Category.query.all()],
        'labels': [{'name': l.name, 'color': l.color} for l in Label.query.all()],
        'platforms': [{'name': p.name, 'icon': p.icon, 'color': p.color} for p in Platform.query.all()],
        'groups': [{'name': g.name, 'color': g.color, 'description': g.description} for g in AccountGroup.query.all()],
        'templates': [{'name': t.name, 'content_type': t.content_type, 'caption_template': t.caption_template,
                       'hashtags': t.hashtags, 'notes': t.notes} for t in ContentTemplate.query.all()],
    }
    return Response(json.dumps(data, ensure_ascii=False, indent=2), mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename=content-os-config-{datetime.now().strftime("%Y%m%d")}.json'})


@app.route('/settings/import', methods=['POST'])
def settings_import():
    f = request.files.get('config_file')
    if not f:
        flash('Keine Datei ausgewählt.', 'error')
        return redirect(url_for('settings'))
    try:
        data = json.loads(f.read().decode('utf-8'))
    except Exception:
        flash('Ungültige JSON-Datei.', 'error')
        return redirect(url_for('settings'))

    imported = 0
    for cat in data.get('categories', []):
        if not Category.query.filter_by(name=cat['name']).first():
            db.session.add(Category(name=cat['name'], color=cat.get('color','#3b82f6'), icon=cat.get('icon','folder')))
            imported += 1
    for lbl in data.get('labels', []):
        if not Label.query.filter_by(name=lbl['name']).first():
            db.session.add(Label(name=lbl['name'], color=lbl.get('color','#3b82f6')))
            imported += 1
    for tmpl in data.get('templates', []):
        if not ContentTemplate.query.filter_by(name=tmpl['name']).first():
            db.session.add(ContentTemplate(name=tmpl['name'], content_type=tmpl.get('content_type','feed'),
                caption_template=tmpl.get('caption_template',''), hashtags=tmpl.get('hashtags',''),
                notes=tmpl.get('notes','')))
            imported += 1
    db.session.commit()
    flash(f'{imported} Einträge importiert.', 'success')
    return redirect(url_for('settings'))


# ─────────────────────── AUTO ARCHIV ───────────────────────

@app.route('/api/content/auto-archive', methods=['POST'])
@login_required
def content_auto_archive():
    days = max(7, int(request.get_json().get('days', 90)))  # Minimum 7 Tage, kein Totalarchiv
    cutoff = datetime.utcnow() - timedelta(days=days)
    items = ContentItem.query.filter(
        ContentItem.created_at < cutoff,
        ContentItem.status.in_(['draft', 'in_progress'])
    ).all()
    for item in items:
        item.status = 'archived'
    db.session.commit()
    return jsonify({'ok': True, 'archived': len(items)})


# ─────────────────────── MEDIA TAGS ───────────────────────

@app.route('/api/media/<int:media_id>/tags', methods=['POST'])
def media_tags_update(media_id):
    item = MediaItem.query.get_or_404(media_id)
    tags = request.get_json().get('tags', [])
    item.tags = json.dumps([t.strip() for t in tags if t.strip()])
    db.session.commit()
    return jsonify({'ok': True, 'tags': item.get_tags()})


# ═══════════════════════════════════════════════════════════════
# ─────────────────── MONATSBERICHT ─────────────────────────────
# ═══════════════════════════════════════════════════════════════

@app.route('/reports/monthly')
@app.route('/reports/monthly/<int:year>/<int:month>')
def monthly_report(year=None, month=None):
    today = datetime.utcnow()
    if not year:  year  = today.year
    if not month: month = today.month

    start = datetime(year, month, 1)
    last_day = cal_mod_global.monthrange(year, month)[1]
    end   = datetime(year, month, last_day, 23, 59, 59)

    # Prev / Next Monat
    if month == 1:  prev_y, prev_m = year-1, 12
    else:           prev_y, prev_m = year, month-1
    if month == 12: next_y, next_m = year+1, 1
    else:           next_y, next_m = year, month+1

    accounts = Account.query.filter_by(status='active').order_by(Account.follower_count.desc()).all()

    report_data = []
    total_start_followers = 0
    total_end_followers   = 0
    total_posts_month     = 0

    # Batch: je 1 Query für Monats-Anfang und -Ende aller Accounts
    acc_ids = [a.id for a in accounts]
    if acc_ids:
        # Früheste Snapshots im Monat pro Account (= Monatsanfang)
        from sqlalchemy import distinct
        _start_snaps = db.session.query(
            AnalyticsSnapshot
        ).filter(
            AnalyticsSnapshot.account_id.in_(acc_ids),
            AnalyticsSnapshot.recorded_at >= start,
            AnalyticsSnapshot.recorded_at <= end,
        ).order_by(AnalyticsSnapshot.account_id, AnalyticsSnapshot.recorded_at.asc()).all()
        _end_snaps = db.session.query(
            AnalyticsSnapshot
        ).filter(
            AnalyticsSnapshot.account_id.in_(acc_ids),
            AnalyticsSnapshot.recorded_at <= end,
        ).order_by(AnalyticsSnapshot.account_id, AnalyticsSnapshot.recorded_at.desc()).all()
        # Jeweils erste Zeile pro Account behalten (ORDER BY garantiert richtige Reihenfolge)
        _snap_start_map, _snap_end_map = {}, {}
        for s in _start_snaps:
            _snap_start_map.setdefault(s.account_id, s)
        for s in _end_snaps:
            _snap_end_map.setdefault(s.account_id, s)
    else:
        _snap_start_map = _snap_end_map = {}

    for acc in accounts:
        snap_start = _snap_start_map.get(acc.id)
        snap_end   = _snap_end_map.get(acc.id)

        followers_start = snap_start.followers if snap_start else acc.follower_count
        followers_end   = snap_end.followers   if snap_end   else acc.follower_count
        growth          = followers_end - followers_start
        growth_pct      = round(growth / followers_start * 100, 2) if followers_start else 0

        # Posts diesen Monat
        posts_month = ScheduledPost.query.filter(
            ScheduledPost.account_id == acc.id,
            ScheduledPost.scheduled_at >= start,
            ScheduledPost.scheduled_at <= end,
            ScheduledPost.status.in_(['published', 'scheduled']),
            ScheduledPost.slot_type != 'disabled',
        ).all()

        # Top-Post nach Likes
        top_post = None
        for p in sorted(posts_month, key=lambda x: x.likes or 0, reverse=True):
            if p.likes:
                top_post = p
                break

        # Ziel-Erreichung
        goal_pct = None
        if acc.growth_goal and acc.growth_goal > 0:
            goal_pct = min(round(followers_end / acc.growth_goal * 100, 1), 100)

        # Posting-Tage (unique Tage mit Post)
        post_days = len(set(p.scheduled_at.date() for p in posts_month))

        total_start_followers += followers_start
        total_end_followers   += followers_end
        total_posts_month     += len(posts_month)

        report_data.append({
            'account':          acc,
            'followers_start':  followers_start,
            'followers_end':    followers_end,
            'growth':           growth,
            'growth_pct':       growth_pct,
            'posts_count':      len(posts_month),
            'post_days':        post_days,
            'top_post':         top_post,
            'goal_pct':         goal_pct,
        })

    # Sortiert nach Wachstum
    report_data.sort(key=lambda x: x['growth'], reverse=True)

    month_name = start.strftime('%B %Y')
    total_growth = total_end_followers - total_start_followers

    return render_template('monthly_report.html',
        report_data=report_data,
        month_name=month_name,
        year=year, month=month,
        prev_y=prev_y, prev_m=prev_m,
        next_y=next_y, next_m=next_m,
        total_start=total_start_followers,
        total_end=total_end_followers,
        total_growth=total_growth,
        total_posts=total_posts_month,
        is_future=(start > today),
        active_page='reports',
    )


# ─────────────────── WACHSTUMSRATE ─────────────────────────────

@app.route('/api/accounts/<int:account_id>/growth-rate')
def account_growth_rate(account_id):
    """Automatische Wachstumsrate aus AnalyticsSnapshots."""
    days = request.args.get('days', 30, type=int)
    since = datetime.utcnow() - timedelta(days=days)

    snaps = AnalyticsSnapshot.query.filter(
        AnalyticsSnapshot.account_id == account_id,
        AnalyticsSnapshot.recorded_at >= since,
    ).order_by(AnalyticsSnapshot.recorded_at.asc()).all()

    if len(snaps) < 2:
        # Fallback: aktueller Follower-Count
        acc = Account.query.get_or_404(account_id)
        return jsonify({'rate_pct': 0, 'growth_abs': 0, 'data_points': 0,
                        'current': acc.follower_count})

    first, last = snaps[0].followers, snaps[-1].followers
    growth_abs = last - first
    rate_pct   = round(growth_abs / first * 100, 2) if first else 0

    # Tägliche Wachstumspunkte
    daily = []
    for i in range(1, len(snaps)):
        diff = snaps[i].followers - snaps[i-1].followers
        daily.append({
            'date': snaps[i].recorded_at.strftime('%Y-%m-%d'),
            'delta': diff,
            'followers': snaps[i].followers,
        })

    return jsonify({
        'rate_pct':   rate_pct,
        'growth_abs': growth_abs,
        'current':    last,
        'from':       first,
        'data_points': len(snaps),
        'daily':      daily,
        'per_day_avg': round(growth_abs / max(days, 1), 1),
    })


# ─────────────────── PRINT / POSTING PLAN ───────────────────────────────

@app.route('/calendar/print')
def calendar_print():
    account_id = request.args.get('account_id', type=int)
    days = request.args.get('days', 14, type=int)
    start = datetime.utcnow().replace(hour=0, minute=0, second=0)
    end = start + timedelta(days=days)
    query = ScheduledPost.query.filter(
        ScheduledPost.scheduled_at >= start,
        ScheduledPost.scheduled_at < end,
        ScheduledPost.status.in_(['scheduled', 'published'])
    )
    if account_id:
        query = query.filter_by(account_id=account_id)
    posts = query.order_by(ScheduledPost.scheduled_at).all()
    accounts = Account.query.filter_by(status='active').order_by(Account.name).all()
    return render_template('print_plan.html', posts=posts, accounts=accounts,
                           account_id=account_id, days=days,
                           start=start, end=end)


# ═══════════════════════════════════════════════════════════════
# ─────────────────── HASHTAG SETS ──────────────────────────────
# ═══════════════════════════════════════════════════════════════

@app.route('/hashtag-sets')
def hashtag_sets():
    sets = HashtagSet.query.order_by(HashtagSet.use_count.desc()).all()
    accounts  = Account.query.filter_by(status='active').order_by(Account.name).all()
    categories = Category.query.order_by(Category.name).all()
    return render_template('hashtag_sets.html', sets=sets,
                           accounts=accounts, categories=categories, active_page='hashtag_sets')

@app.route('/api/hashtag-sets', methods=['GET'])
def api_hashtag_sets_list():
    account_id = request.args.get('account_id', type=int)
    q = HashtagSet.query
    if account_id:
        q = q.filter(db.or_(HashtagSet.account_id == account_id,
                             HashtagSet.account_id.is_(None)))
    sets = q.order_by(HashtagSet.use_count.desc()).all()
    return jsonify([{
        'id': s.id, 'name': s.name, 'hashtags': s.hashtags,
        'account_id': s.account_id, 'use_count': s.use_count,
    } for s in sets])

@app.route('/api/hashtag-sets', methods=['POST'])
def api_hashtag_set_create():
    d = request.get_json()
    s = HashtagSet(
        name=d['name'],
        hashtags=d['hashtags'],
        account_id=d.get('account_id') or None,
        category_id=d.get('category_id') or None,
    )
    db.session.add(s)
    db.session.commit()
    return jsonify({'ok': True, 'id': s.id})

@app.route('/api/hashtag-sets/<int:sid>', methods=['PUT'])
def api_hashtag_set_update(sid):
    s = HashtagSet.query.get_or_404(sid)
    d = request.get_json()
    s.name = d.get('name', s.name)
    s.hashtags = d.get('hashtags', s.hashtags)
    s.account_id = d.get('account_id') or None
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/hashtag-sets/<int:sid>', methods=['DELETE'])
def api_hashtag_set_delete(sid):
    s = HashtagSet.query.get_or_404(sid)
    # FK-Constraint: AccountAutomationProfile.hashtag_set_id → NULL
    AccountAutomationProfile.query.filter_by(hashtag_set_id=sid).update({'hashtag_set_id': None})
    db.session.flush()
    db.session.delete(s)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/hashtag-sets/<int:sid>/use', methods=['POST'])
def api_hashtag_set_use(sid):
    s = HashtagSet.query.get_or_404(sid)
    s.use_count += 1
    db.session.commit()
    return jsonify({'ok': True, 'hashtags': s.hashtags})


# ═══════════════════════════════════════════════════════════════
# ─────────────────── BULK-IMPORT ───────────────────────────────
# ═══════════════════════════════════════════════════════════════

@app.route('/media/bulk-import', methods=['GET'])
def bulk_import_page():
    categories = Category.query.order_by(Category.name).all()
    accounts   = Account.query.filter_by(status='active').order_by(Account.name).all()
    labels     = Label.query.order_by(Label.name).all()
    return render_template('bulk_import.html', categories=categories,
                           accounts=accounts, labels=labels, active_page='bulk_import')

@app.route('/api/media/bulk-import', methods=['POST'])
def api_bulk_import():
    """Mehrere Dateien hochladen → je MediaItem + ContentItem erstellen."""
    files = request.files.getlist('files')
    category_id = request.form.get('category_id', type=int)
    account_ids = request.form.getlist('account_ids', type=int)
    label_ids   = request.form.getlist('label_ids', type=int)
    content_type = request.form.get('content_type', 'feed')
    created = []

    for file in files:
        if not file or not file.filename or not allowed_file(file.filename):
            continue
        original = secure_filename(file.filename)
        ext = original.rsplit('.', 1)[1].lower()
        ftype = get_file_type(original)
        mime = mimetypes.guess_type(original)[0] or 'application/octet-stream'

        file_bytes = file.read()
        cl = _cloudinary_upload(io.BytesIO(file_bytes), original)

        if cl:
            media = MediaItem(
                filename=cl['public_id'],
                original_filename=original,
                file_type=ftype,
                mime_type=mime,
                file_size=cl.get('bytes', len(file_bytes)),
                width=cl.get('width'),
                height=cl.get('height'),
                url=cl['secure_url'],
                storage_source='cloudinary',
                category_id=category_id,
            )
        else:
            unique_name = f"{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_name)
            with open(filepath, 'wb') as f:
                f.write(file_bytes)
            media = MediaItem(
                filename=unique_name,
                original_filename=original,
                file_type=ftype,
                mime_type=mime,
                file_size=os.path.getsize(filepath),
                url=f'/media/file/{unique_name}',
                storage_source='local',
                category_id=category_id,
            )
        db.session.add(media)
        db.session.flush()  # media.id verfügbar

        # Titel = Dateiname ohne Extension, bereinigt
        title = original.rsplit('.', 1)[0].replace('_', ' ').replace('-', ' ').strip()

        ci = ContentItem(
            title=title,
            status='ready',
            content_type=content_type,
            category_id=category_id,
        )
        db.session.add(ci)
        db.session.flush()
        media.content_item_id = ci.id

        # Accounts verknüpfen
        for aid in account_ids:
            acc = Account.query.get(aid)
            if acc and acc not in ci.accounts:
                ci.accounts.append(acc)

        # Labels verknüpfen
        for lid in label_ids:
            lbl = Label.query.get(lid)
            if lbl and lbl not in ci.labels:
                ci.labels.append(lbl)

        created.append({'id': ci.id, 'title': title, 'thumb': media.url})

    db.session.commit()
    log_activity('bulk_import', f'{len(created)} Dateien importiert')
    return jsonify({'ok': True, 'created': created, 'count': len(created)})


# ═══════════════════════════════════════════════════════════════
# ─────────────────── NOTIFICATION SETTINGS & ALERTS ────────────
# ═══════════════════════════════════════════════════════════════

def get_notification_settings():
    ns = NotificationSettings.query.first()
    if not ns:
        ns = NotificationSettings()
        db.session.add(ns)
        db.session.commit()
    return ns

def send_low_stock_email(account_name, stock_days, email):
    """Sendet Low-Stock-Alert per E-Mail (Gmail SMTP oder lokaler Server)."""
    try:
        smtp_host = os.environ.get('SMTP_HOST', 'localhost')
        smtp_port = int(os.environ.get('SMTP_PORT', 25))
        smtp_user = os.environ.get('SMTP_USER', '')
        smtp_pass = os.environ.get('SMTP_PASS', '')
        from_addr = os.environ.get('SMTP_FROM', 'noreply@content-os.de')

        body = f"""Content OS — Low-Stock Alert

Account: {account_name}
Verbleibender Vorrat: {stock_days:.1f} Tage

Bitte plane neue Beiträge für diesen Account ein.

→ https://content-os.de/accounts
"""
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = f'⚠️ Content OS: {account_name} nur noch {stock_days:.0f} Tage Vorrat'
        msg['From'] = from_addr
        msg['To'] = email

        if smtp_user:
            s = smtplib.SMTP_SSL(smtp_host, smtp_port) if smtp_port == 465 else smtplib.SMTP(smtp_host, smtp_port)
            if smtp_port != 25:
                s.starttls()
            s.login(smtp_user, smtp_pass)
        else:
            s = smtplib.SMTP(smtp_host, smtp_port)
        s.sendmail(from_addr, [email], msg.as_string())
        s.quit()
        return True
    except Exception as e:
        app.logger.error(f'Email-Fehler: {e}')
        return False

@app.route('/api/notifications/settings', methods=['GET'])
def api_notif_get():
    ns = get_notification_settings()
    return jsonify({'email': ns.email or '', 'low_stock_days': ns.low_stock_days,
                    'email_enabled': ns.email_enabled})

@app.route('/api/notifications/settings', methods=['POST'])
def api_notif_save():
    d = request.get_json()
    ns = get_notification_settings()
    ns.email = d.get('email', ns.email)
    ns.low_stock_days = int(d.get('low_stock_days', ns.low_stock_days))
    ns.email_enabled = bool(d.get('email_enabled', ns.email_enabled))
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/notifications/test-email', methods=['POST'])
def api_notif_test():
    ns = get_notification_settings()
    if not ns.email:
        return jsonify({'ok': False, 'error': 'Keine E-Mail-Adresse hinterlegt'})
    ok = send_low_stock_email('Test-Account', 2.0, ns.email)
    return jsonify({'ok': ok})


# ═══════════════════════════════════════════════════════════════
# ─────────────────── PERFORMANCE TRACKING ──────────────────────
# ═══════════════════════════════════════════════════════════════

@app.route('/api/posts/<int:post_id>/performance', methods=['POST'])
def post_performance_update(post_id):
    """Likes, Reach, Comments nach dem Posting eintragen."""
    post = ScheduledPost.query.get_or_404(post_id)
    d = request.get_json()
    post.likes    = d.get('likes',    post.likes)
    post.comments = d.get('comments', post.comments)
    post.reach    = d.get('reach',    post.reach)
    if d.get('mark_published'):
        post.status = 'published'
        post.published_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/accounts/<int:account_id>/performance-stats')
def account_performance_stats(account_id):
    """Aggregierte Performance-Daten pro Content-Typ + Timing-Heatmap."""
    posts = ScheduledPost.query.filter(
        ScheduledPost.account_id == account_id,
        ScheduledPost.status == 'published',
        ScheduledPost.likes.isnot(None),
    ).all()

    # Pro Content-Typ
    by_type = {}
    for p in posts:
        t = p.post_type or 'feed'
        if t not in by_type:
            by_type[t] = {'count': 0, 'likes': 0, 'reach': 0, 'comments': 0}
        by_type[t]['count']    += 1
        by_type[t]['likes']    += p.likes or 0
        by_type[t]['reach']    += p.reach or 0
        by_type[t]['comments'] += p.comments or 0
    for t in by_type:
        c = by_type[t]['count']
        by_type[t]['avg_likes']    = round(by_type[t]['likes'] / c, 1)
        by_type[t]['avg_reach']    = round(by_type[t]['reach'] / c, 1)
        by_type[t]['avg_comments'] = round(by_type[t]['comments'] / c, 1)

    # Heatmap: Wochentag (0=Mo) × Stunde → avg Likes
    heatmap = [[0]*24 for _ in range(7)]
    heatmap_count = [[0]*24 for _ in range(7)]
    for p in posts:
        if p.published_at or p.scheduled_at:
            dt = p.published_at or p.scheduled_at
            dow = dt.weekday()  # 0=Mo
            h   = dt.hour
            heatmap[dow][h]       += p.likes or 0
            heatmap_count[dow][h] += 1
    # Durchschnitt
    for dow in range(7):
        for h in range(24):
            cnt = heatmap_count[dow][h]
            heatmap[dow][h] = round(heatmap[dow][h] / cnt, 1) if cnt else 0

    return jsonify({'by_type': by_type, 'heatmap': heatmap,
                    'total_published': len(posts)})


# ═══════════════════════════════════════════════════════════════
# ─────────────────── MULTI-ACCOUNT POST ────────────────────────
# ═══════════════════════════════════════════════════════════════

@app.route('/api/posts/multi-schedule', methods=['POST'])
def multi_schedule():
    """Gleichen Content für mehrere Accounts auf einmal einplanen."""
    d = request.get_json()
    content_item_id = d.get('content_item_id')
    account_ids     = d.get('account_ids', [])
    date_str        = d.get('date')
    time_str        = d.get('time', '18:00')
    captions        = d.get('captions', {})  # {account_id: "caption"}
    post_type       = d.get('post_type', 'feed')

    if not account_ids or not date_str:
        return jsonify({'ok': False, 'error': 'account_ids + date erforderlich'}), 400

    ci = ContentItem.query.get(content_item_id) if content_item_id else None
    scheduled_at = datetime.strptime(f'{date_str} {time_str}', '%Y-%m-%d %H:%M')
    created = []

    for aid in account_ids:
        acc = Account.query.get(aid)
        if not acc:
            continue
        caption = captions.get(str(aid)) or captions.get(aid) or (ci.caption if ci else '') or ''
        post = ScheduledPost(
            account_id=aid,
            content_item_id=content_item_id,
            caption=caption,
            post_type=ci.content_type if ci else post_type,
            slot_type='fixed',
            status='scheduled',
            scheduled_at=scheduled_at,
            media_item_id=ci.media_items[0].id if ci and ci.media_items else None,
            media_ids=json.dumps([m.id for m in ci.media_items]) if ci else '[]',
        )
        db.session.add(post)
        created.append({'account_id': aid, 'account_name': acc.name})

    if ci:
        ci.status = 'scheduled'
    db.session.commit()
    log_activity('multi_scheduled',
        f'Multi-Post: {ci.title if ci else "Post"} → {len(created)} Accounts am {date_str}')
    return jsonify({'ok': True, 'scheduled': created})


# ═══════════════════════════════════════════════════════════════
# ─────────────────── CONTENT RECYCLING ─────────────────────────
# ═══════════════════════════════════════════════════════════════

@app.route('/api/accounts/<int:account_id>/top-performers')
def account_top_performers(account_id):
    """Top-performing published Posts für Recycling."""
    limit = request.args.get('limit', 10, type=int)
    posts = ScheduledPost.query.filter(
        ScheduledPost.account_id == account_id,
        ScheduledPost.status == 'published',
        ScheduledPost.likes.isnot(None),
    ).order_by(ScheduledPost.likes.desc()).limit(limit).all()

    result = []
    for p in posts:
        ci = p.content_item
        result.append({
            'id': p.id,
            'published_at': p.published_at.strftime('%d.%m.%Y') if p.published_at else '',
            'post_type': p.post_type,
            'likes': p.likes or 0,
            'reach': p.reach or 0,
            'comments': p.comments or 0,
            'caption': (p.caption or '')[:100],
            'thumb': (ci.media_items[0].url if ci and ci.media_items else None),
            'content_item_id': p.content_item_id,
        })
    return jsonify(result)

@app.route('/api/posts/<int:post_id>/recycle', methods=['POST'])
def post_recycle(post_id):
    """Einen published Post neu einplanen (Recycling)."""
    original = ScheduledPost.query.get_or_404(post_id)
    d = request.get_json()
    date_str = d.get('date')
    time_str = d.get('time', '18:00')

    if not date_str:
        return jsonify({'ok': False, 'error': 'Datum fehlt'}), 400

    scheduled_at = datetime.strptime(f'{date_str} {time_str}', '%Y-%m-%d %H:%M')
    new_post = ScheduledPost(
        account_id=original.account_id,
        content_item_id=original.content_item_id,
        caption=original.caption,
        hashtags=original.hashtags,
        post_type=original.post_type,
        slot_type='fixed',
        status='scheduled',
        scheduled_at=scheduled_at,
        media_item_id=original.media_item_id,
        media_ids=original.media_ids,
    )
    db.session.add(new_post)
    db.session.commit()
    log_activity('post_recycled', f'Post {post_id} recycelt auf {date_str}')
    return jsonify({'ok': True, 'new_post_id': new_post.id})


# ─────────────────── ACCOUNT-GRUPPEN-PLANER ────────────────────

@app.route('/groups/<int:group_id>/planer')
def group_planer(group_id):
    """Gruppen-Kalender: alle Accounts der Gruppe auf einem Blick."""
    group = AccountGroup.query.get_or_404(group_id)
    all_groups = AccountGroup.query.order_by(AccountGroup.name).all()
    return render_template('gruppen_planer.html',
        group=group, all_groups=all_groups, active_page='accounts')


@app.route('/api/groups/<int:group_id>/planer/events')
def group_planer_events(group_id):
    """Events aller Accounts in der Gruppe für einen Monat."""
    group = AccountGroup.query.get_or_404(group_id)
    month = request.args.get('month', '')
    try:
        y, m = int(month[:4]), int(month[5:7])
    except Exception:
        from datetime import date
        today = date.today()
        y, m = today.year, today.month

    start = datetime(y, m, 1)
    import calendar as cal_mod
    last_day = cal_mod.monthrange(y, m)[1]
    end = datetime(y, m, last_day, 23, 59, 59)

    account_ids = [a.id for a in group.accounts]
    posts = ScheduledPost.query.filter(
        ScheduledPost.account_id.in_(account_ids),
        ScheduledPost.scheduled_at >= start,
        ScheduledPost.scheduled_at <= end,
    ).order_by(ScheduledPost.scheduled_at).all()

    result = []
    for p in posts:
        ci = p.content_item
        result.append({
            'id': p.id,
            'account_id': p.account_id,
            'account_name': p.account.name,
            'date': p.scheduled_at.strftime('%Y-%m-%d'),
            'time': p.scheduled_at.strftime('%H:%M'),
            'slot_type': p.slot_type,
            'status': p.status,
            'post_type': p.post_type,
            'caption': (p.caption or (ci.title if ci else '') or '')[:80],
            'thumb': (ci.media_items[0].url if ci and ci.media_items else None),
        })
    return jsonify({
        'accounts': [{'id': a.id, 'name': a.name, 'handle': a.handle or ''} for a in group.accounts],
        'events': result,
    })


# ─────────────────── FREIGABE-WORKFLOW ──────────────────────────

@app.route('/content/review')
def content_review():
    """Review-Queue: alle Items die auf Freigabe warten."""
    pending = ContentItem.query.filter_by(approval_status='pending_review')\
                               .order_by(ContentItem.created_at.desc()).all()
    approved = ContentItem.query.filter_by(approval_status='approved')\
                                .order_by(ContentItem.reviewed_at.desc()).limit(20).all()
    rejected = ContentItem.query.filter_by(approval_status='rejected')\
                                .order_by(ContentItem.reviewed_at.desc()).limit(20).all()
    team = TeamMember.query.filter_by(active=True).all()
    return render_template('content_review.html',
        pending=pending, approved=approved, rejected=rejected,
        team=team, active_page='content')

@app.route('/api/content/<int:item_id>/submit-review', methods=['POST'])
def submit_review(item_id):
    """Item zur Review einreichen."""
    ci = ContentItem.query.get_or_404(item_id)
    ci.approval_status = 'pending_review'
    # In-App Notification für alle Team-Leads
    _push_notification('review_request',
        f'Review angefragt: {ci.title[:50]}',
        f'"{ci.title}" wurde zur Freigabe eingereicht.',
        link=f'/content/review')
    db.session.commit()
    log_activity('review_submitted', f'{ci.title} zur Review eingereicht')
    return jsonify({'ok': True})

@app.route('/api/content/<int:item_id>/approve', methods=['POST'])
def approve_content(item_id):
    ci = ContentItem.query.get_or_404(item_id)
    d  = request.get_json() or {}
    ci.approval_status = 'approved'
    ci.reviewed_at     = datetime.utcnow()
    ci.review_note     = d.get('note', '')
    if ci.status == 'draft':
        ci.status = 'ready'
    _push_notification('approved',
        f'Freigegeben: {ci.title[:50]}',
        f'Dein Content wurde freigegeben.',
        link=f'/content')
    db.session.commit()
    log_activity('content_approved', f'{ci.title} freigegeben')
    return jsonify({'ok': True})

@app.route('/api/content/<int:item_id>/reject', methods=['POST'])
def reject_content(item_id):
    ci = ContentItem.query.get_or_404(item_id)
    d  = request.get_json() or {}
    ci.approval_status = 'rejected'
    ci.reviewed_at     = datetime.utcnow()
    ci.review_note     = d.get('note', '')
    _push_notification('rejected',
        f'Abgelehnt: {ci.title[:50]}',
        d.get('note', 'Kein Kommentar') or 'Abgelehnt',
        link=f'/content')
    db.session.commit()
    log_activity('content_rejected', f'{ci.title} abgelehnt')
    return jsonify({'ok': True})


# ─────────────────── IN-APP NOTIFICATIONS ───────────────────────

def _push_notification(ntype, title, message, link='', account_id=None):
    """Interne Hilfsfunktion: Notification in DB speichern."""
    try:
        n = AppNotification(type=ntype, title=title, message=message,
                            link=link, account_id=account_id)
        db.session.add(n)
        # Maximal 200 behalten
        old = AppNotification.query.order_by(AppNotification.created_at.asc())\
                                   .offset(200).all()
        for o in old:
            db.session.delete(o)
    except Exception:
        pass

@app.route('/api/notifications/inbox')
def notifications_inbox():
    """Aktuelle In-App Notifications."""
    limit = request.args.get('limit', 20, type=int)
    notifs = AppNotification.query.order_by(AppNotification.created_at.desc()).limit(limit).all()
    unread = AppNotification.query.filter_by(is_read=False).count()
    return jsonify({
        'unread': unread,
        'notifications': [{
            'id':         n.id,
            'type':       n.type,
            'title':      n.title,
            'message':    n.message,
            'link':       n.link,
            'is_read':    n.is_read,
            'created_at': n.created_at.strftime('%d.%m. %H:%M'),
        } for n in notifs]
    })

@app.route('/api/notifications/mark-read', methods=['POST'])
def notifications_mark_read():
    ids = (request.get_json() or {}).get('ids', [])
    if ids:
        AppNotification.query.filter(AppNotification.id.in_(ids))\
                             .update({'is_read': True}, synchronize_session=False)
    else:
        AppNotification.query.update({'is_read': True}, synchronize_session=False)
    db.session.commit()
    return jsonify({'ok': True})


# ─────────────────── DOPPELGÄNGER-ERKENNUNG ─────────────────────

@app.route('/api/content/check-duplicate', methods=['POST'])
def check_duplicate():
    """Prüft ob Content-Item bereits für Account geplant ist."""
    d = request.get_json()
    content_item_id = d.get('content_item_id')
    account_id      = d.get('account_id')
    if not content_item_id or not account_id:
        return jsonify({'duplicate': False})

    existing = ScheduledPost.query.filter(
        ScheduledPost.content_item_id == content_item_id,
        ScheduledPost.account_id      == account_id,
        ScheduledPost.status.in_(['scheduled', 'draft']),
    ).first()

    if existing:
        return jsonify({
            'duplicate': True,
            'date': existing.scheduled_at.strftime('%d.%m.%Y'),
            'post_id': existing.id,
        })
    return jsonify({'duplicate': False})


# ─────────────────── PWA MANIFEST ───────────────────────────────

@app.route('/manifest.json')
def pwa_manifest():
    manifest = {
        "name": "Content OS",
        "short_name": "Content OS",
        "description": "Social Media Management für Stadtseiten",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f172a",
        "theme_color": "#6366f1",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ]
    }
    from flask import Response
    return Response(json.dumps(manifest), mimetype='application/json')

@app.route('/sw.js')
def service_worker():
    sw = """
const CACHE = 'content-os-v1';
const OFFLINE = ['/'];
self.addEventListener('install', e => e.waitUntil(
  caches.open(CACHE).then(c => c.addAll(OFFLINE))
));
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
"""
    from flask import Response
    return Response(sw, mimetype='application/javascript')


# ─────────────────── BULK-FOLLOWER-CSV ──────────────────────────

@app.route('/accounts/bulk-follower-update', methods=['GET'])
def bulk_follower_update_page():
    accounts = Account.query.filter_by(status='active').order_by(Account.name).all()
    instagram_accounts = [a for a in accounts if a.handle]
    return render_template('bulk_follower_update.html',
        accounts=accounts,
        instagram_accounts=instagram_accounts,
        active_page='accounts')

@app.route('/api/accounts/bulk-follower-update', methods=['POST'])
def api_bulk_follower_update():
    """CSV-Upload: account_name_or_id,followers[,date]"""
    import csv, io
    file = request.files.get('csv_file')
    rows_data = request.form.get('rows_json')

    updated = []
    errors  = []

    if rows_data:
        rows = json.loads(rows_data)
    elif file:
        content = file.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
    else:
        return jsonify({'ok': False, 'error': 'Keine Daten'}), 400

    for row in rows:
        name_or_id = str(row.get('account', row.get('name', row.get('id', '')))).strip()
        followers  = row.get('followers', row.get('follower', ''))
        try:
            followers = int(str(followers).replace('.','').replace(',','').strip())
        except Exception:
            errors.append(f'Ungültige Follower-Zahl für "{name_or_id}"')
            continue

        acc = None
        if name_or_id.isdigit():
            acc = Account.query.get(int(name_or_id))
        if not acc:
            acc = Account.query.filter(Account.name.ilike(f'%{name_or_id}%')).first()
        if not acc:
            errors.append(f'Account nicht gefunden: "{name_or_id}"')
            continue

        old, delta = _set_follower_count(acc, followers)
        updated.append({'name': acc.name, 'old': old, 'new': followers, 'delta': delta})

    db.session.commit()
    return jsonify({'ok': True, 'updated': updated, 'errors': errors})


# ─────────────────── INSTAGRAM FOLLOWER-SYNC ────────────────────
# Unterstützt zwei Modi:
#   1. "direct"  — Instagrams interne Web-API (kostenlos, bis ~50 Accounts)
#   2. "apify"   — Apify Instagram Scraper (offiziell, für 100+ Accounts)

import urllib.request as _urllib_request
import time as _time

_ig_sync_status = {
    'running': False,
    'progress': 0,
    'current': '',
    'method': '',
    'last_run': None,
    'result': None,
    'error': None,
}

# ── Direkte Methode (kein API-Key) ────────────────────────────

_IG_DIRECT_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
        'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 '
        'Mobile/15E148 Safari/604.1'
    ),
    'Accept': '*/*',
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.8',
    'X-IG-App-ID': '936619743392459',
    'X-Requested-With': 'XMLHttpRequest',
}


def _fetch_ig_followers_direct(username):
    """Ruft Follower-Zahl direkt von Instagrams Web-API ab (kein Key nötig)."""
    import json as _json
    url = f'https://www.instagram.com/api/v1/users/web_profile_info/?username={username}'
    req = _urllib_request.Request(url, headers=_IG_DIRECT_HEADERS)
    try:
        with _urllib_request.urlopen(req, timeout=15) as r:
            data = _json.loads(r.read())
            return data['data']['user']['edge_followed_by']['count'], None
    except _urllib_request.HTTPError as e:
        if e.code == 404:
            return None, f'@{username}: Account nicht gefunden'
        if e.code == 429:
            return None, f'@{username}: Rate-Limit erreicht — bitte später nochmal'
        return None, f'@{username}: HTTP {e.code}'
    except Exception as e:
        return None, f'@{username}: {str(e)}'


# ── Apify-Methode (offiziell, für 100+ Accounts) ─────────────

def _fetch_ig_followers_apify_batch(usernames, api_token):
    """
    Ruft Follower-Zahlen für eine Liste von Usernames via Apify ab.
    Nutzt den offiziellen Apify Instagram Scraper (apify~instagram-scraper).
    Gibt ein Dict {username: followers} zurück, plus eine Fehlerliste.
    """
    import json as _json, urllib.parse as _parse

    # Alle URLs auf einmal in einem einzigen API-Aufruf
    direct_urls = [f'https://www.instagram.com/{u}/' for u in usernames]
    payload = _json.dumps({
        'directUrls': direct_urls,
        'resultsType': 'details',
        'resultsLimit': 1,
    }).encode()

    url = (
        'https://api.apify.com/v2/acts/apify~instagram-scraper'
        f'/run-sync-get-dataset-items?token={api_token}&timeout=300'
    )
    req = _urllib_request.Request(
        url, data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with _urllib_request.urlopen(req, timeout=360) as r:
            items = _json.loads(r.read())
    except _urllib_request.HTTPError as e:
        body = e.read().decode(errors='replace')[:300]
        return {}, [f'Apify API Fehler HTTP {e.code}: {body}']
    except Exception as e:
        return {}, [f'Apify Verbindungsfehler: {str(e)}']

    result = {}
    errors = []
    for item in (items if isinstance(items, list) else []):
        uname = (item.get('username') or '').lower()
        count = item.get('followersCount') or item.get('followers') or 0
        if uname and isinstance(count, int) and count > 0:
            result[uname] = count
        elif uname:
            errors.append(f'@{uname}: keine Follower-Zahl in Apify-Antwort')

    return result, errors


# ── Haupt-Sync-Worker ─────────────────────────────────────────

def _run_ig_follower_sync():
    """Holt Follower-Zahlen für alle Accounts. Nutzt Apify wenn konfiguriert."""
    global _ig_sync_status
    try:
        with app.app_context():
            # Methode ermitteln (explizit gesetzt oder Fallback auf Token-Vorhanden)
            method_row  = AppSettings.query.filter_by(key='ig_sync_method').first()
            token_row   = AppSettings.query.filter_by(key='apify_token').first()
            apify_token = token_row.value if token_row and token_row.value else None
            method = (method_row.value if method_row and method_row.value else
                      ('apify' if apify_token else 'direct'))
            # Apify gewählt aber kein Token → Fallback auf direkt
            if method == 'apify' and not apify_token:
                method = 'direct'
            _ig_sync_status['method'] = method

            accounts = Account.query.filter(
                Account.handle != None,
                Account.handle != '',
                Account.status == 'active'
            ).order_by(Account.name).all()

            if not accounts:
                _ig_sync_status.update({
                    'running': False,
                    'result': {'updated': 0, 'details': [], 'errors': [], 'total_queried': 0},
                    'last_run': datetime.utcnow().isoformat(),
                    'error': None,
                })
                return

            total = len(accounts)
            updated_list, errors = [], []
            app.logger.info(f'[IG Sync] Methode={method}, {total} Accounts')

            if method == 'apify':
                # ── Apify: alle auf einmal ──
                _ig_sync_status.update({'current': 'Apify-Scraper läuft…', 'progress': 10})
                usernames = [a.handle.lstrip('@') for a in accounts]
                followers_map, apify_errors = _fetch_ig_followers_apify_batch(usernames, apify_token)
                errors.extend(apify_errors)

                acc_map = {a.handle.lstrip('@').lower(): a for a in accounts}
                for uname, followers in followers_map.items():
                    acc = acc_map.get(uname)
                    if acc:
                        old, delta = _set_follower_count(acc, followers)
                        updated_list.append({'name': acc.name, 'handle': uname,
                                             'old': old, 'new': followers, 'delta': delta})
                        app.logger.info(f'[IG Sync/Apify] @{uname}: {old}→{followers}')

                _ig_sync_status['progress'] = 95

            else:
                # ── Direkt: Account für Account mit Pause ──
                for i, acc in enumerate(accounts):
                    username = acc.handle.lstrip('@')
                    _ig_sync_status.update({
                        'current': acc.name,
                        'progress': int((i / total) * 100),
                    })
                    followers, err = _fetch_ig_followers_direct(username)
                    if err:
                        errors.append(err)
                        app.logger.warning(f'[IG Sync/Direct] {err}')
                    elif followers:
                        old, delta = _set_follower_count(acc, followers)
                        updated_list.append({'name': acc.name, 'handle': username,
                                             'old': old, 'new': followers, 'delta': delta})
                        app.logger.info(f'[IG Sync/Direct] @{username}: {old}→{followers}')
                    if i < total - 1:
                        _time.sleep(1.5)

            db.session.commit()

            result = {
                'updated': len(updated_list),
                'details': updated_list,
                'errors': errors,
                'total_queried': total,
                'method': method,
            }
            _ig_sync_status.update({
                'running': False, 'progress': 100, 'current': '',
                'last_run': datetime.utcnow().isoformat(),
                'result': result, 'error': None,
            })
            app.logger.info(
                f'[IG Sync] Fertig: {len(updated_list)}/{total}, {len(errors)} Fehler'
            )

    except Exception as e:
        app.logger.error(f'[IG Sync] Exception: {e}')
        _ig_sync_status.update({'running': False, 'error': str(e)})


# ── Sync-API-Endpunkte ────────────────────────────────────────

@app.route('/api/analytics/sync-followers-apify', methods=['POST'])
@login_required
def sync_followers_apify():
    if _ig_sync_status['running']:
        return jsonify({'ok': False, 'error': 'Sync läuft bereits'}), 409
    count = Account.query.filter(
        Account.handle != None, Account.handle != '', Account.status == 'active'
    ).count()
    if count == 0:
        return jsonify({'ok': False, 'error': 'Keine Accounts mit Instagram-Handle'}), 400
    _ig_sync_status.update({'running': True, 'error': None, 'result': None,
                             'progress': 0, 'current': ''})
    threading.Thread(target=_run_ig_follower_sync, daemon=True).start()
    # Aktive Methode ermitteln für den Client
    has_token = bool(AppSettings.query.filter_by(key='apify_token').first() and
                     AppSettings.query.filter_by(key='apify_token').first().value)
    return jsonify({'ok': True, 'total': count, 'method': 'apify' if has_token else 'direct'})


@app.route('/api/analytics/sync-followers-apify/status')
@login_required
def sync_followers_apify_status():
    return jsonify(_ig_sync_status)


# ── Integrationen-Seite ───────────────────────────────────────

@app.route('/settings/integrations', methods=['GET'])
@login_required
def integrations():
    def gs(key, default=''):
        r = AppSettings.query.filter_by(key=key).first()
        return r.value if r and r.value is not None else default

    apify_token     = gs('apify_token')
    ig_sync_method  = gs('ig_sync_method', 'apify' if gs('apify_token') else 'direct')
    auto_sync       = gs('ig_auto_sync', '1') != '0'
    ig_accounts_count = Account.query.filter(
        Account.handle != None, Account.handle != '', Account.status == 'active'
    ).count()
    return render_template('integrations.html',
        apify_token=apify_token,
        ig_sync_method=ig_sync_method,
        auto_sync=auto_sync,
        ig_accounts_count=ig_accounts_count,
        active_page='integrations')


@app.route('/settings/integrations', methods=['POST'])
@login_required
def integrations_save():
    def upsert(key, val):
        s = AppSettings.query.filter_by(key=key).first()
        if not s:
            s = AppSettings(key=key)
            db.session.add(s)
        s.value = val

    upsert('apify_token',    request.form.get('apify_token', '').strip())
    upsert('ig_sync_method', request.form.get('ig_sync_method', 'direct'))
    upsert('ig_auto_sync',   '1' if request.form.get('ig_auto_sync') else '0')
    db.session.commit()
    flash('Einstellungen gespeichert.', 'success')
    return redirect(url_for('integrations'))


@app.route('/api/integrations/test-apify', methods=['POST'])
@login_required
def test_apify_connection():
    """Testet den Apify-Token mit einem echten Testaufruf (@instagram)."""
    token = request.get_json().get('token', '').strip()
    if not token:
        return jsonify({'ok': False, 'error': 'Kein Token angegeben'})
    followers, errors = _fetch_ig_followers_apify_batch(['instagram'], token)
    if errors:
        return jsonify({'ok': False, 'error': errors[0]})
    count = followers.get('instagram', 0)
    if count > 0:
        return jsonify({'ok': True, 'message': f'Verbindung erfolgreich! (@instagram hat {count:,} Follower)'})
    return jsonify({'ok': False, 'error': 'Apify hat keine Daten zurückgegeben — Token prüfen'})


# ─────────────────── ACCOUNT-KLONEN ─────────────────────────────

@app.route('/api/accounts/<int:account_id>/clone', methods=['POST'])
def account_clone(account_id):
    src  = Account.query.get_or_404(account_id)
    d    = request.get_json() or {}
    name = d.get('name', f'{src.name} (Kopie)').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'Name fehlt'}), 400

    new_acc = Account(
        name=name,
        platform_id=src.platform_id,
        category_id=src.category_id if d.get('copy_category', True) else None,
        status='active',
        posting_interval_days=src.posting_interval_days,
        target_feed_per_day=src.target_feed_per_day,
        target_story_per_day=src.target_story_per_day,
        target_reel_per_week=src.target_reel_per_week,
        min_stock_days=src.min_stock_days,
        optimal_stock_days=src.optimal_stock_days,
        max_stock_days=src.max_stock_days,
        automation_level=src.automation_level,
        priority=src.priority,
    )
    if d.get('copy_labels', True):
        new_acc.labels = list(src.labels)
    db.session.add(new_acc)
    db.session.flush()  # get new_acc.id

    # Hashtag-Sets klonen
    if d.get('copy_hashtags', True):
        for hs in src.hashtag_sets:
            clone_hs = HashtagSet(
                name=hs.name,
                hashtags=hs.hashtags,
                account_id=new_acc.id,
                category_id=hs.category_id,
            )
            db.session.add(clone_hs)

    db.session.commit()
    log_activity('account_cloned', f'{src.name} → {new_acc.name}')
    return jsonify({'ok': True, 'new_id': new_acc.id, 'redirect': f'/accounts/{new_acc.id}'})


# ─────────────────── WIEDERKEHRENDE POSTS ───────────────────────

@app.route('/api/recurring-posts', methods=['POST'])
def recurring_post_create():
    """Erstellt eine Wiederholungsreihe + sofort alle ScheduledPosts."""
    d               = request.get_json()
    content_item_id = d.get('content_item_id')
    account_id      = d.get('account_id')
    dates           = d.get('dates', [])   # Liste von 'YYYY-MM-DD HH:MM'
    note            = d.get('note', '')
    time_str        = d.get('time', '18:00')

    if not account_id or not dates:
        return jsonify({'ok': False, 'error': 'account_id und dates erforderlich'}), 400

    ci  = ContentItem.query.get(content_item_id) if content_item_id else None
    rec = RecurringPost(
        content_item_id=content_item_id,
        account_id=account_id,
        scheduled_dates=json.dumps(dates),
        note=note,
    )
    db.session.add(rec)

    created = []
    for dt_str in dates:
        try:
            if 'T' in dt_str or ' ' in dt_str:
                dt = datetime.strptime(dt_str.replace('T', ' '), '%Y-%m-%d %H:%M')
            else:
                dt = datetime.strptime(f'{dt_str} {time_str}', '%Y-%m-%d %H:%M')
        except Exception:
            continue
        post = ScheduledPost(
            account_id=account_id,
            content_item_id=content_item_id,
            caption=ci.caption or ci.title if ci else '',
            post_type=ci.content_type if ci else 'feed',
            slot_type='fixed',
            status='scheduled',
            scheduled_at=dt,
            media_item_id=ci.media_items[0].id if ci and ci.media_items else None,
            media_ids=json.dumps([m.id for m in ci.media_items]) if ci else '[]',
        )
        db.session.add(post)
        created.append(dt.strftime('%d.%m.%Y %H:%M'))

    db.session.commit()
    log_activity('recurring_created', f'{len(created)} Wiederholungen für Account {account_id}')
    return jsonify({'ok': True, 'created': created, 'recurring_id': rec.id})

@app.route('/api/recurring-posts/<int:rec_id>', methods=['DELETE'])
def recurring_post_delete(rec_id):
    rec = RecurringPost.query.get_or_404(rec_id)
    db.session.delete(rec)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/accounts/<int:account_id>/recurring-posts')
def account_recurring_posts(account_id):
    recs = RecurringPost.query.filter_by(account_id=account_id).order_by(RecurringPost.created_at.desc()).all()
    result = []
    for r in recs:
        ci = r.content_item
        result.append({
            'id': r.id,
            'note': r.note or '',
            'dates': json.loads(r.scheduled_dates or '[]'),
            'content_item': {
                'id': ci.id, 'title': ci.title,
                'thumb': ci.media_items[0].url if ci and ci.media_items else None,
            } if ci else None,
            'created_at': r.created_at.strftime('%d.%m.%Y'),
        })
    return jsonify(result)


# ─────────────────── MANUELLER SNAPSHOT-TRIGGER ────────────────
@app.route('/api/analytics/snapshot-now', methods=['POST'])
@login_required
def snapshot_now():
    """Erstellt sofort einen AnalyticsSnapshot für alle Accounts (ohne auf Mitternacht zu warten)."""
    global _last_daily_snap_date
    _last_daily_snap_date = None   # Reset → _daily_follower_snapshot läuft erneut
    _daily_follower_snapshot()
    count = Account.query.filter_by(status='active').count()
    return jsonify({'ok': True, 'message': f'Snapshots für {count} Accounts angelegt'})


# ─────────────────── AUTOMATION PROFILE ──────────────────────

@app.route('/api/accounts/<int:account_id>/auto-profile', methods=['GET'])
@login_required
def get_auto_profile(account_id):
    acc = Account.query.get_or_404(account_id)
    p = acc.auto_profile
    hashtag_sets = HashtagSet.query.order_by(HashtagSet.name).all()
    if not p:
        return jsonify({
            'mode': 'manual', 'source_type': '', 'rss_url': '', 'ai_prompt': '',
            'ai_style': 'neutral', 'citybot_key': '',
            'posts_per_day': 1.0, 'preferred_times': ['09:00'],
            'default_post_type': 'feed', 'caption_template': '',
            'hashtag_set_id': None, 'auto_approve': False,
            'disable_stock_amp': False, 'notes': '',
            'hashtag_sets': [{'id': h.id, 'name': h.name} for h in hashtag_sets],
        })
    return jsonify({
        'mode': p.mode,
        'source_type': p.source_type or '',
        'rss_url': p.rss_url or '',
        'ai_prompt': p.ai_prompt or '',
        'ai_style': p.ai_style or 'neutral',
        'citybot_key': p.citybot_key or '',
        'posts_per_day': p.posts_per_day or 1.0,
        'preferred_times': p.get_times(),
        'default_post_type': p.default_post_type or 'feed',
        'caption_template': p.caption_template or '',
        'hashtag_set_id': p.hashtag_set_id,
        'auto_approve': p.auto_approve,
        'disable_stock_amp': p.disable_stock_amp,
        'notes': p.notes or '',
        'hashtag_sets': [{'id': h.id, 'name': h.name} for h in hashtag_sets],
    })


@app.route('/api/accounts/<int:account_id>/auto-profile', methods=['POST'])
@login_required
def save_auto_profile(account_id):
    acc = Account.query.get_or_404(account_id)
    d = request.get_json() or {}
    p = acc.auto_profile
    if not p:
        p = AccountAutomationProfile(account_id=account_id)
        db.session.add(p)

    p.mode             = d.get('mode', 'manual')
    p.source_type      = d.get('source_type', '')
    p.rss_url          = d.get('rss_url', '')
    p.ai_prompt        = d.get('ai_prompt', '')
    p.ai_style         = d.get('ai_style', 'neutral')
    p.citybot_key      = d.get('citybot_key', '')
    p.posts_per_day    = float(d.get('posts_per_day', 1.0))
    p.preferred_times  = json.dumps(d.get('preferred_times', ['09:00']))
    p.default_post_type = d.get('default_post_type', 'feed')
    p.caption_template = d.get('caption_template', '')
    p.hashtag_set_id   = d.get('hashtag_set_id') or None
    p.auto_approve     = bool(d.get('auto_approve', False))
    p.disable_stock_amp = bool(d.get('disable_stock_amp', False))
    p.notes            = d.get('notes', '')
    p.updated_at       = datetime.utcnow()

    # Sync automation_level auf Account
    acc.automation_level = 2 if p.mode == 'auto' else 0
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/accounts/<int:account_id>/toggle-auto', methods=['POST'])
@login_required
def toggle_auto(account_id):
    acc = Account.query.get_or_404(account_id)
    p = acc.auto_profile
    if not p:
        p = AccountAutomationProfile(account_id=account_id, mode='auto')
        db.session.add(p)
    else:
        p.mode = 'manual' if p.mode == 'auto' else 'auto'
    p.updated_at = datetime.utcnow()
    acc.automation_level = 2 if p.mode == 'auto' else 0
    db.session.commit()
    return jsonify({'ok': True, 'mode': p.mode})


# ─────────────────────────────────────────────────────────────
# Automation-Übersicht: alle Accounts mit Profil-Daten
@app.route('/content/automation')
@login_required
def content_automation():
    accounts = Account.query.filter_by(status='active').order_by(Account.name).all()
    hashtag_sets = HashtagSet.query.order_by(HashtagSet.name).all()
    return render_template('content_automation.html',
        accounts=accounts, hashtag_sets=hashtag_sets, active_page='content')


# ─────────────────────── ERROR HANDLERS ───────────────────────

@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', code=404,
        title='Seite nicht gefunden',
        message='Die gesuchte Seite existiert nicht.'), 404


@app.errorhandler(500)
def server_error(e):
    return render_template('error.html', code=500,
        title='Serverfehler',
        message='Ein interner Fehler ist aufgetreten.'), 500


if __name__ == '__main__':
    app.run(debug=True, port=5100)
