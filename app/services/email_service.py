from email.message import EmailMessage
import smtplib

from app.core.config import settings


def send_email(to_address: str, subject: str, body: str) -> None:
    if not settings.smtp_host or not settings.smtp_from:
        raise ValueError("SMTP not configured")

    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)

    server: smtplib.SMTP | smtplib.SMTP_SSL | None = None
    try:
        if settings.smtp_ssl:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
            server.ehlo()
            if settings.smtp_tls:
                server.starttls()
                server.ehlo()

        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password or "")

        server.send_message(message)
    finally:
        if server:
            server.quit()
