# models/subscriber.py
from extensions import db
from datetime import datetime

class Subscriber(db.Model):
    __tablename__ = 'subscriber'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # many-to-many: which servers the user wants alerts for
    servers = db.relationship(
        'Server',
        secondary='subscriber_server',
        backref=db.backref('subscribers', lazy='dynamic')
    )

# association table
subscriber_server = db.Table(
    'subscriber_server',
    db.Column('subscriber_id', db.Integer, db.ForeignKey('subscriber.id'), primary_key=True),
    db.Column('server_id', db.Integer, db.ForeignKey('server.id'), primary_key=True)
)
