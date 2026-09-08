# models/status_history.py
from extensions import db

class StatusHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, db.ForeignKey('server.id'), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False, default=db.func.now())
    end_time = db.Column(db.DateTime)
    status = db.Column(db.String(50), nullable=False)
    description = db.Column(db.Text)
    username = db.Column(db.String(100), nullable=True)  # S
