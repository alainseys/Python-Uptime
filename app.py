# app.py
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from urllib.parse import quote_plus # special char handeling for db password
from flask_mail import Mail
from extensions import mail
from utils.email import send_notification
from models.subscribers import Subscriber, subscriber_server
from datetime import datetime, date, timedelta, timezone
import os
import threading
import time
import logging
from logging.handlers import RotatingFileHandler
import requests
from ping3 import ping
from dotenv import load_dotenv
import json
from admin_export_import import register_routes as register_export_import_routes
from functools import wraps

# Import db from extensions
from extensions import db

# Import functions from new modules
from functions.ldap import authenticate_user
from functions.maintenance_scheduler import maintenance_scheduler

app = Flask(__name__)


# === CONFIG ===
load_dotenv()
#app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///status.db'
POSTGRES_USER = os.getenv('POSTGRES_USER')
POSTGRES_PASSWORD = os.getenv('POSTGRES_PASSWORD')
POSTGRES_HOST = os.getenv('POSTGRES_HOST', 'localhost')
POSTGRES_PORT = os.getenv('POSTGRES_PORT', '5432')
POSTGRES_DB = os.getenv('POSTGRES_DB', 'statusdb')
# Build the database URI
db_uri = f"postgresql://{POSTGRES_USER}:{quote_plus(POSTGRES_PASSWORD)}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"

# Safety check – fail early if critical vars are missing
if not all([POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST, POSTGRES_DB]):
    raise ValueError(
        f"Missing required environment variables!\n"
        f"POSTGRES_USER: {POSTGRES_USER}\n"
        f"POSTGRES_PASSWORD: {'<hidden>' if POSTGRES_PASSWORD else 'MISSING'}\n"
        f"POSTGRES_HOST: {POSTGRES_HOST}\n"
        f"POSTGRES_DB: {POSTGRES_DB}"
    )
print(f"Using database URI: {db_uri}")  # ← Debug: confirm it's correct
app.config['SQLALCHEMY_DATABASE_URI'] = db_uri
app.config['SECRET_KEY'] = 'nXj2Ui1EP0scfmv8'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = True
app.config['BASE_URL'] = 'https://status.vanmarcke.be'


# Init Category
STATUS_CATEGORIES = {}
API_STATUS_CATEGORIES = f"{app.config['BASE_URL']}/api/status_categories"
try:
    r = request.gat(API_STATUS_CATEGORIES, timeout=5)
    if r.status_code == 200:
        STATUS_CATEGORIES = r.json()
    else:
        raise ValueError
except:
    # Fallback 
    STATUS_CATEGORIES = {
        "issues": ["Performance Issues", "Partial Outage", "Major Outage"],
        "investigations": ["Under investigation", "Identified", "Investigating"],
        "ok": ["Operational", "Fixed"],
        "maintenance": ["Under Maintenance"]
    }
# Global that replaces the old list_outages
OUTAGE_STATUSES = tuple(STATUS_CATEGORIES["issues"])

# Initialize extensions
db.init_app(app)
register_export_import_routes(app)

# === MAIL CONFIG (port 25, no auth) ===
app.config['MAIL_SERVER'] = 'relay-02.vanmarcke.be'
app.config['MAIL_PORT'] = 25
app.config['MAIL_USE_TLS'] = False
app.config['MAIL_USE_SSL'] = False
app.config['MAIL_USERNAME'] = None
app.config['MAIL_PASSWORD'] = None
from extensions import mail
mail.init_app(app)  # ← CRITICAL


# === NOTIFICATION THROTTLE ===
_last_down_email = {}  # {server_id: datetime}

# === CUSTOM FILTER ===
@app.template_filter('prettyjson')
def prettyjson_filter(value):
    return json.dumps(value, indent=2, ensure_ascii=False)

# === IMPORT MODELS AFTER DB ===
from models import Status, Server, StatusHistory, ScheduledMaintenance, HttpCheck, PingCheck
from models.issue_report import IssueReport
# Import the new ServerDescription model
from models.server_description import ServerDescription

# === LOGGING ===
LOG_DIR = 'logs'
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)
log_handler = RotatingFileHandler(os.path.join(LOG_DIR, 'admin_actions.log'), maxBytes=1000000, backupCount=5)
log_handler.setFormatter(
    logging.Formatter('%(asctime)s - %(levelname)s - User: %(user)s - Action: %(action)s',
                      defaults={'user': 'unknown', 'action': 'unknown'})
)
log_handler.setLevel(logging.INFO)
app.logger.addHandler(log_handler)
app.logger.setLevel(logging.INFO)

# === DECORATOR ===
def login_required(f):
    def wrap(*args, **kwargs):
        if 'username' not in session:
            flash('Please login first.')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    wrap.__name__ = f.__name__
    return wrap

