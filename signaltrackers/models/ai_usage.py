"""
AI Usage Record Model

Database model for tracking AI usage metering data.
"""

from datetime import datetime

from extensions import db


class AIUsageRecord(db.Model):
    """Records each AI interaction for usage metering."""

    __tablename__ = 'ai_usage_records'

    id = db.Column(db.Integer, primary_key=True)
    # NULL for system calls (the scheduled weekly review), which belong to no
    # user and must not count against anyone's quota.
    user_id = db.Column(db.String(36), db.ForeignKey('users.id', ondelete='CASCADE'), nullable=True)
    interaction_type = db.Column(db.String(30), nullable=False)  # see VALID_INTERACTION_TYPES
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    input_tokens = db.Column(db.Integer, nullable=True)
    output_tokens = db.Column(db.Integer, nullable=True)
    cache_read_tokens = db.Column(db.Integer, nullable=True)
    cache_creation_tokens = db.Column(db.Integer, nullable=True)
    model = db.Column(db.String(100), nullable=False)
    estimated_cost = db.Column(db.Numeric(precision=12, scale=8), nullable=False)

    # Indexes for Admin Analytics access patterns
    __table_args__ = (
        db.Index('ix_ai_usage_records_user_id', 'user_id'),
        db.Index('ix_ai_usage_records_interaction_type', 'interaction_type'),
        db.Index('ix_ai_usage_records_timestamp', 'timestamp'),
        db.Index('ix_ai_usage_records_user_type_time', 'user_id', 'interaction_type', 'timestamp'),
    )

    # Relationship
    user = db.relationship('User', backref=db.backref('ai_usage_records', lazy='dynamic'))

    VALID_INTERACTION_TYPES = {
        'chatbot', 'portfolio_analysis', 'section_ai', 'sentence_drill_in',
        'scheduled_briefing',
    }

    def __repr__(self):
        return f'<AIUsageRecord {self.id} user={self.user_id} type={self.interaction_type}>'
