from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import json
import hashlib
import secrets

db = SQLAlchemy()

# Association tables
account_labels = db.Table('account_labels',
    db.Column('account_id', db.Integer, db.ForeignKey('account.id')),
    db.Column('label_id', db.Integer, db.ForeignKey('label.id'))
)

content_accounts = db.Table('content_accounts',
    db.Column('content_id', db.Integer, db.ForeignKey('content_item.id')),
    db.Column('account_id', db.Integer, db.ForeignKey('account.id'))
)

content_labels = db.Table('content_labels',
    db.Column('content_id', db.Integer, db.ForeignKey('content_item.id')),
    db.Column('label_id', db.Integer, db.ForeignKey('label.id'))
)


class Platform(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)  # instagram, tiktok, youtube...
    icon = db.Column(db.String(50))
    color = db.Column(db.String(20), default='#6366f1')
    accounts = db.relationship('Account', backref='platform', lazy=True)


class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    color = db.Column(db.String(20), default='#6366f1')
    icon = db.Column(db.String(50), default='folder')
    description = db.Column(db.Text)
    accounts = db.relationship('Account', backref='category', lazy=True)
    content_items = db.relationship('ContentItem', backref='category', lazy=True)


class Label(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    color = db.Column(db.String(20), default='#6366f1')


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    email = db.Column(db.String(200), unique=True)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(50), default='editor')
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)

    def set_password(self, password):
        salt = secrets.token_hex(16)
        self.password_hash = salt + ':' + hashlib.sha256((salt + password).encode()).hexdigest()

    def check_password(self, password):
        if ':' not in (self.password_hash or ''):
            return False
        salt, hashed = self.password_hash.split(':', 1)
        return hashlib.sha256((salt + password).encode()).hexdigest() == hashed


class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    action = db.Column(db.String(100))   # content_created, post_scheduled, alert_triggered …
    entity_type = db.Column(db.String(50))
    entity_id = db.Column(db.Integer)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    user = db.relationship('User', backref='activity')


class TeamMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False)
    role = db.Column(db.String(50), default='editor')  # owner, manager, editor, poster, analyst
    avatar_url = db.Column(db.String(500))
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Custom permissions JSON
    permissions = db.Column(db.Text, default='{}')

    def get_permissions(self):
        return json.loads(self.permissions or '{}')


