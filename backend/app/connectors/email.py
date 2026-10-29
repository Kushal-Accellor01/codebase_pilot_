"""Email through SMTP with STARTTLS (standard library only)."""
import asyncio
import smtplib
import ssl
from email.message import EmailMessage

from app.connectors.base import TestResult, redact


def _connect(host: str, port: int, user: str | None, password: str | None) -> smtplib.SMTP:
    server = smtplib.SMTP(host, port, timeout=20)
    server.ehlo()
    server.starttls(context=ssl.create_default_context())
    server.ehlo()
    if user and password:
        server.login(user, password)
    return server


def _send(host, port, user, password, sender, recipients, subject, body) -> None:
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, ", ".join(recipients), subject
    message.set_content(body)
    with _connect(host, port, user, password) as server:
        server.send_message(message)


async def send(*, host: str, port: int, user: str | None, password: str | None, sender: str, recipients: list[str],
               subject: str, body: str) -> TestResult:
    if not recipients:
        return TestResult(False, "No recipients are set")
    try:
        await asyncio.to_thread(_send, host, port, user, password, sender, recipients, subject, body)
    except (smtplib.SMTPException, OSError) as exc:
        return TestResult(False, redact(f"Email was not sent: {type(exc).__name__}: {exc}", password))
    return TestResult(True, f"Sent to {', '.join(recipients)}")


async def test_connection(*, host: str | None, port: int, user: str | None, password: str | None) -> TestResult:
    if not host:
        return TestResult(False, "SMTP_HOST is not set on the server")

    def check() -> None:
        with _connect(host, port, user, password):
            pass

    try:
        await asyncio.to_thread(check)
    except (smtplib.SMTPException, OSError) as exc:
        return TestResult(False, redact(f"Could not sign in to {host}: {type(exc).__name__}: {exc}", password))
    return TestResult(True, f"Signed in to {host}")