# Import Helper
def admin_view(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session:
            flash('Please login first.')
            return redirect(url_for('login'))
        statuses = {s.name: {'color': s.color, 'icon': s.icon} for s in Status.query.all()}
        result = f(*args, **kwargs, STATUSES=statuses)
        return result
    return decorated

# === BACKGROUND CHECKER ===
def server_checker(app):
    while True:
        with app.app_context():
            now = datetime.utcnow()
            servers = Server.query.all()
            for server in servers:
                enabled_http_checks = [c for c in server.http_checks if c.enabled]
                enabled_pings = [c for c in server.ping_checks if c.enabled]
                total_checks = len(enabled_http_checks) + len(enabled_pings)
                if total_checks == 0:
                    continue
                active_maint = ScheduledMaintenance.query.filter_by(server_id=server.id, is_active=True).first()
                if active_maint:
                    continue
                failed = 0
                failed_http_labels = []
                failed_ping_labels = []
                for check in enabled_http_checks:
                    try:
                        resp = requests.get(check.url, timeout=10)
                        check.last_checked = now
                        check.last_result = f"Success (Status: {resp.status_code})" if resp.status_code == 200 else f"Failed (Status: {resp.status_code})"
                        if resp.status_code != 200:
                            failed += 1
                            failed_http_labels.append(check.label)
                    except requests.exceptions.RequestException as e:
                        check.last_checked = now
                        check.last_result = f"Failed ({str(e)})"
                        failed += 1
                        failed_http_labels.append(check.label)
                for check in enabled_pings:
                    try:
                        result = ping(check.hostname, timeout=2)
                        check.last_checked = now
                        check.last_result = "Success" if result is not None else "Failed (No response)"
                        if result is None:
                            failed += 1
                            failed_ping_labels.append(check.label)
                    except Exception as e:
                        check.last_checked = now
                        check.last_result = f"Failed ({str(e)})"
                        failed += 1
                        failed_ping_labels.append(check.label)
                if failed == 0:
                    new_status = 'Operational'
                elif failed == 1:
                    new_status = 'Partial Outage'
                else:
                    new_status = 'Major Outage'
                desc = f"Automated check: {failed} of {total_checks} failed (HTTP: {len(enabled_http_checks)}"
                if failed_http_labels:
                    desc += f" ({', '.join(failed_http_labels)})"
                desc += f", Ping: {len(enabled_pings)}"
                if failed_ping_labels:
                    desc += f" ({', '.join(failed_ping_labels)})"
                desc += ")"

                if new_status != server.current_status:
                    current_history = StatusHistory.query.filter_by(server_id=server.id, end_time=None).first()
                    if current_history:
                        current_history.end_time = now
                    history = StatusHistory(
                        server_id=server.id,
                        start_time=now,
                        status=new_status,
                        description=desc,
                        username='system'
                    )
                    db.session.add(history)
                    server.current_status = new_status
                    db.session.commit()

                    # --- NOTIFICATION LOGIC ---
                    
                    is_down = new_status in OUTAGE_STATUSES
                    was_down = server.current_status in OUTAGE_STATUSES

                    if is_down and not was_down:
                        last_sent = _last_down_email.get(server.id)
                        if last_sent is None or (now - last_sent) > timedelta(minutes=30):
                            _notify_down(server, now, description=desc)  # ← PASS DESC
                            _last_down_email[server.id] = now
                    elif not is_down and was_down:
                        _notify_recovery(server, now, description=desc)  # ← PASS DESC
        time.sleep(300)

def _notify_down(server, when, description=""):
    subs = Subscriber.query.join(subscriber_server).filter(
        subscriber_server.c.server_id == server.id).all()
    for sub in subs:
        send_notification(
            to_email=sub.email,
            subject=f"[DOWN] {server.name} is experiencing issues",
            template="down",
            app=app,
            server_name=server.name,
            status=server.current_status,
            start_time=when.strftime("%Y-%m-%d %H:%M UTC"),
            description=description,
            server_id=server.id
        )

def _notify_recovery(server, when, description=""):
    subs = Subscriber.query.join(subscriber_server).filter(
        subscriber_server.c.server_id == server.id).all()
    for sub in subs:
        send_notification(
            to_email=sub.email,
            subject=f"[UP] {server.name} is back online",
            template="recovered",
            app=app,
            server_name=server.name,
            recovered_at=when.strftime("%Y-%m-%d %H:%M UTC"),
            description=description,
            server_id=server.id
        )

# === CONTEXT PROCESSOR FOR SERVER DESCRIPTIONS ===
@app.context_processor
def inject_server_descriptions():
    """Make server descriptions available in all templates"""
    def get_server_description(server_id):
        return ServerDescription.query.filter_by(server_id=server_id, is_active=True).first()
    return dict(get_server_description=get_server_description)

# === ROUTES ===

@app.route('/')
def index():
    from functions.ldap import connect_ldap
    servers = Server.query.all()
    today = date.today()
    active_issues = StatusHistory.query.join(Server).filter(
        StatusHistory.end_time == None,
        StatusHistory.status != 'Operational'
    ).all()
    resolved_issues = StatusHistory.query.join(Server).filter(
        StatusHistory.end_time != None,
        StatusHistory.status != 'Operational',
        db.func.date(StatusHistory.end_time) == today
    ).all()
    scheduled_maintenances = ScheduledMaintenance.query.join(Server).filter(
        db.or_(
            db.func.date(ScheduledMaintenance.start_time) == today,
            db.func.date(ScheduledMaintenance.end_time) == today
        )
    ).all()
    timeline_issues = StatusHistory.query.join(Server).filter(
        db.func.date(StatusHistory.start_time) == today
    ).order_by(StatusHistory.start_time.desc()).all()
    now = datetime.utcnow()
    for issue in timeline_issues:
        time_diff = now - issue.start_time
        minutes = int(time_diff.total_seconds() / 60)
        if minutes < 60:
            issue.relative_time = f"{minutes} minutes ago"
        elif minutes < 1440:
            hours = minutes // 60
            issue.relative_time = f"{hours} hour{'s' if hours > 1 else ''} ago"
        else:
            days = minutes // 1440
            issue.relative_time = f"{days} day{'s' if days > 1 else ''} ago"
        if issue.username and issue.username != 'system':
            domain = 'vm.be'
            upn = f"{issue.username}@{domain}"
            conn, user_info = connect_ldap(upn, None)
            if user_info:
                issue.admin_name = f"{user_info['first_name']} {user_info['last_name']}"
            else:
                issue.admin_name = issue.username
            if conn:
                conn.unbind()
        else:
            issue.admin_name = 'System'
    statuses = {status.name: {'color': status.color, 'icon': status.icon} for status in Status.query.all()}
    return render_template('index.html', servers=servers, STATUSES=statuses,
                         active_issues=active_issues, resolved_issues=resolved_issues,
                         scheduled_maintenances=scheduled_maintenances, timeline_issues=timeline_issues)

@app.route('/server/<int:server_id>')
def server_details(server_id):
    from functions.ldap import connect_ldap
    server = Server.query.get_or_404(server_id)
    histories = StatusHistory.query.filter_by(server_id=server_id).order_by(StatusHistory.start_time.desc()).all()
    maintenances = ScheduledMaintenance.query.filter_by(server_id=server_id).filter(
        (ScheduledMaintenance.end_time > datetime.utcnow()) | (ScheduledMaintenance.is_active == True)
    ).all()
    for history in histories:
        if history.username and history.username != 'system':
            domain = 'vm.be'
            upn = f"{history.username}@{domain}"
            conn, user_info = connect_ldap(upn, None)
            if user_info:
                history.admin_name = f"{user_info['first_name']} {user_info['last_name']}"
            else:
                history.admin_name = history.username
            if conn:
                conn.unbind()
        else:
            history.admin_name = 'System'
    statuses = {status.name: {'color': status.color, 'icon': status.icon} for status in Status.query.all()}
    return render_template('server_details.html', server=server, histories=histories, maintenances=maintenances, STATUSES=statuses)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        is_authenticated, user_info = authenticate_user(username, password)
        if is_authenticated:
            session['username'] = username
            session['full_name'] = f"{user_info['first_name']} {user_info['last_name']}" if user_info else username
            app.logger.info(f"User {username} logged in", extra={'user': username, 'action': 'Logged in'})
            flash('Logged in successfully.')
            return redirect(url_for('admin'))
        flash('Invalid credentials or insufficient permissions.')
    return render_template('login.html')

@app.route('/logout')
def logout():
    username = session.get('username', 'unknown')
    session.pop('username', None)
    session.pop('full_name', None)
    app.logger.info(f"User {username} logged out", extra={'user': username, 'action': 'Logged out'})
    flash('Logged out.')
    return redirect(url_for('index'))

@app.route('/report_problem')
def report_problem_page():
    servers = Server.query.all()
    return jsonify([{'id': s.id, 'name': s.name} for s in servers])

@app.route('/admin')
@login_required
def admin():
    servers = Server.query.all()
    maintenances = ScheduledMaintenance.query.filter(
        (ScheduledMaintenance.end_time > datetime.utcnow()) | (ScheduledMaintenance.is_active == True)
    ).all()
    statuses = {status.name: {'color': status.color, 'icon': status.icon} for status in Status.query.all()}
    return render_template('admin.html', servers=servers, maintenances=maintenances, STATUSES=statuses)

@app.route('/admin/subscribers')
@login_required
def admin_subscribers():
    subs = Subscriber.query.options(db.joinedload(Subscriber.servers)).all()
    return render_template('admin/admin_subscribers.html', subscribers=subs)

@app.route('/admin/reports')
@login_required
def view_reports():
    reports = IssueReport.query.join(Server).order_by(IssueReport.timestamp.desc()).all()
    total_reports = len(reports)
    for report in reports:
        report.formatted_date = report.timestamp.strftime('%Y-%m-%d')
        report.formatted_time = report.timestamp.strftime('%H:%M:%S')
        report.request_body = {
            'server_id': report.server_id,
            'description': report.description or ''
        }
    return render_template('admin/reports.html', reports=reports, total_reports=total_reports)

@app.route('/admin/reset_reports', methods=['POST'])
@login_required
def reset_reports():
    if request.method != 'POST':
        flash('This action requires a POST request.')
        return redirect(url_for('admin'), code=405)
    now = datetime.utcnow()
    IssueReport.query.delete()
    servers = Server.query.all()
    for server in servers:
        if server.current_status != 'Operational':
            current_history = StatusHistory.query.filter_by(server_id=server.id, end_time=None).first()
            if current_history:
                current_history.end_time = now
            history = StatusHistory(
                server_id=server.id,
                start_time=now,
                status='Operational',
                description='Reset all issue reports and server statuses',
                username='system'
            )
            db.session.add(history)
            server.current_status = 'Operational'
    db.session.commit()
    app.logger.info("Reset all issue reports and server statuses to Operational", extra={'user': 'system', 'action': 'Reset issue reports and server statuses'})
    flash('All issue reports have been reset and server statuses set to Operational.')
    return redirect(url_for('admin'))

@app.route('/admin/update_status/<int:server_id>', methods=['GET', 'POST'])
@login_required
def update_status(server_id):
    server = Server.query.get_or_404(server_id)
    if request.method == 'POST':
        new_status = request.form['status']
        description = request.form['description']
        if not Status.query.filter_by(name=new_status).first():
            flash('Invalid status selected.')
            return redirect(url_for('update_status', server_id=server_id))

        # Close current history
        current_history = StatusHistory.query.filter_by(server_id=server_id, end_time=None).first()
        if current_history:
            current_history.end_time = datetime.utcnow()

        # Add new history
        now = datetime.utcnow()
        history = StatusHistory(
            server_id=server_id,
            start_time=now,
            status=new_status,
            description=description or f"Manual status update to {new_status}",
            username=session['username']
        )
        db.session.add(history)

        # Save new status
        old_status = server.current_status
        server.current_status = new_status
        db.session.commit()

        # === NOTIFY ON CHANGE ===
        
        is_down = new_status in OUTAGE_STATUSES
        was_down = old_status in OUTAGE_STATUSES

        if is_down and not was_down:
            last_sent = _last_down_email.get(server.id)
            if last_sent is None or (now - last_sent) > timedelta(minutes=30):
                _notify_down(server, now, description=description)
                _last_down_email[server.id] = now
                app.logger.info(f"Manual DOWN alert sent for {server.name}", extra={'user': session['username'], 'action': 'manual_down_alert'})
        elif not is_down and was_down:
            _notify_recovery(server, now, description=description)
            app.logger.info(f"Manual RECOVERY alert sent for {server.name}", extra={'user': session['username'], 'action': 'manual_recovery_alert'})
            app.logger.info(f"Updated status for server {server.name} from {old_status} to {new_status}", extra={'user': session['username'], 'action': f'Updated server status: {server.name} to {new_status}'})
        flash('Status updated and notifications sent.')
        return redirect(url_for('admin'))

    statuses = Status.query.all()
    return render_template('update_status.html', server=server, statuses=statuses)

@app.route('/admin/add_server', methods=['GET', 'POST'])
@login_required
def add_server():
    if request.method == 'POST':
        name = request.form['name']
        if Server.query.filter_by(name=name).first():
            flash('Server name already exists.')
            return redirect(url_for('add_server'))  # ← optional: stay on form
        else:
            try:
                server = Server(name=name, current_status='Under investigation')
                db.session.add(server)
                db.session.commit()  # ← this commits the transaction
                app.logger.info(f"Added new server {name}", extra={'user': session['username'], 'action': f'Added server: {name}'})
                flash('Server added successfully.')
            except Exception as e:
                db.session.rollback()
                app.logger.error(f"Error adding server {name}: {str(e)}", exc_info=True)
                flash(f'Error adding server: {str(e)}')
            return redirect(url_for('admin'))
    
    return render_template('admin/add_server.html')

@app.route('/admin/schedule_maintenance/<int:server_id>', methods=['GET', 'POST'])
@login_required
def schedule_maintenance(server_id):
    server = Server.query.get_or_404(server_id)
    if request.method == 'POST':
        start_time = datetime.strptime(request.form['start_time'], '%Y-%m-%dT%H:%M')
        end_time = datetime.strptime(request.form['end_time'], '%Y-%m-%dT%H:%M')
        description = request.form['description']
        if end_time <= start_time:
            flash('End time must be after start time.')
        elif start_time < datetime.utcnow():
            flash('Start time must be in the future.')
        else:
            maintenance = ScheduledMaintenance(
                server_id=server_id,
                start_time=start_time,
                end_time=end_time,
                description=description
            )
            db.session.add(maintenance)
            db.session.commit()
            app.logger.info(f"Scheduled maintenance for server {server.name}", extra={'user': session['username'], 'action': f'Scheduled maintenance for server: {server.name} from {start_time} to {end_time}'})
            flash('Maintenance scheduled.')
            return redirect(url_for('admin'))
    return render_template('schedule_maintenance.html', server=server)

@app.route('/admin/delete_maintenance/<int:maintenance_id>', methods=['POST'])
@login_required
def delete_maintenance(maintenance_id):
    maintenance = ScheduledMaintenance.query.get_or_404(maintenance_id)
    server = maintenance.server
    if maintenance.is_active:
        current_history = StatusHistory.query.filter_by(server_id=server.id, end_time=None).first()
        if current_history:
            current_history.end_time = datetime.utcnow()
        history = StatusHistory(
            server_id=server.id,
            start_time=datetime.utcnow(),
            status='Operational',
            description='Maintenance cancelled.',
            username=session['username']
        )
        db.session.add(history)
        server.current_status = 'Operational'
    db.session.delete(maintenance)
    db.session.commit()
    app.logger.info(f"Deleted maintenance for server {server.name}", extra={'user': session['username'], 'action': f'Deleted maintenance for server: {server.name}'})
    flash('Maintenance period deleted.')
    return redirect(url_for('admin'))

@app.route('/admin/edit_server/<int:server_id>', methods=['GET', 'POST'])
@login_required
def edit_server(server_id):
    server = Server.query.get_or_404(server_id)
    if request.method == 'POST':
        new_name = request.form['name']
        if Server.query.filter_by(name=new_name).first():
            flash('Server name already exists.')
        else:
            old_name = server.name
            server.name = new_name
            db.session.commit()
            app.logger.info(f"Updated server name from {old_name} to {new_name}", extra={'user': session['username'], 'action': f'Updated server name: {old_name} to {new_name}'})
            flash('Server name updated.')
            return redirect(url_for('admin'))
    return render_template('admin/edit_server.html', server=server)

@app.route('/admin/delete_server/<int:server_id>', methods=['POST'])
@login_required
def delete_server(server_id):
    server = Server.query.get_or_404(server_id)
    server_name = server.name
    maintenances = ScheduledMaintenance.query.filter_by(server_id=server_id).all()
    for maint in maintenances:
        db.session.delete(maint)
    histories = StatusHistory.query.filter_by(server_id=server_id).all()
    for history in histories:
        db.session.delete(history)
    http_checks = HttpCheck.query.filter_by(server_id=server_id).all()
    for check in http_checks:
        db.session.delete(check)
    ping_checks = PingCheck.query.filter_by(server_id=server_id).all()
    for check in ping_checks:
        db.session.delete(check)
    reports = IssueReport.query.filter_by(server_id=server_id).all()
    for report in reports:
        db.session.delete(report)
    db.session.delete(server)
    db.session.commit()
    app.logger.info(f"Deleted server {server_name}", extra={'user': session['username'], 'action': f'Deleted server: {server_name}'})
    flash('Server deleted successfully.')
    return redirect(url_for('admin'))

@app.route('/admin/manage_statuses', methods=['GET', 'POST'])
@login_required
def manage_statuses():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            status_name = request.form['status_name']
            color = request.form['color']
            icon = request.form.get('icon', None)
            if not status_name or not color:
                flash('Status name and color are required.')
            elif Status.query.filter_by(name=status_name).first():
                flash('Status name already exists.')
            else:
                status = Status(name=status_name, color=color, icon=icon)
                db.session.add(status)
                db.session.commit()
                app.logger.info(f"Added new status {status_name}", extra={'user': session['username'], 'action': f'Added status: {status_name}'})
                flash('Status added.')
                refresh_status_categories()
        elif action == 'edit':
            status_id = request.form['status_id']
            status = Status.query.get_or_404(status_id)
            new_name = request.form['status_name']
            color = request.form['color']
            icon = request.form.get('icon', None)
            if not new_name or not color:
                flash('Status name and color are required.')
            elif new_name != status.name and Status.query.filter_by(name=new_name).first():
                flash('Status name already exists.')
            else:
                old_name = status.name
                status.name = new_name
                status.color = color
                status.icon = icon
                db.session.commit()
                app.logger.info(f"Updated status from {old_name} to {new_name}", extra={'user': session['username'], 'action': f'Updated status: {old_name} to {new_name}'})
                flash('Status updated.')
                refresh_status_categories()
        elif action == 'delete':
            status_id = request.form['status_id']
            status = Status.query.get_or_404(status_id)
            if Server.query.filter_by(current_status=status.name).first() or StatusHistory.query.filter_by(status=status.name).first():
                flash('Cannot delete status; it is currently in use.')
            else:
                status_name = status.name
                db.session.delete(status)
                db.session.commit()
                app.logger.info(f"Deleted status {status_name}", extra={'user': session['username'], 'action': f'Deleted status: {status_name}'})
                flash('Status deleted.')
                refresh_status_categories()
        return redirect(url_for('manage_statuses'))
    statuses = Status.query.all()
    available_icons = [
        'fa-solid fa-circle-check', 'fa-solid fa-exclamation-triangle', 'fa-solid fa-plug-circle-exclamation',
        'fa-solid fa-circle-xmark', 'fa-solid fa-circle-question', 'fa-solid fa-wrench', 'fa-solid fa-bell',
        'fa-solid fa-bolt', 'fa-solid fa-gear', 'fa-solid fa-shield'
    ]
    return render_template('admin/manage_statuses.html', statuses=statuses, available_icons=available_icons)

@app.route('/admin/manage_http_checks/<int:server_id>', methods=['GET', 'POST'])
@login_required
def manage_http_checks(server_id):
    server = Server.query.get_or_404(server_id)
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            label = request.form.get('label')
            url = request.form.get('url')
            if not label or not url:
                flash('Label and URL are required.')
            else:
                check = HttpCheck(server_id=server_id, label=label, url=url, enabled=True)
                db.session.add(check)
                db.session.commit()
                app.logger.info(f"Added HTTP check for server {server.name}: {label}", extra={'user': session['username'], 'action': f'Added HTTP check: {label} for {server.name}'})
                flash('HTTP check added.')
        elif action == 'edit':
            check_id = request.form.get('check_id')
            check = HttpCheck.query.get_or_404(check_id)
            if check.server_id != server_id:
                flash('Unauthorized access.')
                return redirect(url_for('admin'))
            label = request.form.get('label')
            url = request.form.get('url')
            if not label or not url:
                flash('Label and URL are required.')
            else:
                old_label = check.label
                old_url = check.url
                check.label = label
                check.url = url
                db.session.commit()
                app.logger.info(f"Edited HTTP check for server {server.name}: {old_label} to {label}, URL: {old_url} to {url}", extra={'user': session['username'], 'action': f'Edited HTTP check: {old_label} to {label} for {server.name}'})
                flash('HTTP check updated.')
        elif action == 'toggle':
            check_id = request.form.get('check_id')
            check = HttpCheck.query.get_or_404(check_id)
            if check.server_id != server_id:
                flash('Unauthorized access.')
                return redirect(url_for('admin'))
            check.enabled = not check.enabled
            db.session.commit()
            app.logger.info(f"Toggled HTTP check for server {server.name}: {check.label} to {'enabled' if check.enabled else 'disabled'}", extra={'user': session['username'], 'action': f'Toggled HTTP check: {check.label} to {"enabled" if check.enabled else "disabled"} for {server.name}'})
            flash('HTTP check toggled.')
        elif action == 'delete':
            check_id = request.form.get('check_id')
            check = HttpCheck.query.get_or_404(check_id)
            if check.server_id != server_id:
                flash('Unauthorized access.')
                return redirect(url_for('admin'))
            db.session.delete(check)
            db.session.commit()
            app.logger.info(f"Deleted HTTP check for server {server.name}: {check.label}", extra={'user': session['username'], 'action': f'Deleted HTTP check: {check.label} for {server.name}'})
            flash('HTTP check deleted.')
        return redirect(url_for('manage_http_checks', server_id=server_id))
    checks = HttpCheck.query.filter_by(server_id=server_id).all()
    return render_template('admin/manage_http_checks.html', server=server, checks=checks)

@app.route('/admin/manage_ping_checks/<int:server_id>', methods=['GET', 'POST'])
@login_required
def manage_ping_checks(server_id):
    server = Server.query.get_or_404(server_id)
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            label = request.form.get('label')
            hostname = request.form.get('hostname')
            if not label or not hostname:
                flash('Label and hostname are required.')
            else:
                check = PingCheck(server_id=server_id, label=label, hostname=hostname, enabled=True)
                db.session.add(check)
                db.session.commit()
                app.logger.info(f"Added ping check for server {server.name}: {label}", extra={'user': session['username'], 'action': f'Added ping check: {label} for {server.name}'})
                flash('Ping check added.')
        elif action == 'toggle':
            check_id = request.form.get('check_id')
            check = PingCheck.query.get_or_404(check_id)
            if check.server_id != server_id:
                flash('Unauthorized access.')
                return redirect(url_for('admin'))
            check.enabled = not check.enabled
            db.session.commit()
            app.logger.info(f"Toggled ping check for server {server.name}: {check.label} to {'enabled' if check.enabled else 'disabled'}", extra={'user': session['username'], 'action': f'Toggled ping check: {check.label} to {"enabled" if check.enabled else "disabled"} for {server.name}'})
            flash('Ping check toggled.')
        elif action == 'delete':
            check_id = request.form.get('check_id')
            check = PingCheck.query.get_or_404(check_id)
            if check.server_id != server_id:
                flash('Unauthorized access.')
                return redirect(url_for('admin'))
            db.session.delete(check)
            db.session.commit()
            app.logger.info(f"Deleted ping check for server {server.name}: {check.label}", extra={'user': session['username'], 'action': f'Deleted ping check: {check.label} for {server.name}'})
            flash('Ping check deleted.')
        return redirect(url_for('manage_ping_checks', server_id=server_id))
    checks = PingCheck.query.filter_by(server_id=server_id).all()
    return render_template('admin/manage_ping_checks.html', server=server, checks=checks)

@app.route('/admin/force_check/<check_type>/<int:check_id>', methods=['POST'])
@login_required
def force_check(check_type, check_id):
    if check_type not in ['http', 'ping']:
        flash('Invalid check type.')
        return redirect(url_for('admin'))
    now = datetime.utcnow()
    if check_type == 'http':
        check = HttpCheck.query.get_or_404(check_id)
        check_type_name = 'HTTP'
        server = check.server
        try:
            resp = requests.get(check.url, timeout=10)
            check.last_checked = now
            check_result = resp.status_code == 200
            check.last_result = f"Success (Status: {resp.status_code})" if check_result else f"Failed (Status: {resp.status_code})"
        except requests.exceptions.RequestException as e:
            check.last_checked = now
            check_result = False
            check.last_result = f"Failed ({str(e)})"
    else:
        check = PingCheck.query.get_or_404(check_id)
        check_type_name = 'Ping'
        server = check.server
        try:
            result = ping(check.hostname, timeout=2)
            check.last_checked = now
            check_result = result is not None
            check.last_result = "Success" if check_result else "Failed (No response)"
        except Exception as e:
            check.last_checked = now
            check_result = False
            check.last_result = f"Failed ({str(e)})"
    enabled_http_checks = [c for c in server.http_checks if c.enabled]
    enabled_pings = [c for c in server.ping_checks if c.enabled]
    total_checks = len(enabled_http_checks) + len(enabled_pings)
    if total_checks == 0:
        flash('No enabled checks for this server.')
        return redirect(url_for(f'manage_{check_type}_checks', server_id=server.id))
    active_maint = ScheduledMaintenance.query.filter_by(server_id=server.id, is_active=True).first()
    if active_maint:
        flash('Cannot run checks during active maintenance.')
        return redirect(url_for(f'manage_{check_type}_checks', server_id=server.id))
    failed = 0
    failed_http_labels = []
    failed_ping_labels = []
    for http_check in enabled_http_checks:
        if http_check.id == check_id and check_type == 'http':
            if not check_result:
                failed += 1
                failed_http_labels.append(http_check.label)
        else:
            try:
                resp = requests.get(http_check.url, timeout=10)
                http_check.last_checked = now
                http_check.last_result = f"Success (Status: {resp.status_code})" if resp.status_code == 200 else f"Failed (Status: {resp.status_code})"
                if resp.status_code != 200:
                    failed += 1
                    failed_http_labels.append(http_check.label)
            except requests.exceptions.RequestException as e:
                http_check.last_checked = now
                http_check.last_result = f"Failed ({str(e)})"
                failed += 1
                failed_http_labels.append(http_check.label)
    for ping_check in enabled_pings:
        if ping_check.id == check_id and check_type == 'ping':
            if not check_result:
                failed += 1
                failed_ping_labels.append(ping_check.label)
        else:
            try:
                result = ping(ping_check.hostname, timeout=2)
                ping_check.last_checked = now
                ping_check.last_result = "Success" if result is not None else "Failed (No response)"
                if result is None:
                    failed += 1
                    failed_ping_labels.append(ping_check.label)
            except Exception as e:
                ping_check.last_checked = now
                ping_check.last_result = f"Failed ({str(e)})"
                failed += 1
                failed_ping_labels.append(ping_check.label)
    if failed == 0:
        new_status = 'Operational'
    elif failed == 1:
        new_status = 'Partial Outage'
    else:
        new_status = 'Major Outage'
    desc = f"Manual {check_type_name} check for {check.label}: {'Success' if check_result else 'Failed'} ({failed} of {total_checks} failed, HTTP: {len(enabled_http_checks)}"
    if failed_http_labels:
        desc += f" ({', '.join(failed_http_labels)})"
    desc += f", Ping: {len(enabled_pings)}"
    if failed_ping_labels:
        desc += f" ({', '.join(failed_ping_labels)})"
    desc += ")"
    if new_status != server.current_status:
        current_history = StatusHistory.query.filter_by(server_id=server.id, end_time=None).first()
        if current_history:
            current_history.end_time = now
        history = StatusHistory(
            server_id=server.id,
            start_time=now,
            status=new_status,
            description=desc,
            username=session['username']
        )
        db.session.add(history)
        server.current_status = new_status
    db.session.commit()
    app.logger.info(f"Manual {check_type_name} check for server {server.name}: {check.label} - {'Success' if check_result else 'Failed'}", extra={'user': session['username'], 'action': f'Manual {check_type_name} check: {check.label} for {server.name}'})
    flash(f"{check_type_name} check completed: {check.last_result}")
    return redirect(url_for(f'manage_{check_type}_checks', server_id=server.id))

# === SERVER DESCRIPTION ADMIN ROUTES ===

@app.route('/admin/descriptions')
@login_required
def manage_descriptions():
    """Main page to list all server descriptions"""
    servers = Server.query.all()
    # Get descriptions for each server
    descriptions = {}
    for server in servers:
        desc = ServerDescription.query.filter_by(server_id=server.id).first()
        descriptions[server.id] = desc
    return render_template('admin/manage_descriptions.html', 
                         servers=servers, 
                         descriptions=descriptions)

@app.route('/admin/description/<int:server_id>', methods=['GET', 'POST'])
@login_required
def edit_description(server_id):
    """Edit or create a server description"""
    server = Server.query.get_or_404(server_id)
    description = ServerDescription.query.filter_by(server_id=server_id).first()
    
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description_text = request.form.get('description', '').strip()
        contact_info = request.form.get('contact_info', '').strip()
        is_active = request.form.get('is_active') == 'on'
        
        if not title:
            flash('Title is required.', 'error')
            return redirect(url_for('edit_description', server_id=server_id))
        
        if not description_text:
            flash('Description is required.', 'error')
            return redirect(url_for('edit_description', server_id=server_id))
        
        if description:
            # Update existing
            description.title = title
            description.description = description_text
            description.contact_info = contact_info
            description.is_active = is_active
            action = 'updated'
        else:
            # Create new
            description = ServerDescription(
                server_id=server_id,
                title=title,
                description=description_text,
                contact_info=contact_info,
                is_active=is_active
            )
            db.session.add(description)
            action = 'created'
        
        db.session.commit()
        
        app.logger.info(f"{action} description for server {server.name}", 
                       extra={'user': session['username'], 
                              'action': f'{action} description for {server.name}'})
        
        flash(f'Description {action} successfully!', 'success')
        return redirect(url_for('manage_descriptions'))
    
    return render_template('admin/edit_description.html', 
                         server=server, 
                         description=description)

@app.route('/admin/description/delete/<int:server_id>', methods=['POST'])
@login_required
def delete_description(server_id):
    """Delete a server description"""
    description = ServerDescription.query.filter_by(server_id=server_id).first()
    if not description:
        flash('No description found for this server.', 'error')
        return redirect(url_for('manage_descriptions'))
    
    server_name = description.server.name if description.server else 'Unknown'
    db.session.delete(description)
    db.session.commit()
    
    app.logger.info(f"Deleted description for server {server_name}", 
                   extra={'user': session['username'], 
                          'action': f'Deleted description for {server_name}'})
    
    flash('Description deleted successfully!', 'success')
    return redirect(url_for('manage_descriptions'))

# === API ENDPOINTS (MUST BE BEFORE SERVER START) ===
@app.route('/api/servers', methods=['GET'])
def get_servers():
    servers = Server.query.all()
    return jsonify([{'id': s.id, 'name': s.name} for s in servers])

@app.route('/api/report', methods=['POST'])
def report_issue():
    data = request.get_json()
    server_id = data.get('server_id')
    description = data.get('description', '')
    if not server_id:
        return jsonify({'error': 'server_id is required'}), 400
    server = Server.query.get(server_id)
    if not server:
        return jsonify({'error': 'Server not found'}), 404
    report = IssueReport(server_id=server_id, description=description)
    db.session.add(report)
    db.session.commit()
    now = datetime.utcnow()
    time_threshold = now - timedelta(minutes=60)
    recent_reports_count = IssueReport.query.filter(
        IssueReport.server_id == server_id,
        IssueReport.timestamp >= time_threshold
    ).count()
    if recent_reports_count >= 5 and server.current_status == 'Operational':
        new_status = 'Under investigation'
        desc = f"User-reported issue threshold reached ({recent_reports_count} reports in last 60 minutes)"
        current_history = StatusHistory.query.filter_by(server_id=server_id, end_time=None).first()
        if current_history:
            current_history.end_time = now
        history = StatusHistory(
            server_id=server_id,
            start_time=now,
            status=new_status,
            description=desc,
            username='system'
        )
        db.session.add(history)
        server.current_status = new_status
        db.session.commit()
        app.logger.info(f"Automated status update for {server.name} to {new_status} due to user reports",
                        extra={'user': 'system', 'action': f'User reports threshold: {server.name} to {new_status}'})
    return jsonify({'success': True}), 200

@app.route('/api/status_categories', methods=['GET'])
def api_status_categories():
    """
    GET /api/status_categories
    Returns categorized status labels for outage detection logic.
    """
    # === Hard-coded mapping (safe, fast, no DB hit) ===
    categories = {
        "issues": [
            "Performance Issues",
            "Partial Outage",
            "Major Outage"
        ],
        "investigations": [
            "Under investigation",
            "Identified",
            "Investigating"
        ],
        "ok": [
            "Operational",
            "Fixed"
        ],
        "maintenance": [
            "Under Maintenance"
        ]
    }
    return jsonify(categories), 200

@app.route('/api/subscribers', methods=['GET'])
def api_list_subscribers():
    #auth = request.authorization
    #if not auth or auth.username != "admin" or auth.password != "password123":
    #        return jsonify({"error: Unauthorized"}), 401
    """
    GET /api/subscribers
    Returns:
        [
            {
                "email": "john@vm.be",
                "servers": [
                    {"id": 1, "name": "SAP"},
                    {"id": 2, "name": "NETWERK"}
                ]
            },
            ...]
        ]
    """
    subs = Subscriber.query.options(db.joinedload(Subscriber.servers)).all()
    result = []
    for sub in subs:
        result.append({
            "email": sub.email,
            "servers": [
                {"id": s.id, "name": s.name}
                for s in sub.servers
            ]
        })
    return jsonify(result), 200

@app.route('/api/subscribe', methods=['POST'])
def subscribe():
    data = request.get_json() or {}
    email = data.get('email')
    server_ids = data.get('servers', [])
    if not email or not server_ids:
        return jsonify({'error': 'email and servers required'}), 400
    sub = Subscriber.query.filter_by(email=email).first()
    if not sub:
        sub = Subscriber(email=email)
        db.session.add(sub)
    for sid in server_ids:
        srv = db.session.get(Server, sid)
        if srv and srv not in sub.servers:
            sub.servers.append(srv)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/unsubscribe', methods=['POST', 'GET'])
def unsubscribe():
    # === Handle GET from email links ===
    if request.method == 'GET':
        email = request.args.get('email')
        server_ids = request.args.getlist('servers')  # handles multiple
        if not email:
            return "Error: No email provided.", 400
        # Convert to list of int
        try:
            server_ids = [int(sid) for sid in server_ids if sid]
        except:
            server_ids = []
    else:
        # === Handle POST from frontend ===
        data = request.get_json() or {}
        email = data.get('email')
        server_ids = data.get('servers', [])

    if not email:
        return jsonify({'error': 'email required'}), 400

    sub = Subscriber.query.filter_by(email=email).first()
    if not sub:
        msg = f"No subscription found for {email}."
        return (msg, 200) if request.method == 'GET' else (jsonify({'success': True}), 200)

    removed = []
    if server_ids:
        for sid in server_ids:
            srv = db.session.get(Server, sid)
            if srv and srv in sub.servers:
                sub.servers.remove(srv)
                removed.append(srv.name)
    else:
        removed = [s.name for s in sub.servers]
        sub.servers = []

    db.session.commit()

    # === RETURN HTML FOR GET (email click) ===
    if request.method == 'GET':
        server_list = ', '.join(removed) if removed else "none"
        return f"""
        <h2>Unsubscribed Successfully!</h2>
        <p>You have been removed from: <strong>{server_list}</strong></p>
        <p><a href="{app.config['BASE_URL']}">Back to Status Page</a></p>
        """, 200
    else:
        return jsonify({'success': True}), 200
        
@app.route('/api/getstatus', methods=['GET'])
def get_status():
    now = datetime.now(timezone.utc)
    today = date.today()
    servers = Server.query.all()
    statuses = {s.name: {'color': s.color, 'icon': s.icon} for s in Status.query.all()}
    active_issues = StatusHistory.query.join(Server).filter(
        StatusHistory.end_time == None,
        StatusHistory.status != 'Operational'
    ).order_by(StatusHistory.start_time.desc()).all()
    resolved_today = StatusHistory.query.join(Server).filter(
        StatusHistory.status == 'Operational',
        db.func.date(StatusHistory.start_time) == today,
        db.or_(
            StatusHistory.description.contains('Automated check'),
            StatusHistory.description.contains('Manual'),
            StatusHistory.description.contains('User-reported'),
            StatusHistory.description.contains('Reset'),
            StatusHistory.description.contains('Maintenance')
        )
    ).order_by(StatusHistory.start_time.desc()).limit(10).all()
    scheduled_maintenances = ScheduledMaintenance.query.join(Server).filter(
        db.or_(
            ScheduledMaintenance.is_active == True,
            db.func.date(ScheduledMaintenance.start_time) == today,
            db.func.date(ScheduledMaintenance.end_time) == today
        )
    ).all()
    data = {
        "timestamp": now.isoformat() + "Z",
        "summary": {
            "total_servers": len(servers),
            "operational": sum(1 for s in servers if s.current_status == 'Operational'),
            "issues": sum(1 for s in servers if s.current_status != 'Operational')
        },
        "servers": [
            {
                "id": s.id,
                "name": s.name,
                "status": s.current_status,
                "status_info": statuses.get(s.current_status, {"color": "#6b7280", "icon": "fa-solid fa-circle"}),
                "in_maintenance": any(m.is_active for m in s.maintenances) if hasattr(s, 'maintenances') else False
            }
            for s in servers
        ],
        "active_issues": [
            {
                "server_id": issue.server.id,
                "server_name": issue.server.name,
                "status": issue.status,
                "status_color": statuses.get(issue.status, {}).get("color", "#6b7280"),
                "status_icon": statuses.get(issue.status, {}).get("icon", "fa-solid fa-circle"),
                "since": issue.start_time.isoformat() + "Z",
                "description": issue.description,
                "admin": issue.username if issue.username != 'system' else None
            }
            for issue in active_issues
        ],
        "resolved_today": [
            {
                "server_id": r.server.id,
                "server_name": r.server.name,
                "resolved_at": r.start_time.isoformat() + "Z",
                "description": r.description
            }
            for r in resolved_today
        ],
        "scheduled_maintenances": [
            {
                "server_id": m.server.id,
                "server_name": m.server.name,
                "start": m.start_time.strftime('%Y-%m-%d %H:%M UTC'),
                "end": m.end_time.strftime('%Y-%m-%d %H:%M UTC'),
                "description": m.description,
                "is_active": m.is_active
            }
            for m in scheduled_maintenances
        ]
    }
    return jsonify(data)

def refresh_status_categories():
    global OUTAGE_STATUSES, STATUS_CATEGORIES
    try:
        r = request.get(API_STATUS_CATEGORIES, timout=5)
        if r.status_code == 200:
            STATUS_CATEGORIES = r.json()
            OUTAGE_STATUSES = tuple(STATUS_CATEGORIES("issues"))
    except:
        pass # Keep the old values stored
        
# === DATABASE INIT ===
if not os.path.exists('status.db'):
    with app.app_context():
        db.create_all()
        if not Status.query.first():
            default_statuses = [
                ('Operational', '#10b981', 'fa-solid fa-circle-check'),
                ('Performance Issues', '#f59e0b', 'fa-solid fa-exclamation-triangle'),
                ('Partial Outage', '#f59e0b', 'fa-solid fa-plug-circle-exclamation'),
                ('Major Outage', '#ef4444', 'fa-solid fa-circle-xmark'),
                ('Under investigation', '#06b6d4', 'fa-solid fa-circle-question'),
                ('Under Maintenance', '#f59e0b', 'fa-solid fa-wrench'),
                ('Identified', '#f59e0b', 'fa-solid fa-bell'),
                ('Investigating', '#06b6d4', 'fa-solid fa-circle-question'),
                ('Fixed', '#10b981', 'fa-solid fa-circle-check')
            ]
            for name, color, icon in default_statuses:
                status = Status(name=name, color=color, icon=icon)
                db.session.add(status)
            db.session.commit()
        if not Server.query.first():
            default_servers = ['SAP', 'NETWORK', 'CITRIX', 'OFFICE365', 'OFFLINK', 'PHONE', 'SELF-SCAN', 'PRINTERS']
            for name in default_servers:
                server = Server(name=name, current_status='Operational')
                db.session.add(server)
            db.session.commit()
        # Fallback db descriptions
#            # END
            

            print("Initial server descriptions loaded.")

# === BACKGROUND THREADS ===
scheduler_thread = threading.Thread(target=maintenance_scheduler, args=(app,), daemon=True)
scheduler_thread.start()
server_checker_thread = threading.Thread(target=server_checker, args=(app,), daemon=True)
server_checker_thread.start()

# === START SERVER ===
if __name__ == '__main__':
    print("\n=== FLASK URL MAP ===")
    for rule in app.url_map.iter_rules():
        methods = ','.join(sorted(rule.methods))
        print(f"{methods:12} {rule}")
    print("======================\n")
    from waitress import serve
    serve(app, host="0.0.0.0", port=80)
