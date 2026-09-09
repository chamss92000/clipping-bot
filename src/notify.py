"""Notification e-mail (SMTP) — best-effort.

Envoie un récap quand le pipeline a déposé de nouveaux clips sur Drive.
Ne casse JAMAIS le pipeline : toute erreur d'envoi est loggée puis ignorée.

Config (env / secrets) :
  NOTIFY_EMAIL_ENABLE   active l'envoi (défaut false)
  NOTIFY_EMAIL_TO       destinataire
  SMTP_HOST / SMTP_PORT serveur (défaut smtp.gmail.com : 465 SSL / 587 STARTTLS)
  SMTP_USER             adresse d'envoi (compte Gmail)
  SMTP_PASSWORD         mot de passe d'APPLICATION Gmail (pas le mot de passe du compte)
"""

from __future__ import annotations

import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from config import settings
from src.utils.logging import get_logger

log = get_logger("notify")


def send_email(subject: str, html_body: str, text_body: str | None = None) -> bool:
    """Envoie un e-mail HTML (+ repli texte). Retourne True si envoyé."""
    if not settings.NOTIFY_EMAIL_ENABLE:
        return False
    to = settings.NOTIFY_EMAIL_TO
    user = settings.SMTP_USER
    pwd = settings.SMTP_PASSWORD
    if not (to and user and pwd):
        log.warning(
            "Notif e-mail activée mais SMTP_USER / SMTP_PASSWORD / NOTIFY_EMAIL_TO "
            "manquant — e-mail non envoyé."
        )
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("clipping-bot", user))
    msg["To"] = to
    msg.attach(MIMEText(text_body or "Voir la version HTML.", "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    host, port = settings.SMTP_HOST, settings.SMTP_PORT
    try:
        if port == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as srv:
                srv.login(user, pwd)
                srv.sendmail(user, [to], msg.as_string())
        else:  # 587 STARTTLS
            with smtplib.SMTP(host, port, timeout=30) as srv:
                srv.starttls(context=ssl.create_default_context())
                srv.login(user, pwd)
                srv.sendmail(user, [to], msg.as_string())
        log.info("Notif e-mail envoyée à %s", to)
        return True
    except Exception as exc:  # noqa: BLE001 - un échec d'e-mail ne casse rien
        log.warning("Notif e-mail échouée : %s", exc)
        return False


def _folder_url(fid: str | None) -> str | None:
    return f"https://drive.google.com/drive/folders/{fid}" if fid else None


def build_clips_email(clips: list[dict], folder_ids: dict[str, str | None]) -> tuple[str, str, str]:
    """Construit (subject, html, text) à partir des clips produits ce cycle."""
    n = len(clips)
    n_fr = sum(1 for c in clips if (c.get("market") or "intl") == "fr")
    n_intl = n - n_fr
    parts = []
    if n_fr:
        parts.append(f"{n_fr} FR")
    if n_intl:
        parts.append(f"{n_intl} intl")
    subject = f"🎬 clipping-bot : {n} nouveau(x) clip(s) prêt(s)" + (
        f" ({', '.join(parts)})" if parts else ""
    )

    label = {"fr": "🇫🇷 Français", "intl": "🌍 International"}
    html = ["<div style='font-family:system-ui,Arial,sans-serif;font-size:15px;color:#111'>"]
    html.append(f"<h2 style='margin:0 0 4px'>{n} nouveau(x) clip(s) déposé(s) sur Drive</h2>")
    text = [f"{n} nouveau(x) clip(s) déposé(s) sur Drive.", ""]

    for mk in ("fr", "intl"):
        mk_clips = [c for c in clips if (c.get("market") or "intl") == mk]
        if not mk_clips:
            continue
        url = _folder_url(folder_ids.get(mk))
        head = label.get(mk, mk)
        if url:
            html.append(
                f"<h3 style='margin:16px 0 6px'>{head} — "
                f"<a href='{url}'>ouvrir le dossier Drive</a></h3>"
            )
        else:
            html.append(f"<h3 style='margin:16px 0 6px'>{head}</h3>")
        text.append(f"== {head} ==" + (f"  {url}" if url else ""))
        html.append("<ul style='margin:0;padding-left:18px'>")
        for c in mk_clips:
            hook = c.get("hook", "(sans titre)")
            tags = " ".join(c.get("hashtags") or [])
            fname = c.get("clip", "")
            html.append(
                f"<li style='margin:6px 0'><b>{hook}</b><br>"
                f"<span style='color:#555'>fichier : <code>{fname}</code></span><br>"
                f"<span style='color:#555'>légende : {hook} {tags}</span></li>"
            )
            text.append(f"- {hook}\n  fichier : {fname}\n  légende : {hook} {tags}")
        html.append("</ul>")
        text.append("")

    html.append(
        "<p style='color:#888;font-size:13px;margin-top:16px'>"
        "Rappel : poste les clips sur TikTok à la main. Les clips de plus de 48h "
        "sont purgés automatiquement.</p></div>"
    )
    return subject, "\n".join(html), "\n".join(text)
