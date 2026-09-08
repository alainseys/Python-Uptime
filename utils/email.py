# utils/email.py
from flask_mail import Message
from extensions import mail
import urllib.parse

def send_notification(to_email, subject, template, app, **kwargs):
    description = kwargs.get('description', '').strip()
    server_id = kwargs.get('server_id')

    # === Build API unsubscribe URL ===
    if server_id:
        params = {'email': to_email, 'servers': [server_id]}
    else:
        params = {'email': to_email}  # unsubscribe from all

    query_string = urllib.parse.urlencode(params, doseq=True)
    unsub_url = f"{app.config['BASE_URL']}/api/unsubscribe?{query_string}"

    # === Email body ===
    if template == 'down':
        body = (
            f"Warning: {kwargs['server_name']} is DOWN\n\n"
            f"Status: {kwargs['status']}\n"
            f"Since: {kwargs['start_time']}\n"
        )
        if description:
            body += f"\nDetails:\n{description}\n"
        body += "\nPlease check the system immediately.\n\n"
    elif template == 'recovered':
        body = (
            f"Recovery: {kwargs['server_name']} is BACK ONLINE\n\n"
            f"Recovered at: {kwargs['recovered_at']}\n"
        )
        if description:
            body += f"\nDetails:\n{description}\n"
        body += "\nThe issue has been resolved.\n\n"
    else:
        body = "Status update.\n\n"

    body += f"Unsubscribe: {unsub_url}"

    msg = Message(
        subject=subject,
        recipients=[to_email],
        body=body,
        sender="noreply@vanmarcke.be"
    )

    log_msg = f"EMAIL → To: {to_email} | Subject: {subject} | Unsub: {unsub_url}"
    print(log_msg)
    app.logger.info(log_msg, extra={'user': 'system', 'action': 'email_sent'})

    try:
        mail.send(msg)
        print(f"EMAIL SENT → {to_email}")
        app.logger.info(f"EMAIL SUCCESS → {to_email}", extra={'user': 'system', 'action': 'email_success'})
    except Exception as e:
        print(f"EMAIL FAILED → {to_email} | {e}")
        app.logger.error(f"EMAIL FAILED → {to_email} | {e}", extra={'user': 'system', 'action': 'email_failed'})
