# admin_export_import.py
from flask import (
    request, flash, redirect, url_for,
    render_template, session, make_response
)
from extensions import db
from models import (
    Server, Status, StatusHistory, Subscriber,
    HttpCheck, PingCheck, ScheduledMaintenance, IssueReport
)
from models.subscribers import subscriber_server
import json
from datetime import datetime
import os
import tempfile


# === CONFIG ===
UPLOAD_FOLDER = 'temp_uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def register_routes(app):
    @app.route('/admin/export')
    def admin_export():
        """Export full DB as JSON (download)"""
        if 'username' not in session:
            flash('Please login first.')
            return redirect(url_for('login'))

        data = {
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "servers": [],
            "statuses": [],
            "status_history": [],
            "subscribers": [],
            "http_checks": [],
            "ping_checks": [],
            "scheduled_maintenances": [],
            "issue_reports": []
        }

        # --- Servers ---
        for s in Server.query.all():
            data["servers"].append({
                "id": s.id,
                "name": s.name,
                "current_status": s.current_status
            })

        # --- Statuses ---
        for s in Status.query.all():
            data["statuses"].append({
                "name": s.name,
                "color": s.color,
                "icon": s.icon
            })

        # --- History ---
        for h in StatusHistory.query.all():
            data["status_history"].append({
                "server_id": h.server_id,
                "start_time": h.start_time.isoformat() + "Z" if h.start_time else None,
                "end_time": h.end_time.isoformat() + "Z" if h.end_time else None,
                "status": h.status,
                "description": h.description,
                "username": h.username
            })

        # --- Subscribers ---
        for sub in Subscriber.query.options(db.joinedload(Subscriber.servers)).all():
            data["subscribers"].append({
                "email": sub.email,
                "servers": [s.id for s in sub.servers]
            })

        # --- HTTP Checks ---
        for c in HttpCheck.query.all():
            data["http_checks"].append({
                "server_id": c.server_id,
                "label": c.label,
                "url": c.url,
                "enabled": c.enabled,
                "last_checked": c.last_checked.isoformat() + "Z" if c.last_checked else None,
                "last_result": c.last_result
            })

        # --- Ping Checks ---
        for c in PingCheck.query.all():
            data["ping_checks"].append({
                "server_id": c.server_id,
                "label": c.label,
                "hostname": c.hostname,
                "enabled": c.enabled,
                "last_checked": c.last_checked.isoformat() + "Z" if c.last_checked else None,
                "last_result": c.last_result
            })

        # --- Maintenance ---
        for m in ScheduledMaintenance.query.all():
            data["scheduled_maintenances"].append({
                "server_id": m.server_id,
                "start_time": m.start_time.isoformat() + "Z",
                "end_time": m.end_time.isoformat() + "Z",
                "description": m.description,
                "is_active": m.is_active
            })

        # --- Reports ---
        for r in IssueReport.query.all():
            data["issue_reports"].append({
                "server_id": r.server_id,
                "description": r.description,
                "timestamp": r.timestamp.isoformat() + "Z"
            })

        # --- Export as JSON ---
        filename = f"status_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        json_str = json.dumps(data, indent=2, ensure_ascii=False)

        response = make_response(json_str)
        response.headers["Content-Disposition"] = f"attachment; filename={filename}"
        response.mimetype = "application/json"

        app.logger.info("Database exported", extra={'user': session['username'], 'action': 'db_export'})
        return response

    @app.route('/admin/import', methods=['GET', 'POST'])
    def admin_import():
        """Import full DB from JSON file with confirmation"""
        if 'username' not in session:
            flash('Please login first.')
            return redirect(url_for('login'))

        # === GET: Show upload form ===
        if request.method == 'GET':
            # Clean old temp file
            if 'temp_file' in session:
                try:
                    os.remove(session['temp_file'])
                except:
                    pass
                session.pop('temp_file', None)

            statuses = {s.name: {'color': s.color, 'icon': s.icon} for s in Status.query.all()}
            servers = Server.query.all()
            maintenances = ScheduledMaintenance.query.filter(
                (ScheduledMaintenance.end_time > datetime.utcnow()) | (ScheduledMaintenance.is_active == True)
            ).all()
            return render_template(
                'admin_import.html',
                STATUSES=statuses,
                servers=servers,
                maintenances=maintenances
            )

        # === POST: Handle file upload ===
        file = request.files.get('file')
        if not file or file.filename == '' or not file.filename.endswith('.json'):
            flash('Please upload a valid .json file.')
            return redirect(request.url)

        # Save file to temp
        fd, temp_path = tempfile.mkstemp(suffix='.json', dir=UPLOAD_FOLDER)
        file.save(temp_path)
        os.close(fd)
        session['temp_file'] = temp_path

        # === Show confirmation if not confirmed ===
        if not request.form.get('confirm_overwrite'):
            statuses = {s.name: {'color': s.color, 'icon': s.icon} for s in Status.query.all()}
            servers = Server.query.all()
            maintenances = ScheduledMaintenance.query.filter(
                (ScheduledMaintenance.end_time > datetime.utcnow()) | (ScheduledMaintenance.is_active == True)
            ).all()
            return render_template(
                'admin_import_confirm.html',
                filename=file.filename,
                STATUSES=statuses,
                servers=servers,
                maintenances=maintenances
            )

        # === FINAL IMPORT (confirmed) ===
        temp_path = session.get('temp_file')
        if not temp_path or not os.path.exists(temp_path):
            flash('Upload session expired. Please try again.')
            return redirect(url_for('admin_import'))

        try:
            with open(temp_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # === CLEAR DB ===
            db.session.query(IssueReport).delete()
            db.session.query(StatusHistory).delete()
            db.session.query(ScheduledMaintenance).delete()
            db.session.query(HttpCheck).delete()
            db.session.query(PingCheck).delete()
            db.session.query(subscriber_server).delete()
            db.session.query(Subscriber).delete()
            db.session.query(Server).delete()
            db.session.query(Status).delete()
            db.session.commit()

            # === RESTORE STATUS ===
            status_map = {}
            for s in data.get("statuses", []):
                status = Status(name=s["name"], color=s["color"], icon=s["icon"])
                db.session.add(status)
                db.session.flush()
                status_map[s["name"]] = status.name
            db.session.commit()

            # === RESTORE SERVERS ===
            server_map = {}
            for s in data.get("servers", []):
                server = Server(name=s["name"], current_status=s["current_status"])
                db.session.add(server)
                db.session.flush()
                server_map[s["id"]] = server.id
            db.session.commit()

            # === SUBSCRIBERS ===
            for sub in data.get("subscribers", []):
                subscriber = Subscriber(email=sub["email"])
                db.session.add(subscriber)
                db.session.flush()
                for old_sid in sub["servers"]:
                    new_sid = server_map.get(old_sid)
                    if new_sid:
                        srv = Server.query.get(new_sid)
                        if srv:
                            subscriber.servers.append(srv)
            db.session.commit()

            # === HTTP CHECKS ===
            for c in data.get("http_checks", []):
                new_sid = server_map.get(c["server_id"])
                if new_sid:
                    check = HttpCheck(
                        server_id=new_sid,
                        label=c["label"],
                        url=c["url"],
                        enabled=c["enabled"],
                        last_checked=datetime.fromisoformat(c["last_checked"].rstrip("Z")) if c["last_checked"] else None,
                        last_result=c["last_result"]
                    )
                    db.session.add(check)
            db.session.commit()

            # === PING CHECKS ===
            for c in data.get("ping_checks", []):
                new_sid = server_map.get(c["server_id"])
                if new_sid:
                    check = PingCheck(
                        server_id=new_sid,
                        label=c["label"],
                        hostname=c["hostname"],
                        enabled=c["enabled"],
                        last_checked=datetime.fromisoformat(c["last_checked"].rstrip("Z")) if c["last_checked"] else None,
                        last_result=c["last_result"]
                    )
                    db.session.add(check)
            db.session.commit()

            # === MAINTENANCE ===
            for m in data.get("scheduled_maintenances", []):
                new_sid = server_map.get(m["server_id"])
                if new_sid:
                    maint = ScheduledMaintenance(
                        server_id=new_sid,
                        start_time=datetime.fromisoformat(m["start_time"].rstrip("Z")),
                        end_time=datetime.fromisoformat(m["end_time"].rstrip("Z")),
                        description=m["description"],
                        is_active=m["is_active"]
                    )
                    db.session.add(maint)
            db.session.commit()

            # === HISTORY ===
            for h in data.get("status_history", []):
                new_sid = server_map.get(h["server_id"])
                if new_sid:
                    history = StatusHistory(
                        server_id=new_sid,
                        start_time=datetime.fromisoformat(h["start_time"].rstrip("Z")) if h["start_time"] else None,
                        end_time=datetime.fromisoformat(h["end_time"].rstrip("Z")) if h["end_time"] else None,
                        status=h["status"],
                        description=h["description"],
                        username=h["username"]
                    )
                    db.session.add(history)
            db.session.commit()

            # === REPORTS ===
            for r in data.get("issue_reports", []):
                new_sid = server_map.get(r["server_id"])
                if new_sid:
                    report = IssueReport(
                        server_id=new_sid,
                        description=r["description"],
                        timestamp=datetime.fromisoformat(r["timestamp"].rstrip("Z"))
                    )
                    db.session.add(report)
            db.session.commit()

            flash('Database imported successfully!')
            app.logger.info("DB import completed", extra={'user': session['username'], 'action': 'db_import'})

        except Exception as e:
            db.session.rollback()
            flash(f'Import failed: {str(e)}')
            app.logger.error(f"Import failed: {e}", extra={'user': session['username'], 'action': 'db_import_failed'})
        finally:
            # Clean up temp file
            if 'temp_file' in session:
                try:
                    os.remove(session['temp_file'])
                except:
                    pass
                session.pop('temp_file', None)

        return redirect(url_for('admin'))
