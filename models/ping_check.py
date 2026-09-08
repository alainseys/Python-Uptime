# models/ping_check.py
from extensions import db

class PingCheck(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, db.ForeignKey('server.id'), nullable=False)
    hostname = db.Column(db.String(200), nullable=False)
    label = db.Column(db.String(100), nullable=False)
    enabled = db.Column(db.Boolean, default=True)
    last_checked = db.Column(db.DateTime, nullable=True)
    last_result = db.Column(db.String(100), nullable=True)  # Stores check result (added for consistency)
