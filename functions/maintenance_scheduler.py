# maintenance_scheduler.py
from datetime import datetime,timezone
from extensions import db
from models import Server, StatusHistory, ScheduledMaintenance
import time

def maintenance_scheduler(app):
    """Background task to check and apply scheduled maintenance."""
    while True:
        with app.app_context():  # Use the passed app for context
            now = datetime.now(timezone.utc)
            pending_maintenances = ScheduledMaintenance.query.filter_by(is_active=False).filter(ScheduledMaintenance.start_time <= now).all()
            for maint in pending_maintenances:
                server = maint.server
                current_history = StatusHistory.query.filter_by(server_id=server.id, end_time=None).first()
                if current_history:
                    current_history.end_time = now
                history = StatusHistory(
                    server_id=server.id,
                    start_time=now,
                    status='Under Maintenance',
                    description=maint.description,
                    username='system'
                )
                db.session.add(history)
                server.current_status = 'Under Maintenance'
                maint.is_active = True
                db.session.commit()
                app.logger.info(
                    f"Activated maintenance for server {server.name}",
                    extra={'user': 'system', 'action': f'Activated maintenance for server: {server.name}'}
                )
            expired_maintenances = ScheduledMaintenance.query.filter(ScheduledMaintenance.end_time <= now).all()
            for maint in expired_maintenances:
                if maint.is_active:
                    server = maint.server
                    current_history = StatusHistory.query.filter_by(server_id=server.id, end_time=None).first()
                    if current_history:
                        current_history.end_time = now
                    history = StatusHistory(
                        server_id=server.id,
                        start_time=now,
                        status='Operational',
                        description='Maintenance completed.',
                        username='system'
                    )
                    db.session.add(history)
                    server.current_status = 'Operational'
                    maint.is_active = False
                    app.logger.info(
                        f"Deactivated maintenance for server {server.name}",
                        extra={'user': 'system', 'action': f'Deactivated maintenance for server: {server.name}'}
                    )
                db.session.delete(maint)
                db.session.commit()
                app.logger.info(
                    f"Deleted expired maintenance for server {server.name}",
                    extra={'user': 'system', 'action': f'Deleted expired maintenance for server: {server.name}'}
                )
        time.sleep(60)
