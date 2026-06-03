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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
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

    follower_count = db.Column(db.Integer, default=0)
    following_count = db.Column(db.Integer, default=0)
    post_count = db.Column(db.Integer, default=0)

    status = db.Column(db.String(20), default='active')  # active, paused, error, inactive
    automation_level = db.Column(db.Integer, default=0)  # 0-4
    priority = db.Column(db.String(20), default='medium')  # low, medium, high, critical

    # Posting targets per type
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

    status = db.Column(db.String(30), default='draft')
    # draft, in_progress, ready, scheduled, published, archived, error

    author_id = db.Column(db.Integer, db.ForeignKey('team_member.id'))
    author = db.relationship('TeamMember', backref='content_items')

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
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

    caption = db.Column(db.Text)
    hashtags = db.Column(db.Text)
    post_type = db.Column(db.String(30), default='feed')  # feed, reel, story, carousel
    status = db.Column(db.String(30), default='scheduled')
    # scheduled, published, failed, draft, cancelled

    scheduled_at = db.Column(db.DateTime, nullable=False)
    published_at = db.Column(db.DateTime)
    error_message = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey('team_member.id'))

    # Instagram post ID after publishing
    external_post_id = db.Column(db.String(200))

    # Performance (filled after publishing)
    likes = db.Column(db.Integer)
    comments = db.Column(db.Integer)
    reach = db.Column(db.Integer)
    impressions = db.Column(db.Integer)
    saves = db.Column(db.Integer)


class AnalyticsSnapshot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.utcnow)
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
    resolved = db.Column(db.Boolean, default=False)
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
class ContentTemplate(db.Model):
    id               = db.Column(db.Integer, primary_key=True)
    name             = db.Column(db.String(200), nullable=False)
    category_id      = db.Column(db.Integer, db.ForeignKey('category.id'))
    content_type     = db.Column(db.String(30), default='feed')
    caption_template = db.Column(db.Text)   # {{TITEL}}, {{DATUM}} als Platzhalter
    hashtags         = db.Column(db.Text)
    notes            = db.Column(db.Text)
    use_count        = db.Column(db.Integer, default=0)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    category         = db.relationship('Category', backref='templates')


# ── Content Comments ────────────────────────────────────────────
class ContentComment(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    content_item_id = db.Column(db.Integer, db.ForeignKey('content_item.id'), nullable=False)
    user_id         = db.Column(db.Integer, db.ForeignKey('user.id'))
    text            = db.Column(db.Text, nullable=False)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    user            = db.relationship('User', backref='comments')
    content_item    = db.relationship('ContentItem', backref='comments')
