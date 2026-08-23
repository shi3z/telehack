import logging
import smtplib
from email.header import Header
from email.mime.text import MIMEText

import config

log = logging.getLogger("telehack.mail")


def send_login_link(to_email: str, name: str, url: str):
    """ワンタイムログインURLをメール送信する。SMTP未設定時はログ出力のみ(開発モード)。"""
    subject = f"[{config.APP_NAME}] ログインリンク"
    body = (
        f"{name} 様\n\n"
        f"{config.APP_NAME} へのログインリンクです。\n"
        f"以下のURLを {config.LOGIN_TOKEN_TTL_MIN} 分以内に開いてください(1回のみ有効)。\n\n"
        f"{url}\n\n"
        f"心当たりがない場合はこのメールを破棄してください。\n"
    )
    if not config.SMTP_HOST or (config.SMTP_USER and not config.SMTP_PASSWORD):
        log.warning("SMTP未設定(開発モード) %s 宛のログインリンク: %s", to_email, url)
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"] = config.MAIL_FROM
    msg["To"] = to_email

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as smtp:
        if config.SMTP_STARTTLS:
            smtp.starttls()
        if config.SMTP_USER:
            smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
        smtp.sendmail(config.MAIL_FROM, [to_email], msg.as_string())
    log.info("ログインリンクを送信しました: %s", to_email)
