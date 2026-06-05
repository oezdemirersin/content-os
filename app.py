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
                    AccountGroup, ContentTemplate, ContentComment)
from sqlalchemy import func

app = Flask(__name__, template_folder='templates/cms')
_secret = os.environ.get('SECRET_KEY') or 'content-os-secret-2024-v2'
app.config['SECRET_KEY'] = _secret
app.secret_key = _secret
# Render gibt postgres:// zurück, SQLAlchemy braucht postgresql://
_db_url = os.environ.get('DATABASE_URL', 'sqlite:///content_os.db')
if _db_url.startswith('postgres://'):
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'mp4', 'mov', 'avi', 'webm', 'pdf'}

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db.init_app(app)


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
        # SQLite migrations — neue Spalten hinzufügen falls nicht vorhanden
        from sqlalchemy import text, inspect
        inspector = inspect(db.engine)
        account_cols = [c['name'] for c in inspector.get_columns('account')]
        with db.engine.connect() as conn:
            if 'growth_goal' not in account_cols:
                conn.execute(text('ALTER TABLE account ADD COLUMN growth_goal INTEGER'))
            if 'growth_goal_date' not in account_cols:
                conn.execute(text('ALTER TABLE account ADD COLUMN growth_goal_date DATETIME'))
            if 'share_token' not in account_cols:
                conn.execute(text('ALTER TABLE account ADD COLUMN share_token VARCHAR(64)'))
            # ContentItem: caption_score
            ci_cols = [c['name'] for c in inspector.get_columns('content_item')]
            if 'caption_score_manual' not in ci_cols:
                conn.execute(text('ALTER TABLE content_item ADD COLUMN caption_score_manual FLOAT'))
            # ScheduledPost: slot_type
            sp_cols = [c['name'] for c in inspector.get_columns('scheduled_post')]
            if 'slot_type' not in sp_cols:
                conn.execute(text("ALTER TABLE scheduled_post ADD COLUMN slot_type VARCHAR(20) DEFAULT 'fixed'"))
            conn.commit()
        seed_data()

init_db()


# ─────────────────────── ALERT ENGINE ───────────────────────

def generate_alerts():
    """Auto-generate system alerts based on current state."""
    # Clear old unresolved automated alerts
    SystemAlert.query.filter_by(resolved=False).filter(
        SystemAlert.alert_type.in_(['low_stock', 'no_posts', 'empty_stock'])
    ).delete()

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
        elif days < 7:
            db.session.add(SystemAlert(
                account_id=acc.id, alert_type='low_stock', severity='warning',
                message=f'"{acc.name}" hat nur {round(days, 1)} Tage Vorrat'
            ))

        # No posts scheduled at all
        upcoming = ScheduledPost.query.filter_by(account_id=acc.id, status='scheduled')\
            .filter(ScheduledPost.scheduled_at >= now).count()
        if upcoming == 0 and acc.automation_level < 3:
            db.session.add(SystemAlert(
                account_id=acc.id, alert_type='no_posts', severity='warning',
                message=f'"{acc.name}" hat keine geplanten Posts'
            ))

    # Automation errors
    broken_rules = AutomationRule.query.filter(AutomationRule.error_count > 3, AutomationRule.active == True).all()
    for rule in broken_rules:
        db.session.add(SystemAlert(
            alert_type='bot_error', severity='critical',
            message=f'Automatisierung "{rule.name}" hat {rule.error_count} Fehler'
        ))

    try:
        db.session.commit()
    except Exception:
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


def schedule_automations():
    """Background thread that runs automation rules and housekeeping."""
    tick = 0
    while True:
        try:
            with app.app_context():
                now = datetime.utcnow()
                due_rules = AutomationRule.query.filter(
                    AutomationRule.active == True,
                    (AutomationRule.next_run_at == None) |
                    (AutomationRule.next_run_at <= now)
                ).all()
                for rule in due_rules:
                    threading.Thread(target=run_automation_rule, args=(rule.id,), daemon=True).start()

            # Housekeeping every 60 ticks (~1 hour)
            tick += 1
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


def get_file_type(filename):
    ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
    if ext in {'mp4', 'mov', 'avi', 'webm'}:
        return 'video'
    elif ext in {'png', 'jpg', 'jpeg', 'gif', 'webp'}:
        return 'image'
    return 'other'