class Account(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    handle = db.Column(db.String(200))
    profile_image_url = db.Column(db.String(500))
    platform_id = db.Column(db.Integer, db.ForeignKey('platform.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))
    team_member_id = db.Column(db.Integer, db.ForeignKey('team_member.id'))
    team_member = db.relationship('TeamMember', backref='accounts')
    labels = db.relationship('Label', secondary=account_labels, backref='accounts')

    profile_url = db.Column(db.String(500))  # z.B. https://instagram.com/darmstadtschau

    follower_count = db.Column(db.Integer, default=0)
    following_count = db.Column(db.Integer, default=0)
    post_count = db.Column(db.Integer, default=0)

    status = db.Column(db.String(20), default='active', index=True)  # active, paused, error, inactive
    automation_level = db.Column(db.Integer, default=0)  # 0-4
    priority = db.Column(db.String(20), default='medium')  # low, medium, high, critical

    # Posting-Frequenz: Intervall in Tagen (1 = täglich, 3.5 = 2x/Woche, 7 = 1x/Woche)
    posting_interval_days = db.Column(db.Float, default=1.0)

    # Legacy-Felder (bleiben für Kompatibilität)
    target_feed_per_day = db.Column(db.Float, default=1.0)
    target_story_per_day = db.Column(db.Float, default=2.0)
    target_reel_per_week = db.Column(db.Float, default=3.0)

    # Content stock targets
    min_stock_days = db.Column(db.Integer, default=3)
    optimal_stock_days = db.Column(db.Integer, default=14)
    max_stock_days = db.Column(db.Integer, default=30)

    last_post_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text)

    # Wachstumsziel
    growth_goal      = db.Column(db.Integer)
    growth_goal_date = db.Column(db.DateTime)

    # Kunden-Share-Link
    share_token = db.Column(db.String(64), unique=True)

    # Telegram
    telegram_chat_id = db.Column(db.String(100))  # Channel-ID z.B. -1001234567890

    # Layout / Canva
    canva_url         = db.Column(db.String(500))  # Link zum Canva-Ordner / Template
    layout_notes      = db.Column(db.Text)          # Layout-Hinweise (Farben, Schriften, Stil)
    page_persona      = db.Column(db.Text)          # Seiten-Persönlichkeit für Inspiration-KI

    # Relationships
    scheduled_posts = db.relationship('ScheduledPost', backref='account', lazy=True, cascade='all,delete')
    analytics = db.relationship('AnalyticsSnapshot', backref='account', lazy=True, cascade='all,delete')
    ai_config = db.relationship('AIConfig', backref='account', uselist=False, cascade='all,delete')
    automation_rules = db.relationship('AutomationRule', backref='account', lazy=True, cascade='all,delete')

    def feed_stock_days(self):
        now = datetime.utcnow()
        planned = ScheduledPost.query.filter_by(
            account_id=self.id, post_type='feed', status='scheduled'
        ).filter(ScheduledPost.scheduled_at >= now).count()
        if self.target_feed_per_day and self.target_feed_per_day > 0:
            return planned / self.target_feed_per_day
        return 0

    def stock_status(self):
        days = self.feed_stock_days()
        if days >= 14:
            return 'green'
        elif days >= 7:
            return 'yellow'
        elif days >= 3:
            return 'orange'
        return 'red'

    def stock_days_display(self):
        return round(self.feed_stock_days(), 1)

    def latest_analytics(self):
        return AnalyticsSnapshot.query.filter_by(account_id=self.id)\
            .order_by(AnalyticsSnapshot.recorded_at.desc()).first()


class AIConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)

    # Headline
    headline_min_words = db.Column(db.Integer, default=5)
    headline_max_words = db.Column(db.Integer, default=12)
    headline_style = db.Column(db.String(100), default='neutral')

    # Caption
    caption_min_words = db.Column(db.Integer, default=50)
    caption_max_words = db.Column(db.Integer, default=300)
    caption_tone = db.Column(db.String(100), default='informativ')
    caption_structure = db.Column(db.Text)
    caption_hashtags = db.Column(db.Integer, default=10)

    # Image style
    image_style = db.Column(db.String(100), default='news')
    primary_color = db.Column(db.String(20), default='#1a1a2e')
    accent_color = db.Column(db.String(20), default='#e94560')

    # Posting times (JSON list)
    posting_times = db.Column(db.Text, default='["09:00", "18:00"]')

    # Approval
    auto_approve = db.Column(db.Boolean, default=False)
    ai_model = db.Column(db.String(100), default='claude-sonnet-4-6')

    # System prompt / persona
    persona = db.Column(db.Text)

    def get_posting_times(self):
        return json.loads(self.posting_times or '["09:00","18:00"]')


class ContentItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(500), nullable=False)
    raw_text = db.Column(db.Text)
    caption = db.Column(db.Text)
    source_url = db.Column(db.String(1000))
    source_name = db.Column(db.String(200))
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))
    labels = db.relationship('Label', secondary=content_labels, backref='content_items')
    accounts = db.relationship('Account', secondary=content_accounts, backref='content_items')

    status = db.Column(db.String(30), default='draft', index=True)
    # draft, in_progress, ready, scheduled, published, archived, error

    author_id = db.Column(db.Integer, db.ForeignKey('team_member.id'))
    author = db.relationship('TeamMember', foreign_keys='ContentItem.author_id', backref='content_items')

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    published_at = db.Column(db.DateTime)

    # Media attached
    media_items = db.relationship('MediaItem', backref='content_item', lazy=True)
    scheduled_posts = db.relationship('ScheduledPost', backref='content_item', lazy=True)

    # AI generated fields
    ai_headline = db.Column(db.String(500))
    ai_caption = db.Column(db.Text)
    ai_score = db.Column(db.Float)  # relevance score
    is_duplicate = db.Column(db.Boolean, default=False)

    # Content type
    content_type = db.Column(db.String(30), default='feed')  # feed, reel, story, carousel

    # Freigabe-Workflow
    # approval_status: none | pending_review | approved | rejected
    approval_status = db.Column(db.String(20), default='none')
    reviewed_by_id  = db.Column(db.Integer, db.ForeignKey('team_member.id'), nullable=True)
    reviewed_by     = db.relationship('TeamMember', foreign_keys='ContentItem.reviewed_by_id')
    reviewed_at     = db.Column(db.DateTime)
    review_note     = db.Column(db.Text)


class MediaItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(500), nullable=False)
    original_filename = db.Column(db.String(500))
    file_type = db.Column(db.String(50))  # image, video, reel, story, template, logo
    mime_type = db.Column(db.String(100))
    file_size = db.Column(db.Integer)
    width = db.Column(db.Integer)
    height = db.Column(db.Integer)
    duration = db.Column(db.Float)

    url = db.Column(db.String(1000))
    thumbnail_url = db.Column(db.String(1000))
    storage_source = db.Column(db.String(50), default='local')  # local, drive, r2

    content_item_id = db.Column(db.Integer, db.ForeignKey('content_item.id'))
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey('team_member.id'))

    tags = db.Column(db.Text, default='[]')
    usage_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def get_tags(self):
        return json.loads(self.tags or '[]')


class ScheduledPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    content_item_id = db.Column(db.Integer, db.ForeignKey('content_item.id'))
    media_item_id = db.Column(db.Integer, db.ForeignKey('media_item.id'))
    media_ids     = db.Column(db.Text, default='[]')  # JSON-Liste für Carousel: [id, id, ...]

    def get_media_ids(self):
        import json
        try: return json.loads(self.media_ids or '[]')
        except: return []

    caption = db.Column(db.Text)
    hashtags = db.Column(db.Text)
    post_type = db.Column(db.String(30), default='feed', index=True)   # feed, reel, story, carousel
    status    = db.Column(db.String(30), default='scheduled', index=True)
    # scheduled, published, failed, draft, cancelled, disabled

    # Slot-Typ: wie dieser Tag behandelt wird
    slot_type = db.Column(db.String(20), default='fixed')
    # fixed    → bestimmter Post muss an dem Tag live
    # flexible → irgendein freier Post aus dem Vorrat wird genommen
    # disabled → kein Post, User postet selbst

    scheduled_at = db.Column(db.DateTime, nullable=False, index=True)
    published_at = db.Column(db.DateTime)
    error_message = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey('team_member.id'))

    # Instagram post ID after publishing
    external_post_id = db.Column(db.String(200))

    # Telegram
    telegram_sent_at = db.Column(db.DateTime)  # gesetzt sobald an Telegram gesendet

    # Performance (filled after publishing)
    likes = db.Column(db.Integer)
    comments = db.Column(db.Integer)
    reach = db.Column(db.Integer)
    impressions = db.Column(db.Integer)
    saves = db.Column(db.Integer)


class AnalyticsSnapshot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False, index=True)
    recorded_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    followers = db.Column(db.Integer, default=0)
    following = db.Column(db.Integer, default=0)
    posts = db.Column(db.Integer, default=0)
    avg_likes = db.Column(db.Float)
    avg_comments = db.Column(db.Float)
    avg_reach = db.Column(db.Float)
    engagement_rate = db.Column(db.Float)
    stories_count = db.Column(db.Integer)


class AutomationRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'))
    name = db.Column(db.String(200), nullable=False)
    rule_type = db.Column(db.String(50))  # city_news, confession, meme, food_warning, rss
    active = db.Column(db.Boolean, default=True)

    # Source config (JSON)
    source_config = db.Column(db.Text, default='{}')
    # Action config (JSON)
    action_config = db.Column(db.Text, default='{}')

    # Schedule
    run_interval_minutes = db.Column(db.Integer, default=60)
    last_run_at = db.Column(db.DateTime)
    next_run_at = db.Column(db.DateTime)
    run_count = db.Column(db.Integer, default=0)
    error_count = db.Column(db.Integer, default=0)
    last_error = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def get_source_config(self):
        return json.loads(self.source_config or '{}')

    def get_action_config(self):
        return json.loads(self.action_config or '{}')


class AutomationRunLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    rule_id = db.Column(db.Integer, db.ForeignKey('automation_rule.id'), nullable=False)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime)
    status = db.Column(db.String(20), default='running')  # running, success, error
    items_found = db.Column(db.Integer, default=0)
    items_created = db.Column(db.Integer, default=0)
    items_skipped = db.Column(db.Integer, default=0)
    error_message = db.Column(db.Text)
    rule = db.relationship('AutomationRule', backref='run_logs')


class SystemAlert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'))
    alert_type = db.Column(db.String(50))
    severity = db.Column(db.String(20), default='warning')
    message = db.Column(db.Text)
    resolved = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    resolved_at = db.Column(db.DateTime)
    account = db.relationship('Account', backref='alerts')


# ── Account Groups ──────────────────────────────────────────────
account_group_members = db.Table('account_group_members',
    db.Column('group_id',   db.Integer, db.ForeignKey('account_group.id')),
    db.Column('account_id', db.Integer, db.ForeignKey('account.id'))
)

class AccountGroup(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(200), nullable=False)
    color       = db.Column(db.String(20), default='#3b82f6')
    description = db.Column(db.Text)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    accounts    = db.relationship('Account', secondary=account_group_members, backref='groups')


# ── Content Templates ───────────────────────────────────────────
template_accounts = db.Table('template_accounts',
    db.Column('template_id', db.Integer, db.ForeignKey('content_template.id'), primary_key=True),
    db.Column('account_id',  db.Integer, db.ForeignKey('account.id'),          primary_key=True),
)

class ContentTemplate(db.Model):
    id               = db.Column(db.Integer, primary_key=True)
    name             = db.Column(db.String(200), nullable=False)
    category_id      = db.Column(db.Integer, db.ForeignKey('category.id'))
    content_type     = db.Column(db.String(30), default='feed')  # feed|reel|story|carousel
    caption_template = db.Column(db.Text)   # {{TITEL}}, {{DATUM}}, {{STADT}} als Platzhalter
    cta_template     = db.Column(db.Text)   # Call to Action, optional extra Block
    hashtags         = db.Column(db.Text)
    notes            = db.Column(db.Text)

    # Visuell
    preview_image    = db.Column(db.String(500))  # Dateiname im uploads-Ordner
    primary_color    = db.Column(db.String(20), default='')
    secondary_color  = db.Column(db.String(20), default='')
    image_ratio      = db.Column(db.String(10), default='1:1')   # 1:1 | 4:5 | 9:16 | 16:9
    style_notes      = db.Column(db.Text)  # Schriftart, Mood, Layout-Hinweise

    # Zeitplan-Empfehlung
    posting_days     = db.Column(db.Text, default='[]')  # JSON: ["Mon","Wed","Fri"]
    posting_time_pref = db.Column(db.String(10), default='')  # "09:00"

    # Ziel-Accounts (M2M über Hilfstabelle)
    target_accounts  = db.relationship('Account', secondary='template_accounts', backref='templates')

    use_count        = db.Column(db.Integer, default=0)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    category         = db.relationship('Category', backref='templates')

    def get_posting_days(self):
        return json.loads(self.posting_days or '[]')

    @property
    def ratio_px(self):
        """Liefert Pixel-Größe (Breite×Höhe) zum Bildformat."""
        return {'1:1': '1080×1080', '4:5': '1080×1350',
                '9:16': '1080×1920', '16:9': '1080×607'}.get(self.image_ratio, '1080×1080')


