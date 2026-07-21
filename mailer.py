"""SMTP email sender. Uses stdlib smtplib over implicit TLS (port 465).

Configured for Gmail (smtp.gmail.com:465) but works with any SMTP host
that supports SMTPS + login auth. Use a Google *app password*, not your
account password.
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage


def send_code(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    from_email: str,
    from_name: str,
    to_email: str,
    callsign: str,
    code: str,
    ttl_min: int,
) -> None:
    msg = EmailMessage()
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = to_email
    msg["Subject"] = f"SREL Discord verification code for {callsign}"
    msg.set_content(
        f"Your SREL Discord verification code is:\n\n"
        f"    {code}\n\n"
        f"This code expires in {ttl_min} minutes and can be used once.\n"
        f"If you did not request this code, you can ignore this message.\n"
    )
    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15) as s:
        s.login(smtp_user, smtp_password)
        s.send_message(msg)
