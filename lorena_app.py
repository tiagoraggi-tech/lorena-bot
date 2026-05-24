"""
lorena_app.py -- Entry point combinado para Railway.
Une lorena_webhook + lorena_admin em um unico Flask app na porta $PORT.
"""
import os
import secrets
import logging
from datetime import timedelta
from pathlib import Path
from flask import Flask
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# -- Auto-inicializar banco na startup
def _ensure_db():
    """Cria schema e seeds se o banco ainda nao existir."""
    db_path = os.getenv("DB_PATH", "/data/lorena.db")
    db_file = Path(db_path)
    try:
        import db_init
        if not db_file.exists():
            log.info("Banco nao encontrado em %s -- inicializando...", db_path)
            db_init.init_db()
            db_init.seed_default_instructions()
            log.info("Banco inicializado com sucesso.")
        else:
            # Garante schema atualizado mesmo se o arquivo ja existe
            db_init.init_db()
    except Exception as exc:
        log.error("Erro ao inicializar banco: %s", exc)

_ensure_db()

# -- App combinado
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=4)

# -- Rotas do webhook
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
app.add_url_rule(
    "/test-llm",
    endpoint="test_llm",
    view_func=_wh.test_llm,
    methods=["GET"],
)
app.add_url_rule(
    "/internal/set-instruction",
    endpoint="set_instruction_internal",
    view_func=_wh.set_instruction_internal,
    methods=["POST"],
)

# -- Rotas do painel admin
import lorena_admin as _adm

_admin_routes = [
    ("/admin/m/<token>",                         "magic_login",            _adm.magic_login,            ["GET"]),
    ("/admin/login",                             "login_page",             _adm.login_page,             ["GET"]),
    ("/admin/logout",                            "logout",                 _adm.logout,                 ["GET"]),
    ("/admin/",                                  "dashboard",              _adm.dashboard,              ["GET"]),
    ("/admin/instructions",                      "instructions_view",      _adm.instructions_view,      ["GET"]),
    ("/admin/instructions/new",                  "instruction_new",        _adm.instruction_new,        ["POST"]),
    ("/admin/instructions/<int:iid>/deactivate", "instruction_deactivate", _adm.instruction_deactivate, ["POST"]),
    ("/admin/bot/toggle",                        "bot_toggle",             _adm.bot_toggle,             ["POST"]),
    ("/admin/appointments",                      "appointments_view",      _adm.appointments_view,      ["GET"]),
]
for path, endpoint, view_func, methods in _admin_routes:
    app.add_url_rule(path, endpoint=endpoint, view_func=view_func, methods=methods)

# -- Dev
if __name__ == "__main__":
    port = int(os.getenv("PORT", 6001))
    app.run(host="0.0.0.0", port=port, debug=False)
