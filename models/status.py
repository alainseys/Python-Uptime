# models/status.py
from extensions import db

class Status(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    color = db.Column(db.String(7), nullable=False)
    icon = db.Column(db.String(50), nullable=True)
