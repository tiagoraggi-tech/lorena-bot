"""
lorena_app.py — Entry point combinado para Railway.
Une lorena_webhook + lorena_admin em um único Flask app na porta $PORT.
"""
import os
import secrets
from datetime import timedelta
from flask import Flask
from dotenv import load_dotenv

load_dotenv()

# ── App combinado ──────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=4)

# ── Rotas do webhook ───────────────────────────────────────────────
import lorena_webhook as _wh

app.add_url_rule(
    "/webhook/messages-upsert",
    endpoint="messages_upsert",
    view_func=_wh.messages_upsert,
    methods=["POST"],
)
app.add_url_rule(
    "/health",
    endpoint="health",
    view_func=_wh.health,
    methods=["GET"],
)

# ── Rotas do painel admin ──────────────────────────────────────────
import lorena_admin as _adm

_admin_routes = [
    ("/admin/m/<token>",                         "magic_login",           _adm.magic_login,           ["GET"]),
    ("/admin/login",                             "login_page",            _adm.login_page,            ["GET"]),
    ("/admin/logout",                            "logout",                _adm.logout,                ["GET"]),
    ("/admin/",                                  "dashboard",             _adm.dashboard,             ["GET"]),
    ("/admin/instructions",                      "instructions_view",     _adm.instructions_view,     ["GET"]),
    ("/admin/instructions/new",                  "instruction_new",       _adm.instruction_new,       ["POST"]),
    ("/admin/instructions/<int:iid>/deactivate", "instruction_deactivate",_adm.instruction_deactivate,["POST"]),
    ("/admin/bot/toggle",                        "bot_toggle",            _adm.bot_toggle,            ["POST"]),
    ("/admin/appointments",                      "appointments_view",     _adm.appointments_view,     ["GET"]),
]
for path, endpoint, view_func, methods in _admin_routes:
    app.add_url_rule(path, endpoint=endpoint, view_func=view_func, methods=methods)

# ── Dev ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 6001))
    app.run(host="0.0.0.0", port=port, debug=False)
