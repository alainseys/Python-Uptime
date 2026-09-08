# models/server.py
from extensions import db
from .status_history import StatusHistory
from .scheduled_maintenance import ScheduledMaintenance
from .http_check import HttpCheck
from .ping_check import PingCheck

class Server(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    current_status = db.Column(db.String(50), default='Unknown')
    histories = db.relationship('StatusHistory', backref='server', lazy=True)
    maintenances = db.relationship('ScheduledMaintenance', backref='server', lazy=True)
    http_checks = db.relationship('HttpCheck', backref='server', lazy=True)
    ping_checks = db.relationship('PingCheck', backref='server', lazy=True)
    reports = db.relationship('IssueReport', backref='server', lazy=True)