def get_dashboard_stats():
    total_accounts = Account.query.filter_by(status='active').count()
    total_followers = db.session.query(func.sum(Account.follower_count)).scalar() or 0

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0)
    today_end = today_start + timedelta(days=1)
    posts_today = ScheduledPost.query.filter(
        ScheduledPost.scheduled_at >= today_start,
        ScheduledPost.scheduled_at < today_end
    ).count()

    content_ready = ContentItem.query.filter_by(status='ready').count()
    accounts = Account.query.filter_by(status='active').all()
    critical_accounts = [a for a in accounts if a.stock_status() == 'red']
    warning_accounts = [a for a in accounts if a.stock_status() in ('orange', 'yellow')]
    active_alerts = SystemAlert.query.filter_by(resolved=False).count()

    week_ago = datetime.utcnow() - timedelta(days=7)
    old_snap = db.session.query(func.sum(AnalyticsSnapshot.followers))\
        .filter(AnalyticsSnapshot.recorded_at <= week_ago).scalar() or 0
    growth_7d = total_followers - old_snap

    return {
        'total_accounts': total_accounts,
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
    generate_alerts()
    stats = get_dashboard_stats()
    accounts = Account.query.order_by(Account.priority.desc(), Account.follower_count.desc()).limit(10).all()
    recent_content = ContentItem.query.order_by(ContentItem.created_at.desc()).limit(8).all()
    alerts = SystemAlert.query.filter_by(resolved=False).order_by(SystemAlert.severity.desc()).limit(10).all()

    chart_labels, chart_data = [], []
    for i in range(29, -1, -1):
        day = datetime.utcnow() - timedelta(days=i)
        total = db.session.query(func.sum(AnalyticsSnapshot.followers))\
            .filter(func.date(AnalyticsSnapshot.recorded_at) == day.date()).scalar() or 0
        chart_labels.append(day.strftime('%d.%m'))
        chart_data.append(total)

    forecast = linear_forecast(chart_data, 14)

    # Network stock overview
    all_active = Account.query.filter_by(status='active').all()
    stock_summary = {
        'green': sum(1 for a in all_active if a.stock_status() == 'green'),
        'yellow': sum(1 for a in all_active if a.stock_status() == 'yellow'),
        'orange': sum(1 for a in all_active if a.stock_status() == 'orange'),
        'red': sum(1 for a in all_active if a.stock_status() == 'red'),
    }

    recent_activity = ActivityLog.query.order_by(ActivityLog.created_at.desc()).limit(15).all()

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    posts_today = ScheduledPost.query.filter(
        ScheduledPost.scheduled_at >= today_start,
        ScheduledPost.scheduled_at < today_end,
        ScheduledPost.status.in_(['scheduled', 'published'])
    ).order_by(ScheduledPost.scheduled_at).all()

    all_accounts_list = Account.query.order_by(Account.follower_count.desc()).all()

    return render_template('dashboard.html',
        stats=stats, accounts=accounts, recent_content=recent_content, alerts=alerts,
        chart_labels=json.dumps(chart_labels), chart_data=json.dumps(chart_data),
        forecast=json.dumps(forecast), stock_summary=stock_summary,
        recent_activity=recent_activity, posts_today=posts_today,
        all_accounts=all_accounts_list,
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
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
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
        acc = Account(
            name=d['name'], handle=d.get('handle', ''),
            platform_id=int(d['platform_id']),
            category_id=int(d['category_id']) if d.get('category_id') else None,
            follower_count=int(d.get('follower_count') or 0),
            automation_level=int(d.get('automation_level', 0)),
            priority=d.get('priority', 'medium'),
            status=d.get('status', 'active'),
            notes=d.get('notes', ''),
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

    return render_template('account_detail.html',
        account=account, upcoming=upcoming,
        chart_labels=json.dumps(chart_labels), chart_data=json.dumps(chart_data),
        feed_days=round(feed_days, 1), story_days=round(story_days, 1), reel_count=reel_count,
        account_alerts=account_alerts,
        active_page='accounts')


@app.route('/accounts/<int:account_id>/edit', methods=['GET', 'POST'])
def account_edit(account_id):
    account = Account.query.get_or_404(account_id)
    if request.method == 'POST':
        d = request.form
        account.name = d['name']
        account.handle = d.get('handle', '')
        account.platform_id = int(d['platform_id'])
        account.category_id = int(d['category_id']) if d.get('category_id') else None
        account.follower_count = int(d.get('follower_count') or 0)
        account.automation_level = int(d.get('automation_level', 0))
        account.priority = d.get('priority', 'medium')
        account.status = d.get('status', 'active')
        account.notes = d.get('notes', '')
        account.target_feed_per_day = float(d.get('target_feed_per_day') or 1)
        account.target_story_per_day = float(d.get('target_story_per_day') or 2)
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


@app.route('/api/accounts/<int:account_id>/posts/new', methods=['POST'])
def account_post_new(account_id):
    """Create a new scheduled post via calendar drag or form."""
    d = request.get_json()
    slot_type = d.get('slot_type', 'fixed')
    # disabled-Slot: kein echtes Post, nur Platzhalter
    status = 'disabled' if slot_type == 'disabled' else 'scheduled'

    post = ScheduledPost(
        account_id=account_id,
        caption=d.get('caption', ''),
        post_type=d.get('post_type', 'feed'),
        slot_type=slot_type,
        status=status,
        scheduled_at=datetime.fromisoformat(d['scheduled_at']),
    )
    db.session.add(post)
    db.session.commit()
    log_activity('post_scheduled', f'{slot_type.capitalize()}-Slot für {acc.name} am {post.scheduled_at.strftime("%d.%m")} gesetzt')
    return jsonify({'id': post.id, 'ok': True})


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
    ordered = query.order_by(ContentItem.created_at.desc())
    pagination = ordered.paginate(page=page, per_page=per_page, error_out=False)

    # Für Kanban alle Items (max 200) ohne Pagination
    kanban_items = ordered.limit(200).all()

    categories = Category.query.order_by(Category.name).all()
    labels = Label.query.order_by(Label.name).all()

    status_counts = {}
    for s in ['draft', 'in_progress', 'ready', 'scheduled', 'published', 'archived', 'error']:
        status_counts[s] = ContentItem.query.filter_by(status=s).count()

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

    type_counts = {
        'image': MediaItem.query.filter_by(file_type='image').count(),
        'video': MediaItem.query.filter_by(file_type='video').count(),
        'other': MediaItem.query.filter(MediaItem.file_type.notin_(['image', 'video'])).count(),
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
            unique_name = f"{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_name)
            file.save(filepath)

            size = os.path.getsize(filepath)
            ftype = get_file_type(original)

            media = MediaItem(
                filename=unique_name,
                original_filename=original,
                file_type=ftype,
                mime_type=mimetypes.guess_type(original)[0] or 'application/octet-stream',
                file_size=size,
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
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], item.filename)
    if os.path.exists(filepath):
        os.remove(filepath)
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

    posts = query.all()
    type_icons = {'feed': '📸', 'reel': '🎬', 'story': '⭕', 'carousel': '🎠'}

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
        acc = Account.query.get(p.account_id)
        slot = getattr(p, 'slot_type', 'fixed') or 'fixed'
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
        rules=rules, accounts=all_accounts, active_page='automation')


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

@app.route('/api/accounts')
def api_accounts():
    accounts = Account.query.filter_by(status='active').order_by(Account.follower_count.desc()).all()
    return jsonify([{
        'id': a.id, 'name': a.name, 'handle': a.handle,
        'followers': a.follower_count, 'status': a.status,
        'category': a.category.name if a.category else '',
        'platform': a.platform.name if a.platform else '',
        'stock_status': a.stock_status(), 'stock_days': a.stock_days_display(),
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
    # unlink accounts first
    Account.query.filter_by(category_id=cat_id).update({'category_id': None})
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
            return redirect(request.args.get('next') or url_for('dashboard'))
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
            old = acc.follower_count
            acc.follower_count = int(u['followers'])
            snap = AnalyticsSnapshot(account_id=acc.id, followers=acc.follower_count,
                                     recorded_at=datetime.utcnow())
            db.session.add(snap)
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
def content_templates():
    templates = ContentTemplate.query.order_by(ContentTemplate.use_count.desc()).all()
    categories = Category.query.order_by(Category.name).all()
    return render_template('content_templates.html', templates=templates,
                           categories=categories, active_page='content')

@app.route('/templates/new', methods=['POST'])
def template_new():
    d = request.form
    t = ContentTemplate(
        name=d['name'],
        category_id=int(d['category_id']) if d.get('category_id') else None,
        content_type=d.get('content_type','feed'),
        caption_template=d.get('caption_template',''),
        hashtags=d.get('hashtags',''),
        notes=d.get('notes',''),
    )
    db.session.add(t)
    db.session.commit()
    flash(f'Template "{t.name}" gespeichert.', 'success')
    return redirect(url_for('content_templates'))

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
    contact = d.get('contact', '').strip()

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
    for label, acc_id in [('a', id1), ('b', id2)]:
        if not acc_id:
            result[label] = None
            continue
        acc = Account.query.get(acc_id)
        if not acc:
            result[label] = None
            continue
        data, labels = [], []
        for i in range(days - 1, -1, -1):
            day = datetime.utcnow() - timedelta(days=i)
            snap = AnalyticsSnapshot.query.filter_by(account_id=acc_id)\
                .filter(func.date(AnalyticsSnapshot.recorded_at) == day.date())\
                .order_by(AnalyticsSnapshot.recorded_at.desc()).first()
            labels.append(day.strftime('%d.%m'))
            data.append(snap.followers if snap else None)
        result[label] = {
            'id': acc.id, 'name': acc.name,
            'followers': acc.follower_count,
            'labels': labels, 'data': data,
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
def content_auto_archive():
    days = request.get_json().get('days', 90)
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


# ─────────────────────── PRINT / POSTING PLAN ───────────────────────

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
