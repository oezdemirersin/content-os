import os
import json
import csv
import io
import secrets
import threading
import difflib
import mimetypes
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from flask import (Flask, render_template, request, redirect, url_for,
                   jsonify, flash, send_from_directory, make_response, abort)
from werkzeug.utils import secure_filename
from functools import wraps
from flask import session
from models import (db, Platform, Category, Label, TeamMember, Account, AIConfig,
                    ContentItem, ContentFolder, MediaItem, ScheduledPost, AnalyticsSnapshot,
                    AutomationRule, AutomationRunLog, SystemAlert, User, ActivityLog,
                    AccountGroup, ContentTemplate, ContentComment,
                    HashtagSet, NotificationSettings, AppNotification, RecurringPost,
                    AccountAutomationProfile, AppSettings,
                    MemeTemplate, MemeVariant,
                    InspirationSource, InspirationPost,
                    WeatherCache, WeatherTriggerLog,
                    ContentSeries, Kooperation, AccountIdeenContext,
                    Partner, AiUsageLog, AppTodo, Ausgabe, AboKosten, GeplantAusgabe, LocalEvent, SeitenKauf,
                    WatchlistSeite, WatchlistFollowerSnapshot, WatchlistCityMeta,
                    GrowthExperiment, GrowthVariant, GrowthParticipant, GrowthDataPoint, GrowthKnowledge,
                    KnowledgeEntry, MissingChildCase, EmergencyNumber,
                    TrendTopic, TrendSignal, TrendSource, TrendScoreSnapshot,
                    ProductAlert, ProductAlertSource)
import calendar as cal_mod_global
from sqlalchemy import func
from sqlalchemy.orm import joinedload, selectinload

app = Flask(__name__, template_folder='templates/cms')

# ══════════════════════════════════════════════════════════════════════════════
# DB-BACKUP-SYSTEM — 5 Schutzschichten
# ══════════════════════════════════════════════════════════════════════════════
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DB_PATH  = os.path.join(_BASE_DIR, 'instance', 'content_os.db')
_LOCAL_BACKUP_DIR = os.path.join(_BASE_DIR, 'db_backups')


def _do_backup(label='auto'):
    """Lokales DB-Backup in db_backups/. Niemals bei Postgres."""
    import shutil, glob
    if 'postgresql' in os.environ.get('DATABASE_URL', ''):
        return None
    if not os.path.exists(_DB_PATH) or os.path.getsize(_DB_PATH) < 4096:
        return None
    os.makedirs(_LOCAL_BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    dst = os.path.join(_LOCAL_BACKUP_DIR, f'content_os_{label}_{stamp}.db')
    shutil.copy2(_DB_PATH, dst)
    # Max 30 Backups behalten
    old = sorted(glob.glob(os.path.join(_LOCAL_BACKUP_DIR, '*.db')))[:-30]
    for f in old:
        try: os.remove(f)
        except: pass
    return dst


# Backup beim App-Start
_do_backup('startup')

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
    try:
        vorrat_total = db.session.query(func.count(ContentItem.id))\
            .filter(ContentItem.status.in_(['draft', 'ready', 'in_progress', 'scheduled']))\
            .scalar() or 0
    except Exception:
        vorrat_total = 0
    try:
        studio_accounts = db.session.query(Account).join(
            AccountIdeenContext, AccountIdeenContext.account_id == Account.id
        ).filter(AccountIdeenContext.studio_active == True).order_by(Account.name).all()
    except Exception:
        studio_accounts = []
    try:
        changelog_unread = get_changelog_unread_count()
    except Exception:
        changelog_unread = 0
    return {
        'now': datetime.utcnow,
        'emergency_pause_active': _is_emergency_paused(),
        'vorrat_total': vorrat_total,
        'studio_accounts': studio_accounts,
        'changelog_unread': changelog_unread,
    }

@app.template_filter('from_json')
def from_json_filter(s):
    """Jinja2-Filter: JSON-String → Python-Objekt."""
    try:
        return json.loads(s) if s else []
    except Exception:
        return []


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
_secret = os.environ.get('SECRET_KEY')
if not _secret:
    _is_prod = 'postgresql' in os.environ.get('DATABASE_URL', '')
    if _is_prod:
        raise RuntimeError('SECRET_KEY env var must be set in production')
    _secret = 'content-os-dev-only-secret'
app.config['SECRET_KEY'] = _secret
app.secret_key = _secret
# Render gibt postgres:// zurück, SQLAlchemy braucht postgresql://
_db_url = os.environ.get('DATABASE_URL', 'sqlite:///content_os.db')
if _db_url.startswith('postgres://'):
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    # pool_pre_ping wieder aktiv: Render-Postgres läuft im selben Netz (interne
    # Connection-URL), der SELECT-1-Ping kostet nur ~1-3ms — nicht die 50-100ms
    # vom alten Supabase-Setup. Verhindert psycopg2.OperationalError im
    # Background-Thread (z.B. _send_due_telegram_posts), wenn der Server eine
    # idle Connection stale schließt — das fängt pool_recycle=300 allein NICHT ab.
    'pool_pre_ping': True,
    'pool_recycle': 300,
    # pool_size/max_overflow nur für Postgres — SQLite (StaticPool, z.B. in-memory
    # bei Tests) akzeptiert diese Argumente nicht
    **({'pool_size': 5, 'max_overflow': 10,
        'connect_args': {'sslmode': 'require'}}
       if _db_url.startswith('postgresql://') else {}),
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

def _local_upload(file_obj, original_filename):
    """Fallback: save file locally when Cloudinary is not configured."""
    import uuid, pathlib
    upload_dir = pathlib.Path(app.root_path) / 'static' / 'uploads' / 'meme_templates'
    upload_dir.mkdir(parents=True, exist_ok=True)
    ext = original_filename.rsplit('.', 1)[-1].lower() if '.' in original_filename else 'bin'
    fname = f"{uuid.uuid4().hex}.{ext}"
    dest = upload_dir / fname
    if hasattr(file_obj, 'read'):
        dest.write_bytes(file_obj.read())
    else:
        dest.write_bytes(file_obj)
    url = f"/static/uploads/meme_templates/{fname}"
    return {'secure_url': url, 'public_id': fname, '_local': True}


def _cloudinary_upload(file_obj, original_filename):
    """Upload file to Cloudinary (folder: content-os/).
    Falls back to local storage if Cloudinary is not configured."""
    if not _cloudinary_url:
        return _local_upload(file_obj, original_filename)
    ext = original_filename.rsplit('.', 1)[-1].lower() if '.' in original_filename else 'bin'
    resource_type = 'video' if ext in {'mp4', 'mov', 'avi', 'webm'} else 'image'
    try:
        result = cloudinary.uploader.upload(
            file_obj,
            folder='content-os',
            resource_type=resource_type,
            use_filename=False,
            unique_filename=True,
            timeout=45,
        )
        return result
    except Exception as e:
        app.logger.error(f'Cloudinary upload error: {e}')
        return _local_upload(file_obj, original_filename)

def _cloudinary_delete(public_id, resource_type='image'):
    """Delete asset from Cloudinary by public_id."""
    if not _cloudinary_url or not public_id:
        return
    try:
        cloudinary.uploader.destroy(public_id, resource_type=resource_type)
    except Exception as e:
        app.logger.error(f'Cloudinary delete error: {e}')


# ─────────────────────── MEMEOS BRIDGE ───────────────────────
# Nimmt fertige Meme-Posts (Einzelbild oder Karussell) von MemeOS entgegen.
# Öffentlich erreichbar (kein Login), deshalb eigene Auth über einen geteilten Secret-Key —
# fail-closed: ohne konfigurierten Key wird der Request abgelehnt, nicht durchgelassen
# (siehe CityBot-Telegram-Webhook-Fund vom 2026-07-02 — dort war das umgekehrt und ausnutzbar).

def _find_account_for_city(city_name):
    """Bevorzugt einen dedizierten Meme-Account (\"<Stadt> Memes\"),
    fällt sonst auf den News-Account (\"<Stadt>schau\") zurück."""
    if not city_name:
        return None
    candidates = [f'{city_name} Memes', f'{city_name}schau']
    for cand in candidates:
        acc = Account.query.filter(Account.name.ilike(cand)).first()
        if acc:
            return acc
    return Account.query.filter(Account.name.ilike(f'%{city_name}%')).first()


@app.route('/api/memeos/receive', methods=['POST'])
def api_memeos_receive():
    expected_key = os.environ.get('MEMEOS_BRIDGE_KEY') or get_setting('memeos_bridge_key')
    if not expected_key:
        return jsonify({'ok': False, 'error': 'MEMEOS_BRIDGE_KEY nicht konfiguriert'}), 503
    given_key = request.headers.get('X-MemeOS-Key', '')
    if not secrets.compare_digest(given_key, expected_key):
        return jsonify({'ok': False, 'error': 'unauthorized'}), 403

    meta = {}
    if 'meta' in request.form:
        try:
            meta = json.loads(request.form['meta'])
        except Exception:
            meta = {}
    elif request.is_json:
        meta = request.json or {}

    city_name = meta.get('city', '')
    account = _find_account_for_city(city_name)
    if not account:
        return jsonify({'ok': False, 'error': f'Kein Account für Stadt "{city_name}" gefunden'}), 404

    # Bilder einsammeln: entweder mehrere Dateien (Karussell) oder eine einzelne
    files = request.files.getlist('images') or request.files.getlist('image')
    if not files:
        return jsonify({'ok': False, 'error': 'Keine Bilder im Request'}), 400

    media_ids = []
    for file in files:
        if not file or not file.filename:
            continue
        original = secure_filename(file.filename)
        ftype = get_file_type(original) if original else 'image'
        mime = mimetypes.guess_type(original)[0] or 'image/png'
        file_bytes = file.read()
        cl = _cloudinary_upload(io.BytesIO(file_bytes), original or 'memeos.png')
        media = MediaItem(
            filename=cl['public_id'],
            original_filename=original,
            file_type=ftype,
            mime_type=mime,
            file_size=cl.get('bytes', len(file_bytes)),
            width=cl.get('width'),
            height=cl.get('height'),
            url=cl['secure_url'],
            storage_source='local' if cl.get('_local') else 'cloudinary',
        )
        db.session.add(media)
        db.session.flush()
        media_ids.append(media.id)

    if not media_ids:
        db.session.rollback()
        return jsonify({'ok': False, 'error': 'Keine gültigen Bilder empfangen'}), 400

    post_type = 'carousel' if len(media_ids) > 1 else 'feed'
    post = ScheduledPost(
        account_id=account.id,
        media_item_id=media_ids[0],
        media_ids=json.dumps(media_ids),
        caption=meta.get('caption', ''),
        post_type=post_type,
        status='draft',
        slot_type='disabled',
        scheduled_at=datetime.utcnow(),
    )
    db.session.add(post)
    db.session.commit()

    return jsonify({'ok': True, 'scheduled_post_id': post.id, 'media_ids': media_ids,
                    'account': account.name, 'post_type': post_type})


# ─────────────────────── AUTH ───────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated


# ── Global auth guard — schützt ALLE Routen außer Login/Logout/Static ──
# telegram_bot_webhook: Telegram ruft diesen Endpoint ohne Session auf → muss öffentlich sein,
# sonst werden alle eingehenden Bot-Kommandos (/heute, /vorrat …) mit 401 abgewiesen.
# api_memeos_receive: MemeOS hat keine Session hier → eigene Auth via X-MemeOS-Key (siehe Route).
PUBLIC_ENDPOINTS = {'login', 'logout', 'static', 'cron_sync_followers',
                    'cron_morning_report', 'telegram_bot_webhook', 'api_memeos_receive',
                    'canva_callback'}  # Redirect von Canva — Token-Austausch scheitert ohne
                    # gültigen, session-gebundenen code_verifier ohnehin sauber ab

@app.before_request
def global_auth_guard():
    if request.endpoint and request.endpoint not in PUBLIC_ENDPOINTS:
        if not session.get('user_id'):
            if request.path.startswith('/api/') or request.is_json:
                return jsonify({'ok': False, 'error': 'Session abgelaufen – bitte neu anmelden.'}), 401
            return redirect(url_for('login', next=request.path))


def current_user():
    uid = session.get('user_id')
    return User.query.get(uid) if uid else None


def now_berlin():
    """Aktuelle Zeit als naive Berlin-Ortszeit.

    scheduled_at und deadline werden als Berlin-naive Wanduhrzeit gespeichert
    (User gibt im Kalender/Planer lokale Zeit ein). Tages- und Jetzt-Vergleiche
    gegen diese Spalten müssen daher in Berliner Zeit erfolgen — nicht in UTC,
    sonst zeigt die UI zwischen 00:00–02:00 Uhr den falschen Tag.
    NICHT für Vergleiche gegen UTC-Audit-Timestamps (created_at, recorded_at,
    published_at) verwenden."""
    return datetime.now(ZoneInfo('Europe/Berlin')).replace(tzinfo=None)


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


WATCHLIST_SEED = [
    ("Berlin", 3685265, [
        ("Pankow","ca. 427.000 Einwohner"),("Mitte","ca. 397.000 Einwohner"),
        ("Tempelhof-Schöneberg","ca. 357.000 Einwohner"),("Charlottenburg-Wilmersdorf","ca. 340.000 Einwohner"),
        ("Neukölln","ca. 330.000 Einwohner"),("Steglitz-Zehlendorf","ca. 310.000 Einwohner"),
        ("Lichtenberg","ca. 308.000 Einwohner"),("Treptow-Köpenick","ca. 294.000 Einwohner"),
        ("Friedrichshain-Kreuzberg","ca. 293.000 Einwohner"),("Marzahn-Hellersdorf","ca. 293.000 Einwohner"),
        ("Reinickendorf","ca. 268.000 Einwohner"),("Spandau","ca. 260.000 Einwohner"),
    ],[
        "Hertha BSC","1. FC Union Berlin","BFC Dynamo"
    ],[
        ("Freie Universität Berlin","ca. 38.000 Studierende"),
        ("Humboldt-Universität zu Berlin","ca. 36.000 Studierende"),
        ("Technische Universität Berlin","ca. 35.000 Studierende"),
    ]),
    ("Hamburg", 1862565, [
        ("Wandsbek","ca. 456.000 Einwohner"),("Hamburg-Nord","ca. 328.000 Einwohner"),
        ("Hamburg-Mitte","ca. 315.000 Einwohner"),("Altona","ca. 281.000 Einwohner"),
        ("Eimsbüttel","ca. 276.000 Einwohner"),("Harburg","ca. 178.000 Einwohner"),
        ("Bergedorf","ca. 134.000 Einwohner"),
    ],[
        "Hamburger SV","FC St. Pauli","Altona 93"
    ],[
        ("Universität Hamburg","ca. 42.000 Studierende"),
        ("HAW Hamburg","ca. 16.000 Studierende"),
    ]),
    ("München", 1505036, [
        ("Ramersdorf-Perlach","ca. 119.000 Einwohner"),("Neuhausen-Nymphenburg","ca. 101.000 Einwohner"),
        ("Thalkirchen-Obersendling-Forstenried-Fürstenried-Solln","ca. 99.000 Einwohner"),
        ("Bogenhausen","ca. 94.000 Einwohner"),
    ],[
        "FC Bayern München","TSV 1860 München","Türkgücü München"
    ],[
        ("LMU München","ca. 54.000 Studierende"),
        ("Technische Universität München","ca. 52.000 Studierende"),
        ("Hochschule München","ca. 19.000 Studierende"),
    ]),
    ("Köln", 1025523, [
        ("Lindenthal","ca. 153.000 Einwohner"),("Mülheim","ca. 152.000 Einwohner"),
        ("Innenstadt","ca. 128.000 Einwohner"),("Kalk","ca. 126.000 Einwohner"),
        ("Nippes","ca. 118.000 Einwohner"),("Porz","ca. 113.000 Einwohner"),
        ("Rodenkirchen","ca. 112.000 Einwohner"),("Ehrenfeld","ca. 110.000 Einwohner"),
        ("Chorweiler","ca. 83.000 Einwohner"),
    ],[
        "1. FC Köln","Viktoria Köln","Fortuna Köln"
    ],[
        ("Universität zu Köln","ca. 46.000 Studierende"),
        ("TH Köln","ca. 17.500 Studierende"),
    ]),
    ("Frankfurt am Main", 756021, [],[
        "Eintracht Frankfurt","FSV Frankfurt","Rot-Weiss Frankfurt"
    ],[
        ("Goethe-Universität Frankfurt","ca. 41.000 Studierende"),
        ("Frankfurt University of Applied Sciences","ca. 15.000 Studierende"),
    ]),
    ("Düsseldorf", 619444, [
        ("Stadtbezirk 3","ca. 123.000 Einwohner"),("Stadtbezirk 9","ca. 101.000 Einwohner"),
        ("Stadtbezirk 10","ca. 100.000 Einwohner"),("Stadtbezirk 6","ca. 90.000 Einwohner"),
    ],[
        "Fortuna Düsseldorf","TuRU Düsseldorf","Rather SV"
    ],[
        ("Heinrich-Heine-Universität Düsseldorf","ca. 35.000 Studierende"),
    ]),
    ("Stuttgart", 612663, [],[
        "VfB Stuttgart","Stuttgarter Kickers","VfB Stuttgart II"
    ],[
        ("Universität Stuttgart","ca. 24.000 Studierende"),
    ]),
    ("Leipzig", 611850, [
        ("Süd","ca. 110.000 Einwohner"),("Ost","ca. 106.000 Einwohner"),
        ("Nordost","ca. 104.000 Einwohner"),("West","ca. 96.000 Einwohner"),
    ],[
        "RB Leipzig","1. FC Lokomotive Leipzig","BSG Chemie Leipzig"
    ],[
        ("Universität Leipzig","ca. 30.000 Studierende"),
    ]),
    ("Dortmund", 600880, [],[
        "Borussia Dortmund","Türkspor Dortmund","ASC 09 Dortmund"
    ],[
        ("Technische Universität Dortmund","ca. 32.000 Studierende"),
        ("Fachhochschule Dortmund","ca. 15.000 Studierende"),
    ]),
    ("Bremen", 588413, [
        ("Ost","ca. 116.000 Einwohner"),("Süd","ca. 124.000 Einwohner"),
    ],[
        "Werder Bremen","Bremer SV","FC Oberneuland"
    ],[
        ("Universität Bremen","ca. 18.000 Studierende"),
    ]),
    ("Essen", 573618, [
        ("Stadtbezirk II","ca. 95.000 Einwohner"),("Stadtbezirk V","ca. 108.000 Einwohner"),
    ],[
        "Rot-Weiss Essen","Schwarz-Weiß Essen","ETB Schwarz-Weiß Essen"
    ],[
        ("Universität Duisburg-Essen","ca. 42.000 Studierende gesamt"),
    ]),
    ("Dresden", 564904, [
        ("Neustadt","ca. 156.000 Einwohner"),("Prohlis","ca. 121.000 Einwohner"),
    ],[
        "Dynamo Dresden","Dresdner SC","Borea Dresden"
    ],[
        ("Technische Universität Dresden","ca. 30.000 Studierende"),
    ]),
    ("Nürnberg", 531159, [
        ("Südliche Außenstadt","ca. 104.000 Einwohner"),
    ],[
        "1. FC Nürnberg","ASV Nürnberg","SpVgg Mögeldorf"
    ],[]),
    ("Hannover", 522803, [
        ("Vahrenwald-List","ca. 91.000 Einwohner"),
    ],[
        "Hannover 96","HSC Hannover","Arminia Hannover"
    ],[
        ("Leibniz Universität Hannover","ca. 27.000 Studierende"),
    ]),
    ("Duisburg", 500810, [
        ("Duisburg-Mitte","ca. 112.000 Einwohner"),("Duisburg-Süd","ca. 81.000 Einwohner"),
    ],[
        "MSV Duisburg","VfB Homberg","Hamborn 07"
    ],[
        ("Universität Duisburg-Essen","ca. 42.000 Studierende gesamt"),
    ]),
    ("Bochum", 358880, [],[
        "VfL Bochum","SG Wattenscheid 09","DJK TuS Hordel"
    ],[
        ("Ruhr-Universität Bochum","ca. 42.000 Studierende"),
    ]),
    ("Wuppertal", 357243, [],[
        "Wuppertaler SV","Cronenberger SC","FSV Vohwinkel"
    ],[
        ("Bergische Universität Wuppertal","ca. 23.000 Studierende"),
    ]),
    ("Bielefeld", 331419, [],[
        "Arminia Bielefeld","VfB Fichte Bielefeld","TuS Dornberg"
    ],[
        ("Universität Bielefeld","ca. 25.000 Studierende"),
    ]),
    ("Bonn", 323587, [
        ("Stadtbezirk Bonn","ca. 150.000 Einwohner"),
    ],[
        "Bonner SC","FV Endenich","SSV Bornheim"
    ],[
        ("Universität Bonn","ca. 32.000 Studierende"),
    ]),
    ("Mannheim", 318035, [],[
        "Waldhof Mannheim","VfR Mannheim","TSV Amicitia Viernheim"
    ],[]),
    ("Karlsruhe", 309050, [],[
        "Karlsruher SC","ASV Durlach","SVK Beiertheim"
    ],[
        ("Karlsruher Institut für Technologie (KIT)","ca. 22.000 Studierende"),
    ]),
    ("Münster", 306368, [
        ("Mitte","ca. 124.000 Einwohner"),
    ],[
        "Preußen Münster","SC Münster 08","TuS Hiltrup"
    ],[
        ("Universität Münster","ca. 42.000 Studierende"),
    ]),
    ("Augsburg", 301786, [],[
        "FC Augsburg","TSV Schwaben Augsburg","Türkspor Augsburg"
    ],[
        ("Universität Augsburg","ca. 20.000 Studierende"),
    ]),
    ("Wiesbaden", 288850, [],[
        "SV Wehen Wiesbaden","FV Biebrich 02","SpVgg Sonnenberg"
    ],[]),
    ("Gelsenkirchen", 266199, [],[
        "FC Schalke 04","SSV Buer","SV Horst-Emscher 08"
    ],[]),
    ("Mönchengladbach", 267176, [
        ("Nord","ca. 87.000 Einwohner"),("Süd","ca. 87.000 Einwohner"),
    ],[
        "Borussia Mönchengladbach","1. FC Mönchengladbach","Sportfreunde Neuwerk"
    ],[]),
    ("Aachen", 263703, [
        ("Aachen-Mitte","ca. 153.000 Einwohner"),
    ],[
        "Alemannia Aachen","SV Eilendorf","VfL 05 Aachen"
    ],[
        ("RWTH Aachen","ca. 47.000 Studierende"),
    ]),
    ("Braunschweig", 252811, [],[
        "Eintracht Braunschweig","BSC Acosta","Freie Turner Braunschweig"
    ],[
        ("Technische Universität Braunschweig","ca. 17.000 Studierende"),
    ]),
    ("Kiel", 251842, [],[
        "Holstein Kiel","Kilia Kiel","Inter Türkspor Kiel"
    ],[
        ("Christian-Albrechts-Universität zu Kiel","ca. 24.000 Studierende"),
    ]),
    ("Chemnitz", 245618, [],[
        "Chemnitzer FC","VfB Fortuna Chemnitz","BSC Rapid Chemnitz"
    ],[]),
    ("Magdeburg", 244494, [],[
        "1. FC Magdeburg","MSV Börde Magdeburg","VfB Ottersleben"
    ],[]),
    ("Freiburg im Breisgau", 237460, [],[
        "SC Freiburg","Freiburger FC","FC Denzlingen"
    ],[
        ("Universität Freiburg","ca. 24.000 Studierende"),
    ]),
    ("Krefeld", 230738, [],[
        "KFC Uerdingen","VfR Fischeln","SC St. Tönis"
    ],[
        ("Hochschule Niederrhein","ca. 25.000 Studierende gesamt"),
    ]),
    ("Halle (Saale)", 226186, [],[
        "Hallescher FC","VfL Halle 96","SG Union Halle-Neustadt"
    ],[
        ("Martin-Luther-Universität Halle-Wittenberg","ca. 20.000 Studierende"),
    ]),
    ("Mainz", 224684, [],[
        "1. FSV Mainz 05","TSV Schott Mainz","SV Gonsenheim"
    ],[
        ("Johannes Gutenberg-Universität Mainz","ca. 31.000 Studierende"),
    ]),
    ("Erfurt", 218793, [],[
        "FC Rot-Weiß Erfurt","FC Erfurt Nord","SV Empor Erfurt"
    ],[]),
    ("Lübeck", 217061, [],[
        "VfB Lübeck","1. FC Phönix Lübeck","Eichholzer SV"
    ],[]),
    ("Oberhausen", 213178, [
        ("Sterkrade","ca. 87.000 Einwohner"),
    ],[
        "Rot-Weiß Oberhausen","Arminia Klosterhardt","Sterkrade-Nord"
    ],[]),
    ("Rostock", 205307, [
        ("Ortsamtsbereich Mitte","ca. 84.000 Einwohner"),
    ],[
        "Hansa Rostock","Rostocker FC","SV Warnemünde Fußball"
    ],[]),
    ("Kassel", 197230, [],[
        "KSV Hessen Kassel","CSC 03 Kassel","TSV Wolfsanger"
    ],[
        ("Universität Kassel","ca. 23.000 Studierende"),
    ]),
    ("Hagen", 189983, [
        ("Stadtbezirk Mitte","ca. 100.000 Einwohner"),
    ],[
        "SpVg Hagen 11","SSV Hagen","SV Hohenlimburg 1910"
    ],[
        ("FernUniversität in Hagen","ca. 67.000 Studierende"),
    ]),
    ("Potsdam", 184754, [],[
        "SV Babelsberg 03","Fortuna Babelsberg","Potsdamer Kickers"
    ],[
        ("Universität Potsdam","ca. 22.000 Studierende"),
    ]),
    ("Saarbrücken", 182859, [
        ("Mitte","ca. 123.000 Einwohner"),
    ],[
        "1. FC Saarbrücken","SV Saar 05 Saarbrücken","FC Rastpfuhl"
    ],[
        ("Universität des Saarlandes","ca. 16.500 Studierende"),
    ]),
    ("Hamm", 179108, [
        ("Hamm-Mitte","ca. 88.000 Einwohner"),
    ],[
        "Hammer SpVg","Westfalia Rhynern","TuS Uentrop"
    ],[]),
    ("Ludwigshafen am Rhein", 177222, [],[
        "Arminia Ludwigshafen","FSV Oggersheim","Ludwigshafener SC"
    ],[]),
    ("Oldenburg", 177055, [],[
        "VfB Oldenburg","VfL Oldenburg","GVO Oldenburg"
    ],[
        ("Universität Oldenburg","ca. 18.000 Studierende"),
    ]),
    ("Mülheim an der Ruhr", 171674, [],[
        "1. FC Mülheim","VfB Speldorf","Mülheimer FC 97"
    ],[]),
    ("Leverkusen", 168299, [],[
        "Bayer 04 Leverkusen","SV Schlebusch","SC Leverkusen"
    ],[]),
    ("Darmstadt", 167029, [],[
        "SV Darmstadt 98","Rot-Weiß Darmstadt","FCA Darmstadt"
    ],[
        ("Technische Universität Darmstadt","ca. 25.000 Studierende"),
    ]),
    ("Osnabrück", 166257, [],[
        "VfL Osnabrück","Blau-Weiß Schinkel","SSC Dodesheide"
    ],[]),
    ("Solingen", 164621, [
        ("Solingen-Mitte","ca. 89.000 Einwohner"),
    ],[
        "1. FC Solingen","VfB Solingen","BV Gräfrath"
    ],[]),
    ("Paderborn", 155906, [
        ("Kernstadt","ca. 88.000 Einwohner"),
    ],[
        "SC Paderborn 07","Delbrücker SC","SV Heide Paderborn"
    ],[
        ("Universität Paderborn","ca. 20.000 Studierende"),
    ]),
    ("Herne", 156266, [
        ("Herne-Mitte","ca. 86.000 Einwohner"),
    ],[
        "DSC Wanne-Eickel","SV Sodingen","SV Wanne 11"
    ],[]),
    ("Heidelberg", 155756, [],[
        "Heidelberger SC","FC Victoria Bammental","TSG 62/09 Weinheim"
    ],[
        ("Universität Heidelberg","ca. 29.000 Studierende"),
    ]),
    ("Neuss", 153767, [],[
        "VfR Neuss","Holzheimer SG","DJK Novesia Neuss"
    ],[]),
    ("Regensburg", 151517, [],[
        "SSV Jahn Regensburg","Freier TuS Regensburg","SV Fortuna Regensburg"
    ],[
        ("Universität Regensburg","ca. 21.000 Studierende"),
    ]),
    ("Ingolstadt", 140799, [],[
        "FC Ingolstadt 04","MTV Ingolstadt","VfB Eichstätt"
    ],[]),
    ("Pforzheim", 134912, [],[
        "1. CfR Pforzheim","GU-Türk. SV Pforzheim","1. FC Ispringen"
    ],[]),
    ("Würzburg", 133753, [],[
        "Würzburger Kickers","Würzburger FV","TSV Lengfeld"
    ],[
        ("Universität Würzburg","ca. 27.000 Studierende"),
    ]),
    ("Offenbach am Main", 132746, [],[
        "Kickers Offenbach","Spvgg. 03 Neu-Isenburg","TSG Neu-Isenburg"
    ],[]),
    ("Fürth", 131344, [],[
        "SpVgg Greuther Fürth","ASV Fürth","SG Quelle Fürth"
    ],[]),
    ("Heilbronn", 131986, [],[
        "FC Union Heilbronn","VfR Heilbronn","TSG Heilbronn"
    ],[]),
    ("Ulm", 129882, [],[
        "SSV Ulm 1846","TSV Neu-Ulm","FV Illertissen"
    ],[]),
    ("Wolfsburg", 129813, [],[
        "VfL Wolfsburg","Lupo Martini Wolfsburg","SSV Vorsfelde"
    ],[]),
    ("Göttingen", 130521, [],[
        "1. SC Göttingen 05","SVG Göttingen","RSV Göttingen 05"
    ],[
        ("Universität Göttingen","ca. 28.000 Studierende"),
    ]),
    ("Reutlingen", 118852, [
        ("Kernstadt","ca. 87.000 Einwohner"),
    ],[
        "SSV Reutlingen 05","TSG Reutlingen","VfL Pfullingen"
    ],[]),
    ("Bremerhaven", 118502, [
        ("Stadtbezirk Mitte","ca. 81.000 Einwohner"),
    ],[
        "OSC Bremerhaven","Leher TS","ESC Geestemünde"
    ],[]),
    ("Bottrop", 118482, [],[
        "VfB Bottrop","SV Rhenania Bottrop","Batenbrocker Ruhrpott Kicker"
    ],[]),
    ("Erlangen", 116450, [],[
        "ATSV Erlangen","FSV Erlangen-Bruck","SC Eltersdorf"
    ],[
        ("FAU Erlangen-Nürnberg","ca. 39.000 Studierende gesamt"),
    ]),
    ("Recklinghausen", 114851, [],[
        "FC 96 Recklinghausen","SV Hochlar 28","SpVgg Erkenschwick"
    ],[]),
    ("Remscheid", 113333, [],[
        "FC Remscheid","1. Spvg Remscheid","BV 10 Remscheid"
    ],[]),
    ("Koblenz", 113378, [],[
        "TuS Koblenz","Rot-Weiss Koblenz","FC Cosmos Koblenz"
    ],[]),
    ("Bergisch Gladbach", 111174, [],[
        "SV Bergisch Gladbach 09","TV Herkenrath","FC Bensberg"
    ],[]),
    ("Jena", 109725, [],[
        "FC Carl Zeiss Jena","SV Schott Jena","FC Thüringen Jena"
    ],[
        ("Friedrich-Schiller-Universität Jena","ca. 17.500 Studierende"),
    ]),
    ("Salzgitter", 104433, [],[
        "SV Union Salzgitter","FC Germania Bleckenstedt","SC Gitter"
    ],[]),
    ("Trier", 104342, [],[
        "Eintracht Trier","FSV Trier-Tarforst","SV Trier-Irsch"
    ],[]),
    ("Siegen", 102450, [
        ("Siegen-Mitte/Kernstadt","ca. 85.000 Einwohner"),
    ],[
        "Sportfreunde Siegen","1. FC Kaan-Marienborn","TSV Weißtal"
    ],[
        ("Universität Siegen","ca. 17.000 Studierende"),
    ]),
    ("Moers", 101298, [
        ("Moers-Mitte","ca. 82.000 Einwohner"),
    ],[
        "GSV Moers","SV Scherpenberg","VfL Repelen"
    ],[]),
    ("Gütersloh", 99854, [
        ("Kernstadt","ca. 84.000 Einwohner"),
    ],[
        "FC Gütersloh","SV Avenwedde","Aramäer Gütersloh"
    ],[]),
    ("Kaiserslautern", 100426, [],[
        "1. FC Kaiserslautern","SV Morlautern","TSG Kaiserslautern"
    ],[
        ("RPTU Kaiserslautern-Landau","ca. 20.000 Studierende gesamt"),
    ]),
    ("Hildesheim", 98207, [
        ("Kernstadt","ca. 86.000 Einwohner"),
    ],[
        "VfV 06 Hildesheim","SV Borussia Hildesheim","PSV Grün-Weiß Hildesheim"
    ],[]),
    ("Schwerin", 98308, [],[
        "FC Mecklenburg Schwerin","MSV Pampow","SG Dynamo Schwerin"
    ],[]),
    ("Hanau", 97956, [],[
        "Hanauer FC 93","SC 1960 Hanau","TSV 1860 Hanau"
    ],[]),
    ("Flensburg", 95568, [],[
        "SC Weiche Flensburg 08","Flensburg 08","TSB Flensburg"
    ],[]),
    ("Esslingen am Neckar", 96182, [],[
        "FC Esslingen","TSV Berkheim","SV 1845 Esslingen"
    ],[]),
    ("Gera", 95608, [],[
        "Bischofswerdaer FV 08","BSG Wismut Gera","1. FC Gera 03"
    ],[]),
    ("Cottbus", 95123, [],[
        "Energie Cottbus","VfB Krieschow","SV Wacker Ströbitz"
    ],[]),
    ("Düren", 94539, [],[
        "1. FC Düren","Dürener Spielverein","GFC Düren 09"
    ],[]),
    ("Ludwigsburg", 92858, [],[
        "MTV Ludwigsburg","SGV Freiberg Fußball","FSV 08 Bietigheim-Bissingen"
    ],[]),
    ("Tübingen", 92322, [],[
        "Tübinger SV 1845","SV 03 Tübingen","TSG Tübingen"
    ],[
        ("Universität Tübingen","ca. 28.000 Studierende"),
    ]),
    ("Iserlohn", 91317, [],[
        "Iserlohner TS","FC Iserlohn","ASSV Letmathe"
    ],[]),
    ("Witten", 91474, [],[
        "TuS Stockum","SV Herbede","Wittener Sportfreunde"
    ],[]),
    ("Villingen-Schwenningen", 89756, [],[
        "FC 08 Villingen","BFC Villingen","BSV Schwenningen"
    ],[]),
    ("Ratingen", 88914, [],[
        "Ratingen 04/19","TuS 08 Lintorf","DJK Sparta Bilk"
    ],[]),
    ("Gießen", 89179, [],[
        "FC Gießen","TSV Klein-Linden","MTV 1846 Gießen"
    ],[
        ("Justus-Liebig-Universität Gießen","ca. 25.000 Studierende"),
    ]),
    ("Zwickau", 87410, [],[
        "FSV Zwickau","Planitzer SC","SV 1861 Kirchberg"
    ],[]),
    ("Konstanz", 86919, [],[
        "SC Konstanz-Wollmatingen","FC Konstanz","SV Allensbach"
    ],[]),
    ("Marl", 86899, [],[
        "TSV Marl-Hüls","SpVgg Marl","DJK Germania Lenkerbeck"
    ],[]),
    ("Worms", 86753, [],[
        "Wormatia Worms","TuS Neuhausen","SV Horchheim"
    ],[]),
    ("Lünen", 85844, [],[
        "Lüner SV","BV Brambauer-Lünen","Viktoria Lünen"
    ],[]),
]


def _pwf_seed_default_sources():
    """Einmalige Erstbefüllung: zwei live geprüfte Rückruf-Quellen + Auto-
    Recherche standardmäßig an — damit die Product Alert Factory ohne
    manuelle Einrichtung von selbst scannt. Läuft nur EIN EINZIGES MAL (Marker
    in AppSettings), damit ein späteres bewusstes Löschen der Quellen durch
    den Nutzer nicht bei jedem Neustart rückgängig gemacht wird. Nutzt
    AppSettings direkt statt get_setting/set_setting, da diese erst später im
    Modul definiert werden (init_db läuft synchron beim Modul-Laden)."""
    marker = AppSettings.query.filter_by(key='pwf_sources_seeded').first()
    if marker:
        return
    if not ProductAlertSource.query.first():
        db.session.add(ProductAlertSource(
            name='BAuA Produktrückrufe',
            url='https://www.baua.de/DE/Themen/Monitoring-Evaluation/Marktueberwachung-Produktsicherheit/RSS/Produktrueckrufe-RSS-Feed.xml',
            active=True, dedicated_feed=True))
        db.session.add(ProductAlertSource(
            name='produktwarnung.eu', url='https://www.produktwarnung.eu/feed/',
            active=True, dedicated_feed=False))
    if not AppSettings.query.filter_by(key='pwf_auto_research').first():
        db.session.add(AppSettings(key='pwf_auto_research', value='1'))
    db.session.add(AppSettings(key='pwf_sources_seeded', value='1'))
    db.session.commit()


def init_db():
    with app.app_context():
        db.create_all()

        # Veraltete Seed-AutomationRules löschen
        try:
            for bad_name in ['Frankfurt RSS', 'BVL Lebensmittelwarnungen', 'Deutschland News']:
                AutomationRule.query.filter_by(name=bad_name).delete()
            db.session.commit()
        except Exception:
            db.session.rollback()

        from sqlalchemy import text, inspect

        is_postgres = 'postgresql' in str(db.engine.url)

        # ── Auto-Migration: prüft ALLE Modell-Spalten gegen die echte DB ──
        # Verhindert, dass neue Modell-Felder die App auf Render crashen lassen.
        def auto_migrate_columns():
            try:
                insp = inspect(db.engine)
                existing_tables = set(insp.get_table_names())
                dialect = db.engine.dialect
                for table in db.metadata.sorted_tables:
                    if table.name not in existing_tables:
                        continue
                    existing = {c['name'] for c in insp.get_columns(table.name)}
                    for col in table.columns:
                        if col.name in existing:
                            continue
                        col_type = col.type.compile(dialect=dialect)
                        default_sql = ''
                        if col.default is not None and col.default.is_scalar:
                            v = col.default.arg
                            if isinstance(v, str):
                                default_sql = f" DEFAULT '{v}'"
                            elif isinstance(v, bool):
                                # Postgres akzeptiert für BOOLEAN kein DEFAULT 0/1 —
                                # das ließ boolsche Spalten (z.B. trend_topic.alerted)
                                # still fehlschlagen. TRUE/FALSE auf PG, 0/1 auf SQLite.
                                if is_postgres:
                                    default_sql = f" DEFAULT {'TRUE' if v else 'FALSE'}"
                                else:
                                    default_sql = f" DEFAULT {'1' if v else '0'}"
                            elif v is not None:
                                default_sql = f' DEFAULT {v}'
                        elif col.server_default is not None:
                            default_sql = f' DEFAULT {col.server_default.arg}'
                        try:
                            with db.engine.connect() as _c:
                                _c.execute(text(
                                    f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col_type}{default_sql}'
                                ))
                                _c.commit()
                            app.logger.info(f'Auto-Migration: {table.name}.{col.name} hinzugefügt')
                        except Exception:
                            pass
            except Exception as e:
                app.logger.warning(f'Auto-Migration Fehler: {e}')
        auto_migrate_columns()

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
            # ── account ── Telegram ──────────────────────────────────────
            safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS telegram_chat_id VARCHAR(100)')
            # ── account ── Layout / Inspiration ──────────────────────────
            safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS canva_url VARCHAR(500)')
            safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS layout_notes TEXT')
            safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS page_persona TEXT')
            # ── scheduled_post ───────────────────────────────────────────
            safe_alter("ALTER TABLE scheduled_post ADD COLUMN IF NOT EXISTS slot_type VARCHAR(20) DEFAULT 'fixed'")
            safe_alter("ALTER TABLE scheduled_post ADD COLUMN IF NOT EXISTS media_ids TEXT DEFAULT '[]'")
            safe_alter('ALTER TABLE scheduled_post ADD COLUMN IF NOT EXISTS telegram_sent_at TIMESTAMP')
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
            # ── meme_template ────────────────────────────────────────────
            safe_alter('ALTER TABLE meme_template ADD COLUMN IF NOT EXISTS meme_context TEXT')
            safe_alter('ALTER TABLE inspiration_source ADD COLUMN IF NOT EXISTS account_id INTEGER REFERENCES account(id)')
            # ── content_folder ────────────────────────────────────────────
            safe_alter('''CREATE TABLE IF NOT EXISTS content_folder (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                color VARCHAR(20) DEFAULT \'#6366f1\',
                icon VARCHAR(50) DEFAULT \'fa-folder\',
                account_id INTEGER REFERENCES account(id),
                sort_order INTEGER DEFAULT 0,
                posts_per_week INTEGER DEFAULT 0,
                notes TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )''')
            safe_alter('ALTER TABLE content_item ADD COLUMN IF NOT EXISTS folder_id INTEGER REFERENCES content_folder(id)')
            # ── inspiration_post: Likes / Comments / is_saved ────────────
            safe_alter('ALTER TABLE inspiration_post ADD COLUMN IF NOT EXISTS like_count INTEGER')
            safe_alter('ALTER TABLE inspiration_post ADD COLUMN IF NOT EXISTS comment_count INTEGER')
            safe_alter('ALTER TABLE inspiration_post ADD COLUMN IF NOT EXISTS is_saved BOOLEAN DEFAULT FALSE')
            safe_alter('ALTER TABLE inspiration_post ADD COLUMN IF NOT EXISTS carousel_urls TEXT')
            safe_alter('ALTER TABLE inspiration_post ADD COLUMN IF NOT EXISTS video_url VARCHAR(1000)')
            safe_alter('ALTER TABLE inspiration_post ADD COLUMN IF NOT EXISTS suggested_folder_id INTEGER REFERENCES content_folder(id)')
            safe_alter('ALTER TABLE inspiration_post ADD COLUMN IF NOT EXISTS folder_locked BOOLEAN DEFAULT FALSE')
            safe_alter('ALTER TABLE content_folder ADD COLUMN IF NOT EXISTS valid_from DATE')
            safe_alter('ALTER TABLE content_folder ADD COLUMN IF NOT EXISTS valid_until DATE')
            safe_alter('ALTER TABLE content_folder ADD COLUMN IF NOT EXISTS recurring_yearly BOOLEAN DEFAULT FALSE')
            # Bestehende status='saved' Posts migrieren → is_saved=True
            safe_alter("UPDATE inspiration_post SET is_saved=TRUE WHERE status='saved'")
            # ── account: KI-Caption Felder ───────────────────────────────
            safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS default_hashtags TEXT')
            safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS sports_hashtag VARCHAR(200)')
            safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS weather_city VARCHAR(100)')
            safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS hide_in_analytics BOOLEAN DEFAULT FALSE')
            # ── Wasserzeichen + Smart-Refill ─────────────────────────────
            safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS watermark_url VARCHAR(500)')
            safe_alter("ALTER TABLE account ADD COLUMN IF NOT EXISTS watermark_position VARCHAR(10) DEFAULT 'br'")
            safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS watermark_opacity FLOAT DEFAULT 0.7')
            safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS watermark_enabled BOOLEAN DEFAULT FALSE')
            safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS smart_refill_threshold INTEGER DEFAULT 0')
            safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS posts_per_week INTEGER DEFAULT 0')
            # ── Content-Serien ────────────────────────────────────────────
            safe_alter('''CREATE TABLE IF NOT EXISTS content_series (
                id SERIAL PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
                folder_id INTEGER REFERENCES content_folder(id),
                name VARCHAR(200) NOT NULL,
                description TEXT,
                days_of_week TEXT DEFAULT '[]',
                preferred_time VARCHAR(5) DEFAULT '09:00',
                post_type VARCHAR(20) DEFAULT 'feed',
                active BOOLEAN DEFAULT TRUE,
                last_scheduled TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW())''')
            # ── Kooperationen ─────────────────────────────────────────────
            safe_alter('''CREATE TABLE IF NOT EXISTS kooperation (
                id SERIAL PRIMARY KEY,
                account_id INTEGER REFERENCES account(id),
                partner_name VARCHAR(200) NOT NULL,
                koop_type VARCHAR(30) DEFAULT 'paid_post',
                status VARCHAR(20) DEFAULT 'anfrage',
                deadline DATE,
                amount FLOAT,
                currency VARCHAR(3) DEFAULT 'EUR',
                notes TEXT,
                content_item_id INTEGER REFERENCES content_item(id),
                reminder_sent BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW())''')
            safe_alter("ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS koop_type VARCHAR(30) DEFAULT 'paid_post'")
            safe_alter("ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS currency VARCHAR(3) DEFAULT 'EUR'")
            safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS contact_name VARCHAR(200)')
            safe_alter("ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS payment_status VARCHAR(20) DEFAULT 'offen'")
            safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS start_date DATE')
            safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS deliverables TEXT')
            safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS partner_rating INTEGER')
            safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS payment_due_date DATE')
            safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS invoice_number VARCHAR(100)')
            safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS invoice_sent_at DATE')
            safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS payment_received_at DATE')
            safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS payment_notes TEXT')
            safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS posting_dates TEXT')
            safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS invoice_reminder_sent BOOLEAN DEFAULT FALSE')
            safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS payment_reminder_sent BOOLEAN DEFAULT FALSE')
            safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS campaign_name VARCHAR(200)')
            safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS posting_reminder_sent BOOLEAN DEFAULT FALSE')
            safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS partner_id INTEGER')
            # ── Partner-CRM ───────────────────────────────────────────────
            safe_alter('''CREATE TABLE IF NOT EXISTS partner (
                id SERIAL PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                company VARCHAR(200),
                email VARCHAR(200),
                phone VARCHAR(100),
                website VARCHAR(500),
                category VARCHAR(100),
                status VARCHAR(20) DEFAULT 'aktiv',
                rating INTEGER,
                notes TEXT,
                total_deals INTEGER DEFAULT 0,
                total_revenue FLOAT DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW())''')
            safe_alter('''CREATE TABLE IF NOT EXISTS ai_usage_log (
                id SERIAL PRIMARY KEY,
                feature VARCHAR(60) NOT NULL,
                model VARCHAR(80) NOT NULL,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_eur FLOAT DEFAULT 0.0,
                created_at TIMESTAMP DEFAULT NOW())''')
            # ── Content-Ideen-Kontext ─────────────────────────────────────
            safe_alter('''CREATE TABLE IF NOT EXISTS account_ideen_context (
                id SERIAL PRIMARY KEY,
                account_id INTEGER NOT NULL UNIQUE REFERENCES account(id) ON DELETE CASCADE,
                konzept TEXT, zielgruppe TEXT, tonalitaet TEXT, themen TEXT,
                last_generated TIMESTAMP, generated_ideas TEXT,
                updated_at TIMESTAMP DEFAULT NOW())''')
            safe_alter('ALTER TABLE account_ideen_context ADD COLUMN IF NOT EXISTS past_posts_json TEXT')
            safe_alter('ALTER TABLE account_ideen_context ADD COLUMN IF NOT EXISTS page_analysis TEXT')
            safe_alter('ALTER TABLE account_ideen_context ADD COLUMN IF NOT EXISTS analyse_feedback TEXT')
            safe_alter('ALTER TABLE account_ideen_context ADD COLUMN IF NOT EXISTS analyse_category VARCHAR(100)')
            safe_alter('ALTER TABLE account_ideen_context ADD COLUMN IF NOT EXISTS studio_active BOOLEAN DEFAULT FALSE')
            safe_alter('ALTER TABLE account_ideen_context ADD COLUMN IF NOT EXISTS onboarding_done BOOLEAN DEFAULT FALSE')
            safe_alter('ALTER TABLE account_ideen_context ADD COLUMN IF NOT EXISTS usp TEXT')
            safe_alter('ALTER TABLE account_ideen_context ADD COLUMN IF NOT EXISTS onboarding_qa TEXT')
            # ── Wetter-System ────────────────────────────────────────────
            safe_alter('ALTER TABLE content_folder ADD COLUMN IF NOT EXISTS trigger_condition VARCHAR(50)')
            safe_alter('''CREATE TABLE IF NOT EXISTS weather_cache (
                id SERIAL PRIMARY KEY,
                account_id INTEGER NOT NULL UNIQUE REFERENCES account(id),
                city_name VARCHAR(100), temperature FLOAT, weather_code INTEGER,
                wind_speed FLOAT, description VARCHAR(200), forecast_json TEXT,
                checked_at TIMESTAMP DEFAULT NOW())''')
            safe_alter('''CREATE TABLE IF NOT EXISTS weather_trigger_log (
                id SERIAL PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES account(id),
                trigger_type VARCHAR(50) NOT NULL,
                fired_at TIMESTAMP DEFAULT NOW(),
                post_id INTEGER REFERENCES scheduled_post(id),
                city_name VARCHAR(100), temperature FLOAT)''')
            safe_alter('CREATE INDEX IF NOT EXISTS ix_weather_trigger_log_acc ON weather_trigger_log(account_id, trigger_type, fired_at DESC)')
            # ── media_item: Duplikat-Hash ────────────────────────────────
            safe_alter('ALTER TABLE media_item ADD COLUMN IF NOT EXISTS image_hash VARCHAR(64)')
            safe_alter('CREATE INDEX IF NOT EXISTS ix_media_item_image_hash ON media_item(image_hash)')
            safe_alter('''CREATE TABLE IF NOT EXISTS app_todo (
                id SERIAL PRIMARY KEY,
                text TEXT NOT NULL,
                category VARCHAR(50) DEFAULT \'idee\',
                done BOOLEAN DEFAULT FALSE,
                priority INTEGER DEFAULT 0,
                image_path VARCHAR(500),
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW())''')
            safe_alter('ALTER TABLE app_todo ADD COLUMN IF NOT EXISTS image_path VARCHAR(500)')
            safe_alter('ALTER TABLE app_todo ADD COLUMN IF NOT EXISTS linked_page VARCHAR(100)')
            safe_alter('''CREATE TABLE IF NOT EXISTS ausgabe (
                id SERIAL PRIMARY KEY,
                titel VARCHAR(200) NOT NULL,
                betrag FLOAT NOT NULL,
                kategorie VARCHAR(100) DEFAULT \'Sonstiges\',
                datum DATE NOT NULL,
                finanzamt BOOLEAN DEFAULT TRUE,
                notizen TEXT,
                beleg_url VARCHAR(500),
                created_at TIMESTAMP DEFAULT NOW())''')
            safe_alter('''CREATE TABLE IF NOT EXISTS abo_kosten (
                id SERIAL PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                betrag FLOAT NOT NULL,
                intervall VARCHAR(20) DEFAULT \'monatlich\',
                aktiv BOOLEAN DEFAULT TRUE,
                kategorie VARCHAR(100) DEFAULT \'Software & Tools\',
                finanzamt BOOLEAN DEFAULT TRUE,
                notizen TEXT,
                start_datum DATE,
                created_at TIMESTAMP DEFAULT NOW())''')
            safe_alter('''CREATE TABLE IF NOT EXISTS geplant_ausgabe (
                id SERIAL PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                url VARCHAR(500),
                betrag FLOAT,
                kategorie VARCHAR(100) DEFAULT \'Sonstiges\',
                prioritaet VARCHAR(20) DEFAULT \'mittel\',
                notizen TEXT,
                gekauft BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW())''')
            safe_alter('''CREATE TABLE IF NOT EXISTS local_event (
                id SERIAL PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                city VARCHAR(100),
                datum DATE,
                beschreibung TEXT,
                url VARCHAR(500),
                kategorie VARCHAR(50) DEFAULT \'Sonstiges\',
                content_idee TEXT,
                created_at TIMESTAMP DEFAULT NOW())''')
            safe_alter('''CREATE TABLE IF NOT EXISTS seiten_kauf (
                id SERIAL PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                handle VARCHAR(100),
                platform VARCHAR(30) DEFAULT \'Instagram\',
                followers INTEGER,
                engagement_rate FLOAT,
                nische VARCHAR(100),
                preis_vorstellung FLOAT,
                unser_angebot FLOAT,
                einigungspreis FLOAT,
                status VARCHAR(30) DEFAULT \'interessant\',
                kontakt VARCHAR(200),
                url VARCHAR(500),
                notizen TEXT,
                in_geplant BOOLEAN DEFAULT FALSE,
                gekauft_am DATE,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW())''')
            safe_alter('ALTER TABLE seiten_kauf ADD COLUMN IF NOT EXISTS einigungspreis FLOAT')
            safe_alter('ALTER TABLE seiten_kauf ADD COLUMN IF NOT EXISTS in_geplant BOOLEAN DEFAULT FALSE')
            safe_alter('''CREATE TABLE IF NOT EXISTS watchlist_seite (
                id SERIAL PRIMARY KEY,
                stadt VARCHAR(100) NOT NULL,
                ziel_typ VARCHAR(20),
                ziel_name VARCHAR(200) NOT NULL,
                ziel_meta VARCHAR(200),
                platform VARCHAR(30) DEFAULT \'Instagram\',
                url VARCHAR(500),
                handle VARCHAR(100),
                follower INTEGER,
                letzte_aktivitaet VARCHAR(50),
                seiten_status VARCHAR(30) DEFAULT \'nicht_gesucht\',
                notizen TEXT,
                kontaktiert_am TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW())''')
            safe_alter('ALTER TABLE watchlist_seite ADD COLUMN IF NOT EXISTS kontaktiert_am TIMESTAMP')
            safe_alter("ALTER TABLE watchlist_seite ADD COLUMN IF NOT EXISTS wl_kategorie VARCHAR(50) DEFAULT 'stadtseite'")
            safe_alter("ALTER TABLE watchlist_seite ADD COLUMN IF NOT EXISTS kaufprioritaet VARCHAR(20) DEFAULT 'keine'")
            safe_alter('ALTER TABLE watchlist_seite ADD COLUMN IF NOT EXISTS seiten_kategorie VARCHAR(100)')
            safe_alter('ALTER TABLE watchlist_seite ADD COLUMN IF NOT EXISTS preis_vorstellung FLOAT')
            safe_alter('ALTER TABLE watchlist_seite ADD COLUMN IF NOT EXISTS mein_angebot FLOAT')
            safe_alter('ALTER TABLE watchlist_seite ADD COLUMN IF NOT EXISTS zweck VARCHAR(30)')
            safe_alter('ALTER TABLE watchlist_seite ADD COLUMN IF NOT EXISTS ist_befreundet BOOLEAN DEFAULT FALSE')
            safe_alter('ALTER TABLE watchlist_seite ADD COLUMN IF NOT EXISTS seite_geplant BOOLEAN DEFAULT FALSE')
            safe_alter('ALTER TABLE watchlist_seite ADD COLUMN IF NOT EXISTS haben_seite BOOLEAN DEFAULT FALSE')
            safe_alter('ALTER TABLE watchlist_seite ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE')
            safe_alter('ALTER TABLE watchlist_seite ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP')
            safe_alter('''CREATE TABLE IF NOT EXISTS watchlist_follower_snapshot (
                id SERIAL PRIMARY KEY,
                seite_id INTEGER NOT NULL REFERENCES watchlist_seite(id) ON DELETE CASCADE,
                follower INTEGER NOT NULL,
                scanned_at TIMESTAMP DEFAULT NOW())''')
            safe_alter('''CREATE TABLE IF NOT EXISTS watchlist_city_meta (
                stadt VARCHAR(100) PRIMARY KEY,
                haben_seite BOOLEAN DEFAULT FALSE,
                seite_geplant BOOLEAN DEFAULT FALSE,
                updated_at TIMESTAMP DEFAULT NOW())''')
            # ── trend_topic: später ergänzte Spalten (Alert/Verlauf/Feedback) ──
            # Explizit hier, weil die Auto-Migration boolsche Defaults auf PG
            # früher falsch (DEFAULT 0 statt FALSE) erzeugt hat.
            safe_alter('ALTER TABLE trend_topic ADD COLUMN IF NOT EXISTS alerted BOOLEAN DEFAULT FALSE')
            safe_alter('ALTER TABLE trend_topic ADD COLUMN IF NOT EXISTS peak_score INTEGER DEFAULT 0')
            safe_alter('ALTER TABLE trend_topic ADD COLUMN IF NOT EXISTS prev_score INTEGER')
            safe_alter('ALTER TABLE trend_topic ADD COLUMN IF NOT EXISTS feedback INTEGER')
            safe_alter('ALTER TABLE trend_topic ADD COLUMN IF NOT EXISTS feedback_at TIMESTAMP')
            # ── product_alert: später ergänzte Spalten (Bild-Score/Alert/Feedback/Verknüpfung) ──
            safe_alter('ALTER TABLE product_alert ADD COLUMN IF NOT EXISTS image_match_score INTEGER')
            safe_alter('ALTER TABLE product_alert ADD COLUMN IF NOT EXISTS alerted BOOLEAN DEFAULT FALSE')
            safe_alter('ALTER TABLE product_alert ADD COLUMN IF NOT EXISTS feedback INTEGER')
            safe_alter('ALTER TABLE product_alert ADD COLUMN IF NOT EXISTS feedback_at TIMESTAMP')
            safe_alter('ALTER TABLE product_alert ADD COLUMN IF NOT EXISTS related_alert_id INTEGER')
            safe_alter('ALTER TABLE product_alert_source ADD COLUMN IF NOT EXISTS dedicated_feed BOOLEAN DEFAULT FALSE')

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

            # ── content_folder (SQLite) ───────────────────────────────
            existing_tables = inspector.get_table_names()
            if 'content_folder' not in existing_tables:
                safe_alter('''CREATE TABLE content_folder (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(100) NOT NULL,
                    color VARCHAR(20) DEFAULT \'#6366f1\',
                    icon VARCHAR(50) DEFAULT \'fa-folder\',
                    account_id INTEGER REFERENCES account(id),
                    sort_order INTEGER DEFAULT 0,
                    posts_per_week INTEGER DEFAULT 0,
                    notes TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')
            ci_cols2 = [c['name'] for c in inspector.get_columns('content_item')]
            if 'folder_id' not in ci_cols2:
                safe_alter('ALTER TABLE content_item ADD COLUMN folder_id INTEGER REFERENCES content_folder(id)')
            # inspiration_post: Likes / Comments / is_saved
            ip_cols = [c['name'] for c in inspector.get_columns('inspiration_post')]
            if 'like_count' not in ip_cols:
                safe_alter('ALTER TABLE inspiration_post ADD COLUMN like_count INTEGER')
            if 'comment_count' not in ip_cols:
                safe_alter('ALTER TABLE inspiration_post ADD COLUMN comment_count INTEGER')
            if 'is_saved' not in ip_cols:
                safe_alter('ALTER TABLE inspiration_post ADD COLUMN is_saved BOOLEAN DEFAULT 0')
                safe_alter("UPDATE inspiration_post SET is_saved=1 WHERE status='saved'")
            if 'carousel_urls' not in ip_cols:
                safe_alter('ALTER TABLE inspiration_post ADD COLUMN carousel_urls TEXT')
            if 'video_url' not in ip_cols:
                safe_alter('ALTER TABLE inspiration_post ADD COLUMN video_url VARCHAR(1000)')
            if 'suggested_folder_id' not in ip_cols:
                safe_alter('ALTER TABLE inspiration_post ADD COLUMN suggested_folder_id INTEGER REFERENCES content_folder(id)')
            if 'folder_locked' not in ip_cols:
                safe_alter('ALTER TABLE inspiration_post ADD COLUMN folder_locked BOOLEAN DEFAULT 0')
            cf_cols = [c['name'] for c in inspector.get_columns('content_folder')]
            if 'valid_from' not in cf_cols:
                safe_alter('ALTER TABLE content_folder ADD COLUMN valid_from DATE')
            if 'valid_until' not in cf_cols:
                safe_alter('ALTER TABLE content_folder ADD COLUMN valid_until DATE')
            if 'recurring_yearly' not in cf_cols:
                safe_alter('ALTER TABLE content_folder ADD COLUMN recurring_yearly BOOLEAN DEFAULT 0')
            # account: KI-Caption Felder
            if 'default_hashtags' not in account_cols:
                safe_alter('ALTER TABLE account ADD COLUMN default_hashtags TEXT')
            if 'sports_hashtag' not in account_cols:
                safe_alter('ALTER TABLE account ADD COLUMN sports_hashtag VARCHAR(200)')
            if 'weather_city' not in account_cols:
                safe_alter('ALTER TABLE account ADD COLUMN weather_city VARCHAR(100)')
            if 'hide_in_analytics' not in account_cols:
                safe_alter('ALTER TABLE account ADD COLUMN hide_in_analytics BOOLEAN DEFAULT 0')
            if 'watermark_url' not in account_cols:
                safe_alter('ALTER TABLE account ADD COLUMN watermark_url VARCHAR(500)')
            if 'watermark_position' not in account_cols:
                safe_alter("ALTER TABLE account ADD COLUMN watermark_position VARCHAR(10) DEFAULT 'br'")
            if 'watermark_opacity' not in account_cols:
                safe_alter('ALTER TABLE account ADD COLUMN watermark_opacity FLOAT DEFAULT 0.7')
            if 'watermark_enabled' not in account_cols:
                safe_alter('ALTER TABLE account ADD COLUMN watermark_enabled BOOLEAN DEFAULT 0')
            if 'smart_refill_threshold' not in account_cols:
                safe_alter('ALTER TABLE account ADD COLUMN smart_refill_threshold INTEGER DEFAULT 0')
            if 'posts_per_week' not in account_cols:
                safe_alter('ALTER TABLE account ADD COLUMN posts_per_week INTEGER DEFAULT 0')
            if 'telegram_chat_id' not in account_cols:
                safe_alter('ALTER TABLE account ADD COLUMN telegram_chat_id VARCHAR(100)')
            if 'canva_url' not in account_cols:
                safe_alter('ALTER TABLE account ADD COLUMN canva_url VARCHAR(500)')
            if 'layout_notes' not in account_cols:
                safe_alter('ALTER TABLE account ADD COLUMN layout_notes TEXT')
            if 'page_persona' not in account_cols:
                safe_alter('ALTER TABLE account ADD COLUMN page_persona TEXT')
            sp_cols2 = [c['name'] for c in inspector.get_columns('scheduled_post')]
            if 'telegram_sent_at' not in sp_cols2:
                safe_alter('ALTER TABLE scheduled_post ADD COLUMN telegram_sent_at DATETIME')
            ci_cols3 = [c['name'] for c in inspector.get_columns('content_item')]
            if 'approval_status' not in ci_cols3:
                safe_alter("ALTER TABLE content_item ADD COLUMN approval_status VARCHAR(20) DEFAULT 'pending'")
            if 'reviewed_by_id' not in ci_cols3:
                safe_alter('ALTER TABLE content_item ADD COLUMN reviewed_by_id INTEGER')
            if 'reviewed_at' not in ci_cols3:
                safe_alter('ALTER TABLE content_item ADD COLUMN reviewed_at DATETIME')
            if 'review_note' not in ci_cols3:
                safe_alter('ALTER TABLE content_item ADD COLUMN review_note TEXT')
            # content_series, kooperation, account_ideen_context (SQLite)
            safe_alter('''CREATE TABLE IF NOT EXISTS content_series (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES account(id),
                folder_id INTEGER REFERENCES content_folder(id),
                name VARCHAR(200) NOT NULL,
                description TEXT,
                days_of_week TEXT DEFAULT '[]',
                preferred_time VARCHAR(5) DEFAULT '09:00',
                post_type VARCHAR(20) DEFAULT 'feed',
                active BOOLEAN DEFAULT 1,
                last_scheduled DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            safe_alter('''CREATE TABLE IF NOT EXISTS kooperation (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER REFERENCES account(id),
                partner_name VARCHAR(200) NOT NULL,
                koop_type VARCHAR(30) DEFAULT 'paid_post',
                status VARCHAR(20) DEFAULT 'anfrage',
                deadline DATE, amount FLOAT, currency VARCHAR(3) DEFAULT 'EUR',
                notes TEXT, content_item_id INTEGER REFERENCES content_item(id),
                reminder_sent BOOLEAN DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                contact_name VARCHAR(200), payment_status VARCHAR(20) DEFAULT 'offen',
                start_date DATE, deliverables TEXT, partner_rating INTEGER)''')
            koop_cols = [c['name'] for c in inspector.get_columns('kooperation')]
            for _col, _ddl in [
                ('koop_type',           "ALTER TABLE kooperation ADD COLUMN koop_type VARCHAR(30) DEFAULT 'paid_post'"),
                ('currency',            "ALTER TABLE kooperation ADD COLUMN currency VARCHAR(3) DEFAULT 'EUR'"),
                ('contact_name',        'ALTER TABLE kooperation ADD COLUMN contact_name VARCHAR(200)'),
                ('payment_status',      "ALTER TABLE kooperation ADD COLUMN payment_status VARCHAR(20) DEFAULT 'offen'"),
                ('start_date',          'ALTER TABLE kooperation ADD COLUMN start_date DATE'),
                ('deliverables',        'ALTER TABLE kooperation ADD COLUMN deliverables TEXT'),
                ('partner_rating',      'ALTER TABLE kooperation ADD COLUMN partner_rating INTEGER'),
                ('payment_due_date',    'ALTER TABLE kooperation ADD COLUMN payment_due_date DATE'),
                ('invoice_number',      'ALTER TABLE kooperation ADD COLUMN invoice_number VARCHAR(100)'),
                ('invoice_sent_at',     'ALTER TABLE kooperation ADD COLUMN invoice_sent_at DATE'),
                ('payment_received_at', 'ALTER TABLE kooperation ADD COLUMN payment_received_at DATE'),
                ('payment_notes',            'ALTER TABLE kooperation ADD COLUMN payment_notes TEXT'),
                ('posting_dates',            'ALTER TABLE kooperation ADD COLUMN posting_dates TEXT'),
                ('invoice_reminder_sent',    'ALTER TABLE kooperation ADD COLUMN invoice_reminder_sent BOOLEAN DEFAULT 0'),
                ('payment_reminder_sent',    'ALTER TABLE kooperation ADD COLUMN payment_reminder_sent BOOLEAN DEFAULT 0'),
                ('posting_reminder_sent',    'ALTER TABLE kooperation ADD COLUMN posting_reminder_sent BOOLEAN DEFAULT 0'),
                ('campaign_name',            'ALTER TABLE kooperation ADD COLUMN campaign_name VARCHAR(200)'),
                ('partner_id',               'ALTER TABLE kooperation ADD COLUMN partner_id INTEGER REFERENCES partner(id)'),
            ]:
                if _col not in koop_cols:
                    safe_alter(_ddl)
            safe_alter('''CREATE TABLE IF NOT EXISTS account_ideen_context (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL UNIQUE REFERENCES account(id),
                konzept TEXT, zielgruppe TEXT, tonalitaet TEXT, themen TEXT,
                last_generated DATETIME, generated_ideas TEXT,
                past_posts_json TEXT, page_analysis TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            aic_cols = [c['name'] for c in inspector.get_columns('account_ideen_context')]
            if 'past_posts_json' not in aic_cols:
                safe_alter('ALTER TABLE account_ideen_context ADD COLUMN past_posts_json TEXT')
            if 'page_analysis' not in aic_cols:
                safe_alter('ALTER TABLE account_ideen_context ADD COLUMN page_analysis TEXT')
            if 'analyse_feedback' not in aic_cols:
                safe_alter('ALTER TABLE account_ideen_context ADD COLUMN analyse_feedback TEXT')
            if 'analyse_category' not in aic_cols:
                safe_alter('ALTER TABLE account_ideen_context ADD COLUMN analyse_category VARCHAR(100)')
            if 'studio_active' not in aic_cols:
                safe_alter('ALTER TABLE account_ideen_context ADD COLUMN studio_active BOOLEAN DEFAULT 0')
            if 'onboarding_done' not in aic_cols:
                safe_alter('ALTER TABLE account_ideen_context ADD COLUMN onboarding_done BOOLEAN DEFAULT 0')
            if 'usp' not in aic_cols:
                safe_alter('ALTER TABLE account_ideen_context ADD COLUMN usp TEXT')
            if 'onboarding_qa' not in aic_cols:
                safe_alter('ALTER TABLE account_ideen_context ADD COLUMN onboarding_qa TEXT')
            # content_folder: Wetter-Trigger
            if 'trigger_condition' not in cf_cols:
                safe_alter('ALTER TABLE content_folder ADD COLUMN trigger_condition VARCHAR(50)')
            # weather tables (SQLite: CREATE IF NOT EXISTS)
            safe_alter('''CREATE TABLE IF NOT EXISTS weather_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL UNIQUE REFERENCES account(id),
                city_name VARCHAR(100), temperature FLOAT, weather_code INTEGER,
                wind_speed FLOAT, description VARCHAR(200), forecast_json TEXT,
                checked_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            safe_alter('''CREATE TABLE IF NOT EXISTS weather_trigger_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES account(id),
                trigger_type VARCHAR(50) NOT NULL,
                fired_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                post_id INTEGER REFERENCES scheduled_post(id),
                city_name VARCHAR(100), temperature FLOAT)''')
            # ai_usage_log
            safe_alter('''CREATE TABLE IF NOT EXISTS ai_usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feature VARCHAR(60) NOT NULL,
                model VARCHAR(80) NOT NULL,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_eur FLOAT DEFAULT 0.0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            # media_item: Duplikat-Hash
            mi_cols = [c['name'] for c in inspector.get_columns('media_item')]
            if 'image_hash' not in mi_cols:
                safe_alter('ALTER TABLE media_item ADD COLUMN image_hash VARCHAR(64)')
            # app_todo
            safe_alter('''CREATE TABLE IF NOT EXISTS app_todo (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                category VARCHAR(50) DEFAULT 'idee',
                done BOOLEAN DEFAULT 0,
                priority INTEGER DEFAULT 0,
                image_path VARCHAR(500),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            at_cols = [c['name'] for c in inspector.get_columns('app_todo')]
            if 'image_path' not in at_cols:
                safe_alter('ALTER TABLE app_todo ADD COLUMN image_path VARCHAR(500)')
            if 'linked_page' not in at_cols:
                safe_alter('ALTER TABLE app_todo ADD COLUMN linked_page VARCHAR(100)')
            # ausgabe
            safe_alter('''CREATE TABLE IF NOT EXISTS ausgabe (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                titel VARCHAR(200) NOT NULL,
                betrag FLOAT NOT NULL,
                kategorie VARCHAR(100) DEFAULT 'Sonstiges',
                datum DATE NOT NULL,
                finanzamt BOOLEAN DEFAULT 1,
                notizen TEXT,
                beleg_url VARCHAR(500),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            # abo_kosten
            safe_alter('''CREATE TABLE IF NOT EXISTS abo_kosten (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(200) NOT NULL,
                betrag FLOAT NOT NULL,
                intervall VARCHAR(20) DEFAULT 'monatlich',
                aktiv BOOLEAN DEFAULT 1,
                kategorie VARCHAR(100) DEFAULT 'Software & Tools',
                finanzamt BOOLEAN DEFAULT 1,
                notizen TEXT,
                start_datum DATE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            safe_alter('''CREATE TABLE IF NOT EXISTS geplant_ausgabe (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(200) NOT NULL,
                url VARCHAR(500),
                betrag FLOAT,
                kategorie VARCHAR(100) DEFAULT 'Sonstiges',
                prioritaet VARCHAR(20) DEFAULT 'mittel',
                notizen TEXT,
                gekauft BOOLEAN DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            safe_alter('''CREATE TABLE IF NOT EXISTS local_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(200) NOT NULL,
                city VARCHAR(100),
                datum DATE,
                beschreibung TEXT,
                url VARCHAR(500),
                kategorie VARCHAR(50) DEFAULT 'Sonstiges',
                content_idee TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            safe_alter('''CREATE TABLE IF NOT EXISTS seiten_kauf (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(200) NOT NULL,
                handle VARCHAR(100),
                platform VARCHAR(30) DEFAULT 'Instagram',
                followers INTEGER,
                engagement_rate FLOAT,
                nische VARCHAR(100),
                preis_vorstellung FLOAT,
                unser_angebot FLOAT,
                einigungspreis FLOAT,
                status VARCHAR(30) DEFAULT 'interessant',
                kontakt VARCHAR(200),
                url VARCHAR(500),
                notizen TEXT,
                in_geplant BOOLEAN DEFAULT 0,
                gekauft_am DATE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            try:
                sk_cols = [c['name'] for c in inspector.get_columns('seiten_kauf')]
                if 'einigungspreis' not in sk_cols:
                    safe_alter('ALTER TABLE seiten_kauf ADD COLUMN einigungspreis FLOAT')
                if 'in_geplant' not in sk_cols:
                    safe_alter('ALTER TABLE seiten_kauf ADD COLUMN in_geplant BOOLEAN DEFAULT 0')
            except Exception:
                pass
            safe_alter('''CREATE TABLE IF NOT EXISTS watchlist_seite (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stadt VARCHAR(100) NOT NULL,
                ziel_typ VARCHAR(20),
                ziel_name VARCHAR(200) NOT NULL,
                ziel_meta VARCHAR(200),
                platform VARCHAR(30) DEFAULT 'Instagram',
                url VARCHAR(500),
                handle VARCHAR(100),
                follower INTEGER,
                letzte_aktivitaet VARCHAR(50),
                seiten_status VARCHAR(30) DEFAULT 'nicht_gesucht',
                notizen TEXT,
                kontaktiert_am DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            try:
                wl_cols = [c['name'] for c in inspector.get_columns('watchlist_seite')]
                if 'kontaktiert_am' not in wl_cols:
                    safe_alter('ALTER TABLE watchlist_seite ADD COLUMN kontaktiert_am DATETIME')
                if 'wl_kategorie' not in wl_cols:
                    safe_alter("ALTER TABLE watchlist_seite ADD COLUMN wl_kategorie VARCHAR(50) DEFAULT 'stadtseite'")
                if 'kaufprioritaet' not in wl_cols:
                    safe_alter("ALTER TABLE watchlist_seite ADD COLUMN kaufprioritaet VARCHAR(20) DEFAULT 'keine'")
                if 'seiten_kategorie' not in wl_cols:
                    safe_alter('ALTER TABLE watchlist_seite ADD COLUMN seiten_kategorie VARCHAR(100)')
                if 'preis_vorstellung' not in wl_cols:
                    safe_alter('ALTER TABLE watchlist_seite ADD COLUMN preis_vorstellung FLOAT')
                if 'mein_angebot' not in wl_cols:
                    safe_alter('ALTER TABLE watchlist_seite ADD COLUMN mein_angebot FLOAT')
                if 'zweck' not in wl_cols:
                    safe_alter('ALTER TABLE watchlist_seite ADD COLUMN zweck VARCHAR(30)')
                if 'ist_befreundet' not in wl_cols:
                    safe_alter('ALTER TABLE watchlist_seite ADD COLUMN ist_befreundet BOOLEAN DEFAULT 0')
                if 'seite_geplant' not in wl_cols:
                    safe_alter('ALTER TABLE watchlist_seite ADD COLUMN seite_geplant BOOLEAN DEFAULT 0')
                if 'haben_seite' not in wl_cols:
                    safe_alter('ALTER TABLE watchlist_seite ADD COLUMN haben_seite BOOLEAN DEFAULT 0')
                if 'is_deleted' not in wl_cols:
                    safe_alter('ALTER TABLE watchlist_seite ADD COLUMN is_deleted BOOLEAN DEFAULT 0')
                if 'deleted_at' not in wl_cols:
                    safe_alter('ALTER TABLE watchlist_seite ADD COLUMN deleted_at DATETIME')
            except Exception:
                pass
            safe_alter('''CREATE TABLE IF NOT EXISTS watchlist_follower_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seite_id INTEGER NOT NULL REFERENCES watchlist_seite(id),
                follower INTEGER NOT NULL,
                scanned_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            safe_alter('''CREATE TABLE IF NOT EXISTS watchlist_city_meta (
                stadt VARCHAR(100) PRIMARY KEY,
                haben_seite BOOLEAN DEFAULT 0,
                seite_geplant BOOLEAN DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')

            # Neue Felder: Partner, Kooperation (SQLite)
            try:
                p_cols   = [c['name'] for c in inspector.get_columns('partner')]
                if 'account_ids' not in p_cols:
                    safe_alter('ALTER TABLE partner ADD COLUMN account_ids TEXT')
                k_cols   = [c['name'] for c in inspector.get_columns('kooperation')]
                if 'contact_company' not in k_cols:
                    safe_alter('ALTER TABLE kooperation ADD COLUMN contact_company VARCHAR(200)')
                if 'contact_street' not in k_cols:
                    safe_alter('ALTER TABLE kooperation ADD COLUMN contact_street VARCHAR(200)')
                if 'contact_city' not in k_cols:
                    safe_alter('ALTER TABLE kooperation ADD COLUMN contact_city VARCHAR(200)')
                if 'contact_country' not in k_cols:
                    safe_alter("ALTER TABLE kooperation ADD COLUMN contact_country VARCHAR(100) DEFAULT 'Deutschland'")
                if 'vat_exempt' not in k_cols:
                    safe_alter('ALTER TABLE kooperation ADD COLUMN vat_exempt BOOLEAN DEFAULT 0')
                if 'follow_up_reminder_sent' not in k_cols:
                    safe_alter('ALTER TABLE kooperation ADD COLUMN follow_up_reminder_sent BOOLEAN DEFAULT 0')
                td_cols = [c['name'] for c in inspector.get_columns('app_todo')]
                if 'deadline' not in td_cols:
                    safe_alter('ALTER TABLE app_todo ADD COLUMN deadline DATE')
            except Exception:
                pass

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

        # ── Neue Felder: Partner, Kooperation ──────────────────────
        safe_alter('ALTER TABLE partner ADD COLUMN IF NOT EXISTS account_ids TEXT')
        safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS contact_company VARCHAR(200)')
        safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS contact_street VARCHAR(200)')
        safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS contact_city VARCHAR(200)')
        safe_alter("ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS contact_country VARCHAR(100) DEFAULT 'Deutschland'")
        safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS vat_exempt BOOLEAN DEFAULT FALSE')
        safe_alter('ALTER TABLE kooperation ADD COLUMN IF NOT EXISTS follow_up_reminder_sent BOOLEAN DEFAULT FALSE')
        safe_alter('ALTER TABLE app_todo ADD COLUMN IF NOT EXISTS deadline DATE')
        safe_alter('ALTER TABLE app_todo ADD COLUMN IF NOT EXISTS title VARCHAR(200)')
        safe_alter('ALTER TABLE team_member ADD COLUMN IF NOT EXISTS phone VARCHAR(50)')
        safe_alter('ALTER TABLE team_member ADD COLUMN IF NOT EXISTS telegram_username VARCHAR(100)')
        safe_alter('ALTER TABLE team_member ADD COLUMN IF NOT EXISTS notes TEXT')
        safe_alter("ALTER TABLE team_member ADD COLUMN IF NOT EXISTS work_status VARCHAR(20) DEFAULT 'aktiv'")
        safe_alter('ALTER TABLE team_member ADD COLUMN IF NOT EXISTS warning_count INTEGER DEFAULT 0')
        safe_alter('ALTER TABLE team_member ADD COLUMN IF NOT EXISTS tg_personal_chat_id VARCHAR(100)')
        safe_alter('ALTER TABLE knowledge_entry ADD COLUMN IF NOT EXISTS last_verified DATE')
        safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS posting_enabled BOOLEAN DEFAULT TRUE')
        safe_alter('ALTER TABLE account ADD COLUMN IF NOT EXISTS needs_stock BOOLEAN DEFAULT TRUE')
        safe_alter('ALTER TABLE missing_child_case ADD COLUMN IF NOT EXISTS update_detected BOOLEAN DEFAULT FALSE')
        safe_alter('ALTER TABLE missing_child_case ADD COLUMN IF NOT EXISTS update_source_url TEXT')
        safe_alter('ALTER TABLE missing_child_case ADD COLUMN IF NOT EXISTS update_found_at TIMESTAMP')
        safe_alter('ALTER TABLE missing_child_case ADD COLUMN IF NOT EXISTS vermisst_zeit VARCHAR(40)')

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
        _pwf_seed_default_sources()

        # ── Auto-Wetter-Stadt aus Account-Namen befüllen ─────────────
        # Alle deutschen Städte die im CityBot-Netzwerk vorkommen
        _KNOWN_CITIES = [
            'Frankfurt', 'Darmstadt', 'Mainz', 'Wiesbaden', 'Mannheim',
            'Heidelberg', 'Offenbach', 'Hanau', 'Braunschweig', 'Kaiserslautern',
            'Halle', 'Leipzig', 'Berlin', 'Hamburg', 'München', 'Köln',
            'Düsseldorf', 'Stuttgart', 'Nürnberg', 'Bremen', 'Hannover',
            'Freiburg', 'Augsburg', 'Karlsruhe', 'Bonn', 'Münster',
            'Wuppertal', 'Bielefeld', 'Bochum', 'Dortmund', 'Essen',
            'Duisburg', 'Aachen', 'Kiel', 'Lübeck', 'Erfurt', 'Rostock',
            'Kassel', 'Magdeburg', 'Saarbrücken', 'Würzburg', 'Ulm',
        ]
        try:
            _accounts_no_city = Account.query.filter(
                Account.weather_city.is_(None)
            ).all()
            for _acc in _accounts_no_city:
                _name = (_acc.name or '').lower()
                for _city in _KNOWN_CITIES:
                    if _city.lower() in _name:
                        _acc.weather_city = _city
                        break
            db.session.commit()
        except Exception:
            db.session.rollback()

        # Memes-Kategorie anlegen falls nicht vorhanden
        if not Category.query.filter_by(name='Memes').first():
            db.session.add(Category(name='Memes', color='#f59e0b', icon='face-laugh'))
            db.session.commit()

        # ── CityMeta: einmalig aus WatchlistSeite-Einträgen befüllen ─
        try:
            haben_cities = set(r[0] for r in db.session.query(WatchlistSeite.stadt).filter(
                WatchlistSeite.is_deleted == False, WatchlistSeite.haben_seite == True).all())
            geplant_cities = set(r[0] for r in db.session.query(WatchlistSeite.stadt).filter(
                WatchlistSeite.is_deleted == False, WatchlistSeite.seite_geplant == True).all())
            existing_meta = {m.stadt for m in WatchlistCityMeta.query.all()}
            for city in haben_cities | geplant_cities:
                if city not in existing_meta:
                    db.session.add(WatchlistCityMeta(
                        stadt=city,
                        haben_seite=(city in haben_cities),
                        seite_geplant=(city in geplant_cities),
                    ))
            db.session.commit()
        except Exception as _e:
            db.session.rollback()
            app.logger.warning(f'[CityMeta Migration] {_e}')

        # ── Trend Radar: einmalig trend_ig_accounts-Setting in TrendSource
        # migrieren (ersetzt die alte Komma-Liste durch echte Quellen-Zeilen).
        # Nutzt AppSettings/TrendSource direkt (nicht get_setting()/_TR_DEFAULT_
        # IG_ACCOUNTS — die sind erst viel weiter unten im Modul definiert und
        # wären an dieser Stelle im Ladeprozess noch ein NameError, siehe
        # [[project_scheduler_loadorder]]).
        try:
            if TrendSource.query.count() == 0:
                _legacy_setting = AppSettings.query.filter_by(key='trend_ig_accounts').first()
                _legacy = (_legacy_setting.value if _legacy_setting and _legacy_setting.value else
                          'tagesschau,zdfheute,derspiegel,sportschau,br24,hessenschau,ndr,swr3online,wdraktuell')
                for handle in [a.strip().lstrip('@') for a in _legacy.split(',') if a.strip()]:
                    db.session.add(TrendSource(platform='instagram', niche='News',
                                               handle=handle, scan_interval_hours=3))
                db.session.commit()
        except Exception as _e:
            db.session.rollback()
            app.logger.warning(f'[TrendSource Migration] {_e}')

        # ── Changelog-Seed: initiale Einträge ────────────────────────
        try:
            if not get_changelog():
                add_changelog_entry(
                    'Rechnung-Fix + To-Do Tab + Changelog',
                    [
                        'Rechnung: Jinja2 ns.__setattr__ Bug behoben (500-Fehler weg)',
                        'Rechnung: koop_type & currency Migrations für PostgreSQL ergänzt',
                        'To-Do / Ideen Tab mit CRUD (Kategorien, Priorität, Abgehaken)',
                        'Changelog-System: rote Badge auf "Heute" bei neuen Updates',
                        'Content Studio Sidebar: ein Eintrag statt N Sub-Links',
                    ],
                    'feature'
                )
        except Exception:
            pass

init_db()


# ─────────────────────── ALERT ENGINE ───────────────────────

_tg_alert_cache = set()  # verhindert doppelte Alerts in einer Session

def _maybe_send_low_stock_alert(account_name, stock_days):
    key = f'{account_name}:{round(stock_days, 0)}'
    if key in _tg_alert_cache:
        return
    try:
        ns = NotificationSettings.query.first()
        threshold = (ns.low_stock_days if ns else None) or 3
        if stock_days <= threshold:
            _send_central_alert(
                f'⚠️ <b>Low-Stock: {account_name}</b>\nNur noch {stock_days:.1f} Tage Vorrat'
            )
            _tg_alert_cache.add(key)
    except Exception as e:
        app.logger.error(f'Low-Stock Alert Fehler: {e}')


def _send_central_alert(message: str):
    """Sendet eine Nachricht an den zentralen Alert-Telegram-Channel, falls konfiguriert."""
    try:
        token   = get_setting('alert_telegram_token')
        chat_id = get_setting('alert_central_chat_id')
        if token and chat_id:
            _tg_send_message(token, chat_id, f'🔔 <b>ContentOS Alert</b>\n\n{message}')
    except Exception:
        pass


def generate_alerts():
    """Auto-generate system alerts based on current state."""
    # Telegram-relevante Alert-Typen: vor dem Löschen die bereits existierenden
    # Signaturen merken, damit _send_central_alert nur für WIRKLICH neue Alerts
    # feuert. Sonst geht bei dauerhaft bestehender Bedingung (z.B. Watchlist-
    # Eintrag >56 Tage auf „Kontaktiert") alle 5 Min eine Telegram-Nachricht raus.
    # In-App-SystemAlerts (DB) bleiben clear-and-regenerate — das ist unkritisch.
    _CENTRAL_ALERT_TYPES = ('follower_loss', 'watchlist_no_reply')
    _existing_central_sigs = {
        (a.account_id, a.alert_type, a.message)
        for a in SystemAlert.query.filter_by(resolved=False).filter(
            SystemAlert.alert_type.in_(_CENTRAL_ALERT_TYPES)
        ).all()
    }

    # Clear old unresolved automated alerts
    SystemAlert.query.filter_by(resolved=False).filter(
        SystemAlert.alert_type.in_(['low_stock', 'no_posts', 'empty_stock', 'overcapacity',
                                    'follower_loss', 'watchlist_no_reply', 'backup_overdue',
                                    'ai_budget'])
    ).delete()
    db.session.flush()  # sicherstellen dass deletes durch sind bevor neue eingefügt werden

    accounts = Account.query.filter_by(status='active').all()
    now = now_berlin()  # Vorrats-/Lücken-Alerts vergleichen gegen scheduled_at (Berlin-naiv)

    for acc in accounts:
        # „Kein Vorrat nötig": vollautomatisch (level ≥ 3, CityBot/Automation liefert
        # selbst) ODER täglich frische Posts (needs_stock=False) — beide ohne Vorrats-Nag.
        skip_stock = acc.automation_level >= 3 or acc.needs_stock is False

        days = acc.feed_stock_days()

        if not skip_stock:
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
                _maybe_send_low_stock_alert(acc.name, days)
                _push_notification('low_stock',
                    f'⚠️ Kritischer Vorrat: {acc.name}',
                    f'Nur noch {round(days,1)} Tage Content-Vorrat!',
                    link=f'/accounts/{acc.id}', account_id=acc.id)
            elif days < 7:
                db.session.add(SystemAlert(
                    account_id=acc.id, alert_type='low_stock', severity='warning',
                    message=f'"{acc.name}" hat nur {round(days, 1)} Tage Vorrat'
                ))
                _maybe_send_low_stock_alert(acc.name, days)
                _push_notification('low_stock',
                    f'Low Stock: {acc.name}',
                    f'{round(days,1)} Tage Vorrat verbleibend.',
                    link=f'/accounts/{acc.id}', account_id=acc.id)

        # No posts scheduled at all — nur für manuelle Accounts, und nur wenn
        # nicht bereits ein empty_stock-Alert (days==0) gesetzt wurde (sonst doppelt)
        upcoming = ScheduledPost.query.filter_by(account_id=acc.id, status='scheduled')\
            .filter(ScheduledPost.scheduled_at >= now).count()
        if upcoming == 0 and not skip_stock and days > 0:
            db.session.add(SystemAlert(
                account_id=acc.id, alert_type='no_posts', severity='warning',
                message=f'"{acc.name}" hat keine geplanten Posts'
            ))

        # Content-Gap-Alarm: kein Post in den nächsten 48h (nur manuelle Accounts)
        if not skip_stock:
            in_48h = now + timedelta(hours=48)
            gap_post = ScheduledPost.query.filter(
                ScheduledPost.account_id == acc.id,
                ScheduledPost.status == 'scheduled',
                ScheduledPost.scheduled_at >= now,
                ScheduledPost.scheduled_at <= in_48h,
            ).first()
            if not gap_post and upcoming > 0:
                _push_notification('info',
                    f'⏰ Posting-Lücke: {acc.name}',
                    'Kein Post in den nächsten 48 Stunden geplant.',
                    link=f'/accounts/{acc.id}/planer', account_id=acc.id)

        # Überkapazitäts-Prüfung: Wochen mit zu vielen geplanten Posts
        ppw = getattr(acc, 'posts_per_week', 0) or 0
        if ppw > 0:
            from collections import defaultdict as _dd
            _wc = _dd(int)
            _fps = ScheduledPost.query.filter(
                ScheduledPost.account_id == acc.id,
                ScheduledPost.scheduled_at >= now,
                ScheduledPost.status.in_(['pending', 'scheduled'])
            ).all()
            for _sp in _fps:
                _iso = _sp.scheduled_at.isocalendar()
                _wc[(_iso[0], _iso[1])] += 1
            _over = [(w, c) for w, c in _wc.items() if c > ppw]
            if _over:
                _over.sort()
                _msg = ', '.join(
                    f'KW {w[1]}/{w[0]} ({c} Posts, Limit: {ppw})'
                    for w, c in _over[:3]
                )
                db.session.add(SystemAlert(
                    account_id=acc.id, alert_type='overcapacity', severity='warning',
                    message=f'„{acc.name}" Überkapazität in {len(_over)} Woche(n): {_msg}'
                ))

    # Automation errors
    broken_rules = AutomationRule.query.filter(AutomationRule.error_count > 3, AutomationRule.active == True).all()
    for rule in broken_rules:
        db.session.add(SystemAlert(
            alert_type='bot_error', severity='critical',
            message=f'Automatisierung "{rule.name}" hat {rule.error_count} Fehler'
        ))

    # ── Follower-Verlust: Account verliert >10 Follower in einem Tag ───────
    yesterday = (now - timedelta(days=1)).date()
    for acc in accounts:
        if not acc.follower_count:
            continue
        snap = AnalyticsSnapshot.query.filter(
            AnalyticsSnapshot.account_id == acc.id,
            func.date(AnalyticsSnapshot.recorded_at) == yesterday,
        ).order_by(AnalyticsSnapshot.recorded_at.desc()).first()
        if snap and snap.followers:
            loss = snap.followers - acc.follower_count
            if loss > 10:
                msg = (f'📉 "{acc.name}" hat heute {loss} Follower verloren '
                       f'({snap.followers:,} → {acc.follower_count:,})')
                db.session.add(SystemAlert(
                    account_id=acc.id, alert_type='follower_loss', severity='warning',
                    message=msg,
                ))
                if (acc.id, 'follower_loss', msg) not in _existing_central_sigs:
                    _send_central_alert(msg)

    # ── Watchlist: Einträge seit >56 Tagen auf "Kontaktiert" ohne Antwort ──
    wl_threshold = now - timedelta(days=56)
    stale_entries = WatchlistSeite.query.filter(
        WatchlistSeite.is_deleted == False,
        WatchlistSeite.seiten_status == 'kontaktiert',
        WatchlistSeite.kontaktiert_am.isnot(None),
        WatchlistSeite.kontaktiert_am <= wl_threshold,
    ).all()
    stale_by_city = {}
    for e in stale_entries:
        stale_by_city.setdefault(e.stadt, []).append(e.ziel_name or e.handle or '?')
    for city, names in stale_by_city.items():
        n = len(names)
        msg = (f'🕐 Watchlist {city}: {n} Seite{"n" if n>1 else ""} seit >56 Tagen auf '
               f'"Kontaktiert" — wahrscheinlich keine Antwort mehr')
        db.session.add(SystemAlert(
            alert_type='watchlist_no_reply', severity='info',
            message=msg,
        ))
        if (None, 'watchlist_no_reply', msg) not in _existing_central_sigs:
            _send_central_alert(msg)

    # ── Backup-Hygiene-Reminder: ab und zu einen Vollexport machen (Schutz vor
    # versehentlichem Datenverlust / fehlerhaften Migrationen — NICHT wegen
    # DB-Ablauf, die DB läuft auf basic_256mb/bezahlt). Reine In-App-Erinnerung,
    # verschwindet nach dem nächsten Vollexport (last_full_backup_at).
    try:
        threshold_days = int(get_setting('backup_reminder_days') or 30)
        last_bk = get_setting('last_full_backup_at')
        if not last_bk:
            db.session.add(SystemAlert(
                alert_type='backup_overdue', severity='info',
                message=('🛟 Noch kein Daten-Backup erstellt. Sichere deine Daten ab und zu unter '
                         'Einstellungen → Daten → "Vollexport" (Schutz vor versehentlichem Datenverlust).'),
            ))
        else:
            days_since = (datetime.utcnow() - datetime.fromisoformat(last_bk)).days
            if days_since >= threshold_days:
                db.session.add(SystemAlert(
                    alert_type='backup_overdue', severity='info',
                    message=(f'🛟 Letztes Daten-Backup vor {days_since} Tagen — Zeit für einen '
                             f'frischen "Vollexport" unter Einstellungen → Daten.'),
                ))
    except Exception as e:
        app.logger.error('generate_alerts: Backup-Reminder-Fehler — %s', e)

    # ── KI-Budget-Alert: Monats-KI-Kosten über dem gesetzten Budget? ──
    try:
        budget = float(get_setting('ai_budget_eur') or 0)
        if budget > 0:
            _ms = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            spend = db.session.query(func.coalesce(func.sum(AiUsageLog.cost_eur), 0.0)) \
                .filter(AiUsageLog.created_at >= _ms).scalar() or 0.0
            if spend > budget:
                db.session.add(SystemAlert(
                    alert_type='ai_budget', severity='warning',
                    message=(f'🤖 KI-Budget überschritten: {spend:.2f} € diesen Monat '
                             f'(Budget {budget:.0f} €). KI-Nutzung prüfen oder Budget anpassen.')))
    except Exception as e:
        app.logger.error('generate_alerts: KI-Budget-Fehler — %s', e)

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


_last_daily_snap_date  = None  # verhindert mehrfaches Laufen pro Tag
_last_daily_sync_date  = None  # verhindert mehrfachen Follower-Sync pro Tag

_last_growth_sync_date = None


def _growth_lab_daily_sync():
    """
    Nach tägl. Follower-Sync: GrowthDataPoints für laufende Experimente automatisch anlegen
    (nur Follower — Insights werden manuell eingetragen) + Telegram-Reminder bei Experimentende.
    """
    global _last_growth_sync_date
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo('Europe/Berlin')).date()
    if _last_growth_sync_date == today:
        return
    _last_growth_sync_date = today
    try:
        with app.app_context():
            running_exps = (GrowthExperiment.query
                           .filter_by(status='running')
                           .options(
                               joinedload(GrowthExperiment.participants)
                                   .joinedload(GrowthParticipant.account),
                               joinedload(GrowthExperiment.category),
                           ).all())
            dp_count = 0
            for exp in running_exps:
                for p in exp.participants:
                    if not p.start_followers:
                        continue
                    exists = (GrowthDataPoint.query
                              .filter_by(participant_id=p.id)
                              .filter(GrowthDataPoint.recorded_at == today)
                              .first())
                    if not exists and p.account and p.account.follower_count:
                        db.session.add(GrowthDataPoint(
                            participant_id=p.id,
                            recorded_at=today,
                            followers=p.account.follower_count,
                        ))
                        dp_count += 1
                # Telegram-Reminder for experiments ending today or tomorrow
                if exp.start_date and exp.duration_days:
                    end_date  = exp.start_date + timedelta(days=exp.duration_days)
                    days_left = (end_date - today).days
                    if days_left == 0:
                        _send_central_alert(
                            f'📊 <b>Experiment endet heute!</b>\n'
                            f'<b>{exp.name}</b> ({exp.category.name})\n'
                            f'Endwerte (Profilaufrufe, erreichte Konten) in ContentOS eintragen.'
                        )
                    elif days_left == 1:
                        _send_central_alert(
                            f'📊 <b>Experiment endet morgen</b>\n'
                            f'<b>{exp.name}</b> ({exp.category.name})\n'
                            f'Bereit für die Abschlussmessung? → ContentOS → Growth Lab'
                        )
            if dp_count:
                db.session.commit()
                app.logger.info(f'[Growth Lab Sync] {dp_count} DataPoints für {today} angelegt')
    except Exception as e:
        app.logger.error(f'[Growth Lab Sync] Fehler: {e}')


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


# ═══════════════════════════════════════════════════════════════
# ─────────────────── TELEGRAM ENGINE ───────────────────────────
# ═══════════════════════════════════════════════════════════════

import requests as _requests

_TG_API = 'https://api.telegram.org/bot{token}/{method}'
_POST_ICONS = {'feed': '📸', 'reel': '🎬', 'story': '⭕', 'carousel': '🎠'}


def _tg_call(token, method, **kwargs):
    """POST to Telegram Bot API. kwargs werden als JSON-Body oder multipart gesendet."""
    url = _TG_API.format(token=token, method=method)
    try:
        resp = _requests.post(url, timeout=30, **kwargs)
        data = resp.json()
        if not data.get('ok'):
            app.logger.warning('Telegram %s error: %s', method, data.get('description'))
        return data
    except Exception as e:
        app.logger.error('Telegram %s exception: %s', method, e)
        return {'ok': False}


def _tg_send_message(token, chat_id, text):
    return _tg_call(token, 'sendMessage', json={
        'chat_id': chat_id, 'text': text[:4096], 'parse_mode': 'HTML'
    })


def _tg_media_source(media_item):
    """Gibt (url_or_None, filepath_or_None) zurück."""
    if media_item.url:
        return media_item.url, None
    fpath = os.path.join(app.config['UPLOAD_FOLDER'], media_item.filename)
    if os.path.exists(fpath):
        return None, fpath
    return None, None


def _tg_send_photo(token, chat_id, media_item, caption=None):
    url, fpath = _tg_media_source(media_item)
    cap = (caption or '')[:1024] or None
    if url:
        return _tg_call(token, 'sendPhoto', json={
            'chat_id': chat_id, 'photo': url,
            **({'caption': cap} if cap else {})
        })
    elif fpath:
        with open(fpath, 'rb') as f:
            data = {'chat_id': chat_id, **({'caption': cap} if cap else {})}
            return _tg_call(token, 'sendPhoto', data=data, files={'photo': f})
    return {'ok': False}


def _tg_send_video(token, chat_id, media_item, caption=None):
    url, fpath = _tg_media_source(media_item)
    cap = (caption or '')[:1024] or None
    if url:
        return _tg_call(token, 'sendVideo', json={
            'chat_id': chat_id, 'video': url,
            **({'caption': cap} if cap else {})
        })
    elif fpath:
        with open(fpath, 'rb') as f:
            data = {'chat_id': chat_id, **({'caption': cap} if cap else {})}
            return _tg_call(token, 'sendVideo', data=data, files={'video': f})
    return {'ok': False}


def _tg_send_media_group(token, chat_id, media_items, caption=None):
    """Carousel: bis zu 10 Bilder als Gruppe senden. Caption nur beim ersten."""
    items = [mi for mi in media_items if mi][:10]
    if not items:
        return {'ok': False}

    cap = (caption or '')[:1024] or None

    # Wenn alle URLs verfügbar → reiner JSON-Call
    if all(mi.url for mi in items):
        media_json = []
        for i, mi in enumerate(items):
            obj = {'type': 'photo', 'media': mi.url}
            if i == 0 and cap:
                obj['caption'] = cap
            media_json.append(obj)
        return _tg_call(token, 'sendMediaGroup', json={'chat_id': chat_id, 'media': media_json})

    # Lokale Dateien: multipart
    files = {}
    media_json = []
    for i, mi in enumerate(items):
        url, fpath = _tg_media_source(mi)
        if url:
            ref = url
        elif fpath:
            attach_key = f'file{i}'
            files[attach_key] = (os.path.basename(fpath),
                                 open(fpath, 'rb'),
                                 'image/jpeg')
            ref = f'attach://{attach_key}'
        else:
            continue
        obj = {'type': 'photo', 'media': ref}
        if i == 0 and cap:
            obj['caption'] = cap
        media_json.append(obj)

    result = _tg_call(token, 'sendMediaGroup',
                      data={'chat_id': chat_id, 'media': json.dumps(media_json)},
                      files=files)
    for fobj in files.values():
        try: fobj[1].close()
        except: pass
    return result


def _tg_send_document(token, chat_id, media_item, caption=None):
    """Sendet eine Datei als Dokument (volle Qualität, ohne Komprimierung)."""
    url, fpath = _tg_media_source(media_item)
    cap = (caption or '')[:1024] or None
    if url:
        return _tg_call(token, 'sendDocument', json={
            'chat_id': chat_id, 'document': url,
            **({'caption': cap} if cap else {})
        })
    elif fpath:
        with open(fpath, 'rb') as f:
            data = {'chat_id': chat_id, **({'caption': cap} if cap else {})}
            return _tg_call(token, 'sendDocument', data=data,
                            files={'document': (os.path.basename(fpath), f, 'image/jpeg')})
    return {'ok': False}


def _tg_send_action_keyboard(token, chat_id, post_id):
    """Sendet die Aktion-Buttons: Gepostet ✓ / Fehler melden."""
    keyboard = {
        'inline_keyboard': [[
            {'text': '✅  Gepostet auf Instagram', 'callback_data': f'posted_{post_id}'},
            {'text': '⚠️  Fehler melden',          'callback_data': f'error_{post_id}'},
        ]]
    }
    return _tg_call(token, 'sendMessage', json={
        'chat_id': chat_id,
        'text': '👆 Bitte nach dem Posten bestätigen:',
        'reply_markup': keyboard,
        'parse_mode': 'HTML',
    })


def _tg_answer_callback(token, callback_query_id, text, alert=False):
    """Beantwortet einen Callback-Query (Toast / Popup in Telegram)."""
    try:
        _tg_call(token, 'answerCallbackQuery', json={
            'callback_query_id': callback_query_id,
            'text': text,
            'show_alert': alert,
        })
    except Exception:
        pass


def _tg_edit_message_text(token, chat_id, message_id, text):
    """Bearbeitet den Text einer bestehenden Nachricht."""
    try:
        _tg_call(token, 'editMessageText', json={
            'chat_id': chat_id, 'message_id': message_id,
            'text': text, 'parse_mode': 'HTML',
        })
    except Exception:
        pass


def send_telegram_post(post, account=None, token=None):
    """Sendet einen ScheduledPost an den Telegram-Channel als 3-teilige Karte:
    1) Bild(er) als Dokument (volle Auflösung)
    2) Caption als plain text (zum Kopieren)
    3) Aktions-Buttons: Gepostet / Fehler melden
    Gibt True zurück wenn erfolgreich."""
    if account is None:
        account = post.account
    if not account or not account.telegram_chat_id:
        return False
    if token is None:
        token = get_setting('telegram_bot_token')
    if not token:
        app.logger.warning('Telegram: kein Bot-Token konfiguriert')
        return False

    chat_id = account.telegram_chat_id
    icon    = _POST_ICONS.get(post.post_type, '📌')
    date_str = post.scheduled_at.strftime('%d.%m.%Y')
    type_label = {'feed': 'Feed-Post', 'reel': 'Reel', 'story': 'Story', 'carousel': 'Karussell'}.get(post.post_type, post.post_type)

    # ── 1. Header + Bild(er) senden ────────────────────────────
    media_ids_list = post.get_media_ids()
    primary_media  = MediaItem.query.get(post.media_item_id) if post.media_item_id else None
    if not primary_media and media_ids_list:
        primary_media = MediaItem.query.get(media_ids_list[0])

    header_text = (
        f'{icon} <b>{account.name}</b> — {type_label}\n'
        f'📅 Posting-Datum: <b>{date_str}</b>\n'
        f'🆔 Post-ID: #{post.id}'
    )

    ok = False

    if post.post_type == 'reel' and primary_media:
        # Reels als Video
        result = _tg_send_video(token, chat_id, primary_media, header_text[:1024])
        ok = result.get('ok', False)

    elif len(media_ids_list) > 1:
        # Karussell: zuerst Header als Text, dann alle Bilder als Dokumente
        _tg_send_message(token, chat_id, header_text)
        all_media = MediaItem.query.filter(MediaItem.id.in_(media_ids_list)).all()
        id_order  = {mid: i for i, mid in enumerate(media_ids_list)}
        all_media.sort(key=lambda m: id_order.get(m.id, 999))
        ok = True
        for mi in all_media:
            r = _tg_send_document(token, chat_id, mi)
            if not r.get('ok'):
                ok = False

    elif primary_media:
        # Einzelbild als Dokument (volle Qualität)
        result = _tg_send_document(token, chat_id, primary_media, header_text[:1024])
        ok = result.get('ok', False)
        if not ok:
            # Fallback: als Foto
            result = _tg_send_photo(token, chat_id, primary_media, header_text[:1024])
            ok = result.get('ok', False)

    else:
        # Kein Bild → nur Text
        result = _tg_send_message(token, chat_id, header_text)
        ok = result.get('ok', False)

    if not ok:
        return False

    # ── 2. Caption: erst Label, dann Caption ALLEIN (direkt kopierbar) ─
    if post.caption:
        _tg_send_message(token, chat_id, '📋 <b>Caption</b> — Nachricht darunter komplett kopieren:')
        # Reine Caption ohne irgendetwas drumherum → Langdruck → "Text kopieren"
        _tg_call(token, 'sendMessage', json={
            'chat_id': chat_id,
            'text': post.caption[:4096],
        })

    # ── 3. Aktions-Buttons senden ──────────────────────────────
    _tg_send_action_keyboard(token, chat_id, post.id)

    return True


def _send_due_telegram_posts():
    """Prüft jede Minute ob Posts fällig sind und sendet sie an Telegram."""
    with app.app_context():
        try:
            _row = AppSettings.query.filter_by(key='telegram_bot_token').first()
            token = _row.value if _row else None
            if not token:
                return
            # Fälligkeit gegen scheduled_at prüfen (Berlin-naiv gespeichert!) — sonst
            # würde ein für 18:00 Berlin geplanter Post erst um 18:00 UTC = 19/20:00
            # Berlin feuern (1–2 h zu spät).
            now = now_berlin()
            sent_at = datetime.utcnow()  # Audit-Timestamp bleibt UTC (App-Konvention)
            # Nur Accounts mit aktivem Vorrat-/Posting-Toggle (posting_enabled != False)
            due = ScheduledPost.query.join(Account, ScheduledPost.account_id == Account.id).filter(
                ScheduledPost.scheduled_at <= now,
                ScheduledPost.status == 'scheduled',
                ScheduledPost.slot_type != 'disabled',
                ScheduledPost.telegram_sent_at == None,
                Account.posting_enabled.isnot(False),
            ).options(joinedload(ScheduledPost.account)).all()

            sent = 0
            for post in due:
                if send_telegram_post(post, token=token):
                    post.telegram_sent_at = sent_at
                    sent += 1
            if sent:
                db.session.commit()
                app.logger.info('Telegram: %d Post(s) gesendet', sent)
        except Exception as e:
            app.logger.error('_send_due_telegram_posts Fehler: %s', e)
            try: db.session.rollback()
            except: pass


# ─────────────────── WETTER-SYSTEM ────────────────────────────

# Trigger-Konfiguration: Cooldown-Tage + Beschreibung
WEATHER_TRIGGERS = {
    'weather_hot':    {'cooldown': 14, 'label': '🌡️ Hitzewelle (>33°C)'},
    'weather_storm':  {'cooldown':  7, 'label': '🌩️ Unwetter'},
    'weather_snow':   {'cooldown': 30, 'label': '❄️ Erster Schnee'},
    'weather_spring': {'cooldown': 21, 'label': '🌸 Frühlings-Opening'},
    'weather_frost':  {'cooldown': 14, 'label': '🌫️ Extremfrost (<-10°C)'},
}

# Maximale Wetter-Posts pro Woche (globale Sperre)
WEATHER_MAX_PER_WEEK = 1


_KNOWN_CITIES_WEATHER = [
    'Frankfurt', 'Darmstadt', 'Mainz', 'Wiesbaden', 'Mannheim',
    'Heidelberg', 'Offenbach', 'Hanau', 'Braunschweig', 'Kaiserslautern',
    'Halle', 'Leipzig', 'Berlin', 'Hamburg', 'München', 'Köln',
    'Düsseldorf', 'Stuttgart', 'Nürnberg', 'Bremen', 'Hannover',
    'Freiburg', 'Augsburg', 'Karlsruhe', 'Bonn', 'Münster',
    'Wuppertal', 'Bielefeld', 'Bochum', 'Dortmund', 'Essen',
    'Duisburg', 'Aachen', 'Kiel', 'Lübeck', 'Erfurt', 'Rostock',
    'Kassel', 'Magdeburg', 'Saarbrücken', 'Würzburg', 'Ulm',
]


def _get_weather_city(account):
    """Gibt den Stadtnamen für die Wetter-API zurück.
    Reihenfolge: gesetztes Feld → Stadtname im Account-Namen → erstes Wort."""
    if account.weather_city:
        return account.weather_city.strip()
    name = (account.name or '').lower()
    # Bekannte Stadt im Namen suchen
    for city in _KNOWN_CITIES_WEATHER:
        if city.lower() in name:
            return city
    # Letzter Fallback: erstes Wort
    parts = (account.name or '').strip().split()
    return parts[0] if parts else None


def _classify_post_folder(post, folders, api_key):
    """Kern-Logik: analysiert einen InspirationPost via Claude Vision und gibt
    das beste Folder-Match zurück: {'folder_id': int, 'confidence': float, ...}
    Wird von suggest-folder-Endpoint UND vom Batch-Prozess genutzt.
    """
    import base64 as _b64
    import requests as _req
    import anthropic as _ant
    import re as _re

    caption_text = (post.caption or '').strip()[:600]
    folder_lines = [
        f'  - ID {f.id}: "{f.name}"' + (f' — {(f.notes or "").strip()}' if f.notes else '')
        for f in folders
    ]

    prompt_text = f"""Analysiere diesen deutschen Instagram-Post und ordne ihn dem passenden Inhaltskategorie-Ordner zu.

VERFÜGBARE ORDNER:
{chr(10).join(folder_lines)}

POST-CAPTION: "{caption_text or '(keine Caption)'}"

INHALTSKATEGORIEN — Erkennungsmerkmale (zur Orientierung, ordne in die ORDNER oben ein):
• Starterpacks      → Collage aus mehreren Bildern/Symbolen, Text "Der/Die Starter Pack für...", typische Klischees
• Wetter            → Wetterextreme (Hitze, Schnee, Sturm, Gewitter), Wetter-Screenshots, Thermometer
• Events            → Konzerte, Festivals, Stadtfeste, Volksfeste, Messen, Veranstaltungs-Flyer, Bühnen
• Weihnachten       → Weihnachtsmarkt, Advent, Christbaum, Geschenke, Nikolaus, Glühwein, Krippe
• Silvester/Neujahr → Feuerwerk, Raketen, "Frohes neues Jahr", Sektflöten, Countdown
• Frühling          → Kirschblüte, Ostern, erste Sonne, Frühlingsblumen, "endlich Frühling"
• Sommer            → Freibad, Hitzewelle, Eis, Grillen, See/Strand, Sonnenbad
• Herbst            → Blätterfärben, Oktoberfest, Ernte, Kürbis, Nebel, "Herbststimmung"
• Winter            → Schnee, Eislaufen, heiße Schokolade, Frost, Winterlandschaft
• Stadtleben/Memes  → Alltagssituationen, Erkennungszeichen der Stadt, "typisch [Stadt]", lokale Klischees
• Essen & Trinken   → Restaurants, Gerichte, Streetfood, Cafés, lokale Spezialitäten
• Sport/Fußball     → Stadion, Trikots, Spieler, Sportereignisse, Vereinslogo
• Nostalgie         → Alte Fotos, Throwback, "früher war...", historische Bilder, Vergleich alt/neu
• Natur             → Parks, Flüsse, Wälder, Naturlandschaften, Sonnenuntergang
• Humor & Memes     → Witzbilder, Reaktionsbilder, Textmemes, absurde Situationen

Erkenne anhand von: Bild-Motive, Text auf dem Bild (OCR), Caption-Text, Hashtags, Emojis.

Antworte NUR mit diesem JSON (kein anderer Text):
{{"folder_id": <Zahl oder null>, "folder_name": "<Name>", "detected_type": "<erkannter Typ>", "confidence": <0.0-1.0>, "reason": "<1-2 Sätze auf Deutsch>"}}"""

    # Bild laden
    img_b64, img_mtype = None, 'image/jpeg'
    if post.thumbnail_url:
        try:
            _r = _req.get(post.thumbnail_url, timeout=(6, 18),
                          headers={'User-Agent': 'Mozilla/5.0',
                                    'Referer': 'https://www.instagram.com/'})
            if _r.ok:
                img_mtype = _r.headers.get('content-type', 'image/jpeg').split(';')[0].strip()
                if img_mtype not in ('image/jpeg', 'image/png', 'image/gif', 'image/webp'):
                    img_mtype = 'image/jpeg'
                img_b64 = _b64.standard_b64encode(_r.content).decode()
        except Exception:
            pass

    client  = _ant.Anthropic(api_key=api_key)
    content = ([{'type': 'image', 'source': {'type': 'base64',
                                              'media_type': img_mtype,
                                              'data': img_b64}},
                {'type': 'text', 'text': prompt_text}]
               if img_b64 else prompt_text)
    resp = client.messages.create(
        model='claude-haiku-4-5', max_tokens=300,
        system='Du bist ein Content-Klassifizierer für deutsche Instagram-Seiten. Antworte AUSSCHLIESSLICH mit dem JSON-Objekt.',
        messages=[{'role': 'user', 'content': content}]
    )
    _log_ai('kategorisierung', resp)
    raw = resp.content[0].text.strip()
    if '```' in raw:
        _m = _re.search(r'```(?:json)?\s*([\s\S]+?)\s*```', raw)
        raw = _m.group(1) if _m else raw
    result = json.loads(raw)

    folder_id = result.get('folder_id')
    allowed   = {f.id for f in folders}
    if folder_id and int(folder_id) not in allowed:
        folder_id = None

    return {
        'folder_id':     int(folder_id) if folder_id else None,
        'folder_name':   result.get('folder_name', ''),
        'detected_type': result.get('detected_type', ''),
        'confidence':    round(float(result.get('confidence', 0)), 2),
        'reason':        result.get('reason', ''),
        'image_analyzed': img_b64 is not None,
    }


def _auto_classify_batch():
    """Klassifiziert neue InspirationPosts via KI — läuft im Hintergrund.
    Verarbeitet max. 20 Posts pro Durchlauf. Überspringt:
    - Posts mit status != 'new' (bereits in Vorrat oder ignoriert)
    - Posts mit folder_locked=True (manuell kategorisiert)
    - Posts die bereits einen suggested_folder_id haben
    """
    with app.app_context():
        # Einstellung prüfen
        setting = get_setting('auto_classify_inspirationen')
        if setting != 'true':
            return

        api_key = os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')
        if not api_key:
            return

        # Nur frische, unkategorisierte, nicht-gesperrte Posts
        posts = InspirationPost.query.filter(
            InspirationPost.status == 'new',
            InspirationPost.folder_locked == False,
            InspirationPost.suggested_folder_id.is_(None),
        ).order_by(InspirationPost.created_at.desc()).limit(20).all()

        if not posts:
            return

        # Alle Ordner vorladen (nach Account gruppiert)
        from collections import defaultdict as _dd
        all_folders   = ContentFolder.query.all()
        global_folders = [f for f in all_folders if f.account_id is None]
        acc_folders    = _dd(list)
        for f in all_folders:
            if f.account_id:
                acc_folders[f.account_id].append(f)

        for post in posts:
            try:
                source     = db.session.get(InspirationSource, post.source_id)
                account_id = source.account_id if source else None
                folders    = (acc_folders.get(account_id, []) + global_folders) if account_id else global_folders
                if not folders:
                    continue

                result = _classify_post_folder(post, folders, api_key)
                if result and result.get('folder_id'):
                    post.suggested_folder_id = result['folder_id']
                    db.session.commit()
                    app.logger.debug(f'[AutoClassify] Post {post.id} → {result["folder_name"]} ({result["confidence"]})')
            except Exception as e:
                app.logger.debug(f'[AutoClassify] Post {post.id} Fehler: {e}')
                db.session.rollback()


def _check_all_weather():
    """Hauptfunktion: prüft Wetter für alle aktiven Accounts — wird 4× täglich aufgerufen."""
    import requests as _req

    api_key = os.environ.get('OPENWEATHERMAP_API_KEY', '')
    if not api_key:
        return

    with app.app_context():
        accounts = Account.query.filter_by(status='active').all()
        # Nur Stadt-Meme Accounts (weather_city gesetzt ODER Name enthält erkennbare Stadt)
        for acc in accounts:
            city = _get_weather_city(acc)
            if not city:
                continue
            try:
                _process_weather_account(acc, city, api_key)
            except Exception as e:
                app.logger.debug(f'[Weather] Fehler für {acc.name}: {e}')
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


def _process_weather_account(account, city, api_key):
    """Wetter für einen Account holen und Trigger auslösen."""
    import requests as _req, json as _js

    now = datetime.utcnow()

    # ── Cache prüfen: kein neuer Call wenn < 6h alt ──────────────
    cache = WeatherCache.query.filter_by(account_id=account.id).first()
    if cache and (now - cache.checked_at).total_seconds() < 6 * 3600:
        try:
            forecast = _js.loads(cache.forecast_json or '{}')
        except Exception:
            forecast = {}
    else:
        # ── API-Call: 5-Tage-Forecast (1 Call) ───────────────────
        url = (f'https://api.openweathermap.org/data/2.5/forecast'
               f'?q={city},DE&appid={api_key}&units=metric&lang=de&cnt=16')
        try:
            resp = _req.get(url, timeout=10)
        except Exception as e:
            app.logger.debug(f'[Weather] API-Timeout für {city}: {e}')
            return
        if resp.status_code != 200:
            app.logger.debug(f'[Weather] API-Fehler {resp.status_code} für {city}')
            return
        forecast = resp.json()

        if 'list' not in forecast or not forecast['list']:
            return

        cur = forecast['list'][0]
        if not cache:
            cache = WeatherCache(account_id=account.id)
            db.session.add(cache)
        cache.city_name    = city
        cache.temperature  = cur['main']['temp']
        cache.weather_code = cur['weather'][0]['id']
        cache.wind_speed   = cur['wind']['speed']
        cache.description  = cur['weather'][0]['description']
        cache.forecast_json = _js.dumps(forecast)
        cache.checked_at   = now
        db.session.flush()

    if 'list' not in forecast:
        return

    cur   = forecast['list'][0]
    temp  = cur['main']['temp']
    code  = cur['weather'][0]['id']
    month = now.month

    # ── Globale Wochenbremse: max. 1 Wetter-Post/Woche ───────────
    week_start = now - timedelta(days=now.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    weather_this_week = WeatherTriggerLog.query.filter(
        WeatherTriggerLog.account_id == account.id,
        WeatherTriggerLog.fired_at   >= week_start,
    ).count()
    if weather_this_week >= WEATHER_MAX_PER_WEEK:
        return  # Diese Woche wurde schon gepostet

    # ── Aktive Trigger bestimmen ──────────────────────────────────
    active = []
    if temp >= 33:
        active.append('weather_hot')
    if 200 <= code <= 232:          # Gewitter
        active.append('weather_storm')
    if 600 <= code <= 622:          # Schnee
        active.append('weather_snow')
    if month in [2, 3, 4] and temp >= 18:   # Frühling
        active.append('weather_spring')
    if temp <= -10:                 # Extremfrost
        active.append('weather_frost')

    # Priorität: storm > snow > frost > hot > spring
    priority_order = ['weather_storm', 'weather_snow', 'weather_frost',
                      'weather_hot', 'weather_spring']
    active = [t for t in priority_order if t in active]

    for trigger in active:
        fired = _maybe_fire_weather(account, trigger, temp, city)
        if fired:
            break  # Pro Prüfung nur 1 Trigger feuern


def _maybe_fire_weather(account, trigger, temp, city):
    """Prüft Cooldown, sucht Content und plant Post ein. Gibt True zurück wenn gefeuert."""
    now          = datetime.utcnow()
    cooldown_days = WEATHER_TRIGGERS[trigger]['cooldown']

    # ── Cooldown-Prüfung ─────────────────────────────────────────
    last = WeatherTriggerLog.query.filter_by(
        account_id=account.id, trigger_type=trigger
    ).order_by(WeatherTriggerLog.fired_at.desc()).first()

    if last and (now - last.fired_at).days < cooldown_days:
        return False

    # ── Ordner mit diesem Trigger suchen ─────────────────────────
    folder = ContentFolder.query.filter_by(
        trigger_condition=trigger, account_id=account.id
    ).first()
    if not folder:
        # Globaler Fallback-Ordner
        folder = ContentFolder.query.filter_by(
            trigger_condition=trigger, account_id=None
        ).first()
    if not folder:
        return False  # Kein Ordner konfiguriert → nichts zu tun

    # ── Zufälligen Content aus dem Ordner holen ──────────────────
    item = ContentItem.query.filter(
        ContentItem.folder_id == folder.id,
        ContentItem.status.in_(['draft', 'ready']),
        ContentItem.accounts.any(id=account.id)
    ).order_by(db.func.random()).first()

    if not item:
        app.logger.info(f'[Weather] Kein Content für {trigger} / {account.name}')
        return False

    # ── Nächsten freien Slot finden (18:00 Uhr, heute oder morgen) ─
    post_time = now.replace(hour=18, minute=0, second=0, microsecond=0)
    if post_time <= now:
        post_time += timedelta(days=1)
    # Slot belegt? → +1 Tag
    if ScheduledPost.query.filter_by(
        account_id=account.id, scheduled_at=post_time
    ).filter(ScheduledPost.status.in_(['pending', 'scheduled'])).first():
        post_time += timedelta(days=1)

    # ── Scheduled Post anlegen ────────────────────────────────────
    media = item.media_items[0] if item.media_items else None
    sp = ScheduledPost(
        account_id      = account.id,
        content_item_id = item.id,
        media_item_id   = media.id if media else None,
        caption         = item.caption or '',
        scheduled_at    = post_time,
        status          = 'pending',
        post_type       = 'feed',
    )
    db.session.add(sp)
    item.status = 'scheduled'
    db.session.flush()

    # ── Trigger-Log ───────────────────────────────────────────────
    db.session.add(WeatherTriggerLog(
        account_id   = account.id,
        trigger_type = trigger,
        fired_at     = now,
        post_id      = sp.id,
        city_name    = city,
        temperature  = temp,
    ))

    label = WEATHER_TRIGGERS[trigger]['label']
    app.logger.info(f'[Weather] ✓ {label} → {account.name} → Post {post_time:%d.%m %H:%M}')

    # SystemAlert für Dashboard
    db.session.add(SystemAlert(
        account_id = account.id,
        alert_type = 'weather_post',
        severity   = 'info',
        message    = f'{label} erkannt für „{account.name}" — Wetter-Post {post_time:%d.%m.%Y} eingeplant',
    ))
    return True


def _ensure_telegram_webhook():
    """Registriert den Telegram-Webhook beim App-Start automatisch — mit
    secret_token, damit die Härtung ohne manuellen Klick aktiv wird.

    Läuft nur, wenn ein Bot-Token gesetzt ist UND noch kein Secret existiert
    (danach kein erneutes Registrieren → kein Churn). Im Background-Thread gibt
    es keinen request.host, daher wird die Basis-URL aus app_base_url bzw.
    RENDER_EXTERNAL_URL abgeleitet. Muss innerhalb eines app_context laufen.
    MUSS vor dem Thread-Start definiert sein (sonst NameError beim ersten Tick)."""
    token = get_setting('telegram_bot_token')
    if not token or get_setting('telegram_webhook_secret'):
        return
    base_url = get_setting('app_base_url') or os.environ.get('RENDER_EXTERNAL_URL')
    if not base_url:
        return
    base_url = base_url.rstrip('/')
    import secrets as _secrets
    secret = _secrets.token_urlsafe(32)
    import requests as _r
    res = _r.post(
        f'https://api.telegram.org/bot{token}/setWebhook',
        json={'url': f'{base_url}/api/telegram/bot-webhook', 'secret_token': secret},
        timeout=10,
    ).json()
    if res.get('ok'):
        set_setting('telegram_webhook_secret', secret)
        db.session.commit()
        app.logger.info('Telegram-Webhook automatisch registriert (gehärtet).')
    else:
        app.logger.error('Telegram setWebhook (Auto) fehlgeschlagen: %s', res.get('description'))


# Heartbeat des Hintergrund-Schedulers (für /status). Dict-Mutation = kein global nötig.
_sched_health = {'last_tick': None}
_last_mcf_research_at = None  # Missing-Children Auto-Recherche: letzter Lauf

def schedule_automations():
    """Background thread that runs automation rules and housekeeping."""
    tick = 0
    while True:
        try:
            with app.app_context():
                now = datetime.utcnow()
                _sched_health['last_tick'] = now  # Heartbeat für /status

                # ── Einmalig kurz nach dem Start: Telegram-Webhook registrieren ──
                # tick==1 statt 0: Der Thread startet, BEVOR das Modul fertig geladen
                # ist — get_setting/set_setting werden erst nach dem Thread-Start
                # definiert. Bei tick 0 (sofort beim Boot) → NameError; tick 1
                # (~60 s später) läuft garantiert nach vollständigem Modul-Laden.
                if tick == 1:
                    try:
                        _ensure_telegram_webhook()
                    except Exception as _e:
                        app.logger.error('Webhook-Auto-Registrierung: %s', _e)

                # ── Missing Children: Auto-Recherche (nur wenn aktiviert) ──
                if tick >= 2 and get_setting('mcf_auto_research') == '1':
                    global _last_mcf_research_at
                    try:
                        _iv = int(get_setting('mcf_research_interval_min') or 120)
                    except Exception:
                        _iv = 120
                    if _last_mcf_research_at is None or (now - _last_mcf_research_at).total_seconds() >= _iv * 60:
                        _last_mcf_research_at = now
                        try:
                            _mcf_run_research()
                            _mcf_monitor_updates()
                        except Exception as _e:
                            app.logger.error('MCF Auto-Recherche/Monitoring: %s', _e)

                # ── Täglicher Follower-Sync + Snapshot um 23:55 Berliner Zeit ──
                from zoneinfo import ZoneInfo
                global _last_daily_sync_date
                now_berlin = datetime.now(ZoneInfo('Europe/Berlin'))
                today_berlin = now_berlin.date()
                if now_berlin.hour == 23 and now_berlin.minute >= 55:
                    _daily_follower_snapshot()
                    auto_sync_row = AppSettings.query.filter_by(key='ig_auto_sync').first()
                    auto_sync_on  = (not auto_sync_row) or (auto_sync_row.value != '0')
                    if (auto_sync_on and not _ig_sync_status['running']
                            and _last_daily_sync_date != today_berlin):
                        _last_daily_sync_date = today_berlin
                        _ig_sync_status.update({'running': True, 'error': None,
                                                'result': None, 'progress': 0, 'current': ''})
                        threading.Thread(target=_run_ig_follower_sync, daemon=True).start()

                # ── Telegram: fällige Posts senden ───────────────────────
                _send_due_telegram_posts()

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
            # Wetter-Check 4× täglich (alle 360 Ticks = 6 Stunden)
            if tick % 360 == 0:
                threading.Thread(target=_check_all_weather, daemon=True).start()
                threading.Thread(target=_check_koop_reminders, daemon=True).start()
            # KI-Auto-Klassifizierung alle 10 Minuten
            if tick % 10 == 0:
                threading.Thread(target=_auto_classify_batch, daemon=True).start()
            # Smart-Refill alle 30 Minuten
            if tick % 30 == 0:
                threading.Thread(target=_smart_refill_check, daemon=True).start()
            # Trend Radar: alle 3 Stunden scannen (Start bei tick 3, nie tick 0 —
            # get_setting existiert beim Thread-Start noch nicht, s. Webhook-Kommentar)
            if tick >= 3 and (tick - 3) % 180 == 0:
                threading.Thread(target=_tr_auto_scan, daemon=True).start()
            # Product Alert Factory: alle 30 Minuten scannen (nie tick 0, s.o.)
            if tick >= 2 and get_setting('pwf_auto_research') == '1':
                if (tick - 2) % 30 == 0:
                    threading.Thread(target=_pwf_auto_scan, daemon=True).start()
            # Product Alert Factory: einmal täglich vergessene Entwürfe mit
            # längst abgelaufenem MHD automatisch archivieren
            if tick >= 5 and (tick - 5) % 1440 == 0:
                threading.Thread(target=_pwf_auto_archive_scheduled, daemon=True).start()
            # Content-Serien stündlich planen
            if tick % 60 == 0:
                threading.Thread(target=_process_series, daemon=True).start()
            # Schicht 4: Stündliches DB-Backup (lokal + iCloud)
            if tick % 60 == 0:
                threading.Thread(
                    target=lambda: _do_backup('hourly'), daemon=True).start()

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
    """Liest einen Wert aus AppSettings. Gibt default zurück wenn nicht gesetzt.
    Muss innerhalb eines App- oder Request-Kontexts aufgerufen werden."""
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


def _last_synced_date():
    """UTC-Datum des letzten echten Follower-Syncs durch den Bot (None = nie).
    Analytics-Charts enden an diesem Tag — kein verfälschter „heute"-Punkt mit
    noch nicht aktualisierten Followern."""
    v = get_setting('last_follower_sync_at')
    if v:
        try:
            return datetime.fromisoformat(v).date()
        except Exception:
            pass
    return None


def _chart_last_day(today):
    """Letzter darstellbarer Tag: bis zum letzten Sync. Ohne Sync-Marker bis
    gestern (heute erst zeigen, wenn der Bot die Follower aktualisiert hat)."""
    cutoff = _last_synced_date()
    if cutoff is None:
        cutoff = today - timedelta(days=1)
    return min(today, cutoff)


def get_changelog():
    """Gibt alle Changelog-Einträge aus AppSettings zurück."""
    raw = get_setting('changelog', '[]')
    try:
        return json.loads(raw)
    except Exception:
        return []

def add_changelog_entry(title, items, entry_type='feature'):
    """Fügt einen neuen Eintrag zum Changelog hinzu (max 30). Keine DB-Commit nötig."""
    import uuid
    entries = get_changelog()
    entries.insert(0, {
        'id': str(uuid.uuid4())[:8],
        'title': title,
        'items': items,
        'type': entry_type,
        'date': datetime.utcnow().strftime('%Y-%m-%d'),
    })
    set_setting('changelog', json.dumps(entries[:30], ensure_ascii=False))
    db.session.commit()

def get_changelog_unread_count():
    """Zählt Einträge seit dem letzten Dismiss."""
    dismissed_at = get_setting('changelog_dismissed_at')
    entries = get_changelog()
    if not dismissed_at:
        return len(entries)
    from datetime import date as _date
    try:
        cutoff = dismissed_at[:10]  # YYYY-MM-DD
        return sum(1 for e in entries if e.get('date', '0') > cutoff)
    except Exception:
        return 0


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
    now = now_berlin()  # gegen scheduled_at (Berlin-naiv)
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

    today_start = now_berlin().replace(hour=0, minute=0, second=0, microsecond=0)
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

    # Korrektur: nur den letzten Snapshot pro Account für exakt den Stichtag vor 7
    # Tagen nehmen — nicht alle historischen Snapshots aufsummieren (wäre falsch)
    week_ago_date = (datetime.utcnow() - timedelta(days=7)).date()
    _latest_7d = db.session.query(
        AnalyticsSnapshot.account_id,
        func.max(AnalyticsSnapshot.recorded_at).label('latest_at')
    ).filter(
        func.date(AnalyticsSnapshot.recorded_at) == week_ago_date
    ).group_by(AnalyticsSnapshot.account_id).subquery()
    old_snap = db.session.query(func.sum(AnalyticsSnapshot.followers)).join(
        _latest_7d, db.and_(
            AnalyticsSnapshot.account_id == _latest_7d.c.account_id,
            AnalyticsSnapshot.recorded_at == _latest_7d.c.latest_at
        )
    ).scalar() or 0
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
def root_redirect():
    return redirect(url_for('heute'))


@app.route('/dashboard')
@login_required
def dashboard():
    # generate_alerts() wird jetzt vom Scheduler alle 5 Min. ausgeführt —
    # NICHT mehr bei jedem Seitenaufruf (war ~30 Extra-Queries pro Load).
    now = now_berlin()  # nur für die "posts_today"-Tagesgrenze (scheduled_at, Berlin-naiv)

    # ── 1× Accounts mit Kategorie laden (verhindert N lazy-loads im Template) ─
    all_active = Account.query.filter_by(status='active')\
        .options(joinedload(Account.category)).all()
    days_map   = _get_planned_days_batch(all_active)

    # stats nutzt die bereits geladenen Daten (keine eigenen Account-Queries)
    stats = get_dashboard_stats(active_accounts=all_active, days_map=days_map)

    # stock_summary: kein einziger DB-Aufruf (uses days_map)
    stock_summary = {'green': 0, 'yellow': 0, 'orange': 0, 'red': 0}
    for a in all_active:
        stock_summary[_days_to_status(days_map[a.id])] += 1

    # ── Top-10 Accounts für Dashboard-Card: separate Query mit eager-loaded
    # Thumbnails (scheduled_posts → content_item → media_items), um N+1 zu vermeiden
    _priority = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
    top_ids = [a.id for a in sorted(
        all_active,
        key=lambda a: (_priority.get(a.priority, 2), -(a.follower_count or 0))
    )[:10]]
    if top_ids:
        _acc_q = Account.query.filter(Account.id.in_(top_ids)).options(
            joinedload(Account.category),
            selectinload(Account.scheduled_posts)
                .joinedload(ScheduledPost.content_item)
                .selectinload(ContentItem.media_items)
        ).all()
        _order = {aid: i for i, aid in enumerate(top_ids)}
        accounts = sorted(_acc_q, key=lambda a: _order.get(a.id, 99))
    else:
        accounts = []

    recent_content = ContentItem.query.order_by(ContentItem.created_at.desc()).limit(8).all()
    alerts         = SystemAlert.query.filter_by(resolved=False)\
                         .order_by(SystemAlert.severity.desc()).limit(10).all()
    # joinedload verhindert 15 N+1 Queries für log.user
    recent_activity = ActivityLog.query\
        .options(joinedload(ActivityLog.user))\
        .order_by(ActivityLog.created_at.desc()).limit(15).all()

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    posts_today = ScheduledPost.query.filter(
        ScheduledPost.scheduled_at >= today_start,
        ScheduledPost.scheduled_at < today_start + timedelta(days=1),
        ScheduledPost.status.in_(['scheduled', 'published'])
    ).order_by(ScheduledPost.scheduled_at).all()
    # posts_today-Count aus der bereits gefetchten Liste (kein zweiter COUNT-Query)
    stats['posts_today'] = len(posts_today)

    categories = Category.query.order_by(Category.name).all()

    auto_classify_on = get_setting('auto_classify_inspirationen') == 'true'
    _total_new   = InspirationPost.query.filter_by(status='new').count()
    _classified  = InspirationPost.query.filter(
        InspirationPost.status == 'new',
        InspirationPost.suggested_folder_id.isnot(None)
    ).count()
    classify_stats = {'total_new': _total_new, 'classified': _classified,
                      'pending': max(0, _total_new - _classified)}

    return render_template('dashboard.html',
        stats=stats, accounts=accounts, recent_content=recent_content, alerts=alerts,
        stock_summary=stock_summary, days_map=days_map,
        recent_activity=recent_activity, posts_today=posts_today,
        all_accounts=all_active, categories=categories,
        auto_classify_on=auto_classify_on, classify_stats=classify_stats,
        active_page='dashboard')


# ─────────────────────── HEUTE / DAILY ACTION CENTER ────────────────────────

@app.route('/api/dashboard/ai-usage')
@login_required
def dashboard_ai_usage():
    """KI-Verbrauch: heute und diesen Monat."""
    from datetime import date as _d
    today   = datetime.utcnow().date()
    month_start = datetime(today.year, today.month, 1)

    rows_today = AiUsageLog.query.filter(
        AiUsageLog.created_at >= datetime.combine(today, datetime.min.time())
    ).all()
    rows_month = AiUsageLog.query.filter(AiUsageLog.created_at >= month_start).all()

    def summarise(rows):
        total_in   = sum(r.input_tokens  for r in rows)
        total_out  = sum(r.output_tokens for r in rows)
        total_cost = sum(r.cost_eur      for r in rows)
        by_feature = {}
        for r in rows:
            by_feature.setdefault(r.feature, {'calls': 0, 'cost': 0.0})
            by_feature[r.feature]['calls'] += 1
            by_feature[r.feature]['cost']  += r.cost_eur
        return {'calls': len(rows), 'input_tokens': total_in,
                'output_tokens': total_out, 'cost_eur': round(total_cost, 4),
                'by_feature': by_feature}

    return jsonify({'today': summarise(rows_today), 'month': summarise(rows_month)})


@app.route('/heute')
@login_required
def heute():
    """Tages-Briefing: alles was heute Aufmerksamkeit braucht, auf einen Blick."""
    # "heute" in Berliner Zeit (scheduled_at/deadline sind Berlin-naiv gespeichert)
    now = now_berlin()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # ── 1. Telegram Queue ─────────────────────────────────────────────────
    tg_queue = (ScheduledPost.query
                .filter(ScheduledPost.telegram_sent_at.isnot(None))
                .filter(ScheduledPost.status != 'published')
                .options(joinedload(ScheduledPost.account))
                .order_by(ScheduledPost.telegram_sent_at.desc())
                .limit(50).all())

    # ── 2. Stock-Status nur für manuelle Accounts ─────────────────────────
    manual_accounts = Account.query.filter(
        Account.status == 'active',
        Account.automation_level < 3
    ).all()
    days_map = _get_planned_days_batch(manual_accounts)

    no_stock       = sorted([a for a in manual_accounts if days_map.get(a.id, 0) == 0],
                            key=lambda a: {'critical':0,'high':1,'medium':2,'low':3}.get(a.priority,2))
    critical_stock = sorted([a for a in manual_accounts
                              if 0 < days_map.get(a.id, 0) < (a.min_stock_days or 3)],
                            key=lambda a: days_map.get(a.id, 0))
    low_stock      = sorted([a for a in manual_accounts
                              if (a.min_stock_days or 3) <= days_map.get(a.id, 0) < 7],
                            key=lambda a: days_map.get(a.id, 0))

    # ── 3. Heute geplante Posts ───────────────────────────────────────────
    posts_today = (ScheduledPost.query
                   .filter(ScheduledPost.scheduled_at >= today_start,
                           ScheduledPost.scheduled_at < today_start + timedelta(days=1))
                   .options(joinedload(ScheduledPost.account))
                   .order_by(ScheduledPost.scheduled_at).all())

    # ── 4. Offene Alerts ─────────────────────────────────────────────────
    open_alerts = (SystemAlert.query
                   .filter_by(resolved=False)
                   .order_by(SystemAlert.severity.desc())
                   .limit(8).all())

    # ── 5. Follower-Delta (gestern vs. heute) ────────────────────────────
    yesterday_start = today_start - timedelta(days=1)
    follower_delta = None
    try:
        snap_yesterday = db.session.query(
            func.sum(AnalyticsSnapshot.followers)
        ).filter(
            func.date(AnalyticsSnapshot.recorded_at) == yesterday_start.date()
        ).scalar()
        total_now = db.session.query(func.sum(Account.follower_count)).filter_by(status='active').scalar() or 0
        if snap_yesterday:
            follower_delta = int(total_now) - int(snap_yesterday)
    except Exception:
        pass

    # ── 6. Fällige Todos (Deadline heute oder überfällig) ─────────────────
    faellige_todos = AppTodo.query.filter(
        AppTodo.done == False,
        AppTodo.deadline.isnot(None),
        AppTodo.deadline <= today_start.date(),
    ).order_by(AppTodo.deadline.asc()).all()

    # ── 7. Trend Radar: die 3 größten Themen der letzten 24h ──────────────
    trend_topics = []
    try:
        tt_cutoff = datetime.utcnow() - timedelta(hours=24)
        trend_topics = [_tr_topic_dict(t) for t in TrendTopic.query.filter(
            TrendTopic.archived == False,
            TrendTopic.last_seen_at >= tt_cutoff
        ).order_by(TrendTopic.score.desc(), TrendTopic.last_seen_at.desc()).limit(3).all()]
    except Exception:
        pass

    return render_template('heute.html',
        trend_topics=trend_topics,
        tg_queue=tg_queue,
        no_stock=no_stock,
        critical_stock=critical_stock,
        low_stock=low_stock,
        days_map=days_map,
        posts_today=posts_today,
        open_alerts=open_alerts,
        follower_delta=follower_delta,
        faellige_todos=faellige_todos,
        changelog=get_changelog()[:5],
        changelog_unread=get_changelog_unread_count(),
        now=now,
        active_page='heute')


@app.route('/api/changelog/dismiss', methods=['POST'])
@login_required
def changelog_dismiss():
    set_setting('changelog_dismissed_at', datetime.utcnow().strftime('%Y-%m-%d'))
    db.session.commit()
    return jsonify({'ok': True})


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
    acc_type = request.args.get('type', '')   # schnell-Filter: auto | manual | memes

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
    # Schnell-Typ-Filter
    if acc_type == 'auto':
        query = query.filter(Account.automation_level >= 3)
    elif acc_type == 'manual':
        query = query.filter(Account.automation_level < 3)
    elif acc_type == 'memes':
        _meme_cat_ids = [c.id for c in Category.query.filter(
            db.or_(Category.name.ilike('%meme%'), Category.name.ilike('%beicht%'),
                   Category.name.ilike('%satir%'))
        ).all()]
        _cond = [Account.name.ilike('%meme%'), Account.name.ilike('%beicht%'),
                 Account.name.ilike('%satir%')]
        if _meme_cat_ids:
            _cond.append(Account.category_id.in_(_meme_cat_ids))
        query = query.filter(db.or_(*_cond))

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

    # 7-Tage-Wachstum: Snapshot von vor 7 Tagen pro Account
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    acc_ids = [a.id for a in pagination.items]
    growth_map = {}
    if acc_ids:
        snaps = db.session.query(
            AnalyticsSnapshot.account_id,
            func.max(AnalyticsSnapshot.followers).label('followers')
        ).filter(
            AnalyticsSnapshot.account_id.in_(acc_ids),
            func.date(AnalyticsSnapshot.recorded_at) == seven_days_ago.date()
        ).group_by(AnalyticsSnapshot.account_id).all()
        for s in snaps:
            growth_map[s.account_id] = s.followers

    # ── Setup-Tracker: wie viele Seiten startklar / brauchen noch was ──
    _active_total = Account.query.filter_by(status='active').count()
    _no_channel = Account.query.filter(Account.status == 'active',
        db.or_(Account.telegram_chat_id == None, Account.telegram_chat_id == '')).count()
    setup = {
        'geplant':      Account.query.filter_by(status='geplant').count(),
        'active_total': _active_total,
        'no_channel':   _no_channel,
        'ready':        _active_total - _no_channel,
        'posting_off':  Account.query.filter(Account.status == 'active',
                            Account.posting_enabled.is_(False)).count(),
    }

    _f = {'q': q, 'category': category_id, 'platform': platform_id,
          'status': status, 'automation': automation, 'priority': priority, 'sort': sort}
    return render_template('accounts.html',
        accounts=pagination.items, pagination=pagination,
        categories=categories, platforms=platforms,
        active_page='accounts',
        acc_type=acc_type,
        growth_map=growth_map,
        setup=setup,
        filters={k: v for k, v in _f.items() if v})


@app.route('/accounts/new', methods=['GET', 'POST'])
def account_new():
    if request.method == 'POST':
        d = request.form
        interval = float(d.get('posting_interval_days') or 1.0)
        acc = Account(
            name=d['name'], handle=d.get('handle', ''),
            profile_url=d.get('profile_url', '').strip() or None,
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
            needs_stock=(d.get('no_stock') != 'on'),
            telegram_chat_id=d.get('telegram_chat_id', '').strip() or None,
            canva_url=d.get('canva_url', '').strip() or None,
            layout_notes=d.get('layout_notes', '').strip() or None,
            page_persona=d.get('page_persona', '').strip() or None,
            default_hashtags=d.get('default_hashtags', '').strip() or None,
            sports_hashtag=d.get('sports_hashtag', '').strip() or None,
            weather_city=d.get('weather_city', '').strip() or None,
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
        .filter(ScheduledPost.scheduled_at >= now_berlin())\
        .order_by(ScheduledPost.scheduled_at).limit(20).all()
    analytics = AnalyticsSnapshot.query.filter_by(account_id=account_id)\
        .order_by(AnalyticsSnapshot.recorded_at.desc()).limit(30).all()

    chart_labels = [a.recorded_at.strftime('%d.%m') for a in reversed(analytics)]
    chart_data = [a.followers for a in reversed(analytics)]

    # Stock per type — Vergleich gegen scheduled_at (Berlin-naiv)
    now = now_berlin()
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

    # Verknüpfte Content-Templates
    linked_templates = ContentTemplate.query.filter(
        ContentTemplate.target_accounts.any(id=account_id)
    ).order_by(ContentTemplate.name).all()

    is_auto = account.automation_level >= 3

    return render_template('account_detail.html',
        account=account, upcoming=upcoming,
        chart_labels=json.dumps(chart_labels), chart_data=json.dumps(chart_data),
        feed_days=round(feed_days, 1), story_days=round(story_days, 1), reel_count=reel_count,
        account_alerts=account_alerts,
        stock_posts=stock_posts, ready_content=ready_content,
        linked_templates=linked_templates,
        is_auto=is_auto,
        has_ai_key=bool(os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')),
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
        account.needs_stock = (d.get('no_stock') != 'on')
        account.telegram_chat_id = d.get('telegram_chat_id', '').strip() or None
        account.canva_url         = d.get('canva_url', '').strip() or None
        account.layout_notes      = d.get('layout_notes', '').strip() or None
        account.page_persona      = d.get('page_persona', '').strip() or None
        account.default_hashtags  = d.get('default_hashtags', '').strip() or None
        account.sports_hashtag    = d.get('sports_hashtag', '').strip() or None
        account.weather_city      = d.get('weather_city', '').strip() or None
        db.session.commit()
        flash('Account aktualisiert.', 'success')
        return redirect(url_for('account_detail', account_id=account_id))

    categories = Category.query.order_by(Category.name).all()
    platforms = Platform.query.all()
    labels = Label.query.order_by(Label.name).all()
    return render_template('account_form.html',
        account=account, categories=categories, platforms=platforms, labels=labels,
        active_page='accounts')


# ── Inspiration Feature ──────────────────────────────────────────────────────
@app.route('/api/accounts/<int:account_id>/inspire', methods=['POST'])
@login_required
def account_inspire(account_id):
    """Claude generiert Content-Ideen für diese Seite."""
    account = Account.query.get_or_404(account_id)
    d = request.get_json() or {}
    topic    = d.get('topic', '').strip()       # optional: aktuelles Trending-Thema
    count    = max(5, min(20, int(d.get('count', 10))))
    style    = d.get('style', 'standard')       # standard / satirisch / meme / humor

    api_key = os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key konfiguriert.'})

    # Seiten-Beschreibung aus page_persona oder fallback auf notes/name
    persona = account.page_persona or account.notes or ''
    ai_cfg = account.ai_config
    ai_persona = ai_cfg.persona if ai_cfg else ''
    persona = persona or ai_persona or f'Instagram-Seite namens "{account.name}"'

    style_hints = {
        'standard':  'Erstelle abwechslungsreiche, authentische Post-Ideen.',
        'satirisch': 'Erstelle satirische, humorvolle Fake-News im Tagesschau-Stil. Übertreibe aktuelle Ereignisse.',
        'meme':      'Erstelle Meme-Ideen mit lokalem Bezug. Kurz, witzig, teilbar.',
        'humor':     'Erstelle humorvolle, leichte Post-Ideen. Kein politischer Content.',
    }

    system = """Du bist ein kreativer Social-Media-Content-Stratege für deutsche Instagram-Seiten.
Du kennst den deutschen Social-Media-Markt sehr gut — was viral geht, was Menschen teilen, was Emotionen weckt.
Antworte immer auf Deutsch. Deine Ideen sind konkret, umsetzbar und plattformgerecht.
Antworte NUR mit einem JSON-Array, kein anderer Text."""

    topic_section = f'\n\nAktuelles Trending-Thema / Aufhänger: "{topic}"' if topic else ''

    user_prompt = f"""Seite: {account.name}
Plattform: Instagram
Beschreibung / Persönlichkeit: {persona}{topic_section}

{style_hints.get(style, style_hints['standard'])}

Generiere {count} konkrete Content-Ideen für diese Seite.

Format (JSON-Array):
[
  {{
    "titel": "Kurzer Titel der Idee",
    "beschreibung": "Was genau gepostet wird — Bild/Video-Beschreibung + Caption-Idee",
    "typ": "feed|reel|story|carousel",
    "hashtags": "#tag1 #tag2 #tag3",
    "vorproduzierbar": true
  }}
]

Nur das JSON-Array, keine Erklärungen drumherum."""

    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model='claude-opus-4-8',
            max_tokens=4000,
            system=system,
            messages=[{'role': 'user', 'content': user_prompt}]
        )
        _log_ai('inspire', msg)
        raw = msg.content[0].text.strip()

        if '```' in raw:
            import re
            match = re.search(r'```(?:json)?\s*([\s\S]+?)\s*```', raw)
            raw = match.group(1) if match else raw

        ideas = json.loads(raw)
        if not isinstance(ideas, list):
            ideas = list(ideas.values()) if isinstance(ideas, dict) else [ideas]

        return jsonify({'ok': True, 'ideas': ideas, 'count': len(ideas)})

    except json.JSONDecodeError as e:
        return jsonify({'ok': False, 'error': f'JSON-Fehler: {e}', 'raw': raw[:300]})
    except Exception as e:
        app.logger.error('account_inspire error: %s', e)
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/accounts/<int:account_id>/layout', methods=['POST'])
@login_required
def account_layout_save(account_id):
    """Speichert Canva-URL und Layout-Notizen direkt (ohne Formular-Reload)."""
    account = Account.query.get_or_404(account_id)
    d = request.get_json() or {}
    account.canva_url    = d.get('canva_url', '').strip() or None
    account.layout_notes = d.get('layout_notes', '').strip() or None
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/accounts/<int:account_id>/persona', methods=['POST'])
@login_required
def account_persona_save(account_id):
    """Speichert die Seiten-Persönlichkeit für den Inspiration-Generator."""
    account = Account.query.get_or_404(account_id)
    d = request.get_json() or {}
    account.page_persona = d.get('persona', '').strip() or None
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/accounts/<int:account_id>/delete', methods=['POST'])
def account_delete(account_id):
    account = Account.query.get_or_404(account_id)
    name = account.name
    # PostgreSQL FK-Constraints: manuell auflösen bevor Account gelöscht wird.
    # Reihenfolge ist wichtig: zuerst Blätter, dann Äste, dann Stamm.
    from models import AutomationRunLog, SystemAlert, AppNotification, HashtagSet, AccountAutomationProfile, RecurringPost
    # AutomationRunLog.rule_id → AutomationRule (non-nullable): zuerst Logs löschen
    rule_ids = [r.id for r in AutomationRule.query.filter_by(account_id=account_id).all()]
    if rule_ids:
        AutomationRunLog.query.filter(AutomationRunLog.rule_id.in_(rule_ids)).delete(synchronize_session='fetch')
    # nullable FKs → NULL
    SystemAlert.query.filter_by(account_id=account_id).update({'account_id': None})
    AppNotification.query.filter_by(account_id=account_id).update({'account_id': None})
    HashtagSet.query.filter_by(account_id=account_id).update({'account_id': None})
    # non-nullable FKs → ganze Zeile löschen
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


@app.route('/api/posts/<int:post_id>/media', methods=['GET'])
@login_required
def post_media(post_id):
    """Gibt alle Bild-URLs eines geplanten Posts zurück (für Karussell-Vorschau)."""
    post = ScheduledPost.query.get_or_404(post_id)
    urls = []
    # Karussell: media_ids Liste
    media_ids = post.get_media_ids()
    if media_ids:
        items = MediaItem.query.filter(MediaItem.id.in_(media_ids)).all()
        id_to_item = {m.id: m for m in items}
        for mid in media_ids:
            m = id_to_item.get(int(mid))
            if m and m.url:
                urls.append(m.url)
    # Fallback: einzelnes media_item_id
    if not urls and post.media_item_id:
        m = MediaItem.query.get(post.media_item_id)
        if m and m.url:
            urls.append(m.url)
    return jsonify({'ok': True, 'urls': urls})


@app.route('/api/posts/<int:post_id>/send-telegram', methods=['POST'])
@login_required
def post_send_telegram(post_id):
    """Manuell einen Post sofort an Telegram senden (unabhängig vom Zeitplan)."""
    post  = ScheduledPost.query.get_or_404(post_id)
    token = get_setting('telegram_bot_token')
    if not token:
        return jsonify({'ok': False, 'error': 'Kein Telegram-Bot-Token konfiguriert. Bitte in Einstellungen → Integrationen eintragen.'})
    if not post.account or not post.account.telegram_chat_id:
        return jsonify({'ok': False, 'error': 'Kein Telegram-Channel für diesen Account konfiguriert.'})
    ok = send_telegram_post(post, token=token)
    if ok:
        post.telegram_sent_at = datetime.utcnow()
        db.session.commit()
    return jsonify({'ok': ok, 'error': None if ok else 'Telegram-Versand fehlgeschlagen. Bot-Token und Chat-ID prüfen.'})


@app.route('/api/telegram/test', methods=['POST'])
@login_required
def telegram_test():
    """Sendet eine Test-Nachricht an einen Account-Channel."""
    d        = request.get_json() or {}
    chat_id  = d.get('chat_id', '').strip()
    token    = get_setting('telegram_bot_token')
    if not token:
        return jsonify({'ok': False, 'error': 'Kein Bot-Token konfiguriert.'})
    if not chat_id:
        return jsonify({'ok': False, 'error': 'Keine Chat-ID angegeben.'})
    result = _tg_send_message(token, chat_id, '✅ <b>Content OS</b> ist verbunden!\n\nDieser Channel empfängt ab sofort automatisch Posts wenn sie fällig sind.')
    return jsonify({'ok': result.get('ok', False),
                    'error': result.get('description') if not result.get('ok') else None})

@app.route('/api/posts/<int:post_id>/mark-published', methods=['POST'])
@login_required
def post_mark_published(post_id):
    """Markiert einen Post als manuell gepostet (z.B. nach Telegram-Weiterleitung)."""
    post = ScheduledPost.query.get_or_404(post_id)
    post.status = 'published'
    post.published_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/accounts/quick-bulk-create', methods=['POST'])
@login_required
def accounts_quick_bulk_create():
    """Schnell mehrere Accounts anlegen — nur Name + Handle, Rest von Template-Account kopieren."""
    d = request.get_json() or {}
    template_id = d.get('template_id')
    rows = d.get('rows', [])   # [{name, handle}, ...]

    template_acc = None
    if template_id:
        template_acc = Account.query.get(template_id)

    created = []
    errors  = []
    for row in rows:
        name   = (row.get('name') or '').strip()
        handle = (row.get('handle') or '').strip().lstrip('@')
        if not name or not handle:
            errors.append(f'Leer übersprungen: {row}')
            continue
        # Doppelt?
        if Account.query.filter(
            db.func.lower(Account.handle) == handle.lower()
        ).first():
            errors.append(f'Handle @{handle} existiert bereits.')
            continue

        profile_url = f'https://instagram.com/{handle}'

        acc = Account(
            name=name,
            handle=handle,
            profile_url=profile_url,
        )
        # Instagram-Plattform finden (oder erste verfügbare)
        ig = Platform.query.filter(Platform.name.ilike('%instagram%')).first()
        if ig:
            acc.platform_id = ig.id

        # Alle Einstellungen vom Template-Account kopieren
        if template_acc:
            acc.category_id          = template_acc.category_id
            acc.automation_level     = template_acc.automation_level
            acc.priority             = template_acc.priority
            acc.posting_interval_days= template_acc.posting_interval_days
            acc.min_stock_days       = template_acc.min_stock_days
            acc.optimal_stock_days   = template_acc.optimal_stock_days
            acc.canva_url            = template_acc.canva_url
            acc.layout_notes         = template_acc.layout_notes
            acc.page_persona         = template_acc.page_persona
            acc.status               = template_acc.status
        else:
            acc.automation_level = 0
            acc.priority = 'medium'
            acc.posting_interval_days = 1.0
            acc.min_stock_days = 3
            acc.optimal_stock_days = 14
            acc.status = 'active'

        db.session.add(acc)
        created.append({'name': name, 'handle': handle})

    db.session.commit()
    return jsonify({'ok': True, 'created': created, 'errors': errors})


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

            # Duplikat-Prüfung (nur Bilder)
            file_hash = _compute_image_hash(file_bytes) if ftype == 'image' else None
            if file_hash:
                dup, _ = _find_duplicate(file_hash)
                if dup:
                    flash(
                        f'⚠️ Duplikat übersprungen: "{original}" existiert bereits '
                        f'als "{dup.original_filename or dup.filename}" '
                        f'(vom {dup.created_at.strftime("%d.%m.%Y") if dup.created_at else "?"}).',
                        'warning'
                    )
                    continue  # diese Datei überspringen

            # Bildgrößen-Check für Instagram-Kompatibilität
            if ftype == 'image':
                try:
                    from PIL import Image as _PILImage
                    img = _PILImage.open(io.BytesIO(file_bytes))
                    w, h = img.size
                    ratio = w / h if h else 1
                    # Instagram Feed: 4:5 (0.8) bis 1.91:1
                    if ratio < 0.75:
                        flash(
                            f'⚠️ "{original}" ist zu hoch ({w}×{h}, {ratio:.2f}:1) — '
                            f'Instagram akzeptiert maximal 4:5 (0.8:1). Bitte zuschneiden.',
                            'warning'
                        )
                    elif ratio > 1.92:
                        flash(
                            f'⚠️ "{original}" ist zu breit ({w}×{h}, {ratio:.2f}:1) — '
                            f'Instagram akzeptiert maximal 1.91:1. Bitte zuschneiden.',
                            'warning'
                        )
                    elif w < 1080:
                        flash(
                            f'⚠️ "{original}" ist zu niedrig aufgelöst ({w}×{h}) — '
                            f'mindestens 1080px Breite empfohlen.',
                            'warning'
                        )
                except Exception:
                    pass

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
                    image_hash=file_hash,
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
                    image_hash=file_hash,
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
    all_accounts = Account.query\
        .filter_by(status='active')\
        .options(joinedload(Account.category))\
        .order_by(Account.follower_count.desc()).all()

    # Sichtbare Accounts (ohne Test-/Hidden-Accounts) für Gesamt-KPIs
    visible = [a for a in all_accounts if not a.hide_in_analytics]
    hidden  = [a for a in all_accounts if a.hide_in_analytics]
    total_followers = sum(a.follower_count for a in visible)

    # days_map für Account-Ranking (verhindert N+1 Queries im Template)
    days_map = _get_planned_days_batch(all_accounts)

    categories = Category.query.all()
    cat_stats = []
    for cat in categories:
        accs = [a for a in visible if a.category_id == cat.id]
        if accs:
            followers = sum(a.follower_count for a in accs)
            cat_stats.append({
                'name': cat.name, 'color': cat.color,
                'accounts': len(accs), 'followers': followers,
                'pct': round(followers / total_followers * 100, 1) if total_followers else 0
            })
    cat_stats.sort(key=lambda x: x['followers'], reverse=True)

    return render_template('analytics.html',
        accounts=visible, hidden_accounts=hidden,
        cat_stats=cat_stats, total_followers=total_followers,
        days_map=days_map, active_page='analytics')


@app.route('/api/analytics/growth')
def analytics_growth():
    from collections import defaultdict
    days = request.args.get('days', 30, type=int)
    account_id = request.args.get('account_id', type=int)
    include_forecast = request.args.get('forecast', '0') == '1'

    today = datetime.utcnow().date()
    start_date = today - timedelta(days=days - 1)

    # Per-Account-Query statt täglicher Summe — verhindert Einbrüche wenn
    # ein Account an einzelnen Tagen keinen Snapshot hat (z.B. nach Server-Restart).
    # Pro Account + Tag nur den neuesten Wert (MAX recorded_at).
    q = db.session.query(
        AnalyticsSnapshot.account_id,
        func.date(AnalyticsSnapshot.recorded_at).label('d'),
        func.max(AnalyticsSnapshot.followers).label('followers')
    ).filter(func.date(AnalyticsSnapshot.recorded_at) >= start_date)

    if account_id:
        q = q.filter(AnalyticsSnapshot.account_id == account_id)
    else:
        valid_ids = db.session.query(Account.id).filter(
            Account.status == 'active',
            Account.hide_in_analytics == False
        ).subquery()
        q = q.filter(AnalyticsSnapshot.account_id.in_(valid_ids))

    rows = q.group_by(
        AnalyticsSnapshot.account_id,
        func.date(AnalyticsSnapshot.recorded_at)
    ).all()

    # Pro Account: sortierte Timeline [(date_str, followers), ...]
    account_timelines = defaultdict(list)
    for r in rows:
        account_timelines[r.account_id].append((str(r.d), int(r.followers or 0)))
    for aid in account_timelines:
        account_timelines[aid].sort()

    # Für heute: Account.follower_count als Fallback falls noch kein Snapshot
    today_iso = today.isoformat()
    if not account_id:
        acc_today = db.session.query(Account.id, Account.follower_count).filter(
            Account.status == 'active', Account.hide_in_analytics == False
        ).all()
        for aid, fc in acc_today:
            if not any(d == today_iso for d, _ in account_timelines.get(aid, [])):
                account_timelines[aid].append((today_iso, int(fc or 0)))
                account_timelines[aid].sort()
    elif not any(d == today_iso for d, _ in account_timelines.get(account_id, [])):
        acc = Account.query.get(account_id)
        if acc:
            account_timelines[account_id].append((today_iso, int(acc.follower_count or 0)))
            account_timelines[account_id].sort()

    # Tages-Totale berechnen: pro Account letzten bekannten Wert ≤ diesem Tag summieren
    # Nur bis zum letzten echten Sync — kein verfälschter „heute"-Punkt.
    last_day = _chart_last_day(today)
    labels, data = [], []
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        if day > last_day:
            break
        day_iso = day.isoformat()
        day_total = 0
        for aid, timeline in account_timelines.items():
            last_val = 0
            for snap_date, snap_followers in timeline:
                if snap_date <= day_iso:
                    last_val = snap_followers
                else:
                    break
            day_total += last_val
        labels.append(day.strftime('%d.%m'))
        data.append(day_total)

    # Wachstums-Statistiken berechnen
    non_zero = [v for v in data if v > 0]
    start_val = non_zero[0] if non_zero else 0
    end_val   = (data[-1] or 0) if data else 0
    growth    = end_val - start_val
    growth_pct = round(growth / start_val * 100, 1) if start_val else 0
    # Tägliches Delta (nur Tage mit Daten)
    daily_deltas = [data[i] - data[i-1] for i in range(1, len(data)) if data[i] > 0 and data[i-1] > 0]
    daily_avg = round(sum(daily_deltas) / len(daily_deltas), 0) if daily_deltas else 0

    result = {
        'labels': labels, 'data': data,
        'stats': {
            'start': start_val, 'end': end_val,
            'growth': growth, 'growth_pct': growth_pct, 'daily_avg': int(daily_avg),
        }
    }
    if include_forecast and data:
        forecast_vals = linear_forecast(data, 14)
        forecast_labels = [(last_day + timedelta(days=i+1)).strftime('%d.%m') for i in range(14)]
        result['forecast_labels'] = forecast_labels
        result['forecast_data'] = forecast_vals

    return jsonify(result)


@app.route('/api/analytics/active-accounts')
def analytics_active_accounts():
    """
    Anzahl aktiver Accounts pro Tag = distinct Accounts mit einem Snapshot an
    dem Tag (der Snapshot-Job läuft täglich pro aktivem Account). Erfasst auch
    Accounts, die FRÜHER aktiv waren (inzwischen pausiert) — zeigt also „wie
    viele aktiv sind und waren". Orphan-Snapshots gelöschter/versteckter
    Accounts sind ausgeschlossen. Lücken (z.B. Server-Restart) = forward-fill.
    """
    days = request.args.get('days', 30, type=int)
    today = datetime.utcnow().date()
    start_date = today - timedelta(days=days - 1)

    existing_ids = db.session.query(Account.id).filter(
        Account.hide_in_analytics == False
    ).subquery()

    rows = db.session.query(
        func.date(AnalyticsSnapshot.recorded_at).label('d'),
        func.count(func.distinct(AnalyticsSnapshot.account_id)).label('cnt')
    ).filter(
        func.date(AnalyticsSnapshot.recorded_at) >= start_date,
        AnalyticsSnapshot.account_id.in_(existing_ids)
    ).group_by(func.date(AnalyticsSnapshot.recorded_at)).all()
    by_day = {str(r.d): int(r.cnt) for r in rows}

    current_active = db.session.query(func.count(Account.id)).filter(
        Account.status == 'active', Account.hide_in_analytics == False
    ).scalar() or 0

    last_day = _chart_last_day(today)
    labels, data = [], []
    last = 0
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        if day > last_day:
            break
        iso = day.isoformat()
        if iso in by_day:
            last = by_day[iso]
        # sonst: forward-fill (last bleibt) — überbrückt Tage ohne Snapshot-Lauf
        if day == today and current_active > last:
            last = current_active  # heutiger Snapshot evtl. noch nicht / unvollständig erzeugt
        labels.append(day.strftime('%d.%m'))
        data.append(last)

    return jsonify({'labels': labels, 'data': data, 'current': current_active})


@app.route('/api/analytics/portfolio')
def analytics_portfolio():
    """
    Gesamt-Follower über alle Accounts pro Tag.
    Lücken werden mit dem zuletzt bekannten Wert aufgefüllt (forward-fill).
    Heute = max(letzter Snapshot, aktueller Account.follower_count).
    """
    days = request.args.get('days', 30, type=int)
    today = datetime.utcnow().date()

    # Aktuelles Portfolio-Total (nur sichtbare Accounts)
    current_total = db.session.query(func.sum(Account.follower_count))\
        .filter(Account.status == 'active', Account.hide_in_analytics == False).scalar() or 0

    start_date = today - timedelta(days=days - 1)

    # Whitelist: nur aktive + sichtbare Accounts (schließt Orphan-Snapshots aus)
    valid_ids = db.session.query(Account.id).filter(
        Account.status == 'active',
        Account.hide_in_analytics == False
    ).subquery()

    # Subquery: spätester recorded_at pro (account_id, tag) — nur valide Accounts
    latest_per_acc_day = db.session.query(
        AnalyticsSnapshot.account_id,
        func.date(AnalyticsSnapshot.recorded_at).label('snap_day'),
        func.max(AnalyticsSnapshot.recorded_at).label('latest_at')
    ).filter(
        func.date(AnalyticsSnapshot.recorded_at) >= start_date,
        AnalyticsSnapshot.account_id.in_(valid_ids)
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

    last_day = _chart_last_day(today)
    labels, data = [], []
    last_known = None
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        if d > last_day:
            break
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


@app.route('/api/analytics/cleanup-snapshots', methods=['POST'])
def cleanup_orphan_snapshots():
    """Löscht AnalyticsSnapshots von nicht-aktiven oder ausgeblendeten Accounts."""
    valid_ids = [r.id for r in db.session.query(Account.id).filter(
        Account.status == 'active',
        Account.hide_in_analytics == False
    ).all()]
    if valid_ids:
        deleted = AnalyticsSnapshot.query.filter(
            ~AnalyticsSnapshot.account_id.in_(valid_ids)
        ).delete(synchronize_session='fetch')
    else:
        deleted = 0
    db.session.commit()
    return jsonify({'ok': True, 'deleted': deleted,
                    'msg': f'{deleted} veraltete Snapshots gelöscht'})


@app.route('/api/analytics/reset-history', methods=['POST'])
def reset_analytics_history():
    """Löscht ALLE AnalyticsSnapshots und legt einen sauberen Start-Snapshot
    mit dem aktuellen follower_count pro Account an.
    Geeignet nach einer Test-Phase um verfälschte Verlaufsdaten zu entfernen."""
    # Alles löschen
    total_deleted = AnalyticsSnapshot.query.delete(synchronize_session='fetch')
    db.session.flush()

    # Einen frischen Snapshot pro aktivem Account anlegen
    now = datetime.utcnow()
    active_accounts = Account.query.filter_by(status='active').all()
    for acc in active_accounts:
        if acc.follower_count:
            db.session.add(AnalyticsSnapshot(
                account_id=acc.id,
                followers=acc.follower_count,
                recorded_at=now,
            ))
    db.session.commit()
    return jsonify({
        'ok': True,
        'deleted': total_deleted,
        'fresh_snapshots': len(active_accounts),
        'msg': f'Verlauf zurückgesetzt: {total_deleted} alte Snapshots gelöscht, '
               f'{len(active_accounts)} saubere Start-Snapshots angelegt',
    })


@app.route('/api/accounts/<int:account_id>/toggle-analytics', methods=['POST'])
def toggle_analytics_visibility(account_id):
    """Blendet einen Account aus den Analytics-Gesamt-Charts aus / ein."""
    acc = Account.query.get_or_404(account_id)
    acc.hide_in_analytics = not acc.hide_in_analytics
    db.session.commit()
    return jsonify({
        'ok': True,
        'hidden': acc.hide_in_analytics,
        'name': acc.name,
        'msg': f'"{acc.name}" {"ausgeblendet" if acc.hide_in_analytics else "wieder eingeblendet"}',
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
    now_utc = datetime.utcnow()
    next_run_map = {}
    for r in rules:
        if r.last_run_at and r.run_interval_minutes:
            next_run_map[r.id] = r.last_run_at + timedelta(minutes=r.run_interval_minutes)
        else:
            next_run_map[r.id] = None
    return render_template('automation.html',
        rules=rules, accounts=all_accounts, active_page='automation',
        emergency_pause=_is_emergency_paused(),
        next_run_map=next_run_map, now=now_utc)


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
        acc_id = int(d['account_id']) if d.get('account_id') else None
        rule = AutomationRule(
            account_id=acc_id,
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
        if d.get('back_url') == 'studio' and acc_id:
            return redirect(url_for('content_studio_account', account_id=acc_id))
        return redirect(url_for('automation'))

    all_accounts = Account.query.order_by(Account.name).all()
    preselect_account_id = request.args.get('account_id', type=int)
    back_url = request.args.get('back', '')
    return render_template('automation_form.html',
        rule=None, accounts=all_accounts, active_page='automation',
        preselect_account_id=preselect_account_id, back_url=back_url)


@app.route('/automation/<int:rule_id>/delete', methods=['POST'])
@login_required
def automation_delete(rule_id):
    rule = AutomationRule.query.get_or_404(rule_id)
    AutomationRunLog.query.filter_by(rule_id=rule_id).delete(synchronize_session='fetch')
    db.session.delete(rule)
    db.session.commit()
    return jsonify({'ok': True})


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
        cat_name = (account.category.name if account.category else '').lower()
        is_meme = any(w in cat_name for w in ['meme', 'humor', 'satire', 'fun', 'witzig'])
        cfg = AIConfig(
            account_id=account_id,
            caption_tone='humorvoll' if is_meme else 'informativ',
            caption_min_words=5 if is_meme else 30,
            caption_max_words=30 if is_meme else 200,  # kurze Captions performen besser
        )
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
    from zoneinfo import ZoneInfo
    alerts = SystemAlert.query.order_by(SystemAlert.resolved, SystemAlert.severity.desc(),
                                        SystemAlert.created_at.desc()).all()
    berlin = ZoneInfo('Europe/Berlin')
    today  = datetime.now(berlin).date()
    ns = NotificationSettings.query.first()
    low_stock_days = (ns.low_stock_days if ns else None) or 3
    bot_settings = {
        'alert_telegram_token':  get_setting('alert_telegram_token', ''),
        'alert_central_chat_id': get_setting('alert_central_chat_id', ''),
        'telegram_bot_token':    get_setting('telegram_bot_token', ''),
    }
    return render_template('alerts.html', alerts=alerts, active_page='alerts',
                           bot_settings=bot_settings, low_stock_days=low_stock_days,
                           today=today)


# ─────────────────────── MITARBEITER ───────────────────────

@app.route('/mitarbeiter')
@login_required
def mitarbeiter():
    from zoneinfo import ZoneInfo
    members = (TeamMember.query
               .filter_by(active=True)
               .options(joinedload(TeamMember.accounts))
               .order_by(TeamMember.name).all())
    berlin  = ZoneInfo('Europe/Berlin')
    today   = datetime.now(berlin).date()
    all_accounts = (Account.query
                    .filter_by(status='active')
                    .options(joinedload(Account.team_member))
                    .order_by(Account.name).all())
    categories = Category.query.order_by(Category.name).all()
    assigned_count   = sum(len(m.accounts) for m in members)
    unassigned_count = sum(1 for a in all_accounts if a.team_member_id is None)
    # Build category map for each member (for JS filter)
    member_cats = {m.id: list({a.category_id for a in m.accounts if a.category_id}) for m in members}
    # Build account data for Seiten-view JS
    import json as _json
    accounts_json = _json.dumps([{
        'id': a.id, 'name': a.name, 'handle': a.handle or '',
        'category_id': a.category_id, 'category_name': a.category.name if a.category_id and a.category else '',
        'category_color': a.category.color if a.category_id and a.category else '#6366f1',
        'team_member_id': a.team_member_id,
        'team_member_name': a.team_member.name if a.team_member else '',
    } for a in all_accounts])
    members_json = _json.dumps([{'id': m.id, 'name': m.name, 'role': m.role} for m in members])
    cats_json    = _json.dumps([{'id': c.id, 'name': c.name, 'color': c.color} for c in categories])
    return render_template('mitarbeiter.html', members=members, today=today,
                           all_accounts=all_accounts, categories=categories,
                           active_page='mitarbeiter',
                           assigned_count=assigned_count, unassigned_count=unassigned_count,
                           member_cats=member_cats,
                           accounts_json=accounts_json, members_json=members_json, cats_json=cats_json)


@app.route('/api/mitarbeiter/<int:member_id>', methods=['POST'])
@login_required
def api_mitarbeiter_update(member_id):
    member = TeamMember.query.get_or_404(member_id)
    d = request.get_json()
    for field in ('name', 'email', 'role', 'phone', 'telegram_username', 'tg_personal_chat_id', 'notes', 'work_status'):
        if field in d:
            setattr(member, field, d[field] or None)
    if 'active' in d:
        member.active = bool(d['active'])
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/mitarbeiter/<int:member_id>/ping', methods=['POST'])
@login_required
def api_mitarbeiter_ping(member_id):
    member = TeamMember.query.get_or_404(member_id)
    if not member.telegram_username:
        return jsonify({'ok': False, 'error': 'Kein Telegram-Username hinterlegt'}), 400
    token   = get_setting('alert_telegram_token', '')
    chat_id = get_setting('alert_central_chat_id', '')
    if not token or not chat_id:
        return jsonify({'ok': False, 'error': 'Alert-Bot nicht konfiguriert'}), 400
    d = request.get_json() or {}
    username   = member.telegram_username.lstrip('@')
    custom_msg = (d.get('message') or '').strip()
    if custom_msg:
        text = f'\U0001f4e2 @{username}: {custom_msg}'
    else:
        acc_names = ', '.join(a.name for a in member.accounts[:5] if a.status == 'active')
        text = f'\U0001f4e2 *Reminder* für @{username}:\nBitte poste heute'
        if acc_names:
            text += f' für: {acc_names}'
    import requests as _req
    r = _req.post(f'https://api.telegram.org/bot{token}/sendMessage',
                  json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'},
                  timeout=10)
    result = r.json()
    return jsonify({'ok': result.get('ok', False), 'error': result.get('description')})


@app.route('/api/mitarbeiter/<int:member_id>/warn', methods=['POST'])
@login_required
def api_mitarbeiter_warn(member_id):
    member = TeamMember.query.get_or_404(member_id)
    if member.warning_count >= 3:
        return jsonify({'ok': True, 'count': 3, 'fired': False})
    member.warning_count = (member.warning_count or 0) + 1
    fired = False
    if member.warning_count >= 3:
        fired = True
        alert_token  = get_setting('alert_telegram_token', '')
        alert_chatid = get_setting('alert_central_chat_id', '')
        bot_token    = get_setting('telegram_bot_token', '')
        username     = (member.telegram_username or '').lstrip('@')

        dismissal_text = (
            f'Hallo {member.name},\n\n'
            f'wir möchten uns herzlich für deine bisherige Mitarbeit bei uns bedanken. '
            f'Leider müssen wir dir mitteilen, dass wir die Zusammenarbeit ab sofort '
            f'nicht weiter fortführen können.\n\n'
            f'Wir bitten dich, dich umgehend aus allen Accounts und dem System '
            f'auszuloggen und keine weiteren Aktionen auf unseren Seiten vorzunehmen.\n\n'
            f'Wir wünschen dir alles Gute für deinen weiteren Weg.\n'
            f'Vielen Dank und liebe Grüße 🙏'
        )

        # 1a) Direkte DM an den Mitarbeiter (wenn persönliche Chat-ID hinterlegt)
        if bot_token and member.tg_personal_chat_id:
            _tg_send_message(bot_token, member.tg_personal_chat_id, dismissal_text)

        # 1b) Alert-Channel: Trennungsnachricht mit @mention (Fallback / immer)
        if alert_token and alert_chatid:
            mention = f'@{username}' if username else member.name
            _tg_send_message(alert_token, alert_chatid,
                f'📩 <b>Trennungsnachricht für {mention}</b>\n\n{dismissal_text}')

        # 1c) CityBot Journalist → Nachricht in die Telegram-Channels der zugewiesenen Accounts
        if bot_token:
            account_channel_msg = (
                f'👋 Liebes Team, kurze Mitteilung:\n\n'
                f'Es gibt personelle Änderungen in unserem Redaktionsteam. '
                f'Bitte stelle sicher, dass alle bisherigen Zugangsdaten aktualisiert werden '
                f'und keine unbefugten Zugriffe mehr erfolgen.\n\n'
                f'Bei Fragen meldet euch gerne bei uns. Danke!'
            )
            assigned_accounts = Account.query.filter_by(
                team_member_id=member.id, status='active'
            ).all()
            for acc in assigned_accounts:
                if acc.telegram_chat_id:
                    try:
                        _tg_send_message(bot_token, acc.telegram_chat_id, account_channel_msg)
                    except Exception:
                        pass

        # 2) Dringender To-Do
        from datetime import date as _date
        todo = AppTodo(
            title=f'🚨 Zugangsdaten sofort ändern – {member.name} entlassen',
            text=(f'Mitarbeiter {member.name} hat 3 Verwarnungen erhalten und wurde entlassen. '
                  f'Bitte sofort alle Passwörter und Zugangsdaten der zugewiesenen Accounts ändern!'),
            category='aufgabe',
            priority=2,
            deadline=_date.today(),
            done=False
        )
        db.session.add(todo)

        # 3) Central Alert
        dm_status = '✅ DM gesendet' if (bot_token and member.tg_personal_chat_id) else '⚠️ Keine persönliche Chat-ID hinterlegt'
        _send_central_alert(
            f'🚨 <b>Mitarbeiter entlassen: {member.name}</b>\n\n'
            f'• Direktnachricht: {dm_status}\n'
            f'• Alert-Channel: Trennungsnachricht gepostet\n'
            f'• Account-Channels: Benachrichtigt\n'
            f'<b>Bitte Zugangsdaten sofort ändern!</b>'
        )
    db.session.commit()
    return jsonify({'ok': True, 'count': member.warning_count, 'fired': fired})


@app.route('/api/mitarbeiter/<int:member_id>/warn/reset', methods=['POST'])
@login_required
def api_mitarbeiter_warn_reset(member_id):
    member = TeamMember.query.get_or_404(member_id)
    member.warning_count = 0
    db.session.commit()
    return jsonify({'ok': True, 'count': 0})


@app.route('/api/mitarbeiter/new', methods=['POST'])
@login_required
def api_mitarbeiter_new():
    d = request.get_json()
    if not d.get('name') or not d.get('email'):
        return jsonify({'ok': False, 'error': 'Name und E-Mail erforderlich'}), 400
    if TeamMember.query.filter_by(email=d['email']).first():
        return jsonify({'ok': False, 'error': 'E-Mail bereits vergeben'}), 400
    m = TeamMember(
        name=d['name'], email=d['email'],
        role=d.get('role', 'poster'),
        phone=d.get('phone') or None,
        telegram_username=d.get('telegram_username') or None,
        notes=d.get('notes') or None,
    )
    db.session.add(m)
    db.session.commit()
    return jsonify({'ok': True, 'id': m.id})


@app.route('/api/mitarbeiter/<int:member_id>/delete', methods=['POST'])
@login_required
def api_mitarbeiter_delete(member_id):
    member = TeamMember.query.get_or_404(member_id)
    Account.query.filter_by(team_member_id=member_id).update({'team_member_id': None})
    ContentItem.query.filter_by(author_id=member_id).update({'author_id': None})
    ScheduledPost.query.filter_by(created_by_id=member_id).update({'created_by_id': None})
    db.session.flush()
    db.session.delete(member)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/mitarbeiter/<int:member_id>/accounts', methods=['POST'])
@login_required
def api_mitarbeiter_accounts(member_id):
    TeamMember.query.get_or_404(member_id)
    d = request.get_json()
    account_ids = [int(x) for x in (d.get('account_ids') or [])]
    Account.query.filter_by(team_member_id=member_id).update({'team_member_id': None})
    if account_ids:
        Account.query.filter(Account.id.in_(account_ids)).update(
            {'team_member_id': member_id}, synchronize_session='fetch')
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/mitarbeiter/<int:member_id>/stats')
@login_required
def api_mitarbeiter_stats(member_id):
    from zoneinfo import ZoneInfo
    member = TeamMember.query.get_or_404(member_id)
    berlin   = ZoneInfo('Europe/Berlin')
    today    = datetime.now(berlin).date()
    week_ago = today - timedelta(days=6)
    acc_ids  = [a.id for a in member.accounts if a.status == 'active']
    if not acc_ids:
        return jsonify({'days': [], 'totals': {'sent':0,'posted':0,'late':0,'missing':0}})
    days_data = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        posts = ScheduledPost.query.filter(
            ScheduledPost.account_id.in_(acc_ids),
            func.date(ScheduledPost.telegram_sent_at) == day).all()
        sent    = len(posts)
        posted  = sum(1 for p in posts if p.published_at)
        late    = sum(1 for p in posts if p.published_at and
                      p.published_at.replace(tzinfo=timezone.utc).astimezone(berlin).hour >= 22)
        days_data.append({'date': str(day), 'sent': sent, 'posted': posted,
                          'late': late, 'missing': sent - posted})
    totals = {k: sum(d[k] for d in days_data) for k in ('sent','posted','late','missing')}
    return jsonify({'days': days_data, 'totals': totals})


@app.route('/alerts/refresh', methods=['POST'])
def alerts_refresh():
    generate_alerts()
    flash('Alerts neu generiert.', 'success')
    return redirect(url_for('alerts_center'))


@app.route('/api/monitor/posts')
@login_required
def api_monitor_posts():
    from zoneinfo import ZoneInfo
    date_str = request.args.get('date')
    try:
        day = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else datetime.now(ZoneInfo('Europe/Berlin')).date()
    except ValueError:
        return jsonify({'error': 'invalid date'}), 400
    posts = (ScheduledPost.query
        .filter(func.date(ScheduledPost.telegram_sent_at) == day)
        .options(joinedload(ScheduledPost.account))
        .order_by(ScheduledPost.telegram_sent_at).all())
    berlin = ZoneInfo('Europe/Berlin')
    result = []
    for p in posts:
        sent = p.telegram_sent_at.replace(tzinfo=timezone.utc).astimezone(berlin) if p.telegram_sent_at else None
        pub  = p.published_at.replace(tzinfo=timezone.utc).astimezone(berlin) if p.published_at else None
        late = pub and pub.hour >= 22
        result.append({
            'id': p.id,
            'account': p.account.name if p.account else '—',
            'sent': sent.strftime('%H:%M') if sent else None,
            'posted': pub.strftime('%H:%M') if pub else None,
            'status': p.status,
            'late': late,
        })
    return jsonify({'posts': result, 'date': str(day)})


@app.route('/api/bot-settings', methods=['GET', 'POST'])
@login_required
def api_bot_settings():
    BOT_KEYS = ['alert_telegram_token', 'alert_central_chat_id', 'telegram_bot_token']
    if request.method == 'GET':
        ns = NotificationSettings.query.first()
        result = {k: get_setting(k, '') for k in BOT_KEYS}
        result['low_stock_days'] = (ns.low_stock_days if ns else None) or 3
        return jsonify(result)
    d = request.get_json()
    for key in BOT_KEYS:
        if key in d:
            set_setting(key, d[key])
    if 'low_stock_days' in d:
        ns = NotificationSettings.query.first()
        if not ns:
            ns = NotificationSettings(); db.session.add(ns)
        ns.low_stock_days = int(d['low_stock_days'])
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/monitor/workers')
@login_required
def api_monitor_workers():
    from zoneinfo import ZoneInfo
    from models import TeamMember
    date_str = request.args.get('date')
    try:
        day = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else datetime.now(ZoneInfo('Europe/Berlin')).date()
    except ValueError:
        return jsonify({'error': 'invalid date'}), 400
    berlin = ZoneInfo('Europe/Berlin')
    # 7-day window for reliability score
    week_ago = day - timedelta(days=6)
    workers = TeamMember.query.filter_by(active=True).order_by(TeamMember.name).all()
    result = []
    for w in workers:
        acc_ids = [a.id for a in w.accounts if a.status == 'active']
        if not acc_ids:
            continue
        # Today's posts
        posts = (ScheduledPost.query
            .filter(ScheduledPost.account_id.in_(acc_ids),
                    func.date(ScheduledPost.telegram_sent_at) == day)
            .options(joinedload(ScheduledPost.account))
            .order_by(ScheduledPost.telegram_sent_at).all())
        # 7-day stats
        week_posts = (ScheduledPost.query
            .filter(ScheduledPost.account_id.in_(acc_ids),
                    func.date(ScheduledPost.telegram_sent_at) >= week_ago,
                    func.date(ScheduledPost.telegram_sent_at) <= day)
            .all())
        week_sent    = len(week_posts)
        week_posted  = sum(1 for p in week_posts if p.published_at)
        week_late    = sum(1 for p in week_posts if p.published_at and
                          p.published_at.replace(tzinfo=timezone.utc).astimezone(berlin).hour >= 22)
        reliability  = round(week_posted / week_sent * 100) if week_sent else None
        account_rows = []
        for p in posts:
            sent = p.telegram_sent_at.replace(tzinfo=timezone.utc).astimezone(berlin) if p.telegram_sent_at else None
            pub  = p.published_at.replace(tzinfo=timezone.utc).astimezone(berlin)     if p.published_at  else None
            late = bool(pub and pub.hour >= 22)
            account_rows.append({
                'account':  p.account.name if p.account else '—',
                'sent':     sent.strftime('%H:%M') if sent else None,
                'posted':   pub.strftime('%H:%M')  if pub  else None,
                'late':     late,
                'status':   p.status,
            })
        today_sent    = len(posts)
        today_posted  = sum(1 for p in posts if p.published_at)
        today_late    = sum(1 for r in account_rows if r['late'])
        today_missing = today_sent - today_posted
        result.append({
            'id':           w.id,
            'name':         w.name,
            'role':         w.role,
            'account_count': len(acc_ids),
            'today':        {'sent': today_sent, 'posted': today_posted,
                             'late': today_late, 'missing': today_missing},
            'week':         {'sent': week_sent, 'posted': week_posted,
                             'late': week_late, 'reliability': reliability},
            'posts':        account_rows,
        })
    return jsonify({'workers': result, 'date': str(day)})


@app.route('/api/morning-report/test', methods=['POST'])
@login_required
def api_morning_report_test():
    token = os.environ.get('CRON_TOKEN') or get_setting('cron_token') or ''
    if not token:
        return jsonify({'ok': False, 'error': 'CRON_TOKEN nicht gesetzt'})
    import requests as _req
    url = request.host_url.rstrip('/') + '/cron/morning-report'
    try:
        r = _req.get(url, params={'token': token}, timeout=15)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


# ─────────────────────── SETTINGS ───────────────────────

@app.route('/settings')
@login_required
def settings():
    def gs(key, default=''):
        r = AppSettings.query.filter_by(key=key).first()
        return r.value if r and r.value is not None else default
    def mask(k):
        return (k[:6] + '…' + k[-4:]) if k and len(k) > 12 else ('●●●●●' if k else '')

    categories = Category.query.order_by(Category.name).all()
    labels = Label.query.order_by(Label.name).all()
    platforms = Platform.query.all()
    ns = NotificationSettings.query.first()
    _lb = gs('last_full_backup_at')
    last_backup_days = None
    if _lb:
        try:
            last_backup_days = (datetime.utcnow() - datetime.fromisoformat(_lb)).days
        except Exception:
            pass
    _ms = now_berlin().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    ai_spend_month = db.session.query(func.coalesce(func.sum(AiUsageLog.cost_eur), 0.0)) \
        .filter(AiUsageLog.created_at >= _ms).scalar() or 0.0
    return render_template('settings.html',
        categories=categories, labels=labels, platforms=platforms,
        active_page='settings',
        ai_budget=gs('ai_budget_eur', ''),
        ai_spend_month=round(ai_spend_month, 2),
        # Verbindungen
        ig_sync_method=gs('ig_sync_method', 'apify' if gs('apify_token') else 'direct'),
        apify_token_set=bool(gs('apify_token')),
        rapidapi_key_set=bool(gs('rapidapi_key')),
        anthropic_key_set=bool(os.environ.get('ANTHROPIC_API_KEY') or gs('anthropic_api_key')),
        cron_token=gs('cron_token'),
        tg_alert_token_set=bool(gs('alert_telegram_token')),
        tg_alert_chat_id=gs('alert_central_chat_id'),
        tg_bot_token_set=bool(gs('telegram_bot_token')),
        auto_sync=gs('ig_auto_sync', '1') != '0',
        ig_accounts_count=Account.query.filter(
            Account.handle != None, Account.handle != '', Account.status == 'active'
        ).count(),
        # Benachrichtigungen
        low_stock_days=(ns.low_stock_days if ns else None) or 3,
        # Daten
        last_backup_days=last_backup_days,
    )


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
    from collections import defaultdict
    days = request.args.get('days', 30, type=int)
    account_id = request.args.get('account_id', type=int)

    today = datetime.utcnow().date()
    start_date = today - timedelta(days=days - 1)

    fq = db.session.query(
        AnalyticsSnapshot.account_id,
        func.date(AnalyticsSnapshot.recorded_at).label('d'),
        func.max(AnalyticsSnapshot.followers).label('followers')
    ).filter(func.date(AnalyticsSnapshot.recorded_at) >= start_date)
    eq = db.session.query(
        func.date(AnalyticsSnapshot.recorded_at).label('d'),
        func.avg(AnalyticsSnapshot.engagement_rate).label('eng')
    ).filter(func.date(AnalyticsSnapshot.recorded_at) >= start_date)

    if account_id:
        fq = fq.filter(AnalyticsSnapshot.account_id == account_id)
        eq = eq.filter(AnalyticsSnapshot.account_id == account_id)
    else:
        valid_ids = db.session.query(Account.id).filter(
            Account.status == 'active', Account.hide_in_analytics == False
        ).subquery()
        fq = fq.filter(AnalyticsSnapshot.account_id.in_(valid_ids))
        eq = eq.filter(AnalyticsSnapshot.account_id.in_(valid_ids))

    fq = fq.group_by(AnalyticsSnapshot.account_id, func.date(AnalyticsSnapshot.recorded_at))
    eq = eq.group_by(func.date(AnalyticsSnapshot.recorded_at))

    account_timelines = defaultdict(list)
    for r in fq.all():
        account_timelines[r.account_id].append((str(r.d), int(r.followers or 0)))
    for aid in account_timelines:
        account_timelines[aid].sort()

    eng_by_day = {str(r.d): round(float(r.eng or 0), 2) for r in eq.all()}

    last_day = _chart_last_day(today)
    labels, deltas, eng_rates = [], [], []
    prev_total = None
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        if day > last_day:
            break
        day_iso = day.isoformat()
        day_total = 0
        for aid, timeline in account_timelines.items():
            last_val = 0
            for snap_date, snap_followers in timeline:
                if snap_date <= day_iso:
                    last_val = snap_followers
                else:
                    break
            day_total += last_val
        delta = (day_total - prev_total) if prev_total is not None and day_total > 0 else 0
        if day_total > 0:
            prev_total = day_total
        labels.append(day.strftime('%d.%m'))
        deltas.append(delta)
        eng_rates.append(eng_by_day.get(day_iso, 0))

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


@app.route('/api/labels/delete-unused', methods=['POST'])
@login_required
def labels_delete_unused():
    all_labels = Label.query.all()
    deleted = []
    for label in all_labels:
        if len(label.content_items) == 0:
            deleted.append(label.name)
            db.session.delete(label)
    db.session.commit()
    return jsonify({'ok': True, 'deleted': deleted, 'count': len(deleted)})


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
            return redirect(next_url or url_for('heute'))
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
        if action == 'status' and value in ['active', 'paused', 'error', 'inactive', 'geplant']:
            acc.status = value; count += 1
        elif action == 'posting' and value in ['on', 'off']:
            acc.posting_enabled = (value == 'on'); count += 1
        elif action == 'needs_stock' and value in ['on', 'off']:
            acc.needs_stock = (value == 'on'); count += 1
        elif action == 'automation' and value is not None:
            acc.automation_level = int(value); count += 1
        elif action == 'priority' and value in ['critical', 'high', 'medium', 'low']:
            acc.priority = value; count += 1
        elif action == 'category' and value:
            acc.category_id = int(value); count += 1

    db.session.commit()
    return jsonify({'ok': True, 'affected': count})


@app.route('/api/accounts/<int:account_id>/toggle-posting', methods=['POST'])
@login_required
def account_toggle_posting(account_id):
    """Vorrat-/Posting-Toggle: an = Posts werden gesendet, aus = nur Vorrat sammeln."""
    acc = Account.query.get_or_404(account_id)
    acc.posting_enabled = not (acc.posting_enabled is not False)
    db.session.commit()
    return jsonify({'ok': True, 'posting_enabled': acc.posting_enabled})


# ─────────────────────── GLOBAL SEARCH ───────────────────────

@app.route('/api/search')
@login_required
def global_search():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify({'accounts': [], 'content': [], 'media': [], 'kooperationen': []})

    accounts = Account.query.filter(
        Account.name.ilike(f'%{q}%') | Account.handle.ilike(f'%{q}%')
    ).limit(5).all()

    content = ContentItem.query.filter(
        ContentItem.title.ilike(f'%{q}%') | ContentItem.caption.ilike(f'%{q}%')
    ).limit(5).all()

    media = MediaItem.query.filter(
        MediaItem.original_filename.ilike(f'%{q}%')
    ).limit(5).all()

    koops = Kooperation.query.filter(
        Kooperation.partner_name.ilike(f'%{q}%') | Kooperation.campaign_name.ilike(f'%{q}%')
    ).limit(4).all()

    return jsonify({
        'accounts': [{'id': a.id, 'name': a.name, 'handle': a.handle,
                      'url': url_for('account_detail', account_id=a.id)} for a in accounts],
        'content':  [{'id': c.id, 'title': c.title, 'status': c.status,
                      'url': url_for('content_detail', item_id=c.id)} for c in content],
        'media':    [{'id': m.id, 'name': m.original_filename, 'type': m.file_type,
                      'url': url_for('media_library')} for m in media],
        'kooperationen': [{'id': k.id, 'name': k.partner_name,
                           'campaign': k.campaign_name or '',
                           'status': k.status,
                           'url': url_for('kooperationen')} for k in koops],
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
    cutoff = today - timedelta(days=364)
    # Eine einzige GROUP-BY-Query statt bis zu 365 Einzel-COUNT-Queries
    rows = db.session.query(
        func.date(ScheduledPost.scheduled_at).label('d')
    ).filter(
        ScheduledPost.status.in_(['scheduled', 'published']),
        func.date(ScheduledPost.scheduled_at) >= cutoff
    ).group_by(func.date(ScheduledPost.scheduled_at)).all()
    active_dates = {r.d for r in rows}
    streak = 0
    for i in range(365):
        day = today - timedelta(days=i)
        if day in active_dates:
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

@app.route('/api/backup/download')
@login_required
def backup_download():
    """Schicht 5: DB direkt aus dem Browser herunterladen."""
    if 'postgresql' in os.environ.get('DATABASE_URL', ''):
        return jsonify({'error': 'Nur für SQLite verfügbar'}), 400
    if not os.path.exists(_DB_PATH):
        return jsonify({'error': 'Keine lokale Datenbank gefunden'}), 404
    from flask import send_file
    stamp = datetime.now().strftime('%Y-%m-%d_%H-%M')
    return send_file(_DB_PATH, as_attachment=True,
                     download_name=f'content_os_backup_{stamp}.db',
                     mimetype='application/octet-stream')


@app.route('/api/backup/status')
@login_required
def backup_status():
    import glob
    if 'postgresql' in os.environ.get('DATABASE_URL', ''):
        return jsonify({'mode': 'postgres', 'backups': []})
    local_files = sorted(glob.glob(os.path.join(_LOCAL_BACKUP_DIR, '*.db')), reverse=True)[:5]
    db_size = os.path.getsize(_DB_PATH) if os.path.exists(_DB_PATH) else 0
    return jsonify({
        'mode': 'sqlite',
        'db_size_kb': round(db_size / 1024, 1),
        'local_backups': [os.path.basename(f) for f in local_files],
        'last_backup': os.path.basename(local_files[0]) if local_files else None,
    })


@app.route('/api/backup/now', methods=['POST'])
@login_required
def backup_now():
    """Manuelles Backup auslösen."""
    result = _do_backup('manual')
    if result:
        return jsonify({'ok': True, 'file': os.path.basename(result)})
    return jsonify({'ok': False, 'error': 'Backup fehlgeschlagen oder Postgres'})


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


@app.route('/api/export/all')
@login_required
def export_all_data():
    """Vollständiger JSON-Export aller Nutzerdaten."""
    from flask import Response

    def _date(v):
        return v.isoformat() if v else None

    accounts = Account.query.options(joinedload(Account.category)).all()
    content_items = ContentItem.query.options(
        joinedload(ContentItem.category),
        joinedload(ContentItem.folder),
        joinedload(ContentItem.media_items),
    ).order_by(ContentItem.created_at.desc()).all()
    folders = ContentFolder.query.all()
    scheduled = ScheduledPost.query.order_by(ScheduledPost.scheduled_at.desc()).limit(500).all()
    inspiration = InspirationPost.query.order_by(InspirationPost.created_at.desc()).all()
    settings_rows = AppSettings.query.all()
    hashtag_sets = HashtagSet.query.all() if 'HashtagSet' in dir() else []
    koops = Kooperation.query.all()
    serien = ContentSeries.query.all()

    data = {
        'exported_at': datetime.utcnow().isoformat(),
        'version': '2.0',

        'accounts': [{
            'id': a.id, 'name': a.name, 'handle': a.handle,
            'platform': a.platform.name if a.platform else None,
            'category': a.category.name if a.category else None,
            'follower_count': a.follower_count, 'status': a.status,
            'notes': a.notes, 'page_persona': a.page_persona,
            'canva_url': a.canva_url, 'weather_city': a.weather_city,
            'default_hashtags': a.default_hashtags,
            'telegram_chat_id': a.telegram_chat_id,
            'smart_refill_threshold': a.smart_refill_threshold,
            'created_at': _date(a.created_at),
        } for a in accounts],

        'folders': [{
            'id': f.id, 'name': f.name, 'account_id': f.account_id,
            'color': f.color, 'icon': f.icon, 'notes': f.notes,
            'posts_per_week': f.posts_per_week,
        } for f in folders],

        'content_items': [{
            'id': c.id, 'title': c.title, 'caption': c.caption,
            'status': c.status, 'content_type': c.content_type,
            'category': c.category.name if c.category else None,
            'folder': c.folder.name if c.folder else None,
            'source_url': c.source_url, 'source_name': c.source_name,
            'ai_caption': c.ai_caption, 'ai_score': c.ai_score,
            'media_count': len(c.media_items) if c.media_items else 0,
            'created_at': _date(c.created_at), 'updated_at': _date(c.updated_at),
        } for c in content_items],

        'scheduled_posts': [{
            'id': s.id, 'account_id': s.account_id,
            'post_type': s.post_type, 'status': s.status,
            'scheduled_at': _date(s.scheduled_at),
            'published_at': _date(s.published_at),
            'caption': s.caption,
        } for s in scheduled],

        'inspiration_posts': [{
            'id': i.id, 'source_url': i.source_url,
            'caption': i.caption, 'status': i.status,
            'like_count': i.like_count,
            'created_at': _date(i.created_at),
        } for i in inspiration],

        'kooperationen': [{
            'id': k.id, 'partner_name': k.partner_name,
            'koop_type': k.koop_type, 'status': k.status,
            'amount': float(k.amount) if k.amount else None,
            'currency': k.currency, 'deadline': _date(k.deadline),
            'notes': k.notes,
        } for k in koops],

        'serien': [{
            'id': s.id, 'name': s.name, 'account_id': s.account_id,
            'days_of_week': json.loads(s.days_of_week or '[]'),
            'preferred_time': s.preferred_time, 'post_type': s.post_type,
            'active': s.active,
        } for s in serien],

        'settings': {r.key: r.value for r in settings_rows},
    }

    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    # Backup-Zeitpunkt merken → Reminder-Alert (Render Free-DB Wipe nach 90 Tagen)
    set_setting('last_full_backup_at', datetime.utcnow().isoformat())
    db.session.commit()
    stamp = datetime.now().strftime('%Y-%m-%d_%H-%M')
    return Response(
        json_str, mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename=content-os-ALLES-{stamp}.json'}
    )


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
    folder_id   = request.form.get('folder_id', type=int)
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
            folder_id=folder_id,
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


@app.route('/api/media/<int:media_id>/generate-captions', methods=['POST'])
@login_required
def media_generate_captions(media_id):
    """Generiert 3 Caption-Vorschläge für ein Bild via Claude Vision."""
    media = MediaItem.query.get_or_404(media_id)
    if media.file_type != 'image':
        return jsonify({'ok': False, 'error': 'Nur für Bilder'}), 400

    d = request.get_json() or {}
    account_id = d.get('account_id')
    tone = 'informativ'
    context_text = d.get('context', '')

    account = Account.query.get(account_id) if account_id else None
    if account and account.ai_config:
        tone = account.ai_config.caption_tone or tone

    api_key = get_setting('anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key'}), 400

    try:
        import anthropic, base64, requests as req_lib
        client = anthropic.Anthropic(api_key=api_key)

        # Bild laden (URL oder Cloudinary)
        img_url = media.url
        resp = req_lib.get(img_url, timeout=10)
        img_b64 = base64.standard_b64encode(resp.content).decode('utf-8')
        mime = resp.headers.get('Content-Type', 'image/jpeg').split(';')[0]

        account_info = ''
        if account:
            account_info = f'Account: {account.name} | Ton: {tone}'
            if account.default_hashtags:
                account_info += f' | Hashtags: {account.default_hashtags}'

        prompt = f"""Du bist ein Social-Media-Texter. Erstelle 3 verschiedene Caption-Vorschläge für dieses Bild.

{account_info}
{('Kontext: ' + context_text) if context_text else ''}

Regeln:
- Max. 1–2 kurze Sätze (kurze Captions performen besser!)
- Passend zur Bildaussage
- Emojis erlaubt wenn sinnvoll
- Jede Caption einen anderen Stil: emotional / informativ / Call-to-Action

Antworte NUR mit diesem Format (3 Einträge, kein weiterer Text):
CAPTION_1: [Text]
CAPTION_2: [Text]
CAPTION_3: [Text]"""

        msg = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=600,
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': img_b64}},
                    {'type': 'text', 'text': prompt}
                ]
            }]
        )
        _log_ai('captions_batch', msg)
        raw = msg.content[0].text
        captions = []
        for line in raw.split('\n'):
            for prefix in ['CAPTION_1:', 'CAPTION_2:', 'CAPTION_3:']:
                if line.startswith(prefix):
                    captions.append(line[len(prefix):].strip())
        return jsonify({'ok': True, 'captions': captions[:3]})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/accounts/<int:account_id>/batch-schedule', methods=['POST'])
@login_required
def account_batch_schedule(account_id):
    """Kombination aus fixen Terminen + automatischer Verteilung.

    Erwartet JSON:
      items:     [{content_item_id, scheduled_at: 'YYYY-MM-DD' | null, caption: '...'}]
      post_time: '18:00'  (Uhrzeit für alle Posts, Default 18:00)
    """
    account = Account.query.get_or_404(account_id)
    d = request.get_json() or {}
    items     = d.get('items', [])
    post_time = d.get('post_time', '18:00')
    interval  = float(account.posting_interval_days or 1.0)

    try:
        h, m = map(int, post_time.split(':'))
    except Exception:
        h, m = 18, 0

    # ── Feste Termine sofort parsen ────────────────────────────
    fixed_items = []
    auto_items  = []
    for item in items:
        cid = item.get('content_item_id')
        if not cid:
            continue
        date_str = (item.get('scheduled_at') or '').strip()
        if date_str:
            try:
                sched_at = datetime.strptime(f'{date_str} {h:02d}:{m:02d}', '%Y-%m-%d %H:%M')
                fixed_items.append((item, sched_at))
            except ValueError:
                auto_items.append(item)
        else:
            auto_items.append(item)

    # ── Startpunkt für Auto-Posts: nach letztem geplanten Post ─
    last_post = (ScheduledPost.query
                 .filter_by(account_id=account_id, status='scheduled')
                 .order_by(ScheduledPost.scheduled_at.desc())
                 .first())
    if last_post:
        auto_start = last_post.scheduled_at + timedelta(days=interval)
        auto_start = auto_start.replace(hour=h, minute=m, second=0, microsecond=0)
    else:
        auto_start = datetime.utcnow().replace(hour=h, minute=m, second=0, microsecond=0)
        if auto_start <= datetime.utcnow():
            auto_start += timedelta(days=1)

    # Feste Datum-Strings als "belegt" markieren
    fixed_date_strings = {sched.strftime('%Y-%m-%d') for _, sched in fixed_items}

    # Auto-Slots generieren (feste Termine überspringen)
    auto_slots = []
    candidate  = auto_start
    while len(auto_slots) < len(auto_items):
        if candidate.strftime('%Y-%m-%d') not in fixed_date_strings:
            auto_slots.append(candidate)
        candidate += timedelta(days=interval)

    # ── Bulk-Load aller ContentItems ──────────────────────────
    all_ids = [it.get('content_item_id') for it in items if it.get('content_item_id')]
    ci_map  = {c.id: c for c in ContentItem.query.filter(ContentItem.id.in_(all_ids))
                                .options(selectinload(ContentItem.media_items)).all()}

    created = []

    def _make_post(item, sched_at, is_fixed):
        ci = ci_map.get(item.get('content_item_id'))
        if not ci:
            return
        caption = (item.get('caption') or '').strip() or ci.caption or ci.title or ''
        media   = ci.media_items[0] if ci.media_items else None
        post = ScheduledPost(
            account_id       = account_id,
            content_item_id  = ci.id,
            caption          = caption,
            post_type        = ci.content_type or 'feed',
            slot_type        = 'fixed',
            status           = 'scheduled',
            scheduled_at     = sched_at,
            media_item_id    = media.id  if media else None,
            media_ids        = json.dumps([m.id for m in ci.media_items]),
        )
        db.session.add(post)
        ci.status = 'scheduled'
        created.append({
            'date':    sched_at.strftime('%d.%m.%Y'),
            'weekday': ['Mo','Di','Mi','Do','Fr','Sa','So'][sched_at.weekday()],
            'title':   ci.title or '',
            'thumb':   media.url if media else None,
            'fixed':   is_fixed,
        })

    for item, sched_at in fixed_items:
        _make_post(item, sched_at, True)

    for item, slot in zip(auto_items, auto_slots):
        _make_post(item, slot, False)

    db.session.commit()
    log_activity('batch_scheduled',
                 f'{len(created)} Posts für {account.name} eingeplant '
                 f'({len(fixed_items)} fix, {len(auto_items)} auto)')
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

@app.route('/api/notifications/settings', methods=['GET'])
def api_notif_get():
    ns = get_notification_settings()
    return jsonify({'low_stock_days': ns.low_stock_days})

@app.route('/api/notifications/settings', methods=['POST'])
def api_notif_save():
    d = request.get_json()
    ns = get_notification_settings()
    ns.low_stock_days = int(d.get('low_stock_days', ns.low_stock_days))
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/notifications/test-alert', methods=['POST'])
def api_notif_test():
    _send_central_alert('⚠️ <b>Low-Stock: Test-Account</b>\nNur noch 2.0 Tage Vorrat')
    return jsonify({'ok': True})


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


def _fetch_ig_followers_rapidapi_batch(usernames, rapidapi_key, include_last_post=False):
    """
    Ruft Follower-Zahlen für eine Liste von Usernames via RapidAPI ab.
    Probiert mehrere bekannte Instagram-Scraper-APIs durch.
    Gibt {username_lower: followers_int}, [error_strings] zurück.
    """
    import json as _json

    # Kandidaten: (host, url, params_fn, followers_key_path)
    # followers_key_path: Liste von Keys die verschachtelt traversiert werden
    APIS = [
        ('instagram-scraper21.p.rapidapi.com',
         'https://instagram-scraper21.p.rapidapi.com/api/v1/info',
         lambda u: {'username': u},
         ['data', 'user', 'edge_followed_by', 'count']),
        ('instagram-scraper-api2.p.rapidapi.com',
         'https://instagram-scraper-api2.p.rapidapi.com/v1/info',
         lambda u: {'username_or_id_or_url': u},
         ['data', 'follower_count']),
        ('instagram-scraper-api2.p.rapidapi.com',
         'https://instagram-scraper-api2.p.rapidapi.com/v1.2/info',
         lambda u: {'username_or_id_or_url': u},
         ['data', 'follower_count']),
        ('instagram130.p.rapidapi.com',
         'https://instagram130.p.rapidapi.com/v1/info',
         lambda u: {'username_or_id_or_url': u},
         ['data', 'follower_count']),
        ('instagram-data1.p.rapidapi.com',
         'https://instagram-data1.p.rapidapi.com/user/info',
         lambda u: {'username': u},
         ['data', 'follower_count']),
        ('instagram47.p.rapidapi.com',
         'https://instagram47.p.rapidapi.com/getUser',
         lambda u: {'username': u},
         ['graphql', 'user', 'edge_followed_by', 'count']),
    ]

    def _dig(d, keys):
        """Traversiert ein verschachteltes Dict entlang keys."""
        for k in keys:
            if not isinstance(d, dict):
                return None
            d = d.get(k)
        return d

    def _extract_last_post(raw):
        for path in (['data','last_reel_media'], ['data','latest_reel_media'],
                     ['data','last_media_at'], ['data','user','last_reel_media']):
            val = _dig(raw, path)
            if isinstance(val, (int, float)) and val > 0:
                return int(val)
        for ep in (['data','user','edge_owner_to_timeline_media','edges'],
                   ['graphql','user','edge_owner_to_timeline_media','edges']):
            edges = _dig(raw, ep)
            if edges and isinstance(edges, list) and edges:
                ts = edges[0].get('node', {}).get('taken_at_timestamp')
                if isinstance(ts, (int, float)) and ts > 0:
                    return int(ts)
        return None

    # Finde die erste funktionierende API mit einem Test-Username
    working = None
    test_user = usernames[0] if usernames else 'instagram'
    for host, url, mk_params, key_path in APIS:
        try:
            hdrs = {'x-rapidapi-key': rapidapi_key, 'x-rapidapi-host': host}
            resp = req_lib.get(url, headers=hdrs, params=mk_params(test_user), timeout=15)
            if resp.status_code == 200:
                raw = resp.json()
                val = _dig(raw, key_path)
                if isinstance(val, int) and val > 0:
                    working = (host, url, mk_params, key_path)
                    break
        except Exception:
            continue

    if not working:
        err = ['RapidAPI: Keine funktionierende Instagram-User-Info-API gefunden. '
               'Bitte überprüfe dein Abonnement auf rapidapi.com.']
        return ({}, err, {}) if include_last_post else ({}, err)

    host, url, mk_params, key_path = working
    result, errors, last_post_map = {}, [], {}
    hdrs = {'x-rapidapi-key': rapidapi_key, 'x-rapidapi-host': host}

    for uname in usernames:
        try:
            resp = req_lib.get(url, headers=hdrs, params=mk_params(uname), timeout=15)
            if resp.status_code == 200:
                raw = resp.json()
                val = _dig(raw, key_path)
                if isinstance(val, int) and val > 0:
                    result[uname.lower()] = val
                    if include_last_post:
                        last_post_map[uname.lower()] = _extract_last_post(raw)
                else:
                    errors.append(f'@{uname}: keine Follower-Zahl in Antwort')
            elif resp.status_code == 429:
                errors.append(f'@{uname}: Rate-Limit — warte kurz')
                import time as _time; _time.sleep(2)
            else:
                errors.append(f'@{uname}: HTTP {resp.status_code}')
        except Exception as e:
            errors.append(f'@{uname}: {str(e)[:80]}')

    return (result, errors, last_post_map) if include_last_post else (result, errors)


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
    """Holt Follower-Zahlen für alle Accounts. Nutzt Apify, RapidAPI oder Direktzugriff."""
    global _ig_sync_status
    try:
        with app.app_context():
            method_row    = AppSettings.query.filter_by(key='ig_sync_method').first()
            token_row     = AppSettings.query.filter_by(key='apify_token').first()
            rapidapi_row  = AppSettings.query.filter_by(key='rapidapi_key').first()
            apify_token   = token_row.value if token_row and token_row.value else None
            rapidapi_key  = rapidapi_row.value if rapidapi_row and rapidapi_row.value else None

            # Methode: explizit gesetzt → nehmen; sonst: apify > rapidapi > direct
            if method_row and method_row.value:
                method = method_row.value
            elif apify_token:
                method = 'apify'
            elif rapidapi_key:
                method = 'rapidapi'
            else:
                method = 'direct'

            # Sanity checks
            if method == 'apify' and not apify_token:
                method = 'rapidapi' if rapidapi_key else 'direct'
            if method == 'rapidapi' and not rapidapi_key:
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

            usernames = [a.handle.lstrip('@') for a in accounts]
            acc_map   = {a.handle.lstrip('@').lower(): a for a in accounts}

            if method == 'apify':
                # ── Apify: alle auf einmal ──
                _ig_sync_status.update({'current': 'Apify-Scraper läuft…', 'progress': 10})
                followers_map, apify_errors = _fetch_ig_followers_apify_batch(usernames, apify_token)
                errors.extend(apify_errors)

            elif method == 'rapidapi':
                # ── RapidAPI: alle auf einmal mit bestehenden Instagram-Scraper-APIs ──
                _ig_sync_status.update({'current': 'RapidAPI Instagram-Scraper läuft…', 'progress': 10})
                followers_map, rap_errors = _fetch_ig_followers_rapidapi_batch(usernames, rapidapi_key)
                errors.extend(rap_errors)

            else:
                # ── Direkt: Account für Account mit Pause ──
                followers_map = {}
                for i, acc in enumerate(accounts):
                    username = acc.handle.lstrip('@')
                    _ig_sync_status.update({
                        'current': acc.name,
                        'progress': int((i / total) * 100),
                    })
                    count_val, err = _fetch_ig_followers_direct(username)
                    if err:
                        errors.append(err)
                        app.logger.warning(f'[IG Sync/Direct] {err}')
                    elif count_val:
                        followers_map[username.lower()] = count_val
                    if i < total - 1:
                        _time.sleep(1.5)

            # ── Gemeinsames Update ───────────────────────────────────
            for uname, followers in followers_map.items():
                acc = acc_map.get(uname)
                if acc:
                    old, delta = _set_follower_count(acc, followers)
                    updated_list.append({'name': acc.name, 'handle': uname,
                                         'old': old, 'new': followers, 'delta': delta})
                    app.logger.info(f'[IG Sync/{method.upper()}] @{uname}: {old}→{followers}')

            _ig_sync_status['progress'] = 95

            # Persistenter Zeitstempel des letzten echten Follower-Syncs —
            # die Analytics-Charts zeigen Tage erst, wenn sie hierdurch belegt sind.
            if updated_list:
                set_setting('last_follower_sync_at', datetime.utcnow().isoformat())
            db.session.commit()
            _growth_lab_daily_sync()

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

@app.route('/cron/sync-followers')
def cron_sync_followers():
    """Öffentlicher Cron-Endpunkt — kein Login nötig, aber Token-Schutz.
    Wird täglich um 23:59 von cron-job.org aufgerufen.
    Token in AppSettings key='cron_token' oder ENV CRON_TOKEN."""
    import os
    expected = os.environ.get('CRON_TOKEN') or get_setting('cron_token') or ''
    token = request.args.get('token', '')
    if not expected or token != expected:
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    if _ig_sync_status['running']:
        return jsonify({'ok': False, 'msg': 'Sync läuft bereits'}), 200
    _ig_sync_status.update({'running': True, 'error': None, 'result': None,
                             'progress': 0, 'current': ''})
    threading.Thread(target=_run_ig_follower_sync, daemon=True).start()
    _daily_follower_snapshot()
    return jsonify({'ok': True, 'msg': 'Follower-Sync gestartet', 'time': datetime.utcnow().isoformat()})


@app.route('/cron/morning-report')
def cron_morning_report():
    """Täglicher Morgen-Report an den Alert-Telegram-Channel (07:00 Berlin).
    Zeigt: Follower-Delta, nicht-gepostete Posts, spät-gepostete Posts (nach 22 Uhr)."""
    expected = os.environ.get('CRON_TOKEN') or get_setting('cron_token') or ''
    given = request.args.get('token', '')
    if not expected or given != expected:
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    tg_token = get_setting('alert_telegram_token') or get_setting('telegram_bot_token')
    chat_id  = get_setting('alert_central_chat_id')
    if not tg_token or not chat_id:
        return jsonify({'ok': False, 'error': 'Telegram nicht konfiguriert'})

    berlin    = ZoneInfo('Europe/Berlin')
    yesterday = (datetime.now(berlin) - timedelta(days=1)).date()

    # Nicht gepostet: gestern per Telegram geschickt, aber noch kein published_at
    not_posted = (ScheduledPost.query
        .filter(func.date(ScheduledPost.telegram_sent_at) == yesterday,
                ScheduledPost.status.notin_(['published', 'error']))
        .options(joinedload(ScheduledPost.account)).all())

    # Spät gepostet: published nach 22 Uhr Berliner Zeit
    posted_yesterday = (ScheduledPost.query
        .filter(func.date(ScheduledPost.telegram_sent_at) == yesterday,
                ScheduledPost.status == 'published',
                ScheduledPost.published_at.isnot(None))
        .options(joinedload(ScheduledPost.account)).all())
    late_posted = [p for p in posted_yesterday
                   if p.published_at.replace(tzinfo=timezone.utc).astimezone(berlin).hour >= 22]

    # Follower-Delta
    total_now = db.session.query(func.sum(Account.follower_count)).filter_by(status='active').scalar() or 0
    snap_sum  = db.session.query(func.sum(AnalyticsSnapshot.followers)).filter(
        func.date(AnalyticsSnapshot.recorded_at) == yesterday).scalar()
    delta     = (int(total_now) - int(snap_sum)) if snap_sum else None
    delta_str = (f'+{delta:,}' if delta >= 0 else f'{delta:,}') if delta is not None else '–'

    # Konfigurationsfehler: Accounts mit geplanten Posts aber ohne telegram_chat_id
    today = datetime.now(berlin).date()
    misconfigured = (db.session.query(Account)
        .join(ScheduledPost, ScheduledPost.account_id == Account.id)
        .filter(
            Account.status == 'active',
            Account.telegram_chat_id == None,
            Account.automation_level < 3,
            func.date(ScheduledPost.scheduled_at) == today,
            ScheduledPost.status == 'scheduled',
        ).distinct().all())

    lines = [f'🔔 <b>ContentOS — {yesterday.strftime("%d.%m.%Y")}</b>', '',
             f'📊 Follower gestern: <b>{delta_str}</b>', '']

    if not not_posted and not late_posted:
        lines.append('✅ Alle Posts wurden rechtzeitig gepostet')
    else:
        if not_posted:
            lines.append(f'⚠️ Nicht gepostet: <b>{len(not_posted)}</b>')
        if late_posted:
            lines.append(f'🌙 Nach 22 Uhr gepostet: <b>{len(late_posted)}</b>')

    if misconfigured:
        lines.append('')
        lines.append(f'⚙️ Kein Telegram-Channel ({len(misconfigured)}):')
        for acc in misconfigured[:10]:
            lines.append(f'  • {acc.name}')

    keyboard = []
    ds = yesterday.isoformat()
    if not_posted:
        keyboard.append([{'text': f'Details: {len(not_posted)} nicht gepostet',
                          'callback_data': f'morning_np_{ds}'}])
    if late_posted:
        keyboard.append([{'text': f'Details: {len(late_posted)} spät gepostet',
                          'callback_data': f'morning_lp_{ds}'}])

    payload = {'chat_id': chat_id, 'text': '\n'.join(lines), 'parse_mode': 'HTML'}
    if keyboard:
        payload['reply_markup'] = {'inline_keyboard': keyboard}

    try:
        import requests as _r
        r = _r.post(f'https://api.telegram.org/bot{tg_token}/sendMessage',
                    json=payload, timeout=10)
        return jsonify({'ok': r.json().get('ok', False)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


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
    apify_key    = AppSettings.query.filter_by(key='apify_token').first()
    rapidapi_key = AppSettings.query.filter_by(key='rapidapi_key').first()
    if apify_key and apify_key.value:
        method = 'apify'
    elif rapidapi_key and rapidapi_key.value:
        method = 'rapidapi'
    else:
        method = 'direct'
    return jsonify({'ok': True, 'total': count, 'method': method})


@app.route('/api/analytics/sync-followers-apify/status')
@login_required
def sync_followers_apify_status():
    return jsonify(_ig_sync_status)


# ── Integrationen-Seite ───────────────────────────────────────

@app.route('/settings/integrations', methods=['GET'])
@login_required
def integrations():
    return redirect(url_for('settings') + '#verbindungen')


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
    return redirect(url_for('settings') + '#verbindungen')


@app.route('/api/integrations/export')
@login_required
def integrations_export():
    """Export all API keys and integration settings as JSON for backup."""
    backup_keys = [
        'apify_token', 'ig_sync_method', 'ig_auto_sync',
        'telegram_bot_token', 'anthropic_api_key', 'rapidapi_key', 'cron_token',
        'invoice_sender_name', 'invoice_sender_street', 'invoice_sender_city',
        'invoice_sender_email', 'invoice_sender_phone', 'invoice_sender_iban',
        'invoice_sender_bic', 'invoice_sender_bank_name',
        'invoice_sender_tax_number', 'invoice_sender_vat_number',
        'invoice_sender_is_kleinunternehmer', 'invoice_prefix', 'invoice_payment_days',
        'notif_email', 'notif_days', 'notif_enabled',
        'koop_preisrechner_settings', 'todo_categories',
    ]
    data = {}
    for key in backup_keys:
        val = get_setting(key)
        if val is not None:
            # Skip binary logo data from backup (too large)
            if key == 'invoice_logo_b64':
                continue
            data[key] = val
    import json as _json
    payload = _json.dumps(data, ensure_ascii=False, indent=2)
    from flask import Response
    return Response(
        payload,
        mimetype='application/json',
        headers={'Content-Disposition': 'attachment; filename=content-os-keys-backup.json'}
    )


@app.route('/api/integrations/import', methods=['POST'])
@login_required
def integrations_import():
    """Restore API keys from a previously exported JSON backup."""
    f = request.files.get('backup_file')
    if not f:
        return jsonify({'ok': False, 'error': 'Keine Datei'}), 400
    try:
        import json as _json
        data = _json.loads(f.read())
    except Exception:
        return jsonify({'ok': False, 'error': 'Ungültige JSON-Datei'}), 400
    allowed = {
        'apify_token', 'ig_sync_method', 'ig_auto_sync',
        'telegram_bot_token', 'anthropic_api_key', 'rapidapi_key', 'cron_token',
        'invoice_sender_name', 'invoice_sender_street', 'invoice_sender_city',
        'invoice_sender_email', 'invoice_sender_phone', 'invoice_sender_iban',
        'invoice_sender_bic', 'invoice_sender_bank_name',
        'invoice_sender_tax_number', 'invoice_sender_vat_number',
        'invoice_sender_is_kleinunternehmer', 'invoice_prefix', 'invoice_payment_days',
        'notif_email', 'notif_days', 'notif_enabled',
        'koop_preisrechner_settings', 'todo_categories',
    }
    count = 0
    for key, value in data.items():
        if key in allowed and value is not None:
            set_setting(key, str(value))
            count += 1
    db.session.commit()
    return jsonify({'ok': True, 'restored': count})


@app.route('/api/cron-token/generate', methods=['POST'])
@login_required
def cron_token_generate():
    import secrets
    token = secrets.token_urlsafe(32)
    set_setting('cron_token', token)
    db.session.commit()
    return jsonify({'ok': True, 'token': token})


@app.route('/settings/telegram', methods=['POST'])
@login_required
def telegram_settings_save():
    token = request.form.get('telegram_bot_token', '').strip()
    s = AppSettings.query.filter_by(key='telegram_bot_token').first()
    if not s:
        s = AppSettings(key='telegram_bot_token')
        db.session.add(s)
    s.value = token
    db.session.commit()
    flash('Telegram Bot-Token gespeichert.', 'success')
    return redirect(url_for('settings') + '#verbindungen')


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


# ═══════════════════════════════════════════════════════════════
# ─────────────────── STADT-MEMES ───────────────────────────────
# ═══════════════════════════════════════════════════════════════

# Stadtprofile — fest im Code, kein DB-Aufwand.
# Claude bekommt diese Daten als Kontext um Memes akkurat zu adaptieren.
CITY_PROFILES = {
    'Frankfurt': {
        'emoji': '🏙️',
        'bundesland': 'Hessen',
        'spitznamen': ['Mainhattan', 'Bankfurt', 'Krankfurt'],
        'wahrzeichen': ['Römer', 'Frankfurter Dom', 'Skyline', 'EZB', 'Palmengarten', 'Alte Oper', 'Zeil'],
        'hauptplatz': 'Römerberg',
        'markt': 'Kleinmarkthalle',
        'stadtteile': ['Sachsenhausen', 'Bornheim', 'Nordend', 'Westend', 'Bockenheim', 'Gallus', 'Fechenheim'],
        'local_food': ['Grüne Soße', 'Handkäse mit Musik', 'Äpfelwein', 'Rippchen', 'Bembel'],
        'dialekt': ['Äppler', 'Stöffche', 'ei gude wie', 'Grie Soß', 'Bembel'],
        'verein': 'Eintracht Frankfurt (die Adler)',
        'humor': 'Banker-Klischees, Äpfelwein-Kultur, Römer-Touristen, S-Bahn Chaos, B-Ebene',
        'typisch': 'Jeder ist Banker oder kennt einen. Die Kleinmarkthalle ist heilig. S-Bahn immer zu spät.',
    },
    'Darmstadt': {
        'emoji': '🔬',
        'bundesland': 'Hessen',
        'spitznamen': ['Wissenschaftsstadt', 'Da', 'Dabbse', 'Stadt der Informatiker'],
        'wahrzeichen': ['Mathildenhöhe', 'Luisenplatz', 'Hessisches Landesmuseum', 'Waldspirale', 'Jagdschloss'],
        'hauptplatz': 'Luisenplatz',
        'markt': 'Wochenmarkt am Marktplatz',
        'stadtteile': ['Bessungen', 'Eberstadt', 'Arheilgen', 'Kranichstein', 'Wixhausen', 'Griesheim'],
        'local_food': ['Darmstädter Pils', 'Ebbelwoi aus dem Odenwald'],
        'dialekt': ['Dabbse', 'gell', 'des', 'Dabbse Ditschi'],
        'verein': 'SV Darmstadt 98 (die Lilien)',
        'humor': 'TU-Studenten, IT-Nerds, ESOC/ESA, Jugendstil, Kleinstadt-Großstadt-Komplex gegenüber Frankfurt',
        'typisch': 'Jeder studiert oder arbeitet an der TU. Die Lilien werden immer abgestiegen. Mathildenhöhe für Instagram.',
    },
    'Braunschweig': {
        'emoji': '🦁',
        'bundesland': 'Niedersachsen',
        'spitznamen': ['Löwenstadt', 'BS', 'Braunschweig die Löwenstadt'],
        'wahrzeichen': ['Braunschweiger Löwe', 'Burgplatz', 'Dom St. Blasii', 'Dankwarderode', 'Magniviertel'],
        'hauptplatz': 'Burgplatz / Hagenmarkt',
        'markt': 'Wochenmarkt am Kohlmarkt',
        'stadtteile': ['Innenstadt', 'Weststadt', 'Lehndorf', 'Stöckheim', 'Rühme', 'Gliesmarode'],
        'local_food': ['Mumme (Malzbier)', 'Braunschweiger Mettwurst', 'Leberwurst'],
        'dialekt': ['moin', 'nee', 'wat', 'Lowenkopp'],
        'verein': 'Eintracht Braunschweig',
        'humor': 'Dauerchaos mit VW nebenan, Niedersachsen-Provinz-Gefühl, Löwe ist überall, Hannover-Rivalität',
        'typisch': 'Der Löwe ist überall — auf Gebäuden, Autos, T-Shirts. TU Braunschweig Studenten. Hannover ist der Erzfeind.',
    },
    'Mainz': {
        'emoji': '🎭',
        'bundesland': 'Rheinland-Pfalz',
        'spitznamen': ['Meenz', 'Fassenacht-Hauptstadt', 'Gutenberg-Stadt'],
        'wahrzeichen': ['Mainzer Dom', 'Gutenberg-Museum', 'Schillerplatz', 'Zitadelle', 'Rheinufer'],
        'hauptplatz': 'Schillerplatz / Marktplatz',
        'markt': 'Wochenmarkt am Dom',
        'stadtteile': ['Altstadt', 'Gonsenheim', 'Bretzenheim', 'Hechtsheim', 'Mombach', 'Neustadt'],
        'local_food': ['Weck, Worscht un Woi', 'Mainzer Käse', 'Riesling', 'Fassenacht-Krapfen'],
        'dialekt': ['Meenz', 'Fassenacht', 'Woi statt Wein', 'un welle mer se noch emol lewe'],
        'verein': '1. FSV Mainz 05 (die Nullfünfer)',
        'humor': 'Fassenacht ist Religion, ZDF-Klischees, Grenzstadt zu Hessen, Rheinland-Pfalz vergisst Mainz',
        'typisch': 'Fassenacht > Weihnachten. Alle arbeiten beim ZDF oder der Unimedizin. Weck Worscht un Woi ist Lebensmotto.',
    },
    'Freiburg': {
        'emoji': '☀️',
        'bundesland': 'Baden-Württemberg',
        'spitznamen': ['Breisgau-Metropole', 'sonnigste Stadt Deutschlands', 'Öko-Hauptstadt'],
        'wahrzeichen': ['Freiburger Münster', 'Schlossberg', 'Martinstor', 'Schwarzwald', 'Bächle', 'Augustinerplatz'],
        'hauptplatz': 'Münsterplatz / Rathausplatz',
        'markt': 'Münstermarkt',
        'stadtteile': ['Altstadt', 'Wiehre', 'Stühlinger', 'Vauban', 'Haslach', 'Zähringen'],
        'local_food': ['Badischer Wein', 'Flammkuchen', 'Schwarzwälder Kirschtorte', 'Vesper'],
        'dialekt': ['Schneckle', 'noi', 'jo', 'des isch', 'Gäll', 'Bächle'],
        'verein': 'SC Freiburg (der Sportclub)',
        'humor': 'Bächle-Rein treten bringt Unglück (Heiratslegende), Öko-Hipster, immer Sonne, Vauban-Klischees',
        'typisch': 'Alle fahren Fahrrad. Wer ins Bächle tritt, heiratet einen Freiburger. SC Freiburg überperformt immer.',
    },
    'Hanau': {
        'emoji': '✨',
        'bundesland': 'Hessen',
        'spitznamen': ['Gebrüder-Grimm-Stadt', 'Goldschmiedestadt', 'Brüder Grimm Geburtsort'],
        'wahrzeichen': ['Brüder Grimm Nationaldenkmal', 'Schloss Philippsruhe', 'Freiheitsplatz', 'Goldschmiedehaus'],
        'hauptplatz': 'Freiheitsplatz / Marktplatz',
        'markt': 'Wochenmarkt Freiheitsplatz',
        'stadtteile': ['Innenstadt', 'Kesselstadt', 'Lamboy', 'Wolfgang', 'Steinheim', 'Großauheim'],
        'local_food': ['Apfelwein', 'Hessische Küche'],
        'dialekt': ['ei gude wie', 'gell', 'des'],
        'verein': 'KSV Hessen Kassel (regionale Verbindung)',
        'humor': 'Grimm-Märchen-Klischees, Frankfurt-Schatten (immer neben Frankfurt), Goldschmied-Nische',
        'typisch': 'Alle denken Hanau ist nur wegen Grimm bekannt. Frankfurt ist näher als München. Klein aber Hessen.',
    },
    'Köln': {
        'emoji': '⛪',
        'bundesland': 'Nordrhein-Westfalen',
        'spitznamen': ['Domstadt', 'Kölsch-Stadt', 'Veedel-Stadt', 'Metropole am Rhein'],
        'wahrzeichen': ['Kölner Dom', 'Hohenzollernbrücke', 'Rheinufer', '4711 Haus', 'KölnArena / Lanxess Arena'],
        'hauptplatz': 'Domplatz / Alter Markt',
        'markt': 'Kölner Wochenmarkt / Alter Markt',
        'stadtteile': ['Ehrenfeld', 'Nippes', 'Sülz', 'Rodenkirchen', 'Schäl Sick', 'Klettenberg', 'Mülheim'],
        'local_food': ['Kölsch (das Bier)', 'Halve Hahn', 'Kölscher Kaviar', 'Rheinischer Sauerbraten', 'Reibekuchen'],
        'dialekt': ['Veedel', 'Köbes', 'Kölsch', 'Mädche', 'Jecken', 'Jeck', 'Mer losse d\'r Dom en Kölle'],
        'verein': '1. FC Köln (der FC, die Geißböcke)',
        'humor': 'Karneval ist alles, Kölsch-Dialekt, Schäl Sick (rechtsrheinisch), Dom-Touristen, FC vs. Fortuna',
        'typisch': 'Kölsch trinkt man aus 0,2l Stangen. Jede Ecke ist ein Veedel. Karneval ist wichtiger als Silvester.',
    },
    'Hamburg': {
        'emoji': '⚓',
        'bundesland': 'Hamburg (Stadtstaat)',
        'spitznamen': ['Tor zur Welt', 'Hansestadt', 'Elphi-Stadt', 'Moinstadt'],
        'wahrzeichen': ['Elbphilharmonie', 'Speicherstadt', 'Hamburger Hafen', 'Michel (St. Michaelis)', 'Reeperbahn', 'Alster'],
        'hauptplatz': 'Rathausmarkt / Jungfernstieg',
        'markt': 'Fischmarkt (sonntags früh)',
        'stadtteile': ['Altona', 'Eimsbüttel', 'Barmbek', 'Blankenese', 'Harburg', 'Wandsbek', 'Winterhude'],
        'local_food': ['Fischbrötchen', 'Matjes', 'Labskaus', 'Franzbrötchen', 'Rote Grütze'],
        'dialekt': ['Moin', 'schnacken', 'Deern', 'Pegel', 'Digga', 'Moin moin (nur Touristen sagen das zweimal)'],
        'verein': 'HSV (Hamburger SV, der Dino) & FC St. Pauli (der Kiez)',
        'humor': 'Hanseatische Zurückhaltung, Regen immer, Fischmarkt sonntags um 5 Uhr morgens, HSV-Schmerz',
        'typisch': 'Hamburger sagen nur einmal Moin. HSV-Fan zu sein ist ein Lifestyle aus Schmerz. Franzbrötchen > Croissant.',
    },
}

_MEME_SYSTEM_PROMPT = """Du bist Experte für deutsche Stadt-Meme-Seiten auf Instagram.
Du kennst den typischen Humor jeder Stadt sehr genau: Lokale Witze, Klischees, Sehenswürdigkeiten, Dialekt.

Deine Aufgabe: Du bekommst eine fertige Meme-Caption für eine Stadt.
Adaptiere sie für jede andere Stadt so, dass sie sich wirklich lokal und authentisch anfühlt.
Swap ONLY die stadtspezifischen Referenzen aus — Wahrzeichen, lokale Orte, Dialektwörter, lokale Klischees.
Der grundlegende Witz/das Meme-Format bleibt EXAKT gleich.
Antworte NUR mit einem JSON-Objekt."""

_MEME_IMAGE_ANALYSIS_PROMPT = """Du bist Experte für deutsche Stadt-Meme-Seiten auf Instagram.
Du analysierst ein Meme-Template-Bild und recherchierst das perfekte lokale Äquivalent für andere Städte.

Deine Aufgabe:
1. Analysiere das Bild: Was zeigt es? Welcher Ort / welche Situation / welches Element ist der Kern des Memes?
2. Für jede Ziel-Stadt: Was wäre das exakte lokale Äquivalent? Konkret und spezifisch.

Format deiner Antwort — nur dieses JSON, kein anderer Text:
{
  "erkannt": "Kurze Beschreibung was du im Bild siehst und was der Kern-Witz/die Kern-Situation ist",
  "quell_referenz": "Das stadtspezifische Element aus der Quell-Stadt (z.B. 'Luisenplatz, Darmstadt')",
  "staedte": {
    "Frankfurt": {
      "ersatz": "Das lokale Äquivalent in Frankfurt (kurz, konkret)",
      "begruendung": "Warum das der perfekte Tausch ist",
      "canva_text": "Was du im Canva-Template statt dem Original eintragen solltest"
    }
  }
}"""


@app.route('/memes')
@login_required
def memes_dashboard():
    """Stadt-Memes: Template-Galerie + Caption-Adapt-Tool."""
    # Alle hochgeladenen Templates
    templates = MemeTemplate.query.order_by(MemeTemplate.created_at.desc()).all()

    # Für jedes Template: wie viele Varianten schon fertig?
    # + city→status Map für die Galerie-Dots
    total_cities = len(CITY_PROFILES)
    template_stats = {}
    template_variants = {}   # {template_id: {city: status}}
    for t in templates:
        vs = t.variants  # lazy='select' → bereits geladene Liste
        done  = sum(1 for v in vs if v.status == 'done')
        skip  = sum(1 for v in vs if v.status == 'skip')
        total = total_cities - 1  # ohne Quell-Stadt
        template_stats[t.id]   = {'done': done, 'skip': skip, 'total': total,
                                   'open': max(0, total - done - skip)}
        template_variants[t.id] = {v.city: v.status for v in vs}

    # Meme-Accounts
    meme_accounts = Account.query.filter(Account.status == 'active').filter(
        db.or_(Account.name.ilike('%meme%'), Account.name.ilike('%beicht%'),
               Account.name.ilike('%humor%'))
    ).order_by(Account.name).all()

    has_ai_key = bool(os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key'))

    events = LocalEvent.query.order_by(LocalEvent.datum.asc()).all()
    events_data = [{'id': e.id, 'name': e.name, 'city': e.city or '', 'datum': e.datum.isoformat() if e.datum else '',
                    'beschreibung': e.beschreibung or '', 'url': e.url or '', 'kategorie': e.kategorie,
                    'content_idee': e.content_idee or ''} for e in events]

    return render_template('memes.html',
        city_profiles=CITY_PROFILES,
        templates=templates,
        template_stats=template_stats,
        template_variants=template_variants,
        meme_accounts=meme_accounts,
        has_ai_key=has_ai_key,
        cities=list(CITY_PROFILES.keys()),
        events=events_data,
        active_page='memes')


@app.route('/memes/<int:template_id>')
@login_required
def meme_detail(template_id):
    """Detail-Ansicht eines Meme-Templates mit Städte-Checkliste."""
    tmpl = MemeTemplate.query.get_or_404(template_id)
    # Alle Varianten als Dict {city: MemeVariant}
    variant_map = {v.city: v for v in tmpl.variants}

    # Für Städte ohne Variant noch leere Placeholder
    all_cities = list(CITY_PROFILES.keys())
    other_cities = [c for c in all_cities if c != tmpl.source_city]

    # Statistik
    done_count = sum(1 for v in variant_map.values() if v.status == 'done')
    skip_count = sum(1 for v in variant_map.values() if v.status == 'skip')
    open_count = max(0, len(other_cities) - done_count - skip_count)

    has_ai_key = bool(os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key'))

    return render_template('meme_detail.html',
        tmpl=tmpl,
        variant_map=variant_map,
        other_cities=other_cities,
        city_profiles=CITY_PROFILES,
        done_count=done_count,
        skip_count=skip_count,
        open_count=open_count,
        has_ai_key=has_ai_key,
        active_page='memes')


@app.route('/api/memes/seasonal-calendar', methods=['POST'])
@login_required
def memes_seasonal_calendar():
    """Generiert saisonale Meme-Ideen für die nächsten 4 Wochen."""
    from datetime import date as _d
    d = request.get_json() or {}
    current_topics = d.get('current_topics', '')
    city = d.get('city', '')

    api_key = os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key konfiguriert.'}), 400

    today = _d.today()
    month_names = ['Januar','Februar','März','April','Mai','Juni',
                   'Juli','August','September','Oktober','November','Dezember']
    month_name = month_names[today.month - 1]

    city_context = ''
    if city and city in CITY_PROFILES:
        cp = CITY_PROFILES[city]
        city_context = f'Für: {city} ({cp.get("bundesland","")}) — {cp.get("typisch","")}'

    prompt = f"""Heute ist {today.strftime('%d. %B %Y')}.

Erstelle einen Saisonkalender mit Meme-Ideen für die nächsten 4 Wochen.
{city_context}
{('Aktuelle Themen: ' + current_topics) if current_topics else ''}

Für jede Woche: 3–4 konkrete Meme-Anlässe. Denke an:
- Feiertage, Schulferien, saisonale Events
- Wiederkehrende Kultur-Momente (Bundesliga-Spieltag, Tatort-Sonntag, etc.)
- Saisonale Klischees (Hitze, Regen, Herbst, etc.)
- Aktuell trendige Themen (wenn bekannt)

Format:
WOCHE_1: [Datum-Spanne]
- ANLASS: [Name] | MEME_IDEE: [konkrete Idee] | FORMAT: [POV/Vergleich/Ranking/etc.]
- ...

WOCHE_2: ...
WOCHE_3: ...
WOCHE_4: ..."""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=2000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        _log_ai('memes_seasonal', msg)
        raw = msg.content[0].text

        # Parse weeks
        weeks = []
        current_week = None
        for line in raw.split('\n'):
            line = line.strip()
            if line.startswith('WOCHE_'):
                if current_week:
                    weeks.append(current_week)
                parts = line.split(':', 1)
                current_week = {'label': parts[1].strip() if len(parts) > 1 else line, 'ideas': []}
            elif line.startswith('- ') and current_week is not None:
                idea_line = line[2:]
                idea = {}
                for part in idea_line.split('|'):
                    if ':' in part:
                        k, v = part.split(':', 1)
                        idea[k.strip().lower()] = v.strip()
                if idea:
                    current_week['ideas'].append(idea)
        if current_week:
            weeks.append(current_week)

        return jsonify({'ok': True, 'weeks': weeks, 'raw': raw})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/memes/adapt', methods=['POST'])
@login_required
def memes_adapt():
    """Claude adaptiert eine Meme-Caption für alle Städte."""
    d = request.get_json() or {}
    source_city    = d.get('source_city', '').strip()
    source_caption = d.get('caption', '').strip()
    target_cities  = d.get('target_cities', [c for c in CITY_PROFILES if c != source_city])
    meme_context   = d.get('meme_context') or {}   # {typ, kern, ton, zielgruppe}

    if not source_caption:
        return jsonify({'ok': False, 'error': 'Keine Caption eingegeben.'})
    if source_city not in CITY_PROFILES:
        return jsonify({'ok': False, 'error': f'Stadt "{source_city}" nicht bekannt.'})

    api_key = os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key konfiguriert. Bitte in Integrationen eintragen.'})

    # Stadtprofile für den Kontext aufbereiten
    profiles_text = ''
    for city in target_cities:
        if city not in CITY_PROFILES:
            continue
        p = CITY_PROFILES[city]
        profiles_text += f"""
{city} ({p['emoji']}):
  Spitznamen: {', '.join(p['spitznamen'])}
  Wahrzeichen: {', '.join(p['wahrzeichen'][:4])}
  Hauptplatz: {p['hauptplatz']}
  Markt/Halle: {p['markt']}
  Stadtteile: {', '.join(p['stadtteile'][:4])}
  Lokales Essen: {', '.join(p['local_food'][:3])}
  Dialekt-Wörter: {', '.join(p['dialekt'][:4])}
  Verein: {p['verein']}
  Typischer Humor: {p['humor']}
  Besonderheiten: {p['typisch']}
"""

    # Optionaler Meme-Kontext aus Upload-Fragen
    context_block = ''
    if meme_context:
        ctx_parts = []
        if meme_context.get('typ'):
            ctx_parts.append(f"Meme-Typ: {meme_context['typ']}")
        if meme_context.get('kern'):
            ctx_parts.append(f"Stadt-spezifisches Kern-Element (was ersetzt werden muss): {meme_context['kern']}")
        if meme_context.get('ton'):
            ctx_parts.append(f"Humor-Ton: {meme_context['ton']}")
        if meme_context.get('zielgruppe'):
            ctx_parts.append(f"Zielgruppe: {meme_context['zielgruppe']}")
        if ctx_parts:
            context_block = '\nKontext zu diesem Meme:\n' + '\n'.join(f'  - {p}' for p in ctx_parts) + '\n'

    user_prompt = f"""Quell-Stadt: {source_city}
Original-Caption:
\"\"\"{source_caption}\"\"\"{context_block}

Stadtprofil {source_city}:
  Wahrzeichen: {', '.join(CITY_PROFILES[source_city]['wahrzeichen'][:4])}
  Markt: {CITY_PROFILES[source_city]['markt']}
  Humor: {CITY_PROFILES[source_city]['humor']}

Ziel-Städte und ihre Profile:
{profiles_text}

Adaptiere die Caption für jede Ziel-Stadt. Antworte mit folgendem JSON:
{{
  "Frankfurt": "...",
  "Darmstadt": "...",
  usw.
}}

Nur die Städte in der Liste, kein extra Text, nur das JSON-Objekt."""

    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model='claude-opus-4-8',
            max_tokens=4096,
            system=_MEME_SYSTEM_PROMPT,
            messages=[{'role': 'user', 'content': user_prompt}]
        )
        _log_ai('meme_adapt', message)
        raw = message.content[0].text.strip()

        # JSON extrahieren (falls Claude Markdown-Blöcke drumherum schreibt)
        if '```' in raw:
            import re
            match = re.search(r'```(?:json)?\s*([\s\S]+?)\s*```', raw)
            raw = match.group(1) if match else raw
        result = json.loads(raw)

        # Quell-Stadt auch im Ergebnis ergänzen
        result[source_city] = source_caption

        return jsonify({'ok': True, 'results': result, 'source_city': source_city})

    except json.JSONDecodeError as e:
        return jsonify({'ok': False, 'error': f'Claude hat kein gültiges JSON zurückgegeben: {e}', 'raw': raw})
    except Exception as e:
        app.logger.error('memes_adapt error: %s', e)
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/memes/create-content', methods=['POST'])
@login_required
def memes_create_content():
    """Erstellt ContentItems aus adaptierten Meme-Captions."""
    d = request.get_json() or {}
    items_data = d.get('items', [])  # [{city, caption, account_id?}]
    meme_cat = Category.query.filter(Category.name.ilike('%meme%')).first()
    created = 0

    for item in items_data:
        city    = item.get('city', '').strip()
        caption = item.get('caption', '').strip()
        acc_id  = item.get('account_id')
        if not caption:
            continue
        ci = ContentItem(
            title=f'{city} Meme — {caption[:60]}{"…" if len(caption) > 60 else ""}',
            caption=caption,
            raw_text=caption,
            category_id=meme_cat.id if meme_cat else None,
            status='draft',
            content_type='feed',
            source_name=f'Meme-Generator ({city})',
        )
        db.session.add(ci)
        db.session.flush()
        if acc_id:
            acc = Account.query.get(acc_id)
            if acc:
                ci.accounts.append(acc)
        created += 1

    db.session.commit()
    return jsonify({'ok': True, 'created': created})


@app.route('/settings/anthropic', methods=['POST'])
@login_required
def anthropic_key_save():
    key = request.form.get('anthropic_api_key', '').strip()
    if not key:
        flash('Kein neuer Key eingegeben — bestehender Key bleibt erhalten.', 'info')
        return redirect(url_for('settings') + '#verbindungen')
    s = AppSettings.query.filter_by(key='anthropic_api_key').first()
    if not s:
        s = AppSettings(key='anthropic_api_key')
        db.session.add(s)
    s.value = key
    db.session.commit()
    flash('Anthropic API-Key gespeichert.', 'success')
    return redirect(url_for('settings') + '#verbindungen')


@app.route('/settings/rapidapi-key', methods=['POST'])
@login_required
def rapidapi_key_save():
    key = request.form.get('rapidapi_key', '').strip()
    if not key:
        flash('Kein neuer Key eingegeben — bestehender Key bleibt erhalten.', 'info')
        return redirect(url_for('settings') + '#verbindungen')
    s = AppSettings.query.filter_by(key='rapidapi_key').first()
    if not s:
        s = AppSettings(key='rapidapi_key')
        db.session.add(s)
    s.value = key
    db.session.commit()
    flash('RapidAPI-Key gespeichert.', 'success')
    return redirect(url_for('settings') + '#verbindungen')


# ── Meme Template Upload ──────────────────────────────────────
@app.route('/api/memes/template/upload', methods=['POST'])
@login_required
def meme_template_upload():
    """Lädt ein Meme-Template-Bild hoch und speichert es als MemeTemplate."""
    file = request.files.get('image')
    title = request.form.get('title', '').strip()
    source_city = request.form.get('source_city', '').strip()
    notes = request.form.get('notes', '').strip()

    # Kontext-Fragen
    meme_type      = request.form.get('meme_type', '').strip()
    core_element   = request.form.get('core_element', '').strip()
    humor_tone     = request.form.get('humor_tone', '').strip()
    target_audience = request.form.get('target_audience', '').strip()

    import json as _json
    meme_context = _json.dumps({
        'typ': meme_type,
        'kern': core_element,
        'ton': humor_tone,
        'zielgruppe': target_audience,
    }, ensure_ascii=False) if any([meme_type, core_element, humor_tone, target_audience]) else None

    if not file or not file.filename:
        return jsonify({'ok': False, 'error': 'Kein Bild angegeben.'})
    if source_city not in CITY_PROFILES:
        return jsonify({'ok': False, 'error': f'Stadt "{source_city}" nicht bekannt.'})

    # Auto-title aus Dateiname
    if not title:
        base = file.filename.rsplit('.', 1)[0].replace('_', ' ').replace('-', ' ')
        title = base[:100]

    # Cloudinary Upload
    result = _cloudinary_upload(file, file.filename)
    if not result:
        return jsonify({'ok': False, 'error': 'Cloudinary Upload fehlgeschlagen. Ist CLOUDINARY_URL gesetzt?'})

    tmpl = MemeTemplate(
        title=title,
        image_url=result.get('secure_url', ''),
        cloudinary_public_id=result.get('public_id', ''),
        source_city=source_city,
        notes=notes,
        meme_context=meme_context,
    )
    db.session.add(tmpl)
    db.session.commit()

    # Quell-Stadt-Variante sofort als "done" anlegen
    src_var = MemeVariant(
        template_id=tmpl.id,
        city=source_city,
        status='done',
        notes='Original-Vorlage',
    )
    db.session.add(src_var)
    db.session.commit()

    return jsonify({'ok': True, 'template_id': tmpl.id,
                    'image_url': tmpl.image_url, 'title': tmpl.title})


@app.route('/api/memes/template/<int:template_id>/delete', methods=['POST'])
@login_required
def meme_template_delete(template_id):
    """Löscht ein Meme-Template inkl. Cloudinary-Bild und allen Varianten."""
    tmpl = MemeTemplate.query.get_or_404(template_id)
    if tmpl.cloudinary_public_id:
        _cloudinary_delete(tmpl.cloudinary_public_id)
    db.session.delete(tmpl)
    db.session.commit()
    flash('Template gelöscht.', 'info')
    return redirect(url_for('memes_dashboard'))


# ── Meme Template Analysis (Claude multimodal) ───────────────
@app.route('/api/memes/<int:template_id>/analyse', methods=['POST'])
@login_required
def meme_template_analyse(template_id):
    """Claude analysiert das Bild und schlägt für jede Stadt das Äquivalent vor."""
    tmpl = MemeTemplate.query.get_or_404(template_id)
    if not tmpl.image_url:
        return jsonify({'ok': False, 'error': 'Kein Bild für dieses Template vorhanden.'})

    api_key = os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key konfiguriert.'})

    source_city = tmpl.source_city or ''
    other_cities = [c for c in CITY_PROFILES if c != source_city]

    # Stadtprofile für den Prompt
    profiles_text = ''
    for city in other_cities:
        p = CITY_PROFILES[city]
        profiles_text += f"""
{city} ({p['emoji']}):
  Wahrzeichen: {', '.join(p['wahrzeichen'][:5])}
  Hauptplatz: {p['hauptplatz']}
  Markt: {p['markt']}
  Stadtteile: {', '.join(p['stadtteile'][:3])}
  Lokales Essen: {', '.join(p['local_food'][:3])}
  Dialekt: {', '.join(p['dialekt'][:3])}
  Verein: {p['verein']}
  Humor: {p['humor']}
  Typisch: {p['typisch']}
"""

    user_text = f"""Das ist ein Meme-Template für {source_city}.

Quell-Stadt-Profil ({source_city}):
  Wahrzeichen: {', '.join(CITY_PROFILES[source_city]['wahrzeichen'][:5]) if source_city in CITY_PROFILES else 'unbekannt'}
  Hauptplatz: {CITY_PROFILES[source_city]['hauptplatz'] if source_city in CITY_PROFILES else ''}
  Humor: {CITY_PROFILES[source_city]['humor'] if source_city in CITY_PROFILES else ''}

Ziel-Städte und ihre Profile:
{profiles_text}

Analysiere das Bild und erstelle für jede dieser Städte einen konkreten Vorschlag:
{json.dumps(other_cities)}

Antworte NUR mit dem JSON-Objekt (kein Markdown, kein anderer Text)."""

    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model='claude-opus-4-8',
            max_tokens=6000,
            system=_MEME_IMAGE_ANALYSIS_PROMPT,

            messages=[{
                'role': 'user',
                'content': [
                    {
                        'type': 'image',
                        'source': {
                            'type': 'url',
                            'url': tmpl.image_url,
                        }
                    },
                    {'type': 'text', 'text': user_text}
                ]
            }]
        )
        _log_ai('meme_analyse', message)
        raw = message.content[0].text.strip()

        # JSON extrahieren
        if '```' in raw:
            import re
            match = re.search(r'```(?:json)?\s*([\s\S]+?)\s*```', raw)
            raw = match.group(1) if match else raw

        result = json.loads(raw)

        # Varianten speichern / aktualisieren
        staedte_data = result.get('staedte', {})
        for city, data in staedte_data.items():
            if city not in CITY_PROFILES:
                continue
            suggestion_text = (
                f"**Ersatz:** {data.get('ersatz', '')}\n"
                f"**Warum:** {data.get('begruendung', '')}\n"
                f"**Canva-Text:** {data.get('canva_text', '')}"
            )
            existing = MemeVariant.query.filter_by(
                template_id=template_id, city=city
            ).first()
            if existing:
                existing.suggestion = suggestion_text
                existing.updated_at = datetime.utcnow()
            else:
                db.session.add(MemeVariant(
                    template_id=template_id,
                    city=city,
                    status='pending',
                    suggestion=suggestion_text,
                ))
        db.session.commit()

        return jsonify({
            'ok': True,
            'erkannt': result.get('erkannt', ''),
            'quell_referenz': result.get('quell_referenz', ''),
            'staedte': staedte_data,
            'cities_updated': len(staedte_data),
        })

    except json.JSONDecodeError as e:
        return jsonify({'ok': False, 'error': f'Claude JSON-Fehler: {e}', 'raw': raw[:500]})
    except Exception as e:
        app.logger.error('meme_analyse error: %s', e)
        return jsonify({'ok': False, 'error': str(e)})


# ── Meme Variant Status Update ────────────────────────────────
@app.route('/api/memes/<int:template_id>/variant/<city>/update', methods=['POST'])
@login_required
def meme_variant_update(template_id, city):
    """Aktualisiert Status und/oder Notizen einer Meme-Variante."""
    MemeTemplate.query.get_or_404(template_id)  # 404 if template missing
    d = request.get_json() or {}
    new_status = d.get('status')   # pending / done / skip
    notes = d.get('notes')

    variant = MemeVariant.query.filter_by(template_id=template_id, city=city).first()
    if not variant:
        variant = MemeVariant(template_id=template_id, city=city, status='pending')
        db.session.add(variant)

    if new_status in ('pending', 'done', 'skip'):
        variant.status = new_status
    if notes is not None:
        variant.notes = notes
    variant.updated_at = datetime.utcnow()
    db.session.commit()

    return jsonify({'ok': True, 'status': variant.status})


# ── Meme Variant Notes Save ───────────────────────────────────
@app.route('/api/memes/<int:template_id>/variant/<city>/notes', methods=['POST'])
@login_required
def meme_variant_notes(template_id, city):
    """Speichert nur die Notizen einer Variante."""
    MemeTemplate.query.get_or_404(template_id)
    notes = (request.get_json() or {}).get('notes', '')
    variant = MemeVariant.query.filter_by(template_id=template_id, city=city).first()
    if not variant:
        variant = MemeVariant(template_id=template_id, city=city, status='pending')
        db.session.add(variant)
    variant.notes = notes
    variant.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})


# ─────────────────────── INSPIRATIONEN ────────────────────────

@app.route('/inspirationen')
@login_required
def inspirationen():
    sources = InspirationSource.query.order_by(InspirationSource.username).all()
    status_filter  = request.args.get('status', 'new')
    source_filter  = request.args.get('source', type=int)
    account_filter = request.args.get('account', type=int)   # alle Quellen dieses Accounts
    sort_by        = request.args.get('sort', 'date_desc')
    min_likes      = request.args.get('min_likes', type=int)
    date_from_str  = request.args.get('date_from', '')
    date_to_str    = request.args.get('date_to', '')

    # Quellen nach Account gruppieren (für Sidebar)
    from collections import defaultdict
    sources_by_account = defaultdict(list)   # account_id → [sources]
    for s in sources:
        sources_by_account[s.account_id].append(s)
    # Accounts mit Quellen laden (für Gruppen-Header)
    account_ids_with_sources = [aid for aid in sources_by_account if aid]
    accounts_with_sources = {
        a.id: a for a in Account.query.filter(Account.id.in_(account_ids_with_sources)).all()
    } if account_ids_with_sources else {}

    q = InspirationPost.query
    if status_filter == 'saved':
        # Gespeichert = is_saved=True (unabhängig vom status)
        q = q.filter(InspirationPost.is_saved == True)
    elif status_filter and status_filter != 'all':
        q = q.filter(InspirationPost.status == status_filter)
    if source_filter:
        q = q.filter_by(source_id=source_filter)
    elif account_filter:
        # Alle Quellen dieses Accounts
        src_ids = [s.id for s in sources if s.account_id == account_filter]
        if src_ids:
            q = q.filter(InspirationPost.source_id.in_(src_ids))
        else:
            q = q.filter(db.false())
    if min_likes:
        q = q.filter(InspirationPost.like_count >= min_likes)
    if date_from_str:
        try:
            q = q.filter(InspirationPost.post_date >= datetime.fromisoformat(date_from_str))
        except Exception:
            pass
    if date_to_str:
        try:
            q = q.filter(InspirationPost.post_date <= datetime.fromisoformat(date_to_str + 'T23:59:59'))
        except Exception:
            pass

    if sort_by == 'likes_desc':
        q = q.order_by(InspirationPost.like_count.desc().nulls_last(),
                       InspirationPost.post_date.desc())
    elif sort_by == 'date_asc':
        q = q.order_by(InspirationPost.post_date.asc())
    else:  # date_desc (default)
        q = q.order_by(InspirationPost.post_date.desc())

    posts = q.limit(300).all()

    counts = {
        'new':     InspirationPost.query.filter_by(status='new').count(),
        'saved':   InspirationPost.query.filter(InspirationPost.is_saved == True).count(),
        'ignored': InspirationPost.query.filter_by(status='ignored').count(),
        'used':    InspirationPost.query.filter_by(status='used').count(),
    }

    has_rapidapi_key = bool(get_setting('rapidapi_key'))
    all_accounts = Account.query.order_by(Account.name).all()
    all_folders  = ContentFolder.query.order_by(ContentFolder.sort_order, ContentFolder.name).all()

    # ── Status-Zählungen pro Quelle (eine einzige Query) ─────────────
    _st_rows = db.session.query(
        InspirationPost.source_id,
        InspirationPost.status,
        func.count(InspirationPost.id)
    ).group_by(InspirationPost.source_id, InspirationPost.status).all()
    source_counts = defaultdict(lambda: {'new': 0, 'saved': 0, 'ignored': 0, 'used': 0})
    for _sid, _st, _cnt in _st_rows:
        if _st in ('new', 'ignored', 'used'):
            source_counts[_sid][_st] = _cnt
    # saved = is_saved=True (unabhängig vom status)
    _sv_rows = db.session.query(
        InspirationPost.source_id,
        func.count(InspirationPost.id)
    ).filter(InspirationPost.is_saved == True).group_by(InspirationPost.source_id).all()
    for _sid, _cnt in _sv_rows:
        source_counts[_sid]['saved'] = _cnt

    # ── Gruppen-Counts (aggregiert aus Quellen-Counts) ────────────────
    account_src_counts = defaultdict(lambda: {'new': 0, 'saved': 0, 'ignored': 0, 'used': 0})
    for _src in sources:
        if _src.account_id:
            for _st in ('new', 'saved', 'ignored', 'used'):
                account_src_counts[_src.account_id][_st] += source_counts[_src.id][_st]

    return render_template('inspirationen.html',
        sources=sources, posts=posts, counts=counts,
        sources_by_account=dict(sources_by_account),
        accounts_with_sources=accounts_with_sources,
        source_counts={k: dict(v) for k, v in source_counts.items()},
        account_src_counts={k: dict(v) for k, v in account_src_counts.items()},
        status_filter=status_filter, source_filter=source_filter,
        account_filter=account_filter,
        sort_by=sort_by, min_likes=min_likes or '',
        date_from=date_from_str, date_to=date_to_str,
        has_rapidapi_key=has_rapidapi_key,
        all_accounts=all_accounts,
        all_folders=all_folders,
        now=datetime.utcnow(),
        active_page='inspirationen')


@app.route('/api/inspirationen/sources', methods=['POST'])
@login_required
def inspiration_source_add():
    """Neue Instagram-Quelle hinzufügen."""
    d = request.get_json() or {}
    username = (d.get('username') or '').strip().lstrip('@').lower()
    if not username:
        return jsonify({'ok': False, 'error': 'Username fehlt'})
    if InspirationSource.query.filter_by(username=username).first():
        return jsonify({'ok': False, 'error': f'@{username} wird bereits beobachtet.'})
    src = InspirationSource(username=username, notes=d.get('notes', ''))
    db.session.add(src)
    db.session.commit()
    return jsonify({'ok': True, 'id': src.id, 'username': src.username})


@app.route('/api/inspirationen/sources/<int:src_id>', methods=['DELETE'])
@login_required
def inspiration_source_delete(src_id):
    src = InspirationSource.query.get_or_404(src_id)
    db.session.delete(src)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/inspirationen/sources/<int:src_id>/account', methods=['POST'])
@login_required
def inspiration_source_set_account(src_id):
    """Standard-Account für eine Inspirations-Quelle setzen."""
    src = InspirationSource.query.get_or_404(src_id)
    d   = request.get_json() or {}
    account_id = d.get('account_id') or None
    if account_id:
        acc = Account.query.get(account_id)
        if not acc:
            return jsonify({'ok': False, 'error': 'Account nicht gefunden'})
    src.account_id = account_id
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/inspirationen/fetch/<int:src_id>', methods=['POST'])
@login_required
def inspiration_fetch(src_id):
    """Holt ALLE Posts einer Quelle via RapidAPI mit Pagination.
    Apify bleibt dem Follower-Sync vorbehalten (Free-Tier schonen).
    """
    import requests as req_lib

    src          = InspirationSource.query.get_or_404(src_id)
    rapidapi_key = get_setting('rapidapi_key')

    if not rapidapi_key:
        return jsonify({'ok': False,
                        'error': 'Kein RapidAPI-Key konfiguriert. '
                                 'Bitte in Einstellungen → Integrationen eintragen.'})

    # ── Alle bekannten Instagram-Scraper-APIs auf RapidAPI ────────
    # Der Key funktioniert automatisch für die APIs, die der User abonniert hat.
    # Wir probieren alle durch bis einer antwortet (kein 403/404).
    CANDIDATE_APIS = [
        # ── Abonnierte API (instagram-scraper21) — zuerst ─────────
        ('instagram-scraper21.p.rapidapi.com',
         'https://instagram-scraper21.p.rapidapi.com/api/v1/posts',
         lambda u, c: {'username': u, 'limit': '100', 'include_captions': 'true',
                       **({'cursor': c} if c else {})}),
        # ── Fallbacks ─────────────────────────────────────────────
        ('instagram-scraper-api2.p.rapidapi.com',
         'https://instagram-scraper-api2.p.rapidapi.com/v1/posts',
         lambda u, c: {'username_or_id_or_url': u, **({'cursor': c} if c else {})}),
        ('instagram-scraper-api2.p.rapidapi.com',
         'https://instagram-scraper-api2.p.rapidapi.com/v1.2/posts',
         lambda u, c: {'username_or_id_or_url': u, **({'cursor': c} if c else {})}),
        ('instagram-looter2.p.rapidapi.com',
         'https://instagram-looter2.p.rapidapi.com/feed-by-username',
         lambda u, c: {'username': u, 'count': '50'}),
        ('instagram47.p.rapidapi.com',
         'https://instagram47.p.rapidapi.com/getMediaByUsername',
         lambda u, c: {'username': u}),
        ('instagram-data1.p.rapidapi.com',
         'https://instagram-data1.p.rapidapi.com/user/posts',
         lambda u, c: {'username': u, **({'cursor': c} if c else {})}),
        ('instagram130.p.rapidapi.com',
         'https://instagram130.p.rapidapi.com/v1/posts',
         lambda u, c: {'username_or_id_or_url': u, **({'cursor': c} if c else {})}),
        ('rocketapi-for-instagram.p.rapidapi.com',
         'https://rocketapi-for-instagram.p.rapidapi.com/instagram/user/get_media',
         lambda u, c: {'username': u, **({'cursor': c} if c else {})}),
        ('instagram-scraper3.p.rapidapi.com',
         'https://instagram-scraper3.p.rapidapi.com/user/posts',
         lambda u, c: {'username': u}),
        ('instagram-api-2022.p.rapidapi.com',
         'https://instagram-api-2022.p.rapidapi.com/api/user/posts',
         lambda u, c: {'username': u}),
    ]

    def _extract_items(raw):
        data_block = raw.get('data') or {}
        if isinstance(data_block, list) and data_block:
            return data_block, None, False
        page_items = (
            data_block.get('items') or data_block.get('posts')
            or data_block.get('edges')
            or raw.get('items') or raw.get('posts') or raw.get('edges')
            or (raw if isinstance(raw, list) else [])
        ) or []
        cursor = (
            data_block.get('end_cursor') or data_block.get('next_cursor')
            or data_block.get('pagination_token')
            or raw.get('end_cursor') or raw.get('next_cursor')
        )
        has_next = bool(
            data_block.get('has_next_page') or data_block.get('more_available')
            or raw.get('has_next_page') or raw.get('more_available') or cursor
        )
        return page_items, cursor, has_next

    items     = []
    last_err  = ''
    working_api = None

    # Schritt 1: Welche API antwortet? (erste Seite)
    for host, url, mk_params in CANDIDATE_APIS:
        try:
            hdrs = {'x-rapidapi-key': rapidapi_key, 'x-rapidapi-host': host}
            resp = req_lib.get(url, headers=hdrs, params=mk_params(src.username, None), timeout=20)
            if resp.status_code == 200:
                raw = resp.json()
                page_items, cursor, has_next = _extract_items(raw)
                if page_items:
                    items.extend(page_items)
                    working_api = (host, url, mk_params, cursor, has_next)
                    break
            else:
                last_err = f'HTTP {resp.status_code} ({host})'
        except Exception as e:
            last_err = str(e)

    # Schritt 2: Weitere Seiten mit der funktionierenden API laden
    if working_api:
        host, url, mk_params, cursor, has_next = working_api
        hdrs = {'x-rapidapi-key': rapidapi_key, 'x-rapidapi-host': host}
        for _ in range(49):   # Max 50 Seiten gesamt
            if not has_next or not cursor:
                break
            try:
                resp = req_lib.get(url, headers=hdrs, params=mk_params(src.username, cursor), timeout=20)
                if resp.status_code != 200:
                    break
                raw = resp.json()
                page_items, cursor, has_next = _extract_items(raw)
                if not page_items:
                    break
                items.extend(page_items)
            except Exception:
                break

    if not items:
        return jsonify({'ok': False,
                        'error': f'Keine Posts geladen. Bitte prüfe ob du auf RapidAPI '
                                 f'einen Instagram-Scraper abonniert hast. ({last_err})'})

    # ── Hilfsfunktion: Video-URL extrahieren ──────────────────
    def _extract_video_url(item):
        # scraper21: video = [{url, width, height}]
        v = item.get('video')
        if isinstance(v, list) and v:
            first = v[0]
            if isinstance(first, dict):
                u = first.get('url') or first.get('videoUrl')
                if u and u.startswith('http'): return u
            elif isinstance(first, str) and first.startswith('http'):
                return first
        if isinstance(v, str) and v.startswith('http'):
            return v
        # Andere API-Formate
        for key in ('video_url', 'videoUrl', 'video_versions', 'video_resources'):
            vv = item.get(key)
            if isinstance(vv, str) and vv.startswith('http'):
                return vv
            if isinstance(vv, list) and vv:
                first = vv[0]
                u = first.get('url') if isinstance(first, dict) else str(first)
                if u.startswith('http'): return u
        return None

    # ── Hilfsfunktion: Bild-URL extrahieren ───────────────────
    def _extract_img(item):
        # instagram-scraper21: image = [{url, height, width}, ...]
        img_list = item.get('image')
        if isinstance(img_list, list) and img_list:
            best = img_list[0].get('url', '') if isinstance(img_list[0], dict) else str(img_list[0])
            thumb = img_list[-1].get('url', best) if isinstance(img_list[-1], dict) else best
            if best.startswith('http'):
                return best, thumb
        # Andere APIs: image_versions2 / image_versions
        for key in ('image_versions2', 'image_versions'):
            iv_items = (item.get(key) or {}).get('items') or []
            if iv_items:
                return iv_items[0].get('url'), iv_items[-1].get('url', iv_items[0].get('url'))
        # Direkte URL-Felder
        for key in ('displayUrl', 'display_url', 'thumbnail_url', 'image_url', 'url'):
            v = item.get(key)
            if isinstance(v, str) and v.startswith('http'):
                return v, v
            if isinstance(v, list) and v:
                u = v[0].get('url', '') if isinstance(v[0], dict) else str(v[0])
                if u.startswith('http'):
                    return u, u
        # Carousel: erstes Bild nehmen
        cm = item.get('carousel_media') or item.get('images') or []
        if cm:
            first = cm[0] if isinstance(cm[0], dict) else None
            if first:
                return _extract_img(first)
            if isinstance(cm[0], str) and cm[0].startswith('http'):
                return cm[0], cm[0]
        return None, None

    # ── Posts verarbeiten (kein künstliches Limit) ─────────────
    new_count = 0
    for item in items:
        code = str(item.get('shortCode') or item.get('code') or
                   item.get('shortcode') or item.get('id') or '')
        if not code:
            continue
        # Likes / Kommentare — verschiedene API-Feldnamen abdecken
        def _int_or_none(val):
            try: return int(val) if val is not None else None
            except: return None

        raw_likes = (item.get('likeCount') or item.get('like_count') or
                     item.get('likes') or item.get('likes_count') or
                     (item.get('edge_media_to_like') or {}).get('count'))
        raw_comments = (item.get('commentsCount') or item.get('comment_count') or
                        item.get('comments') or
                        (item.get('edge_media_to_comment') or {}).get('count'))
        like_val    = _int_or_none(raw_likes)
        comment_val = _int_or_none(raw_comments)

        existing = InspirationPost.query.filter_by(instagram_code=code).first()
        if existing:
            # Likes/Kommentare bei bestehenden Posts aktualisieren (falls neu verfügbar)
            if like_val is not None:
                existing.like_count    = like_val
            if comment_val is not None:
                existing.comment_count = comment_val
            continue

        img_url, thumb_url = _extract_img(item)
        if not img_url:
            continue

        # Karussel: alle Bild-URLs sammeln
        carousel_urls_json = None
        if media_type == 'carousel':
            cm = item.get('carousel_media') or item.get('images') or []
            all_urls = []
            for slide in cm:
                if isinstance(slide, str) and slide.startswith('http'):
                    all_urls.append(slide)
                elif isinstance(slide, dict):
                    u, _ = _extract_img(slide)
                    if u:
                        all_urls.append(u)
            if all_urls:
                carousel_urls_json = json.dumps(all_urls)

        # Caption wird bewusst NICHT gespeichert (nur eigene Texte verwenden)
        caption = ''

        # Datum — Apify liefert ISO-String, RapidAPI Unix-Timestamp
        post_date = None
        ts = item.get('timestamp') or item.get('taken_at') or item.get('taken_at_timestamp')
        if ts:
            try:
                if isinstance(ts, str):
                    post_date = datetime.fromisoformat(ts.replace('Z', '+00:00')).replace(tzinfo=None)
                else:
                    post_date = datetime.utcfromtimestamp(int(ts))
            except Exception:
                pass

        # Typ — scraper21: product_type ("clips"/"feed"), video=[{url}]
        type_str    = str(item.get('type') or item.get('product_type') or '').lower()
        mt          = item.get('media_type', 1)
        has_video   = bool(item.get('video'))
        carousel_ct = len(item.get('carousel_media') or item.get('images') or [])
        if type_str in ('video', 'clips', 'reel') or has_video or mt == 2:
            media_type = 'video'
        elif type_str == 'sidecar' or mt == 8 or carousel_ct > 1:
            media_type = 'carousel'
        else:
            media_type = 'image'

        video_url = _extract_video_url(item) if media_type == 'video' else None

        post = InspirationPost(
            source_id=src.id, instagram_code=code,
            image_url=img_url, thumbnail_url=thumb_url or img_url,
            caption=caption, post_date=post_date,
            media_type=media_type, status='new',
            carousel_urls=carousel_urls_json,
            video_url=video_url,
            like_count=like_val,
            comment_count=comment_val,
        )
        db.session.add(post)
        new_count += 1

    src.last_fetch = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'new': new_count,
                    'message': f'{new_count} neue Posts von @{src.username} geladen.'})


@app.route('/api/inspirationen/<int:post_id>/status', methods=['POST'])
@login_required
def inspiration_post_status(post_id):
    """Status eines Inspirations-Posts ändern: new | ignored | used.
    'saved' wird über /save gehandelt (is_saved-Flag).
    """
    post   = InspirationPost.query.get_or_404(post_id)
    status = (request.get_json() or {}).get('status', 'new')
    if status not in ('new', 'saved', 'ignored', 'used'):
        return jsonify({'ok': False, 'error': 'Ungültiger Status'})
    if status == 'saved':
        # Legacy: als Bookmark behandeln
        post.is_saved = True
    else:
        post.status = status
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/inspirationen/<int:post_id>/save', methods=['POST'])
@login_required
def inspiration_post_save(post_id):
    """Inspo-Lesezeichen togglen (is_saved). Unabhängig vom status."""
    post = InspirationPost.query.get_or_404(post_id)
    post.is_saved = not post.is_saved
    db.session.commit()
    return jsonify({'ok': True, 'is_saved': post.is_saved})


@app.route('/api/inspirationen/<int:post_id>/lock-folder', methods=['POST'])
@login_required
def inspiration_post_lock_folder(post_id):
    """Setzt folder_locked=True + speichert manuell gewählten Ordner als suggested_folder_id.
    Damit überschreibt der KI-Batch-Prozess diesen Post nicht mehr.
    """
    post = InspirationPost.query.get_or_404(post_id)
    d    = request.get_json() or {}
    folder_id = d.get('folder_id')
    post.folder_locked       = True
    post.suggested_folder_id = int(folder_id) if folder_id else None
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/inspirationen/<int:post_id>', methods=['DELETE'])
@login_required
def inspiration_post_delete(post_id):
    """Einen Inspirations-Post dauerhaft löschen."""
    post = InspirationPost.query.get_or_404(post_id)
    db.session.delete(post)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/inspirationen/<int:post_id>/suggest-folder', methods=['POST'])
@login_required
def inspiration_suggest_folder(post_id):
    """KI-gestützte Ordner-Zuordnung via _classify_post_folder() Hilfsfunktion.
    Body: { account_id: int|null }
    """
    post = InspirationPost.query.get_or_404(post_id)
    d    = request.get_json() or {}
    account_id = d.get('account_id')

    api_key = os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key konfiguriert.'})

    if account_id:
        folders = ContentFolder.query.filter(
            db.or_(ContentFolder.account_id == int(account_id),
                   ContentFolder.account_id.is_(None))
        ).order_by(ContentFolder.name).all()
    else:
        folders = ContentFolder.query.order_by(ContentFolder.name).all()

    if not folders:
        return jsonify({'ok': False, 'error': 'Keine Ordner vorhanden. Lege zuerst Ordner an.'})

    try:
        result = _classify_post_folder(post, folders, api_key)
    except json.JSONDecodeError:
        return jsonify({'ok': False, 'error': 'KI-Antwort konnte nicht geparst werden.'})
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Claude-Fehler: {e}'})

    # Vorschlag speichern wenn noch nicht gesetzt und nicht gesperrt
    if result.get('folder_id') and not post.folder_locked and not post.suggested_folder_id:
        post.suggested_folder_id = result['folder_id']
        db.session.commit()

    return jsonify({'ok': True, **result})


@app.route('/api/inspirationen/<int:post_id>/use', methods=['POST'])
@login_required
def inspiration_post_use(post_id):
    """
    Lädt das Bild eines Inspiration-Posts zu Cloudinary hoch und erstellt
    ein ContentItem daraus.
    Body-Parameter:
      account_id  – Ziel-Account (optional)
      mode        – 'reserve' (Vorrat, draft) | 'ready' (bereit, sofort einplanbar)
      caption     – ggf. umformulierte Caption
    """
    import requests as req_lib

    post = InspirationPost.query.get_or_404(post_id)
    data = request.get_json() or {}
    account_id   = data.get('account_id') or None
    folder_id    = data.get('folder_id') or None
    mode         = data.get('mode', 'reserve')   # 'reserve' | 'ready' | 'schedule'
    caption      = data.get('caption') or post.caption or ''
    scheduled_at_raw = data.get('scheduled_at')  # ISO datetime string oder None

    scheduled_at = None
    if scheduled_at_raw:
        try:
            scheduled_at = datetime.fromisoformat(scheduled_at_raw)
        except Exception:
            pass

    # ── Datei(en) laden und hochladen ────────────────────────────
    def _fetch_bytes(url):
        try:
            resp = req_lib.get(url, timeout=(15, 60),
                               headers={'User-Agent': 'Mozilla/5.0',
                                        'Referer': 'https://www.instagram.com/'})
            resp.raise_for_status()
            return resp.content
        except Exception:
            return None

    force_dup = bool(data.get('force_duplicate'))

    # ── VIDEO ─────────────────────────────────────────────────────
    if post.media_type == 'video':
        vid_url = post.video_url or post.image_url
        if not vid_url:
            return jsonify({'ok': False, 'error': 'Keine Video-URL gespeichert. '
                                                   'Bitte Quelle neu laden (↓ Laden).'})
        vid_bytes = _fetch_bytes(vid_url)
        if not vid_bytes:
            return jsonify({'ok': False, 'error': 'Video nicht mehr abrufbar (CDN abgelaufen). '
                                                   'Bitte Quelle neu laden (↓ Laden).'})
        fname = f'insp_{post.instagram_code}.mp4'
        cl = _cloudinary_upload(io.BytesIO(vid_bytes), fname)
        if cl:
            media = MediaItem(
                filename=cl['public_id'], original_filename=fname,
                file_type='video', mime_type='video/mp4',
                file_size=cl.get('bytes', len(vid_bytes)),
                width=cl.get('width'), height=cl.get('height'),
                url=cl['secure_url'], storage_source='cloudinary',
            )
        else:
            uname = f"{uuid.uuid4().hex}.mp4"
            with open(os.path.join(app.config['UPLOAD_FOLDER'], uname), 'wb') as _f:
                _f.write(vid_bytes)
            media = MediaItem(
                filename=uname, original_filename=fname,
                file_type='video', mime_type='video/mp4',
                file_size=len(vid_bytes), url=f'/media/file/{uname}',
                storage_source='local',
            )
        db.session.add(media)
        db.session.flush()
        media_items = [media]

    # ── BILD / KARUSSEL ───────────────────────────────────────────
    else:
        carousel_urls_list = []
        if post.media_type == 'carousel' and post.carousel_urls:
            try:
                carousel_urls_list = json.loads(post.carousel_urls)
            except Exception:
                pass
        if not carousel_urls_list:
            carousel_urls_list = [post.image_url]

        img_bytes_first = _fetch_bytes(carousel_urls_list[0])
        if not img_bytes_first and post.thumbnail_url != carousel_urls_list[0]:
            img_bytes_first = _fetch_bytes(post.thumbnail_url)
        if not img_bytes_first:
            return jsonify({'ok': False,
                            'error': 'Bild nicht mehr abrufbar (CDN-Link abgelaufen). '
                                     'Bitte die Quelle neu laden (↓ Laden).'})

        # Duplikat-Prüfung (erstes Bild)
        img_hash = _compute_image_hash(img_bytes_first)
        if img_hash and not force_dup:
            dup, diff = _find_duplicate(img_hash)
            if dup:
                dup_info = f'"{dup.original_filename or dup.filename}" (vom {dup.created_at.strftime("%d.%m.%Y") if dup.created_at else "?"})'
                return jsonify({
                    'ok': False, 'duplicate': True,
                    'error': f'⚠️ Dieses Bild existiert bereits in deiner Medienbibliothek: {dup_info}. '
                             f'Trotzdem übernehmen? Dann erneut klicken.',
                    'dup_media_id': dup.id, 'dup_info': dup_info,
                })

        def _upload_one(img_bytes, idx):
            fname = f'insp_{post.instagram_code}_{idx}.jpg'
            h     = _compute_image_hash(img_bytes) if idx > 0 else img_hash
            cl    = _cloudinary_upload(io.BytesIO(img_bytes), fname)
            if cl:
                return MediaItem(
                    filename=cl['public_id'], original_filename=fname,
                    file_type='image', mime_type='image/jpeg',
                    file_size=cl.get('bytes', len(img_bytes)),
                    width=cl.get('width'), height=cl.get('height'),
                    url=cl['secure_url'], storage_source='cloudinary', image_hash=h,
                )
            uname = f"{uuid.uuid4().hex}.jpg"
            with open(os.path.join(app.config['UPLOAD_FOLDER'], uname), 'wb') as _f:
                _f.write(img_bytes)
            return MediaItem(
                filename=uname, original_filename=fname,
                file_type='image', mime_type='image/jpeg',
                file_size=len(img_bytes), url=f'/media/file/{uname}',
                storage_source='local', image_hash=h,
            )

        all_bytes = [img_bytes_first]
        for url in carousel_urls_list[1:]:
            b = _fetch_bytes(url)
            if b:
                all_bytes.append(b)

        media_items = []
        for idx, byt in enumerate(all_bytes):
            m = _upload_one(byt, idx)
            db.session.add(m)
            media_items.append(m)
        db.session.flush()
        media = media_items[0]

    content_status = 'draft' if mode == 'reserve' else 'scheduled' if (mode == 'schedule' and scheduled_at) else 'ready'
    ci = ContentItem(
        title        = caption[:80] if caption else (f'Inspiration @{post.source.username}' if post.source else 'Inspiration'),
        caption      = caption,
        status       = content_status,
        content_type = 'feed',
        folder_id    = int(folder_id) if folder_id else None,
    )
    db.session.add(ci)
    db.session.flush()
    # Account via Many-to-Many verknüpfen
    if account_id:
        acc = db.session.get(Account, account_id)
        if acc:
            ci.accounts.append(acc)
    # Alle Karussel-Bilder mit ContentItem verknüpfen
    for m in media_items:
        m.content_item_id = ci.id
    post.status          = 'used'
    post.content_item_id = ci.id

    # Direkt einplanen wenn Datum gewählt
    sched_post = None
    if mode == 'schedule' and scheduled_at and account_id:
        sched_post = ScheduledPost(
            account_id      = account_id,
            content_item_id = ci.id,
            media_item_id   = media.id,
            caption         = caption,
            scheduled_at    = scheduled_at,
            status          = 'pending',
            post_type       = 'feed',
        )
        db.session.add(sched_post)

    db.session.commit()

    if mode == 'reserve':
        mode_label = 'als Vorrat gespeichert'
    elif mode == 'schedule' and scheduled_at:
        mode_label = f'für {scheduled_at.strftime("%d.%m.%Y um %H:%M")} eingeplant'
    else:
        mode_label = 'als bereit markiert'

    return jsonify({'ok': True, 'content_item_id': ci.id,
                    'thumb': media.url,
                    'message': f'Bild übernommen und {mode_label} ✓'})


@app.route('/api/inspirationen/<int:post_id>/rewrite', methods=['POST'])
@login_required
def inspiration_rewrite(post_id):
    """Formuliert eine Caption mit Claude komplett um, behält Stil + Kontext."""
    post    = InspirationPost.query.get_or_404(post_id)
    data    = request.get_json() or {}
    caption = data.get('caption') or post.caption or ''
    if not caption.strip():
        return jsonify({'ok': False, 'error': 'Keine Caption vorhanden.'})

    anthropic_key = get_setting('anthropic_api_key')
    if not anthropic_key:
        return jsonify({'ok': False, 'error': 'Anthropic API-Key fehlt (Einstellungen → Integrationen).'})

    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=anthropic_key)
        msg = client.messages.create(
            model      = 'claude-opus-4-8',
            max_tokens = 600,
            messages   = [{
                'role': 'user',
                'content': (
                    "Formuliere diese Instagram-Caption komplett um.\n"

                    "Regeln:\n"
                    "- Behalte exakt denselben Kontext, Humor, Tonalität und die Kernaussage\n"
                    "- Kein Satz darf identisch zur Vorlage sein\n"
                    "- Gleiche Sprache wie das Original (Deutsch wenn Original Deutsch ist)\n"
                    "- Keine Meta-Kommentare, nur die fertige Caption\n"
                    "- Hashtags dürfen weggelassen werden\n\n"
                    f"Original:\n{caption}"
                )
            }]
        )
        _log_ai('inspo_rewrite', msg)
        rewritten = msg.content[0].text.strip()
        return jsonify({'ok': True, 'rewritten': rewritten})
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Claude-Fehler: {str(e)}'})


# ─────────────────────── CONTENT-ORDNER (Kategorien) ───────────────────────

@app.route('/kategorien')
@login_required
def kategorien():
    """Verwaltung der Vorrat-Ordner (Kategorien) pro Account."""
    account_id = request.args.get('account', type=int)
    accounts   = Account.query.order_by(Account.name).all()

    q = ContentFolder.query
    if account_id:
        q = q.filter(
            db.or_(ContentFolder.account_id == account_id,
                   ContentFolder.account_id.is_(None))
        )
    folders = q.order_by(ContentFolder.account_id.nullslast(),
                         ContentFolder.sort_order, ContentFolder.name).all()

    # ── Anzahl Posts pro Ordner (mit Status-Aufschlüsselung) ─────
    folder_counts   = {}   # fid → Gesamt (aktiv)
    folder_statuses = {}   # fid → {status: count}
    for f in folders:
        rows = db.session.query(ContentItem.status, func.count(ContentItem.id))\
            .filter(ContentItem.folder_id == f.id)\
            .group_by(ContentItem.status).all()
        sc = {s: c for s, c in rows}
        active = sum(sc.get(s, 0) for s in ['draft', 'ready', 'in_progress', 'scheduled'])
        folder_counts[f.id]   = active
        folder_statuses[f.id] = sc

    # ── Pipeline-Zahlen ──────────────────────────────────────────
    base_ci = ContentItem.query
    base_ip = InspirationPost.query
    if account_id:
        base_ci = base_ci.filter(ContentItem.accounts.any(id=account_id))

    pipeline = {
        'insp_new':       base_ip.filter_by(status='new').count(),
        'insp_saved':     base_ip.filter_by(is_saved=True).count(),
        'vorrat_draft':   base_ci.filter_by(status='draft').count(),
        'vorrat_ready':   base_ci.filter_by(status='ready').count(),
        'vorrat_sched':   base_ci.filter_by(status='scheduled').count(),
        'published':      base_ci.filter_by(status='published').count(),
    }

    # ── Verlorene Posts (kein Ordner ODER kein Account) ──────────
    orphan_no_folder = ContentItem.query.filter(
        ContentItem.folder_id.is_(None),
        ContentItem.status.in_(['draft', 'ready'])
    ).count()
    orphan_no_account = db.session.query(func.count(ContentItem.id)).filter(
        ContentItem.status.in_(['draft', 'ready']),
        ~ContentItem.accounts.any()
    ).scalar() or 0

    from datetime import date as _date_today
    return render_template('kategorien.html',
        folders=folders, accounts=accounts,
        folder_counts=folder_counts,
        folder_statuses=folder_statuses,
        pipeline=pipeline,
        orphan_no_folder=orphan_no_folder,
        orphan_no_account=orphan_no_account,
        sel_account=account_id,
        today=_date_today.today(),
        active_page='kategorien')


@app.route('/api/weather/check-now', methods=['POST'])
@login_required
def weather_check_now():
    """Manueller Wetter-Check — für Tests und sofortiges Auslösen."""
    threading.Thread(target=_check_all_weather, daemon=True).start()
    return jsonify({'ok': True, 'message': 'Wetter-Check gestartet'})


@app.route('/api/weather/status')
@login_required
def weather_status():
    """Übersicht: letzter Check, letzte Trigger, Cooldown-Status."""
    accounts = Account.query.filter_by(status='active').all()
    result = []
    now = datetime.utcnow()
    for acc in accounts:
        city = _get_weather_city(acc)
        if not city:
            continue
        cache = WeatherCache.query.filter_by(account_id=acc.id).first()
        triggers_info = {}
        for tkey, tconf in WEATHER_TRIGGERS.items():
            last = WeatherTriggerLog.query.filter_by(
                account_id=acc.id, trigger_type=tkey
            ).order_by(WeatherTriggerLog.fired_at.desc()).first()
            if last:
                days_ago  = (now - last.fired_at).days
                remaining = max(0, tconf['cooldown'] - days_ago)
                triggers_info[tkey] = {
                    'last_fired': last.fired_at.strftime('%d.%m.%Y'),
                    'cooldown_remaining': remaining,
                    'on_cooldown': remaining > 0,
                }
            else:
                triggers_info[tkey] = {'last_fired': None, 'cooldown_remaining': 0, 'on_cooldown': False}
        result.append({
            'account': acc.name,
            'city': city,
            'last_check': cache.checked_at.strftime('%d.%m.%Y %H:%M') if cache else None,
            'temperature': cache.temperature if cache else None,
            'description': cache.description if cache else None,
            'triggers': triggers_info,
        })
    return jsonify(result)


@app.route('/api/folders', methods=['GET'])
@login_required
def folders_list():
    account_id = request.args.get('account', type=int)
    q = ContentFolder.query
    if account_id:
        q = q.filter(db.or_(ContentFolder.account_id == account_id,
                             ContentFolder.account_id.is_(None)))
    folders = q.order_by(ContentFolder.sort_order, ContentFolder.name).all()
    return jsonify([{
        'id': f.id, 'name': f.name, 'color': f.color, 'icon': f.icon,
        'account_id': f.account_id, 'posts_per_week': f.posts_per_week,
        'sort_order': f.sort_order, 'notes': f.notes or ''
    } for f in folders])


@app.route('/api/folders', methods=['POST'])
@login_required
def folder_create():
    d          = request.get_json() or {}
    name       = (d.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'Name darf nicht leer sein.'})
    account_id = d.get('account_id') or None
    def _parse_date(s):
        try: return datetime.strptime(s, '%Y-%m-%d').date() if s else None
        except: return None

    try:
        f = ContentFolder(
            name             = name,
            color            = d.get('color', '#6366f1'),
            icon             = d.get('icon', 'fa-folder'),
            account_id       = account_id,
            sort_order       = int(d.get('sort_order', 0) or 0),
            posts_per_week   = int(d.get('posts_per_week', 0) or 0),
            notes            = d.get('notes', '') or '',
            valid_from        = _parse_date(d.get('valid_from')),
            valid_until       = _parse_date(d.get('valid_until')),
            recurring_yearly  = bool(d.get('recurring_yearly', False)),
            trigger_condition = d.get('trigger_condition') or None,
        )
        db.session.add(f)
        db.session.commit()
        return jsonify({'ok': True, 'id': f.id, 'name': f.name, 'color': f.color, 'icon': f.icon})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f'folder_create error: {e}')
        return jsonify({'ok': False, 'error': f'Datenbankfehler: {str(e)}'})


@app.route('/api/folders/<int:fid>', methods=['PUT'])
@login_required
def folder_update(fid):
    f = ContentFolder.query.get_or_404(fid)
    d = request.get_json() or {}
    def _pd(s):
        try: return datetime.strptime(s, '%Y-%m-%d').date() if s else None
        except: return None
    if 'name'             in d: f.name             = (d['name'] or '').strip() or f.name
    if 'color'            in d: f.color            = d['color']
    if 'icon'             in d: f.icon             = d['icon']
    if 'account_id'       in d: f.account_id       = d['account_id'] or None
    if 'sort_order'       in d: f.sort_order        = int(d['sort_order'] or 0)
    if 'posts_per_week'   in d: f.posts_per_week    = int(d['posts_per_week'] or 0)
    if 'notes'            in d: f.notes             = d['notes']
    if 'valid_from'       in d: f.valid_from        = _pd(d['valid_from'])
    if 'valid_until'      in d: f.valid_until       = _pd(d['valid_until'])
    if 'recurring_yearly'  in d: f.recurring_yearly  = bool(d['recurring_yearly'])
    if 'trigger_condition' in d: f.trigger_condition = d['trigger_condition'] or None
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/folders/<int:fid>', methods=['DELETE'])
@login_required
def folder_delete(fid):
    f = ContentFolder.query.get_or_404(fid)
    # Posts aus dem Ordner herauslösen (nicht löschen)
    ContentItem.query.filter_by(folder_id=fid).update({'folder_id': None})
    db.session.delete(f)
    db.session.commit()
    return jsonify({'ok': True})


# ── Standard-Ordner-Templates für deutsche Stadt-Instagram-Seiten ────────────
_STANDARD_FOLDER_TEMPLATES = [
    {'name': 'Starterpacks',     'icon': 'fa-grip',              'color': '#6366f1',
     'notes': 'Starter Pack Memes — Collage-Format "Der Starter Pack für..."',
     'posts_per_week': 1},
    {'name': 'Wetter',           'icon': 'fa-cloud-sun-rain',    'color': '#38bdf8',
     'notes': 'Wetterextreme, Hitze, Schnee, Sturm, Wetter-Screenshots',
     'posts_per_week': 1},
    {'name': 'Events',           'icon': 'fa-calendar-star',     'color': '#f59e0b',
     'notes': 'Konzerte, Festivals, Stadtfeste, Messen, Veranstaltungs-Flyer',
     'posts_per_week': 1},
    {'name': 'Weihnachten',      'icon': 'fa-snowflake',         'color': '#ef4444',
     'notes': 'Weihnachtsmarkt, Advent, Christbaum, Glühwein, Winterzauber',
     'posts_per_week': 2, 'valid_from_md': '12-01', 'valid_until_md': '01-06', 'recurring': True},
    {'name': 'Frühling',         'icon': 'fa-seedling',          'color': '#22c55e',
     'notes': 'Kirschblüte, Ostern, erste Sonne, Frühlingsblumen',
     'posts_per_week': 1, 'valid_from_md': '03-01', 'valid_until_md': '05-31', 'recurring': True},
    {'name': 'Sommer',           'icon': 'fa-sun',               'color': '#fbbf24',
     'notes': 'Freibad, Hitzewelle, Eis essen, Grillen, See/Strand',
     'posts_per_week': 2, 'valid_from_md': '06-01', 'valid_until_md': '08-31', 'recurring': True},
    {'name': 'Herbst',           'icon': 'fa-leaf',              'color': '#f97316',
     'notes': 'Blätterfärben, Oktoberfest, Kürbis, Ernte, Nebelstimmung',
     'posts_per_week': 1, 'valid_from_md': '09-01', 'valid_until_md': '11-30', 'recurring': True},
    {'name': 'Winter',           'icon': 'fa-icicles',           'color': '#93c5fd',
     'notes': 'Schnee, Eislaufen, Frost, Winterlandschaft (außerhalb Weihnachten)',
     'posts_per_week': 1, 'valid_from_md': '12-01', 'valid_until_md': '02-28', 'recurring': True},
    {'name': 'Stadtleben',       'icon': 'fa-city',              'color': '#8b5cf6',
     'notes': 'Alltagsmemes, Erkennungszeichen, "typisch [Stadt]", lokale Klischees',
     'posts_per_week': 2},
    {'name': 'Essen & Trinken',  'icon': 'fa-utensils',          'color': '#d97706',
     'notes': 'Restaurants, Gerichte, Streetfood, Cafés, lokale Spezialitäten',
     'posts_per_week': 1},
    {'name': 'Sport',            'icon': 'fa-futbol',            'color': '#10b981',
     'notes': 'Sport, Fußball, lokale Vereine, Sportereignisse',
     'posts_per_week': 1},
    {'name': 'Nostalgie',        'icon': 'fa-clock-rotate-left', 'color': '#9ca3af',
     'notes': 'Throwbacks, alte Fotos, "früher war...", Vergleich alt/neu',
     'posts_per_week': 1},
    {'name': 'Humor & Memes',    'icon': 'fa-face-laugh',        'color': '#ec4899',
     'notes': 'Allgemeine Witzbilder, Reaktionsbilder, Textmemes',
     'posts_per_week': 1},
    {'name': 'Natur',            'icon': 'fa-tree',              'color': '#16a34a',
     'notes': 'Parks, Flüsse, Wälder, Sonnenuntergänge, Naturlandschaften',
     'posts_per_week': 1},
    {'name': 'Silvester',        'icon': 'fa-champagne-glasses', 'color': '#c084fc',
     'notes': 'Feuerwerk, Jahreswechsel, "Frohes neues Jahr", Countdown',
     'posts_per_week': 2, 'valid_from_md': '12-27', 'valid_until_md': '01-03', 'recurring': True},
]


@app.route('/api/settings/auto-classify', methods=['POST'])
@login_required
def toggle_auto_classify():
    """Schaltet KI-Auto-Kategorisierung ein oder aus.
    Body: { enabled: true|false }  ODER leerer Body → togglet den aktuellen Wert.
    """
    d       = request.get_json() or {}
    current = get_setting('auto_classify_inspirationen') == 'true'
    enabled = d.get('enabled', not current)   # toggle wenn nicht angegeben

    _set = AppSettings.query.filter_by(key='auto_classify_inspirationen').first()
    if _set:
        _set.value = 'true' if enabled else 'false'
    else:
        db.session.add(AppSettings(key='auto_classify_inspirationen',
                                   value='true' if enabled else 'false'))
    db.session.commit()

    # Stats für die UI
    total_new   = InspirationPost.query.filter_by(status='new').count()
    classified  = InspirationPost.query.filter(
        InspirationPost.status == 'new',
        InspirationPost.suggested_folder_id.isnot(None)
    ).count()
    return jsonify({'ok': True, 'enabled': enabled,
                    'total_new': total_new, 'classified': classified,
                    'pending': total_new - classified})


@app.route('/api/folders/create-standard', methods=['POST'])
@login_required
def folders_create_standard():
    """Legt Standard-Inhaltskategorien als Ordner an.
    Body: { account_id: int|null, skip_existing: bool (default true) }
    Gibt zurück: { created: [Namen], skipped: [Namen] }
    """
    from datetime import date as _date
    d          = request.get_json() or {}
    account_id = d.get('account_id') or None
    skip_ex    = d.get('skip_existing', True)

    # Vorhandene Namen für diesen Account
    existing_q = ContentFolder.query
    if account_id:
        existing_q = existing_q.filter(
            db.or_(ContentFolder.account_id == int(account_id),
                   ContentFolder.account_id.is_(None))
        )
    existing_names = {f.name.lower() for f in existing_q.all()}

    created, skipped = [], []
    year = datetime.utcnow().year

    for t in _STANDARD_FOLDER_TEMPLATES:
        if skip_ex and t['name'].lower() in existing_names:
            skipped.append(t['name'])
            continue

        valid_from  = None
        valid_until = None
        if t.get('valid_from_md'):
            try:
                m, day = t['valid_from_md'].split('-')
                valid_from = _date(year, int(m), int(day))
            except Exception:
                pass
        if t.get('valid_until_md'):
            try:
                m, day = t['valid_until_md'].split('-')
                # Dezember→Januar: valid_until im nächsten Jahr
                y = year + 1 if t.get('valid_from_md', '').startswith('12') and m == '01' else year
                valid_until = _date(y, int(m), int(day))
            except Exception:
                pass

        f = ContentFolder(
            name             = t['name'],
            icon             = t['icon'],
            color            = t['color'],
            notes            = t.get('notes', ''),
            posts_per_week   = t.get('posts_per_week', 1),
            account_id       = int(account_id) if account_id else None,
            valid_from       = valid_from,
            valid_until      = valid_until,
            recurring_yearly = t.get('recurring', False),
        )
        db.session.add(f)
        created.append(t['name'])

    db.session.commit()
    return jsonify({'ok': True, 'created': created, 'skipped': skipped,
                    'total_created': len(created)})


@app.route('/api/autoplan', methods=['POST'])
@login_required
def autoplan():
    """
    Verteilt Posts aus dem Vorrat automatisch auf den Kalender.
    Body: { account_id, date_from, date_to, posts_per_week,
            post_times: ["18:00","12:00"],
            post_days: [1,2,3,4,5],        # 0=Mo … 6=So
            folder_rules: {folder_id: posts_per_week, ...} }
    """
    d            = request.get_json() or {}
    account_id   = d.get('account_id')
    if not account_id:
        return jsonify({'ok': False, 'error': 'account_id fehlt.'})

    try:
        date_from = datetime.fromisoformat(d['date_from'])
        date_to   = datetime.fromisoformat(d['date_to'])
    except Exception:
        return jsonify({'ok': False, 'error': 'Ungültiges Datum.'})

    import random as _random
    posts_per_week = int(d.get('posts_per_week', 7) or 7)
    day_mode       = d.get('day_mode', 'fixed')
    post_days      = [int(x) for x in (d.get('post_days') or [0,1,2,3,4,5,6])]
    min_gap_days   = max(0, int(d.get('min_gap_days', 0) or 0))
    # days_per_week wird automatisch aus posts_per_week abgeleitet (kein separates UI-Feld)
    # Frontend schickt es vorsichtshalber mit, Fallback: ppw / posts_per_day
    _ppd_hint = max(1, int(d.get('posts_per_day', 1) or 1)) if d.get('time_mode') == 'random' \
                else max(1, len(d.get('post_times') or ['18:00']))
    days_per_week  = max(1, min(7, int(d.get('days_per_week') or 0) or
                                   -(-posts_per_week // _ppd_hint)))  # ceil ohne math
    folder_rules   = {int(k): int(v) for k, v in (d.get('folder_rules') or {}).items() if int(v) > 0}
    time_mode      = d.get('time_mode', 'fixed')

    # Hilfsfunktion: Uhrzeiten für einen Tag generieren
    def _times_for_day(day_dt):
        if time_mode == 'random':
            try:
                fh, fm = map(int, d.get('time_from', '11:00').split(':'))
                th, tm = map(int, d.get('time_to',   '18:00').split(':'))
            except Exception:
                fh, fm, th, tm = 11, 0, 18, 0
            ppd         = max(1, int(d.get('posts_per_day', 1) or 1))
            from_min    = fh * 60 + fm
            to_min      = th * 60 + tm
            if from_min >= to_min:
                to_min = from_min + 60
            gap         = 90   # mindestens 90 Minuten Abstand
            chosen      = []
            attempts    = 0
            while len(chosen) < ppd and attempts < 200:
                attempts += 1
                candidate = _random.randint(from_min, to_min)
                if all(abs(candidate - c) >= gap for c in chosen):
                    chosen.append(candidate)
            chosen.sort()
            return [day_dt.replace(hour=m // 60, minute=m % 60, second=0, microsecond=0)
                    for m in chosen]
        else:
            post_times = d.get('post_times') or ['18:00']
            result = []
            for t in post_times:
                try:
                    h, m = map(int, t.split(':'))
                    result.append(day_dt.replace(hour=h, minute=m, second=0, microsecond=0))
                except Exception:
                    pass
            return result

    # Alle Slots im Zeitraum berechnen
    slots = []
    if day_mode == 'random':
        # Woche für Woche: zufällig days_per_week Tage auswählen
        cur = date_from.replace(hour=0, minute=0, second=0, microsecond=0)
        # Auf Montag der ersten Woche zurückgehen
        week_start = cur - timedelta(days=cur.weekday())
        while week_start <= date_to:
            # Alle 7 Tage der Woche die im Zeitraum liegen
            week_days = []
            for wd in range(7):
                day = week_start + timedelta(days=wd)
                if date_from.date() <= day.date() <= date_to.date():
                    week_days.append(day)
            if week_days:
                chosen = _random.sample(week_days, min(days_per_week, len(week_days)))
                chosen.sort()
                for day in chosen:
                    slots.extend(_times_for_day(day))
            week_start += timedelta(weeks=1)
    else:
        cur = date_from.replace(hour=0, minute=0, second=0, microsecond=0)
        while cur <= date_to:
            if cur.weekday() in post_days:
                slots.extend(_times_for_day(cur))
            cur += timedelta(days=1)

    if not slots:
        return jsonify({'ok': False, 'error': 'Keine Posting-Slots im gewählten Zeitraum.'})

    # Mindestabstand zwischen Posts filtern
    if min_gap_days > 0:
        slots.sort()
        filtered = []
        last_day = None
        for s in slots:
            if last_day is None or (s.date() - last_day).days > min_gap_days:
                filtered.append(s)
                last_day = s.date()
        slots = filtered

    # ── Prioritäts-Ordner: Zeitfenster laden ─────────────────────
    from datetime import date as _date
    import random as _rand
    today = _date.today()

    def _folder_is_active(folder):
        if not folder.valid_from or not folder.valid_until:
            return False
        vf, vu = folder.valid_from, folder.valid_until
        if folder.recurring_yearly:
            vf = vf.replace(year=today.year)
            vu = vu.replace(year=today.year)
            if vf <= vu:
                return vf <= today <= vu
            else:
                return today >= vf or today <= vu
        return vf <= today <= vu

    def _slot_in_window(slot_dt, vf, vu, recurring):
        """Prüft ob ein Slot innerhalb eines Zeitfensters liegt."""
        sd = slot_dt.date()
        if recurring:
            vf2 = vf.replace(year=sd.year)
            vu2 = vu.replace(year=sd.year)
            if vf2 <= vu2:
                return vf2 <= sd <= vu2
            else:
                return sd >= vf2 or sd <= vu2
        return vf <= sd <= vu

    all_content_folders = ContentFolder.query.all()
    active_priority_folders = [f for f in all_content_folders if _folder_is_active(f)]
    active_priority_fids    = [f.id for f in active_priority_folders]

    # Slots die in mindestens einem Prioritätsfenster liegen
    def _is_priority_slot(slot_dt):
        for pf in active_priority_folders:
            if _slot_in_window(slot_dt, pf.valid_from, pf.valid_until, pf.recurring_yearly):
                return True
        return False

    priority_slot_set = {s for s in slots if _is_priority_slot(s)}

    # ── Alle bestehenden Posts im Zeitraum laden ───────────────────
    existing_sps = ScheduledPost.query.filter(
        ScheduledPost.account_id == account_id,
        ScheduledPost.scheduled_at >= date_from,
        ScheduledPost.scheduled_at <= date_to,
        ScheduledPost.status.in_(['pending', 'scheduled'])
    ).all()
    existing_map = {sp.scheduled_at: sp for sp in existing_sps}  # zeit → ScheduledPost
    existing_times = set(existing_map.keys())

    # ── Überschneidende Zeitfenster erkennen ─────────────────────
    warnings = []
    overlap_split = {}   # fid → Anteil der Priority-Slots (0.0–1.0)
    if len(active_priority_folders) > 1:
        # Berechne Slots pro Ordner innerhalb des Planungszeitraums
        folder_slot_sets = {}
        for pf in active_priority_folders:
            folder_slot_sets[pf.id] = {
                s for s in slots
                if _slot_in_window(s, pf.valid_from, pf.valid_until, pf.recurring_yearly)
            }
        # Überschneidungspaare finden
        fids = list(folder_slot_sets.keys())
        for i in range(len(fids)):
            for j in range(i + 1, len(fids)):
                a, b = fids[i], fids[j]
                overlap = folder_slot_sets[a] & folder_slot_sets[b]
                if overlap:
                    fa = next(f for f in active_priority_folders if f.id == a)
                    fb = next(f for f in active_priority_folders if f.id == b)
                    warnings.append(
                        f'⚠ Zeitfenster-Überschneidung: „{fa.name}" und „{fb.name}" '
                        f'teilen sich {len(overlap)} Slot(s) — werden proportional aufgeteilt.'
                    )
        # Proportionale Aufteilung: jeder Ordner bekommt Anteil basierend auf Posts-Pool-Größe
        for pf in active_priority_folders:
            pool_size = ContentItem.query.filter(
                ContentItem.status.in_(['draft', 'ready']),
                ContentItem.accounts.any(id=account_id),
                ContentItem.folder_id == pf.id
            ).count()
            overlap_split[pf.id] = max(pool_size, 1)
        total_weight = sum(overlap_split.values())
        for fid in overlap_split:
            overlap_split[fid] /= total_weight   # → 0.0–1.0

    # ── Randoms aus Priority-Slots rauswerfen ─────────────────────
    moved_count  = 0
    freed_slots  = []   # Slots die durch Verschiebung frei wurden

    if active_priority_folders and priority_slot_set:
        # Nicht-Prioritäts-Posts die in Priority-Slots liegen
        to_move = []
        for t, sp in existing_map.items():
            if t in priority_slot_set:
                ci = sp.content_item
                if ci and ci.folder_id not in active_priority_fids:
                    to_move.append(sp)
                elif ci is None:
                    to_move.append(sp)  # unbekannte Posts auch verschieben

        # Freie Slots außerhalb des Prioritätsfensters suchen
        non_priority_free = sorted(
            [s for s in slots if s not in priority_slot_set and s not in existing_times],
            key=lambda x: x
        )
        # Zusätzliche Slots nach dem Planungszeitraum generieren (Puffer)
        extra_start = date_to + timedelta(days=1)
        extra_cur   = extra_start.replace(hour=18, minute=0, second=0, microsecond=0)
        for _ in range(len(to_move) * 3):
            if extra_cur.weekday() in post_days:
                non_priority_free.append(extra_cur)
            extra_cur += timedelta(days=1)

        for sp in to_move:
            if non_priority_free:
                new_slot = non_priority_free.pop(0)
                old_slot = sp.scheduled_at
                sp.scheduled_at = new_slot
                existing_times.discard(old_slot)
                existing_times.add(new_slot)
                freed_slots.append(old_slot)
                moved_count += 1
            else:
                # Kein freier Platz → Post auf status='pending' zurücksetzen (entplanen)
                sp.status = 'pending'
                if sp.content_item:
                    sp.content_item.status = 'ready'
                freed_slots.append(sp.scheduled_at)
                existing_times.discard(sp.scheduled_at)
                moved_count += 1

    # ── Freie Slots berechnen (nach Verschiebungen) ───────────────
    free_slots = sorted([s for s in slots if s not in existing_times])

    if not free_slots and not active_priority_folders:
        return jsonify({'ok': False, 'error': 'Alle Slots im Zeitraum sind bereits belegt.'})

    # ── Post-Pool-Hilfsfunktion ───────────────────────────────────
    ALL_FOLDERS = -1

    def _get_pool(folder_id=None):
        q = ContentItem.query.filter(
            ContentItem.status.in_(['draft', 'ready']),
            ContentItem.accounts.any(id=account_id)
        )
        if folder_id and folder_id != ALL_FOLDERS:
            q = q.filter(ContentItem.folder_id == folder_id)
        elif folder_id is None:
            q = q.filter(ContentItem.folder_id.is_(None))
        return q.order_by(db.func.random()).all()

    # ── Slot-Aufteilung nach Ordner-Priorität ────────────────────
    total_slots   = len(free_slots)
    slot_idx      = 0
    created       = 0
    used_item_ids = set()
    assignments   = []

    if active_priority_folders:
        if len(active_priority_folders) > 1 and overlap_split:
            # Überschneidung: proportional aufteilen
            for pf in active_priority_folders:
                # Slots die speziell diesem Ordner gehören
                pf_slots = [s for s in free_slots
                            if _slot_in_window(s, pf.valid_from, pf.valid_until, pf.recurring_yearly)]
                # Bei Überschneidungen: proportionaler Anteil der geteilten Slots
                shared = [s for s in pf_slots if any(
                    _slot_in_window(s, other.valid_from, other.valid_until, other.recurring_yearly)
                    for other in active_priority_folders if other.id != pf.id
                )]
                exclusive = [s for s in pf_slots if s not in shared]
                n = len(exclusive) + round(len(shared) * overlap_split.get(pf.id, 0.5))
                assignments.append((pf.id, max(n, 1)))
        else:
            for pf in active_priority_folders:
                n = len([s for s in free_slots
                         if _slot_in_window(s, pf.valid_from, pf.valid_until, pf.recurring_yearly)])
                assignments.append((pf.id, max(n, 1)))

    # Normale Ordner für verbleibende Slots
    priority_n = sum(n for _, n in assignments)
    remaining  = max(0, total_slots - priority_n)
    if folder_rules and remaining > 0:
        total_ppw = sum(folder_rules.values())
        for fid, ppw in folder_rules.items():
            n = max(1, round(remaining * ppw / total_ppw))
            assignments.append((fid if fid else None, n))
        extra = remaining - sum(n for _, n in assignments[len(active_priority_folders):])
        if extra > 0:
            assignments.append((ALL_FOLDERS, extra))
    elif remaining > 0:
        assignments.append((ALL_FOLDERS, remaining))

    # ── Scheduling-Loop ───────────────────────────────────────────
    overflow_by_folder = {}   # fid → [leftover ContentItems] für nächstes Jahr

    for folder_id, n_slots in assignments:
        pool = _get_pool(folder_id)
        pool = [p for p in pool if p.id not in used_item_ids]
        _rand.shuffle(pool)
        scheduled_this_round = 0
        for item in pool:
            if scheduled_this_round >= n_slots:
                break
            if slot_idx >= len(free_slots):
                break
            slot  = free_slots[slot_idx]
            media = item.media_items[0] if item.media_items else None
            sp = ScheduledPost(
                account_id      = account_id,
                content_item_id = item.id,
                media_item_id   = media.id if media else None,
                caption         = item.caption or item.title or '',
                scheduled_at    = slot,
                status          = 'pending',
                post_type       = item.content_type or 'feed',
            )
            db.session.add(sp)
            item.status = 'scheduled'
            used_item_ids.add(item.id)
            slot_idx             += 1
            created              += 1
            scheduled_this_round += 1

        # Überlauf bei jährlich-wiederkehrenden Ordnern sammeln
        if folder_id in active_priority_fids:
            pf = next((f for f in active_priority_folders if f.id == folder_id), None)
            if pf and pf.recurring_yearly:
                leftover = [p for p in pool[scheduled_this_round:] if p.id not in used_item_ids]
                if leftover:
                    overflow_by_folder[pf] = leftover

    # ── Überlauf → nächstes Jahr einplanen ───────────────────────
    overflow_created = 0
    for pf, leftover in overflow_by_folder.items():
        try:
            next_vf = pf.valid_from.replace(year=today.year + 1)
            next_vu = pf.valid_until.replace(year=today.year + 1)
            # Einfache Slot-Generierung: jeden Tag im nächsten Fenster mit 18:00 Uhr
            ny_slots = []
            ny_cur = datetime(next_vf.year, next_vf.month, next_vf.day, 18, 0)
            ny_end = datetime(next_vu.year, next_vu.month, next_vu.day, 23, 59)
            while ny_cur <= ny_end and len(ny_slots) < len(leftover):
                if ny_cur.weekday() in post_days:
                    ny_slots.append(ny_cur)
                ny_cur += timedelta(days=1)
            # Schon belegte nächstjährige Slots ausschließen
            ny_existing = {sp.scheduled_at for sp in ScheduledPost.query.filter(
                ScheduledPost.account_id == account_id,
                ScheduledPost.scheduled_at >= ny_slots[0] if ny_slots else date_from,
                ScheduledPost.scheduled_at <= ny_slots[-1] if ny_slots else date_to,
                ScheduledPost.status.in_(['pending', 'scheduled'])
            ).all()} if ny_slots else set()
            ny_free = [s for s in ny_slots if s not in ny_existing]
            for item, slot in zip(leftover, ny_free):
                media = item.media_items[0] if item.media_items else None
                sp = ScheduledPost(
                    account_id      = account_id,
                    content_item_id = item.id,
                    media_item_id   = media.id if media else None,
                    caption         = item.caption or item.title or '',
                    scheduled_at    = slot,
                    status          = 'pending',
                    post_type       = item.content_type or 'feed',
                )
                db.session.add(sp)
                item.status = 'scheduled'
                overflow_created += 1
        except Exception:
            pass

    db.session.commit()

    # ── Überkapazitäts-Warnung: Wochen mit zu vielen Posts ────────
    overcapacity_weeks = []
    if posts_per_week > 0:
        from collections import defaultdict as _dd
        week_counts = _dd(int)
        future_posts = ScheduledPost.query.filter(
            ScheduledPost.account_id == account_id,
            ScheduledPost.scheduled_at >= now_berlin(),
            ScheduledPost.status.in_(['pending', 'scheduled'])
        ).all()
        for sp in future_posts:
            iso = sp.scheduled_at.isocalendar()
            week_counts[(iso[0], iso[1])] += 1
        overcapacity_weeks = [
            f'KW {w[1]}/{w[0]} ({cnt} Posts, Limit: {posts_per_week})'
            for w, cnt in sorted(week_counts.items()) if cnt > posts_per_week
        ]
        # SystemAlert erstellen wenn Überkapazität
        if overcapacity_weeks:
            existing_alert = SystemAlert.query.filter_by(
                alert_type='overcapacity', account_id=account_id, resolved=False
            ).first()
            if not existing_alert:
                db.session.add(SystemAlert(
                    alert_type='overcapacity',
                    severity='warning',
                    account_id=account_id,
                    message=f'Überkapazität in {len(overcapacity_weeks)} Woche(n): '
                            + ', '.join(overcapacity_weeks[:3]),
                ))
                db.session.commit()

    # ── Ergebnis zusammenbauen ────────────────────────────────────
    msg_parts = [f'{created} Posts eingeplant ✓']
    if moved_count:
        msg_parts.append(f'{moved_count} Random-Post(s) verschoben')
    if overflow_created:
        msg_parts.append(f'{overflow_created} Überlauf-Posts für {today.year + 1} vorgemerkt')
    if overcapacity_weeks:
        msg_parts.append(f'⚠ Überkapazität in {len(overcapacity_weeks)} Woche(n)')

    return jsonify({
        'ok': True,
        'created': created,
        'moved': moved_count,
        'overflow': overflow_created,
        'warnings': warnings + (
            [f'⚠ Überkapazität: ' + '; '.join(overcapacity_weeks[:5])]
            if overcapacity_weeks else []
        ),
        'message': ' · '.join(msg_parts),
    })


# ─────────────── KI-CAPTION + DUPLIKAT-ERKENNUNG ──────────────

def _compute_image_hash(img_bytes):
    """Perceptual Hash für Duplikat-Erkennung (imagehash pHash)."""
    try:
        import imagehash
        from PIL import Image
        import io as _io
        img = Image.open(_io.BytesIO(img_bytes)).convert('RGB')
        return str(imagehash.phash(img))
    except Exception:
        return None


def _find_duplicate(hash_str, tolerance=8):
    """Sucht ein ähnliches Bild in der Medienbibliothek.
    Gibt (MediaItem, diff) zurück oder (None, None)."""
    if not hash_str:
        return None, None
    try:
        import imagehash
        new_h = imagehash.hex_to_hash(hash_str)
        existing = (MediaItem.query
                    .filter(MediaItem.image_hash.isnot(None))
                    .order_by(MediaItem.created_at.desc())
                    .limit(1000).all())
        best, best_diff = None, tolerance + 1
        for m in existing:
            try:
                diff = abs(new_h - imagehash.hex_to_hash(m.image_hash))
                if diff <= tolerance and diff < best_diff:
                    best, best_diff = m, diff
            except Exception:
                continue
        return (best, best_diff) if best else (None, None)
    except Exception:
        return None, None


@app.route('/api/caption/generate', methods=['POST'])
@login_required
def caption_generate():
    """KI-Caption aus Bild via Claude Vision.
    Body: { image_url, account_id (optional) }
    Gibt caption + hashtags (aus Account-Einstellungen) zurück.
    """
    import base64 as _b64
    import requests as _req
    import anthropic as _ant

    d = request.get_json() or {}
    image_url  = (d.get('image_url') or '').strip()
    account_id = d.get('account_id')

    if not image_url:
        return jsonify({'ok': False, 'error': 'Kein Bild-URL angegeben.'})

    api_key = os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key konfiguriert.'})

    # Bild laden
    try:
        r = _req.get(image_url, timeout=(10, 25),
                     headers={'User-Agent': 'Mozilla/5.0',
                               'Referer': 'https://www.instagram.com/'})
        r.raise_for_status()
        img_bytes   = r.content
        media_type  = r.headers.get('content-type', 'image/jpeg').split(';')[0].strip()
        if media_type not in ('image/jpeg', 'image/png', 'image/gif', 'image/webp'):
            media_type = 'image/jpeg'
        img_b64 = _b64.standard_b64encode(img_bytes).decode()
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Bild nicht abrufbar: {e}'})

    # Account-Hashtags laden
    default_hashtags = ''
    sports_hashtag   = ''
    acc_persona      = ''
    if account_id:
        acc = db.session.get(Account, int(account_id))
        if acc:
            default_hashtags = (acc.default_hashtags or '').strip()
            sports_hashtag   = (acc.sports_hashtag   or '').strip()
            acc_persona      = (acc.page_persona      or '').strip()

    persona_hint = f'\nSeitencharakter: {acc_persona}' if acc_persona else ''

    prompt = f"""Analysiere dieses Bild und erstelle eine Instagram-Caption auf Deutsch.{persona_hint}

Regeln:
- 2–4 Sätze, ansprechend und zum Bild passend
- Keine Hashtags in der Caption selbst
- Zuletzt: Ist Fußball das Hauptthema? (nur wenn eindeutig erkennbar)

Antworte exakt in diesem Format:
CAPTION: [deine Caption]
FUSSBALL: [Ja / Nein]"""

    try:
        client = _ant.Anthropic(api_key=api_key)
        resp   = client.messages.create(
            model=(get_setting('caption_model') or 'claude-haiku-4-5'),
            max_tokens=500,
            system='Du bist ein Social-Media-Manager für deutschsprachige Instagram-Seiten.',
            messages=[{'role': 'user', 'content': [
                {'type': 'image', 'source': {'type': 'base64',
                                              'media_type': media_type,
                                              'data': img_b64}},
                {'type': 'text', 'text': prompt}
            ]}]
        )
        _log_ai('caption', resp)
        text = resp.content[0].text.strip()
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Claude-Fehler: {e}'})

    # Parsen
    caption     = ''
    is_football = False
    for line in text.splitlines():
        if line.startswith('CAPTION:'):
            caption = line[8:].strip()
        elif line.startswith('FUSSBALL:'):
            is_football = 'ja' in line.lower()

    # Hashtags zusammenbauen
    hashtags = default_hashtags
    if is_football and sports_hashtag:
        hashtags = (hashtags + ' ' + sports_hashtag).strip() if hashtags else sports_hashtag

    return jsonify({
        'ok':          True,
        'caption':     caption,
        'hashtags':    hashtags,
        'is_football': is_football,
    })


# ═══════════════════════════════════════════════════════════════
# SMART-REFILL
# ═══════════════════════════════════════════════════════════════

def _smart_refill_check():
    """Prüft alle aktiven Accounts; füllt Vorrat auf wenn unter Schwellwert."""
    with app.app_context():
        if get_setting('smart_refill_enabled') != 'true':
            return
        global_threshold = int(get_setting('smart_refill_threshold_days') or 7)
        accounts = Account.query.filter_by(status='active').all()
        for acc in accounts:
            try:
                threshold = acc.smart_refill_threshold or global_threshold
                days = acc.feed_stock_days()
                if days >= threshold:
                    continue
                # Passende InspirationPosts suchen (status='new', noch nicht verwendet)
                folder_ids = [f.id for f in ContentFolder.query.filter(
                    db.or_(ContentFolder.account_id == acc.id,
                           ContentFolder.account_id.is_(None))
                ).all()]
                candidates = InspirationPost.query.filter(
                    InspirationPost.status == 'new',
                    InspirationPost.is_saved == True,
                ).order_by(
                    InspirationPost.like_count.desc().nullslast(),
                    InspirationPost.created_at.desc()
                ).limit(5).all()
                if not candidates:
                    # Auch nicht-gespeicherte nehmen wenn keine gespeicherten da
                    candidates = InspirationPost.query.filter_by(status='new').order_by(
                        InspirationPost.like_count.desc().nullslast()
                    ).limit(5).all()
                added = 0
                for post in candidates:
                    if added >= 3:
                        break
                    folder_id = post.suggested_folder_id
                    if not folder_id and folder_ids:
                        folder_id = folder_ids[0]
                    item = ContentItem(
                        account_id=acc.id,
                        folder_id=folder_id,
                        title=f'Auto-Refill: {(post.caption or "")[:60]}',
                        caption=post.caption or '',
                        status='ready',
                        content_type='feed',
                        source_url=post.thumbnail_url,
                        created_at=datetime.utcnow(),
                    )
                    db.session.add(item)
                    post.status = 'used'
                    added += 1
                if added:
                    db.session.commit()
                    app.logger.info('Smart-Refill: %d Posts für %s nachgefüllt', added, acc.name)
            except Exception as e:
                db.session.rollback()
                app.logger.error('Smart-Refill Fehler für %s: %s', acc.name, e)


@app.route('/api/settings/smart-refill', methods=['POST'])
@login_required
def toggle_smart_refill():
    d = request.get_json() or {}
    enabled = d.get('enabled')
    if enabled is None:
        enabled = get_setting('smart_refill_enabled') != 'true'
    else:
        enabled = bool(enabled)
    set_setting('smart_refill_enabled', 'true' if enabled else 'false')
    if 'threshold_days' in d:
        set_setting('smart_refill_threshold_days', str(int(d['threshold_days'])))
    return jsonify({'ok': True, 'enabled': enabled,
                    'threshold_days': int(get_setting('smart_refill_threshold_days') or 7)})


@app.route('/api/settings/telegram-alert', methods=['GET'])
@login_required
def tg_alert_settings_get():
    return jsonify({
        'token':   get_setting('alert_telegram_token') or '',
        'chat_id': get_setting('alert_central_chat_id') or '',
    })


@app.route('/api/settings/telegram-alert', methods=['POST'])
@login_required
def tg_alert_settings_save():
    d = request.get_json() or {}
    set_setting('alert_telegram_token',   (d.get('token')   or '').strip())
    set_setting('alert_central_chat_id',  (d.get('chat_id') or '').strip())
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/settings/telegram-alert/test', methods=['POST'])
@login_required
def tg_alert_test():
    try:
        _send_central_alert('🔔 ContentOS Test-Nachricht — Telegram-Alerts sind aktiv!')
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/settings/telegram-bot/test', methods=['POST'])
@login_required
def tg_bot_test():
    import requests as _req
    token = get_setting('telegram_bot_token', '')
    if not token:
        return jsonify({'ok': False, 'error': 'Kein Bot-Token gesetzt'}), 400
    try:
        r = _req.get(f'https://api.telegram.org/bot{token}/getMe', timeout=8)
        d = r.json()
        if d.get('ok'):
            return jsonify({'ok': True, 'username': d['result']['username']})
        return jsonify({'ok': False, 'error': d.get('description', 'Unbekannter Fehler')}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# FOLLOWER-MEILENSTEIN-TRACKER
# ═══════════════════════════════════════════════════════════════

_MILESTONES = [1000, 2000, 5000, 10000, 25000, 50000, 100000,
               250000, 500000, 1000000, 2000000, 5000000]


def _next_milestone(followers):
    for m in _MILESTONES:
        if followers < m:
            return m
    return None


def _milestone_eta(account):
    """Berechnet ETA zum nächsten Milestone basierend auf 7-Tage-Wachstum."""
    target = _next_milestone(account.follower_count or 0)
    if not target:
        return None, None, None
    remaining = target - (account.follower_count or 0)
    # Wachstum letzte 7 Tage
    week_ago = datetime.utcnow() - timedelta(days=7)
    latest_7d_sub = db.session.query(
        func.max(AnalyticsSnapshot.recorded_at).label('lat')
    ).filter(
        AnalyticsSnapshot.account_id == account.id,
        func.date(AnalyticsSnapshot.recorded_at) == func.date(week_ago)
    ).subquery()
    old_snap = db.session.query(AnalyticsSnapshot.followers).filter(
        AnalyticsSnapshot.account_id == account.id,
        AnalyticsSnapshot.recorded_at == db.session.query(latest_7d_sub.c.lat).scalar_subquery()
    ).scalar()
    if old_snap and account.follower_count:
        weekly_growth = account.follower_count - old_snap
        daily_growth = weekly_growth / 7
    else:
        daily_growth = 0
    if daily_growth <= 0:
        return target, remaining, None
    days_needed = remaining / daily_growth
    eta_date = (datetime.utcnow() + timedelta(days=days_needed)).strftime('%d.%m.%Y')
    return target, remaining, eta_date


@app.route('/api/milestones')
@login_required
def get_milestones():
    accounts = Account.query.filter_by(status='active').all()
    result = []
    for acc in accounts:
        target, remaining, eta = _milestone_eta(acc)
        if target:
            result.append({
                'id': acc.id,
                'account_name': acc.name,
                'followers': acc.follower_count or 0,
                'next_milestone': target,
                'remaining': remaining,
                'eta_days': eta,
                'just_reached': False,
                'pct': round((acc.follower_count or 0) / target * 100, 1) if target else 0,
            })
    result.sort(key=lambda x: x['pct'], reverse=True)
    return jsonify({'milestones': result})


# ═══════════════════════════════════════════════════════════════
# CONTENT-SERIEN-PLANER
# ═══════════════════════════════════════════════════════════════

@app.route('/serien')
@login_required
def content_serien():
    series = ContentSeries.query.order_by(ContentSeries.created_at.desc()).all()
    accounts = Account.query.filter_by(status='active').order_by(Account.name).all()
    folders = ContentFolder.query.order_by(ContentFolder.name).all()
    return render_template('serien.html', series=series,
                           accounts=accounts, folders=folders, active_page='serien')


@app.route('/api/serien', methods=['GET'])
@login_required
def serie_list():
    series = ContentSeries.query.order_by(ContentSeries.created_at.desc()).all()
    out = []
    for s in series:
        out.append({
            'id': s.id,
            'account_id': s.account_id,
            'account_name': s.account.name if s.account else '',
            'folder_id': s.folder_id,
            'name': s.name,
            'description': s.description,
            'days_of_week': json.loads(s.days_of_week or '[]'),
            'preferred_time': s.preferred_time,
            'post_type': s.post_type,
            'active': s.active,
            'last_scheduled': s.last_scheduled.isoformat() if s.last_scheduled else None,
        })
    return jsonify(out)


@app.route('/api/serien', methods=['POST'])
@login_required
def serie_create():
    d = request.get_json() or {}
    s = ContentSeries(
        account_id=int(d['account_id']),
        folder_id=d.get('folder_id') or None,
        name=d['name'].strip(),
        description=d.get('description', '').strip(),
        days_of_week=json.dumps(d.get('days_of_week', [])),
        preferred_time=d.get('preferred_time', '09:00'),
        post_type=d.get('post_type', 'feed'),
        active=d.get('active', True),
    )
    db.session.add(s)
    db.session.commit()
    return jsonify({'ok': True, 'id': s.id})


@app.route('/api/serien/<int:sid>', methods=['PUT', 'DELETE'])
@login_required
def serie_update(sid):
    s = ContentSeries.query.get_or_404(sid)
    if request.method == 'DELETE':
        db.session.delete(s)
        db.session.commit()
        return jsonify({'ok': True})
    d = request.get_json() or {}
    s.name = d.get('name', s.name).strip()
    s.description = d.get('description', s.description or '').strip()
    s.account_id = int(d.get('account_id', s.account_id))
    s.folder_id = d.get('folder_id') or None
    s.days_of_week = json.dumps(d.get('days_of_week', json.loads(s.days_of_week or '[]')))
    s.preferred_time = d.get('preferred_time', s.preferred_time)
    s.post_type = d.get('post_type', s.post_type)
    s.active = d.get('active', s.active)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/serien/<int:sid>/toggle', methods=['POST'])
@login_required
def serie_toggle(sid):
    s = ContentSeries.query.get_or_404(sid)
    s.active = not s.active
    db.session.commit()
    return jsonify({'ok': True, 'active': s.active})


def _process_series():
    """Stündlich: legt ScheduledPosts für fällige Serien an."""
    with app.app_context():
        now = datetime.utcnow()
        today_weekday = now.weekday()  # 0=Mo .. 6=So
        for s in ContentSeries.query.filter_by(active=True).all():
            try:
                days = json.loads(s.days_of_week or '[]')
                if today_weekday not in days:
                    continue
                # Schon heute eingeplant?
                h, m_str = s.preferred_time.split(':')
                scheduled_dt = now.replace(hour=int(h), minute=int(m_str), second=0, microsecond=0)
                if scheduled_dt < now:
                    continue
                already = ScheduledPost.query.filter(
                    ScheduledPost.account_id == s.account_id,
                    func.date(ScheduledPost.scheduled_at) == now.date(),
                ).filter(
                    ScheduledPost.notes.like(f'%[SERIE:{s.id}]%')
                ).first()
                if already:
                    continue
                # Post aus Vorrat holen
                q = ContentItem.query.filter(
                    ContentItem.account_id == s.account_id,
                    ContentItem.status == 'ready',
                )
                if s.folder_id:
                    q = q.filter(ContentItem.folder_id == s.folder_id)
                item = q.order_by(ContentItem.created_at.asc()).first()
                if not item:
                    continue
                sp = ScheduledPost(
                    account_id=s.account_id,
                    content_item_id=item.id,
                    scheduled_at=scheduled_dt,
                    post_type=s.post_type,
                    status='scheduled',
                    notes=f'Auto-Serie: {s.name} [SERIE:{s.id}]',
                )
                item.status = 'scheduled'
                db.session.add(sp)
                s.last_scheduled = now
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                app.logger.error('Serie %d Fehler: %s', s.id, e)


# ═══════════════════════════════════════════════════════════════
# CONTENT-IDEEN
# ═══════════════════════════════════════════════════════════════

@app.route('/content-ideen')
@login_required
def content_ideen():
    accounts = Account.query.filter(
        Account.status.in_(['active', 'pause'])
    ).options(
        joinedload(Account.category)
    ).order_by(Account.follower_count.desc()).all()
    # IdeenContext pro Account laden/anlegen
    for acc in accounts:
        if not acc.ideen_context:
            ctx = AccountIdeenContext(account_id=acc.id)
            db.session.add(ctx)
    db.session.commit()
    return render_template('content_ideen.html', accounts=accounts, active_page='content_ideen')


@app.route('/api/content-ideen/context/<int:account_id>', methods=['GET'])
@login_required
def get_ideen_context(account_id):
    ctx = AccountIdeenContext.query.filter_by(account_id=account_id).first()
    if not ctx:
        return jsonify({})
    ideas = []
    if ctx.generated_ideas:
        try:
            ideas = json.loads(ctx.generated_ideas)
        except Exception:
            ideas = []
    past_posts = []
    if ctx.past_posts_json:
        try: past_posts = json.loads(ctx.past_posts_json)
        except: pass
    return jsonify({
        'konzept':          ctx.konzept or '',
        'zielgruppe':       ctx.zielgruppe or '',
        'tonalitaet':       ctx.tonalitaet or '',
        'themen':           ctx.themen or '',
        'updated_at':       ctx.updated_at.isoformat() if ctx.updated_at else None,
        'generated_ideas':  ideas,
        'past_posts':       past_posts,
        'page_analysis':    ctx.page_analysis or '',
        'analyse_feedback': ctx.analyse_feedback or '',
        'analyse_category': ctx.analyse_category or '',
    })


@app.route('/api/content-ideen/<int:account_id>/save-feedback', methods=['POST'])
@login_required
def save_analyse_feedback(account_id):
    """Speichert Account-spezifisches Feedback/Korrekturen zur KI-Analyse."""
    d = request.get_json() or {}
    ctx = AccountIdeenContext.query.filter_by(account_id=account_id).first()
    if not ctx:
        ctx = AccountIdeenContext(account_id=account_id)
        db.session.add(ctx)
    ctx.analyse_feedback = d.get('feedback', '').strip()
    if 'category' in d:
        ctx.analyse_category = d.get('category', '').strip()
    ctx.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/content-ideen/save-context', methods=['POST'])
@login_required
def save_ideen_context():
    d = request.get_json() or {}
    account_id = int(d['account_id'])
    ctx = AccountIdeenContext.query.filter_by(account_id=account_id).first()
    if not ctx:
        ctx = AccountIdeenContext(account_id=account_id)
        db.session.add(ctx)
    ctx.konzept    = d.get('konzept', ctx.konzept or '').strip()
    ctx.zielgruppe = d.get('zielgruppe', ctx.zielgruppe or '').strip()
    ctx.tonalitaet = d.get('tonalitaet', ctx.tonalitaet or '').strip()
    ctx.themen     = d.get('themen', ctx.themen or '').strip()
    ctx.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/content-ideen/<int:account_id>/past-posts', methods=['POST'])
@login_required
def save_past_posts(account_id):
    """Speichert die Liste bisheriger Beiträge mit Insights."""
    d = request.get_json() or {}
    posts = d.get('posts', [])
    ctx = AccountIdeenContext.query.filter_by(account_id=account_id).first()
    if not ctx:
        ctx = AccountIdeenContext(account_id=account_id)
        db.session.add(ctx)
    ctx.past_posts_json = json.dumps(posts, ensure_ascii=False)
    ctx.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'count': len(posts)})


@app.route('/api/content-ideen/<int:account_id>/clear-posts', methods=['POST'])
@login_required
def clear_past_posts(account_id):
    """Löscht alle gespeicherten Beiträge eines Accounts."""
    ctx = AccountIdeenContext.query.filter_by(account_id=account_id).first()
    if ctx:
        ctx.past_posts_json = None
        ctx.updated_at = datetime.utcnow()
        db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/ig-thumb/<filename>')
@login_required
def serve_ig_thumb(filename):
    """Serviert während des Scans gecachte Instagram-Thumbnails aus /tmp."""
    import pathlib as _pl, re as _re
    if not _re.match(r'^[a-f0-9]{32}\.jpg$', filename):
        abort(400)
    fpath = _pl.Path('/tmp/ig_thumbs') / filename
    if not fpath.exists():
        abort(404)
    resp = make_response(fpath.read_bytes())
    resp.headers['Content-Type'] = 'image/jpeg'
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@app.route('/api/proxy-image')
@login_required
def proxy_image():
    """Fallback-Proxy für Instagram-CDN-Bilder."""
    from urllib.parse import urlparse as _urlparse
    url = request.args.get('url', '').strip()
    if not url or not url.startswith('https://'):
        abort(400)
    try:
        host = _urlparse(url).hostname or ''
    except Exception:
        abort(400)
    allowed = ('cdninstagram.com', 'fbcdn.net', 'scontent', 'instagram')
    if not any(a in host for a in allowed):
        abort(403)
    try:
        req = _urllib_request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) '
                          'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1',
            'Referer': 'https://www.instagram.com/',
            'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
        })
        with _urllib_request.urlopen(req, timeout=8) as r:
            data = r.read()
            ctype = r.headers.get('Content-Type', 'image/jpeg').split(';')[0]
            resp = make_response(data)
            resp.headers['Content-Type'] = ctype
            resp.headers['Cache-Control'] = 'public, max-age=3600'
            return resp
    except Exception:
        abort(404)


@app.route('/api/content-ideen/<int:account_id>/analyse', methods=['POST'])
@login_required
def analyse_page(account_id):
    """KI analysiert bisherige Beiträge und erstellt ein Seiten-Profil."""
    import anthropic as _ant
    acc = Account.query.get_or_404(account_id)
    ctx = AccountIdeenContext.query.filter_by(account_id=account_id).first()
    if not ctx or not ctx.past_posts_json:
        return jsonify({'ok': False, 'error': 'Keine bisherigen Beiträge hinterlegt.'})

    api_key = os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key konfiguriert.'})

    try:
        posts = json.loads(ctx.past_posts_json)
    except Exception:
        return jsonify({'ok': False, 'error': 'Beitrags-Daten ungültig.'})

    def _fmt_post_line(idx, p):
        views = p.get('views', '') or p.get('reach', '') or ''
        likes = p.get('likes', '') or ''
        komm  = p.get('kommentare', '') or p.get('saves', '') or ''
        datum = p.get('datum', '') or ''
        eng_parts = []
        if views: eng_parts.append(f'▶{views}')
        if likes: eng_parts.append(f'♥{likes}')
        if komm:  eng_parts.append(f'💬{komm}')
        eng_str = ' | '.join(eng_parts) if eng_parts else '—'
        desc = p.get('beschreibung', '') or '(keine Caption)'
        return (f'{idx}. [{p.get("format","?")}] {datum}  {eng_str}\n'
                f'   Caption: {desc[:200]}')

    # Nach Likes sortiert → Top-Posts zuerst
    def _like_key(p):
        try: return int(p.get('likes') or 0)
        except: return 0
    sorted_posts = sorted(posts, key=_like_key, reverse=True)
    top_lines = [_fmt_post_line(i+1, p) for i, p in enumerate(sorted_posts[:10])]
    all_lines  = [_fmt_post_line(i+1, p) for i, p in enumerate(posts)]

    # Feedback + Kategorie-Regeln laden
    account_feedback  = (ctx.analyse_feedback or '').strip()
    account_category  = (ctx.analyse_category or '').strip()
    category_rules    = ''
    if account_category:
        cat_key        = f'analyse_cat_{account_category.lower().replace(" ", "_")}'
        category_rules = (get_setting(cat_key) or '').strip()

    feedback_block = ''
    if account_feedback:
        feedback_block += f'\nKORREKTUREN & KONTEXT VOM ACCOUNT-INHABER:\n{account_feedback}\n'
    if category_rules:
        feedback_block += f'\nKATEGORIE-REGELN ({account_category}):\n{category_rules}\n'

    prompt = f"""Du analysierst die Instagram-Seite „{acc.name}" und willst verstehen, WARUM bestimmte Posts gut liefen.
{feedback_block}
{len(posts)} Beiträge analysiert.

TOP 10 POSTS (nach Likes sortiert):
{chr(10).join(top_lines)}

ALLE {len(posts)} POSTS (chronologisch):
{chr(10).join(all_lines)}

Deine Aufgabe: Erkläre WARUM die erfolgreichen Posts liefen — nicht nur was gepostet wurde.
Berücksichtige dabei die Korrekturen und den Kontext vom Account-Inhaber wenn vorhanden.
Nutze ausschließlich dieses Format:

TOP_POSTS_MUSTER: [Was haben die besten Posts gemeinsam? Welches Thema, welcher Stil, welche Caption-Länge, welcher Typ?]
WARUM_LIEFEN_SIE: [Welche Emotion/psychologischer Trigger steckt dahinter? Humor, Lokalstolz, Überraschung, FOMO, Nostalgie?]
HOOK_ANALYSE: [Wie beginnen die Captions der Top-Posts? Was macht die ersten Worte unwiderstehlich?]
FORMAT_WARUM: [Warum funktioniert das beste Format bei dieser Zielgruppe — was ist der echte Grund?]
FLOP_MUSTER: [Was machen schwache Posts anders? Welche Themen/Stile zünden bei der Audience NICHT?]
REPLIZIEREN: [3 konkrete, sofort umsetzbare Ideen um das Erfolgsrezept zu wiederholen]
SEITEN_DNA: [1-2 Sätze: Was liebt die Audience an dieser Seite wirklich — und warum kommen sie wieder?]

Beziehe dich konkret auf echte Captions und Zahlen. Keine allgemeinen Social-Media-Tipps."""

    try:
        client = _ant.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=1500,
            messages=[{'role': 'user', 'content': prompt}]
        )
        _log_ai('ideen_analyse', resp)
        analysis = resp.content[0].text.strip()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

    ctx.page_analysis = analysis
    ctx.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'analysis': analysis})


@app.route('/api/content-ideen/<int:account_id>/scrape-analyse', methods=['POST'])
@login_required
def scrape_analyse_profile(account_id):
    """Scannt Instagram-Profil via Apify (letzte 100 Posts) + KI-Analyse mit Vision."""
    import json as _json, base64 as _b64, traceback as _tb
    import anthropic as _ant
    try:
        return _scrape_analyse_profile_inner(account_id, _json, _b64, _ant)
    except Exception as _e:
        app.logger.error(f'scrape_analyse_profile error: {_tb.format_exc()}')
        return jsonify({'ok': False, 'error': f'Unerwarteter Fehler: {str(_e)}'})


def _scrape_analyse_profile_inner(account_id, _json, _b64, _ant):
    acc = Account.query.get_or_404(account_id)

    # Handle ermitteln
    handle = acc.handle
    if not handle and acc.profile_url:
        handle = acc.profile_url.rstrip('/').split('/')[-1]
    if not handle:
        return jsonify({'ok': False, 'error': 'Kein Instagram-Handle beim Account hinterlegt (Einstellungen → Account).'})

    apify_token = get_setting('apify_token')
    if not apify_token:
        return jsonify({'ok': False, 'error': 'Kein Apify-Token. Bitte unter Einstellungen → Integrationen hinterlegen.'})

    api_key = os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key konfiguriert.'})

    # 1. Posts via Apify scrapen
    payload = _json.dumps({
        'directUrls': [f'https://www.instagram.com/{handle}/'],
        'resultsType': 'posts',
        'resultsLimit': 100,
        'maxPostsPerProfile': 100,
        'addParentData': False,
    }).encode()

    apify_url = (
        'https://api.apify.com/v2/acts/apify~instagram-scraper'
        f'/run-sync-get-dataset-items?token={apify_token}&timeout=300'
    )
    req = _urllib_request.Request(
        apify_url, data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with _urllib_request.urlopen(req, timeout=360) as r:
            items = _json.loads(r.read())
    except _urllib_request.HTTPError as e:
        body = e.read().decode(errors='replace')[:300]
        return jsonify({'ok': False, 'error': f'Apify Fehler {e.code}: {body}'})
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Apify Verbindungsfehler: {str(e)}'})

    if not items or not isinstance(items, list):
        return jsonify({'ok': False, 'error': 'Keine Posts gefunden. Ist der Account öffentlich und der Handle korrekt?'})

    # 2. Posts aufbereiten
    _type_map = {'Image': 'Foto', 'Video': 'Reel/Video', 'Sidecar': 'Karussell'}
    post_lines = []
    image_urls = []
    simplified_posts = []

    # Bilder in /tmp cachen damit Browser-Proxy sie zuverlässig liefern kann
    import hashlib as _hashlib, pathlib as _pathlib
    _thumb_dir = _pathlib.Path('/tmp/ig_thumbs')
    _thumb_dir.mkdir(exist_ok=True)

    def _cache_image(url):
        """Lädt Bild von Instagram CDN auf den Server und gibt lokalen Pfad zurück."""
        if not url:
            return ''
        fname = _hashlib.md5(url.encode()).hexdigest() + '.jpg'
        fpath = _thumb_dir / fname
        if fpath.exists():
            return fname
        try:
            req = _urllib_request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) '
                              'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1',
                'Referer': 'https://www.instagram.com/',
                'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
            })
            with _urllib_request.urlopen(req, timeout=8) as r:
                fpath.write_bytes(r.read())
            return fname
        except Exception:
            return ''

    for i, item in enumerate(items[:100], 1):
        caption = (item.get('caption') or '').strip()
        post_type = _type_map.get(item.get('type', ''), item.get('type', '?'))
        likes    = item.get('likesCount', '') or ''
        comments = item.get('commentsCount', '') or ''
        views    = item.get('videoViewCount', '') or item.get('videoPlayCount', '') or ''
        timestamp = (item.get('timestamp') or '')[:10]
        short_code = item.get('shortCode') or item.get('shortcode') or ''
        ig_url = item.get('url') or (f'https://www.instagram.com/p/{short_code}/' if short_code else '')

        # Bild-URL: mehrere Felder probieren
        raw_img = (item.get('displayUrl') or
                   item.get('thumbnailUrl') or
                   item.get('videoThumbnailUrl') or
                   (item.get('images') or [None])[0] or '')

        # Nur für erste 30 Posts Bild cachen (Speed)
        cached_fname = _cache_image(raw_img) if i <= 30 and raw_img else ''

        eng_parts = []
        if views    != '': eng_parts.append(f'▶ {views}')
        if likes    != '': eng_parts.append(f'♥ {likes}')
        if comments != '': eng_parts.append(f'💬 {comments}')
        eng_str = ' | '.join(eng_parts) if eng_parts else 'keine Zahlen'

        if i <= 50:
            post_lines.append(
                f'{i}. [{post_type}] {timestamp}\n'
                f'   Caption: {caption[:250] or "(keine Caption)"}\n'
                f'   {eng_str}'
            )

        if raw_img and len(image_urls) < 5:
            image_urls.append(raw_img)

        simplified_posts.append({
            'format':       _type_map.get(item.get('type', ''), '?'),
            'beschreibung': caption[:250],
            'views':        views,
            'likes':        likes,
            'kommentare':   comments,
            'datum':        timestamp,
            'ig_url':       ig_url,
            'short_code':   short_code,
            'cached_thumb': cached_fname,  # lokaler Dateiname in /tmp/ig_thumbs/
        })

    # 3. Bilder für Vision laden (max 4, best-effort)
    vision_images = []
    for img_url in image_urls[:4]:
        try:
            img_req = _urllib_request.Request(img_url, headers={'User-Agent': 'Mozilla/5.0'})
            with _urllib_request.urlopen(img_req, timeout=12) as img_r:
                img_data = img_r.read()
                ctype = img_r.headers.get('Content-Type', 'image/jpeg').split(';')[0]
                if ctype not in ('image/jpeg', 'image/png', 'image/webp', 'image/gif'):
                    ctype = 'image/jpeg'
                vision_images.append({
                    'type': 'image',
                    'source': {'type': 'base64', 'media_type': ctype, 'data': _b64.b64encode(img_data).decode()}
                })
        except Exception:
            pass

    # 4. Claude-Analyse
    ctx_info = acc.name
    if acc.handle:
        ctx_info += f' (@{acc.handle})'
    if acc.category:
        ctx_info += f' | {acc.category.name}'

    # Top-Posts nach Likes sortieren für bessere Analyse
    def _like_key_s(p):
        try: return int(p.get('likes') or 0)
        except: return 0
    sorted_s = sorted(simplified_posts, key=_like_key_s, reverse=True)
    top_lines_s = []
    for p in sorted_s[:10]:
        eng = []
        if p.get('views'):      eng.append(f'▶{p["views"]}')
        if p.get('likes'):      eng.append(f'♥{p["likes"]}')
        if p.get('kommentare'): eng.append(f'💬{p["kommentare"]}')
        top_lines_s.append(
            f'[{p.get("format","?")}] {p.get("datum","")}  {" | ".join(eng) or "—"}\n'
            f'  Caption: {(p.get("beschreibung") or "(keine)")[:200]}'
        )

    # Feedback + Kategorie-Regeln laden
    ctx_obj = AccountIdeenContext.query.filter_by(account_id=account_id).first()
    account_feedback_s  = (ctx_obj.analyse_feedback if ctx_obj else '') or ''
    account_category_s  = (ctx_obj.analyse_category if ctx_obj else '') or ''
    category_rules_s    = ''
    if account_category_s:
        cat_key_s        = f'analyse_cat_{account_category_s.lower().replace(" ", "_")}'
        category_rules_s = (get_setting(cat_key_s) or '').strip()

    feedback_block_s = ''
    if account_feedback_s:
        feedback_block_s += f'\nKORREKTUREN & KONTEXT VOM ACCOUNT-INHABER:\n{account_feedback_s}\n'
    if category_rules_s:
        feedback_block_s += f'\nKATEGORIE-REGELN ({account_category_s}):\n{category_rules_s}\n'

    total_scanned = len(simplified_posts)
    prompt = f"""Du analysierst den Instagram-Account „{ctx_info}" und willst verstehen, WARUM bestimmte Posts liefen.
{feedback_block_s}
{total_scanned} Posts gescannt. Reach nicht verfügbar — du siehst Views (Reels), Likes, Kommentare.

TOP 10 POSTS (nach Likes sortiert):
{chr(10).join(top_lines_s)}

ALLE {len(post_lines)} POSTS (chronologisch, erste 50):
{chr(10).join(post_lines)}

Deine Aufgabe: Erkläre WARUM die erfolgreichen Posts liefen.
Berücksichtige Korrekturen/Kontext vom Account-Inhaber wenn vorhanden.
Nutze ausschließlich dieses Format:

TOP_POSTS_MUSTER: [Was haben die Top-Posts gemeinsam? Thema, Stil, Caption-Länge, Typ?]
WARUM_LIEFEN_SIE: [Welche Emotion/Trigger? Humor, Lokalstolz, Überraschung, FOMO, Nostalgie?]
HOOK_ANALYSE: [Wie beginnen die Captions der Top-Posts? Was macht sie unwiderstehlich?]
FORMAT_WARUM: [Warum funktioniert das beste Format hier — was ist der echte Grund für die Zielgruppe?]
FLOP_MUSTER: [Was machen schwache Posts anders? Welche Themen/Stile zünden NICHT?]
REPLIZIEREN: [3 konkrete, sofort umsetzbare Ideen basierend auf dem Erfolgsrezept]
SEITEN_DNA: [1-2 Sätze: Was liebt die Audience wirklich — und warum kommen sie wieder?]

Beziehe dich auf echte Captions und Zahlen. Keine allgemeinen Tipps."""

    try:
        client = _ant.Anthropic(api_key=api_key)
        content = vision_images + [{'type': 'text', 'text': prompt}]
        resp = client.messages.create(
            model='claude-opus-4-8',
            max_tokens=2000,
            messages=[{'role': 'user', 'content': content}],
        )
        _log_ai('ideen_scrape_analyse', resp)
        analysis = resp.content[0].text.strip()
    except Exception as e:
        return jsonify({'ok': False, 'error': f'KI-Fehler: {str(e)}'})

    # 5. In AccountIdeenContext speichern
    ctx = AccountIdeenContext.query.filter_by(account_id=account_id).first()
    if not ctx:
        ctx = AccountIdeenContext(account_id=account_id)
        db.session.add(ctx)

    ctx.past_posts_json = _json.dumps(simplified_posts, ensure_ascii=False)
    ctx.page_analysis = analysis
    ctx.updated_at = datetime.utcnow()
    db.session.commit()

    return jsonify({
        'ok': True,
        'analysis': analysis,
        'posts_found': len(items),
        'images_analysed': len(vision_images),
    })


@app.route('/api/content-ideen/generate', methods=['POST'])

@login_required
def generate_content_ideen():
    import anthropic as _ant
    d = request.get_json() or {}
    account_id = int(d['account_id'])
    count = min(int(d.get('count', 15)), 30)
    focus = d.get('focus', '').strip()  # optionaler Fokus z.B. "Weihnachten"

    acc = Account.query.get_or_404(account_id)
    ctx = AccountIdeenContext.query.filter_by(account_id=account_id).first()

    api_key = os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key konfiguriert.'})

    konzept    = (ctx.konzept    if ctx else '') or f'Instagram-Seite: {acc.name}'
    zielgruppe = (ctx.zielgruppe if ctx else '') or 'Allgemein'
    tonalitaet = (ctx.tonalitaet if ctx else '') or 'Humorvoll, nahbar'
    themen     = (ctx.themen     if ctx else '') or 'Stadtleben, Humor, lokale Themen'
    kategorie  = acc.category.name if acc.category else 'Allgemein'

    analyse_category = (ctx.analyse_category or '') if ctx else ''
    is_meme_page = any(w in (analyse_category + ' ' + konzept + ' ' + tonalitaet).lower()
                       for w in ['meme', 'humor', 'satire', 'witzig', 'komisch', 'fun'])

    # WHY-Analyse: nur die relevanten Abschnitte extrahieren
    page_analysis = (ctx.page_analysis or '') if ctx else ''
    why_hint = ''
    if page_analysis:
        # Erste 800 Zeichen sind meistens TOP_POSTS_MUSTER + WARUM_LIEFEN_SIE
        why_hint = '\n\nWAS BEI DIESER SEITE VIRAL GEHT (aus Analyse):\n' + page_analysis[:900].strip()

    aktuell_hint = f'\n\nAKTUELLE THEMEN / ANLÄSSE (heute relevant):\n{focus}' if focus else ''

    if is_meme_page:
        prompt = f"""Du bist ein Meme-Stratege der versteht wie virales Internet-Humor funktioniert.

Erstelle {count} konkrete Meme-Ideen für diese Seite:

SEITE: {acc.name}
KONZEPT: {konzept}
ZIELGRUPPE: {zielgruppe}
TON: {tonalitaet}{aktuell_hint}{why_hint}

MEME-FORMATE die du nutzen kannst:
- POV: [Situation] — Identifikation, jeder kennt das
- Erwartung vs. Realität — lokaler Kontrast, Enttäuschung/Überraschung
- Ranking / Top 5 — zieht immer, Diskussion entsteht
- Twitter/X-Screenshot-Style — wirkt authentisch, niedrige Produktionshürde
- Vergleich: [A] vs. [B] — zwei Bilder, eine Aussage
- "Wenn du..." — Einleitung die sofort Wiedererkennung triggert
- Lokales Ereignis + viraler Trend gemischt — Timing ist alles
- Kommentar-Köder — bewusst kontroverse aber harmlose Aussage die Diskussion triggert

Für jede Idee dieses Format, durch --- getrennt:
TITEL: [max. 60 Zeichen]
FORMAT: [Reel / Foto / Karussell]
MEME_TYP: [welches Meme-Format aus der Liste oben]
TRIGGER: [Was macht es relatable/viral? Welches Gefühl löst es aus: Wiedererkennung / Empörung / Hype / Nostalgie / Lachen?]
IDEE: [Konkret: was ist zu sehen, was steht drauf, wie ist es aufgebaut?]
CAPTION: [kurz, 1-2 Sätze, kein Hashtag — oft reicht ein Satz oder sogar nichts]
HASHTAGS: [4-6 Hashtags]
---

Regeln: Kein generisches "Postet ein lustiges Bild über X". Jede Idee muss sofort umsetzbar sein — ich muss genau wissen was ich filmen/designen soll."""
    else:
        prompt = f"""Du bist ein kreativer Social-Media-Stratege für Instagram.

Erstelle genau {count} konkrete Content-Ideen für diese Seite:

SEITE: {acc.name}
KATEGORIE: {kategorie}
KONZEPT: {konzept}
ZIELGRUPPE: {zielgruppe}
TON/STIL: {tonalitaet}
THEMEN: {themen}{aktuell_hint}{why_hint}

Für jede Idee dieses Format, durch --- getrennt:
TITEL: [kurzer Titel, max. 60 Zeichen]
FORMAT: [Foto / Story / Reel / Karussell]
IDEE: [2-3 Sätze: Was wird gezeigt? Was macht es besonders?]
CAPTION: [1-2 kurze Sätze, kein Hashtag — kurze Captions performen besser]
HASHTAGS: [5-8 passende Hashtags]
---

Wichtig: Ideen müssen sehr spezifisch und umsetzbar sein. Keine generischen Tipps."""

    try:
        client = _ant.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=5000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        _log_ai('ideen_generate', resp)
        raw = resp.content[0].text.strip()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

    # Parsen
    ideas = []
    for block in raw.split('---'):
        block = block.strip()
        if not block:
            continue
        idea = {}
        for line in block.splitlines():
            for key, field in [('TITEL:', 'titel'), ('FORMAT:', 'format'),
                                ('MEME_TYP:', 'meme_typ'), ('TRIGGER:', 'trigger'),
                                ('IDEE:', 'idee'), ('CAPTION:', 'caption'),
                                ('HASHTAGS:', 'hashtags')]:
                if line.startswith(key):
                    idea[field] = line[len(key):].strip()
        if 'titel' in idea and 'idee' in idea:
            ideas.append(idea)

    # Speichern
    if ctx:
        ctx.last_generated = datetime.utcnow()
        ctx.generated_ideas = json.dumps(ideas, ensure_ascii=False)
        db.session.commit()

    return jsonify({'ok': True, 'ideas': ideas, 'count': len(ideas)})


@app.route('/api/content-ideen/<int:account_id>/save-idea', methods=['POST'])
@login_required
def save_idea_to_vorrat(account_id):
    """Speichert eine einzelne KI-Idee als ContentItem in den Vorrat."""
    d = request.get_json() or {}
    acc = Account.query.get_or_404(account_id)
    folder_id = d.get('folder_id') or None
    item = ContentItem(
        account_id=account_id,
        folder_id=folder_id,
        title=d.get('titel', 'KI-Idee')[:200],
        caption=(d.get('caption', '') + '\n\n' + d.get('hashtags', '')).strip(),
        status='draft',
        content_type=d.get('format', 'feed').lower(),
        ai_headline=d.get('titel', ''),
        ai_caption=d.get('idee', ''),
        created_at=datetime.utcnow(),
    )
    db.session.add(item)
    db.session.commit()
    return jsonify({'ok': True, 'id': item.id})


# ═══════════════════════════════════════════════════════════════
# KOOPERATIONS-TRACKER
# ═══════════════════════════════════════════════════════════════

_AI_PRICING = {
    # USD pro Million Tokens → in EUR (×0.92) — Preise Stand 2026-06
    'claude-haiku-4-5':           {'in': 1.00,  'out': 5.00},
    'claude-haiku-4-5-20251001':  {'in': 1.00,  'out': 5.00},
    'claude-sonnet-4-6':          {'in': 3.00,  'out': 15.00},
    'claude-opus-4-5':            {'in': 5.00,  'out': 25.00},
    'claude-opus-4-8':            {'in': 5.00,  'out': 25.00},
}

def _log_ai(feature, resp):
    """Logt einen API-Call nach client.messages.create(). resp = Anthropic-Response."""
    try:
        p = _AI_PRICING.get(resp.model, {'in': 3.00, 'out': 15.00})
        it, ot = resp.usage.input_tokens, resp.usage.output_tokens
        cost = (it * p['in'] + ot * p['out']) / 1_000_000 * 0.92
        entry = AiUsageLog(feature=feature, model=resp.model,
                           input_tokens=it, output_tokens=ot, cost_eur=cost)
        db.session.add(entry)
        db.session.commit()
    except Exception:
        pass


def _koop_ref_date(k):
    """Primäres Datum einer Koop: frühestes Posting → start_date → deadline → created_at."""
    from datetime import date as _d
    if k.posting_dates:
        try:
            dates = [_d.fromisoformat(x) for x in json.loads(k.posting_dates) if x]
            if dates: return min(dates)
        except: pass
    return k.start_date or k.deadline or (k.created_at.date() if k.created_at else _d.min)


def _check_koop_reminders():
    """Erinnerungen: 3 Tage vor Posting + Rechnung (3W nach Posting) + Zahlung (2W nach Rechnung)
    + Deadline morgen + Follow-up nach 14 Tagen ohne Reaktion."""
    from datetime import date as _d, timedelta as _td
    try:
        with app.app_context():
            today = _d.today()
            in_3_days = today + _td(days=3)
            tomorrow  = today + _td(days=1)
            fourteen_days_ago = today - _td(days=14)
            koops = Kooperation.query.filter(
                Kooperation.status.in_(['aktiv', 'abgeschlossen', 'anfrage'])
            ).all()
            changed = False
            for k in koops:
                # Erinnerung 0: 3 Tage VOR dem Posting
                if not getattr(k, 'posting_reminder_sent', False) and k.posting_dates:
                    try:
                        dates = [_d.fromisoformat(x) for x in json.loads(k.posting_dates) if x]
                        upcoming = [d for d in dates if today <= d <= in_3_days]
                    except: upcoming = []
                    if upcoming:
                        next_post = min(upcoming)
                        days_until = (next_post - today).days
                        db.session.add(SystemAlert(
                            account_id=k.account_id,
                            alert_type='koop_posting_soon',
                            severity='info',
                            message=(f'📅 Koop {k.partner_name}: Posting in {days_until} Tag(en) am {next_post}! '
                                     f'Content bereit?'),
                        ))
                        k.posting_reminder_sent = True
                        changed = True
                # Erinnerung 1: Rechnung noch nicht gesendet, 3 Wochen nach erstem Posting
                if (not k.invoice_reminder_sent and not k.invoice_sent_at
                        and not k.payment_received_at and k.posting_dates):
                    try:
                        dates = [_d.fromisoformat(x) for x in json.loads(k.posting_dates) if x]
                        first = min(dates) if dates else None
                    except: first = None
                    if first and (today - first).days >= 21:
                        db.session.add(SystemAlert(
                            account_id=k.account_id,
                            alert_type='koop_invoice_due',
                            severity='warning',
                            message=f'💼 Koop {k.partner_name}: Rechnung noch nicht gesendet! '
                                    f'Erstes Posting war {first} (vor {(today-first).days} Tagen).',
                        ))
                        k.invoice_reminder_sent = True
                        changed = True

                # Erinnerung 2: Rechnung gesendet, Zahlung nach 2 Wochen noch ausstehend
                if (not k.payment_reminder_sent and k.invoice_sent_at
                        and not k.payment_received_at):
                    if (today - k.invoice_sent_at).days >= 14:
                        db.session.add(SystemAlert(
                            account_id=k.account_id,
                            alert_type='koop_payment_due',
                            severity='warning',
                            message=f'💰 Koop {k.partner_name}: Zahlung noch nicht eingegangen! '
                                    f'Rechnung wurde am {k.invoice_sent_at} gesendet '
                                    f'({(today - k.invoice_sent_at).days} Tage her).',
                        ))
                        k.payment_reminder_sent = True
                        changed = True

                # Erinnerung 3: Deadline morgen
                if k.deadline == tomorrow and k.status == 'aktiv':
                    msg = f'⚠️ Koop {k.partner_name}: Deadline ist MORGEN ({tomorrow})!'
                    db.session.add(SystemAlert(
                        account_id=k.account_id,
                        alert_type='koop_deadline_tomorrow',
                        severity='warning',
                        message=msg,
                    ))
                    _send_central_alert(msg)
                    changed = True

                # Erinnerung 4: Follow-up — 14 Tage in "Anfrage" ohne Reaktion
                if (not getattr(k, 'follow_up_reminder_sent', False)
                        and k.status == 'anfrage'
                        and k.created_at.date() <= fourteen_days_ago):
                    msg = f'📬 Koop {k.partner_name}: Noch keine Reaktion nach 14 Tagen — Follow-up empfohlen'
                    db.session.add(SystemAlert(
                        account_id=k.account_id,
                        alert_type='koop_follow_up',
                        severity='info',
                        message=msg,
                    ))
                    k.follow_up_reminder_sent = True
                    _send_central_alert(msg)
                    changed = True

            if changed:
                db.session.commit()
    except Exception as e:
        pass


# ── Ausgaben ──────────────────────────────────────────────────

DEFAULT_AUSGABE_KATEGORIEN = [
    'Software & Tools', 'Equipment & Hardware', 'Marketing & Werbung',
    'Freelancer & Dienstleister', 'Büro & Verwaltung', 'Reise & Transport',
    'Weiterbildung', 'Sonstiges'
]

def get_ausgabe_kategorien():
    raw = get_setting('ausgabe_kategorien')
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return DEFAULT_AUSGABE_KATEGORIEN[:]


@app.route('/ausgaben')
@login_required
def ausgaben():
    from datetime import date
    jahr = request.args.get('jahr', date.today().year, type=int)
    alle = Ausgabe.query.filter(
        db.extract('year', Ausgabe.datum) == jahr
    ).order_by(Ausgabe.datum.desc()).all()

    gesamt        = sum(a.betrag for a in alle)
    finanzamt_sum = sum(a.betrag for a in alle if a.finanzamt)
    privat_sum    = sum(a.betrag for a in alle if not a.finanzamt)

    monate_fa  = [0.0] * 12
    monate_prv = [0.0] * 12
    for a in alle:
        m = a.datum.month - 1
        if a.finanzamt: monate_fa[m]  += a.betrag
        else:           monate_prv[m] += a.betrag

    items = [{
        'id': a.id, 'titel': a.titel, 'betrag': a.betrag,
        'kategorie': a.kategorie, 'datum': a.datum.isoformat(),
        'finanzamt': a.finanzamt, 'notizen': a.notizen or '',
        'beleg_url': a.beleg_url or '',
    } for a in alle]

    # Abos
    abos_all = AboKosten.query.order_by(AboKosten.aktiv.desc(), AboKosten.name).all()
    def monatlich(a):
        if a.intervall == 'jährlich':     return round(a.betrag / 12, 2)
        if a.intervall == 'quartalsweise': return round(a.betrag / 3, 2)
        return a.betrag
    abo_items = [{
        'id': a.id, 'name': a.name, 'betrag': a.betrag, 'intervall': a.intervall,
        'aktiv': a.aktiv, 'kategorie': a.kategorie, 'finanzamt': a.finanzamt,
        'notizen': a.notizen or '', 'start_datum': a.start_datum.isoformat() if a.start_datum else '',
        'monatlich': monatlich(a),
    } for a in abos_all]
    abo_monatlich     = sum(monatlich(a) for a in abos_all if a.aktiv)
    abo_jaehrlich     = round(abo_monatlich * 12, 2)
    abo_finanzamt_mo  = sum(monatlich(a) for a in abos_all if a.aktiv and a.finanzamt)

    try:
        geplant_budget = float(get_setting('geplant_budget') or 0)
    except Exception:
        geplant_budget = 0.0

    try:
        monat_budget = float(get_setting('monat_budget') or 0)
    except Exception:
        monat_budget = 0.0

    today_date = date.today()
    monat_summe = sum(
        a.betrag for a in alle
        if a.datum.month == today_date.month and a.datum.year == today_date.year
    )

    jahre = db.session.query(
        db.extract('year', Ausgabe.datum)
    ).distinct().order_by(db.extract('year', Ausgabe.datum).desc()).all()
    jahre = [int(r[0]) for r in jahre if r[0]]
    if date.today().year not in jahre:
        jahre.insert(0, date.today().year)

    return render_template('ausgaben.html',
        active_page='ausgaben',
        items=items, jahr=jahr, jahre=jahre,
        gesamt=gesamt, finanzamt_sum=finanzamt_sum, privat_sum=privat_sum,
        monate_fa=monate_fa, monate_prv=monate_prv,
        kategorien=get_ausgabe_kategorien(),
        abo_items=abo_items, abo_monatlich=abo_monatlich, abo_jaehrlich=abo_jaehrlich,
        abo_finanzamt_mo=abo_finanzamt_mo,
        geplant_budget=geplant_budget,
        monat_budget=monat_budget, monat_summe=monat_summe,
        geplant_items=[{
            'id': g.id, 'name': g.name, 'url': g.url or '',
            'betrag': g.betrag, 'kategorie': g.kategorie,
            'prioritaet': g.prioritaet, 'notizen': g.notizen or '',
            'gekauft': g.gekauft,
        } for g in GeplantAusgabe.query.order_by(
            GeplantAusgabe.gekauft.asc(),
            GeplantAusgabe.prioritaet.asc(),
            GeplantAusgabe.created_at.desc()
        ).all()],
        seitenkauf_items=[{
            'id': s.id, 'name': s.name, 'handle': s.handle or '',
            'platform': s.platform, 'followers': s.followers,
            'engagement_rate': s.engagement_rate,
            'nische': s.nische or '', 'preis_vorstellung': s.preis_vorstellung,
            'unser_angebot': s.unser_angebot,
            'einigungspreis': s.einigungspreis,
            'status': s.status, 'kontakt': s.kontakt or '',
            'url': s.url or '', 'notizen': s.notizen or '',
            'in_geplant': bool(s.in_geplant),
            'gekauft_am': s.gekauft_am.isoformat() if s.gekauft_am else '',
        } for s in SeitenKauf.query.order_by(SeitenKauf.created_at.desc()).all()],
    )


@app.route('/api/seitenkauf', methods=['GET'])
@login_required
def seitenkauf_list():
    items = SeitenKauf.query.order_by(SeitenKauf.created_at.desc()).all()
    return jsonify([{
        'id': s.id, 'name': s.name, 'handle': s.handle or '',
        'platform': s.platform, 'followers': s.followers,
        'engagement_rate': s.engagement_rate,
        'nische': s.nische or '', 'preis_vorstellung': s.preis_vorstellung,
        'unser_angebot': s.unser_angebot,
        'einigungspreis': s.einigungspreis,
        'status': s.status, 'kontakt': s.kontakt or '',
        'url': s.url or '', 'notizen': s.notizen or '',
        'in_geplant': bool(s.in_geplant),
        'gekauft_am': s.gekauft_am.isoformat() if s.gekauft_am else '',
    } for s in items])


@app.route('/api/seitenkauf', methods=['POST'])
@login_required
def seitenkauf_create():
    d = request.json or {}
    if not d.get('name'):
        return jsonify({'ok': False, 'error': 'Name fehlt'}), 400
    s = SeitenKauf(
        name=d['name'], handle=d.get('handle'), platform=d.get('platform', 'Instagram'),
        followers=d.get('followers'), engagement_rate=d.get('engagement_rate'),
        nische=d.get('nische'), preis_vorstellung=d.get('preis_vorstellung'),
        unser_angebot=d.get('unser_angebot'), einigungspreis=d.get('einigungspreis'),
        status=d.get('status', 'interessant'),
        kontakt=d.get('kontakt'), url=d.get('url'), notizen=d.get('notizen'),
    )
    db.session.add(s)
    db.session.commit()
    return jsonify({'ok': True, 'id': s.id})


@app.route('/api/seitenkauf/<int:sid>', methods=['PUT'])
@login_required
def seitenkauf_update(sid):
    s = SeitenKauf.query.get_or_404(sid)
    d = request.json or {}
    for field in ['name', 'handle', 'platform', 'followers', 'engagement_rate',
                  'nische', 'preis_vorstellung', 'unser_angebot', 'einigungspreis',
                  'status', 'kontakt', 'url', 'notizen']:
        if field in d:
            setattr(s, field, d[field])
    if d.get('status') == 'gekauft' and not s.gekauft_am:
        from datetime import date as _date
        s.gekauft_am = _date.today()
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/seitenkauf/<int:sid>', methods=['DELETE'])
@login_required
def seitenkauf_delete(sid):
    s = SeitenKauf.query.get_or_404(sid)
    db.session.delete(s)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/seitenkauf/<int:sid>/zu-geplant', methods=['POST'])
@login_required
def seitenkauf_zu_geplant(sid):
    s = SeitenKauf.query.get_or_404(sid)
    s.in_geplant = True
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/seitenkauf/<int:sid>/von-geplant', methods=['POST'])
@login_required
def seitenkauf_von_geplant(sid):
    s = SeitenKauf.query.get_or_404(sid)
    s.in_geplant = False
    db.session.commit()
    return jsonify({'ok': True})


def _seed_watchlist():
    """Seed watchlist_seite table from WATCHLIST_SEED if empty."""
    if WatchlistSeite.query.first():
        return 0
    count = 0
    for (stadt, ew, bezirke, vereine, unis) in WATCHLIST_SEED:
        meta_stadt = f"ca. {ew:,} Einwohner".replace(',', '.')
        db.session.add(WatchlistSeite(stadt=stadt, ziel_typ='stadtseite',
            ziel_name=f"{stadt} – Stadtseite", ziel_meta=meta_stadt))
        count += 1
        for (bname, bmeta) in bezirke:
            db.session.add(WatchlistSeite(stadt=stadt, ziel_typ='bezirk',
                ziel_name=bname, ziel_meta=bmeta))
            count += 1
        for vname in vereine:
            db.session.add(WatchlistSeite(stadt=stadt, ziel_typ='verein',
                ziel_name=vname, ziel_meta='Fußballverein'))
            count += 1
        for (uname, umeta) in unis:
            db.session.add(WatchlistSeite(stadt=stadt, ziel_typ='uni',
                ziel_name=uname, ziel_meta=umeta))
            count += 1
    db.session.commit()
    return count


def _wl_dict(s):
    return {
        'id': s.id, 'stadt': s.stadt, 'ziel_typ': s.ziel_typ,
        'ziel_name': s.ziel_name, 'ziel_meta': s.ziel_meta or '',
        'platform': s.platform or 'Instagram', 'url': s.url or '',
        'handle': s.handle or '', 'follower': s.follower,
        'letzte_aktivitaet': s.letzte_aktivitaet or '',
        'seiten_status': s.seiten_status or 'nicht_gesucht',
        'kaufprioritaet': s.kaufprioritaet or 'keine',
        'seiten_kategorie': s.seiten_kategorie or '',
        'preis_vorstellung': s.preis_vorstellung,
        'mein_angebot': s.mein_angebot,
        'notizen': s.notizen or '',
        'zweck': s.zweck or '',
        'ist_befreundet': bool(s.ist_befreundet),
        'seite_geplant': bool(s.seite_geplant),
        'haben_seite': bool(s.haben_seite),
        'kontaktiert_am': s.kontaktiert_am.strftime('%Y-%m-%d') if s.kontaktiert_am else None,
        'wl_kategorie': s.wl_kategorie or 'stadtseite',
    }


@app.route('/api/watchlist/stadtseiten', methods=['GET'])
@login_required
def watchlist_list():
    q = WatchlistSeite.query.filter_by(is_deleted=False)
    if request.args.get('stadt'):
        q = q.filter_by(stadt=request.args['stadt'])
    if request.args.get('ziel_typ'):
        q = q.filter_by(ziel_typ=request.args['ziel_typ'])
    if request.args.get('status'):
        q = q.filter_by(seiten_status=request.args['status'])
    items = q.order_by(WatchlistSeite.ziel_typ, WatchlistSeite.id).all()
    return jsonify([_wl_dict(s) for s in items])


@app.route('/api/watchlist/stadtseiten', methods=['POST'])
@login_required
def watchlist_create():
    d = request.json or {}
    if not d.get('ziel_name') or not d.get('stadt'):
        return jsonify({'ok': False, 'error': 'Fehlende Pflichtfelder'}), 400
    s = WatchlistSeite(
        stadt=d['stadt'], ziel_typ=d.get('ziel_typ','stadtseite'),
        ziel_name=d['ziel_name'], ziel_meta=d.get('ziel_meta'),
        platform=d.get('platform','Instagram'), url=d.get('url'),
        handle=d.get('handle'), follower=d.get('follower'),
        letzte_aktivitaet=d.get('letzte_aktivitaet'),
        seiten_status=d.get('seiten_status','nicht_gesucht'),
        kaufprioritaet=d.get('kaufprioritaet','keine'),
        seiten_kategorie=d.get('seiten_kategorie') or None,
        preis_vorstellung=d.get('preis_vorstellung'),
        mein_angebot=d.get('mein_angebot'),
        notizen=d.get('notizen'),
        zweck=d.get('zweck') or None,
        ist_befreundet=bool(d.get('ist_befreundet', False)),
        wl_kategorie=d.get('wl_kategorie','stadtseite'),
        kontaktiert_am=datetime.strptime(d['kontaktiert_am'], '%Y-%m-%d') if d.get('kontaktiert_am') else None,
    )
    db.session.add(s)
    db.session.commit()
    return jsonify({'ok': True, 'id': s.id})


@app.route('/api/watchlist/stadtseiten/<int:sid>', methods=['PUT'])
@login_required
def watchlist_update(sid):
    s = WatchlistSeite.query.get_or_404(sid)
    d = request.json or {}
    old_status = s.seiten_status
    for f in ['platform','url','handle','follower','letzte_aktivitaet','seiten_status','kaufprioritaet','seiten_kategorie','preis_vorstellung','mein_angebot','notizen','ziel_name','ziel_meta','wl_kategorie','zweck','ist_befreundet','seite_geplant','haben_seite']:
        if f in d:
            setattr(s, f, d[f])
    if d.get('kontaktiert_am'):
        try:
            s.kontaktiert_am = datetime.strptime(d['kontaktiert_am'], '%Y-%m-%d')
        except (ValueError, TypeError):
            pass
    elif d.get('seiten_status') == 'kontaktiert' and old_status != 'kontaktiert' and not s.kontaktiert_am:
        s.kontaktiert_am = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'kontaktiert_am': s.kontaktiert_am.strftime('%Y-%m-%d') if s.kontaktiert_am else None})


@app.route('/api/watchlist/stadtseiten/<int:sid>', methods=['DELETE'])
@login_required
def watchlist_delete(sid):
    """Soft-delete: moves to papierkorb instead of permanent deletion."""
    s = WatchlistSeite.query.get_or_404(sid)
    s.is_deleted = True
    s.deleted_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/watchlist/stadtseiten/<int:sid>/restore', methods=['POST'])
@login_required
def watchlist_restore(sid):
    s = WatchlistSeite.query.get_or_404(sid)
    s.is_deleted = False
    s.deleted_at = None
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/watchlist/stadtseiten/<int:sid>/permanent', methods=['DELETE'])
@login_required
def watchlist_delete_permanent(sid):
    s = WatchlistSeite.query.get_or_404(sid)
    db.session.delete(s)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/watchlist/angebote', methods=['GET'])
@login_required
def watchlist_angebote():
    """Alle Einträge mit Preis-Vorstellung oder eigenem Angebot."""
    items = WatchlistSeite.query.filter(
        WatchlistSeite.is_deleted == False,
        db.or_(WatchlistSeite.mein_angebot != None, WatchlistSeite.preis_vorstellung != None)
    ).order_by(
        db.case({'hoch':0,'mittel':1,'niedrig':2,'keine':3}, value=WatchlistSeite.kaufprioritaet),
        WatchlistSeite.stadt
    ).all()
    return jsonify([_wl_dict(s) for s in items])


@app.route('/api/watchlist/papierkorb', methods=['GET'])
@login_required
def watchlist_papierkorb():
    items = WatchlistSeite.query.filter_by(is_deleted=True)\
        .order_by(WatchlistSeite.deleted_at.desc()).all()
    return jsonify([_wl_dict(s) | {'deleted_at': s.deleted_at.strftime('%Y-%m-%d %H:%M') if s.deleted_at else None} for s in items])


@app.route('/api/watchlist/seed', methods=['POST'])
@login_required
def watchlist_seed():
    n = _seed_watchlist()
    return jsonify({'ok': True, 'created': n})


@app.route('/api/watchlist/staedte', methods=['GET'])
@login_required
def watchlist_staedte():
    """Returns list of cities with entry counts and population."""
    import re
    from sqlalchemy import func
    rows = db.session.query(
        WatchlistSeite.stadt,
        func.count(WatchlistSeite.id).label('total'),
        func.sum(db.case((WatchlistSeite.url != None, 1), else_=0)).label('gefunden'),
    ).filter_by(is_deleted=False).group_by(WatchlistSeite.stadt).order_by(WatchlistSeite.stadt).all()

    # Get population from ziel_meta of the stadtseite entry per city
    ew_map = {}
    stadtseiten = WatchlistSeite.query.filter_by(ziel_typ='stadtseite').all()
    for s in stadtseiten:
        if s.ziel_meta:
            m = re.search(r'[\d\.]+', s.ziel_meta.replace('.', ''))
            if m:
                try:
                    ew_map[s.stadt] = int(m.group())
                except ValueError:
                    pass

    city_metas = {m.stadt: m for m in WatchlistCityMeta.query.all()}

    befreundete_set = set(
        r[0] for r in db.session.query(WatchlistSeite.stadt).filter(
            WatchlistSeite.is_deleted == False, WatchlistSeite.ist_befreundet == True).all()
    )
    angebote_rows = db.session.query(
        WatchlistSeite.stadt, func.sum(WatchlistSeite.mein_angebot)
    ).filter(
        WatchlistSeite.is_deleted == False, WatchlistSeite.mein_angebot != None
    ).group_by(WatchlistSeite.stadt).all()
    angebote_map = {r[0]: float(r[1]) for r in angebote_rows if r[1]}

    return jsonify([{
        'stadt': r.stadt, 'total': r.total, 'gefunden': r.gefunden or 0,
        'ew': ew_map.get(r.stadt, 0),
        'seite_geplant': bool(city_metas[r.stadt].seite_geplant) if r.stadt in city_metas else False,
        'haben_seite': bool(city_metas[r.stadt].haben_seite) if r.stadt in city_metas else False,
        'hat_befreundete': r.stadt in befreundete_set,
        'angebote_summe': angebote_map.get(r.stadt, 0),
    } for r in rows])


@app.route('/api/watchlist/staedte/<string:stadt>/haben', methods=['PUT'])
@login_required
def watchlist_toggle_haben(stadt):
    """Toggle haben_seite flag for a city in CityMeta."""
    d = request.json or {}
    new_val = bool(d.get('haben', True))
    meta = WatchlistCityMeta.query.filter_by(stadt=stadt).first()
    if not meta:
        if not WatchlistSeite.query.filter_by(stadt=stadt, is_deleted=False).first():
            return jsonify({'ok': False, 'error': 'Keine Einträge'}), 404
        meta = WatchlistCityMeta(stadt=stadt)
        db.session.add(meta)
    meta.haben_seite = new_val
    meta.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'haben': new_val})


@app.route('/api/watchlist/staedte/<string:stadt>/geplant', methods=['PUT'])
@login_required
def watchlist_toggle_geplant(stadt):
    """Toggle seite_geplant flag for a city in CityMeta."""
    d = request.json or {}
    new_val = bool(d.get('geplant', True))
    meta = WatchlistCityMeta.query.filter_by(stadt=stadt).first()
    if not meta:
        if not WatchlistSeite.query.filter_by(stadt=stadt, is_deleted=False).first():
            return jsonify({'ok': False, 'error': 'Keine Einträge'}), 404
        meta = WatchlistCityMeta(stadt=stadt)
        db.session.add(meta)
    meta.seite_geplant = new_val
    meta.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'geplant': new_val})


@app.route('/api/watchlist/sonstige', methods=['GET'])
@login_required
def watchlist_sonstige():
    """Returns all non-stadtseite watchlist entries grouped by wl_kategorie."""
    q = WatchlistSeite.query.filter(WatchlistSeite.wl_kategorie != 'stadtseite', WatchlistSeite.is_deleted == False)
    if request.args.get('kategorie'):
        q = q.filter_by(wl_kategorie=request.args['kategorie'])
    items = q.order_by(WatchlistSeite.wl_kategorie, WatchlistSeite.id).all()
    return jsonify([_wl_dict(s) for s in items])


@app.route('/api/watchlist/kategorien', methods=['GET'])
@login_required
def watchlist_kategorien():
    """Returns distinct wl_kategorie values in use."""
    from sqlalchemy import distinct
    rows = db.session.query(distinct(WatchlistSeite.wl_kategorie)).all()
    kats = sorted([r[0] for r in rows if r[0] and r[0] != 'stadtseite'])
    return jsonify({'kategorien': kats})


@app.route('/api/watchlist/seite/<int:sid>/snapshots', methods=['GET'])
@login_required
def watchlist_snapshots(sid):
    snaps = WatchlistFollowerSnapshot.query.filter_by(seite_id=sid)\
        .order_by(WatchlistFollowerSnapshot.scanned_at.desc()).all()
    return jsonify([{
        'id': s.id, 'follower': s.follower,
        'scanned_at': s.scanned_at.strftime('%Y-%m-%d %H:%M'),
    } for s in snaps])


@app.route('/api/watchlist/scan-followers', methods=['POST'])
@login_required
def watchlist_scan_followers():
    """MANUAL-ONLY follower scan via RapidAPI. Never call this automatically."""
    d = request.json or {}
    ids = d.get('ids', [])  # list of WatchlistSeite IDs to scan
    if not ids:
        return jsonify({'ok': False, 'error': 'Keine IDs angegeben'}), 400

    seiten = WatchlistSeite.query.filter(WatchlistSeite.id.in_(ids)).all()
    if not seiten:
        return jsonify({'ok': False, 'error': 'Keine Einträge gefunden'}), 404

    # Only scan entries that have a handle
    to_scan = [s for s in seiten if s.handle]
    if not to_scan:
        return jsonify({'ok': False, 'error': 'Keine Handles hinterlegt'}), 400

    rapidapi_key = get_setting('rapidapi_key') or ''
    if not rapidapi_key:
        return jsonify({'ok': False, 'error': 'Kein RapidAPI Key konfiguriert'}), 400

    handles = [s.handle.lstrip('@') for s in to_scan]
    try:
        results, scan_errors, last_post_map = _fetch_ig_followers_rapidapi_batch(
            handles, rapidapi_key, include_last_post=True)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    updated = []
    for s in to_scan:
        handle_clean = s.handle.lstrip('@')
        count = results.get(handle_clean)
        if count is None:
            continue
        prev = s.follower
        s.follower = count
        ts = last_post_map.get(handle_clean)
        if ts:
            s.letzte_aktivitaet = datetime.fromtimestamp(ts).strftime('%d.%m.%Y')
        snap = WatchlistFollowerSnapshot(seite_id=s.id, follower=count)
        db.session.add(snap)
        wachstum = count - prev if prev is not None else None
        updated.append({
            'id': s.id, 'handle': s.handle, 'follower': count,
            'wachstum': wachstum,
            'letzte_aktivitaet': s.letzte_aktivitaet or '',
        })
    db.session.commit()
    return jsonify({'ok': True, 'updated': updated, 'scanned': len(updated)})


@app.route('/seiten-watchlist')
@login_required
def seiten_watchlist():
    _seed_watchlist()
    return render_template('seiten_watchlist.html', active_page='watchlist')


@app.route('/api/ausgaben/kategorien', methods=['GET'])
@login_required
def ausgabe_kategorien_get():
    return jsonify({'ok': True, 'kategorien': get_ausgabe_kategorien()})


@app.route('/api/ausgaben/kategorien', methods=['POST'])
@login_required
def ausgabe_kategorien_save():
    d = request.json or {}
    kategorien = [k.strip() for k in d.get('kategorien', []) if k.strip()]
    if not kategorien:
        return jsonify({'ok': False, 'error': 'Mindestens eine Kategorie nötig'}), 400
    set_setting('ausgabe_kategorien', json.dumps(kategorien, ensure_ascii=False))
    db.session.commit()
    return jsonify({'ok': True, 'kategorien': kategorien})


@app.route('/api/abos', methods=['POST'])
@login_required
def abo_create():
    from datetime import date as _date
    d = request.json or {}
    try:
        a = AboKosten(
            name      = d['name'].strip(),
            betrag    = float(d['betrag']),
            intervall = d.get('intervall', 'monatlich'),
            aktiv     = bool(d.get('aktiv', True)),
            kategorie = d.get('kategorie', 'Software & Tools'),
            finanzamt = bool(d.get('finanzamt', True)),
            notizen   = d.get('notizen', '').strip() or None,
            start_datum = _date.fromisoformat(d['start_datum']) if d.get('start_datum') else None,
        )
        db.session.add(a)
        db.session.commit()
        return jsonify({'ok': True, 'id': a.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/abos/<int:aid>', methods=['PUT'])
@login_required
def abo_update(aid):
    from datetime import date as _date
    a = AboKosten.query.get_or_404(aid)
    d = request.json or {}
    if 'name'       in d: a.name       = d['name'].strip()
    if 'betrag'     in d: a.betrag     = float(d['betrag'])
    if 'intervall'  in d: a.intervall  = d['intervall']
    if 'aktiv'      in d: a.aktiv      = bool(d['aktiv'])
    if 'kategorie'  in d: a.kategorie  = d['kategorie']
    if 'finanzamt'  in d: a.finanzamt  = bool(d['finanzamt'])
    if 'notizen'    in d: a.notizen    = d['notizen'].strip() or None
    if 'start_datum' in d:
        a.start_datum = _date.fromisoformat(d['start_datum']) if d['start_datum'] else None
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/abos/<int:aid>', methods=['DELETE'])
@login_required
def abo_delete(aid):
    a = AboKosten.query.get_or_404(aid)
    db.session.delete(a)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/ausgaben', methods=['POST'])
@login_required
def ausgabe_create():
    from datetime import date as _date
    d = request.json or {}
    try:
        a = Ausgabe(
            titel     = d['titel'].strip(),
            betrag    = float(d['betrag']),
            kategorie = d.get('kategorie', 'Sonstiges'),
            datum     = _date.fromisoformat(d['datum']),
            finanzamt = bool(d.get('finanzamt', True)),
            notizen   = d.get('notizen', '').strip() or None,
            beleg_url = d.get('beleg_url', '').strip() or None,
        )
        db.session.add(a)
        if d.get('abo'):
            abo = AboKosten(
                name       = d['titel'].strip(),
                betrag     = float(d['betrag']),
                intervall  = d.get('abo_intervall', 'monatlich'),
                kategorie  = d.get('kategorie', 'Sonstiges'),
                finanzamt  = bool(d.get('finanzamt', True)),
                notizen    = d.get('notizen', '').strip() or None,
                start_datum= _date.fromisoformat(d['datum']),
                aktiv      = True,
            )
            db.session.add(abo)
        db.session.commit()
        return jsonify({'ok': True, 'id': a.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/ausgaben/<int:aid>', methods=['PUT'])
@login_required
def ausgabe_update(aid):
    from datetime import date as _date
    a = Ausgabe.query.get_or_404(aid)
    d = request.json or {}
    try:
        if 'titel'     in d: a.titel     = d['titel'].strip()
        if 'betrag'    in d: a.betrag    = float(d['betrag'])
        if 'kategorie' in d: a.kategorie = d['kategorie']
        if 'datum'     in d: a.datum     = _date.fromisoformat(d['datum'])
        if 'finanzamt' in d: a.finanzamt = bool(d['finanzamt'])
        if 'notizen'   in d: a.notizen   = d['notizen'].strip() or None
        if 'beleg_url' in d: a.beleg_url = d['beleg_url'].strip() or None
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/ausgaben/<int:aid>', methods=['DELETE'])
@login_required
def ausgabe_delete(aid):
    a = Ausgabe.query.get_or_404(aid)
    db.session.delete(a)
    db.session.commit()
    return jsonify({'ok': True})


# ── Geplante Ausgaben ──────────────────────────────────────────
@app.route('/api/geplant', methods=['POST'])
@login_required
def geplant_create():
    d = request.json or {}
    try:
        g = GeplantAusgabe(
            name       = d['name'].strip(),
            url        = d.get('url', '').strip() or None,
            betrag     = float(d['betrag']) if d.get('betrag') else None,
            kategorie  = d.get('kategorie', 'Sonstiges'),
            prioritaet = d.get('prioritaet', 'mittel'),
            notizen    = d.get('notizen', '').strip() or None,
        )
        db.session.add(g)
        db.session.commit()
        return jsonify({'ok': True, 'id': g.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/geplant/<int:gid>', methods=['PUT'])
@login_required
def geplant_update(gid):
    from datetime import date as _date
    g = GeplantAusgabe.query.get_or_404(gid)
    d = request.json or {}
    if 'name'       in d: g.name       = d['name'].strip()
    if 'url'        in d: g.url        = d['url'].strip() or None
    if 'betrag'     in d: g.betrag     = float(d['betrag']) if d['betrag'] else None
    if 'kategorie'  in d: g.kategorie  = d['kategorie']
    if 'prioritaet' in d: g.prioritaet = d['prioritaet']
    if 'notizen'    in d: g.notizen    = d['notizen'].strip() or None
    if 'gekauft'    in d: g.gekauft    = bool(d['gekauft'])
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/geplant/<int:gid>', methods=['DELETE'])
@login_required
def geplant_delete(gid):
    g = GeplantAusgabe.query.get_or_404(gid)
    db.session.delete(g)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/geplant/<int:gid>/kaufen', methods=['POST'])
@login_required
def geplant_kaufen(gid):
    """Markiert als gekauft und legt optional eine echte Ausgabe an."""
    from datetime import date as _date
    g = GeplantAusgabe.query.get_or_404(gid)
    d = request.json or {}
    g.gekauft = True
    if g.betrag and d.get('als_ausgabe', True):
        a = Ausgabe(
            titel     = g.name,
            betrag    = float(d.get('betrag', g.betrag)),
            kategorie = g.kategorie,
            datum     = _date.fromisoformat(d['datum']) if d.get('datum') else _date.today(),
            finanzamt = bool(d.get('finanzamt', True)),
            notizen   = g.notizen,
        )
        db.session.add(a)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/ausgaben/monat-budget', methods=['POST'])
@login_required
def monat_budget_save():
    d = request.json or {}
    try:
        val = float(d.get('budget', 0))
        set_setting('monat_budget', str(val))
        db.session.commit()
        return jsonify({'ok': True, 'budget': val})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/geplant/budget', methods=['POST'])
@login_required
def geplant_budget_save():
    d = request.json or {}
    try:
        val = float(d.get('budget', 0))
        set_setting('geplant_budget', str(val))
        db.session.commit()
        return jsonify({'ok': True, 'budget': val})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/events', methods=['POST'])
@login_required
def event_create():
    from datetime import date as _date
    d = request.json or {}
    try:
        e = LocalEvent(
            name=d['name'].strip(), city=d.get('city', '').strip() or None,
            datum=_date.fromisoformat(d['datum']) if d.get('datum') else None,
            beschreibung=d.get('beschreibung', '').strip() or None,
            url=d.get('url', '').strip() or None,
            kategorie=d.get('kategorie', 'Sonstiges'),
            content_idee=d.get('content_idee', '').strip() or None,
        )
        db.session.add(e)
        db.session.commit()
        return jsonify({'ok': True, 'id': e.id})
    except Exception as ex:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(ex)}), 400


@app.route('/api/events/<int:eid>', methods=['PUT'])
@login_required
def event_update(eid):
    from datetime import date as _date
    e = LocalEvent.query.get_or_404(eid)
    d = request.json or {}
    if 'name'         in d: e.name         = d['name'].strip()
    if 'city'         in d: e.city         = d['city'].strip() or None
    if 'datum'        in d: e.datum        = _date.fromisoformat(d['datum']) if d['datum'] else None
    if 'beschreibung' in d: e.beschreibung = d['beschreibung'].strip() or None
    if 'url'          in d: e.url          = d['url'].strip() or None
    if 'kategorie'    in d: e.kategorie    = d['kategorie']
    if 'content_idee' in d: e.content_idee = d['content_idee'].strip() or None
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/events/<int:eid>', methods=['DELETE'])
@login_required
def event_delete(eid):
    e = LocalEvent.query.get_or_404(eid)
    db.session.delete(e)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/kooperationen')
@login_required
def kooperationen():
    koops = Kooperation.query.all()
    koops.sort(key=_koop_ref_date, reverse=True)
    # Alle Accounts laden (nicht nur aktive!) — sonst verliert das Bearbeiten-Formular
    # bei älteren Kooperationen, deren Account inzwischen pausiert/inaktiv ist, den
    # Account-Bezug (Dropdown hat kein passendes <option>, Speichern überschreibt
    # account_id stillschweigend mit dem ersten Eintrag der Liste).
    accounts = Account.query.order_by(Account.status != 'active', Account.name).all()
    today = now_berlin().date()  # Deadline-/Überfällig-Vergleich gegen User-Datümer
    return render_template('kooperationen.html', koops=koops,
                           accounts=accounts, today=today, active_page='kooperationen')


@app.route('/api/kooperationen', methods=['GET'])
@login_required
def koop_list():
    koops = Kooperation.query.all()
    koops.sort(key=_koop_ref_date, reverse=True)
    out = []
    for k in koops:
        delivs = []
        if k.deliverables:
            try: delivs = json.loads(k.deliverables)
            except: pass
        out.append({
            'id': k.id,
            'account_id': k.account_id,
            'account_name': k.account.name if k.account else '',
            'partner_name': k.partner_name,
            'koop_type': k.koop_type,
            'status': k.status,
            'deadline': k.deadline.isoformat() if k.deadline else None,
            'start_date': k.start_date.isoformat() if k.start_date else None,
            'amount': float(k.amount) if k.amount else None,
            'currency': k.currency or 'EUR',
            'notes': k.notes or '',
            'contact_name': k.contact_name or '',
            'contact_company': k.contact_company or '',
            'contact_street': k.contact_street or '',
            'contact_city': k.contact_city or '',
            'contact_country': k.contact_country or 'Deutschland',
            'vat_exempt': bool(k.vat_exempt),
            'payment_status': k.payment_status or 'offen',
            'deliverables': delivs,
            'partner_rating': k.partner_rating,
            'created_at': k.created_at.isoformat() if k.created_at else None,
            'payment_due_date':    k.payment_due_date.isoformat() if k.payment_due_date else None,
            'invoice_number':      k.invoice_number or '',
            'invoice_sent_at':     k.invoice_sent_at.isoformat() if k.invoice_sent_at else None,
            'payment_received_at': k.payment_received_at.isoformat() if k.payment_received_at else None,
            'payment_notes':       k.payment_notes or '',
            'posting_dates':       json.loads(k.posting_dates) if k.posting_dates else [],
            'campaign_name':       k.campaign_name or '',
        })
    return jsonify(out)


@app.route('/api/kooperationen', methods=['POST'])
@login_required
def koop_create():
    d = request.get_json() or {}
    if not (d.get('partner_name') or '').strip():
        return jsonify({'ok': False, 'error': 'Partnername fehlt'}), 400
    try:
        k = Kooperation(
            account_id=d.get('account_id') or None,
            partner_name=d['partner_name'].strip(),
            koop_type=d.get('koop_type', 'paid_post'),
            status=d.get('status', 'anfrage'),
            deadline=datetime.strptime(d['deadline'], '%Y-%m-%d').date() if d.get('deadline') else None,
            start_date=datetime.strptime(d['start_date'], '%Y-%m-%d').date() if d.get('start_date') else None,
            amount=float(d['amount']) if d.get('amount') else None,
            currency=d.get('currency', 'EUR'),
            notes=d.get('notes', '').strip(),
            contact_name=d.get('contact_name', '').strip() or None,
            contact_company=d.get('contact_company', '').strip() or None,
            contact_street=d.get('contact_street', '').strip() or None,
            contact_city=d.get('contact_city', '').strip() or None,
            contact_country=d.get('contact_country', '').strip() or 'Deutschland',
            vat_exempt=bool(d.get('vat_exempt')),
            payment_status=d.get('payment_status', 'offen'),
            deliverables=json.dumps(d.get('deliverables', []), ensure_ascii=False) if d.get('deliverables') else None,
            partner_rating=int(d['partner_rating']) if d.get('partner_rating') else None,
            payment_due_date=datetime.strptime(d['payment_due_date'], '%Y-%m-%d').date() if d.get('payment_due_date') else None,
            invoice_number=d.get('invoice_number', '').strip() or None,
            invoice_sent_at=datetime.strptime(d['invoice_sent_at'], '%Y-%m-%d').date() if d.get('invoice_sent_at') else None,
            payment_received_at=datetime.strptime(d['payment_received_at'], '%Y-%m-%d').date() if d.get('payment_received_at') else None,
            payment_notes=d.get('payment_notes', '').strip() or None,
            posting_dates=json.dumps([x for x in d.get('posting_dates', []) if x], ensure_ascii=False) if d.get('posting_dates') else None,
            campaign_name=d.get('campaign_name', '').strip() or None,
        )
        db.session.add(k)
        db.session.commit()
        return jsonify({'ok': True, 'id': k.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/kooperationen/<int:kid>', methods=['PUT', 'DELETE'])
@login_required
def koop_update(kid):
    k = Kooperation.query.get_or_404(kid)
    if request.method == 'DELETE':
        db.session.delete(k)
        db.session.commit()
        return jsonify({'ok': True})
    d = request.get_json() or {}
    k.partner_name    = d.get('partner_name', k.partner_name).strip()
    k.koop_type       = d.get('koop_type', k.koop_type)
    k.status          = d.get('status', k.status)
    if 'amount' in d:
        k.amount = float(d['amount']) if d['amount'] not in (None, '') else None
    k.currency        = d.get('currency', k.currency)
    k.notes           = d.get('notes', k.notes or '').strip()
    if 'account_id' in d:
        k.account_id = d['account_id'] or None
    k.contact_name    = d.get('contact_name', k.contact_name or '').strip() or None
    contact_company = d.get('contact_company')
    if contact_company is not None: k.contact_company = contact_company.strip() or None
    contact_street  = d.get('contact_street')
    if contact_street  is not None: k.contact_street  = contact_street.strip() or None
    contact_city    = d.get('contact_city')
    if contact_city    is not None: k.contact_city    = contact_city.strip() or None
    contact_country = d.get('contact_country')
    if contact_country is not None: k.contact_country = contact_country.strip() or 'Deutschland'
    if 'vat_exempt' in d:
        k.vat_exempt = bool(d['vat_exempt'])
    k.payment_status  = d.get('payment_status', k.payment_status or 'offen')
    if 'campaign_name' in d:
        k.campaign_name = d['campaign_name'].strip() or None
    k.partner_rating  = int(d['partner_rating']) if d.get('partner_rating') else k.partner_rating
    if d.get('deadline'):
        k.deadline = datetime.strptime(d['deadline'], '%Y-%m-%d').date()
    if 'deadline' in d and not d['deadline']:
        k.deadline = None
    if d.get('start_date'):
        k.start_date = datetime.strptime(d['start_date'], '%Y-%m-%d').date()
    if 'start_date' in d and not d['start_date']:
        k.start_date = None
    if 'deliverables' in d:
        k.deliverables = json.dumps(d['deliverables'], ensure_ascii=False)
    if 'payment_notes' in d:
        k.payment_notes = d['payment_notes'].strip() or None
    if 'posting_dates' in d:
        dates = [x for x in (d['posting_dates'] or []) if x]
        k.posting_dates = json.dumps(dates, ensure_ascii=False) if dates else None
        # Wenn neue Posting-Daten gesetzt werden, Reminder zurücksetzen falls noch nicht gesendet
        if dates and not k.invoice_sent_at:
            k.invoice_reminder_sent = False
    if 'invoice_number' in d:
        k.invoice_number = d['invoice_number'].strip() or None
    for _date_field in ('payment_due_date', 'invoice_sent_at', 'payment_received_at'):
        if _date_field in d:
            val = d[_date_field]
            setattr(k, _date_field, datetime.strptime(val, '%Y-%m-%d').date() if val else None)
    # Auto-sync payment_status from received date
    if k.payment_received_at:
        k.payment_status = 'bezahlt'
    elif k.invoice_sent_at:
        k.payment_status = 'rechnungsgestellt'
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/kooperationen/chart')
@login_required
def koop_chart():
    """Monatliche Einnahmen-Übersicht ab Jan 2025."""
    from datetime import date as _date
    start = _date(2025, 1, 1)
    today = _date.today()
    # Monatsliste aufbauen
    months, d = [], _date(start.year, start.month, 1)
    while d <= _date(today.year, today.month, 1):
        months.append(d.strftime('%Y-%m'))
        d = _date(d.year + (d.month == 12), (d.month % 12) + 1, 1)

    # Alle nicht-stornierten Koops ohne created_at-Filter —
    # Gruppierung läuft über _koop_ref_date (Posting → Deadline → created_at)
    koops = Kooperation.query.filter(Kooperation.status != 'storniert').all()

    bucket = {m: {'bezahlt': 0.0, 'ausstehend': 0.0, 'anzahl': 0} for m in months}
    from datetime import date as _d2
    for k in koops:
        if not k.amount:
            continue
        ref_date = _koop_ref_date(k)
        if not ref_date or ref_date == _d2.min:
            continue
        month = ref_date.strftime('%Y-%m')
        if month not in bucket:   # Außerhalb Jan 2025 – heute → überspringen
            continue
        bucket[month]['anzahl'] += 1
        if k.payment_status == 'bezahlt' or k.payment_received_at:
            bucket[month]['bezahlt'] += float(k.amount)
        else:
            bucket[month]['ausstehend'] += float(k.amount)

    labels = [datetime.strptime(m, '%Y-%m').strftime('%b %Y') for m in months]
    return jsonify({
        'labels':     labels,
        'bezahlt':    [bucket[m]['bezahlt']    for m in months],
        'ausstehend': [bucket[m]['ausstehend'] for m in months],
        'anzahl':     [bucket[m]['anzahl']     for m in months],
    })


@app.route('/api/kooperationen/<int:kid>/status', methods=['PATCH'])
@login_required
def koop_quick_status(kid):
    k = Kooperation.query.get_or_404(kid)
    k.status = request.get_json().get('status', k.status)
    db.session.commit()
    return jsonify({'ok': True, 'status': k.status})


@app.route('/api/kooperationen/<int:kid>/payment', methods=['PATCH'])
@login_required
def koop_quick_payment(kid):
    from datetime import date as _date
    k = Kooperation.query.get_or_404(kid)
    action = request.get_json().get('action')  # 'invoice' | 'paid' | 'reset'
    today = _date.today().isoformat()
    if action == 'invoice':
        k.invoice_sent_at = _date.today()
        k.payment_status  = 'rechnungsgestellt'
    elif action == 'paid':
        if not k.invoice_sent_at:
            k.invoice_sent_at = _date.today()
        k.payment_received_at = _date.today()
        k.payment_status      = 'bezahlt'
    elif action == 'reset':
        k.invoice_sent_at     = None
        k.payment_received_at = None
        k.payment_status      = 'offen'
    db.session.commit()
    return jsonify({
        'ok': True,
        'payment_status':      k.payment_status,
        'invoice_sent_at':     k.invoice_sent_at.isoformat() if k.invoice_sent_at else None,
        'payment_received_at': k.payment_received_at.isoformat() if k.payment_received_at else None,
    })


@app.route('/api/kooperationen/<int:kid>/deliverables', methods=['PUT'])
@login_required
def koop_save_deliverables(kid):
    k = Kooperation.query.get_or_404(kid)
    k.deliverables = json.dumps(request.get_json().get('deliverables', []), ensure_ascii=False)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/kooperationen/<int:kid>/generate-contract', methods=['POST'])
@login_required
def koop_generate_contract(kid):
    """Generiert einen Kooperationsvertrag oder Rechnung via Claude."""
    k = Kooperation.query.get_or_404(kid)
    d = request.get_json() or {}
    doc_type = d.get('type', 'vertrag')  # 'vertrag' oder 'rechnung'

    # Daten aus der Koop + optionale Felder aus Request
    partner = d.get('partner_name') or k.partner_name
    amount  = d.get('amount') or k.amount or 0
    currency = k.currency or 'EUR'
    our_name = d.get('our_name', '')
    our_address = d.get('our_address', '')
    our_tax_id  = d.get('our_tax_id', '')
    partner_address = d.get('partner_address', '')
    deliverables_text = d.get('deliverables_text', k.deliverables or '')
    posting_dates_text = ''
    if k.posting_dates:
        try:
            dates = json.loads(k.posting_dates)
            posting_dates_text = ', '.join(dates)
        except: pass
    invoice_number = d.get('invoice_number') or k.invoice_number or ''
    campaign = d.get('campaign') or k.campaign_name or ''
    extra_notes = d.get('extra_notes', '')

    api_key = get_setting('anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key konfiguriert'}), 400

    if doc_type == 'rechnung':
        prompt = f"""Erstelle eine professionelle Rechnung auf Deutsch.

RECHNUNGSSTELLER:
{our_name}
{our_address}
Steuernummer / USt-ID: {our_tax_id}

RECHNUNGSEMPFÄNGER:
{partner}
{partner_address}

RECHNUNGSNUMMER: {invoice_number or 'R-' + str(k.id).zfill(4)}
RECHNUNGSDATUM: {datetime.utcnow().strftime('%d.%m.%Y')}
FÄLLIG BIS: {(datetime.utcnow() + timedelta(days=14)).strftime('%d.%m.%Y')}

LEISTUNG:
Kampagne/Projekt: {campaign or 'Social-Media-Kooperation'}
Partner: {partner}
Posting-Termine: {posting_dates_text or 'nach Vereinbarung'}
Leistungsbeschreibung: {deliverables_text or 'Social-Media-Posting und Kooperation'}

BETRAG: {amount} {currency} (zzgl. 19% MwSt. falls zutreffend)

{('Hinweise: ' + extra_notes) if extra_notes else ''}

Erstelle die Rechnung als strukturierten, professionellen Text mit allen üblichen Rechnung-Bestandteilen. Nutze HTML-Formatierung (h2, p, table, strong, etc.) damit es druckfertig aussieht."""
    else:
        prompt = f"""Erstelle einen professionellen Kooperationsvertrag auf Deutsch.

VERTRAGSPARTEIEN:
Auftragnehmer: {our_name}, {our_address}
Auftraggeber: {partner}, {partner_address}

PROJEKT: {campaign or 'Social-Media-Kooperation'}
VERGÜTUNG: {amount} {currency}
LEISTUNGEN: {deliverables_text or 'Social-Media-Postings, Kooperation'}
POSTING-TERMINE: {posting_dates_text or 'nach Vereinbarung'}
{('WEITERE HINWEISE: ' + extra_notes) if extra_notes else ''}

Erstelle einen vollständigen, rechtlich soliden Vertrag mit:
- Vertragsgegenstand
- Leistungen des Auftragnehmers
- Vergütung und Zahlungsbedingungen
- Nutzungsrechte und Bildrechte
- Geheimhaltung
- Laufzeit und Kündigung
- Schlussbestimmungen

Nutze HTML-Formatierung (h2, h3, p, ol, strong) damit es druckfertig aussieht."""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=3000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        _log_ai('koop_vertrag', msg)
        text = msg.content[0].text
        return jsonify({'ok': True, 'html': text})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/koop-preisrechner/settings', methods=['GET'])
@login_required
def koop_preisrechner_settings_get():
    import json as _json
    raw = get_setting('koop_preisrechner_settings')
    if raw:
        try:
            return jsonify({'ok': True, 'settings': _json.loads(raw)})
        except:
            pass
    default = {'story': 20, 'post': 50, 'reel': 80, 'paket': 60,
                'engagement_bonus_5': 20, 'engagement_bonus_10': 40,
                'min_faktor': 0.8, 'max_faktor': 1.2}
    return jsonify({'ok': True, 'settings': default})


@app.route('/api/koop-preisrechner/settings', methods=['POST'])
@login_required
def koop_preisrechner_settings_save():
    import json as _json
    d = request.json or {}
    set_setting('koop_preisrechner_settings', _json.dumps(d.get('settings', {})))
    db.session.commit()
    return jsonify({'ok': True})


# ─────────────────────── PARTNER-CRM ────────────────────────────

@app.route('/partner')
@login_required
def partner_list():
    partners = Partner.query.order_by(Partner.name).all()
    # Statistik je Partner: Anzahl Koops + Umsatz
    for p in partners:
        koops = Kooperation.query.filter_by(partner_id=p.id).all()
        p._koop_count = len(koops)
        p._revenue = sum(k.amount or 0 for k in koops if k.payment_received_at)
    return render_template('partner.html', partners=partners, active_page='partner')


@app.route('/api/partner', methods=['GET'])
@login_required
def api_partner_list():
    q = request.args.get('q', '').strip()
    query = Partner.query
    if q:
        query = query.filter(Partner.name.ilike(f'%{q}%') | Partner.company.ilike(f'%{q}%'))
    partners = query.order_by(Partner.name).all()
    return jsonify([{
        'id': p.id, 'name': p.name, 'company': p.company or '',
        'email': p.email or '', 'phone': p.phone or '',
        'website': p.website or '', 'category': p.category or '',
        'status': p.status, 'rating': p.rating,
        'notes': p.notes or '',
        'account_ids': p.account_ids or '',
    } for p in partners])


@app.route('/api/partner', methods=['POST'])
@login_required
def api_partner_create():
    d = request.get_json() or {}
    if not d.get('name'):
        return jsonify({'ok': False, 'error': 'Name erforderlich'}), 400
    p = Partner(
        name=d['name'], company=d.get('company', ''),
        email=d.get('email', ''), phone=d.get('phone', ''),
        website=d.get('website', ''), category=d.get('category', ''),
        status=d.get('status', 'aktiv'), rating=d.get('rating'),
        notes=d.get('notes', ''),
        account_ids=d.get('account_ids', '') or None,
    )
    db.session.add(p)
    db.session.commit()
    return jsonify({'ok': True, 'id': p.id})


@app.route('/api/partner/<int:pid>', methods=['PUT'])
@login_required
def api_partner_update(pid):
    p = Partner.query.get_or_404(pid)
    d = request.get_json() or {}
    for field in ['name', 'company', 'email', 'phone', 'website', 'category', 'status', 'notes', 'account_ids']:
        if field in d:
            setattr(p, field, d[field])
    if 'rating' in d:
        p.rating = int(d['rating']) if d['rating'] else None
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/partner/<int:pid>/pdf')
@login_required
def api_partner_pdf(pid):
    p = Partner.query.get_or_404(pid)
    koops = Kooperation.query.filter_by(partner_id=pid).order_by(Kooperation.created_at.desc()).all()
    revenue_paid = sum(k.amount or 0 for k in koops if k.payment_received_at)
    from flask import render_template_string
    html = render_template_string("""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Partner: {{ p.name }}</title>
<style>
  body{font-family:Arial,sans-serif;max-width:700px;margin:40px auto;color:#111;font-size:13px}
  h1{font-size:22px;margin:0 0 4px}
  .sub{color:#6b7280;font-size:12px;margin-bottom:24px}
  .section{margin-bottom:20px}
  .label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#9ca3af;margin-bottom:4px}
  table{width:100%;border-collapse:collapse;margin-top:8px}
  th{background:#111;color:#fff;padding:8px 10px;font-size:11px;text-align:left}
  td{padding:8px 10px;border-bottom:1px solid #f0f0f0;font-size:12px}
  .badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:11px;font-weight:700}
  .aktiv{background:#d1fae5;color:#065f46} .inaktiv{background:#f3f4f6;color:#374151} .blacklist{background:#fee2e2;color:#991b1b}
  @media print{body{margin:20px}}
</style>
</head><body>
<div style="display:flex;justify-content:space-between;align-items:flex-start">
  <div><h1>{{ p.name }}</h1>
  {% if p.company %}<div class="sub">{{ p.company }}</div>{% endif %}</div>
  <span class="badge {{ p.status }}">{{ p.status | capitalize }}</span>
</div>
<div class="section" style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
  {% if p.email %}<div><div class="label">E-Mail</div>{{ p.email }}</div>{% endif %}
  {% if p.phone %}<div><div class="label">Telefon</div>{{ p.phone }}</div>{% endif %}
  {% if p.website %}<div><div class="label">Website</div>{{ p.website }}</div>{% endif %}
  {% if p.account_ids %}<div><div class="label">Seiten</div>{{ p.account_ids }}</div>{% endif %}
  {% if p.rating %}<div><div class="label">Bewertung</div>{{ '⭐' * p.rating }}</div>{% endif %}
  <div><div class="label">Umsatz (bezahlt)</div><b>{{ revenue_paid | round(2) }} €</b></div>
</div>
{% if p.notes %}<div class="section"><div class="label">Notizen</div><p style="margin:4px 0;line-height:1.6">{{ p.notes }}</p></div>{% endif %}
{% if koops %}
<div class="section"><div class="label">Kooperationen ({{ koops|length }})</div>
<table><thead><tr><th>Kampagne</th><th>Status</th><th>Betrag</th><th>Datum</th></tr></thead><tbody>
{% for k in koops %}
<tr><td>{{ k.campaign_name or k.koop_type }}</td><td>{{ k.status }}</td>
<td>{% if k.amount %}{{ k.amount }} €{% else %}—{% endif %}</td>
<td>{{ k.created_at.strftime('%d.%m.%Y') if k.created_at else '—' }}</td></tr>
{% endfor %}
</tbody></table></div>{% endif %}
<div style="margin-top:30px;color:#9ca3af;font-size:11px">Erstellt: {{ p.created_at.strftime('%d.%m.%Y') if p.created_at else '' }} · Content OS</div>
<script>window.print()</script>
</body></html>""", p=p, koops=koops, revenue_paid=revenue_paid)
    return html


@app.route('/api/partner/<int:pid>', methods=['DELETE'])
@login_required
def api_partner_delete(pid):
    p = Partner.query.get_or_404(pid)
    Kooperation.query.filter_by(partner_id=pid).update({'partner_id': None})
    db.session.delete(p)
    db.session.commit()
    return jsonify({'ok': True})


# ─────────────────────── POSTING-ZIEL / WEEKLY GOAL ─────────────

@app.route('/api/accounts/<int:account_id>/weekly-goal', methods=['POST'])
@login_required
def account_set_weekly_goal(account_id):
    acc = Account.query.get_or_404(account_id)
    d = request.get_json() or {}
    acc.posts_per_week = int(d.get('posts_per_week', 0) or 0)
    db.session.commit()
    return jsonify({'ok': True})


# ═══════════════════════════════════════════════════════════════
# AUTO-WASSERZEICHEN
# ═══════════════════════════════════════════════════════════════

def _apply_watermark(img_bytes, account):
    """Blendet Wasserzeichen auf ein Bild ein. Gibt neue img_bytes zurück."""
    if not account or not account.watermark_enabled or not account.watermark_url:
        return img_bytes
    try:
        import io as _io
        from PIL import Image
        import requests as _req
        base = Image.open(_io.BytesIO(img_bytes)).convert('RGBA')
        wm_resp = _req.get(account.watermark_url, timeout=5)
        wm = Image.open(_io.BytesIO(wm_resp.content)).convert('RGBA')
        # Wasserzeichen: max 20% der Bildbreite
        max_wm_w = int(base.width * 0.20)
        if wm.width > max_wm_w:
            ratio = max_wm_w / wm.width
            wm = wm.resize((max_wm_w, int(wm.height * ratio)), Image.LANCZOS)
        # Opazität
        opacity = int((account.watermark_opacity or 0.7) * 255)
        r, g, b, a = wm.split()
        a = a.point(lambda x: min(x, opacity))
        wm.putalpha(a)
        # Position
        pad = 12
        pos_map = {
            'tl': (pad, pad),
            'tr': (base.width - wm.width - pad, pad),
            'bl': (pad, base.height - wm.height - pad),
            'br': (base.width - wm.width - pad, base.height - wm.height - pad),
        }
        pos = pos_map.get(account.watermark_position or 'br', pos_map['br'])
        layer = Image.new('RGBA', base.size, (0, 0, 0, 0))
        layer.paste(wm, pos)
        result = Image.alpha_composite(base, layer).convert('RGB')
        buf = _io.BytesIO()
        result.save(buf, format='JPEG', quality=92)
        return buf.getvalue()
    except Exception as e:
        app.logger.warning('Wasserzeichen-Fehler: %s', e)
        return img_bytes


@app.route('/api/accounts/<int:account_id>/folders', methods=['GET'])
@login_required
def account_folders(account_id):
    folders = ContentFolder.query.filter_by(account_id=account_id).order_by(ContentFolder.name).all()
    return jsonify([{'id': f.id, 'name': f.name} for f in folders])


@app.route('/api/accounts/<int:account_id>/watermark', methods=['POST'])
@login_required
def upload_watermark(account_id):
    """Lädt ein Wasserzeichen-Bild für einen Account hoch."""
    acc = Account.query.get_or_404(account_id)
    if 'file' not in request.files:
        return jsonify({'ok': False, 'error': 'Keine Datei'})
    file = request.files['file']
    filename = f'watermark_{account_id}_{int(datetime.utcnow().timestamp())}.png'
    upload_folder = os.path.join(app.static_folder, 'watermarks')
    os.makedirs(upload_folder, exist_ok=True)
    filepath = os.path.join(upload_folder, filename)
    file.save(filepath)
    acc.watermark_url = f'/static/watermarks/{filename}'
    acc.watermark_enabled = True
    acc.watermark_position = request.form.get('position', 'br')
    acc.watermark_opacity = float(request.form.get('opacity', 0.7))
    db.session.commit()
    return jsonify({'ok': True, 'url': acc.watermark_url})


@app.route('/api/accounts/<int:account_id>/watermark-toggle', methods=['POST'])
@login_required
def toggle_watermark(account_id):
    acc = Account.query.get_or_404(account_id)
    acc.watermark_enabled = not acc.watermark_enabled
    db.session.commit()
    return jsonify({'ok': True, 'enabled': acc.watermark_enabled})


# ═══════════════════════════════════════════════════════════════
# TELEGRAM-BOT KOMMANDOS (Webhook für eingehende Nachrichten)
# ═══════════════════════════════════════════════════════════════

@app.route('/api/telegram/bot-webhook', methods=['POST'])
def telegram_bot_webhook():
    """Empfängt Updates vom Telegram-Bot. Reagiert account-spezifisch wenn Chat-ID einem Account gehört."""
    # ── Sicherheit: secret_token prüfen ──────────────────────────
    # Der Endpoint ist öffentlich (Telegram hat keine Session). Telegram sendet
    # den bei setWebhook hinterlegten Secret-Token im Header zurück. Ist ein
    # Secret konfiguriert, MUSS er passen — sonst stammt der Request nicht von
    # Telegram und wird verworfen. (Kein Secret = Alt-Setup, dann durchlassen,
    # bis der User den Webhook neu registriert.)
    expected_secret = get_setting('telegram_webhook_secret')
    if expected_secret:
        sent_secret = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if sent_secret != expected_secret:
            return jsonify({'ok': False}), 403

    data  = request.get_json() or {}
    token = get_setting('telegram_bot_token')
    if not token:
        return jsonify({'ok': True})

    # ── Callback-Queries (Button-Taps) ───────────────────────────
    cb = data.get('callback_query')
    if cb:
        cb_id      = cb.get('id')
        cb_data    = cb.get('data', '')
        cb_chat_id = str(cb.get('message', {}).get('chat', {}).get('id', ''))
        cb_msg_id  = cb.get('message', {}).get('message_id')
        cb_user    = cb.get('from', {}).get('first_name', 'Unbekannt')

        if cb_data.startswith('posted_'):
            try:
                post_id = int(cb_data.split('_', 1)[1])
                post = ScheduledPost.query.get(post_id)
                if post and post.status != 'published':
                    post.status       = 'published'
                    post.published_at = datetime.utcnow()
                    db.session.commit()
                    _tg_answer_callback(token, cb_id, '✅ Als gepostet markiert!', alert=False)
                    _tg_edit_message_text(token, cb_chat_id, cb_msg_id,
                        f'✅ <b>Gepostet von {cb_user}</b> um {datetime.utcnow().strftime("%H:%M")} UTC\n'
                        f'Post #{post_id}')
                else:
                    _tg_answer_callback(token, cb_id, 'Bereits markiert.', alert=False)
            except Exception as e:
                _tg_answer_callback(token, cb_id, f'Fehler: {e}', alert=True)

        elif cb_data.startswith('mcfposted_'):
            try:
                case_id = int(cb_data.split('_', 1)[1])
                case = MissingChildCase.query.get(case_id)
                if case and case.status != 'veroeffentlicht':
                    case.status = 'veroeffentlicht'
                    if not case.published_at:
                        case.published_at = datetime.utcnow()
                    db.session.commit()
                    _tg_answer_callback(token, cb_id, '✅ Als veröffentlicht markiert!', alert=False)
                    _tg_edit_message_text(token, cb_chat_id, cb_msg_id,
                        f'✅ <b>Veröffentlicht von {cb_user}</b> um {datetime.utcnow().strftime("%H:%M")} UTC\n'
                        f'Vermisstenfall #{case_id}')
                else:
                    _tg_answer_callback(token, cb_id, 'Bereits markiert.', alert=False)
            except Exception as e:
                _tg_answer_callback(token, cb_id, f'Fehler: {e}', alert=True)

        elif cb_data.startswith('pwfposted_'):
            try:
                alert_id = int(cb_data.split('_', 1)[1])
                pa = ProductAlert.query.get(alert_id)
                if pa and pa.status != 'veroeffentlicht':
                    pa.status = 'veroeffentlicht'
                    if not pa.published_at:
                        pa.published_at = datetime.utcnow()
                    db.session.commit()
                    _tg_answer_callback(token, cb_id, '✅ Als veröffentlicht markiert!', alert=False)
                    _tg_edit_message_text(token, cb_chat_id, cb_msg_id,
                        f'✅ <b>Veröffentlicht von {cb_user}</b> um {datetime.utcnow().strftime("%H:%M")} UTC\n'
                        f'Produktwarnung #{alert_id}')
                else:
                    _tg_answer_callback(token, cb_id, 'Bereits markiert.', alert=False)
            except Exception as e:
                _tg_answer_callback(token, cb_id, f'Fehler: {e}', alert=True)

        elif cb_data.startswith('morning_np_'):
            try:
                d = datetime.strptime(cb_data[len('morning_np_'):], '%Y-%m-%d').date()
                posts = (ScheduledPost.query
                    .filter(func.date(ScheduledPost.telegram_sent_at) == d,
                            ScheduledPost.status.notin_(['published', 'error']))
                    .options(joinedload(ScheduledPost.account)).all())
                lines = [f'⚠️ <b>Nicht gepostet am {d.strftime("%d.%m")}:</b>', '']
                for p in posts[:30]:
                    name = p.account.name if p.account else f'#{p.account_id}'
                    sent = p.telegram_sent_at.strftime('%H:%M') if p.telegram_sent_at else '?'
                    lines.append(f'• {name} (Bot: {sent} UTC)')
                if len(posts) > 30:
                    lines.append(f'… und {len(posts)-30} weitere')
                _tg_send_message(token, cb_chat_id, '\n'.join(lines))
                _tg_answer_callback(token, cb_id, '', alert=False)
            except Exception as e:
                _tg_answer_callback(token, cb_id, f'Fehler: {e}', alert=True)

        elif cb_data.startswith('morning_lp_'):
            try:
                berlin = ZoneInfo('Europe/Berlin')
                d = datetime.strptime(cb_data[len('morning_lp_'):], '%Y-%m-%d').date()
                raw = (ScheduledPost.query
                    .filter(func.date(ScheduledPost.telegram_sent_at) == d,
                            ScheduledPost.status == 'published',
                            ScheduledPost.published_at.isnot(None))
                    .options(joinedload(ScheduledPost.account)).all())
                posts = [p for p in raw
                         if p.published_at.replace(tzinfo=timezone.utc).astimezone(berlin).hour >= 22]
                lines = [f'🌙 <b>Nach 22 Uhr gepostet am {d.strftime("%d.%m")}:</b>', '']
                for p in posts[:30]:
                    name = p.account.name if p.account else f'#{p.account_id}'
                    sent = p.telegram_sent_at.strftime('%H:%M') if p.telegram_sent_at else '?'
                    pub  = p.published_at.replace(tzinfo=timezone.utc).astimezone(berlin).strftime('%H:%M')
                    lines.append(f'• {name} — Bot: {sent} UTC → Gepostet: {pub} Uhr')
                if len(posts) > 30:
                    lines.append(f'… und {len(posts)-30} weitere')
                _tg_send_message(token, cb_chat_id, '\n'.join(lines))
                _tg_answer_callback(token, cb_id, '', alert=False)
            except Exception as e:
                _tg_answer_callback(token, cb_id, f'Fehler: {e}', alert=True)

        elif cb_data.startswith('error_'):
            try:
                post_id = int(cb_data.split('_', 1)[1])
                post = ScheduledPost.query.get(post_id)
                if post:
                    post.status = 'error'
                    post.error_message = f'Fehler gemeldet von {cb_user} um {datetime.utcnow().strftime("%d.%m.%Y %H:%M")} UTC'
                    # SystemAlert für Dashboard
                    alert = SystemAlert(
                        account_id = post.account_id,
                        alert_type = 'post_error',
                        severity   = 'error',
                        message    = f'Post #{post_id} ({post.scheduled_at.strftime("%d.%m.%Y") if post.scheduled_at else "?"}) wurde von {cb_user} als fehlerhaft gemeldet. Bitte prüfen und erneut freigeben.',
                        resolved   = False,
                    )
                    db.session.add(alert)
                    db.session.commit()
                    _tg_answer_callback(token, cb_id, '⚠️ Fehler gemeldet! Admin wurde benachrichtigt.', alert=True)
                    _tg_edit_message_text(token, cb_chat_id, cb_msg_id,
                        f'⚠️ <b>Fehler gemeldet von {cb_user}</b>\n'
                        f'Post #{post_id} — Admin prüft den Post.')
                else:
                    _tg_answer_callback(token, cb_id, 'Post nicht gefunden.', alert=True)
            except Exception as e:
                _tg_answer_callback(token, cb_id, f'Fehler: {e}', alert=True)

        return jsonify({'ok': True})

    # ── Normale Nachrichten / Commands ───────────────────────────
    msg = data.get('message') or data.get('edited_message', {})
    if not msg:
        return jsonify({'ok': True})
    chat_id = str(msg.get('chat', {}).get('id', ''))
    text    = (msg.get('text') or '').strip()
    if not chat_id or not text.startswith('/'):
        return jsonify({'ok': True})

    def _tg_reply(txt):
        try:
            import requests as _r
            _r.post(f'https://api.telegram.org/bot{token}/sendMessage',
                    json={'chat_id': chat_id, 'text': txt, 'parse_mode': 'HTML'}, timeout=8)
        except Exception:
            pass

    cmd = text.split()[0].lower().split('@')[0]
    args = text.split()[1:]

    # Account anhand der Chat-ID identifizieren (für per-Channel-Modus)
    account = Account.query.filter_by(telegram_chat_id=chat_id).first()

    # ── /heute (/liste) — Heutige Posts für diesen Account (oder alle) ──
    if cmd in ('/heute', '/liste'):
        today = now_berlin().date()  # scheduled_at ist Berlin-naiv
        q = ScheduledPost.query.filter(
            func.date(ScheduledPost.scheduled_at) == today,
            ScheduledPost.status.in_(['scheduled', 'draft'])
        )
        if account:
            q = q.filter(ScheduledPost.account_id == account.id)
        posts = q.order_by(ScheduledPost.scheduled_at).limit(10).all()
        if posts:
            header = f'<b>📅 Heute {today.strftime("%d.%m")} – {account.name if account else "Alle Accounts"}</b>'
            lines  = [header]
            for p in posts:
                status_icon = '📤' if p.telegram_sent_at else ('✅' if p.status == 'scheduled' else '✏️')
                posted_mark = ' ✅ gepostet' if p.status == 'published' else ''
                lines.append(f'{status_icon} #{p.id} {p.scheduled_at.strftime("%H:%M")}{posted_mark}')
            _tg_reply('\n'.join(lines))
        else:
            name = f' für {account.name}' if account else ''
            _tg_reply(f'Heute keine Posts geplant{name}.')

    # ── /naechste [n] — Nächste n Posts ──
    elif cmd == '/naechste':
        n = min(int(args[0]) if args and args[0].isdigit() else 5, 10)
        now = now_berlin()  # gegen scheduled_at (Berlin-naiv)
        q = ScheduledPost.query.filter(
            ScheduledPost.scheduled_at >= now,
            ScheduledPost.status.in_(['scheduled', 'draft'])
        )
        if account:
            q = q.filter(ScheduledPost.account_id == account.id)
        posts = q.order_by(ScheduledPost.scheduled_at).limit(n).all()
        if posts:
            header = f'<b>⏭ Nächste {n} Posts{" – " + account.name if account else ""}</b>'
            lines  = [header]
            for p in posts:
                date_str = p.scheduled_at.strftime('%d.%m %H:%M')
                cap_preview = (p.caption or '')[:50].replace('\n', ' ')
                lines.append(f'• #{p.id} {date_str}\n  {cap_preview}{"…" if len(p.caption or "") > 50 else ""}')
            _tg_reply('\n'.join(lines))
        else:
            _tg_reply('Keine kommenden Posts geplant.')

    # ── /vorrat — Content-Vorrat für diesen Account ──
    elif cmd == '/vorrat':
        if account:
            d = account.feed_stock_days()
            emoji = '🟢' if d >= 14 else '🟡' if d >= 7 else '🔴'
            total = ScheduledPost.query.filter_by(
                account_id=account.id, status='scheduled'
            ).filter(ScheduledPost.scheduled_at >= now_berlin()).count()
            _tg_reply(
                f'{emoji} <b>{account.name}</b>\n'
                f'Vorrat: <b>{round(d, 1)} Tage</b> ({total} Posts geplant)\n'
                f'Follower: {account.follower_count or 0:,}'
            )
        else:
            # Alle Accounts (globale Nutzung)
            accounts = Account.query.filter_by(status='active').all()
            lines = ['<b>📊 Vorrat-Übersicht</b>']
            for acc in accounts[:10]:
                d = acc.feed_stock_days()
                emoji = '🟢' if d >= 14 else '🟡' if d >= 7 else '🔴'
                lines.append(f'{emoji} {acc.name}: {round(d, 1)}T')
            _tg_reply('\n'.join(lines))

    # ── /follower — Follower-Zahlen ──
    elif cmd == '/follower':
        if account:
            snaps = AnalyticsSnapshot.query.filter_by(account_id=account.id)\
                .order_by(AnalyticsSnapshot.recorded_at.desc()).limit(2).all()
            current = snaps[0].followers if snaps else (account.follower_count or 0)
            delta   = current - snaps[1].followers if len(snaps) >= 2 else 0
            delta_str = f' ({("+" if delta >= 0 else "")}{delta:,} seit gestern)' if delta != 0 else ''
            _tg_reply(
                f'👥 <b>{account.name}</b>\n'
                f'{current:,} Follower{delta_str}'
            )
        else:
            accounts = Account.query.filter_by(status='active')\
                .order_by(Account.follower_count.desc()).limit(8).all()
            lines = ['<b>👥 Follower-Übersicht</b>']
            for acc in accounts:
                lines.append(f'• {acc.name}: {acc.follower_count or 0:,}')
            _tg_reply('\n'.join(lines))

    # ── /gepostet [id] — Post als auf Instagram gepostet markieren ──
    elif cmd == '/gepostet':
        if not args:
            _tg_reply('Verwendung: /gepostet [post-id]\nBeispiel: /gepostet 42')
        else:
            try:
                post = ScheduledPost.query.get(int(args[0]))
                if not post:
                    _tg_reply(f'Post #{args[0]} nicht gefunden.')
                elif account and post.account_id != account.id:
                    _tg_reply('Dieser Post gehört nicht zu diesem Account-Channel.')
                else:
                    post.status     = 'published'
                    post.published_at = datetime.utcnow()
                    db.session.commit()
                    acc_name = post.account.name if post.account else ''
                    _tg_reply(f'✅ Post #{post.id} ({acc_name}) als gepostet markiert!')
            except Exception as e:
                _tg_reply(f'Fehler: {e}')

    # ── /status — Überblick ──
    elif cmd == '/status':
        if account:
            d = account.feed_stock_days()
            emoji = '🟢' if d >= 14 else '🟡' if d >= 7 else '🔴'
            today_count = ScheduledPost.query.filter(
                ScheduledPost.account_id == account.id,
                func.date(ScheduledPost.scheduled_at) == now_berlin().date(),
                ScheduledPost.status.in_(['scheduled', 'draft'])
            ).count()
            _tg_reply(
                f'{emoji} <b>{account.name}</b>\n'
                f'Vorrat: {round(d, 1)} Tage\n'
                f'Follower: {account.follower_count or 0:,}\n'
                f'Heute geplant: {today_count} Post(s)'
            )
        else:
            accounts = Account.query.filter_by(status='active').all()
            lines = ['<b>Content OS Status</b>']
            for acc in accounts[:8]:
                d = acc.feed_stock_days()
                emoji = '🟢' if d >= 14 else '🟡' if d >= 7 else '🔴'
                lines.append(f'{emoji} {acc.name}: {round(d, 1)}T | {acc.follower_count or 0:,} Follower')
            _tg_reply('\n'.join(lines))

    # ── /check — Noch nicht gepostete Posts heute ──
    elif cmd == '/check':
        today_utc = datetime.utcnow().date()
        pending = (ScheduledPost.query
            .filter(func.date(ScheduledPost.telegram_sent_at) == today_utc,
                    ScheduledPost.status.notin_(['published', 'error']))
            .options(joinedload(ScheduledPost.account)).all())
        if not pending:
            _tg_reply('✅ Alle heutigen Posts wurden gepostet!')
        else:
            lines = [f'⏳ <b>Noch nicht gepostet heute ({len(pending)}):</b>', '']
            for p in pending[:30]:
                name = p.account.name if p.account else f'#{p.account_id}'
                sent = p.telegram_sent_at.strftime('%H:%M') if p.telegram_sent_at else '?'
                lines.append(f'• {name} (Bot: {sent} UTC)')
            if len(pending) > 30:
                lines.append(f'… und {len(pending)-30} weitere')
            _tg_reply('\n'.join(lines))

    # ── /hilfe ──
    elif cmd == '/hilfe':
        acc_name = f' ({account.name})' if account else ''
        _tg_reply(
            f'<b>Content OS Bot{acc_name}</b>\n\n'
            '/check — Wer hat heute noch nicht gepostet?\n'
            '/heute — Heutige geplante Posts\n'
            '/naechste [n] — Nächste n Posts (Standard: 5)\n'
            '/vorrat — Content-Vorrat in Tagen\n'
            '/follower — Follower-Zahlen\n'
            '/gepostet [id] — Post als gepostet markieren\n'
            '/status — Kurzübersicht\n'
            '/hilfe — Diese Hilfe'
        )
    else:
        _tg_reply('Unbekanntes Kommando. /hilfe für alle Befehle.')

    return jsonify({'ok': True})


@app.route('/api/telegram/register-webhook', methods=['POST'])
@login_required
def telegram_register_webhook():
    """Registriert den Webhook bei Telegram — eine Anfrage genügt."""
    token = get_setting('telegram_bot_token')
    if not token:
        return jsonify({'ok': False, 'error': 'Kein Bot-Token konfiguriert.'})
    # Render-URL aus Request-Host ableiten oder aus AppSettings lesen
    base_url = get_setting('app_base_url') or f'https://{request.host}'
    webhook_url = f'{base_url}/api/telegram/bot-webhook'
    # Secret-Token generieren + speichern: Telegram sendet ihn bei jedem Update
    # im Header X-Telegram-Bot-Api-Secret-Token zurück → Webhook akzeptiert dann
    # nur noch echte Telegram-Requests. (1–256 Zeichen A-Z a-z 0-9 _ -)
    import secrets as _secrets
    secret = _secrets.token_urlsafe(32)
    try:
        import requests as _r
        res = _r.post(
            f'https://api.telegram.org/bot{token}/setWebhook',
            json={'url': webhook_url, 'secret_token': secret}, timeout=10
        ).json()
        if res.get('ok'):
            set_setting('telegram_webhook_secret', secret)
            db.session.commit()
            return jsonify({'ok': True, 'webhook_url': webhook_url})
        return jsonify({'ok': False, 'error': res.get('description', 'Unbekannter Fehler')})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


# ═══════════════════════════════════════════════════════════════
# FEATURE-TOGGLES (KI-Caption, Smart-Refill, Duplikat, Wasserzeichen)
# ═══════════════════════════════════════════════════════════════

@app.route('/api/settings/feature-toggles', methods=['GET'])
@login_required
def get_feature_toggles():
    return jsonify({
        'ki_caption':        get_setting('ki_caption_enabled', 'true') == 'true',
        'smart_refill':      get_setting('smart_refill_enabled', 'false') == 'true',
        'duplicate_check':   get_setting('duplicate_check_enabled', 'true') == 'true',
        'auto_watermark':    get_setting('auto_watermark_global', 'false') == 'true',
        'refill_days':       int(get_setting('smart_refill_threshold_days') or 7),
    })


@app.route('/api/settings/feature-toggles', methods=['POST'])
@login_required
def set_feature_toggle():
    d = request.get_json() or {}
    mapping = {
        'ki_caption':      'ki_caption_enabled',
        'smart_refill':    'smart_refill_enabled',
        'duplicate_check': 'duplicate_check_enabled',
        'auto_watermark':  'auto_watermark_global',
    }
    updated = {}
    for key, setting_key in mapping.items():
        if key in d:
            val = 'true' if d[key] else 'false'
            set_setting(setting_key, val)
            updated[key] = d[key]
    if 'refill_days' in d:
        set_setting('smart_refill_threshold_days', str(int(d['refill_days'])))
        updated['refill_days'] = int(d['refill_days'])
    return jsonify({'ok': True, 'updated': updated})


@app.route('/api/settings/get', methods=['GET'])
@login_required
def api_settings_get():
    key = request.args.get('key', '').strip()
    if not key:
        return jsonify({'ok': False, 'error': 'key required'}), 400
    return jsonify({'ok': True, 'key': key, 'value': get_setting(key) or ''})


@app.route('/api/settings/set', methods=['POST'])
@login_required
def api_settings_set():
    d = request.get_json() or {}
    key   = (d.get('key') or '').strip()
    value = (d.get('value') or '')
    if not key:
        return jsonify({'ok': False, 'error': 'key required'}), 400
    set_setting(key, value)
    return jsonify({'ok': True})


# ═══════════════════════════════════════════════════════════════
# RECHNUNGSGENERATOR
# ═══════════════════════════════════════════════════════════════

INVOICE_SETTINGS_KEYS = [
    'invoice_sender_name', 'invoice_sender_street', 'invoice_sender_city',
    'invoice_sender_email', 'invoice_sender_phone',
    'invoice_sender_iban', 'invoice_sender_bic', 'invoice_sender_bank_name',
    'invoice_sender_tax_number', 'invoice_sender_vat_number',
    'invoice_sender_is_kleinunternehmer',
    'invoice_prefix', 'invoice_payment_days',
    'invoice_logo_b64',
]


@app.route('/api/invoice/settings', methods=['GET'])
@login_required
def invoice_settings_get():
    data = {k: get_setting(k, '') for k in INVOICE_SETTINGS_KEYS}
    data.setdefault('invoice_prefix', 'RE')
    data.setdefault('invoice_payment_days', '14')
    data.setdefault('invoice_sender_is_kleinunternehmer', 'true')
    return jsonify(data)


@app.route('/api/invoice/settings', methods=['POST'])
@login_required
def invoice_settings_save():
    d = request.get_json() or {}
    for k in INVOICE_SETTINGS_KEYS:
        if k in d:
            set_setting(k, str(d[k]))
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/invoice/logo', methods=['POST'])
@login_required
def invoice_logo_upload():
    import base64
    f = request.files.get('logo')
    if not f:
        return jsonify({'ok': False, 'error': 'No file'})
    data = base64.b64encode(f.read()).decode()
    mime = f.mimetype or 'image/png'
    set_setting('invoice_logo_b64', f'data:{mime};base64,{data}')
    db.session.commit()
    return jsonify({'ok': True})


def _next_invoice_number():
    """Generiert die nächste Rechnungsnummer und speichert den Zähler."""
    year = datetime.utcnow().year
    counter_key = f'invoice_counter_{year}'
    prefix = get_setting('invoice_prefix', 'RE')
    current = int(get_setting(counter_key, '0') or '0')
    next_num = current + 1
    set_setting(counter_key, str(next_num))
    db.session.commit()
    return f'{prefix}-{year}-{next_num:03d}'


@app.route('/api/kooperationen/<int:kid>/rechnung/generate', methods=['POST'])
@login_required
def koop_generate_invoice_number(kid):
    """Weist dieser Kooperation eine Rechnungsnummer zu (nur wenn noch keine vorhanden)."""
    k = Kooperation.query.get_or_404(kid)
    if not k.invoice_number:
        k.invoice_number = _next_invoice_number()
        k.invoice_sent_at = datetime.utcnow().date()
        if k.payment_status == 'offen':
            k.payment_status = 'rechnungsgestellt'
        db.session.commit()
    return jsonify({'ok': True, 'invoice_number': k.invoice_number})


@app.route('/kooperationen/<int:kid>/rechnung')
@login_required
def koop_rechnung(kid):
    k = Kooperation.query.get_or_404(kid)
    settings = {key: get_setting(key, '') for key in INVOICE_SETTINGS_KEYS}
    settings.setdefault('invoice_sender_is_kleinunternehmer', 'true')
    settings.setdefault('invoice_payment_days', '14')

    deliverables = []
    if k.deliverables:
        try:
            raw = json.loads(k.deliverables)
            # Normalize: strings → dicts (alte Datenformate absichern)
            deliverables = [
                d if isinstance(d, dict) else {'text': str(d), 'done': False}
                for d in (raw if isinstance(raw, list) else [])
            ]
        except Exception:
            deliverables = []

    posting_dates = []
    if k.posting_dates:
        try:
            posting_dates = json.loads(k.posting_dates)
        except Exception:
            posting_dates = []

    payment_due = None
    if k.invoice_sent_at:
        try:
            days = int(settings.get('invoice_payment_days') or 14)
            from datetime import timedelta
            payment_due = k.invoice_sent_at + timedelta(days=days)
        except Exception:
            pass

    account = k.account
    return render_template(
        'rechnung.html',
        k=k,
        settings=settings,
        deliverables=deliverables,
        posting_dates=posting_dates,
        payment_due=payment_due,
        account=account,
        today=datetime.utcnow().date(),
    )


# ─────────────────────── CONTENT STUDIO ───────────────────────

@app.route('/content-studio')
@login_required
def content_studio():
    studio_accs = Account.query.join(
        AccountIdeenContext, AccountIdeenContext.account_id == Account.id
    ).filter(AccountIdeenContext.studio_active == True).order_by(Account.name).all()
    all_accounts = Account.query.filter(
        Account.status.in_(['active', 'pause'])
    ).order_by(Account.name).all()
    # Ensure every account has an AccountIdeenContext
    studio_ids = {a.id for a in studio_accs}
    for acc in all_accounts:
        if not acc.ideen_context:
            db.session.add(AccountIdeenContext(account_id=acc.id))
    db.session.commit()
    first = studio_accs[0] if studio_accs else None
    return render_template('content_studio.html',
        studio_accounts=studio_accs,
        all_accounts=all_accounts,
        selected_account=first,
        active_page='studio',
        automation_rules=[])


@app.route('/content-studio/<int:account_id>')
@login_required
def content_studio_account(account_id):
    acc = Account.query.get_or_404(account_id)
    studio_accs = Account.query.join(
        AccountIdeenContext, AccountIdeenContext.account_id == Account.id
    ).filter(AccountIdeenContext.studio_active == True).order_by(Account.name).all()
    all_accounts = Account.query.filter(
        Account.status.in_(['active', 'pause'])
    ).order_by(Account.name).all()
    if not acc.ideen_context:
        ctx = AccountIdeenContext(account_id=account_id)
        db.session.add(ctx)
        db.session.commit()
    automation_rules = AutomationRule.query.filter_by(account_id=account_id)\
        .order_by(AutomationRule.active.desc(), AutomationRule.name).all()
    return render_template('content_studio.html',
        studio_accounts=studio_accs,
        all_accounts=all_accounts,
        selected_account=acc,
        active_page='studio',
        automation_rules=automation_rules)


@app.route('/api/content-studio/<int:account_id>/toggle', methods=['POST'])
@login_required
def studio_toggle(account_id):
    acc = Account.query.get_or_404(account_id)
    ctx = AccountIdeenContext.query.filter_by(account_id=account_id).first()
    if not ctx:
        ctx = AccountIdeenContext(account_id=account_id)
        db.session.add(ctx)
    ctx.studio_active = not ctx.studio_active
    db.session.commit()
    return jsonify({'ok': True, 'active': ctx.studio_active})


@app.route('/api/content-studio/<int:account_id>/info', methods=['GET'])
@login_required
def studio_get_info(account_id):
    ctx = AccountIdeenContext.query.filter_by(account_id=account_id).first()
    if not ctx:
        return jsonify({})
    qa = []
    if ctx.onboarding_qa:
        try: qa = json.loads(ctx.onboarding_qa)
        except: pass
    ideas = []
    if ctx.generated_ideas:
        try: ideas = json.loads(ctx.generated_ideas)
        except: pass
    return jsonify({
        'konzept':       ctx.konzept or '',
        'zielgruppe':    ctx.zielgruppe or '',
        'tonalitaet':    ctx.tonalitaet or '',
        'themen':        ctx.themen or '',
        'usp':           ctx.usp or '',
        'page_analysis': ctx.page_analysis or '',
        'onboarding_done': bool(ctx.onboarding_done),
        'studio_active': bool(ctx.studio_active),
        'onboarding_qa': qa,
        'generated_ideas': ideas,
        'last_generated': ctx.last_generated.isoformat() if ctx.last_generated else None,
    })


@app.route('/api/content-studio/<int:account_id>/save-basics', methods=['POST'])
@login_required
def studio_save_basics(account_id):
    """Speichert Basis-Info (Step 1 des Onboardings)."""
    acc = Account.query.get_or_404(account_id)
    d = request.get_json() or {}
    ctx = AccountIdeenContext.query.filter_by(account_id=account_id).first()
    if not ctx:
        ctx = AccountIdeenContext(account_id=account_id, studio_active=True)
        db.session.add(ctx)
    ctx.konzept    = d.get('konzept', '').strip()
    ctx.zielgruppe = d.get('zielgruppe', '').strip()
    ctx.tonalitaet = d.get('tonalitaet', '').strip()
    ctx.themen     = d.get('themen', '').strip()
    ctx.usp        = d.get('usp', '').strip()
    ctx.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/content-studio/<int:account_id>/generate-questions', methods=['POST'])
@login_required
def studio_generate_questions(account_id):
    """Claude analysiert Basis-Info und generiert gezielte Nachfragen (Step 2)."""
    acc = Account.query.get_or_404(account_id)
    ctx = AccountIdeenContext.query.filter_by(account_id=account_id).first()
    if not ctx:
        return jsonify({'ok': False, 'error': 'Zuerst Basis-Info speichern'}), 400

    api_key = get_setting('anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Claude API-Key'}), 400

    prompt = f"""Du bist ein Social-Media-Stratege der einen Instagram-Account 100% verstehen will.

Account: {acc.name} (@{acc.handle or 'unbekannt'})
Plattform: Instagram
Konzept: {ctx.konzept or 'nicht angegeben'}
Zielgruppe: {ctx.zielgruppe or 'nicht angegeben'}
Ton/Stil: {ctx.tonalitaet or 'nicht angegeben'}
Themen: {ctx.themen or 'nicht angegeben'}
USP/Alleinstellungsmerkmal: {ctx.usp or 'nicht angegeben'}

Stelle 6 präzise Fragen um diesen Account wirklich zu verstehen. Die Fragen sollen:
- Spezifisch für DIESEN Account sein (nicht generisch)
- Dir helfen einzigartigen Content zu erstellen
- Auf Content-Formate, Posting-Rhythmus, Engagement, Konkurrenz, Herausforderungen eingehen
- Praxis-orientiert sein (was funktioniert, was nicht)

Antworte NUR mit einem JSON-Array: [{{"frage": "...", "erklaerung": "Warum ich das frage: ..."}}]"""

    try:
        import anthropic as _ant
        client = _ant.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=1200,
            messages=[{'role': 'user', 'content': prompt}]
        )
        _log_ai('studio_questions', msg)
        raw = msg.content[0].text.strip()
        # JSON aus Antwort extrahieren
        import re as _re
        m = _re.search(r'\[[\s\S]*\]', raw)
        questions = json.loads(m.group(0)) if m else []
        return jsonify({'ok': True, 'questions': questions})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/content-studio/<int:account_id>/complete-onboarding', methods=['POST'])
@login_required
def studio_complete_onboarding(account_id):
    """Speichert Q&A-Antworten und generiert die vollständige Seiten-DNA."""
    acc = Account.query.get_or_404(account_id)
    d = request.get_json() or {}
    qa_pairs = d.get('qa', [])  # [{frage, antwort}, ...]

    ctx = AccountIdeenContext.query.filter_by(account_id=account_id).first()
    if not ctx:
        return jsonify({'ok': False, 'error': 'Context nicht gefunden'}), 404

    ctx.onboarding_qa = json.dumps(qa_pairs, ensure_ascii=False)

    api_key = get_setting('anthropic_api_key')
    if not api_key:
        ctx.onboarding_done = True
        db.session.commit()
        return jsonify({'ok': True, 'analysis': ''})

    qa_text = '\n'.join([f"F: {q['frage']}\nA: {q.get('antwort', '(keine Antwort)')}" for q in qa_pairs])
    prompt = f"""Du bist ein Social-Media-Experte. Erstelle eine umfassende Seiten-DNA für diesen Instagram-Account.

ACCOUNT: {acc.name} (@{acc.handle or '?'})
KONZEPT: {ctx.konzept or ''}
ZIELGRUPPE: {ctx.zielgruppe or ''}
TON: {ctx.tonalitaet or ''}
THEMEN: {ctx.themen or ''}
USP: {ctx.usp or ''}

VERTIEFENDE Q&A:
{qa_text}

Erstelle eine strukturierte Seiten-DNA mit diesen Abschnitten (Markdown-Format):
## 🧬 Seiten-DNA: {acc.name}
### Kernidentität
### Zielgruppe (detailliert)
### Content-Säulen (3-5 Hauptthemen mit Gewichtung)
### Ton & Sprache
### Was funktioniert / Was vermeiden
### Posting-Strategie
### Einzigartige Stärken
### Optimierungspotenzial

Sei konkret, spezifisch und praxis-orientiert. Kein Allgemein-Blabla."""

    try:
        import anthropic as _ant
        client = _ant.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=2000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        _log_ai('studio_dna', msg)
        analysis = msg.content[0].text.strip()
        ctx.page_analysis  = analysis
        ctx.onboarding_done = True
        ctx.updated_at     = datetime.utcnow()
        db.session.commit()
        return jsonify({'ok': True, 'analysis': analysis})
    except Exception as e:
        ctx.onboarding_done = True
        db.session.commit()
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/content-studio/<int:account_id>/generate-ideas', methods=['POST'])
@login_required
def studio_generate_ideas(account_id):
    """Generiert Content-Ideen basierend auf der Seiten-DNA."""
    acc = Account.query.get_or_404(account_id)
    d = request.get_json() or {}
    extra = d.get('extra', '')  # optionaler Kontext vom User (aktuelles Thema, etc.)

    ctx = AccountIdeenContext.query.filter_by(account_id=account_id).first()
    if not ctx:
        return jsonify({'ok': False, 'error': 'Kein Kontext'}), 404

    api_key = get_setting('anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Claude API-Key'}), 400

    dna = ctx.page_analysis or ''
    if not dna:
        nl = '\n'
        dna = f"Konzept: {ctx.konzept}{nl}Zielgruppe: {ctx.zielgruppe}{nl}Ton: {ctx.tonalitaet}{nl}Themen: {ctx.themen}"
    qa_text = ''
    if ctx.onboarding_qa:
        try:
            qa = json.loads(ctx.onboarding_qa)
            qa_text = '\n'.join([f"- {q['frage']}: {q.get('antwort','')}" for q in qa])
        except: pass

    prompt = f"""Du bist Content-Stratege für den Instagram-Account "{acc.name}" (@{acc.handle or '?'}).

SEITEN-DNA:
{dna}

{f"AKTUELLER KONTEXT VOM BETREIBER: {extra}" if extra else ""}

Generiere 8 konkrete Content-Ideen für die nächsten 2 Wochen.
Jede Idee muss:
- Zum einzigartigen Stil dieser Seite passen
- Einen klaren Hook haben (erste Zeile die stoppt)
- Ein konkretes Format nennen (Reel, Karussell, Feed-Post, Story)
- Einen Posting-Tag/Zeitfenster empfehlen

Format als JSON-Array:
[{{"titel": "...", "hook": "...", "format": "reel|feed|karussell|story", "beschreibung": "...", "warum": "Warum das zur Seite passt", "tag": "Mo|Di|Mi|Do|Fr|Sa|So"}}]"""

    try:
        import anthropic as _ant
        client = _ant.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=2500,
            messages=[{'role': 'user', 'content': prompt}]
        )
        _log_ai('studio_ideas', msg)
        raw = msg.content[0].text.strip()
        import re as _re
        m = _re.search(r'\[[\s\S]*\]', raw)
        ideas = json.loads(m.group(0)) if m else []
        ctx.generated_ideas = json.dumps(ideas, ensure_ascii=False)
        ctx.last_generated  = datetime.utcnow()
        db.session.commit()
        return jsonify({'ok': True, 'ideas': ideas})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/content-studio/<int:account_id>/reset-onboarding', methods=['POST'])
@login_required
def studio_reset_onboarding(account_id):
    ctx = AccountIdeenContext.query.filter_by(account_id=account_id).first()
    if ctx:
        ctx.onboarding_done = False
        ctx.onboarding_qa   = None
        ctx.page_analysis   = None
        ctx.konzept = ctx.zielgruppe = ctx.tonalitaet = ctx.themen = ctx.usp = None
        db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/accounts/<int:account_id>/skip-onboarding', methods=['POST'])
@login_required
def api_skip_onboarding(account_id):
    acc = Account.query.get_or_404(account_id)
    ctx = acc.ideen_context or AccountIdeenContext(account_id=account_id)
    ctx.onboarding_done = True
    if not ctx.id:
        db.session.add(ctx)
    db.session.commit()
    return jsonify({'ok': True})


# ─────────────────────── TO-DO ────────────────────────────────

@app.route('/todos')
@login_required
def todos():
    items = AppTodo.query.order_by(AppTodo.done.asc(), AppTodo.priority.desc(), AppTodo.created_at.desc()).all()
    # Erste Nutzung: Content-Studio-Philosophie-Notiz vorausfüllen
    if not items:
        seed = AppTodo(
            text='Jede Seite im Content Studio bekommt eine eigene maßgeschneiderte „Fabrik" '
                 '— kein generisches Formular, sondern ein UI das genau zu dieser Content-Art passt. '
                 'Vorgehen: Seite im Chat erklären → ich designe das perfekte Layout.',
            category='feature',
            priority=1
        )
        db.session.add(seed)
        db.session.commit()
        items = [seed]
    items_data = [{
        'id': t.id, 'text': t.text, 'category': t.category or 'idee',
        'done': bool(t.done), 'priority': t.priority or 0,
        'image_path': t.image_path or '',
        'linked_page': t.linked_page or '',
        'deadline': t.deadline.isoformat() if t.deadline else None,
        'created_at': t.created_at.isoformat() if t.created_at else None
    } for t in items]
    return render_template('todos.html', active_page='todos', items=items_data)


@app.route('/api/todos', methods=['GET'])
@login_required
def api_todos_list():
    items = AppTodo.query.order_by(AppTodo.done.asc(), AppTodo.priority.desc(), AppTodo.created_at.desc()).all()
    return jsonify([{
        'id': t.id, 'title': t.title or '', 'text': t.text, 'category': t.category,
        'done': t.done, 'priority': t.priority,
        'linked_page': t.linked_page or '',
        'deadline': t.deadline.isoformat() if t.deadline else None,
        'created_at': t.created_at.isoformat() if t.created_at else None
    } for t in items])


@app.route('/api/todos', methods=['POST'])
@login_required
def api_todo_create():
    data = request.get_json() or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'ok': False, 'error': 'Text erforderlich'}), 400
    dl = data.get('deadline')
    t = AppTodo(
        title=(data.get('title') or '').strip() or None,
        text=text,
        category=data.get('category', 'idee'),
        priority=int(data.get('priority', 0)),
        linked_page=data.get('linked_page') or None,
        deadline=datetime.strptime(dl, '%Y-%m-%d').date() if dl else None,
        done=False
    )
    db.session.add(t)
    db.session.commit()
    return jsonify({'ok': True, 'id': t.id})


@app.route('/api/todos/<int:tid>', methods=['PUT'])
@login_required
def api_todo_update(tid):
    t = AppTodo.query.get_or_404(tid)
    data = request.get_json() or {}
    if 'title' in data:
        t.title = data['title'].strip() or None
    if 'text' in data:
        t.text = data['text'].strip() or t.text
    if 'category' in data:
        t.category = data['category']
    if 'done' in data:
        t.done = bool(data['done'])
    if 'priority' in data:
        t.priority = int(data['priority'])
    if 'linked_page' in data:
        t.linked_page = data['linked_page'] or None
    if 'deadline' in data:
        dl = data['deadline']
        t.deadline = datetime.strptime(dl, '%Y-%m-%d').date() if dl else None
    t.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/todos/<int:tid>', methods=['DELETE'])
@login_required
def api_todo_delete(tid):
    t = AppTodo.query.get_or_404(tid)
    db.session.delete(t)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/todos/<int:tid>/image', methods=['POST'])
@login_required
def api_todo_image(tid):
    import pathlib, uuid
    t = AppTodo.query.get_or_404(tid)
    f = request.files.get('image')
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': 'Kein Bild'}), 400
    upload_dir = pathlib.Path(app.root_path) / 'static' / 'uploads' / 'todo_images'
    upload_dir.mkdir(parents=True, exist_ok=True)
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else 'jpg'
    fname = f"{uuid.uuid4().hex}.{ext}"
    f.save(upload_dir / fname)
    t.image_path = f"/static/uploads/todo_images/{fname}"
    db.session.commit()
    return jsonify({'ok': True, 'image_path': t.image_path})


@app.route('/api/todos/<int:tid>/image', methods=['DELETE'])
@login_required
def api_todo_image_delete(tid):
    import pathlib
    t = AppTodo.query.get_or_404(tid)
    if t.image_path:
        try:
            p = pathlib.Path(app.root_path) / t.image_path.lstrip('/')
            if p.exists():
                p.unlink()
        except Exception:
            pass
        t.image_path = None
        db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/todo-categories', methods=['GET'])
@login_required
def todo_categories_get():
    raw = get_setting('todo_categories', '[]')
    try:
        cats = json.loads(raw)
    except Exception:
        cats = []
    return jsonify(cats)


@app.route('/api/todo-categories', methods=['POST'])
@login_required
def todo_categories_save():
    cats = request.get_json() or []
    set_setting('todo_categories', json.dumps(cats))
    db.session.commit()
    return jsonify({'ok': True})


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


# ══════════════════════════════════════════════════════════
#  GROWTH LAB
# ══════════════════════════════════════════════════════════

@app.route('/growth-lab')
@login_required
def growth_lab():
    from zoneinfo import ZoneInfo
    berlin = ZoneInfo('Europe/Berlin')
    today  = datetime.now(berlin).date()
    experiments = (GrowthExperiment.query
                   .options(joinedload(GrowthExperiment.category),
                            joinedload(GrowthExperiment.variants),
                            joinedload(GrowthExperiment.participants))
                   .order_by(GrowthExperiment.created_at.desc()).all())
    categories = (Category.query
                  .filter(Category.accounts.any(Account.status == 'active'))
                  .order_by(Category.name).all())
    knowledge  = (GrowthKnowledge.query
                  .options(joinedload(GrowthKnowledge.category))
                  .order_by(GrowthKnowledge.created_at.desc()).all())
    # Compute progress for running experiments
    exp_meta = {}
    for e in experiments:
        if e.status == 'running' and e.start_date:
            end_date = e.start_date + timedelta(days=e.duration_days)
            elapsed  = (today - e.start_date).days
            progress = min(100, round(elapsed / e.duration_days * 100))
            remaining = (end_date - today).days
        elif e.status == 'completed':
            progress, remaining = 100, 0
        else:
            progress, remaining = 0, e.duration_days
        exp_meta[e.id] = {'progress': progress, 'remaining': remaining}
    import json as _json
    cats_json = _json.dumps([{'id': c.id, 'name': c.name, 'color': c.color} for c in categories])
    # Build accounts per category for the create modal
    cat_accounts = {}
    for c in categories:
        cat_accounts[c.id] = [{'id': a.id, 'name': a.name, 'followers': a.follower_count or 0}
                               for a in Account.query.filter_by(category_id=c.id, status='active')
                                               .order_by(Account.name).all()]
    cat_accounts_json = _json.dumps(cat_accounts)
    return render_template('growth_lab.html',
                           experiments=experiments, categories=categories,
                           knowledge=knowledge, exp_meta=exp_meta,
                           cats_json=cats_json, cat_accounts_json=cat_accounts_json,
                           active_page='growth_lab', today=today)


@app.route('/growth-lab/<int:exp_id>')
@login_required
def growth_experiment_detail(exp_id):
    from zoneinfo import ZoneInfo
    berlin = ZoneInfo('Europe/Berlin')
    today  = datetime.now(berlin).date()
    exp = (GrowthExperiment.query
           .options(joinedload(GrowthExperiment.category),
                    joinedload(GrowthExperiment.variants),
                    joinedload(GrowthExperiment.participants).joinedload(GrowthParticipant.account))
           .filter_by(id=exp_id).first_or_404())
    # Progress
    if exp.status == 'running' and exp.start_date:
        end_date  = exp.start_date + timedelta(days=exp.duration_days)
        elapsed   = (today - exp.start_date).days
        progress  = min(100, round(elapsed / exp.duration_days * 100))
        remaining = (end_date - today).days
    elif exp.status == 'completed':
        progress, remaining = 100, 0
    else:
        progress, remaining = 0, exp.duration_days
    # Per-variant live stats
    variant_stats = _compute_variant_stats(exp, today)
    # Knowledge for this category
    knowledge = (GrowthKnowledge.query
                 .filter_by(category_id=exp.category_id)
                 .order_by(GrowthKnowledge.created_at.desc()).all())
    import json as _json
    ai_analysis = None
    if exp.ai_analysis_json:
        try:
            ai_analysis = _json.loads(exp.ai_analysis_json)
        except Exception:
            pass
    # Kategorie-Benchmark: Ø Follower-Wachstum aller Seiten der Kategorie im Experimenzeitraum
    benchmark = None
    if exp.start_date and exp.status in ('running', 'completed'):
        cat_accs = Account.query.filter_by(category_id=exp.category_id, status='active').all()
        deltas = []
        for acc in cat_accs:
            start_snap = (AnalyticsSnapshot.query
                         .filter_by(account_id=acc.id)
                         .filter(func.date(AnalyticsSnapshot.recorded_at) >= exp.start_date)
                         .order_by(AnalyticsSnapshot.recorded_at).first())
            if start_snap:
                deltas.append((acc.follower_count or 0) - start_snap.followers)
        if deltas:
            benchmark = {'avg_delta': round(sum(deltas) / len(deltas), 1), 'count': len(deltas)}
    return render_template('growth_experiment.html',
                           exp=exp, progress=progress, remaining=remaining,
                           variant_stats=variant_stats, knowledge=knowledge,
                           ai_analysis=ai_analysis, today=today,
                           benchmark=benchmark,
                           active_page='growth_lab')


def _compute_variant_stats(exp, today):
    """Returns list of dicts with aggregated stats per variant, sorted by avg follower delta."""
    results = []
    for v in exp.variants:
        parts = [p for p in exp.participants if p.variant_id == v.id]
        total_delta = 0
        total_pv    = 0
        total_reach = 0
        delta_count = 0  # only count participants with actual start values
        for p in parts:
            latest = (GrowthDataPoint.query
                      .filter_by(participant_id=p.id)
                      .order_by(GrowthDataPoint.recorded_at.desc()).first())
            if latest:
                curr_f = latest.followers or p.start_followers or 0
                total_pv    += latest.profile_visits    or 0
                total_reach += latest.reached_accounts  or 0
                total_delta += curr_f - (p.start_followers or 0)
                delta_count += 1
            elif p.start_followers:
                # Start captured but no end measurement yet — use live follower count as estimate
                acc = Account.query.get(p.account_id)
                curr_f = acc.follower_count if acc else p.start_followers
                total_delta += curr_f - p.start_followers
                delta_count += 1
            # else: start_followers=0 means experiment not started yet — don't compute delta
        has_data   = delta_count > 0
        n          = max(delta_count, 1)
        avg_delta  = round(total_delta / n, 1) if has_data else None
        total_delta_val = total_delta if has_data else None
        conversion = round(total_delta / total_pv * 100, 2) if total_pv > 0 and has_data else None
        results.append({
            'id': v.id, 'name': v.name, 'description': v.description or '',
            'color': v.color, 'is_control': v.is_control,
            'account_count': len(parts),
            'total_delta': total_delta_val, 'avg_delta': avg_delta,
            'total_profile_visits': total_pv, 'total_reached': total_reach,
            'conversion': conversion,
        })
    results.sort(key=lambda x: x['avg_delta'] if x['avg_delta'] is not None else float('-inf'), reverse=True)
    return results


@app.route('/growth-lab/new', methods=['POST'])
@login_required
def growth_lab_new():
    d = request.get_json()
    if not d or not d.get('name') or not d.get('category_id'):
        return jsonify({'ok': False, 'error': 'Name und Kategorie erforderlich'}), 400
    cat = Category.query.get(d['category_id'])
    if not cat:
        return jsonify({'ok': False, 'error': 'Kategorie nicht gefunden'}), 400
    exp = GrowthExperiment(
        name=d['name'].strip(),
        category_id=cat.id,
        goal=d.get('goal', '').strip() or None,
        description=d.get('description', '').strip() or None,
        duration_days=int(d.get('duration_days', 30)),
    )
    db.session.add(exp)
    db.session.flush()  # get exp.id
    # Create variants
    for vd in (d.get('variants') or []):
        v = GrowthVariant(
            experiment_id=exp.id,
            name=vd.get('name', 'Variante').strip(),
            description=vd.get('description', '').strip() or None,
            is_control=bool(vd.get('is_control', False)),
            color=vd.get('color', '#6366f1'),
        )
        db.session.add(v)
        db.session.flush()
        # Assign accounts — validate same category
        for acc_id in (vd.get('account_ids') or []):
            acc = Account.query.get(acc_id)
            if acc and acc.category_id == cat.id:
                p = GrowthParticipant(experiment_id=exp.id, account_id=acc.id, variant_id=v.id)
                db.session.add(p)
    db.session.commit()
    return jsonify({'ok': True, 'id': exp.id})


@app.route('/growth-lab/<int:exp_id>/start', methods=['POST'])
@login_required
def growth_lab_start(exp_id):
    from zoneinfo import ZoneInfo
    exp = GrowthExperiment.query.get_or_404(exp_id)
    if exp.status != 'draft':
        return jsonify({'ok': False, 'error': 'Experiment läuft bereits oder ist abgeschlossen'}), 400
    d = request.get_json() or {}
    berlin = ZoneInfo('Europe/Berlin')
    exp.start_date = datetime.now(berlin).date()
    exp.status     = 'running'
    # Save start values per participant
    start_values = d.get('start_values', {})  # {str(participant_id): {followers, profile_visits, reached_accounts}}
    for p in exp.participants:
        sv = start_values.get(str(p.id), {})
        # Prefer manually entered start, fallback to Account.follower_count
        if sv.get('followers') is not None:
            p.start_followers = int(sv['followers'])
        else:
            acc = Account.query.get(p.account_id)
            p.start_followers = acc.follower_count or 0 if acc else 0
        if sv.get('profile_visits') is not None:
            p.start_profile_visits = int(sv['profile_visits'])
        if sv.get('reached_accounts') is not None:
            p.start_reached_accounts = int(sv['reached_accounts'])
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/growth-lab/<int:exp_id>/complete', methods=['POST'])
@login_required
def growth_lab_complete(exp_id):
    exp = GrowthExperiment.query.get_or_404(exp_id)
    exp.status = 'completed'
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/growth-lab/<int:exp_id>/duplicate', methods=['POST'])
@login_required
def growth_lab_duplicate(exp_id):
    src = (GrowthExperiment.query
           .options(joinedload(GrowthExperiment.variants))
           .get_or_404(exp_id))
    new_exp = GrowthExperiment(
        name=src.name + ' (Kopie)',
        category_id=src.category_id,
        goal=src.goal,
        description=src.description,
        duration_days=src.duration_days,
        status='draft',
    )
    db.session.add(new_exp)
    db.session.flush()
    for v in src.variants:
        db.session.add(GrowthVariant(
            experiment_id=new_exp.id,
            name=v.name,
            description=v.description,
            is_control=v.is_control,
            color=v.color,
        ))
    db.session.commit()
    return jsonify({'ok': True, 'id': new_exp.id})


@app.route('/growth-lab/<int:exp_id>/delete', methods=['POST'])
@login_required
def growth_lab_delete(exp_id):
    exp = GrowthExperiment.query.get_or_404(exp_id)
    db.session.delete(exp)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/growth-lab/<int:exp_id>/data-point', methods=['POST'])
@login_required
def api_growth_data_point(exp_id):
    GrowthExperiment.query.get_or_404(exp_id)
    d = request.get_json()
    participant_id = d.get('participant_id')
    if not participant_id:
        return jsonify({'ok': False, 'error': 'participant_id fehlt'}), 400
    p = GrowthParticipant.query.filter_by(id=participant_id, experiment_id=exp_id).first_or_404()
    from datetime import date as _date
    recorded_at = _date.fromisoformat(d['date']) if d.get('date') else datetime.utcnow().date()
    # Upsert: one data point per participant per day
    dp = GrowthDataPoint.query.filter_by(participant_id=p.id, recorded_at=recorded_at).first()
    if not dp:
        dp = GrowthDataPoint(participant_id=p.id, recorded_at=recorded_at)
        db.session.add(dp)
    if d.get('followers')        is not None: dp.followers        = int(d['followers'])
    if d.get('profile_visits')   is not None: dp.profile_visits   = int(d['profile_visits'])
    if d.get('reached_accounts') is not None: dp.reached_accounts = int(d['reached_accounts'])
    if d.get('notes')            is not None: dp.notes            = d['notes'].strip() or None
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/growth-lab/<int:exp_id>/chart-data')
@login_required
def api_growth_chart(exp_id):
    exp = GrowthExperiment.query.get_or_404(exp_id)
    variants = GrowthVariant.query.filter_by(experiment_id=exp_id).order_by(GrowthVariant.id).all()
    participants = GrowthParticipant.query.filter_by(experiment_id=exp_id).all()

    # Build per-variant date → {deltas, profile_visits}
    variant_data = {v.id: {} for v in variants}
    for p in participants:
        dps = GrowthDataPoint.query.filter_by(participant_id=p.id).order_by(GrowthDataPoint.recorded_at).all()
        for dp in dps:
            ds = str(dp.recorded_at)
            if ds not in variant_data[p.variant_id]:
                variant_data[p.variant_id][ds] = {'deltas': [], 'pv': []}
            delta = (dp.followers or 0) - (p.start_followers or 0)
            variant_data[p.variant_id][ds]['deltas'].append(delta)
            if dp.profile_visits:
                variant_data[p.variant_id][ds]['pv'].append(dp.profile_visits)

    # Always include start date at zero
    if exp.start_date:
        sd = str(exp.start_date)
        for vid in variant_data:
            if sd not in variant_data[vid]:
                variant_data[vid][sd] = {'deltas': [0], 'pv': []}

    all_dates = sorted({d for v in variant_data.values() for d in v.keys()})

    follower_datasets    = []
    conversion_datasets  = []
    for v in variants:
        vd = variant_data[v.id]
        f_pts = []
        c_pts = []
        for ds in all_dates:
            if ds in vd and vd[ds]['deltas']:
                avg_d = sum(vd[ds]['deltas']) / len(vd[ds]['deltas'])
                f_pts.append(round(avg_d, 1))
                total_pv = sum(vd[ds]['pv'])
                total_d  = sum(vd[ds]['deltas'])
                c_pts.append(round(total_d / total_pv * 100, 2) if total_pv > 0 else None)
            else:
                f_pts.append(None)
                c_pts.append(None)
        follower_datasets.append({'label': v.name, 'data': f_pts,
                                  'borderColor': v.color, 'backgroundColor': v.color + '33',
                                  'tension': 0.3, 'fill': False})
        conversion_datasets.append({'label': v.name, 'data': c_pts,
                                    'borderColor': v.color, 'backgroundColor': v.color + '33',
                                    'tension': 0.3, 'fill': False})
    return jsonify({'labels': all_dates,
                    'follower_datasets': follower_datasets,
                    'conversion_datasets': conversion_datasets})


@app.route('/api/growth-lab/<int:exp_id>/analyze', methods=['POST'])
@login_required
def api_growth_analyze(exp_id):
    exp = GrowthExperiment.query.get_or_404(exp_id)
    api_key = os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key hinterlegt'}), 400
    import json as _json
    from zoneinfo import ZoneInfo
    berlin = ZoneInfo('Europe/Berlin')
    today  = datetime.now(berlin).date()
    variant_stats = _compute_variant_stats(exp, today)
    prompt = f"""Du analysierst ein Instagram-Wachstumsexperiment.

Kategorie: {exp.category.name}
Experiment: {exp.name}
Ziel: {exp.goal or '—'}
Dauer: {exp.duration_days} Tage
Beschreibung: {exp.description or '—'}

Varianten-Ergebnisse (sortiert nach Ø Follower-Wachstum pro Account):
{_json.dumps(variant_stats, ensure_ascii=False, indent=2)}

Erstelle eine datenbasierte Analyse auf Deutsch. Antworte NUR mit validem JSON:
{{
  "winner": "Varianten-Name oder null wenn keine Daten",
  "winner_reason": "Warum hat diese Variante besser funktioniert? Konkret, 2-3 Sätze.",
  "insights": ["Erkenntnis 1", "Erkenntnis 2", "Erkenntnis 3"],
  "recommendation": "Welche Strategie sollte auf weitere {exp.category.name}-Seiten ausgerollt werden? 1-2 Sätze.",
  "ratings": {{"Varianten-Name": "success|neutral|weak"}},
  "summary": "Kurze Gesamtzusammenfassung in 2 Sätzen."
}}
Ratings: success=deutlich besser, neutral=ähnlich, weak=schlechter als Durchschnitt. Ohne Kontrollgruppe: relativ zueinander."""
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=900,
            messages=[{'role': 'user', 'content': prompt}]
        )
        _log_ai('growth_analyse', msg)
        result = _json.loads(msg.content[0].text)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    exp.ai_analysis_json = _json.dumps(result, ensure_ascii=False)
    exp.status = 'completed'
    winner_name = result.get('winner')
    if winner_name:
        wv = next((v for v in exp.variants if v.name == winner_name), None)
        if wv:
            exp.winner_variant_id = wv.id
    for insight in result.get('insights', []):
        if insight.strip():
            db.session.add(GrowthKnowledge(category_id=exp.category_id,
                                           insight=insight.strip(), experiment_id=exp_id))
    db.session.commit()
    return jsonify({'ok': True, 'analysis': result})


@app.route('/api/growth-lab/ideas/<int:cat_id>', methods=['POST'])
@login_required
def api_growth_ideas(cat_id):
    cat = Category.query.get_or_404(cat_id)
    api_key = os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key'}), 400
    import json as _json
    knowledge = GrowthKnowledge.query.filter_by(category_id=cat_id).order_by(GrowthKnowledge.created_at.desc()).limit(10).all()
    past_exps = GrowthExperiment.query.filter_by(category_id=cat_id).order_by(GrowthExperiment.created_at.desc()).limit(5).all()
    knowledge_txt = '\n'.join(f'- {k.insight}' for k in knowledge) or '(noch keine Erkenntnisse)'
    past_txt      = '\n'.join(f'- {e.name} ({e.status})' for e in past_exps) or '(noch keine Experimente)'
    prompt = f"""Du bist ein Instagram-Wachstumsexperte. Generiere 5 konkrete Experiment-Ideen für Instagram-Seiten der Kategorie "{cat.name}".

Bisherige Erkenntnisse:
{knowledge_txt}

Bisherige Experimente:
{past_txt}

Antworte NUR mit validem JSON:
{{"ideas": [{{"name": "Experiment-Name", "goal": "Was wird getestet?", "variants": ["Variante A: ...", "Variante B: ...", "Kontrollgruppe: aktueller Stand"], "rationale": "Warum sinnvoll? (1 Satz)"}}]}}
Testbare Variablen: Bio-Text, CTA in Bio, Posting-Frequenz, Caption-Stil, Hashtag-Strategie, Profilbild-Stil."""
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=1200,
            messages=[{'role': 'user', 'content': prompt}]
        )
        _log_ai('growth_ideas', msg)
        result = _json.loads(msg.content[0].text)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    return jsonify({'ok': True, 'ideas': result.get('ideas', []), 'category': cat.name})


@app.route('/api/growth-lab/knowledge', methods=['POST'])
@login_required
def api_growth_knowledge_add():
    d = request.get_json()
    if not d or not d.get('category_id') or not d.get('insight'):
        return jsonify({'ok': False, 'error': 'category_id und insight erforderlich'}), 400
    k = GrowthKnowledge(category_id=int(d['category_id']),
                        insight=d['insight'].strip())
    db.session.add(k)
    db.session.commit()
    return jsonify({'ok': True, 'id': k.id})


@app.route('/api/growth-lab/knowledge/<int:k_id>/delete', methods=['POST'])
@login_required
def api_growth_knowledge_delete(k_id):
    k = GrowthKnowledge.query.get_or_404(k_id)
    db.session.delete(k)
    db.session.commit()
    return jsonify({'ok': True})


# ═══════════════════════════════════════════════════════════════
# INSTAGRAM INTELLIGENCE CENTER — Wissens-DB + KI-Knowledge-Engine
# ═══════════════════════════════════════════════════════════════

IIC_CATEGORIES = [
    'Algorithm Updates', 'Feed Ranking', 'Reels Ranking', 'Story Ranking',
    'Explore Ranking', 'SEO', 'Community Guidelines', 'Recommendation Guidelines',
    'Spam Detection', 'Shadowban & Einschränkungen', 'Monetarisierung', 'Urheberrecht',
    'AI-Content', 'Hashtags', 'Posting-Frequenz', 'Engagement-Signale',
    'Negative Ranking-Signale', 'Creator-Best-Practices', 'Mythen & Missverständnisse',
    'Eigene Experimente', 'Wachstums-Hacks (Indizien)',
]
IIC_STATUS = ['bestätigt', 'wahrscheinlich', 'unklar', 'widerlegt']

IIC_SYSTEM = (
    "Du bist der Research- und Wissensmotor des internen Dashboards Instagram "
    "Intelligence Center. Ziel: die genaueste, ehrlichste, datenbasierte Wissensdatenbank "
    "über den Instagram-Algorithmus aufbauen.\n\n"
    "REGELN (zwingend):\n"
    "- Stelle NIEMALS Vermutungen als Fakten dar. Kennzeichne Unsicherheit.\n"
    "- Vertrauensscore: 100=offiziell bestätigt (Instagram/Meta/Mosseri direkt), "
    "80=mehrere vertrauenswürdige Quellen, 50=starke Indizien, 20=unbestätigte Theorie.\n"
    "- Status: 'bestätigt' (offizielle Quelle), 'wahrscheinlich' (gute Indizien), "
    "'unklar' (widersprüchlich/dünn), 'widerlegt' (nachweislich falsch).\n"
    "- Nenne Quelle + Link wenn vorhanden. Erfinde KEINE Quellen oder URLs.\n"
    "- AKTUALITÄT IST PFLICHT: Nenne immer das Quelldatum (source_date, falls bekannt). "
    "Bevorzuge aktuelle Quellen. Wenn eine Information möglicherweise veraltet ist oder du nicht "
    "sicher bestätigen kannst, dass sie HEUTE noch gilt, senke den Vertrauensscore deutlich und "
    "setze status auf 'unklar' mit Hinweis im Text. Eine ältere Aussage nur dann als 'bestätigt' "
    "führen, wenn sie nachweislich weiterhin aktuell ist.\n"
    "- Nicht-offizielle, aber plausible Tipps/Hacks (logisch aus bestätigten Mechaniken "
    "abgeleitet): Kategorie 'Wachstums-Hacks (Indizien)', Vertrauensscore HÖCHSTENS 55, "
    "status 'wahrscheinlich' oder 'unklar', und klar als Hypothese/Indiz kennzeichnen.\n"
    "- Kategorien NUR aus: " + ', '.join(IIC_CATEGORIES) + ".\n"
    "- Leite konkrete praktische Auswirkungen fürs Seiten-Wachstum ab.\n\n"
    "Antworte AUSSCHLIESSLICH mit einem JSON-Array (kein weiterer Text):\n"
    '[{"title":"kurz & präzise","category":"<eine Kategorie>","source_name":"...",'
    '"source_url":"... oder null","source_date":"YYYY-MM-DD oder null","summary":"2-4 Sätze",'
    '"key_points":["...","..."],"practical_impact":"Was heißt das konkret fürs Posten?",'
    '"confidence":0-100,"status":"bestätigt|wahrscheinlich|unklar|widerlegt"}]'
)


def _iic_claude(api_key, user_content, tools=None, model=None, max_tokens=4000, feature='iic'):
    """Claude-Aufruf für die Knowledge-Engine; behandelt server-tool pause_turn."""
    import anthropic as _ant
    client = _ant.Anthropic(api_key=api_key)
    model = model or (get_setting('analysis_model') or 'claude-sonnet-4-6')
    messages = [{'role': 'user', 'content': user_content}]
    resp = None
    for _ in range(5):
        kwargs = dict(model=model, max_tokens=max_tokens, system=IIC_SYSTEM, messages=messages)
        if tools:
            kwargs['tools'] = tools
        resp = client.messages.create(**kwargs)
        _log_ai(feature, resp)
        if resp.stop_reason == 'pause_turn':
            messages.append({'role': 'assistant', 'content': resp.content})
            continue
        break
    return ''.join(b.text for b in resp.content if getattr(b, 'type', '') == 'text')


def _iic_parse_and_save(text):
    """Extrahiert das JSON-Array und legt KnowledgeEntries an. Gibt die Liste zurück.
    Nutzt _extract_balanced_json statt einer gierigen Regex — die Recherche-/
    URL-Modi laufen mit web_search/web_fetch-Tools, deren mehrteilige Antworten
    (Zitier-Klammern wie "[1]" etc.) eine gierige \\[[\\s\\S]*\\]-Regex genau wie
    beim Trend-Radar-Bug zerreißen können (s. project_trend_radar-Memory)."""
    try:
        items = json.loads(_extract_balanced_json(text or '', '[', ']') or '[]')
    except Exception as e:
        app.logger.error('IIC JSON-Parse fehlgeschlagen: %s', e)
        items = []
    created = []
    for it in items:
        if not isinstance(it, dict) or not it.get('title') or not it.get('summary'):
            continue
        cat = it.get('category') if it.get('category') in IIC_CATEGORIES else 'Algorithm Updates'
        st  = it.get('status') if it.get('status') in IIC_STATUS else 'unklar'
        sd = None
        if it.get('source_date'):
            try:
                from datetime import date as _date
                sd = _date.fromisoformat(str(it['source_date'])[:10])
            except Exception:
                sd = None
        try:
            conf = max(0, min(100, int(it.get('confidence', 50))))
        except Exception:
            conf = 50
        e = KnowledgeEntry(
            title=str(it['title'])[:300], category=cat,
            source_name=(str(it.get('source_name') or '')[:200] or None),
            source_url=(it.get('source_url') or None),
            source_date=sd, summary=it.get('summary'),
            key_points=json.dumps(it.get('key_points', []), ensure_ascii=False),
            practical_impact=it.get('practical_impact'),
            confidence=conf, status=st, last_verified=now_berlin().date())
        db.session.add(e)
        created.append(e)
    db.session.commit()
    return created


def _iic_entry_dict(e):
    import json as _json
    from datetime import date as _d
    try:
        kp = _json.loads(e.key_points) if e.key_points else []
    except Exception:
        kp = []
    # Aktualität: stale = seit >180 Tagen nicht mehr als „heute gültig" bestätigt
    # (oder nie geprüft). source_year zeigt zusätzlich, wie alt die Ur-Quelle ist.
    lv = getattr(e, 'last_verified', None)
    stale = True if not lv else (_d.today() - lv).days > 180
    return {
        'id': e.id, 'title': e.title, 'category': e.category,
        'source_name': e.source_name, 'source_url': e.source_url,
        'source_date': e.source_date.isoformat() if e.source_date else None,
        'source_year': e.source_date.year if e.source_date else None,
        'last_verified': lv.strftime('%d.%m.%Y') if lv else None,
        'stale': stale,
        'summary': e.summary, 'key_points': kp, 'practical_impact': e.practical_impact,
        'confidence': e.confidence, 'status': e.status, 'pinned': e.pinned,
        'created_at': e.created_at.strftime('%d.%m.%Y') if e.created_at else None,
    }


@app.route('/intelligence')
@login_required
def intelligence_center():
    from collections import Counter
    entries = KnowledgeEntry.query.order_by(
        KnowledgeEntry.pinned.desc(), KnowledgeEntry.created_at.desc()).all()
    cat_counts = Counter(e.category for e in entries)
    return render_template('intelligence.html',
        entries=[_iic_entry_dict(e) for e in entries],
        categories=IIC_CATEGORIES, statuses=IIC_STATUS, cat_counts=dict(cat_counts),
        cheatsheet=get_setting('iic_cheatsheet', ''),
        cheatsheet_updated=get_setting('iic_cheatsheet_updated', ''),
        setup_guide=get_setting('iic_setup_guide', ''),
        setup_updated=get_setting('iic_setup_updated', ''),
        setup_bm=get_setting('iic_setup_bm', ''),
        setup_plan=get_setting('iic_setup_plan', ''),
        protection_guide=get_setting('iic_protection_guide', ''),
        protection_updated=get_setting('iic_protection_updated', ''),
        ai_ready=bool(os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')),
        active_page='intelligence')


@app.route('/api/intelligence/ingest', methods=['POST'])
@login_required
def iic_ingest():
    d = request.get_json() or {}
    mode = d.get('mode', 'text')
    api_key = os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key konfiguriert'}), 400
    hint = (d.get('category') or '').strip()
    cat_hint = f"\nKategorie-Schwerpunkt (falls passend): {hint}." if hint else ''
    try:
        if mode == 'url':
            url = (d.get('url') or '').strip()
            if not url:
                return jsonify({'ok': False, 'error': 'Keine URL angegeben'}), 400
            uc = (f"Hole den Inhalt dieser Quelle und extrahiere alle relevanten "
                  f"Instagram-Algorithmus-Erkenntnisse als Wissens-Einträge:\n{url}{cat_hint}")
            text = _iic_claude(api_key, uc, tools=[{'type': 'web_fetch_20260209', 'name': 'web_fetch'}],
                               feature='iic_url', max_tokens=5000)
        elif mode == 'research':
            topic = (d.get('topic') or '').strip()
            if not topic:
                return jsonify({'ok': False, 'error': 'Kein Thema angegeben'}), 400
            uc = (f"Recherchiere im Web (bevorzugt offizielle Quellen: Instagram-/Meta-Blog, "
                  f"Help-Center, Adam Mosseri, Recommendation Guidelines) zu folgendem Thema und "
                  f"lege fundierte Wissens-Einträge an. Thema: {topic}{cat_hint}\n"
                  f"Nutze mehrere Suchen falls nötig. Offizielle Quellen → hoher Score.")
            text = _iic_claude(api_key, uc, tools=[{'type': 'web_search_20260209', 'name': 'web_search'}],
                               feature='iic_research', max_tokens=6000)
        else:
            raw = (d.get('text') or '').strip()
            if not raw:
                return jsonify({'ok': False, 'error': 'Kein Text angegeben'}), 400
            src = (d.get('source_name') or '').strip()
            uc = (f"Analysiere diesen Quelltext (z.B. Mosseri-Transkript oder Artikel) und "
                  f"extrahiere die Instagram-Erkenntnisse als Wissens-Einträge."
                  f"{(' Quelle: ' + src + '.') if src else ''}{cat_hint}\n\n"
                  f"--- QUELLTEXT ---\n{raw[:20000]}")
            text = _iic_claude(api_key, uc, feature='iic_text')
        created = _iic_parse_and_save(text)
        if mode == 'text' and created:
            raw = (d.get('text') or '').strip()
            src = (d.get('source_name') or '').strip()
            for e in created:
                e.raw_content = raw[:20000]
                if src and not e.source_name:
                    e.source_name = src[:200]
            db.session.commit()
        return jsonify({'ok': True, 'count': len(created),
                        'entries': [_iic_entry_dict(e) for e in created]})
    except Exception as ex:
        app.logger.error('iic_ingest Fehler: %s', ex)
        return jsonify({'ok': False, 'error': str(ex)}), 500


@app.route('/api/intelligence/entry/<int:eid>', methods=['PUT', 'DELETE'])
@login_required
def iic_entry(eid):
    e = KnowledgeEntry.query.get_or_404(eid)
    if request.method == 'DELETE':
        db.session.delete(e)
        db.session.commit()
        return jsonify({'ok': True})
    d = request.get_json() or {}
    if 'pinned' in d:
        e.pinned = bool(d['pinned'])
    if d.get('status') in IIC_STATUS:
        e.status = d['status']
    if 'confidence' in d:
        try:
            e.confidence = max(0, min(100, int(d['confidence'])))
        except Exception:
            pass
    if d.get('category') in IIC_CATEGORIES:
        e.category = d['category']
    for f in ('title', 'summary', 'practical_impact', 'source_name', 'source_url'):
        if f in d:
            setattr(e, f, (d[f] or None))
    db.session.commit()
    return jsonify({'ok': True, 'entry': _iic_entry_dict(e)})


@app.route('/api/intelligence/cheatsheet/regenerate', methods=['POST'])
@login_required
def iic_cheatsheet_regen():
    api_key = os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key'}), 400
    entries = KnowledgeEntry.query.order_by(KnowledgeEntry.confidence.desc()).limit(150).all()
    if not entries:
        return jsonify({'ok': False, 'error': 'Noch keine Wissens-Einträge vorhanden'}), 400
    facts = '\n'.join(
        f"- [{e.category} · {e.status} · {e.confidence}%] {e.title}: {e.summary}" for e in entries)
    uc = (
        "Erstelle aus den folgenden Wissens-Einträgen ein kompaktes, aktuelles Cheat Sheet "
        "Was funktioniert gerade auf Instagram? in Markdown. Struktur exakt:\n"
        "## Ranking-Faktoren nach Priorität\n## Wichtigste Do's\n## Wichtigste Don'ts\n"
        "## Bekannte Änderungen\n## Warnungen\n## Offene Fragen\n\n"
        "Markiere JEDE Aussage am Ende mit (✅ bestätigt / 🟡 wahrscheinlich / ❓ unklar / "
        "❌ widerlegt). Formuliere Wahrscheinlichkeiten statt Absolutaussagen. Nur aus den "
        "Einträgen ableiten, nichts erfinden.\n\n--- WISSENS-EINTRÄGE ---\n" + facts[:18000])
    try:
        import anthropic as _ant
        client = _ant.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=(get_setting('analysis_model') or 'claude-sonnet-4-6'),
            max_tokens=3000, messages=[{'role': 'user', 'content': uc}])
        _log_ai('iic_cheatsheet', resp)
        md = ''.join(b.text for b in resp.content if getattr(b, 'type', '') == 'text').strip()
    except Exception as ex:
        return jsonify({'ok': False, 'error': str(ex)}), 500
    set_setting('iic_cheatsheet', md)
    set_setting('iic_cheatsheet_updated', now_berlin().strftime('%d.%m.%Y %H:%M'))
    db.session.commit()
    return jsonify({'ok': True, 'cheatsheet': md, 'updated': get_setting('iic_cheatsheet_updated')})


# ═══════════════════════════════════════════════════════════════
# ─────────────────── SYSTEMSTATUS ───────────────────────────────
# ═══════════════════════════════════════════════════════════════
def _ago(dt_utc):
    """'vor X Min/Std/Tagen' aus einem UTC-datetime (naiv)."""
    if not dt_utc:
        return 'nie'
    s = (datetime.utcnow() - dt_utc).total_seconds()
    if s < 0:
        return 'gerade eben'
    if s < 90:
        return 'gerade eben'
    if s < 5400:
        return f'vor {int(s // 60)} Min'
    if s < 172800:
        return f'vor {int(s // 3600)} Std'
    return f'vor {int(s // 86400)} Tagen'


def _system_status():
    """Live-Status aller Subsysteme — eine Ampel pro System. Erkennt stillen
    Stillstand (Sync/Webhook/Posting/Scheduler), bevor er alle Accounts trifft."""
    nowb = now_berlin()
    utcnow = datetime.utcnow()
    items = []

    # 1) Follower-Sync
    sv = get_setting('last_follower_sync_at')
    sdt = None
    if sv:
        try:
            sdt = datetime.fromisoformat(sv)
        except Exception:
            sdt = None
    if sdt:
        age_h = (utcnow - sdt).total_seconds() / 3600
        st = 'green' if age_h < 36 else ('yellow' if age_h < 72 else 'red')
    else:
        st = 'red'
    res = _ig_sync_status.get('result') or {}
    if _ig_sync_status.get('running'):
        detail = 'läuft gerade …'
    elif res:
        detail = f"{res.get('updated', '?')}/{res.get('total_queried', '?')} Accounts aktualisiert"
    else:
        detail = 'kein Lauf in dieser Sitzung erfasst'
    items.append({'key': 'sync', 'title': 'Follower-Sync', 'icon': 'fa-rotate', 'status': st,
                  'label': _ago(sdt) if sdt else 'noch nie gelaufen', 'detail': detail,
                  'action': {'label': 'Jetzt syncen', 'endpoint': '/api/analytics/sync-followers-apify'}})

    # 2) Telegram-Webhook
    token = get_setting('telegram_bot_token')
    if not token:
        items.append({'key': 'webhook', 'title': 'Telegram-Webhook', 'icon': 'fa-link', 'status': 'off',
                      'label': 'nicht konfiguriert', 'detail': 'Kein Bot-Token gesetzt'})
    else:
        try:
            import requests as _r
            wi = _r.get(f'https://api.telegram.org/bot{token}/getWebhookInfo', timeout=6).json().get('result', {})
            url = wi.get('url') or ''
            pending = wi.get('pending_update_count', 0) or 0
            lerr = wi.get('last_error_message')
            lerr_date = wi.get('last_error_date', 0) or 0
            recent_err = bool(lerr and (utcnow.timestamp() - lerr_date < 3600))
            if not url:
                st, lab, det = 'red', 'nicht registriert', 'Kein Webhook gesetzt'
            elif recent_err:
                st, lab, det = 'red', 'Fehler', str(lerr)[:90]
            elif pending > 20:
                st, lab, det = 'yellow', 'Rückstau', f'{pending} ausstehende Updates'
            else:
                det = f'{pending} ausstehend' + (f' · letzter Fehler: {lerr}' if lerr else ' · keine Fehler')
                st, lab = 'green', 'aktiv'
            items.append({'key': 'webhook', 'title': 'Telegram-Webhook', 'icon': 'fa-link', 'status': st,
                          'label': lab, 'detail': det,
                          'action': {'label': 'Neu registrieren', 'endpoint': '/api/telegram/register-webhook'}})
        except Exception as e:
            items.append({'key': 'webhook', 'title': 'Telegram-Webhook', 'icon': 'fa-link', 'status': 'unklar',
                          'label': 'nicht erreichbar', 'detail': str(e)[:90]})

    # 3) Posting-Engine — überfällige, SENDBARE Posts (Channel vorhanden) zählen.
    base = ScheduledPost.query.join(Account, ScheduledPost.account_id == Account.id).filter(
        ScheduledPost.scheduled_at <= nowb, ScheduledPost.status == 'scheduled',
        ScheduledPost.slot_type != 'disabled', ScheduledPost.telegram_sent_at == None,
        Account.posting_enabled.isnot(False))
    due = base.filter(Account.telegram_chat_id.isnot(None),
                      Account.telegram_chat_id != '',
                      Account.telegram_chat_id != 'None').all()  # 'None'-String = kein echter Channel
    nochan = max(0, base.count() - len(due))
    sent24 = ScheduledPost.query.filter(ScheduledPost.telegram_sent_at >= utcnow - timedelta(hours=24)).count()
    if due:
        oldest = min(p.scheduled_at for p in due)
        overdue_min = (nowb - oldest).total_seconds() / 60
        st = 'red' if overdue_min > 10 else 'yellow'
        lab = f'{len(due)} überfällig'
        det = f'ältester seit {int(overdue_min)} Min · {sent24} in 24 h gesendet'
    else:
        st, lab, det = 'green', 'aktuell', f'{sent24} Posts in 24 h gesendet'
    if nochan:
        det += f' · {nochan} warten auf Channel-Einrichtung'
    posting = {'key': 'posting', 'title': 'Posting-Engine', 'icon': 'fa-paper-plane',
               'status': st, 'label': lab, 'detail': det}
    if due or nochan:
        posting['link'] = {'label': 'Hängende Posts ansehen', 'url': '/haengende-posts'}
    items.append(posting)

    # 4) Scheduler-Herzschlag
    lt = _sched_health.get('last_tick')
    if not lt:
        st, lab = 'unklar', 'noch kein Tick'
    else:
        age = (utcnow - lt).total_seconds()
        st = 'green' if age < 180 else 'red'
        lab = _ago(lt)
    items.append({'key': 'scheduler', 'title': 'Hintergrund-Scheduler', 'icon': 'fa-heart-pulse',
                  'status': st, 'label': lab, 'detail': 'Sendet Posts, Sync, Alerts (Tick ~60 s)'})

    # 5) KI-Budget (nur wenn gesetzt)
    try:
        budget = float(get_setting('ai_budget_eur') or 0)
    except Exception:
        budget = 0
    if budget > 0:
        ms = nowb.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        spend = db.session.query(func.coalesce(func.sum(AiUsageLog.cost_eur), 0.0)) \
            .filter(AiUsageLog.created_at >= ms).scalar() or 0
        ratio = spend / budget
        st = 'green' if ratio < 0.8 else ('yellow' if ratio <= 1 else 'red')
        items.append({'key': 'ai', 'title': 'KI-Budget', 'icon': 'fa-wand-magic-sparkles', 'status': st,
                      'label': f'{spend:.2f} / {budget:.0f} €', 'detail': f'{ratio * 100:.0f} % des Monatsbudgets'})

    # 6) Letztes Backup
    bv = get_setting('last_full_backup_at')
    bdt = None
    if bv:
        try:
            bdt = datetime.fromisoformat(bv)
        except Exception:
            bdt = None
    if bdt:
        age_d = (utcnow - bdt).days
        st = 'green' if age_d < 14 else ('yellow' if age_d < 45 else 'red')
        items.append({'key': 'backup', 'title': 'Letztes Backup', 'icon': 'fa-download', 'status': st,
                      'label': _ago(bdt), 'detail': 'Voll-Export (Accounts, Config)'})
    else:
        items.append({'key': 'backup', 'title': 'Letztes Backup', 'icon': 'fa-download', 'status': 'yellow',
                      'label': 'noch keins', 'detail': 'Noch kein Voll-Export erstellt'})

    rank = {'red': 3, 'yellow': 2, 'unklar': 1, 'green': 0, 'off': 0}
    overall = max(items, key=lambda i: rank.get(i['status'], 0))['status'] if items else 'green'
    if overall not in ('red', 'yellow'):
        overall = 'green'
    return {'overall': overall, 'items': items, 'checked_at': nowb.strftime('%H:%M:%S')}


@app.route('/api/status')
@login_required
def api_system_status():
    return jsonify(_system_status())


@app.route('/status')
@login_required
def system_status_page():
    return render_template('status.html', active_page='status')


@app.route('/citybot-team', methods=['GET', 'POST'])
@login_required
def citybot_team():
    """Holt die Team-/Aktivitätsübersicht aus dem CityBot (News-Bot), damit die
    Mitarbeiter-Kontrolle für ALLE Systeme an einer Stelle liegt. Nur Kennzahlen —
    die Inhalte der beiden Systeme bleiben getrennt.
    Konfiguration (CityBot-URL + API-Key) direkt auf dieser Seite."""
    if request.method == 'POST':
        set_setting('citybot_base_url', (request.form.get('base_url') or '').strip().rstrip('/'))
        set_setting('citybot_api_key', (request.form.get('api_key') or '').strip())
        flash('CityBot-Verbindung gespeichert.', 'success')
        return redirect(url_for('citybot_team'))

    base_url = get_setting('citybot_base_url', '') or ''
    api_key  = get_setting('citybot_api_key', '') or ''
    days     = request.args.get('days', '7')
    try:
        days = max(1, min(90, int(days)))
    except (ValueError, TypeError):
        days = 7

    data, error = None, None
    if base_url and api_key:
        try:
            resp = _requests.get(
                f'{base_url}/api/external/team-activity',
                headers={'X-CityBot-Key': api_key},
                params={'days': days}, timeout=12,
            )
            if resp.status_code == 403:
                error = 'API-Key wird vom CityBot abgelehnt (403). Key prüfen.'
            elif resp.status_code == 503:
                error = 'Im CityBot ist noch kein API-Key gesetzt (Team-Tab → Key generieren).'
            elif not resp.ok:
                error = f'CityBot antwortete mit HTTP {resp.status_code}.'
            else:
                data = resp.json()
        except Exception as e:
            error = f'CityBot nicht erreichbar: {e}'

    return render_template('citybot_team.html', active_page='citybot_team',
                           data=data, error=error, base_url=base_url,
                           api_key=api_key, days=days)


# ─────────────────── HÄNGENDE POSTS ─────────────────────────────
def _stuck_posts_data():
    """Überfällige, nicht gesendete Posts (status=scheduled, fällig, Posting an).
    Gruppiert pro Account mit Grund (kein echter Channel vs. Senden fehlgeschlagen)."""
    nowb = now_berlin()
    posts = ScheduledPost.query.join(Account, ScheduledPost.account_id == Account.id).filter(
        ScheduledPost.scheduled_at <= nowb, ScheduledPost.status == 'scheduled',
        ScheduledPost.telegram_sent_at == None, ScheduledPost.slot_type != 'disabled',
        Account.posting_enabled.isnot(False)
    ).order_by(ScheduledPost.scheduled_at).all()
    groups = {}
    for p in posts:
        a = p.account
        if a.id not in groups:
            ch = (a.telegram_chat_id or '').strip()
            groups[a.id] = {'account': a, 'channel': ch,
                            'has_channel': bool(ch and ch not in ('None', 'null')),
                            'posts': []}
        groups[a.id]['posts'].append({
            'id': p.id, 'when': p.scheduled_at.strftime('%d.%m.%Y %H:%M'),
            'type': p.post_type, 'days_over': max(0, (nowb - p.scheduled_at).days),
            'caption': (p.caption or '').strip()[:70],
        })
    # Kein-Channel zuerst (häufigste Ursache), dann nach Anzahl
    return sorted(groups.values(), key=lambda g: (g['has_channel'], -len(g['posts']))), len(posts)


@app.route('/haengende-posts')
@login_required
def stuck_posts_page():
    groups, total = _stuck_posts_data()
    return render_template('haengende_posts.html', groups=groups, total=total, active_page='status')


@app.route('/api/scheduled-post/<int:pid>/cancel', methods=['POST'])
@login_required
def stuck_post_cancel(pid):
    p = ScheduledPost.query.get_or_404(pid)
    p.status = 'cancelled'
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/scheduled-post/<int:pid>/retry', methods=['POST'])
@login_required
def stuck_post_retry(pid):
    p = ScheduledPost.query.get_or_404(pid)
    try:
        ok = send_telegram_post(p)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:120]})
    if ok:
        p.telegram_sent_at = datetime.utcnow()
        db.session.commit()
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'Senden fehlgeschlagen — Channel-ID und Bot-Rechte (Admin im Channel?) prüfen.'})


@app.route('/api/account/<int:account_id>/cancel-stuck', methods=['POST'])
@login_required
def account_cancel_stuck(account_id):
    nowb = now_berlin()
    rows = ScheduledPost.query.filter(
        ScheduledPost.account_id == account_id, ScheduledPost.scheduled_at <= nowb,
        ScheduledPost.status == 'scheduled', ScheduledPost.telegram_sent_at == None,
        ScheduledPost.slot_type != 'disabled').all()
    for p in rows:
        p.status = 'cancelled'
    db.session.commit()
    return jsonify({'ok': True, 'cancelled': len(rows)})


# ═══════════════════════════════════════════════════════════════
# ─────────── MISSING CHILDREN FACTORY (Content Studio) ──────────
# Eigenständiger Workflow: Vermisstenmeldungen → seriöses IG-Poster.
# LEITPLANKE: Es wird NIE eine Angabe erfunden. Fehlende Felder bleiben weg.
# ═══════════════════════════════════════════════════════════════
_MCF_FONT_DIR = os.path.join(os.path.dirname(__file__), 'fonts')

def _mcf_font(size, bold=False):
    from PIL import ImageFont
    name = 'LiberationSans-Bold.ttf' if bold else 'LiberationSans-Regular.ttf'
    for p in (os.path.join(_MCF_FONT_DIR, name),
              '/usr/share/fonts/truetype/liberation/' + name,
              '/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf' % ('-Bold' if bold else '')):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    from PIL import ImageFont as _IF
    return _IF.load_default()


def _mcf_load_image_bytes(media):
    """Bytes eines MediaItem laden (Cloudinary-URL oder lokal). None bei Fehler."""
    if not media:
        return None
    try:
        u = media.url or ''
        if u.startswith('http'):
            import requests as _r
            resp = _r.get(u, timeout=8)
            if resp.ok:
                return resp.content
        fp = os.path.join(app.config['UPLOAD_FOLDER'], media.filename)
        if os.path.exists(fp):
            return open(fp, 'rb').read()
        if u.startswith('/'):
            fp2 = os.path.join(app.root_path, u.lstrip('/'))
            if os.path.exists(fp2):
                return open(fp2, 'rb').read()
    except Exception as e:
        app.logger.warning('MCF Bild-Load: %s', e)
    return None


def _mcf_wrap(draw, text, font, max_w):
    """Text in Zeilen umbrechen, die in max_w passen."""
    lines, cur = [], ''
    for w in str(text).split():
        test = (cur + ' ' + w).strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or ['']


_MCF_ZEIT_LABEL = {
    'morgens': 'morgens', 'vormittags': 'vormittags', 'mittags': 'mittags',
    'nachmittags': 'nachmittags', 'abends': 'abends', 'nachts': 'nachts',
}


def _mcf_seit_label(case):
    """Kombiniertes 'Vermisst seit'-Label aus Datum + optionaler Tageszeit.
    Erfindet nichts — nur was gesetzt ist."""
    zeit = _MCF_ZEIT_LABEL.get((case.vermisst_zeit or '').strip().lower())
    if case.vermisst_seit:
        try:
            d = case.vermisst_seit.strftime('%d.%m.%Y')
        except Exception:
            d = None
        if d:
            return f'{d}, {zeit}' if zeit else d
    return zeit


def _mcf_draw_photo_placeholder(bw, bh):
    """Baut ein Platzhalter-Bild (helles Feld + Silhouette-Icon + Hinweistext) in
    der Größe (bw, bh), wenn für einen Fall (noch) kein Foto vorliegt — damit das
    Poster nie eine leere Fläche zeigt, sondern klar erkennbar 'kein Foto'.
    Gibt ein RGB-Image zurück (wird vom Aufrufer mit abgerundeter Maske eingesetzt)."""
    from PIL import Image as _Image, ImageDraw as _ImageDraw
    bw, bh = int(bw), int(bh)
    BG, ICON = (238, 240, 244), (196, 201, 212)
    box_img = _Image.new('RGB', (bw, bh), BG)
    d = _ImageDraw.Draw(box_img)
    cx, cy = bw / 2, bh / 2
    scale = min(max(min(bw, bh) / 300.0, 0.5), 1.5)
    head_r = 42 * scale
    head_cy = cy - 50 * scale
    d.ellipse([cx - head_r, head_cy - head_r, cx + head_r, head_cy + head_r], fill=ICON)
    body_w, body_h = 168 * scale, 110 * scale
    body_top = head_cy + head_r * 0.55
    d.rounded_rectangle([cx - body_w / 2, body_top, cx + body_w / 2, body_top + body_h],
                         radius=body_h * 0.5, fill=ICON)
    d.rectangle([0, body_top + body_h * 0.55, bw, bh], fill=BG)  # Schulter-Überstand kappen
    f_ph = _mcf_font(22, bold=False)
    txt = 'Kein Foto verfügbar'
    tw = d.textlength(txt, font=f_ph)
    ty = min(body_top + body_h + 18 * scale, bh - 32)
    d.text((cx - tw / 2, ty), txt, font=f_ph, fill=(150, 156, 168))
    return box_img


def _render_missing_child_image(case, contact_line=None):
    """Adaptives 1080×1350-Vermisstenposter im klassischen 'Abreißzettel'-Stil
    (Aushang am Schwarzen Brett): dicker schwarzer Rahmen, große Blocktypografie,
    quadratisches Foto mit schlichtem schwarzen Rand (kein Verlauf/Schatten/Karten-
    Optik), Fakten als zentrierte Text-Zeilen, abreißbare Kontakt-Streifen am
    unteren Rand. Rendert NUR vorhandene Felder (keine Lücken, funktioniert auch
    mit minimalen Angaben). Speichert flach im Upload-Ordner, gibt Dateinamen zurück."""
    from PIL import Image, ImageDraw, ImageOps
    import io as _io
    W, H = 1080, 1350
    WHITE = (255, 255, 255); BLACK = (18, 18, 18); RED = (176, 24, 24); GRAY = (110, 110, 110)
    img = Image.new('RGB', (W, H), WHITE)
    d = ImageDraw.Draw(img)

    M = 40  # Rand bis zum äußeren Rahmen
    d.rectangle([M, M, W - M, H - M], outline=BLACK, width=8)
    PAD = M + 44

    acc = Account.query.get(case.account_id) if case.account_id else None
    brand = ((acc.handle or acc.name) if acc else '') or ''
    brand = brand.strip()
    if brand:
        f_brand = _mcf_font(20, bold=True)
        bw = d.textlength(brand, font=f_brand)
        d.text((W - PAD - bw, M + 24), brand, font=f_brand, fill=GRAY)

    f_hdr = _mcf_font(84, bold=True)
    hdr = 'VERMISST'
    d.text(((W - d.textlength(hdr, font=f_hdr)) / 2, M + 26), hdr, font=f_hdr, fill=BLACK)
    y0 = M + 26 + 96
    if case.stadt:
        f_sub = _mcf_font(26, bold=True)
        s = case.stadt.upper()
        d.text(((W - d.textlength(s, font=f_sub)) / 2, y0), s, font=f_sub, fill=RED)
        y0 += 42
    else:
        y0 += 14

    # Detail-Zeilen (nur vorhandene Felder) als kombinierte, zentrierte
    # "Label: Wert"-Zeilen vorab aufbauen — VOR dem Foto, damit die Foto-Höhe
    # sich an den verbleibenden Platzbedarf anpassen kann (siehe unten). So
    # geht bei vielen/langen Angaben kein Feld mehr stillschweigend verloren,
    # es wird höchstens auf max. 2 Zeilen gekürzt. Reihenfolge nach Wichtigkeit
    # für Wiedererkennung/Fahndung — falls im Extremfall doch etwas weichen
    # muss, fällt zuerst das Unwichtigste weg.
    f_val = _mcf_font(27, bold=False)
    LINE_H, ROW_GAP, MAX_LINES = 34, 18, 2
    max_w = W - 2 * PAD
    rows_src = []
    seit = _mcf_seit_label(case)
    if seit:
        rows_src.append(('Vermisst seit', seit))
    if case.letzter_ort:
        rows_src.append(('Zuletzt gesehen', case.letzter_ort))
    if case.merkmale:
        rows_src.append(('Besondere Merkmale', case.merkmale))
    if case.kleidung:
        rows_src.append(('Kleidung', case.kleidung))
    if case.haarfarbe:
        rows_src.append(('Haare', case.haarfarbe))
    if case.groesse:
        rows_src.append(('Größe', case.groesse))
    if case.stadtteil:
        rows_src.append(('Ortsteil', case.stadtteil))

    row_render = []
    for label, val in rows_src:
        text = f'{label}: {val}'
        lines = _mcf_wrap(d, text, f_val, max_w)
        if len(lines) > MAX_LINES:
            lines = lines[:MAX_LINES]
            last = lines[-1]
            while d.textlength(last + '…', font=f_val) > max_w and len(last) > 1:
                last = last[:-1]
            lines[-1] = last.rstrip() + '…'
        row_render.append(lines)

    rows_height = sum(len(lines) * LINE_H + ROW_GAP for lines in row_render)

    # Footer (CTA-Zeile + gestrichelte Linie + abreißbare Kontakt-Streifen) hat
    # eine feste, von den Fall-Daten unabhängige Höhe — wird komplett vorab
    # reserviert, damit er nie mit den Fakten-Zeilen kollidiert.
    FOOTER_H = 190
    row_cutoff = H - M - FOOTER_H

    name = case.display_name()
    f_name = _mcf_font(50, bold=True)
    f_meta = _mcf_font(28, bold=False)
    name_block_h = 58 + (38 if (case.alter is not None or case.stadt) else 0) + 14

    # Foto: hat IMMER Vorrang vor den Detail-Zeilen — ein gut erkennbares Gesicht
    # ist für ein Vermisstenposter wichtiger als möglichst viele Textzeilen. Das
    # Foto ist deshalb immer mindestens quadratisch (1:1) und ausreichend groß;
    # reicht der Platz nicht für alle Zeilen, werden zuerst die unwichtigsten
    # Zeilen gekürzt/weggelassen — nie das Foto verkleinert.
    PHOTO_MIN, PHOTO_MAX = 460, 700
    photo_overhead = 26  # Abstand zwischen Foto und Name
    budget = row_cutoff - y0 - name_block_h - photo_overhead
    photo_side = max(PHOTO_MIN, min(PHOTO_MAX, budget - rows_height))

    photo = None
    if case.foto_media_id:
        b = _mcf_load_image_bytes(MediaItem.query.get(case.foto_media_id))
        if b:
            try:
                photo = ImageOps.exif_transpose(Image.open(_io.BytesIO(b)).convert('RGB'))
            except Exception:
                photo = None

    box_w = box_h = photo_side
    box_x0 = int((W - box_w) / 2)
    y = y0
    if photo:
        ratio = max(box_w / photo.width, box_h / photo.height)
        photo = photo.resize((int(photo.width * ratio), int(photo.height * ratio)), Image.LANCZOS)
        lft, top = (photo.width - box_w) // 2, (photo.height - box_h) // 2
        content = photo.crop((lft, top, lft + box_w, top + box_h))
    else:
        # Kein Foto vorhanden — statt einer leeren Fläche einen Platzhalter mit
        # Silhouette-Icon zeigen, damit das Poster nicht leer/unfertig wirkt.
        content = _mcf_draw_photo_placeholder(box_w, box_h)
    img.paste(content, (box_x0, int(y)))
    d.rectangle([box_x0, y, box_x0 + box_w, y + box_h], outline=BLACK, width=4)

    y += box_h + photo_overhead

    # Name + Alter/Stadt
    d.text(((W - d.textlength(name, font=f_name)) / 2, y), name, font=f_name, fill=BLACK)
    y += 58
    meta_parts = []
    if case.alter is not None:
        meta_parts.append(f'{case.alter} Jahre')
    if case.stadt:
        meta_parts.append(case.stadt)
    if meta_parts:
        meta = ' · '.join(meta_parts)
        d.text(((W - d.textlength(meta, font=f_meta)) / 2, y), meta, font=f_meta, fill=BLACK)
        y += 38
    y += 14

    # Fakten-Zeilen: schlichter, zentrierter Text (kein Karten-/Icon-Design) —
    # row_cutoff bleibt als Sicherheitsnetz für echte Extremfälle; eine Zeile
    # wird entweder komplett gezeichnet oder ganz weggelassen.
    for lines in row_render:
        row_h = len(lines) * LINE_H + ROW_GAP
        if y + row_h > row_cutoff:
            break
        for ln in lines:
            d.text(((W - d.textlength(ln, font=f_val)) / 2, y), ln, font=f_val, fill=BLACK)
            y += LINE_H
        y += ROW_GAP

    # Footer: CTA-Zeile, Trennlinie, große Kontaktzeile — bewusst OHNE
    # Abreiß-Streifen: das Poster ist für Instagram gedacht, ein physisch
    # abreißbarer Zettel ergibt in einem digitalen Bild keinen Sinn.
    footer_top = row_cutoff
    f_cta = _mcf_font(22, bold=True)
    cta_lines = _mcf_wrap(d, 'JEDER HINWEIS KANN HELFEN — BITTE TEILEN ODER ANRUFEN', f_cta, max_w)
    cy = footer_top
    for ln in cta_lines:
        d.text(((W - d.textlength(ln, font=f_cta)) / 2, cy), ln, font=f_cta, fill=RED)
        cy += 28

    rule_y = cy + 12
    d.line([PAD, rule_y, W - PAD, rule_y], fill=BLACK, width=2)

    cl = contact_line or 'Hinweise an die Polizei: Notruf 110'
    f_cl = _mcf_font(36, bold=True)
    cll = _mcf_wrap(d, cl, f_cl, max_w)
    cy = rule_y + 28
    for ln in cll:
        d.text(((W - d.textlength(ln, font=f_cl)) / 2, cy), ln, font=f_cl, fill=BLACK)
        cy += 44

    fname = f'mcf_{case.id}_{int(datetime.utcnow().timestamp())}.jpg'
    img.save(os.path.join(app.config['UPLOAD_FOLDER'], fname), format='JPEG', quality=92)
    return fname

def _mcf_extract_number(text):
    """Findet eine deutsche Telefon-/Kontaktnummer in einem Text. None wenn keine."""
    if not text:
        return None
    import re as _re
    m = _re.search(r'(\+49[\s\-/]?\d[\d\s\-/]{6,}\d|0\d{2,5}[\s\-/]?\d[\d\s\-/]{4,}\d)', str(text))
    return _re.sub(r'\s+', ' ', m.group(1)).strip() if m else None


def _resolve_emergency_contact(case):
    """Kontaktzeile: Quell-Nummer > Stadt/Stadtteil-DB > nur 110. Erfindet NIE eine
    Nummer. Gibt (contact_line, local_number_or_None)."""
    local = (case.quelle_nummer or '').strip() or None
    if not local and case.stadt:
        base = EmergencyNumber.query.filter(db.func.lower(EmergencyNumber.stadt) == case.stadt.strip().lower())
        row = None
        if case.stadtteil:
            row = base.filter(db.func.lower(EmergencyNumber.stadtteil) == case.stadtteil.strip().lower()).first()
        if not row:
            row = base.filter((EmergencyNumber.stadtteil == None) | (EmergencyNumber.stadtteil == '')).first()
        if not row:
            row = base.first()
        if row:
            local = row.number
    if local:
        return (f'Hinweise: Polizei {local} · Notruf 110', local)
    return ('Hinweise an die Polizei: Notruf 110', None)


def _mcf_dedup_key(vorname, nachname, alter, stadt, vermisst_seit):
    parts = [(vorname or '').strip().lower(), (nachname or '').strip().lower(),
             str(alter or ''), (stadt or '').strip().lower(),
             vermisst_seit.isoformat() if vermisst_seit else '']
    key = '|'.join(parts)
    return key if key.replace('|', '').strip() else ''


def _mcf_find_duplicate(dedup_key, exclude_id=None):
    if not dedup_key:
        return None
    q = MissingChildCase.query.filter(MissingChildCase.dedup_key == dedup_key)
    if exclude_id:
        q = q.filter(MissingChildCase.id != exclude_id)
    return q.first()


def _mcf_generate_caption(case, contact_line):
    """Caption NUR aus bekannten Feldern — erfindet nichts. KI optional, mit
    faktentreuem Fallback ohne KI."""
    facts = []
    if case.display_name() != 'Unbekannt':
        facts.append(f'Name: {case.display_name()}')
    if case.alter is not None:
        facts.append(f'Alter: {case.alter} Jahre')
    seit = _mcf_seit_label(case)
    if seit:
        facts.append(f'Vermisst seit: {seit}')
    if case.stadt:
        facts.append(f'Stadt: {case.stadt}')
    if case.stadtteil:
        facts.append(f'Ortsteil: {case.stadtteil}')
    if case.letzter_ort:
        facts.append(f'Zuletzt gesehen: {case.letzter_ort}')
    if case.groesse:
        facts.append(f'Größe: {case.groesse}')
    if case.haarfarbe:
        facts.append(f'Haare: {case.haarfarbe}')
    if case.kleidung:
        facts.append(f'Kleidung: {case.kleidung}')
    if case.merkmale:
        facts.append(f'Besondere Merkmale: {case.merkmale}')
    if case.beschreibung:
        facts.append(f'Beschreibung: {case.beschreibung}')
    if case.weitere_infos:
        facts.append(f'Weitere Informationen: {case.weitere_infos}')

    def _fallback():
        tag = f" #{case.stadt.replace(' ', '')}" if case.stadt else ''
        return '\n'.join(['🚨 VERMISST 🚨', ''] + facts + ['', contact_line, '',
                         'Bitte teilen, um zu helfen. #Vermisst' + tag])

    api_key = get_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY')
    if not api_key or not facts:
        return _fallback()
    system = (
        'Du erstellst Instagram-Captions für Vermisstenmeldungen von Kindern. '
        'ABSOLUT ZWINGEND: Verwende AUSSCHLIESSLICH die angegebenen Fakten. Erfinde, '
        'ergänze oder vermute NIEMALS etwas. Fehlende Angaben lässt du komplett weg. '
        'Ton: sachlich, ernst, respektvoll, klar strukturiert, gut lesbar. Beginne mit '
        "'🚨 VERMISST'. Liste die bekannten Angaben übersichtlich. Baue die vorgegebene "
        'Kontaktzeile unverändert ein. Ende mit kurzem Aufruf zum Teilen und wenigen '
        'passenden Hashtags. Keine Spekulation, keine Übertreibung, kein Clickbait.'
    )
    user = f"Bekannte Fakten (NUR diese verwenden):\n{chr(10).join(facts)}\n\nKontaktzeile (unverändert einbauen):\n{contact_line}"
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        model = get_setting('caption_model') or 'claude-haiku-4-5'
        resp = client.messages.create(model=model, max_tokens=700, system=system,
                                       messages=[{'role': 'user', 'content': user}])
        _log_ai('mcf_caption', resp)
        return resp.content[0].text.strip()
    except Exception as e:
        app.logger.warning('MCF Caption-Fehler: %s', e)
        return _fallback()


def _mcf_resolution_texts(case):
    """Vorschläge für Update-Kommentar + Story bei Auflösung (Kind gefunden).
    Bewusst generisch/faktentreu — keine erfundenen Details."""
    name = case.display_name()
    who = name if name != 'Unbekannt' else 'das vermisste Kind'
    comment = (f'Update: {who} wurde nach offiziellen Angaben wohlbehalten gefunden. '
               'Vielen Dank an alle, die beim Teilen geholfen haben. 💙')
    story = f'Gute Nachrichten: {who} wurde wohlbehalten gefunden. Danke fürs Teilen!'
    return comment, story


def _mcf_save_photo(fobj):
    """Speichert ein hochgeladenes Foto lokal + legt MediaItem an. Gibt id/None."""
    try:
        import uuid as _uuid
        ext = os.path.splitext(fobj.filename)[1].lower() or '.jpg'
        name = f'mcfphoto_{_uuid.uuid4().hex}{ext}'
        fobj.save(os.path.join(app.config['UPLOAD_FOLDER'], name))
        w = h = None
        try:
            from PIL import Image as _I
            w, h = _I.open(os.path.join(app.config['UPLOAD_FOLDER'], name)).size
        except Exception:
            pass
        m = MediaItem(filename=name, original_filename=fobj.filename, url=f'/media/file/{name}',
                      file_type='image', storage_source='local', width=w, height=h)
        db.session.add(m)
        db.session.flush()
        return m.id
    except Exception as e:
        app.logger.warning('MCF Foto-Upload: %s', e)
        return None


def _mcf_save_photo_bytes(raw_bytes, ext='.jpg', orig_filename=None):
    """Speichert rohe Bild-Bytes (z.B. Auto-Ausschnitt aus einer Vorlage) als
    MediaItem. Committet sofort (steht noch keinem Fall zu). Gibt id/None."""
    try:
        import uuid as _uuid
        name = f'mcfphoto_{_uuid.uuid4().hex}{ext}'
        path = os.path.join(app.config['UPLOAD_FOLDER'], name)
        with open(path, 'wb') as fh:
            fh.write(raw_bytes)
        w = h = None
        try:
            from PIL import Image as _I
            w, h = _I.open(path).size
        except Exception:
            pass
        m = MediaItem(filename=name, original_filename=orig_filename or name, url=f'/media/file/{name}',
                      file_type='image', storage_source='local', width=w, height=h)
        db.session.add(m)
        db.session.commit()
        return m.id
    except Exception as e:
        app.logger.warning('MCF Foto-Bytes-Upload: %s', e)
        return None


def _mcf_target_account():
    """Feste Ziel-Seite für Missing-Children-Fälle: eine bundesweite IG-Seite,
    keine Stadt-Zuordnung (in Einstellungen konfiguriert)."""
    acc_id = get_setting('mcf_target_account_id')
    if not acc_id:
        return None
    return Account.query.get(int(acc_id))


def _mcf_parse_date(s):
    s = (s or '').strip()
    for fmt in ('%Y-%m-%d', '%d.%m.%Y'):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None


def _mcf_case_dict(c):
    acc = Account.query.get(c.account_id) if c.account_id else None
    return {
        'id': c.id, 'name': c.display_name(), 'vorname': c.vorname, 'nachname': c.nachname,
        'alter': c.alter, 'stadt': c.stadt, 'stadtteil': c.stadtteil, 'geschlecht': c.geschlecht,
        'vermisst_seit': c.vermisst_seit.isoformat() if c.vermisst_seit else None,
        'vermisst_zeit': c.vermisst_zeit,
        'letzter_ort': c.letzter_ort, 'groesse': c.groesse, 'haarfarbe': c.haarfarbe,
        'kleidung': c.kleidung, 'merkmale': c.merkmale,
        'beschreibung': c.beschreibung, 'weitere_infos': c.weitere_infos,
        'quelle_name': c.quelle_name, 'quelle_url': c.quelle_url, 'quelle_nummer': c.quelle_nummer,
        'status': c.status, 'origin': c.origin,
        'image': f'/media/file/{c.generated_image_path}' if c.generated_image_path else None,
        'caption': c.caption, 'contact_line': c.contact_line,
        'account': acc.name if acc else None, 'account_id': c.account_id,
        'telegram_sent': bool(c.telegram_sent_at),
        'published_at': c.published_at.strftime('%d.%m.%Y %H:%M') if c.published_at else None,
        'ig_post_ref': c.ig_post_ref, 'update_comment': c.update_comment, 'story_draft': c.story_draft,
        'resolution_note': c.resolution_note,
        'update_detected': bool(c.update_detected), 'update_source_url': c.update_source_url,
        'created_at': c.created_at.strftime('%d.%m.%Y') if c.created_at else None,
    }


@app.route('/content-studio/missing-children')
@login_required
def missing_children_factory():
    status_f = request.args.get('status', '')
    q = MissingChildCase.query
    if status_f:
        q = q.filter_by(status=status_f)
    cases = q.order_by((MissingChildCase.status == 'erledigt'),
                       MissingChildCase.created_at.desc()).all()
    accounts = Account.query.filter(Account.status.in_(['active', 'paused'])).order_by(Account.name).all()
    from collections import Counter
    counts = Counter(c.status for c in MissingChildCase.query.all())
    return render_template('missing_children.html',
        cases=[_mcf_case_dict(c) for c in cases],
        accounts=[{'id': a.id, 'name': a.name} for a in accounts],
        counts=dict(counts), status_f=status_f, active_page='studio')


@app.route('/content-studio/missing-children/<int:case_id>')
@login_required
def missing_child_case(case_id):
    c = MissingChildCase.query.get_or_404(case_id)
    accounts = Account.query.filter(Account.status.in_(['active', 'paused'])).order_by(Account.name).all()
    return render_template('missing_child_detail.html', case=_mcf_case_dict(c),
        accounts=[{'id': a.id, 'name': a.name} for a in accounts], active_page='studio')


@app.route('/api/mcf/case', methods=['POST'])
@login_required
def mcf_create_case():
    d = request.form
    vorname = (d.get('vorname') or '').strip() or None
    nachname = (d.get('nachname') or '').strip() or None
    stadt = (d.get('stadt') or '').strip() or None
    files = [f for f in request.files.getlist('fotos') if f and f.filename]
    try:
        extracted_photo_id = int(d.get('extracted_photo_media_id')) if (d.get('extracted_photo_media_id') or '').strip() else None
    except Exception:
        extracted_photo_id = None
    if not (vorname or nachname or stadt or files or extracted_photo_id):
        return jsonify({'ok': False, 'error': 'Bitte mindestens Foto oder Name/Stadt angeben.'}), 400
    try:
        alter = int(d.get('alter')) if (d.get('alter') or '').strip() else None
    except Exception:
        alter = None
    vs = _mcf_parse_date(d.get('vermisst_seit'))
    dk = _mcf_dedup_key(vorname, nachname, alter, stadt, vs)
    dup = _mcf_find_duplicate(dk)
    if dup:
        return jsonify({'ok': True, 'duplicate': True, 'case_id': dup.id,
                        'msg': 'Dieser Fall existiert bereits — es wurde kein Duplikat angelegt.'})
    acc_id = None
    try:
        acc_id = int(d.get('account_id')) if (d.get('account_id') or '').strip() else None
    except Exception:
        acc_id = None
    account = Account.query.get(acc_id) if acc_id else _mcf_target_account()
    c = MissingChildCase(
        vorname=vorname, nachname=nachname, alter=alter,
        geschlecht=(d.get('geschlecht') or '').strip() or None,
        stadt=stadt, stadtteil=(d.get('stadtteil') or '').strip() or None,
        vermisst_seit=vs, vermisst_zeit=(d.get('vermisst_zeit') or '').strip() or None,
        letzter_ort=(d.get('letzter_ort') or '').strip() or None,
        groesse=(d.get('groesse') or '').strip() or None,
        haarfarbe=(d.get('haarfarbe') or '').strip() or None,
        kleidung=(d.get('kleidung') or '').strip() or None,
        merkmale=(d.get('merkmale') or '').strip() or None,
        beschreibung=(d.get('beschreibung') or '').strip() or None,
        weitere_infos=(d.get('weitere_infos') or '').strip() or None,
        quelle_name=(d.get('quelle_name') or '').strip() or None,
        quelle_url=(d.get('quelle_url') or '').strip() or None,
        quelle_nummer=(d.get('quelle_nummer') or '').strip() or None,
        account_id=account.id if account else None,
        origin='manuell', dedup_key=dk, status='entwurf')
    db.session.add(c)
    db.session.flush()
    media_ids = []
    for f in files:
        mid = _mcf_save_photo(f)
        if mid:
            media_ids.append(mid)
    if extracted_photo_id and extracted_photo_id not in media_ids:
        media_ids.append(extracted_photo_id)
    if media_ids:
        c.foto_media_id = media_ids[0]
        c.weitere_fotos = json.dumps(media_ids[1:])
    cl, _ = _resolve_emergency_contact(c)
    c.contact_line = cl
    c.generated_image_path = _render_missing_child_image(c, cl)
    c.caption = _mcf_generate_caption(c, cl)
    db.session.commit()
    return jsonify({'ok': True, 'case_id': c.id, 'case': _mcf_case_dict(c)})


@app.route('/api/mcf/extract-from-image', methods=['POST'])
@login_required
def mcf_extract_from_image():
    """Nimmt ein Foto/Scan einer bestehenden (offiziellen) Vermisstenanzeige entgegen,
    liest die Angaben per Claude Vision aus (erfindet NIE etwas) und versucht best-effort,
    das Foto der vermissten Person aus der Vorlage auszuschneiden. Klappt der Auto-Ausschnitt
    nicht sicher, wird kein Foto geraten — der Mensch lädt es dann manuell zugeschnitten hoch."""
    f = request.files.get('poster_image')
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': 'Kein Bild hochgeladen.'}), 400
    api_key = get_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key konfiguriert (Einstellungen → KI).'}), 400

    img_bytes = f.read()
    if not img_bytes:
        return jsonify({'ok': False, 'error': 'Bild ist leer.'}), 400
    if len(img_bytes) > 15 * 1024 * 1024:
        return jsonify({'ok': False, 'error': 'Bild zu groß (max. 15 MB).'}), 400
    ext = (os.path.splitext(f.filename)[1] or '.jpg').lower()
    mime = {'.png': 'image/png', '.webp': 'image/webp', '.gif': 'image/gif'}.get(ext, 'image/jpeg')

    import base64 as _b64
    import io as _io
    img_b64 = _b64.standard_b64encode(img_bytes).decode()

    system = (
        'Du liest eine gescannte/fotografierte OFFIZIELLE Vermisstenanzeige (Polizei o.ä.) für ein '
        'Kind aus. ABSOLUT ZWINGEND: Extrahiere AUSSCHLIESSLICH, was wörtlich auf dem Bild steht — '
        'erfinde oder vermute NIEMALS etwas. Fehlt eine Angabe, setze null. Antworte NUR mit einem '
        'JSON-Objekt, keine Erklärungen davor/danach:\n'
        '{"vorname": str|null, "nachname": str|null, "alter": int|null, "geschlecht": str|null, '
        '"stadt": str|null, "stadtteil": str|null, "vermisst_seit": "YYYY-MM-DD"|null, '
        '"vermisst_zeit": "morgens"|"vormittags"|"mittags"|"nachmittags"|"abends"|"nachts"|null '
        '(NUR wenn eine Tageszeit/Uhrzeit explizit angegeben ist), '
        '"letzter_ort": str|null, "groesse": str|null, "haarfarbe": str|null, "kleidung": str|null, '
        '"merkmale": str|null, "beschreibung": str|null, "quelle_nummer": str|null '
        '(auf der Anzeige genannte Kontakt-/Notrufnummer), '
        '"photo_bbox": [x0,y0,x1,y1]|null — NUR wenn auf der Vorlage ein klar abgegrenztes separates '
        'Foto der vermissten Person zu sehen ist (kein Logo, kein Wappen, kein Fließtext). Gib die '
        'Bounding-Box als Bruchteile (0.0–1.0) der Bildbreite/-höhe zurück (x0,y0 = oben-links, '
        'x1,y1 = unten-rechts). Bei Unsicherheit: null.}'
    )
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        model = get_setting('vision_model') or 'claude-sonnet-4-6'
        resp = client.messages.create(
            model=model, max_tokens=800, system=system,
            messages=[{'role': 'user', 'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': img_b64}},
                {'type': 'text', 'text': 'Lies diese Vermisstenanzeige aus und antworte nur mit dem JSON-Objekt.'}
            ]}]
        )
        _log_ai('mcf_extract_image', resp)
        raw = resp.content[0].text.strip()
        import re as _re
        m = _re.search(r'\{.*\}', raw, _re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Claude-Fehler: {e}'}), 500

    # Best-effort Foto-Ausschnitt — nur übernehmen, wenn Bounding-Box plausibel ist
    photo_media_id = None
    photo_extracted = False
    bbox = data.get('photo_bbox')
    if isinstance(bbox, list) and len(bbox) == 4:
        try:
            from PIL import Image, ImageOps
            im = ImageOps.exif_transpose(Image.open(_io.BytesIO(img_bytes)).convert('RGB'))
            iw, ih = im.size
            x0, y0, x1, y1 = [float(v) for v in bbox]
            if 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1 and (x1 - x0) * (y1 - y0) >= 0.02:
                box = (int(x0 * iw), int(y0 * ih), int(x1 * iw), int(y1 * ih))
                crop = im.crop(box)
                buf = _io.BytesIO()
                crop.save(buf, format='JPEG', quality=90)
                photo_media_id = _mcf_save_photo_bytes(buf.getvalue(), '.jpg', f.filename)
                photo_extracted = bool(photo_media_id)
        except Exception as e:
            app.logger.warning('MCF Foto-Ausschnitt: %s', e)

    fields = {k: data.get(k) for k in (
        'vorname', 'nachname', 'alter', 'geschlecht', 'stadt', 'stadtteil',
        'vermisst_seit', 'vermisst_zeit', 'letzter_ort', 'groesse', 'haarfarbe',
        'kleidung', 'merkmale', 'beschreibung', 'quelle_nummer')}
    resp_data = {'ok': True, 'fields': fields, 'photo_extracted': photo_extracted}
    if photo_media_id:
        mi = MediaItem.query.get(photo_media_id)
        resp_data['photo_media_id'] = photo_media_id
        resp_data['photo_url'] = mi.url if mi else None
    return jsonify(resp_data)


def _mcf_is_safe_url(url):
    """Blockt SSRF: nur http/https, keine privaten/internen IP-Ziele."""
    import ipaddress
    import socket
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return False
        ip = ipaddress.ip_address(socket.gethostbyname(parsed.hostname))
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)
    except Exception:
        return False


from html.parser import HTMLParser as _HTMLParser


class _MCFTextExtractor(_HTMLParser):
    """Minimaler HTML→Text-Extraktor ohne Zusatz-Dependency (kein bs4 im
    Projekt) — überspringt script/style sowie Navigation/Header/Footer/Formulare
    (auf vielen Seiten, v.a. Behörden-/News-Portalen, macht allein das Hauptmenü
    tausende Zeichen aus und würde sonst den eigentlichen Meldungstext aus dem
    an Claude gesendeten Textausschnitt verdrängen). Stack-basiert, damit
    verschachtelte/unsauber geschlossene Tags nicht durcheinanderkommen."""
    SKIP_TAGS = ('script', 'style', 'noscript', 'nav', 'header', 'footer', 'form')
    # Einzelne Tags ohne Ende (z.B. <img>, <input>) werden nicht auf den Stack gelegt
    VOID_TAGS = ('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
                 'link', 'meta', 'param', 'source', 'track', 'wbr')

    def __init__(self):
        super().__init__()
        self._skip_stack = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS and tag not in self.VOID_TAGS:
            self._skip_stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self._skip_stack:
            # letztes offenes Vorkommen dieses Tags entfernen (robust gegen
            # unsauber verschachteltes/nicht geschlossenes HTML)
            for i in range(len(self._skip_stack) - 1, -1, -1):
                if self._skip_stack[i] == tag:
                    del self._skip_stack[i:]
                    break

    def handle_data(self, data):
        if not self._skip_stack:
            t = data.strip()
            if t:
                self.parts.append(t)

    def text(self):
        return '\n'.join(self.parts)


def _mcf_fetch_url_content(url):
    """Lädt eine Vermisstenanzeigen-Webseite, gibt (text, image_url) zurück.
    text: sichtbarer Seiteninhalt (gekürzt) für die Feld-Extraktion per Claude.
    image_url: bestes verfügbares Vorschaubild (og:image/twitter:image) oder None
    — wird NICHT blind übernommen, sondern dem Nutzer vor dem Speichern angezeigt."""
    import re as _re
    from urllib.parse import urljoin
    resp = _requests.get(url, timeout=10, headers={'User-Agent': 'ContentOS/1.0'},
                          stream=True, allow_redirects=True)
    resp.raise_for_status()
    # Redirect-Ziel erneut prüfen (SSRF via Redirect auf internes Ziel verhindern)
    if not _mcf_is_safe_url(resp.url):
        raise ValueError('Umleitung auf nicht erlaubtes Ziel.')
    raw = resp.raw.read(3 * 1024 * 1024, decode_content=True)
    html = raw.decode(resp.encoding or 'utf-8', errors='ignore')

    parser = _MCFTextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    text = parser.text()[:6000]

    image_url = None
    for pat in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']'):
        m = _re.search(pat, html, _re.I)
        if m:
            image_url = urljoin(resp.url, m.group(1))
            break
    return text, image_url


@app.route('/api/mcf/extract-from-url', methods=['POST'])
@login_required
def mcf_extract_from_url():
    """Nimmt einen Link zu einer bestehenden (offiziellen) Vermisstenanzeige entgegen,
    liest die Seite aus und extrahiert die Angaben per Claude (erfindet NIE etwas).
    Ein gefundenes Vorschaubild wird als Kandidat übernommen, aber im Frontend erst
    nach Sichtprüfung durch den Nutzer gespeichert — klappt kein sicherer Fund,
    bleibt der manuelle Zuschnitt-Upload wie beim Foto-Upload-Pfad der Fallback."""
    from urllib.parse import urlparse as _urlparse
    d = request.get_json(silent=True) or request.form
    url = (d.get('url') or '').strip()
    if not url:
        return jsonify({'ok': False, 'error': 'Bitte einen Link angeben.'}), 400
    if not _mcf_is_safe_url(url):
        return jsonify({'ok': False, 'error': 'Diese URL ist nicht erlaubt.'}), 400
    api_key = get_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key konfiguriert (Einstellungen → KI).'}), 400

    try:
        text, image_url = _mcf_fetch_url_content(url)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Seite konnte nicht geladen werden: {e}'}), 400
    if not text.strip():
        return jsonify({'ok': False, 'error': 'Auf der Seite wurde kein lesbarer Text gefunden.'}), 400

    data = _mcf_extract_case_from_text(text, api_key) or {}
    if not any(data.get(k) for k in (
            'vorname', 'nachname', 'alter', 'stadt', 'letzter_ort',
            'groesse', 'haarfarbe', 'kleidung', 'merkmale', 'beschreibung')):
        return jsonify({'ok': False, 'error': 'Auf der Seite konnten keine Angaben zu einer Vermisstenmeldung erkannt werden.'}), 400

    # Vorschaubild bestenfalls laden — best effort, gleiche SSRF-Prüfung wie die Seite selbst
    photo_media_id = None
    photo_extracted = False
    if image_url and _mcf_is_safe_url(image_url):
        try:
            ir = _requests.get(image_url, timeout=8, headers={'User-Agent': 'ContentOS/1.0'}, stream=True)
            ir.raise_for_status()
            if not _mcf_is_safe_url(ir.url):
                raise ValueError('Bild-Umleitung nicht erlaubt.')
            ctype = (ir.headers.get('Content-Type') or '').lower()
            if ctype.startswith('image/'):
                img_bytes = ir.raw.read(15 * 1024 * 1024, decode_content=True)
                ext = {'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif'}.get(ctype, '.jpg')
                photo_media_id = _mcf_save_photo_bytes(img_bytes, ext, os.path.basename(image_url))
                photo_extracted = bool(photo_media_id)
        except Exception as e:
            app.logger.warning('MCF URL-Foto: %s', e)

    fields = {k: data.get(k) for k in (
        'vorname', 'nachname', 'alter', 'stadt', 'stadtteil',
        'vermisst_seit', 'vermisst_zeit', 'letzter_ort', 'groesse', 'haarfarbe',
        'kleidung', 'merkmale', 'beschreibung', 'quelle_nummer')}
    resp_data = {'ok': True, 'fields': fields, 'photo_extracted': photo_extracted,
                 'quelle_url': url, 'quelle_name': _urlparse(url).hostname or url}
    if photo_media_id:
        mi = MediaItem.query.get(photo_media_id)
        resp_data['photo_media_id'] = photo_media_id
        resp_data['photo_url'] = mi.url if mi else None
    return jsonify(resp_data)


@app.route('/api/mcf/case/<int:cid>/regenerate-image', methods=['POST'])
@login_required
def mcf_regen_image(cid):
    c = MissingChildCase.query.get_or_404(cid)
    cl, _ = _resolve_emergency_contact(c)
    c.contact_line = cl
    c.generated_image_path = _render_missing_child_image(c, cl)
    db.session.commit()
    return jsonify({'ok': True, 'image': f'/media/file/{c.generated_image_path}'})


@app.route('/api/mcf/case/<int:cid>/regenerate-caption', methods=['POST'])
@login_required
def mcf_regen_caption(cid):
    c = MissingChildCase.query.get_or_404(cid)
    cl, _ = _resolve_emergency_contact(c)
    c.caption = _mcf_generate_caption(c, cl)
    db.session.commit()
    return jsonify({'ok': True, 'caption': c.caption})


@app.route('/api/mcf/case/<int:cid>/update', methods=['POST'])
@login_required
def mcf_update_case(cid):
    c = MissingChildCase.query.get_or_404(cid)
    d = request.get_json(silent=True) or request.form
    for f in ('vorname', 'nachname', 'stadt', 'stadtteil', 'letzter_ort', 'groesse',
              'haarfarbe', 'vermisst_zeit', 'kleidung', 'merkmale', 'beschreibung',
              'weitere_infos', 'quelle_name', 'quelle_url', 'quelle_nummer', 'caption'):
        if f in d:
            setattr(c, f, (d.get(f) or '').strip() or None)
    if 'alter' in d:
        try:
            c.alter = int(d.get('alter')) if str(d.get('alter') or '').strip() else None
        except Exception:
            pass
    if 'account_id' in d:
        try:
            c.account_id = int(d.get('account_id')) or None
        except Exception:
            c.account_id = None
    c.dedup_key = _mcf_dedup_key(c.vorname, c.nachname, c.alter, c.stadt, c.vermisst_seit)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/mcf/case/<int:cid>/status', methods=['POST'])
@login_required
def mcf_set_status(cid):
    c = MissingChildCase.query.get_or_404(cid)
    d = request.get_json(silent=True) or {}
    st = d.get('status')
    if st not in ('entwurf', 'veroeffentlicht', 'erledigt'):
        return jsonify({'ok': False, 'error': 'ungültiger Status'}), 400
    c.status = st
    if st == 'veroeffentlicht' and not c.published_at:
        c.published_at = datetime.utcnow()
        if (d.get('ig_post_ref') or '').strip():
            c.ig_post_ref = d.get('ig_post_ref').strip()
    if st == 'erledigt':
        cm, story = _mcf_resolution_texts(c)
        c.update_comment = c.update_comment or cm
        c.story_draft = c.story_draft or story
        c.update_detected = False
        if (d.get('resolution_note') or '').strip():
            c.resolution_note = d.get('resolution_note').strip()
    db.session.commit()
    return jsonify({'ok': True, 'case': _mcf_case_dict(c)})


@app.route('/api/mcf/case/<int:cid>/telegram', methods=['POST'])
@login_required
def mcf_send_telegram(cid):
    c = MissingChildCase.query.get_or_404(cid)
    token = get_setting('telegram_bot_token')
    if not token:
        return jsonify({'ok': False, 'error': 'Kein Telegram-Bot-Token konfiguriert.'}), 400
    acc = Account.query.get(c.account_id) if c.account_id else None
    chat_id = ((acc.telegram_chat_id or '').strip() if acc else '')
    if not chat_id or chat_id in ('None', 'null'):
        return jsonify({'ok': False, 'error': 'Ziel-Seite hat keinen Telegram-Channel. Bitte Seite wählen und Channel eintragen.'}), 400
    # Bild sicherstellen
    if not c.generated_image_path or not os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], c.generated_image_path)):
        cl, _ = _resolve_emergency_contact(c)
        c.contact_line = cl
        c.generated_image_path = _render_missing_child_image(c, cl)
    img_path = os.path.join(app.config['UPLOAD_FOLDER'], c.generated_image_path)
    try:
        with open(img_path, 'rb') as f:
            _tg_call(token, 'sendPhoto', data={'chat_id': chat_id}, files={'photo': f})
        # Caption + Quelle als kopierbarer Text
        parts = [c.caption or '']
        src = []
        if c.quelle_name:
            src.append(f'Quelle: {c.quelle_name}')
        if c.quelle_url:
            src.append(c.quelle_url)
        if src:
            parts.append('\n'.join(src))
        text = '\n\n'.join(p for p in parts if p).strip()[:4000]
        if text:
            _tg_call(token, 'sendMessage', json={'chat_id': chat_id, 'text': text})
        _tg_call(token, 'sendMessage', json={
            'chat_id': chat_id,
            'text': 'Nach dem Posten auf Instagram bitte bestätigen:',
            'reply_markup': {'inline_keyboard': [[
                {'text': '✅ Auf Instagram veröffentlicht', 'callback_data': f'mcfposted_{c.id}'}]]}})
    except Exception as e:
        app.logger.warning('MCF Telegram-Versand: %s', e)
        return jsonify({'ok': False, 'error': f'Versand fehlgeschlagen: {e}'}), 500
    c.telegram_sent_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/mcf/case/<int:cid>/delete', methods=['POST'])
@login_required
def mcf_delete_case(cid):
    c = MissingChildCase.query.get_or_404(cid)
    db.session.delete(c)
    db.session.commit()
    return jsonify({'ok': True})


# ─── Notfallnummern-Verwaltung ──────────────────────────────────
@app.route('/api/mcf/emergency', methods=['POST'])
@login_required
def mcf_emergency_add():
    d = request.get_json(silent=True) or request.form
    stadt = (d.get('stadt') or '').strip()
    number = (d.get('number') or '').strip()
    if not stadt or not number:
        return jsonify({'ok': False, 'error': 'Stadt und Nummer sind nötig.'}), 400
    stadtteil = (d.get('stadtteil') or '').strip() or None
    base = EmergencyNumber.query.filter(db.func.lower(EmergencyNumber.stadt) == stadt.lower())
    if stadtteil:
        ex = base.filter(db.func.lower(EmergencyNumber.stadtteil) == stadtteil.lower()).first()
    else:
        ex = base.filter((EmergencyNumber.stadtteil == None) | (EmergencyNumber.stadtteil == '')).first()
    label = (d.get('label') or '').strip() or None
    if ex:
        ex.number, ex.label, ex.source, ex.verified = number, label, 'manuell', True
    else:
        db.session.add(EmergencyNumber(stadt=stadt, stadtteil=stadtteil, number=number,
                                       label=label, source='manuell', verified=True))
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/mcf/emergency/<int:eid>/delete', methods=['POST'])
@login_required
def mcf_emergency_delete(eid):
    e = EmergencyNumber.query.get_or_404(eid)
    db.session.delete(e)
    db.session.commit()
    return jsonify({'ok': True})


# ─── Quellen-Verwaltung (RSS) ───────────────────────────────────
@app.route('/api/mcf/source', methods=['POST'])
@login_required
def mcf_source_add():
    d = request.get_json(silent=True) or request.form
    url = (d.get('url') or '').strip()
    if not url.startswith('http'):
        return jsonify({'ok': False, 'error': 'Bitte eine gültige RSS-URL angeben.'}), 400
    src = json.loads(get_setting('mcf_sources') or '[]')
    if not any(s.get('url') == url for s in src):
        src.append({'name': (d.get('name') or '').strip() or url, 'url': url})
        set_setting('mcf_sources', json.dumps(src))
        db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/mcf/source/delete', methods=['POST'])
@login_required
def mcf_source_delete():
    d = request.get_json(silent=True) or {}
    url = (d.get('url') or '').strip()
    src = [s for s in json.loads(get_setting('mcf_sources') or '[]') if s.get('url') != url]
    set_setting('mcf_sources', json.dumps(src))
    db.session.commit()
    return jsonify({'ok': True})


# ─── Auto-Recherche (Phase 2) ───────────────────────────────────
def _mcf_extract_case_from_text(text, api_key):
    """Prüft eine Meldung auf Vermisstenmeldung eines KINDES und extrahiert Felder
    NUR aus dem Text (erfindet nichts). Gibt dict|None."""
    if not api_key or not text:
        return None
    system = (
        'Du prüfst deutsche Polizei-/Nachrichtenmeldungen auf Vermisstenmeldungen von KINDERN '
        '(unter 18 Jahren). Extrahiere AUSSCHLIESSLICH, was wörtlich im Text steht — erfinde oder '
        'vermute NIEMALS etwas. Fehlt eine Angabe, setze null. Antworte NUR mit einem JSON-Objekt: '
        '{"is_case": bool (Vermisstenmeldung?), "is_child": bool (vermisste Person unter 18? nur true '
        'wenn im Text belegt, z.B. Alter<18 oder "Kind"/"Junge"/"Mädchen"/"Schüler"/"Jugendliche"), '
        '"vorname": str|null, "nachname": str|null, "alter": int|null, "stadt": str|null, '
        '"stadtteil": str|null, "vermisst_seit": "YYYY-MM-DD"|null, '
        '"vermisst_zeit": "morgens"|"vormittags"|"mittags"|"nachmittags"|"abends"|"nachts"|null '
        '(NUR wenn eine Tageszeit/Uhrzeit explizit im Text steht), "letzter_ort": str|null, '
        '"groesse": str|null, "haarfarbe": str|null, "kleidung": str|null, '
        '"merkmale": str|null, "beschreibung": str|null, "quelle_nummer": str|null (im Text genannte '
        'Polizei-Kontaktnummer)}.'
    )
    try:
        import anthropic
        import re as _re
        client = anthropic.Anthropic(api_key=api_key)
        model = get_setting('caption_model') or 'claude-haiku-4-5'
        resp = client.messages.create(model=model, max_tokens=600, system=system,
                                       messages=[{'role': 'user', 'content': str(text)[:4000]}])
        _log_ai('mcf_research', resp)
        raw = resp.content[0].text.strip()
        m = _re.search(r'\{.*\}', raw, _re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        app.logger.warning('MCF Extract: %s', e)
        return None


def _mcf_run_research():
    """Scannt konfigurierte RSS-Quellen nach Vermisstenmeldungen von Kindern und
    legt Auto-Entwürfe an (mit Dedup + Auto-Poster/Caption). Gibt (created, checked)."""
    sources = json.loads(get_setting('mcf_sources') or '[]')
    api_key = get_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY')
    if not api_key or not sources:
        return 0, 0
    KW = ['vermisst', 'vermisste', 'vermisster', 'vermisstenfahndung', 'abgängig', 'fahndung', 'wird vermisst']
    checked = created = 0
    for src in sources:
        url = (src.get('url') or '').strip()
        name = src.get('name') or url
        if not url:
            continue
        try:
            entries = fetch_rss_feed(url, keywords=KW) or []
        except Exception as e:
            app.logger.warning('MCF Feed %s: %s', url, e)
            continue
        for e in entries:
            checked += 1
            link = (e.get('url') or '').strip()
            if link and MissingChildCase.query.filter_by(quelle_url=link).first():
                continue
            data = _mcf_extract_case_from_text(f"{e.get('title', '')}\n\n{e.get('description', '')}", api_key)
            if not data or not data.get('is_case') or not data.get('is_child'):
                continue
            vs = _mcf_parse_date(data.get('vermisst_seit'))
            alter = data.get('alter') if isinstance(data.get('alter'), int) else None
            dk = _mcf_dedup_key(data.get('vorname'), data.get('nachname'), alter, data.get('stadt'), vs)
            if _mcf_find_duplicate(dk):
                continue
            stadt = (data.get('stadt') or '').strip() or None
            acc = _mcf_target_account()

            def _s(k):
                v = data.get(k)
                return (str(v).strip() or None) if v else None

            c = MissingChildCase(
                origin='auto', status='entwurf', dedup_key=dk,
                vorname=_s('vorname'), nachname=_s('nachname'), alter=alter,
                stadt=stadt, stadtteil=_s('stadtteil'), vermisst_seit=vs,
                vermisst_zeit=_s('vermisst_zeit'),
                letzter_ort=_s('letzter_ort'), groesse=_s('groesse'),
                haarfarbe=_s('haarfarbe'),
                kleidung=_s('kleidung'), merkmale=_s('merkmale'), beschreibung=_s('beschreibung'),
                quelle_name=name, quelle_url=link or None, quelle_nummer=_s('quelle_nummer'),
                account_id=acc.id if acc else None)
            db.session.add(c)
            db.session.flush()
            cl, _ = _resolve_emergency_contact(c)
            c.contact_line = cl
            try:
                c.generated_image_path = _render_missing_child_image(c, cl)
                c.caption = _mcf_generate_caption(c, cl)
            except Exception as ex:
                app.logger.warning('MCF Auto-Gen: %s', ex)
            created += 1
        db.session.commit()
    return created, checked


@app.route('/api/mcf/research/run', methods=['POST'])
@login_required
def mcf_research_run():
    api_key = get_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Auto-Recherche braucht einen Anthropic-API-Key (Einstellungen → KI).'}), 400
    if not json.loads(get_setting('mcf_sources') or '[]'):
        return jsonify({'ok': False, 'error': 'Noch keine Quellen hinterlegt.'}), 400
    created, checked = _mcf_run_research()
    return jsonify({'ok': True, 'created': created, 'checked': checked})


@app.route('/api/mcf/research/toggle', methods=['POST'])
@login_required
def mcf_research_toggle():
    d = request.get_json(silent=True) or {}
    set_setting('mcf_auto_research', '1' if d.get('on') else '0')
    db.session.commit()
    return jsonify({'ok': True, 'on': d.get('on') and True or False})


@app.route('/api/mcf/target-account', methods=['POST'])
@login_required
def mcf_target_account_save():
    d = request.get_json(silent=True) or {}
    acc_id = (str(d.get('account_id')) or '').strip()
    set_setting('mcf_target_account_id', acc_id or '')
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/content-studio/missing-children/einstellungen')
@login_required
def mcf_einstellungen():
    sources = json.loads(get_setting('mcf_sources') or '[]')
    numbers = EmergencyNumber.query.order_by(EmergencyNumber.stadt, EmergencyNumber.stadtteil).all()
    target_account_id = get_setting('mcf_target_account_id')
    return render_template('mcf_einstellungen.html',
        sources=sources,
        numbers=[{'id': n.id, 'stadt': n.stadt, 'stadtteil': n.stadtteil, 'number': n.number,
                  'label': n.label, 'verified': n.verified} for n in numbers],
        auto_on=(get_setting('mcf_auto_research') == '1'),
        accounts=Account.query.order_by(Account.name).all(),
        target_account_id=int(target_account_id) if target_account_id else None,
        active_page='studio')


# ─── Phase 3: Überwachung nach Veröffentlichung ─────────────────
def _mcf_extract_resolution(text, api_key):
    """Prüft, ob eine Meldung die AUFLÖSUNG einer Kinder-Vermisstenmeldung ist
    (gefunden / Fahndung eingestellt). Extrahiert Namen/Stadt zur Zuordnung. dict|None."""
    if not api_key or not text:
        return None
    system = (
        'Du prüfst deutsche Meldungen, ob sie die AUFLÖSUNG einer Vermisstenmeldung eines Kindes '
        'beschreiben (Kind wohlbehalten/lebend gefunden oder aufgefunden, Fahndung eingestellt oder '
        'zurückgenommen, Suche beendet). Extrahiere NUR was im Text steht, erfinde nichts. Antworte '
        'NUR als JSON: {"is_resolution": bool, "positive": bool (wohlbehalten/lebend gefunden?), '
        '"vorname": str|null, "nachname": str|null, "stadt": str|null, "alter": int|null}.'
    )
    try:
        import anthropic
        import re as _re
        client = anthropic.Anthropic(api_key=api_key)
        model = get_setting('caption_model') or 'claude-haiku-4-5'
        resp = client.messages.create(model=model, max_tokens=300, system=system,
                                       messages=[{'role': 'user', 'content': str(text)[:4000]}])
        _log_ai('mcf_monitor', resp)
        m = _re.search(r'\{.*\}', resp.content[0].text.strip(), _re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        app.logger.warning('MCF Resolution-Extract: %s', e)
        return None


def _mcf_match_published(cases, info):
    """Ordnet eine Auflösungs-Info einem veröffentlichten Fall zu — konservativ:
    Nachname + (Vorname ODER Stadt), oder Vorname + Stadt. None wenn unsicher."""
    def norm(s):
        return (s or '').strip().lower()
    iv, inn, ist = norm(info.get('vorname')), norm(info.get('nachname')), norm(info.get('stadt'))
    for c in cases:
        cv, cnn, cst = norm(c.vorname), norm(c.nachname), norm(c.stadt)
        name_last = bool(inn and cnn and inn == cnn)
        name_first = bool(iv and cv and iv == cv)
        city = bool(ist and cst and (ist in cst or cst in ist))
        if (name_last and (name_first or city)) or (name_first and city):
            return c
    return None


def _mcf_monitor_updates():
    """Scannt Quellen nach Auflösungs-Meldungen zu VERÖFFENTLICHTEN Fällen und
    markiert Treffer (update_detected). Setzt NIE automatisch auf erledigt — der
    Mensch bestätigt. Gibt (flagged, checked)."""
    published = MissingChildCase.query.filter_by(status='veroeffentlicht', update_detected=False).all()
    api_key = get_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY')
    sources = json.loads(get_setting('mcf_sources') or '[]')
    if not published or not api_key or not sources:
        return 0, 0
    RES_KW = ['gefunden', 'wohlbehalten', 'aufgefunden', 'fahndung eingestellt', 'fahndung zurück',
              'nicht mehr vermisst', 'wohlauf', 'angetroffen', 'suche beendet']
    flagged = checked = 0
    for src in sources:
        try:
            entries = fetch_rss_feed(src.get('url'), keywords=RES_KW) or []
        except Exception:
            continue
        for e in entries:
            checked += 1
            info = _mcf_extract_resolution(f"{e.get('title', '')}\n\n{e.get('description', '')}", api_key)
            if not info or not info.get('is_resolution'):
                continue
            m = _mcf_match_published(published, info)
            if m and not m.update_detected:
                m.update_detected = True
                m.update_source_url = (e.get('url') or None)
                m.update_found_at = datetime.utcnow()
                flagged += 1
        db.session.commit()
    return flagged, checked


@app.route('/api/mcf/monitor/run', methods=['POST'])
@login_required
def mcf_monitor_run():
    api_key = get_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Braucht einen Anthropic-API-Key.'}), 400
    flagged, checked = _mcf_monitor_updates()
    return jsonify({'ok': True, 'flagged': flagged, 'checked': checked})


@app.route('/api/mcf/case/<int:cid>/dismiss-update', methods=['POST'])
@login_required
def mcf_dismiss_update(cid):
    c = MissingChildCase.query.get_or_404(cid)
    c.update_detected = False
    db.session.commit()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════════
# TREND RADAR — Welche Themen hat heute wahrscheinlich der größte Teil
# Deutschlands mitbekommen? Rohsignale sammeln → KI-Clustering → Score.
# Qualität vor Quantität: lieber 3 echte Großthemen als 100 News-Schnipsel.
# ══════════════════════════════════════════════════════════════════════════════

_TR_PLATFORM_LABELS = {
    'google':    'Google Trends',
    'instagram': 'Instagram',
    'tiktok':    'TikTok',
    'tv':        'TV',
    'youtube':   'YouTube',
    'wikipedia': 'Wikipedia',
    'reddit':    'Reddit',
}
_tr_scan_status = {'running': False, 'error': None, 'started_at': None,
                   'finished_at': None, 'signals': 0, 'topics': 0, 'step': ''}


def _tr_add_signal(source, title, detail=None, url=None, metric=None):
    """Signal speichern; Duplikate (gleiche Quelle+Titel innerhalb 20h) nur aktualisieren."""
    title = (title or '').strip()
    if not title:
        return None
    dedup = f'{source}:{title.lower()}'[:400]
    cutoff = datetime.utcnow() - timedelta(hours=20)
    existing = TrendSignal.query.filter(
        TrendSignal.dedup_key == dedup,
        TrendSignal.detected_at >= cutoff).first()
    if existing:
        if metric is not None:
            existing.metric = metric
        if detail:
            existing.detail = detail
        return None
    sig = TrendSignal(source=source, title=title[:400], detail=detail, url=url,
                      metric=metric, dedup_key=dedup)
    db.session.add(sig)
    return sig


# ── Collector 1: Google Trends (Trending Now DE, RSS) ─────────────────────────
# Hinweis: Googles frühere separate "Realtime Trends"-API ist tot (404). Der
# /trending/rss-Feed IST inzwischen Googles Live-"Trending Now"-Feed und
# aktualisiert sich untertägig. Wir werten zusätzlich das pubDate jedes Trends
# aus (wie frisch ist der Spike?) — das ist der Realtime-Hebel: ganz junge
# Ausschläge werden erkennbar und fließen genauer in started_at ein.
def _tr_fetch_google_trends():
    import urllib.request
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    count = 0
    req = urllib.request.Request('https://trends.google.com/trending/rss?geo=DE',
                                 headers={'User-Agent': 'Mozilla/5.0 (ContentOS TrendRadar)'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        root = ET.fromstring(resp.read())
    now = datetime.utcnow()
    for item in root.findall('.//item'):
        title = (item.findtext('title') or '').strip()
        if not title:
            continue
        traffic, news_title, news_url = None, None, None
        for child in item:
            tag = child.tag.split('}')[-1]
            if tag == 'approx_traffic':
                traffic = (child.text or '').strip()
            elif tag == 'news_item' and news_title is None:
                for sub in child:
                    stag = sub.tag.split('}')[-1]
                    if stag == 'news_item_title':
                        news_title = (sub.text or '').strip()
                    elif stag == 'news_item_url':
                        news_url = (sub.text or '').strip()
        # Frische aus pubDate (Realtime-Signal): wie viele Stunden läuft der Trend?
        fresh_h = None
        pub = item.findtext('pubDate')
        if pub:
            try:
                dt = parsedate_to_datetime(pub)
                if dt.tzinfo:
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                fresh_h = max(0.0, (now - dt).total_seconds() / 3600)
            except Exception:
                fresh_h = None
        detail = f'~{traffic} Suchanfragen' if traffic else 'Trending Search DE'
        if fresh_h is not None:
            detail += (' · gerade frisch aufgekommen' if fresh_h < 3
                       else f' · seit ~{fresh_h:.0f} Std. im Trend')
        if news_title:
            detail += f' · News: {news_title[:120]}'
        # approx_traffic wie "500+" / "20.000+" → Zahl für metric
        metric = None
        if traffic:
            try:
                metric = float(traffic.replace('+', '').replace('.', '').replace(',', ''))
            except Exception:
                metric = None
        # Sehr frische Spikes zusätzlich gewichten (Realtime-Priorisierung)
        if metric and fresh_h is not None and fresh_h < 3:
            metric *= 1.5
        if _tr_add_signal('google_trends', title, detail, news_url, metric):
            count += 1
    return count


# ── Collector: TV-Quoten (DWDL) ───────────────────────────────────────────────
# Es gibt keinen offenen strukturierten Quoten-Feed mehr. DWDLs Haupt-RSS enthält
# aber verlässlich die täglichen Quoten-Artikel ("XY siegt", "Sat.1 legt zu",
# Zuschauerzahlen). Wir filtern per Stichwörtern und legen sie als echte TV-Signale
# an — ergänzend zur KI-Websuche, die TV bisher allein bestätigen musste.
def _tr_fetch_tv_quoten():
    import urllib.request
    import xml.etree.ElementTree as ET
    import re as _re
    count = 0
    req = urllib.request.Request('https://www.dwdl.de/rss/allethemen.xml',
                                 headers={'User-Agent': 'Mozilla/5.0 (ContentOS TrendRadar)'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        root = ET.fromstring(resp.read())
    kw = ('quote', 'zuschauer', 'marktanteil', 'million', 'einschalt', 'reichweite',
          'primetime', 'sahen', 'quoten', 'tv-tag', 'einschaltquote')
    for item in root.findall('.//item'):
        title = (item.findtext('title') or '').strip()
        desc = (item.findtext('description') or '').strip()
        if not title:
            continue
        blob = (title + ' ' + desc).lower()
        if not any(k in blob for k in kw):
            continue
        link = (item.findtext('link') or '').strip()
        # Zuschauerzahl aus dem Text ("4,52 Millionen" / "1,3 Mio.") → metric
        metric, detail = None, 'TV-Quoten (DWDL)'
        m = _re.search(r'(\d+[.,]?\d*)\s*(?:Mio\.?|Millionen)', title + ' ' + desc, _re.I)
        if m:
            try:
                metric = float(m.group(1).replace(',', '.')) * 1_000_000
                detail = f'~{m.group(1)} Mio. Zuschauer (DWDL)'
            except Exception:
                metric = None
        if _tr_add_signal('tv', title, detail, link, metric):
            count += 1
    return count


# ── Collector 2: Wikipedia Top-Pageviews (de) ────────────────────────────────
def _tr_fetch_wikipedia():
    import requests as req_lib
    count = 0
    # Gestern probieren; Wikimedia lädt die Daten teils erst spät → Fallback vorgestern
    r = None
    for days_back in (1, 2):
        day = datetime.utcnow() - timedelta(days=days_back)
        url = ('https://wikimedia.org/api/rest_v1/metrics/pageviews/top/'
               f'de.wikipedia/all-access/{day.year}/{day.month:02d}/{day.day:02d}')
        r = req_lib.get(url, headers={'User-Agent': 'ContentOS-TrendRadar/1.0'}, timeout=15)
        if r.status_code == 200:
            break
    if r is None or r.status_code != 200:
        return 0
    articles = (r.json().get('items') or [{}])[0].get('articles') or []
    skip_prefixes = ('Wikipedia:', 'Spezial:', 'Datei:', 'Hilfe:', 'Portal:',
                     'Kategorie:', 'Vorlage:', 'Benutzer:', 'Wikipedia_')
    taken = 0
    for a in articles:
        name = a.get('article') or ''
        if (not name or name.startswith(skip_prefixes) or name == 'Hauptseite'
                or name == 'wiki.phtml' or '.php' in name):
            continue
        views = int(a.get('views') or 0)
        if views < 30000:      # unter ~30k Aufrufen/Tag kein Massenthema
            continue
        title = name.replace('_', ' ')
        detail = f'{views:,} Aufrufe gestern (de.wikipedia)'.replace(',', '.')
        wp_url = f'https://de.wikipedia.org/wiki/{name}'
        if _tr_add_signal('wikipedia', title, detail, wp_url, float(views)):
            count += 1
        taken += 1
        if taken >= 20:
            break
    return count


# ── Collector 3: Reddit r/de (nur außergewöhnlich große Posts) ────────────────
def _tr_fetch_reddit():
    import requests as req_lib
    count = 0
    r = req_lib.get('https://www.reddit.com/r/de/top.json?t=day&limit=30',
                    headers={'User-Agent': 'ContentOS-TrendRadar/1.0'}, timeout=15)
    if r.status_code != 200:
        return 0
    for child in (r.json().get('data') or {}).get('children') or []:
        d = child.get('data') or {}
        ups = int(d.get('ups') or 0)
        if ups < 1500:          # nur massiv aufgefallene Threads
            continue
        title = (d.get('title') or '').strip()
        detail = f'{ups:,} Upvotes in r/de (24h)'.replace(',', '.')
        url = 'https://www.reddit.com' + (d.get('permalink') or '')
        if _tr_add_signal('reddit', title, detail, url, float(ups)):
            count += 1
    return count


# ── Collector 4: YouTube Trending DE (optionaler API-Key) ────────────────────
def _tr_fetch_youtube():
    import requests as req_lib
    key = get_setting('youtube_api_key')
    if not key:
        return 0
    count = 0
    r = req_lib.get('https://www.googleapis.com/youtube/v3/videos',
                    params={'part': 'snippet,statistics', 'chart': 'mostPopular',
                            'regionCode': 'DE', 'maxResults': 25, 'key': key},
                    timeout=15)
    if r.status_code != 200:
        return 0
    for v in r.json().get('items') or []:
        stats = v.get('statistics') or {}
        views = int(stats.get('viewCount') or 0)
        if views < 200000:      # nur außergewöhnlich große Videos
            continue
        sn = v.get('snippet') or {}
        title = (sn.get('title') or '').strip()
        channel = sn.get('channelTitle') or ''
        detail = f'{views:,} Views · YouTube-Trending DE ({channel})'.replace(',', '.')
        url = f"https://www.youtube.com/watch?v={v.get('id')}"
        if _tr_add_signal('youtube', title, detail, url, float(views)):
            count += 1
    return count


# ── Collector 5: Instagram-News-Overperformance (RapidAPI) ───────────────────
def _tr_fetch_ig_posts_page(username, rapidapi_key):
    """Erste Posts-Seite eines Accounts — gleiche API-Kandidaten wie Inspirationen."""
    import requests as req_lib
    candidates = [
        ('instagram-scraper21.p.rapidapi.com',
         'https://instagram-scraper21.p.rapidapi.com/api/v1/posts',
         {'username': username, 'limit': '30', 'include_captions': 'true'}),
        ('instagram-scraper-api2.p.rapidapi.com',
         'https://instagram-scraper-api2.p.rapidapi.com/v1/posts',
         {'username_or_id_or_url': username}),
        ('instagram-looter2.p.rapidapi.com',
         'https://instagram-looter2.p.rapidapi.com/feed-by-username',
         {'username': username, 'count': '30'}),
    ]
    for host, url, params in candidates:
        try:
            resp = req_lib.get(url, params=params, timeout=20,
                               headers={'x-rapidapi-key': rapidapi_key,
                                        'x-rapidapi-host': host})
            if resp.status_code != 200:
                continue
            raw = resp.json()
            data_block = raw.get('data') or {}
            if isinstance(data_block, list) and data_block:
                return data_block
            items = (data_block.get('items') or data_block.get('posts')
                     or raw.get('items') or raw.get('posts') or [])
            if items:
                return items
        except Exception:
            continue
    return []


# Absolute Relevanz-Stufen für Instagram-Posts (unabhängig vom Account-Schnitt):
# ein Post mit 150k+ Likes ist auf deutschen News-Accounts (z.B. Tagesschau)
# IMMER ein Massenthema, selbst wenn der Account-Median so hoch liegt, dass die
# 2×-Ratio-Schwelle ihn nicht triggern würde.
_TR_IG_VERY_RELEVANT_LIKES = 150_000   # → "SEHR RELEVANT"
_TR_IG_CRITICAL_LIKES      = 200_000   # → "KRITISCH RELEVANT"


def _tr_process_account_posts(source, parsed, platform, url_fmt):
    """Gemeinsame Overperformance-Auswertung für Instagram/TikTok-Quellen:
    cached den Account-Median (TrendSource.avg_likes/avg_updated_at), berechnet
    neben dem reinen Verhältnis auch die Like-Geschwindigkeit pro Stunde (ein
    frischer Post mit hoher Rate ist aussagekräftiger als nur "viele Likes
    insgesamt") und erzeugt daraus TrendSignals. `parsed` = Liste von
    (likes, comments, ts_oder_None, caption, code). `url_fmt(code)` baut die
    Post-URL für die jeweilige Plattform."""
    now_ts = datetime.utcnow().timestamp()
    source.last_scanned_at = datetime.utcnow()
    likes_list = [p[0] for p in parsed]
    if len(likes_list) < 6:
        return 0
    sorted_likes = sorted(likes_list)
    median = sorted_likes[len(sorted_likes) // 2]
    if median <= 0:
        return 0
    source.avg_likes = float(median)
    source.avg_updated_at = datetime.utcnow()
    count = 0
    for likes, comments, ts, caption, code in parsed:
        ratio = likes / median
        # Absolute Stufen (Instagram): ab 150k Likes zählt der Post unabhängig
        # vom Account-Schnitt, ab 200k gilt er als kritisch relevant
        level = None
        if platform == 'instagram' and likes >= _TR_IG_CRITICAL_LIKES:
            level = '🔴 KRITISCH RELEVANT'
        elif platform == 'instagram' and likes >= _TR_IG_VERY_RELEVANT_LIKES:
            level = '🟠 SEHR RELEVANT'
        # sonst: nur klar überdurchschnittliche Posts
        if level is None and (ratio < 2.0 or likes < 3000):
            continue
        # frisch muss er immer sein (max. 72h alt)
        if ts and (now_ts - ts) > 72 * 3600:
            continue
        parts = [f'{likes:,} Likes'.replace(',', '.')]
        if comments:
            parts.append(f'{comments:,} Kommentare'.replace(',', '.'))
        if ts:
            hours_since = max((now_ts - ts) / 3600, 0.5)
            velocity = likes / hours_since
            parts.append(f'{velocity:,.0f}/Std. (seit {hours_since:.0f} Std.)'.replace(',', '.'))
        parts.append(f'{ratio:.1f}× über Account-Schnitt'.replace(',', '.'))
        headline = caption.split('\n')[0][:200] if caption else f'Post {code}'
        title = f'@{source.handle}: {headline}'
        detail = ' · '.join(parts) + f' ({source.niche})'
        if level:
            detail = f'{level} · {detail}'
        url = url_fmt(code) if code else None
        if _tr_add_signal(platform, title, detail, url, float(likes)):
            count += 1
    return count


def _tr_fetch_instagram_news():
    """Nur Posts deutlich ÜBER der Durchschnitts-Performance des Accounts
    zählen. Läuft über alle aktiven Instagram-TrendSources und respektiert je
    Quelle ihr eigenes Scan-Intervall (scan_interval_hours) — Nischen, die
    seltener geprüft werden müssen (z.B. Memes), sparen dadurch API-Calls,
    ohne dass zeitkritische News-Accounts seltener gescanned werden."""
    rapidapi_key = get_setting('rapidapi_key')
    if not rapidapi_key:
        return 0
    now = datetime.utcnow()
    sources = TrendSource.query.filter_by(platform='instagram', active=True).all()
    due = [s for s in sources if not s.last_scanned_at
           or (now - s.last_scanned_at).total_seconds() >= s.scan_interval_hours * 3600]
    count = 0
    for source in due[:20]:
        try:
            items = _tr_fetch_ig_posts_page(source.handle, rapidapi_key)
            parsed = []
            for item in items:
                raw_likes = (item.get('likeCount') or item.get('like_count') or
                             item.get('likes') or
                             (item.get('edge_media_to_like') or {}).get('count'))
                raw_comments = (item.get('commentsCount') or item.get('comment_count') or
                                (item.get('edge_media_to_comment') or {}).get('count'))
                try:
                    likes = int(raw_likes)
                except Exception:
                    continue
                try:
                    comments = int(raw_comments)
                except Exception:
                    comments = 0
                ts = (item.get('taken_at') or item.get('taken_at_timestamp') or
                      item.get('timestamp'))
                try:
                    ts = float(ts)
                except Exception:
                    ts = None
                caption = item.get('caption') or ''
                if isinstance(caption, dict):
                    caption = caption.get('text') or ''
                code = str(item.get('shortCode') or item.get('code') or
                           item.get('shortcode') or item.get('id') or '')
                parsed.append((likes, comments, ts, caption.strip(), code))
            count += _tr_process_account_posts(
                source, parsed, 'instagram',
                lambda code: f'https://www.instagram.com/p/{code}/')
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            app.logger.warning('Trend Radar IG @%s: %s', source.handle, e)
    return count


# ── Collector 6: TikTok-Overperformance (RapidAPI) ───────────────────────────
def _tr_fetch_tiktok_posts_page(username, rapidapi_key):
    """Erste Posts-Seite eines TikTok-Accounts — mehrere Kandidaten-Hosts nach
    dem gleichen Fallback-Muster wie bei Instagram. ACHTUNG: ungetestet, da
    beim Aufbau keine echte TikTok-RapidAPI-Subscription verfügbar war —
    Host/Feld-Namen nach dem ersten echten Scan ggf. nachjustieren."""
    import requests as req_lib
    candidates = [
        ('tiktok-scraper7.p.rapidapi.com',
         'https://tiktok-scraper7.p.rapidapi.com/user/posts',
         {'unique_id': f'@{username}', 'count': '30', 'cursor': '0'}),
        ('tiktok-video-no-watermark2.p.rapidapi.com',
         'https://tiktok-video-no-watermark2.p.rapidapi.com/user/posts',
         {'unique_id': f'@{username}', 'count': '30', 'cursor': '0'}),
        ('tokapi-mobile-version.p.rapidapi.com',
         'https://tokapi-mobile-version.p.rapidapi.com/v1/post/user',
         {'username': username, 'count': '30'}),
    ]
    for host, url, params in candidates:
        try:
            resp = req_lib.get(url, params=params, timeout=20,
                               headers={'x-rapidapi-key': rapidapi_key,
                                        'x-rapidapi-host': host})
            if resp.status_code != 200:
                continue
            raw = resp.json()
            data_block = raw.get('data') or {}
            if isinstance(data_block, list) and data_block:
                return data_block
            items = (data_block.get('videos') or data_block.get('items')
                     or data_block.get('posts') or raw.get('videos')
                     or raw.get('items') or raw.get('posts') or [])
            if items:
                return items
        except Exception:
            continue
    return []


def _tr_fetch_tiktok():
    """TikTok-Overperformance analog zu Instagram (_tr_process_account_posts
    wird geteilt) — läuft über aktive TikTok-TrendSources, respektiert je
    Quelle ihr Scan-Intervall."""
    rapidapi_key = get_setting('rapidapi_key')
    if not rapidapi_key:
        return 0
    now = datetime.utcnow()
    sources = TrendSource.query.filter_by(platform='tiktok', active=True).all()
    due = [s for s in sources if not s.last_scanned_at
           or (now - s.last_scanned_at).total_seconds() >= s.scan_interval_hours * 3600]
    count = 0
    for source in due[:20]:
        try:
            items = _tr_fetch_tiktok_posts_page(source.handle, rapidapi_key)
            parsed = []
            for item in items:
                stats = item.get('stats') or {}
                raw_likes = (item.get('digg_count') or item.get('diggCount') or
                             item.get('like_count') or item.get('likeCount') or
                             stats.get('diggCount') or stats.get('digg_count'))
                raw_comments = (item.get('comment_count') or item.get('commentCount') or
                                stats.get('commentCount') or stats.get('comment_count'))
                try:
                    likes = int(raw_likes)
                except Exception:
                    continue
                try:
                    comments = int(raw_comments)
                except Exception:
                    comments = 0
                ts = (item.get('create_time') or item.get('createTime'))
                try:
                    ts = float(ts)
                except Exception:
                    ts = None
                caption = item.get('desc') or item.get('title') or item.get('description') or ''
                if isinstance(caption, dict):
                    caption = caption.get('text') or ''
                code = str(item.get('video_id') or item.get('aweme_id') or item.get('id') or '')
                parsed.append((likes, comments, ts, str(caption).strip(), code))
            count += _tr_process_account_posts(
                source, parsed, 'tiktok',
                lambda code, u=source.handle: f'https://www.tiktok.com/@{u}/video/{code}')
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            app.logger.warning('Trend Radar TikTok @%s: %s', source.handle, e)
    return count


def _tr_collect_signals():
    """Alle Quellen abklappern; jede Quelle darf einzeln fehlschlagen."""
    collectors = [
        ('Google Trends', _tr_fetch_google_trends),
        ('Wikipedia', _tr_fetch_wikipedia),
        ('Reddit', _tr_fetch_reddit),
        ('YouTube', _tr_fetch_youtube),
        ('Instagram', _tr_fetch_instagram_news),
        ('TikTok', _tr_fetch_tiktok),
        ('TV-Quoten', _tr_fetch_tv_quoten),
    ]
    total = 0
    for name, fn in collectors:
        _tr_scan_status['step'] = f'Sammle Signale: {name}…'
        try:
            total += fn()
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            app.logger.warning('Trend Radar %s: %s', name, e)
    return total


# ── KI-Clustering: Rohsignale → wenige große Themen ──────────────────────────
_TR_CLUSTER_SYSTEM = """Du bist der Trend-Radar von Content OS. Deine einzige Aufgabe:
aus Rohsignalen abschätzen, welche Themen HEUTE wahrscheinlich der größte Teil
Deutschlands mitbekommen hat. Das ist eine Relevanzübersicht, KEINE Nachrichtenübersicht.

Harte Regeln:
- Qualität vor Quantität. Gibt es nur 3 wirklich große Themen, liefere nur 3. Maximal 12.
- Nur Themen mit breiter, deutschlandweiter Aufmerksamkeit (Score >= 55). Nischenthemen,
  Lokalnachrichten, Evergreen-Wikipedia-Artikel (Feiertage, Serien-Dauerbrenner,
  Promi-Biografien ohne aktuellen Anlass) konsequent weglassen.
- Ein EINZELNES Wikipedia-Signal OHNE Bestätigung durch mindestens eine andere
  Quelle (Social-Media-Overperformance, Reddit, Google Trends, YouTube) reicht
  NICHT für ein Thema. Wikipedia-Ausreißer entstehen oft durch Zufall, Trivia,
  Kreuzworträtsel-Fragen oder TV-Wiederholungen, ohne dass es ein echtes,
  breites Gesprächsthema ist — im Zweifel weglassen statt raten.
- Signale mit konkreten Kennzahlen (Like-Verhältnis zum Account-Schnitt,
  Like-Geschwindigkeit pro Stunde, Upvotes, Suchvolumen) sind aussagekräftiger
  als die reine Tatsache, dass eine einzelne Quelle etwas erwähnt hat — gewichte
  sie im Score entsprechend stärker als bloße Erwähnungen ohne Zahlen dahinter.
- Instagram-Signale mit der Markierung "SEHR RELEVANT" (150k+ Likes) sind ein
  starker Beleg für deutschlandweite Aufmerksamkeit — das zugehörige Thema
  gehört fast immer in die Liste (Score typischerweise 70+). Die Markierung
  "KRITISCH RELEVANT" (200k+ Likes) ist das stärkste Einzelsignal überhaupt:
  Thema IMMER aufnehmen und den Score sehr hoch ansetzen (typischerweise 85+),
  auch wenn andere Quellen es (noch) nicht zeigen.
- Begriffe zum selben Ereignis IMMER zu EINEM Thema zusammenführen
  (z.B. "Bahn", "DB", "Streik", "Zugausfall" → "Bahn-Streik"). Nie zwei Einträge
  für dasselbe Ereignis.
- Score 0-100 = geschätzter Anteil der Bevölkerung, der das Thema mitbekommen hat,
  gestützt auf Stärke und ANZAHL unabhängiger Quellen. Mehrere Plattformen
  gleichzeitig = stärkstes Indiz.
- platforms nur nennen, wenn Signale es belegen. Instagram/TikTok-Signale kommen
  direkt aus echten Account-Overperformance-Daten (inkl. Meme-Nischen, nicht nur
  News). "tv"-Signale kommen jetzt ebenfalls direkt aus echten Quoten-Meldungen
  (Quelle "tv", von DWDL) — setze has_tv, wenn ein solches Signal zum Thema passt.
  Ergänzend darfst du TV per Websuche prüfen (max. 2 Suchen, z.B. genaue
  Zuschauerzahl/Quote gestern), aber nur wenn es einen Mehrwert bringt.
- Wenn ein existierendes Thema (existing_id) dasselbe Ereignis beschreibt, dieses
  weiterführen statt ein neues anzulegen.

Antworte NUR mit einem JSON-Array, keinerlei Text davor oder danach:
[{"existing_id": null oder Zahl,
  "title": "prägnanter Kurztitel, max 60 Zeichen",
  "description": "1-2 Sätze: worum es geht und warum es gerade groß ist",
  "score": 0-100,
  "started_hours_ago": Zahl,
  "platforms": ["google","instagram","tiktok","tv","youtube","wikipedia","reddit"],
  "signal_ids": [ids der zugehörigen Signale],
  "sources": [{"source": "Quellenname", "detail": "warum relevant", "url": "oder null"}]}]"""


def _balanced_span_from(text, start, open_char, close_char):
    """Liefert den klammern-balancierten Teilstring ab Position `start` (die
    auf open_char zeigen muss), oder None wenn die Klammern nie schließen
    (z.B. bei einer durch max_tokens abgeschnittenen Antwort). Ignoriert
    Klammern innerhalb von JSON-Strings (inkl. Escapes), damit z.B. ein "["
    in einem Beschreibungstext die Tiefenzählung nicht verfälscht."""
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _extract_balanced_json(text, open_char='[', close_char=']'):
    """Findet ein JSON-Array/-Objekt im Text — robuster als eine gierige Regex
    (z.B. \\[[\\s\\S]*\\]), die vom ERSTEN bis zum LETZTEN Klammer-Zeichen im
    GESAMTEN Text matcht und dabei über unzusammenhängende Klammer-Paare
    hinwegreißt (bricht z.B. bei aktivierter Websuche, wenn die Antwort
    mehrteilig ist, oder wenn irgendwo im Fließtext Zitier-Klammern wie "[1]"
    auftauchen — beides erzeugt "Expecting ',' delimiter"/"Extra data").
    Probiert stattdessen JEDES Klammer-Vorkommen als möglichen Start und
    nimmt das erste vollständige Segment, das tatsächlich als Liste von
    Objekten (bzw. bei '{' als Objekt) parsebar ist — eine einzelne Fußnote
    wie "[1]" ist zwar auch balanciert, aber kein Objekt/keine Objektliste
    und wird deshalb übersprungen."""
    pos = 0
    while True:
        start = text.find(open_char, pos)
        if start == -1:
            return None
        span = _balanced_span_from(text, start, open_char, close_char)
        if span:
            try:
                parsed = json.loads(span)
                if open_char == '[' and (not parsed or isinstance(parsed[0], dict)):
                    return span
                if open_char == '{' and isinstance(parsed, dict):
                    return span
            except Exception:
                pass
        pos = start + 1


def _tr_claude(api_key, user_content, max_tokens=6000):
    """Claude-Call fürs Clustering; behandelt pause_turn (Websuche-Tool)."""
    import anthropic as _ant
    client = _ant.Anthropic(api_key=api_key)
    model = get_setting('analysis_model') or 'claude-sonnet-4-6'
    messages = [{'role': 'user', 'content': user_content}]
    resp = None
    for _ in range(5):
        resp = client.messages.create(
            model=model, max_tokens=max_tokens, system=_TR_CLUSTER_SYSTEM,
            tools=[{'type': 'web_search_20260209', 'name': 'web_search', 'max_uses': 2}],
            messages=messages)
        _log_ai('trend_radar', resp)
        if resp.stop_reason == 'pause_turn':
            messages.append({'role': 'assistant', 'content': resp.content})
            continue
        break
    return ''.join(b.text for b in resp.content if getattr(b, 'type', '') == 'text')


def _tr_cluster_and_save(api_key):
    """Signale der letzten 24h clustern und als TrendTopics speichern."""
    import re as _re
    cutoff = datetime.utcnow() - timedelta(hours=24)
    signals = TrendSignal.query.filter(TrendSignal.detected_at >= cutoff)\
        .order_by(TrendSignal.source, TrendSignal.metric.desc().nullslast()).all()
    if not signals:
        return 0

    sig_lines = []
    for s in signals:
        age_h = int((datetime.utcnow() - s.detected_at).total_seconds() // 3600)
        sig_lines.append(f'[{s.id}] ({s.source}, vor {age_h}h) {s.title}'
                         + (f' — {s.detail}' if s.detail else ''))

    topic_cutoff = datetime.utcnow() - timedelta(days=7)
    existing = TrendTopic.query.filter(TrendTopic.archived == False,
                                       TrendTopic.last_seen_at >= topic_cutoff)\
        .order_by(TrendTopic.score.desc()).all()
    ex_lines = [f'[{t.id}] {t.title} (Score {t.score}): {(t.description or "")[:150]}'
                for t in existing]

    # ── Nutzer-Feedback der letzten 30 Tage als Kalibrierung ──────────────
    fb_cutoff = datetime.utcnow() - timedelta(days=30)
    fb_up = TrendTopic.query.filter(TrendTopic.feedback == 1,
                                    TrendTopic.feedback_at >= fb_cutoff)\
        .order_by(TrendTopic.feedback_at.desc()).limit(15).all()
    fb_down = TrendTopic.query.filter(TrendTopic.feedback == -1,
                                      TrendTopic.feedback_at >= fb_cutoff)\
        .order_by(TrendTopic.feedback_at.desc()).limit(15).all()
    fb_block = ''
    if fb_up or fb_down:
        fb_block = '\n\n--- NUTZER-FEEDBACK (zur Kalibrierung, aus echter Rückmeldung) ---\n'
        if fb_up:
            fb_block += ('Als WIRKLICH große Themen bestätigt: '
                         + '; '.join(t.title for t in fb_up) + '\n')
        if fb_down:
            fb_block += ('Als ZU KLEIN / kein echtes Großthema markiert: '
                         + '; '.join(t.title for t in fb_down) + '\n')
        fb_block += ('Lerne daraus: Themen, die den "zu klein"-Beispielen ähneln, '
                     'zurückhaltender bewerten oder weglassen; Themen wie die '
                     'bestätigten ruhig aufnehmen.')

    berlin_now = now_berlin().strftime('%A, %d.%m.%Y %H:%M')
    uc = (f'Jetzt ist {berlin_now} (Deutschland).\n\n'
          f'--- EXISTIERENDE THEMEN (letzte 7 Tage) ---\n'
          + ('\n'.join(ex_lines) if ex_lines else '(keine)')
          + fb_block
          + f'\n\n--- ROHSIGNALE (letzte 24h, {len(signals)} Stück) ---\n'
          + '\n'.join(sig_lines)[:24000])

    # max_tokens hochgesetzt: 12 Themen mit je Quellen-Array/Signal-IDs können
    # das alte Limit von 6000 überschreiten und die JSON-Antwort mitten im
    # Objekt abschneiden (unschließbare Klammer -> _extract_balanced_json
    # liefert dann sauber None statt eines kaputten Teilstrings, aber besser
    # ist, das Abschneiden gar nicht erst zu riskieren).
    text = _tr_claude(api_key, uc, max_tokens=8000)
    raw_json = _extract_balanced_json(text or '', '[', ']')
    try:
        items = json.loads(raw_json) if raw_json else []
    except (json.JSONDecodeError, TypeError) as e:
        app.logger.error('Trend Radar JSON-Parse fehlgeschlagen: %s | raw: %s', e, (raw_json or '')[:500])
        items = []

    by_id = {t.id: t for t in existing}
    valid_platforms = set(_TR_PLATFORM_LABELS)
    try:
        alert_threshold = int(get_setting('trend_alert_score') or 80)
    except Exception:
        alert_threshold = 80
    saved = 0
    for it in items:
        if not isinstance(it, dict) or not it.get('title'):
            continue
        try:
            score = max(0, min(100, int(it.get('score', 0))))
        except Exception:
            continue
        if score < 55:
            continue
        topic = by_id.get(it.get('existing_id'))
        if topic is None:
            topic = TrendTopic(title=str(it['title'])[:200])
            db.session.add(topic)
        topic.title = str(it['title'])[:200]
        topic.description = it.get('description')
        # Verlauf: alten Score als prev_score sichern (nur wenn schon mal bewertet),
        # Peak nachziehen, Snapshot für die Sparkline schreiben
        if topic.score:
            topic.prev_score = topic.score
        topic.peak_score = max(topic.peak_score or 0, score)
        topic.score = score
        topic.last_seen_at = datetime.utcnow()
        db.session.flush()
        db.session.add(TrendScoreSnapshot(topic_id=topic.id, score=score))
        try:
            hours_ago = max(0, float(it.get('started_hours_ago') or 0))
        except Exception:
            hours_ago = 0
        started = datetime.utcnow() - timedelta(hours=hours_ago)
        if topic.started_at is None or started < topic.started_at:
            topic.started_at = started
        plats = {p for p in (it.get('platforms') or []) if p in valid_platforms}
        topic.has_google    = 'google' in plats
        topic.has_instagram = 'instagram' in plats
        topic.has_tiktok    = 'tiktok' in plats
        topic.has_tv        = 'tv' in plats
        topic.has_youtube   = 'youtube' in plats
        topic.has_wikipedia = 'wikipedia' in plats
        topic.has_reddit    = 'reddit' in plats
        topic.sources_json = json.dumps(
            [s for s in (it.get('sources') or []) if isinstance(s, dict)][:20],
            ensure_ascii=False)
        db.session.flush()
        sig_ids = {i for i in (it.get('signal_ids') or []) if isinstance(i, int)}
        if sig_ids:
            TrendSignal.query.filter(TrendSignal.id.in_(sig_ids))\
                .update({'topic_id': topic.id}, synchronize_session=False)
        # Alert bei großen Themen (einmalig pro Thema)
        if not topic.alerted and score >= alert_threshold:
            _tr_fire_topic_alert(topic)
            topic.alerted = True
        saved += 1
    db.session.commit()
    return saved


def _tr_fire_topic_alert(topic):
    """Meldet ein großes Thema per Telegram-Alert + In-App-Notification.
    Meme-Timing zählt — man soll nicht erst beim nächsten Reinschauen davon erfahren."""
    stufe = 'KRITISCH RELEVANT' if topic.score >= 85 else 'SEHR RELEVANT'
    plats = [lbl for key, lbl in [
        ('google', 'Google'), ('instagram', 'Instagram'), ('tiktok', 'TikTok'),
        ('tv', 'TV'), ('youtube', 'YouTube'), ('wikipedia', 'Wikipedia'),
        ('reddit', 'Reddit')] if getattr(topic, 'has_' + key, False)]
    plat_str = ', '.join(plats) if plats else '—'
    try:
        _send_central_alert(
            f'📡 <b>Trend Radar: {stufe}</b>\n'
            f'<b>{topic.title}</b> (Score {topic.score})\n'
            f'{(topic.description or "")[:200]}\n'
            f'Plattformen: {plat_str}')
    except Exception:
        pass
    try:
        # fester Pfad statt url_for: der Scan läuft im Hintergrund-Thread ohne
        # Request-Kontext, dort würde url_for eine Exception werfen
        _push_notification(
            'info', f'📡 Großthema: {topic.title}',
            f'{stufe} · Score {topic.score} · {plat_str}',
            link='/trend-radar')
    except Exception:
        pass


def _tr_run_scan():
    """Kompletter Scan: Signale sammeln + clustern. Läuft in eigenem Thread."""
    if _tr_scan_status['running']:
        return
    _tr_scan_status.update({'running': True, 'error': None, 'signals': 0,
                            'topics': 0, 'started_at': datetime.utcnow().isoformat(),
                            'finished_at': None, 'step': 'Starte…'})
    try:
        with app.app_context():
            _tr_scan_status['signals'] = _tr_collect_signals()
            api_key = os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')
            if api_key:
                _tr_scan_status['step'] = 'KI führt Themen zusammen…'
                _tr_scan_status['topics'] = _tr_cluster_and_save(api_key)
            else:
                _tr_scan_status['error'] = 'Kein Anthropic-API-Key — nur Signale gesammelt.'
            set_setting('trend_last_scan', datetime.utcnow().isoformat())
            # Rohsignale älter als 7 Tage aufräumen
            TrendSignal.query.filter(
                TrendSignal.detected_at < datetime.utcnow() - timedelta(days=7)).delete()
            # Score-Snapshots älter als 14 Tage aufräumen
            TrendScoreSnapshot.query.filter(
                TrendScoreSnapshot.recorded_at < datetime.utcnow() - timedelta(days=14)).delete()
            db.session.commit()
    except Exception as e:
        _tr_scan_status['error'] = str(e)
        app.logger.error('Trend Radar Scan: %s', e)
    finally:
        _tr_scan_status['running'] = False
        _tr_scan_status['step'] = ''
        _tr_scan_status['finished_at'] = datetime.utcnow().isoformat()


def _tr_auto_scan():
    """Scheduler-Einstieg: nur wenn aktiviert und Key vorhanden."""
    with app.app_context():
        if get_setting('trend_radar_auto', '1') == '0':
            return
        if not (os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')):
            return
    _tr_run_scan()


def _tr_rel_time(dt):
    """UTC-Datetime → deutsche Relativzeit ("vor 3 Std.")."""
    if not dt:
        return ''
    secs = (datetime.utcnow() - dt).total_seconds()
    if secs < 3600:
        return f'vor {max(1, int(secs // 60))} Min.'
    if secs < 48 * 3600:
        return f'vor {int(secs // 3600)} Std.'
    return f'vor {int(secs // 86400)} Tagen'


def _tr_topic_dict(t):
    berlin = ZoneInfo('Europe/Berlin')
    try:
        sources = json.loads(t.sources_json or '[]')
    except Exception:
        sources = []
    started_local = (t.started_at.replace(tzinfo=timezone.utc).astimezone(berlin)
                     if t.started_at else None)
    # Echte Post-Links direkt aus den verknüpften Signalen ziehen (die KI-„sources"
    # oben sind nur Text und lassen die tatsächliche URL oft weg). So bekommt man
    # pro Thema die konkreten Instagram-/TikTok-/YouTube-Beiträge zum Anklicken.
    links = []
    linked = (TrendSignal.query
              .filter(TrendSignal.topic_id == t.id, TrendSignal.url.isnot(None))
              .order_by(TrendSignal.metric.desc().nullslast()).limit(12).all())
    for s in linked:
        links.append({'source': s.source, 'title': s.title[:140],
                      'detail': s.detail or '', 'url': s.url})

    # ── Trend-Richtung + Verlauf ──────────────────────────────────────────
    # Pfeil aus dem Vergleich zum letzten Scan; kleine Toleranz gegen Rauschen
    if t.prev_score is None:
        trend = 'new'
    elif t.score >= t.prev_score + 3:
        trend = 'up'
    elif t.score <= t.prev_score - 3:
        trend = 'down'
    else:
        trend = 'flat'
    peak = t.peak_score or t.score
    # „Peak vorbei": deutlich (>=8 Punkte) unter dem Höchststand und nicht mehr steigend
    past_peak = bool(peak - t.score >= 8 and trend in ('down', 'flat'))

    # Sparkline aus den letzten ~12 Snapshots (chronologisch)
    snaps = (TrendScoreSnapshot.query.filter_by(topic_id=t.id)
             .order_by(TrendScoreSnapshot.recorded_at.desc()).limit(12).all())
    scores = [s.score for s in reversed(snaps)]
    spark_points = ''
    if len(scores) >= 2:
        lo, hi = min(scores), max(scores)
        span = (hi - lo) or 1
        w, h, pad = 80.0, 22.0, 2.0
        step = w / (len(scores) - 1)
        pts = []
        for i, sc in enumerate(scores):
            x = i * step
            y = h - pad - (sc - lo) / span * (h - 2 * pad)
            pts.append(f'{x:.1f},{y:.1f}')
        spark_points = ' '.join(pts)

    return {
        'id': t.id, 'title': t.title, 'description': t.description or '',
        'score': t.score,
        'trend': trend, 'peak_score': peak, 'past_peak': past_peak,
        'prev_score': t.prev_score, 'spark_points': spark_points,
        'feedback': t.feedback or 0,
        'started_rel': _tr_rel_time(t.started_at),
        'started_fmt': started_local.strftime('%d.%m. %H:%M') if started_local else '',
        'last_seen_rel': _tr_rel_time(t.last_seen_at),
        'platforms': {
            'google': t.has_google, 'instagram': t.has_instagram,
            'tiktok': t.has_tiktok, 'tv': t.has_tv, 'youtube': t.has_youtube,
            'wikipedia': t.has_wikipedia, 'reddit': t.has_reddit,
        },
        'sources': sources,
        'post_links': links,
    }


@app.route('/trend-radar')
@login_required
def trend_radar():
    return render_template('trend_radar.html',
        active_page='trend_radar',
        platform_labels=_TR_PLATFORM_LABELS,
        ai_ready=bool(os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')),
        rapidapi_ready=bool(get_setting('rapidapi_key')),
        youtube_ready=bool(get_setting('youtube_api_key')))


@app.route('/api/trend-radar/topics')
@login_required
def trend_radar_topics():
    try:
        hours = int(request.args.get('hours', 24))
    except Exception:
        hours = 24
    hours = max(1, min(hours, 24 * 7))
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    topics = TrendTopic.query.filter(TrendTopic.archived == False,
                                     TrendTopic.last_seen_at >= cutoff)\
        .order_by(TrendTopic.score.desc(), TrendTopic.last_seen_at.desc()).all()
    sig_cutoff = datetime.utcnow() - timedelta(hours=24)
    signal_count = TrendSignal.query.filter(TrendSignal.detected_at >= sig_cutoff).count()
    last_scan = get_setting('trend_last_scan')
    last_scan_rel = ''
    if last_scan:
        try:
            last_scan_rel = _tr_rel_time(datetime.fromisoformat(last_scan))
        except Exception:
            pass
    return jsonify({'topics': [_tr_topic_dict(t) for t in topics],
                    'signal_count': signal_count,
                    'last_scan_rel': last_scan_rel,
                    'scan_running': _tr_scan_status['running']})


@app.route('/api/trend-radar/scan', methods=['POST'])
@login_required
def trend_radar_scan():
    if _tr_scan_status['running']:
        return jsonify({'ok': False, 'error': 'Scan läuft bereits.'})
    threading.Thread(target=_tr_run_scan, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/trend-radar/status')
@login_required
def trend_radar_status():
    return jsonify(_tr_scan_status)


@app.route('/api/trend-radar/topic/<int:tid>', methods=['DELETE'])
@login_required
def trend_radar_topic_delete(tid):
    t = TrendTopic.query.get_or_404(tid)
    t.archived = True
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/trend-radar/topic/<int:tid>/feedback', methods=['POST'])
@login_required
def trend_radar_feedback(tid):
    """Nutzer bewertet ob ein Thema wirklich groß war (+1) oder nicht (-1).
    Fließt beim nächsten Scan als Kalibrierung in den KI-Prompt ein."""
    t = TrendTopic.query.get_or_404(tid)
    d = request.get_json() or {}
    try:
        v = int(d.get('value', 0))
    except Exception:
        v = 0
    t.feedback = v if v in (1, -1) else None
    t.feedback_at = datetime.utcnow() if t.feedback else None
    db.session.commit()
    return jsonify({'ok': True, 'feedback': t.feedback or 0})


@app.route('/api/trend-radar/settings', methods=['GET', 'POST'])
@login_required
def trend_radar_settings():
    if request.method == 'GET':
        return jsonify({
            'youtube_key_set': bool(get_setting('youtube_api_key')),
            'auto': get_setting('trend_radar_auto', '1') != '0',
            'alert_score': int(get_setting('trend_alert_score') or 80),
            'alert_configured': bool(get_setting('alert_telegram_token')
                                     and get_setting('alert_central_chat_id')),
        })
    d = request.get_json() or {}
    if 'youtube_api_key' in d and (d['youtube_api_key'] or '').strip():
        set_setting('youtube_api_key', d['youtube_api_key'].strip())
    if 'auto' in d:
        set_setting('trend_radar_auto', '1' if d['auto'] else '0')
    if 'alert_score' in d:
        try:
            set_setting('trend_alert_score', str(max(55, min(100, int(d['alert_score'])))))
        except Exception:
            pass
    db.session.commit()   # set_setting committet NICHT selbst — sonst verschwindet der Key
    return jsonify({'ok': True, 'youtube_key_set': bool(get_setting('youtube_api_key'))})


def _tr_source_dict(s):
    return {
        'id': s.id, 'platform': s.platform, 'niche': s.niche, 'handle': s.handle,
        'active': s.active, 'scan_interval_hours': s.scan_interval_hours,
        'avg_likes': s.avg_likes,
        'avg_updated_rel': _tr_rel_time(s.avg_updated_at) if s.avg_updated_at else '',
        'last_scanned_rel': _tr_rel_time(s.last_scanned_at) if s.last_scanned_at else '',
    }


@app.route('/api/trend-radar/sources', methods=['GET', 'POST'])
@login_required
def trend_radar_sources():
    """Quellen-Verwaltung: mehrere Nischen (News/Memes/eigene) statt einer
    einzelnen Komma-Liste — ersetzt das frühere Setting trend_ig_accounts."""
    if request.method == 'GET':
        sources = TrendSource.query.order_by(
            TrendSource.niche, TrendSource.platform, TrendSource.handle).all()
        return jsonify({'sources': [_tr_source_dict(s) for s in sources]})
    d = request.get_json() or {}
    handle = (d.get('handle') or '').strip().lstrip('@')
    platform = (d.get('platform') or '').strip().lower()
    if not handle or platform not in ('instagram', 'tiktok'):
        return jsonify({'ok': False, 'error': 'handle und platform (instagram/tiktok) erforderlich'}), 400
    niche = (d.get('niche') or 'News').strip() or 'News'
    try:
        interval = max(1, int(d.get('scan_interval_hours') or (3 if niche.lower() == 'news' else 12)))
    except Exception:
        interval = 12
    src = TrendSource(platform=platform, niche=niche, handle=handle,
                      scan_interval_hours=interval, active=True)
    db.session.add(src)
    db.session.commit()
    return jsonify({'ok': True, 'source': _tr_source_dict(src)})


@app.route('/api/trend-radar/sources/<int:sid>', methods=['PATCH', 'DELETE'])
@login_required
def trend_radar_source_edit(sid):
    src = TrendSource.query.get_or_404(sid)
    if request.method == 'DELETE':
        db.session.delete(src)
        db.session.commit()
        return jsonify({'ok': True})
    d = request.get_json() or {}
    if 'handle' in d and (d['handle'] or '').strip():
        src.handle = d['handle'].strip().lstrip('@')
    if 'niche' in d and (d['niche'] or '').strip():
        src.niche = d['niche'].strip()
    if 'platform' in d and d['platform'] in ('instagram', 'tiktok'):
        src.platform = d['platform']
    if 'active' in d:
        src.active = bool(d['active'])
    if 'scan_interval_hours' in d:
        try:
            src.scan_interval_hours = max(1, int(d['scan_interval_hours']))
        except Exception:
            pass
    db.session.commit()
    return jsonify({'ok': True, 'source': _tr_source_dict(src)})


# ══════════════════════════════════════════════════════════════════════════════
# PRODUCT ALERT FACTORY (Content Studio) — Rückrufe & Produktwarnungen
# Genauigkeit vor Geschwindigkeit: nichts wird erfunden, fehlende Angaben
# bleiben NULL. Struktur/Konventionen bewusst eng an der Missing-Children-
# Factory (mcf_*) angelehnt — gleiche SSRF-Absicherung, gleicher Dedup-/
# Status-/Telegram-Workflow.
# ══════════════════════════════════════════════════════════════════════════════

PWF_CATEGORIES = ['Produktrückruf', 'Produktwarnung', 'Gesundheitswarnung',
                  'Testbericht', 'Verkaufsstopp', 'Entwarnung']
PWF_RISK_LEVELS = ['GERING', 'MITTEL', 'HOCH', 'KRITISCH']

_PWF_CATEGORY_BANNER = {
    'Produktrückruf': 'RÜCKRUF', 'Produktwarnung': 'WARNUNG',
    'Gesundheitswarnung': 'GESUNDHEITSWARNUNG', 'Testbericht': 'TESTBERICHT',
    'Verkaufsstopp': 'VERKAUFSSTOPP', 'Entwarnung': 'ENTWARNUNG',
}
_PWF_RISK_COLOR = {
    'GERING': (100, 116, 139), 'MITTEL': (217, 119, 6),
    'HOCH': (220, 38, 38), 'KRITISCH': (127, 29, 29),
}
_PWF_RISK_COLOR_DEFAULT = (71, 85, 105)
_PWF_ENTWARNUNG_COLOR = (21, 128, 61)

_PWF_JSON_SCHEMA = (
    '{"is_case": bool (handelt es sich wirklich um eine Produktwarnung/einen Rückruf/'
    'einen Testbericht o.ä.? false bei irrelevantem Text), '
    '"kategorie": "Produktrückruf"|"Produktwarnung"|"Gesundheitswarnung"|"Testbericht"|'
    '"Verkaufsstopp"|"Entwarnung"|null, '
    '"titel": str|null, "kurztitel": str|null (max 60 Zeichen), '
    '"produktname": str|null, "marke": str|null, "hersteller": str|null, "produktgruppe": str|null, '
    '"produktbild_vorhanden": bool, "produktbild_url": str|null, '
    '"charge": str|null, "losnummer": str|null, "ean": str|null, '
    '"mhd": str|null, "verbrauchsdatum": str|null, "verpackungsgroesse": str|null, '
    '"verkaufsstellen": str|null, "online_shop": str|null, "filiale": str|null, '
    '"betroffene_regionen": str|null, "betroffene_laender": str|null, "verkaufszeitraum": str|null, '
    '"rueckrufgrund": str|null, "gefahrstoff": str|null, '
    '"risiko": "GERING"|"MITTEL"|"HOCH"|"KRITISCH"|null, "gefaehrdete_gruppen": str|null, '
    '"h_nicht_verwenden": bool, "h_nicht_essen": bool, "h_nicht_trinken": bool, '
    '"h_nicht_einnehmen": bool, "h_zurueckgeben": bool, "h_entsorgen": bool, '
    '"h_erstattung": bool, "h_kassenbon_erforderlich": bool, '
    '"relevanz": int|null (0-100, wie stark betrifft es die breite Öffentlichkeit), '
    '"dringlichkeit": int|null (0-100, wie zeitkritisch), '
    '"de_relevant": bool, "eu_relevant": bool, "weltweit_relevant": bool, '
    '"grosse_marke": bool, "lebensmittel": bool, "kinderprodukt": bool, '
    '"originalquelle": str|null, "quelle_datum": "YYYY-MM-DD"|null, '
    '"ig_titel": str|null, "ig_untertitel": str|null, "ig_kurzbeschreibung": str|null, '
    '"caption": str|null, "ig_alt_text": str|null, "story_text": str|null}'
)

_PWF_EXTRACT_SYSTEM = (
    'Du bist die zentrale KI einer automatisierten Product Alert Factory.\n\n'
    'Deine wichtigste Regel lautet: Genauigkeit vor Geschwindigkeit.\n'
    '- Veröffentliche niemals Informationen, die nicht eindeutig im Text stehen.\n'
    '- Erfinde niemals Daten, auch keine plausible Vermutung.\n'
    '- Fehlt eine Information, setze den Wert auf null — niemals raten oder mit Platzhalter füllen.\n'
    '- Bevorzuge die Originalquelle, falls im Text erkennbar (z.B. Behörde, Hersteller-Statement).\n'
    '- Vergleiche keine mehreren Quellen (das übernimmt eine andere Instanz) — arbeite nur mit dem '
    'gegebenen Text.\n\n'
    'Bestimme Kategorie und Risikostufe aus dem Text. Relevanz (0-100) = wie stark betrifft es die '
    'breite Öffentlichkeit (Reichweite/Bekanntheit der Marke, Anzahl Verkaufsstellen). Dringlichkeit '
    '(0-100) = wie zeitkritisch (Gesundheitsrisiko, knappes Ablaufdatum).\n\n'
    'Erstelle zusätzlich fertige Instagram-Texte (ig_titel, ig_untertitel, ig_kurzbeschreibung, '
    'caption, ig_alt_text, story_text). Die Caption enthält: kurze Zusammenfassung, warum gewarnt '
    'wird, welche Produkte betroffen sind, was Verbraucher tun sollen, Quelle. Sachlich und neutral, '
    'keine Übertreibung, keine Panikmache, kein Clickbait — NUR aus den extrahierten Fakten, nichts '
    'ergänzen.\n\n'
    'Antworte NUR mit einem JSON-Objekt, keine Erklärung davor oder danach, exakt in diesem Schema:\n'
    + _PWF_JSON_SCHEMA
)


def _pwf_dedup_key(produktname, marke, charge, titel):
    parts = [(produktname or '').strip().lower(), (marke or '').strip().lower(),
             (charge or '').strip().lower()]
    key = '|'.join(p for p in parts if p)
    if not key:
        key = (titel or '').strip().lower()
    return key[:400] or None


def _pwf_find_duplicate(dedup_key, exclude_id=None):
    if not dedup_key:
        return None
    q = ProductAlert.query.filter_by(dedup_key=dedup_key)
    if exclude_id:
        q = q.filter(ProductAlert.id != exclude_id)
    return q.first()


def _pwf_find_related_recall(produktname, marke, exclude_id=None):
    """Sucht zu einer neuen Entwarnung den passenden ursprünglichen Rückruf
    (gleiches Produkt/gleiche Marke, nicht selbst schon eine Entwarnung).
    Nur für die Verknüpfung — ändert NIE automatisch den Status des Fundes
    (gleiches Vorsicht-Prinzip wie update_detected bei der Missing-Children-
    Factory: der Mensch entscheidet, ob wirklich derselbe Fall gemeint ist)."""
    pn = (produktname or '').strip().lower()
    mk = (marke or '').strip().lower()
    if not pn and not mk:
        return None
    # WICHTIG: "!= 'Entwarnung'" allein würde in SQL Zeilen mit kategorie IS NULL
    # per Drei-Werte-Logik stillschweigend ausschließen (NULL != x ist NULL,
    # nicht TRUE) — deshalb explizit is_(None) mit einschließen.
    q = ProductAlert.query.filter(
        db.or_(ProductAlert.status != 'archiviert', ProductAlert.status.is_(None)),
        db.or_(ProductAlert.kategorie != 'Entwarnung', ProductAlert.kategorie.is_(None)))
    if exclude_id:
        q = q.filter(ProductAlert.id != exclude_id)
    for c in q.all():
        c_pn = (c.produktname or '').strip().lower()
        c_mk = (c.marke or '').strip().lower()
        if pn and c_pn and c_pn == pn:
            return c
        if mk and pn and c_mk == mk and c_pn[:15] == pn[:15]:
            return c
    return None


def _pwf_latest_date_in_text(text):
    """Findet alle dd.mm.yyyy/dd.mm.yy-Daten in einem Freitext (z.B. MHD-Feld
    'MHD 20.05.26 – 27.05.26') und gibt das späteste zurück. None wenn nichts
    sicher parsebar ist — es wird NIE geraten."""
    if not text:
        return None
    import re as _re
    dates = []
    for d, m, y in _re.findall(r'(\d{1,2})\.(\d{1,2})\.(\d{2,4})', text):
        try:
            year = int(y) if len(y) == 4 else 2000 + int(y)
            dates.append(datetime(year, int(m), int(d)).date())
        except Exception:
            continue
    return max(dates) if dates else None


def _pwf_auto_archive_scheduled():
    """Scheduler-Einstieg: eigener app_context für den Hintergrund-Thread."""
    with app.app_context():
        _pwf_auto_archive_expired()


def _pwf_auto_archive_expired():
    """Archiviert vergessene Entwürfe automatisch, deren MHD seit >30 Tagen
    abgelaufen ist — nur Entwürfe (nie veröffentlichte/schon archivierte
    Posts), nur wenn ein Datum sicher aus dem MHD-Freitext lesbar ist."""
    cutoff = datetime.utcnow().date() - timedelta(days=30)
    archived = 0
    for a in ProductAlert.query.filter_by(status='entwurf').all():
        latest = _pwf_latest_date_in_text(a.mhd)
        if latest and latest < cutoff:
            a.status = 'archiviert'
            archived += 1
    if archived:
        db.session.commit()
    return archived


def _pwf_calibration_block():
    """Kurzer Kalibrierungs-Hinweis aus dem Nutzer-Feedback der letzten 60 Tage
    — analog zur Trend-Radar-Feedback-Schleife, hier pro Extraktion statt pro
    Batch-Scan eingespeist, da PWF jede Meldung einzeln extrahiert."""
    cutoff = datetime.utcnow() - timedelta(days=60)
    good = ProductAlert.query.filter(ProductAlert.feedback == 1, ProductAlert.feedback_at >= cutoff)\
        .order_by(ProductAlert.feedback_at.desc()).limit(8).all()
    bad = ProductAlert.query.filter(ProductAlert.feedback == -1, ProductAlert.feedback_at >= cutoff)\
        .order_by(ProductAlert.feedback_at.desc()).limit(8).all()
    if not good and not bad:
        return ''
    lines = ['\n\n--- NUTZER-FEEDBACK ZUR KALIBRIERUNG (aus echter Rückmeldung, letzte 60 Tage) ---']
    if good:
        lines.append('Als korrekt eingeschätzt bestätigt (Kategorie/Risiko/Relevanz passten):')
        for a in good:
            lines.append(f'- "{a.display_name()}": Kategorie={a.kategorie}, Risiko={a.risiko}, Relevanz={a.relevanz}')
    if bad:
        lines.append('Als FALSCH eingeschätzt markiert (Kategorie/Risiko/Relevanz stimmten nicht):')
        for a in bad:
            lines.append(f'- "{a.display_name()}": eingeschätzt als Kategorie={a.kategorie}, Risiko={a.risiko}, Relevanz={a.relevanz}')
    lines.append('Berücksichtige dieses Muster bei neuen Einschätzungen — ändert NICHTS an der Grundregel: '
                 'nur aus dem gegebenen Text ableiten, nie erfinden.')
    return '\n'.join(lines)


def _pwf_extract_from_text(text, api_key):
    """Extrahiert Produktwarnungs-Felder NUR aus dem gegebenen Text (erfindet
    nichts). Gibt dict|None."""
    if not api_key or not (text or '').strip():
        return None
    try:
        import anthropic
        import re as _re
        client = anthropic.Anthropic(api_key=api_key)
        model = get_setting('analysis_model') or 'claude-sonnet-4-6'
        resp = client.messages.create(
            model=model, max_tokens=1800, system=_PWF_EXTRACT_SYSTEM + _pwf_calibration_block(),
            messages=[{'role': 'user', 'content': str(text)[:8000]}])
        _log_ai('pwf_extract_text', resp)
        raw = resp.content[0].text.strip()
        m = _re.search(r'\{.*\}', raw, _re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        app.logger.warning('PWF Extract-Text: %s', e)
        return None


def _pwf_extract_from_image_bytes(img_bytes, mime, api_key):
    """Vision-Extraktion aus einem fotografierten/gescannten Rückruf-Hinweis,
    Etikett oder Presseausschnitt — gleiche NULL-bei-Unklarheit-Disziplin."""
    if not api_key or not img_bytes:
        return None
    try:
        import anthropic
        import base64 as _b64
        import re as _re
        img_b64 = _b64.standard_b64encode(img_bytes).decode()
        client = anthropic.Anthropic(api_key=api_key)
        model = get_setting('vision_model') or 'claude-sonnet-4-6'
        resp = client.messages.create(
            model=model, max_tokens=1800,
            system=_PWF_EXTRACT_SYSTEM + '\n\nDas Material ist ein FOTO/SCAN, kein Fließtext — '
                   'lies alles Erkennbare (Etikett, Aushang, Screenshot einer Meldung) sorgfältig aus.'
                   + _pwf_calibration_block(),
            messages=[{'role': 'user', 'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': img_b64}},
                {'type': 'text', 'text': 'Lies dieses Material aus und antworte nur mit dem JSON-Objekt.'}
            ]}])
        _log_ai('pwf_extract_image', resp)
        raw = resp.content[0].text.strip()
        m = _re.search(r'\{.*\}', raw, _re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        app.logger.warning('PWF Extract-Image: %s', e)
        return None


def _pwf_save_photo(fobj):
    """Speichert ein hochgeladenes Produktfoto lokal + legt MediaItem an."""
    try:
        import uuid as _uuid
        ext = os.path.splitext(fobj.filename)[1].lower() or '.jpg'
        name = f'pwfphoto_{_uuid.uuid4().hex}{ext}'
        fobj.save(os.path.join(app.config['UPLOAD_FOLDER'], name))
        w = h = None
        try:
            from PIL import Image as _I
            w, h = _I.open(os.path.join(app.config['UPLOAD_FOLDER'], name)).size
        except Exception:
            pass
        m = MediaItem(filename=name, original_filename=fobj.filename, url=f'/media/file/{name}',
                      file_type='image', storage_source='local', width=w, height=h)
        db.session.add(m)
        db.session.flush()
        return m.id
    except Exception as e:
        app.logger.warning('PWF Foto-Upload: %s', e)
        return None


def _pwf_save_photo_bytes(raw_bytes, ext='.jpg', orig_filename=None):
    try:
        import uuid as _uuid
        name = f'pwfphoto_{_uuid.uuid4().hex}{ext}'
        path = os.path.join(app.config['UPLOAD_FOLDER'], name)
        with open(path, 'wb') as fh:
            fh.write(raw_bytes)
        w = h = None
        try:
            from PIL import Image as _I
            w, h = _I.open(path).size
        except Exception:
            pass
        m = MediaItem(filename=name, original_filename=orig_filename or name, url=f'/media/file/{name}',
                      file_type='image', storage_source='local', width=w, height=h)
        db.session.add(m)
        db.session.commit()
        return m.id
    except Exception as e:
        app.logger.warning('PWF Foto-Bytes-Upload: %s', e)
        return None


def _pwf_maybe_fetch_photo(url, orig_filename=None):
    """Best-effort: eine per KI/og:image gefundene Produktbild-URL laden und als
    MediaItem speichern. SSRF-geprüft, still fehlschlagend — kein Foto ist kein
    Fehler, es bleibt einfach None."""
    if not url or not _mcf_is_safe_url(url):
        return None
    try:
        r = _requests.get(url, timeout=8, headers={'User-Agent': 'ContentOS/1.0'}, stream=True)
        r.raise_for_status()
        if not _mcf_is_safe_url(r.url):
            return None
        ctype = (r.headers.get('Content-Type') or '').lower()
        if not ctype.startswith('image/'):
            return None
        img_bytes = r.raw.read(15 * 1024 * 1024, decode_content=True)
        ext = {'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif'}.get(ctype, '.jpg')
        return _pwf_save_photo_bytes(img_bytes, ext, orig_filename or os.path.basename(url))
    except Exception as e:
        app.logger.warning('PWF Bild-Fetch: %s', e)
        return None


def _pwf_score_image_match(headline, image_bytes, mime='image/jpeg'):
    """Bewertet per Haiku-Vision, wie gut ein Produktfoto inhaltlich zur Meldung
    passt (0-100) — Muster von CityBots Bild-Match-Score übernommen.
    0-20 komplett falsches Motiv, 21-50 generisches Symbolbild ohne Bezug,
    51-80 thematisch passend aber nicht exakt das Produkt, 81-100 exaktes Produkt.
    BULLETPROOF: fehlender Key/Bild/Headline oder JEDER Fehler → None, blockiert
    nie den Post-Flow."""
    api_key = get_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY')
    if not api_key or not image_bytes or not headline:
        return None
    try:
        import anthropic, base64 as _b64, re as _re
        img_b64 = _b64.standard_b64encode(image_bytes).decode()
        client = anthropic.Anthropic(api_key=api_key)
        content = [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': img_b64}},
            {'type': 'text', 'text': (
                f'Produkt/Meldung: "{headline}"\n\n'
                'Wie gut passt dieses Bild inhaltlich (Produkt, Verpackung, Motiv)? '
                'Bewerte auf einer Skala von 0-100:\n'
                '0-20 = komplett falsches Motiv (anderes Produkt/Thema)\n'
                '21-50 = generisches Symbolbild ohne konkreten Bezug\n'
                '51-80 = thematisch passend, aber nicht exakt das beschriebene Produkt\n'
                '81-100 = exakt das beschriebene Produkt erkennbar\n'
                'Antworte NUR mit der Zahl, sonst nichts.'
            )},
        ]
        resp = client.messages.create(model='claude-haiku-4-5', max_tokens=6,
                                      messages=[{'role': 'user', 'content': content}])
        _log_ai('pwf_image_match', resp)
        m = _re.search(r'\d+', resp.content[0].text)
        return max(0, min(100, int(m.group()))) if m else None
    except Exception as e:
        app.logger.debug('PWF Bild-Match-Score: %s', e)
        return None


def _pwf_score_image_match_async(alert_id):
    """Läuft im Hintergrund-Thread, blockiert nie den aufrufenden Flow (Create/
    Extraktion/Auto-Recherche warten nicht auf das Vision-Ergebnis)."""
    def _run():
        with app.app_context():
            a = ProductAlert.query.get(alert_id)
            if not a or not a.foto_media_id:
                return
            media = MediaItem.query.get(a.foto_media_id)
            photo_bytes = _mcf_load_image_bytes(media) if media else None
            headline = a.produktname or a.titel or a.kurztitel
            score = _pwf_score_image_match(headline, photo_bytes)
            if score is not None:
                a.image_match_score = score
                db.session.commit()
    threading.Thread(target=_run, daemon=True).start()


def _pwf_draw_placeholder(bw, bh):
    """Heller Platzhalter-Kasten mit Hinweistext, wenn (noch) kein Produktfoto vorliegt."""
    from PIL import Image as _Image, ImageDraw as _ImageDraw
    bw, bh = int(bw), int(bh)
    box = _Image.new('RGB', (bw, bh), (238, 240, 244))
    d = _ImageDraw.Draw(box)
    f = _mcf_font(24, bold=False)
    txt = 'Kein Produktbild verfügbar'
    tw = d.textlength(txt, font=f)
    d.text(((bw - tw) / 2, bh / 2 - 12), txt, font=f, fill=(150, 156, 168))
    return box


def _render_product_alert_image(alert):
    """1080×1350 Instagram-Karte: farbiger Risiko-Banner oben, Produktfoto,
    Titel + Fakten, Handlungsempfehlung-Leiste unten. Rendert nur vorhandene
    Felder. Speichert flach im Upload-Ordner, gibt Dateinamen zurück."""
    from PIL import Image, ImageDraw, ImageOps
    W, H = 1080, 1350
    WHITE = (255, 255, 255); INK = (30, 30, 32); GRAY = (110, 110, 110)
    img = Image.new('RGB', (W, H), WHITE)
    d = ImageDraw.Draw(img)

    # ── Banner ──────────────────────────────────────────────────
    is_entwarnung = alert.kategorie == 'Entwarnung'
    banner_color = (_PWF_ENTWARNUNG_COLOR if is_entwarnung
                    else _PWF_RISK_COLOR.get(alert.risiko, _PWF_RISK_COLOR_DEFAULT))
    BANNER_H = 190
    d.rectangle([0, 0, W, BANNER_H], fill=banner_color)
    if alert.kategorie:
        f_cat = _mcf_font(22, bold=True)
        cat = alert.kategorie.upper()
        cw = d.textlength(cat, font=f_cat)
        d.text(((W - cw) / 2, 32), cat, font=f_cat, fill=(255, 255, 255))
    f_big = _mcf_font(64, bold=True)
    big = _PWF_CATEGORY_BANNER.get(alert.kategorie, 'PRODUKTWARNUNG')
    bw_ = d.textlength(big, font=f_big)
    d.text(((W - bw_) / 2, 78), big, font=f_big, fill=WHITE)
    if alert.risiko and not is_entwarnung:
        f_pill = _mcf_font(20, bold=True)
        pill_txt = f'RISIKO: {alert.risiko}'
        pw = d.textlength(pill_txt, font=f_pill) + 28
        px, py, ph = (W - pw) / 2, 152, 30
        d.rounded_rectangle([px, py, px + pw, py + ph], radius=ph / 2, fill=WHITE)
        tw2 = d.textlength(pill_txt, font=f_pill)
        d.text((px + (pw - tw2) / 2, py + 5), pill_txt, font=f_pill, fill=banner_color)

    # ── Produktfoto ─────────────────────────────────────────────
    PH_SIZE = 620
    px0 = (W - PH_SIZE) // 2
    py0 = BANNER_H + 40
    media = MediaItem.query.get(alert.foto_media_id) if alert.foto_media_id else None
    photo_bytes = _mcf_load_image_bytes(media) if media else None
    if photo_bytes:
        try:
            import io as _io
            photo = ImageOps.exif_transpose(Image.open(_io.BytesIO(photo_bytes)).convert('RGB'))
            photo = ImageOps.fit(photo, (PH_SIZE, PH_SIZE), method=Image.LANCZOS)
        except Exception:
            photo = _pwf_draw_placeholder(PH_SIZE, PH_SIZE)
    else:
        photo = _pwf_draw_placeholder(PH_SIZE, PH_SIZE)
    img.paste(photo, (px0, py0))
    d.rectangle([px0, py0, px0 + PH_SIZE, py0 + PH_SIZE], outline=(210, 210, 214), width=2)

    y = py0 + PH_SIZE + 34

    # ── Titel / Produktname ─────────────────────────────────────
    headline = alert.produktname or alert.titel or alert.kurztitel or ''
    if headline:
        f_h = _mcf_font(40, bold=True)
        for line in _mcf_wrap(d, headline, f_h, W - 120)[:2]:
            lw = d.textlength(line, font=f_h)
            d.text(((W - lw) / 2, y), line, font=f_h, fill=INK)
            y += 48

    sub_parts = [p for p in (alert.marke, alert.hersteller) if p]
    if sub_parts:
        f_sub = _mcf_font(24, bold=False)
        sub = ' · '.join(sub_parts)
        sw = d.textlength(sub, font=f_sub)
        d.text(((W - sw) / 2, y + 4), sub, font=f_sub, fill=GRAY)
        y += 40

    y += 14
    d.line([(80, y), (W - 80, y)], fill=(226, 228, 232), width=2)
    y += 22

    # ── Fakten-Zeilen (nur vorhandene, max. bis Platz reicht) ───
    facts = []
    if alert.rueckrufgrund:
        facts.append(('Grund', alert.rueckrufgrund))
    if alert.charge or alert.losnummer:
        facts.append(('Charge/Los', ' · '.join(p for p in (alert.charge, alert.losnummer) if p)))
    if alert.mhd:
        facts.append(('MHD', alert.mhd))
    if alert.betroffene_regionen:
        facts.append(('Regionen', alert.betroffene_regionen))
    if alert.verkaufszeitraum:
        facts.append(('Zeitraum', alert.verkaufszeitraum))

    f_lbl = _mcf_font(22, bold=True)
    f_val = _mcf_font(22, bold=False)
    max_y = H - 150
    for label, value in facts:
        if y > max_y - 30:
            break
        d.text((80, y), f'{label}:', font=f_lbl, fill=INK)
        lbl_w = d.textlength(f'{label}: ', font=f_lbl)
        val_lines = _mcf_wrap(d, value, f_val, W - 160 - lbl_w)
        d.text((80 + lbl_w, y), val_lines[0], font=f_val, fill=(70, 70, 74))
        y += 32
        for extra in val_lines[1:2]:
            if y > max_y:
                break
            d.text((80, y), extra, font=f_val, fill=(70, 70, 74))
            y += 32

    # ── Handlungsempfehlung-Leiste ───────────────────────────────
    actions = []
    if alert.h_nicht_verwenden: actions.append('NICHT VERWENDEN')
    if alert.h_nicht_essen: actions.append('NICHT ESSEN')
    if alert.h_nicht_trinken: actions.append('NICHT TRINKEN')
    if alert.h_nicht_einnehmen: actions.append('NICHT EINNEHMEN')
    if alert.h_zurueckgeben: actions.append('ZURÜCKGEBEN')
    if alert.h_entsorgen: actions.append('ENTSORGEN')
    if alert.h_erstattung: actions.append('ERSTATTUNG MÖGLICH')
    if alert.h_kassenbon_erforderlich: actions.append('KASSENBON NÖTIG')

    STRIP_H = 96
    d.rectangle([0, H - STRIP_H, W, H], fill=(245, 246, 248))
    if actions:
        f_act = _mcf_font(24, bold=True)
        act_txt = '  •  '.join(actions)
        lines = _mcf_wrap(d, act_txt, f_act, W - 100)[:2]
        ay = H - STRIP_H + (14 if len(lines) > 1 else 32)
        for line in lines:
            lw = d.textlength(line, font=f_act)
            d.text(((W - lw) / 2, ay), line, font=f_act, fill=INK)
            ay += 30
    if alert.originalquelle:
        f_src = _mcf_font(17, bold=False)
        src = f'Quelle: {alert.originalquelle}'
        sw = d.textlength(src, font=f_src)
        d.text(((W - sw) / 2, H - 24), src, font=f_src, fill=GRAY)

    import uuid as _uuid
    fname = f'pwfcard_{alert.id}_{_uuid.uuid4().hex[:8]}.jpg'
    img.save(os.path.join(app.config['UPLOAD_FOLDER'], fname), 'JPEG', quality=92)
    return fname


def _pwf_generate_caption(alert):
    """Caption NUR aus den aktuellen (ggf. vom Menschen editierten) Feldern —
    erfindet nichts. KI optional, mit faktentreuem Fallback ohne KI."""
    facts = []
    if alert.kategorie:
        facts.append(f'Kategorie: {alert.kategorie}')
    if alert.produktname:
        facts.append(f'Produkt: {alert.produktname}')
    if alert.marke:
        facts.append(f'Marke: {alert.marke}')
    if alert.hersteller:
        facts.append(f'Hersteller: {alert.hersteller}')
    if alert.charge or alert.losnummer:
        facts.append('Charge/Los: ' + ' · '.join(p for p in (alert.charge, alert.losnummer) if p))
    if alert.mhd:
        facts.append(f'MHD: {alert.mhd}')
    if alert.verkaufsstellen:
        facts.append(f'Verkaufsstellen: {alert.verkaufsstellen}')
    if alert.betroffene_regionen:
        facts.append(f'Betroffene Regionen: {alert.betroffene_regionen}')
    if alert.rueckrufgrund:
        facts.append(f'Grund: {alert.rueckrufgrund}')
    if alert.gefahrstoff:
        facts.append(f'Gefahrstoff: {alert.gefahrstoff}')
    if alert.risiko:
        facts.append(f'Risikostufe: {alert.risiko}')
    if alert.gefaehrdete_gruppen:
        facts.append(f'Besonders gefährdet: {alert.gefaehrdete_gruppen}')
    actions = []
    if alert.h_nicht_verwenden: actions.append('nicht verwenden')
    if alert.h_nicht_essen: actions.append('nicht essen')
    if alert.h_nicht_trinken: actions.append('nicht trinken')
    if alert.h_nicht_einnehmen: actions.append('nicht einnehmen')
    if alert.h_zurueckgeben: actions.append('zurückgeben')
    if alert.h_entsorgen: actions.append('entsorgen')
    if alert.h_erstattung: actions.append('Erstattung möglich')
    if alert.h_kassenbon_erforderlich: actions.append('Kassenbon erforderlich')
    if actions:
        facts.append('Handlungsempfehlung: ' + ', '.join(actions))
    src = []
    if alert.originalquelle:
        src.append(f'Quelle: {alert.originalquelle}')
    if alert.quelle_url:
        src.append(alert.quelle_url)

    def _fallback():
        head = _PWF_CATEGORY_BANNER.get(alert.kategorie, 'PRODUKTWARNUNG')
        return '\n'.join([f'⚠️ {head}', ''] + facts + ([''] + src if src else []))

    api_key = get_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY')
    if not api_key or not facts:
        return _fallback()
    system = (
        'Du erstellst Instagram-Captions für Produktwarnungen/Rückrufe. ABSOLUT ZWINGEND: '
        'Verwende AUSSCHLIESSLICH die angegebenen Fakten. Erfinde, ergänze oder vermute NIEMALS '
        'etwas. Fehlende Angaben lässt du komplett weg. Ton: sachlich, neutral, klar strukturiert. '
        'Baue die Quelle unverändert ein. Keine Übertreibung, keine Panikmache, kein Clickbait.'
    )
    user = f"Bekannte Fakten (NUR diese verwenden):\n{chr(10).join(facts)}"
    if src:
        user += f"\n\nQuelle (unverändert einbauen):\n{chr(10).join(src)}"
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        model = get_setting('caption_model') or 'claude-haiku-4-5'
        resp = client.messages.create(model=model, max_tokens=700, system=system,
                                       messages=[{'role': 'user', 'content': user}])
        _log_ai('pwf_caption', resp)
        return resp.content[0].text.strip()
    except Exception as e:
        app.logger.warning('PWF Caption-Fehler: %s', e)
        return _fallback()


def _pwf_target_account():
    acc_id = get_setting('pwf_target_account_id')
    if not acc_id:
        return None
    return Account.query.get(int(acc_id))


def _pwf_maybe_fire_urgent_alert(a):
    """Sofortiger Telegram-Alert bei KRITISCH-Risiko oder sehr hoher Dringlichkeit
    — hier zählen Minuten, im Gegensatz zu normalen Entwürfen. Einmalig pro
    Alert (alerted-Flag), analog zum Trend-Radar-Alert."""
    if a.alerted:
        return
    urgent = (a.risiko == 'KRITISCH') or (a.dringlichkeit is not None and a.dringlichkeit >= 85)
    if not urgent:
        return
    try:
        _send_central_alert(
            f'🚨 <b>Product Alert Factory: {a.risiko or "SEHR DRINGEND"}</b>\n'
            f'<b>{a.display_name()}</b>\n'
            f'{(a.rueckrufgrund or "")[:200]}\n'
            f'→ /content-studio/produktwarnungen/{a.id}')
    except Exception:
        pass
    try:
        _push_notification(
            'info', f'🚨 {a.risiko or "Dringend"}: {a.display_name()}',
            (a.rueckrufgrund or '')[:150],
            link=f'/content-studio/produktwarnungen/{a.id}')
    except Exception:
        pass
    a.alerted = True


def _pwf_alert_dict(a):
    acc = Account.query.get(a.account_id) if a.account_id else None
    media = MediaItem.query.get(a.foto_media_id) if a.foto_media_id else None
    return {
        'id': a.id, 'name': a.display_name(),
        'kategorie': a.kategorie, 'titel': a.titel, 'kurztitel': a.kurztitel,
        'produktname': a.produktname, 'marke': a.marke, 'hersteller': a.hersteller,
        'produktgruppe': a.produktgruppe, 'produktbild_url': a.produktbild_url,
        'charge': a.charge, 'losnummer': a.losnummer, 'ean': a.ean,
        'mhd': a.mhd, 'verbrauchsdatum': a.verbrauchsdatum, 'verpackungsgroesse': a.verpackungsgroesse,
        'verkaufsstellen': a.verkaufsstellen, 'online_shop': a.online_shop, 'filiale': a.filiale,
        'betroffene_regionen': a.betroffene_regionen, 'betroffene_laender': a.betroffene_laender,
        'verkaufszeitraum': a.verkaufszeitraum,
        'rueckrufgrund': a.rueckrufgrund, 'gefahrstoff': a.gefahrstoff,
        'risiko': a.risiko, 'gefaehrdete_gruppen': a.gefaehrdete_gruppen,
        'h_nicht_verwenden': a.h_nicht_verwenden, 'h_nicht_essen': a.h_nicht_essen,
        'h_nicht_trinken': a.h_nicht_trinken, 'h_nicht_einnehmen': a.h_nicht_einnehmen,
        'h_zurueckgeben': a.h_zurueckgeben, 'h_entsorgen': a.h_entsorgen,
        'h_erstattung': a.h_erstattung, 'h_kassenbon_erforderlich': a.h_kassenbon_erforderlich,
        'relevanz': a.relevanz, 'dringlichkeit': a.dringlichkeit,
        'de_relevant': a.de_relevant, 'eu_relevant': a.eu_relevant, 'weltweit_relevant': a.weltweit_relevant,
        'grosse_marke': a.grosse_marke, 'lebensmittel': a.lebensmittel, 'kinderprodukt': a.kinderprodukt,
        'ig_titel': a.ig_titel, 'ig_untertitel': a.ig_untertitel,
        'ig_kurzbeschreibung': a.ig_kurzbeschreibung, 'caption': a.caption,
        'ig_alt_text': a.ig_alt_text, 'story_text': a.story_text,
        'image': f'/media/file/{a.generated_image_path}' if a.generated_image_path else None,
        'photo_url': media.url if media else None,
        'originalquelle': a.originalquelle, 'quelle_url': a.quelle_url,
        'quelle_datum': a.quelle_datum.isoformat() if a.quelle_datum else None,
        'account': acc.name if acc else None, 'account_id': a.account_id,
        'status': a.status, 'origin': a.origin,
        'telegram_sent': bool(a.telegram_sent_at),
        'published_at': a.published_at.strftime('%d.%m.%Y %H:%M') if a.published_at else None,
        'ig_post_ref': a.ig_post_ref,
        'created_at': a.created_at.strftime('%d.%m.%Y') if a.created_at else None,
        'image_match_score': a.image_match_score,
        'feedback': a.feedback or 0,
        'related_alert': _pwf_related_dict(a.related_alert_id) if a.related_alert_id else None,
        'linked_entwarnung': _pwf_linked_entwarnung_dict(a.id),
    }


def _pwf_related_dict(related_id):
    r = ProductAlert.query.get(related_id) if related_id else None
    return {'id': r.id, 'name': r.display_name()} if r else None


def _pwf_linked_entwarnung_dict(alert_id):
    """Reverse-Lookup: gibt es eine Entwarnung, die auf DIESEN Alert verweist?
    Nur informativ — ändert nie automatisch den Status (Vorsicht-Prinzip wie
    bei der Missing-Children-Factory: der Mensch entscheidet)."""
    e = ProductAlert.query.filter_by(related_alert_id=alert_id).first()
    return {'id': e.id, 'name': e.display_name()} if e else None


def _pwf_apply_fields(alert, d, prefix_new=False):
    """Setzt alle bekannten Felder aus dict d auf alert — gemeinsame Logik für
    Create (aus KI-Extraktion/Formular) und Update (Bearbeiten-Formular)."""
    str_fields = ('kategorie', 'titel', 'kurztitel', 'produktname', 'marke', 'hersteller',
                 'produktgruppe', 'produktbild_url', 'charge', 'losnummer', 'ean', 'mhd',
                 'verbrauchsdatum', 'verpackungsgroesse', 'verkaufsstellen', 'online_shop',
                 'filiale', 'betroffene_regionen', 'betroffene_laender', 'verkaufszeitraum',
                 'rueckrufgrund', 'gefahrstoff', 'risiko', 'gefaehrdete_gruppen',
                 'ig_titel', 'ig_untertitel', 'ig_kurzbeschreibung', 'caption',
                 'ig_alt_text', 'story_text', 'originalquelle', 'quelle_url')
    bool_fields = ('h_nicht_verwenden', 'h_nicht_essen', 'h_nicht_trinken', 'h_nicht_einnehmen',
                  'h_zurueckgeben', 'h_entsorgen', 'h_erstattung', 'h_kassenbon_erforderlich',
                  'de_relevant', 'eu_relevant', 'weltweit_relevant',
                  'grosse_marke', 'lebensmittel', 'kinderprodukt')
    for f in str_fields:
        if f in d:
            v = d.get(f)
            setattr(alert, f, (str(v).strip() or None) if v not in (None, '') else None)
    if 'kategorie' in d and alert.kategorie not in PWF_CATEGORIES:
        alert.kategorie = None
    if 'risiko' in d and alert.risiko not in PWF_RISK_LEVELS:
        alert.risiko = None
    for f in bool_fields:
        if f in d:
            v = d.get(f)
            setattr(alert, f, bool(v) if not isinstance(v, str) else v.lower() in ('1', 'true', 'on', 'yes'))
    for f in ('relevanz', 'dringlichkeit'):
        if f in d:
            try:
                setattr(alert, f, max(0, min(100, int(d.get(f)))))
            except Exception:
                pass
    if 'quelle_datum' in d:
        try:
            alert.quelle_datum = datetime.strptime(str(d['quelle_datum'])[:10], '%Y-%m-%d').date()
        except Exception:
            alert.quelle_datum = None


@app.route('/content-studio/produktwarnungen')
@login_required
def product_alert_factory():
    status_f = request.args.get('status', '')
    q = ProductAlert.query
    if status_f:
        q = q.filter_by(status=status_f)
    alerts = q.order_by((ProductAlert.status == 'archiviert'), ProductAlert.created_at.desc()).all()
    accounts = Account.query.filter(Account.status.in_(['active', 'paused'])).order_by(Account.name).all()
    from collections import Counter
    counts = Counter(a.status for a in ProductAlert.query.all())
    return render_template('produktwarnungen.html',
        alerts=[_pwf_alert_dict(a) for a in alerts],
        accounts=[{'id': a.id, 'name': a.name} for a in accounts],
        counts=dict(counts), status_f=status_f,
        categories=PWF_CATEGORIES, risk_levels=PWF_RISK_LEVELS, active_page='studio')


@app.route('/content-studio/produktwarnungen/<int:alert_id>')
@login_required
def product_alert_detail(alert_id):
    a = ProductAlert.query.get_or_404(alert_id)
    accounts = Account.query.filter(Account.status.in_(['active', 'paused'])).order_by(Account.name).all()
    return render_template('produktwarnung_detail.html', alert=_pwf_alert_dict(a),
        accounts=[{'id': x.id, 'name': x.name} for x in accounts],
        categories=PWF_CATEGORIES, risk_levels=PWF_RISK_LEVELS, active_page='studio')


@app.route('/api/pwf/extract-from-text', methods=['POST'])
@login_required
def pwf_extract_from_text():
    d = request.get_json(silent=True) or request.form
    text = (d.get('text') or '').strip()
    if not text:
        return jsonify({'ok': False, 'error': 'Bitte Text einfügen.'}), 400
    api_key = get_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key konfiguriert (Einstellungen → KI).'}), 400
    data = _pwf_extract_from_text(text, api_key) or {}
    if not data.get('is_case'):
        return jsonify({'ok': False, 'error': 'Im Text wurde keine Produktwarnung/kein Rückruf erkannt.'}), 400
    photo_media_id = None
    if data.get('produktbild_vorhanden') and data.get('produktbild_url'):
        photo_media_id = _pwf_maybe_fetch_photo(data.get('produktbild_url'))
    resp = {'ok': True, 'fields': data, 'photo_extracted': bool(photo_media_id)}
    if photo_media_id:
        mi = MediaItem.query.get(photo_media_id)
        resp['photo_media_id'] = photo_media_id
        resp['photo_url'] = mi.url if mi else None
    return jsonify(resp)


@app.route('/api/pwf/extract-from-url', methods=['POST'])
@login_required
def pwf_extract_from_url():
    from urllib.parse import urlparse as _urlparse
    d = request.get_json(silent=True) or request.form
    url = (d.get('url') or '').strip()
    if not url:
        return jsonify({'ok': False, 'error': 'Bitte einen Link angeben.'}), 400
    if not _mcf_is_safe_url(url):
        return jsonify({'ok': False, 'error': 'Diese URL ist nicht erlaubt.'}), 400
    api_key = get_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key konfiguriert (Einstellungen → KI).'}), 400
    try:
        text, og_image = _mcf_fetch_url_content(url)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Seite konnte nicht geladen werden: {e}'}), 400
    if not text.strip():
        return jsonify({'ok': False, 'error': 'Auf der Seite wurde kein lesbarer Text gefunden.'}), 400
    data = _pwf_extract_from_text(text, api_key) or {}
    if not data.get('is_case'):
        return jsonify({'ok': False, 'error': 'Auf der Seite wurde keine Produktwarnung/kein Rückruf erkannt.'}), 400
    if not data.get('originalquelle'):
        data['originalquelle'] = _urlparse(url).hostname or url
    photo_url = data.get('produktbild_url') or og_image
    photo_media_id = _pwf_maybe_fetch_photo(photo_url) if photo_url else None
    resp = {'ok': True, 'fields': data, 'photo_extracted': bool(photo_media_id),
           'quelle_url': url}
    if photo_media_id:
        mi = MediaItem.query.get(photo_media_id)
        resp['photo_media_id'] = photo_media_id
        resp['photo_url'] = mi.url if mi else None
    return jsonify(resp)


@app.route('/api/pwf/extract-from-image', methods=['POST'])
@login_required
def pwf_extract_from_image():
    f = request.files.get('source_image')
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': 'Kein Bild hochgeladen.'}), 400
    api_key = get_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Kein Anthropic API-Key konfiguriert (Einstellungen → KI).'}), 400
    img_bytes = f.read()
    if not img_bytes:
        return jsonify({'ok': False, 'error': 'Bild ist leer.'}), 400
    if len(img_bytes) > 15 * 1024 * 1024:
        return jsonify({'ok': False, 'error': 'Bild zu groß (max. 15 MB).'}), 400
    ext = (os.path.splitext(f.filename)[1] or '.jpg').lower()
    mime = {'.png': 'image/png', '.webp': 'image/webp', '.gif': 'image/gif'}.get(ext, 'image/jpeg')
    data = _pwf_extract_from_image_bytes(img_bytes, mime, api_key) or {}
    if not data.get('is_case'):
        return jsonify({'ok': False, 'error': 'Auf dem Bild wurde keine Produktwarnung/kein Rückruf erkannt.'}), 400
    # Das hochgeladene Bild selbst als Produktfoto-Kandidat übernehmen
    photo_media_id = _pwf_save_photo_bytes(img_bytes, ext, f.filename)
    resp = {'ok': True, 'fields': data, 'photo_extracted': bool(photo_media_id)}
    if photo_media_id:
        mi = MediaItem.query.get(photo_media_id)
        resp['photo_media_id'] = photo_media_id
        resp['photo_url'] = mi.url if mi else None
    return jsonify(resp)


@app.route('/api/pwf/alert', methods=['POST'])
@login_required
def pwf_create_alert():
    d = request.form
    files = [f for f in request.files.getlist('fotos') if f and f.filename]
    try:
        extracted_photo_id = int(d.get('extracted_photo_media_id')) if (d.get('extracted_photo_media_id') or '').strip() else None
    except Exception:
        extracted_photo_id = None
    if not any((d.get('titel'), d.get('produktname'), d.get('kurztitel'), files, extracted_photo_id)):
        return jsonify({'ok': False, 'error': 'Bitte mindestens Titel/Produktname oder ein Bild angeben.'}), 400
    dk = _pwf_dedup_key(d.get('produktname'), d.get('marke'), d.get('charge'), d.get('titel'))
    # Eine Entwarnung zum selben Produkt ist KEIN Duplikat, sondern ein eigenes,
    # späteres Ereignis — sonst würde sie hier fälschlich blockiert, weil sie
    # denselben Dedup-Key (Produktname/Marke/Charge) wie der Original-Rückruf hat.
    if d.get('kategorie') != 'Entwarnung':
        dup = _pwf_find_duplicate(dk)
        if dup:
            return jsonify({'ok': True, 'duplicate': True, 'alert_id': dup.id,
                            'msg': 'Diese Warnung existiert bereits — es wurde kein Duplikat angelegt.'})
    acc_id = None
    try:
        acc_id = int(d.get('account_id')) if (d.get('account_id') or '').strip() else None
    except Exception:
        acc_id = None
    account = Account.query.get(acc_id) if acc_id else _pwf_target_account()

    a = ProductAlert(origin='manuell', status='entwurf', dedup_key=dk,
                     account_id=account.id if account else None)
    _pwf_apply_fields(a, d.to_dict())
    db.session.add(a)
    db.session.flush()
    _pwf_maybe_fire_urgent_alert(a)
    if a.kategorie == 'Entwarnung':
        related = _pwf_find_related_recall(a.produktname, a.marke, exclude_id=a.id)
        if related:
            a.related_alert_id = related.id

    media_ids = []
    for f in files:
        mid = _pwf_save_photo(f)
        if mid:
            media_ids.append(mid)
    if extracted_photo_id and extracted_photo_id not in media_ids:
        media_ids.append(extracted_photo_id)
    if media_ids:
        a.foto_media_id = media_ids[0]

    a.generated_image_path = _pwf_render_card(a)
    if not a.caption:
        a.caption = _pwf_generate_caption(a)
    db.session.commit()
    if a.foto_media_id:
        _pwf_score_image_match_async(a.id)
    return jsonify({'ok': True, 'alert_id': a.id, 'alert': _pwf_alert_dict(a)})


@app.route('/api/pwf/alert/<int:aid>/update', methods=['POST'])
@login_required
def pwf_update_alert(aid):
    a = ProductAlert.query.get_or_404(aid)
    d = request.get_json(silent=True) or request.form
    _pwf_apply_fields(a, d if isinstance(d, dict) else d.to_dict())
    if 'account_id' in d:
        try:
            a.account_id = int(d.get('account_id')) or None
        except Exception:
            a.account_id = None
    a.dedup_key = _pwf_dedup_key(a.produktname, a.marke, a.charge, a.titel)
    db.session.commit()
    return jsonify({'ok': True, 'alert': _pwf_alert_dict(a)})


@app.route('/api/pwf/alert/<int:aid>/status', methods=['POST'])
@login_required
def pwf_set_status(aid):
    a = ProductAlert.query.get_or_404(aid)
    d = request.get_json(silent=True) or {}
    st = d.get('status')
    if st not in ('entwurf', 'veroeffentlicht', 'archiviert'):
        return jsonify({'ok': False, 'error': 'ungültiger Status'}), 400
    a.status = st
    if st == 'veroeffentlicht' and not a.published_at:
        a.published_at = datetime.utcnow()
        if (d.get('ig_post_ref') or '').strip():
            a.ig_post_ref = d.get('ig_post_ref').strip()
    db.session.commit()
    return jsonify({'ok': True, 'alert': _pwf_alert_dict(a)})


@app.route('/api/pwf/alert/<int:aid>/regenerate-image', methods=['POST'])
@login_required
def pwf_regen_image(aid):
    a = ProductAlert.query.get_or_404(aid)
    a.generated_image_path = _pwf_render_card(a)
    db.session.commit()
    return jsonify({'ok': True, 'image': f'/media/file/{a.generated_image_path}'})


@app.route('/api/pwf/alert/<int:aid>/regenerate-caption', methods=['POST'])
@login_required
def pwf_regen_caption(aid):
    a = ProductAlert.query.get_or_404(aid)
    a.caption = _pwf_generate_caption(a)
    db.session.commit()
    return jsonify({'ok': True, 'caption': a.caption})


@app.route('/api/pwf/alert/<int:aid>/feedback', methods=['POST'])
@login_required
def pwf_feedback(aid):
    """Nutzer bewertet ob Kategorie/Risiko/Relevanz der KI-Einschätzung
    zutrafen. Fließt über _pwf_calibration_block in künftige Extraktionen ein."""
    a = ProductAlert.query.get_or_404(aid)
    d = request.get_json(silent=True) or {}
    try:
        v = int(d.get('value', 0))
    except Exception:
        v = 0
    a.feedback = v if v in (1, -1) else None
    a.feedback_at = datetime.utcnow() if a.feedback else None
    db.session.commit()
    return jsonify({'ok': True, 'feedback': a.feedback or 0})


@app.route('/api/pwf/alert/<int:aid>/check-image-match', methods=['POST'])
@login_required
def pwf_check_image_match(aid):
    """Manueller Re-Check des Bild-Match-Scores (z.B. nach Fotowechsel) —
    synchron, damit der 'Prüfen'-Button ein sofortiges Ergebnis zeigt."""
    a = ProductAlert.query.get_or_404(aid)
    if not a.foto_media_id:
        return jsonify({'ok': False, 'error': 'Kein Produktfoto hinterlegt.'}), 400
    media = MediaItem.query.get(a.foto_media_id)
    photo_bytes = _mcf_load_image_bytes(media) if media else None
    headline = a.produktname or a.titel or a.kurztitel
    score = _pwf_score_image_match(headline, photo_bytes)
    if score is None:
        return jsonify({'ok': False, 'error': 'Bewertung fehlgeschlagen (kein API-Key oder Fehler).'}), 400
    a.image_match_score = score
    db.session.commit()
    return jsonify({'ok': True, 'image_match_score': score})


@app.route('/api/pwf/alert/<int:aid>/telegram', methods=['POST'])
@login_required
def pwf_send_telegram(aid):
    a = ProductAlert.query.get_or_404(aid)
    token = get_setting('telegram_bot_token')
    if not token:
        return jsonify({'ok': False, 'error': 'Kein Telegram-Bot-Token konfiguriert.'}), 400
    acc = Account.query.get(a.account_id) if a.account_id else None
    chat_id = ((acc.telegram_chat_id or '').strip() if acc else '')
    if not chat_id or chat_id in ('None', 'null'):
        return jsonify({'ok': False, 'error': 'Ziel-Seite hat keinen Telegram-Channel. Bitte Seite wählen und Channel eintragen.'}), 400
    if not a.generated_image_path or not os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], a.generated_image_path)):
        a.generated_image_path = _pwf_render_card(a)
    img_path = os.path.join(app.config['UPLOAD_FOLDER'], a.generated_image_path)
    try:
        with open(img_path, 'rb') as f:
            _tg_call(token, 'sendPhoto', data={'chat_id': chat_id}, files={'photo': f})
        parts = [a.caption or '']
        src = []
        if a.originalquelle:
            src.append(f'Quelle: {a.originalquelle}')
        if a.quelle_url:
            src.append(a.quelle_url)
        if src:
            parts.append('\n'.join(src))
        text = '\n\n'.join(p for p in parts if p).strip()[:4000]
        if text:
            _tg_call(token, 'sendMessage', json={'chat_id': chat_id, 'text': text})
        _tg_call(token, 'sendMessage', json={
            'chat_id': chat_id,
            'text': 'Nach dem Posten auf Instagram bitte bestätigen:',
            'reply_markup': {'inline_keyboard': [[
                {'text': '✅ Auf Instagram veröffentlicht', 'callback_data': f'pwfposted_{a.id}'}]]}})
    except Exception as e:
        app.logger.warning('PWF Telegram-Versand: %s', e)
        return jsonify({'ok': False, 'error': f'Versand fehlgeschlagen: {e}'}), 500
    a.telegram_sent_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/pwf/alert/<int:aid>/delete', methods=['POST'])
@login_required
def pwf_delete_alert(aid):
    a = ProductAlert.query.get_or_404(aid)
    db.session.delete(a)
    db.session.commit()
    return jsonify({'ok': True})


# ─── Quellen-Verwaltung + Auto-Recherche ──────────────────────────────────
def _pwf_run_research():
    """Scannt konfigurierte RSS-Quellen nach Produktwarnungen/Rückrufen und
    legt Auto-Entwürfe an (mit Dedup + Auto-Bild/Caption). Gibt (created, checked)."""
    sources = ProductAlertSource.query.filter_by(active=True).all()
    api_key = get_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY')
    if not api_key or not sources:
        return 0, 0
    KW = ['rückruf', 'rueckruf', 'warnung', 'warnhinweis', 'gesundheitsgefahr',
         'verkaufsstopp', 'kontamination', 'fremdkörper', 'allergen']
    checked = created = 0
    for src in sources:
        try:
            # Dedizierte Rückruf-Feeds (z.B. BAuA) brauchen keinen Keyword-
            # Filter — jeder Eintrag ist bereits ein Rückruf, die Titel
            # enthalten oft keine "Rückruf"/"Warnung"-Wörter (nur Produkt-
            # name+Hersteller). Allgemeine News-Feeds werden weiter gefiltert.
            entries = fetch_rss_feed(src.url, keywords=None if src.dedicated_feed else KW) or []
        except Exception as e:
            app.logger.warning('PWF Feed %s: %s', src.url, e)
            continue
        src.last_scanned_at = datetime.utcnow()
        for e in entries:
            checked += 1
            link = (e.get('url') or '').strip()
            if link and ProductAlert.query.filter_by(quelle_url=link).first():
                continue
            data = _pwf_extract_from_text(f"{e.get('title', '')}\n\n{e.get('description', '')}", api_key)
            if not data or not data.get('is_case'):
                continue
            dk = _pwf_dedup_key(data.get('produktname'), data.get('marke'),
                                data.get('charge'), data.get('titel'))
            # Entwarnung zum selben Produkt ist kein Duplikat, s. pwf_create_alert
            if data.get('kategorie') != 'Entwarnung' and _pwf_find_duplicate(dk):
                continue
            acc = _pwf_target_account()
            a = ProductAlert(origin='auto', status='entwurf', dedup_key=dk,
                             account_id=acc.id if acc else None,
                             quelle_url=link or None,
                             originalquelle=data.get('originalquelle') or src.name)
            _pwf_apply_fields(a, data)
            db.session.add(a)
            db.session.flush()
            _pwf_maybe_fire_urgent_alert(a)
            if a.kategorie == 'Entwarnung':
                related = _pwf_find_related_recall(a.produktname, a.marke, exclude_id=a.id)
                if related:
                    a.related_alert_id = related.id
            if data.get('produktbild_vorhanden') and data.get('produktbild_url'):
                pid = _pwf_maybe_fetch_photo(data.get('produktbild_url'))
                if pid:
                    a.foto_media_id = pid
            try:
                a.generated_image_path = _pwf_render_card(a)
                if not a.caption:
                    a.caption = _pwf_generate_caption(a)
            except Exception as ex:
                app.logger.warning('PWF Auto-Gen: %s', ex)
            db.session.commit()
            if a.foto_media_id:
                _pwf_score_image_match_async(a.id)
            created += 1
        db.session.commit()
    return created, checked


def _pwf_auto_scan():
    with app.app_context():
        if get_setting('pwf_auto_research') != '1':
            return
        if not (os.environ.get('ANTHROPIC_API_KEY') or get_setting('anthropic_api_key')):
            return
        _pwf_run_research()


@app.route('/api/pwf/research/run', methods=['POST'])
@login_required
def pwf_research_run():
    api_key = get_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Auto-Recherche braucht einen Anthropic-API-Key (Einstellungen → KI).'}), 400
    if not ProductAlertSource.query.filter_by(active=True).first():
        return jsonify({'ok': False, 'error': 'Noch keine Quellen hinterlegt.'}), 400
    created, checked = _pwf_run_research()
    return jsonify({'ok': True, 'created': created, 'checked': checked})


@app.route('/api/pwf/research/toggle', methods=['POST'])
@login_required
def pwf_research_toggle():
    d = request.get_json(silent=True) or {}
    set_setting('pwf_auto_research', '1' if d.get('on') else '0')
    db.session.commit()
    return jsonify({'ok': True, 'on': bool(d.get('on'))})


@app.route('/api/pwf/sources', methods=['GET', 'POST'])
@login_required
def pwf_sources():
    if request.method == 'GET':
        srcs = ProductAlertSource.query.order_by(ProductAlertSource.name).all()
        return jsonify({'sources': [{'id': s.id, 'name': s.name, 'url': s.url, 'active': s.active,
                                     'dedicated_feed': bool(s.dedicated_feed),
                                     'last_scanned_rel': _tr_rel_time(s.last_scanned_at) if s.last_scanned_at else ''}
                                    for s in srcs]})
    d = request.get_json(silent=True) or {}
    url = (d.get('url') or '').strip()
    if not url:
        return jsonify({'ok': False, 'error': 'URL erforderlich'}), 400
    s = ProductAlertSource(name=(d.get('name') or url).strip(), url=url, active=True,
                           dedicated_feed=bool(d.get('dedicated_feed')))
    db.session.add(s)
    db.session.commit()
    return jsonify({'ok': True, 'source': {'id': s.id, 'name': s.name, 'url': s.url, 'active': s.active,
                                           'dedicated_feed': s.dedicated_feed}})


@app.route('/api/pwf/sources/<int:sid>', methods=['PATCH', 'DELETE'])
@login_required
def pwf_source_edit(sid):
    s = ProductAlertSource.query.get_or_404(sid)
    if request.method == 'DELETE':
        db.session.delete(s)
        db.session.commit()
        return jsonify({'ok': True})
    d = request.get_json(silent=True) or {}
    if 'dedicated_feed' in d:
        s.dedicated_feed = bool(d['dedicated_feed'])
    if 'active' in d:
        s.active = bool(d['active'])
    if 'name' in d and (d['name'] or '').strip():
        s.name = d['name'].strip()
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/pwf/target-account', methods=['POST'])
@login_required
def pwf_target_account_save():
    d = request.get_json(silent=True) or {}
    acc_id = (str(d.get('account_id')) or '').strip()
    set_setting('pwf_target_account_id', acc_id or '')
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/content-studio/produktwarnungen/einstellungen')
@login_required
def pwf_einstellungen():
    sources = ProductAlertSource.query.order_by(ProductAlertSource.name).all()
    accounts = Account.query.filter(Account.status.in_(['active', 'paused'])).order_by(Account.name).all()
    return render_template('pwf_einstellungen.html',
        sources=[{'id': s.id, 'name': s.name, 'url': s.url, 'active': s.active,
                 'dedicated_feed': bool(s.dedicated_feed)} for s in sources],
        accounts=[{'id': a.id, 'name': a.name} for a in accounts],
        target_account_id=get_setting('pwf_target_account_id'),
        auto_on=(get_setting('pwf_auto_research') == '1'),
        active_page='studio')


# ══════════════════════════════════════════════════════════════════════════════
# CANVA-AUTOFILL für die Product Alert Factory — optionale Alternative zum
# lokalen PIL-Renderer. Baut auf demselben OAuth-PKCE + Autofill-Muster wie die
# MemeOS-Canva-Anbindung auf (eigene Client-ID nötig — eigenständige
# Codebase/DB, auch wenn dasselbe Canva-Konto verbunden wird), ergänzt um
# Bild-Feld-Autofill (Produktfoto) über die Canva Asset-Upload-API, die
# MemeOS bisher nicht nutzt (dort nur Text-Felder).
#
# WICHTIG — das kann kein Code allein: der Mensch baut das Canva-Brand-
# Template EINMAL selbst (Layout, Farben, Icons — siehe Designbriefing),
# benennt die Platzhalter-Felder in Canva exakt wie unten in
# PWF_CANVA_DEFAULT_FIELD_MAP vorgeschlagen (oder passt die Zuordnung in den
# Einstellungen an) und trägt die Brand-Template-ID ein. Ohne echten
# Canva-Account/-Template konnte dieser Teil hier nur bis zur API-Grenze
# getestet werden (siehe Commit-Notiz) — der erste echte Autofill-Lauf muss
# vom Menschen beobachtet werden.
# ══════════════════════════════════════════════════════════════════════════════

CANVA_CLIENT_ID     = os.environ.get('CANVA_CLIENT_ID', '')
CANVA_CLIENT_SECRET = os.environ.get('CANVA_CLIENT_SECRET', '')
CANVA_SCOPES = 'asset:read asset:write design:content:read design:content:write brand_template:read'

# Vorschlag für die Feldnamen, die in Canva als "Feld" markiert werden müssen
# (Elemente auswählen → rechte Seitenleiste → "Feld verbinden"). Der Nutzer
# kann die rechte Seite in den Einstellungen frei anpassen, falls er in Canva
# andere Namen vergeben hat.
PWF_CANVA_DEFAULT_FIELD_MAP = {
    'kategorie_banner': 'kategorie_banner', 'kategorie': 'kategorie', 'risiko': 'risiko',
    'titel': 'titel', 'beschreibung': 'beschreibung',
    'produktname': 'produktname', 'marke': 'marke', 'hersteller': 'hersteller',
    'charge': 'charge', 'losnummer': 'losnummer', 'mhd': 'mhd', 'ean': 'ean',
    'verpackungsgroesse': 'verpackungsgroesse', 'verkaufsstellen': 'verkaufsstellen',
    'betroffene_regionen': 'betroffene_regionen',
    'rueckrufgrund': 'rueckrufgrund', 'handlungsempfehlung': 'handlungsempfehlung',
    'originalquelle': 'originalquelle', 'quelle_datum': 'quelle_datum',
    'produktfoto': 'produktfoto',   # Bild-Feld — einziges Feld vom Typ "image"
}
PWF_CANVA_IMAGE_KEYS = {'produktfoto'}


def _canva_redirect_uri():
    base = (get_setting('app_base_url') or os.environ.get('RENDER_EXTERNAL_URL')
            or request.host_url.rstrip('/'))
    return base.rstrip('/') + '/canva/callback'


def _canva_load_tokens():
    try:
        return json.loads(get_setting('canva_tokens') or '{}')
    except Exception:
        return {}


def _canva_save_tokens(tokens):
    set_setting('canva_tokens', json.dumps(tokens))
    db.session.commit()


def _canva_get_token():
    if not CANVA_CLIENT_ID or not CANVA_CLIENT_SECRET:
        return None
    tokens = _canva_load_tokens()
    access_token = tokens.get('access_token')
    expires_at = tokens.get('expires_at', '')
    try:
        if access_token and expires_at:
            if datetime.fromisoformat(expires_at) > datetime.utcnow() + timedelta(minutes=5):
                return access_token
    except Exception:
        pass
    refresh_token = tokens.get('refresh_token') or get_setting('canva_refresh_token_backup')
    if not refresh_token:
        return None
    try:
        r = _requests.post('https://api.canva.com/rest/v1/oauth/token', data={
            'grant_type': 'refresh_token', 'refresh_token': refresh_token,
            'client_id': CANVA_CLIENT_ID, 'client_secret': CANVA_CLIENT_SECRET,
        }, timeout=15)
        if r.ok:
            data = r.json()
            new_tokens = {
                'access_token': data.get('access_token'),
                'refresh_token': data.get('refresh_token', refresh_token),
                'expires_at': (datetime.utcnow() + timedelta(seconds=data.get('expires_in', 3600))).isoformat(),
            }
            _canva_save_tokens(new_tokens)
            return new_tokens['access_token']
    except Exception as ex:
        app.logger.warning('Canva Token-Refresh: %s', ex)
    return None


def _canva_is_connected():
    if not CANVA_CLIENT_ID or not CANVA_CLIENT_SECRET:
        return False
    if get_setting('canva_explicitly_disconnected') == '1':
        return False
    tokens = _canva_load_tokens()
    access_token = tokens.get('access_token')
    expires_at = tokens.get('expires_at', '')
    try:
        if access_token and expires_at:
            if datetime.fromisoformat(expires_at) > datetime.utcnow() + timedelta(minutes=5):
                return True
    except Exception:
        pass
    return bool(tokens.get('refresh_token') or get_setting('canva_refresh_token_backup'))


@app.route('/canva/connect')
@login_required
def canva_connect():
    if not CANVA_CLIENT_ID:
        flash('CANVA_CLIENT_ID ist nicht gesetzt (Umgebungsvariable auf Render).', 'error')
        return redirect(url_for('pwf_einstellungen'))
    import hashlib as _hashlib, base64 as _b64, urllib.parse as _uparse
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _b64.urlsafe_b64encode(
        _hashlib.sha256(code_verifier.encode()).digest()).rstrip(b'=').decode()
    session['canva_code_verifier'] = code_verifier
    params = {
        'client_id': CANVA_CLIENT_ID, 'redirect_uri': _canva_redirect_uri(),
        'response_type': 'code', 'scope': CANVA_SCOPES,
        'code_challenge': code_challenge, 'code_challenge_method': 'S256',
        'state': 'contentos_pwf_canva_auth',
    }
    return redirect('https://www.canva.com/api/oauth/authorize?' + _uparse.urlencode(params))


@app.route('/canva/callback')
def canva_callback():
    code = request.args.get('code')
    if request.args.get('error') or not code:
        return redirect(url_for('pwf_einstellungen') + '?canva=error')
    code_verifier = session.pop('canva_code_verifier', '')
    try:
        token_data = {
            'grant_type': 'authorization_code', 'code': code,
            'redirect_uri': _canva_redirect_uri(), 'client_id': CANVA_CLIENT_ID,
            'code_verifier': code_verifier,
        }
        if CANVA_CLIENT_SECRET:
            token_data['client_secret'] = CANVA_CLIENT_SECRET
        r = _requests.post('https://api.canva.com/rest/v1/oauth/token', data=token_data, timeout=15)
        if r.ok:
            data = r.json()
            tokens = {
                'access_token': data.get('access_token'),
                'refresh_token': data.get('refresh_token'),
                'expires_at': (datetime.utcnow() + timedelta(seconds=data.get('expires_in', 3600))).isoformat(),
            }
            _canva_save_tokens(tokens)
            if data.get('refresh_token'):
                set_setting('canva_refresh_token_backup', data['refresh_token'])
            set_setting('canva_explicitly_disconnected', '0')
            db.session.commit()
            return redirect(url_for('pwf_einstellungen') + '?canva=connected')
    except Exception as ex:
        app.logger.error('Canva Callback: %s', ex)
    return redirect(url_for('pwf_einstellungen') + '?canva=error')


@app.route('/canva/disconnect', methods=['POST'])
@login_required
def canva_disconnect():
    _canva_save_tokens({})
    set_setting('canva_explicitly_disconnected', '1')
    db.session.commit()
    return redirect(url_for('pwf_einstellungen'))


@app.route('/api/canva/status')
@login_required
def api_canva_status():
    return jsonify({'connected': _canva_is_connected(), 'client_id_set': bool(CANVA_CLIENT_ID)})


def _canva_upload_asset(image_bytes, file_name):
    """Lädt ein Bild als Canva-Asset hoch (nötig für Bild-Autofill-Felder wie
    das Produktfoto). Async Job wie Autofill/Export — pollt bis fertig."""
    token = _canva_get_token()
    if not token or not image_bytes:
        return None
    import base64 as _b64, time as _time
    metadata = json.dumps({'name_base64': _b64.b64encode(file_name.encode()).decode()})
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/octet-stream',
              'Asset-Upload-Metadata': metadata}
    try:
        r = _requests.post('https://api.canva.com/rest/v1/asset-uploads',
                           headers=headers, data=image_bytes, timeout=30)
        if not r.ok:
            app.logger.warning('Canva Asset-Upload: %s %s', r.status_code, r.text[:150])
            return None
        job_id = r.json().get('job', {}).get('id')
        if not job_id:
            return None
    except Exception as ex:
        app.logger.warning('Canva Asset-Upload Request: %s', ex)
        return None
    for _ in range(15):
        _time.sleep(1.5)
        try:
            sr = _requests.get(f'https://api.canva.com/rest/v1/asset-uploads/{job_id}',
                               headers={'Authorization': f'Bearer {token}'}, timeout=10)
            if sr.ok:
                jd = sr.json().get('job', {})
                if jd.get('status') == 'success':
                    return jd.get('asset', {}).get('id')
                if jd.get('status') == 'failed':
                    return None
        except Exception:
            pass
    return None


def _pwf_canva_field_values(alert):
    """Baut die Text-/Bild-Werte für den Canva-Autofill aus einem ProductAlert
    (oder einem gleich aufgebauten dict, für den Einstellungs-Testlauf)."""
    def g(key, default=None):
        return (alert.get(key) if isinstance(alert, dict) else getattr(alert, key, None)) or default

    actions_lbl = {'h_nicht_verwenden': 'Nicht verwenden', 'h_nicht_essen': 'Nicht essen',
                   'h_nicht_trinken': 'Nicht trinken', 'h_nicht_einnehmen': 'Nicht einnehmen',
                   'h_zurueckgeben': 'Zurückgeben', 'h_entsorgen': 'Entsorgen',
                   'h_erstattung': 'Erstattung möglich', 'h_kassenbon_erforderlich': 'Kassenbon nötig'}
    actions = [lbl for key, lbl in actions_lbl.items() if g(key)]

    values = {
        'kategorie_banner': _PWF_CATEGORY_BANNER.get(g('kategorie'), 'PRODUKTWARNUNG'),
        'kategorie': g('kategorie', ''), 'risiko': g('risiko', ''),
        'titel': g('titel') or g('produktname', ''), 'beschreibung': g('rueckrufgrund', '')[:180],
        'produktname': g('produktname', ''), 'marke': g('marke', ''), 'hersteller': g('hersteller', ''),
        'charge': g('charge', ''), 'losnummer': g('losnummer', ''), 'mhd': g('mhd', ''),
        'ean': g('ean', ''), 'verpackungsgroesse': g('verpackungsgroesse', ''),
        'verkaufsstellen': g('verkaufsstellen', ''), 'betroffene_regionen': g('betroffene_regionen', ''),
        'rueckrufgrund': g('rueckrufgrund', ''), 'handlungsempfehlung': ' • '.join(actions),
        'originalquelle': g('originalquelle', ''),
        'quelle_datum': str(g('quelle_datum') or ''),
    }
    return {k: v for k, v in values.items() if v not in (None, '')}


def _pwf_canva_autofill(alert, photo_bytes=None):
    """Füllt das konfigurierte Canva-Brand-Template mit den Alert-Daten und
    exportiert es als PNG. Gibt PNG-Bytes|None (None = kein harter Fehler,
    Aufrufer fällt auf den lokalen PIL-Renderer zurück)."""
    template_id = get_setting('pwf_canva_template_id')
    token = _canva_get_token()
    if not template_id or not token:
        return None
    try:
        field_map = json.loads(get_setting('pwf_canva_field_map') or '{}') or PWF_CANVA_DEFAULT_FIELD_MAP
    except Exception:
        field_map = PWF_CANVA_DEFAULT_FIELD_MAP

    values = _pwf_canva_field_values(alert)
    data = {}
    for our_key, value in values.items():
        canva_field = field_map.get(our_key)
        if canva_field:
            data[canva_field] = {'type': 'text', 'text': str(value)[:500]}

    img_field = field_map.get('produktfoto')
    if img_field and photo_bytes:
        asset_id = _canva_upload_asset(photo_bytes, 'produktfoto.jpg')
        if asset_id:
            data[img_field] = {'type': 'image', 'asset_id': asset_id}

    if not data:
        return None
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    import time as _time
    try:
        r = _requests.post('https://api.canva.com/rest/v1/autofills', headers=headers,
                           json={'brand_template_id': template_id, 'data': data}, timeout=20)
        if not r.ok:
            app.logger.warning('Canva Autofill: %s %s', r.status_code, r.text[:200])
            return None
        job_id = r.json().get('job', {}).get('id')
        if not job_id:
            return None
    except Exception as ex:
        app.logger.warning('Canva Autofill Request: %s', ex)
        return None

    design_id = None
    for _ in range(20):
        _time.sleep(2)
        try:
            sr = _requests.get(f'https://api.canva.com/rest/v1/autofills/{job_id}',
                               headers=headers, timeout=10)
            if sr.ok:
                jd = sr.json().get('job', {})
                if jd.get('status') == 'success':
                    design_id = jd.get('result', {}).get('design', {}).get('id')
                    break
                if jd.get('status') == 'failed':
                    app.logger.warning('Canva Autofill failed: %s', jd.get('error'))
                    return None
        except Exception:
            pass
    if not design_id:
        return None

    try:
        er = _requests.post('https://api.canva.com/rest/v1/exports', headers=headers,
                            json={'design_id': design_id, 'format': {'type': 'png', 'lossless': True}},
                            timeout=20)
        if not er.ok:
            return None
        export_job_id = er.json().get('job', {}).get('id')
        if not export_job_id:
            return None
    except Exception:
        return None

    for _ in range(20):
        _time.sleep(2)
        try:
            pr = _requests.get(f'https://api.canva.com/rest/v1/exports/{export_job_id}',
                               headers=headers, timeout=10)
            if pr.ok:
                ej = pr.json().get('job', {})
                if ej.get('status') == 'success':
                    urls = ej.get('result', {}).get('urls', [])
                    if urls:
                        img_r = _requests.get(urls[0], timeout=30)
                        if img_r.ok:
                            return img_r.content
                    break
                if ej.get('status') == 'failed':
                    return None
        except Exception:
            pass
    return None


def _pwf_save_canva_png(alert, png_bytes):
    import uuid as _uuid
    fname = f'pwfcanva_{alert.id}_{_uuid.uuid4().hex[:8]}.png'
    with open(os.path.join(app.config['UPLOAD_FOLDER'], fname), 'wb') as f:
        f.write(png_bytes)
    return fname


def _pwf_render_card(alert):
    """Wählt den Render-Weg: Canva-Autofill (falls konfiguriert & verbunden),
    sonst der lokale PIL-Renderer. Fällt bei jedem Canva-Fehler automatisch
    auf PIL zurück — es entsteht NIE ein Post ohne Bild."""
    if get_setting('pwf_render_type') == 'canva' and _canva_is_connected() and get_setting('pwf_canva_template_id'):
        media = MediaItem.query.get(alert.foto_media_id) if alert.foto_media_id else None
        photo_bytes = _mcf_load_image_bytes(media) if media else None
        png_bytes = _pwf_canva_autofill(alert, photo_bytes)
        if png_bytes:
            return _pwf_save_canva_png(alert, png_bytes)
        app.logger.warning('PWF Canva-Autofill fehlgeschlagen — Fallback auf PIL-Renderer (Alert %s)', alert.id)
    return _render_product_alert_image(alert)


@app.route('/api/pwf/canva-config', methods=['GET', 'POST'])
@login_required
def pwf_canva_config():
    if request.method == 'GET':
        return jsonify({
            'connected': _canva_is_connected(), 'client_id_set': bool(CANVA_CLIENT_ID),
            'template_id': get_setting('pwf_canva_template_id') or '',
            'field_map': get_setting('pwf_canva_field_map') or json.dumps(PWF_CANVA_DEFAULT_FIELD_MAP, ensure_ascii=False, indent=2),
            'render_type': get_setting('pwf_render_type') or 'pil',
        })
    d = request.get_json(silent=True) or {}
    if 'template_id' in d:
        set_setting('pwf_canva_template_id', (d.get('template_id') or '').strip())
    if 'field_map' in d:
        try:
            json.loads(d.get('field_map') or '{}')   # nur validieren
            set_setting('pwf_canva_field_map', d.get('field_map'))
        except Exception:
            return jsonify({'ok': False, 'error': 'Feld-Zuordnung ist kein gültiges JSON.'}), 400
    if 'render_type' in d and d.get('render_type') in ('pil', 'canva'):
        set_setting('pwf_render_type', d.get('render_type'))
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/pwf/canva-test', methods=['POST'])
@login_required
def pwf_canva_test():
    """Testet den Autofill mit Beispieldaten (kein echter Fall nötig) —
    für den 'Testen'-Button in den Einstellungen, direkt nach dem Einrichten."""
    if not get_setting('pwf_canva_template_id'):
        return jsonify({'ok': False, 'error': 'Bitte zuerst eine Brand-Template-ID eintragen.'}), 400
    if not _canva_is_connected():
        return jsonify({'ok': False, 'error': 'Nicht mit Canva verbunden.'}), 400
    demo = {
        'kategorie': 'Produktrückruf', 'risiko': 'HOCH', 'titel': 'Test: Rückruf Beispielprodukt',
        'produktname': 'Beispiel-Schokolade 100g', 'marke': 'Testmarke', 'hersteller': 'Test GmbH',
        'charge': 'L-TEST01', 'mhd': '01.01.2027', 'rueckrufgrund': 'Dies ist ein Testlauf — keine echte Warnung.',
        'betroffene_regionen': 'Bundesweit', 'originalquelle': 'Content OS Testlauf',
        'h_nicht_essen': True, 'h_zurueckgeben': True, 'h_erstattung': True,
    }
    png_bytes = _pwf_canva_autofill(demo, photo_bytes=None)
    if not png_bytes:
        return jsonify({'ok': False, 'error': 'Autofill fehlgeschlagen — Logs prüfen (falsche Template-ID, '
                        'Feldnamen stimmen nicht mit dem Canva-Template überein, oder API-Fehler).'}), 400
    import base64 as _b64
    return jsonify({'ok': True, 'preview': 'data:image/png;base64,' + _b64.standard_b64encode(png_bytes).decode()})


if __name__ == '__main__':
    app.run(debug=True, port=5100)