# ── Content Comments ────────────────────────────────────────────
class ContentComment(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    content_item_id = db.Column(db.Integer, db.ForeignKey('content_item.id'), nullable=False)
    user_id         = db.Column(db.Integer, db.ForeignKey('user.id'))
    text            = db.Column(db.Text, nullable=False)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    user            = db.relationship('User', backref='comments')
    content_item    = db.relationship('ContentItem', backref='comments')


# ── Hashtag Sets ────────────────────────────────────────────
class HashtagSet(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(200), nullable=False)
    hashtags    = db.Column(db.Text, nullable=False)  # roh, z.B. "#darmstadt #darmstadtschau"
    account_id  = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=True)  # NULL = global
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=True)
    use_count   = db.Column(db.Integer, default=0)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    account     = db.relationship('Account', backref='hashtag_sets')
    category    = db.relationship('Category', backref='hashtag_sets')


# ── NotificationSettings ─────────────────────────────────────
class NotificationSettings(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    email           = db.Column(db.String(300))
    low_stock_days  = db.Column(db.Integer, default=3)   # Alert wenn Vorrat < X Tage
    email_enabled   = db.Column(db.Boolean, default=False)
    updated_at      = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── In-App Notifications ─────────────────────────────────────
class AppNotification(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    type       = db.Column(db.String(40))   # low_stock | review_request | approved | rejected | info
    title      = db.Column(db.String(300))
    message    = db.Column(db.Text)
    link       = db.Column(db.String(500))  # optional click-through URL
    is_read    = db.Column(db.Boolean, default=False)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    account    = db.relationship('Account', backref='notifications')

# ── Account Automation Profile ───────────────────────────────
class AccountAutomationProfile(db.Model):
    """Konfiguriert den Content-Modus jedes Accounts: manuell oder automatisiert."""
    id          = db.Column(db.Integer, primary_key=True)
    account_id  = db.Column(db.Integer, db.ForeignKey('account.id'), unique=True, nullable=False)
    account     = db.relationship('Account', backref=db.backref('auto_profile', uselist=False))

    # Haupt-Modus
    mode        = db.Column(db.String(20), default='manual')  # manual | auto

    # Quelle (bei auto)
    source_type = db.Column(db.String(30), default='')  # rss | ai | citybot | template
    rss_url     = db.Column(db.String(500))
    ai_prompt   = db.Column(db.Text)
    ai_style    = db.Column(db.String(50), default='neutral')  # neutral | engaging | formal | casual
    citybot_key = db.Column(db.String(200))  # API-Key / Bot-ID des externen CityBots

    # Zeitplan
    posts_per_day    = db.Column(db.Float, default=1.0)
    preferred_times  = db.Column(db.Text, default='["09:00"]')  # JSON-Array

    # Format
    default_post_type = db.Column(db.String(20), default='feed')  # feed | story | reel
    caption_template  = db.Column(db.Text)
    hashtag_set_id    = db.Column(db.Integer, db.ForeignKey('hashtag_set.id'), nullable=True)
    hashtag_set       = db.relationship('HashtagSet', backref='auto_profiles')

    # Optionen
    auto_approve      = db.Column(db.Boolean, default=False)  # Freigabe ohne Review
    disable_stock_amp = db.Column(db.Boolean, default=False)  # Vorrats-Ampel aus

    notes      = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    def get_times(self):
        import json
        return json.loads(self.preferred_times or '["09:00"]')

    @property
    def is_auto(self):
        return self.mode == 'auto'


# ── App-weite Einstellungen (Key-Value-Store) ─────────────────
class AppSettings(db.Model):
    """Allgemeiner Key-Value-Store für API-Keys und globale Konfiguration."""
    id         = db.Column(db.Integer, primary_key=True)
    key        = db.Column(db.String(100), unique=True, nullable=False)
    value      = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── Wiederkehrende Posts ──────────────────────────────────────
class RecurringPost(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    content_item_id = db.Column(db.Integer, db.ForeignKey('content_item.id'))
    account_id      = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    scheduled_dates = db.Column(db.Text, default='[]')  # JSON-Array von YYYY-MM-DD Strings
    note            = db.Column(db.Text)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    content_item    = db.relationship('ContentItem', backref='recurring_posts')
    account         = db.relationship('Account', backref='recurring_posts')


# ── Meme Templates ───────────────────────────────────────────
class MemeTemplate(db.Model):
    """Ein hochgeladenes Meme-Template-Bild (Canva-Export)."""
    __tablename__ = 'meme_template'
    id                   = db.Column(db.Integer, primary_key=True)
    title                = db.Column(db.String(200))
    image_url            = db.Column(db.String(500))        # Cloudinary URL
    cloudinary_public_id = db.Column(db.String(200))        # für späteres Löschen
    source_city          = db.Column(db.String(100))        # z.B. "Darmstadt"
    notes                = db.Column(db.Text)
    meme_context         = db.Column(db.Text)               # JSON: Typ, Kern-Element, Ton, Zielgruppe
    created_at           = db.Column(db.DateTime, default=datetime.utcnow)
    variants             = db.relationship('MemeVariant', backref='template',
                                           lazy='select', cascade='all,delete')


class MemeVariant(db.Model):
    """Status + Claude-Vorschlag für eine Stadt-Variante eines Templates."""
    __tablename__ = 'meme_variant'
    id              = db.Column(db.Integer, primary_key=True)
    template_id     = db.Column(db.Integer, db.ForeignKey('meme_template.id'), nullable=False)
    city            = db.Column(db.String(100), nullable=False)   # z.B. "Frankfurt"
    status          = db.Column(db.String(20), default='pending')
    # pending → noch offen | done → Canva-Version fertig | skip → überspringen
    suggestion      = db.Column(db.Text)   # Claude-Vorschlag (Text/JSON)
    notes           = db.Column(db.Text)   # eigene Notizen
    content_item_id = db.Column(db.Integer, db.ForeignKey('content_item.id'), nullable=True)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at      = db.Column(db.DateTime, default=datetime.utcnow)


class InspirationSource(db.Model):
    """Eine Instagram-Seite die wir beobachten (Inspiration-Quellen)."""
    __tablename__ = 'inspiration_source'
    id         = db.Column(db.Integer, primary_key=True)
    username   = db.Column(db.String(100), nullable=False, unique=True)
    notes      = db.Column(db.Text)
    last_fetch = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Standard-Account: Posts von dieser Quelle gehen automatisch hierhin
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=True)
    posts      = db.relationship('InspirationPost', backref='source',
                                 lazy='select', cascade='all,delete')


class InspirationPost(db.Model):
    """Ein heruntergeladener Post von einer beobachteten Seite."""
    __tablename__ = 'inspiration_post'
    id              = db.Column(db.Integer, primary_key=True)
    source_id       = db.Column(db.Integer, db.ForeignKey('inspiration_source.id'), nullable=False)
    instagram_code  = db.Column(db.String(50), unique=True)   # Post-Shortcode
    image_url       = db.Column(db.String(1000))              # Original Instagram CDN URL
    thumbnail_url   = db.Column(db.String(1000))              # kleinere Version
    caption         = db.Column(db.Text)
    post_date       = db.Column(db.DateTime)
    media_type      = db.Column(db.String(20), default='image')  # image | video | carousel
    # Status: new=frisch | saved=will ich verwenden | ignored=nicht interessant | used=schon übernommen
    status          = db.Column(db.String(20), default='new', index=True)
    content_item_id = db.Column(db.Integer, db.ForeignKey('content_item.id'), nullable=True)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
