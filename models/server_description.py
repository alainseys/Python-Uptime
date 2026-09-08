# models/server_description.py
from extensions import db
from datetime import datetime

class ServerDescription(db.Model):
    __tablename__ = 'server_descriptions'
    
    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, db.ForeignKey('server.id'), unique=True, nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    contact_info = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationship to Server
    server = db.relationship('Server', backref='description', lazy=True)
    
    def __repr__(self):
        return f'<ServerDescription {self.server.name if self.server else "Unknown"}>'
