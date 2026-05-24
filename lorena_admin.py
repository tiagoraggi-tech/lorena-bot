"""
lorena_admin.py — Painel admin Flask (porta 6002)
Auth: magic link enviado por WhatsApp ao número da Jaqueline.
"""
import os
import sqlite3
import secrets
import logging
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash
from dotenv import load_dotenv
from lorena_instructions import (
    list_active_instructions, add_instruction, deactivate_instruction,
    is_bot_active, set_bot_status, get_bot_status, VALID_CATEGORIES,
)

load_dotenv()
log = logging.getLogger("lorena.admin")
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=4)

DB_PATH = os.getenv("DB_PATH", "/data/lorena.db")
JAQUELINE_PHONE = os.getenv("JAQUELINE_PHONE", "5524999025732")
MAGIC_LINK_TTL_HOURS = 24


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "authorized_phone" not in session:
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return wrapper


def generate_magic_link() -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(hours=MAGIC_LINK_TTL_HOURS)).isoformat()
    conn = _conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO magic_links (token, authorized_phone, expires_at) VALUES (?, ?, ?)",
                (token, JAQUELINE_PHONE, expires))
    conn.commit()
    conn.close()
    base = os.getenv("PUBLIC_BASE_URL", "http://localhost:6002")
    return f"{base}/admin/m/{token}"


@app.route("/admin/m/<token>")
def magic_login(token):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM magic_links WHERE token=? AND used=0", (token,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return render_template("error.html", message="Link inválido ou já usado",
                               detail="Peça novo link enviando /painel pelo seu WhatsApp."), 401
    if row["expires_at"] < datetime.utcnow().isoformat():
        conn.close()
        return render_template("error.html", message="Link expirado",
                               detail="Envie /painel pelo WhatsApp pra gerar novo."), 401
    cur.execute("UPDATE magic_links SET used=1, used_at=? WHERE token=?",
                (datetime.utcnow().isoformat(), token))
    conn.commit()
    conn.close()
    session.permanent = True
    session["authorized_phone"] = row["authorized_phone"]
    return redirect(url_for("dashboard"))


@app.route("/admin/login")
def login_page():
    return render_template("login.html"), 401


@app.route("/admin/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


@app.route("/admin/")
@login_required
def dashboard():
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM lorena_instructions WHERE active=1")
    active_instructions = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM appointments_log WHERE success=1 AND created_at > datetime('now', '-7 days')")
    appointments_week = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM handoffs_to_jaqueline WHERE created_at > datetime('now', '-7 days')")
    handoffs_week = cur.fetchone()["c"]
    cur.execute("SELECT * FROM bot_status WHERE id=1")
    status = dict(cur.fetchone())
    conn.close()
    return render_template("dashboard.html", active_instructions=active_instructions,
                           appointments_week=appointments_week, handoffs_week=handoffs_week,
                           bot_status=status)


@app.route("/admin/instructions")
@login_required
def instructions_view():
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM lorena_instructions ORDER BY active DESC, priority DESC, created_at DESC")
    instructions = [dict(r) for r in cur.fetchall()]
    conn.close()
    return render_template("instructions.html", instructions=instructions, categories=VALID_CATEGORIES)


@app.route("/admin/instructions/new", methods=["POST"])
@login_required
def instruction_new():
    text = request.form.get("instruction_text", "").strip()
    category = request.form.get("category", "GERAL")
    try:
        priority = int(request.form.get("priority", 5))
    except ValueError:
        priority = 5
    try:
        iid = add_instruction(text, category, priority,
                              created_by_phone=session["authorized_phone"], created_via="panel")
        flash(f"Instrução #{iid} criada.", "success")
    except Exception as e:
        flash(f"Erro: {e}", "error")
    return redirect(url_for("instructions_view"))


@app.route("/admin/instructions/<int:iid>/deactivate", methods=["POST"])
@login_required
def instruction_deactivate(iid):
    ok = deactivate_instruction(iid, by_phone=session["authorized_phone"])
    flash("Instrução desativada." if ok else "Não encontrada.", "success" if ok else "error")
    return redirect(url_for("instructions_view"))


@app.route("/admin/bot/toggle", methods=["POST"])
@login_required
def bot_toggle():
    current = is_bot_active()
    reason = request.form.get("reason", "Toggle via painel")
    set_bot_status(not current, session["authorized_phone"], reason)
    flash(f"Bot {'pausado' if current else 'ativado'}.", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin/appointments")
@login_required
def appointments_view():
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM appointments_log ORDER BY created_at DESC LIMIT 100")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return render_template("appointments.html", appointments=rows)


if __name__ == "__main__":
    port = int(os.getenv("ADMIN_PANEL_PORT", 6002))
    log.info("Painel admin Lorena na porta %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
