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

# -- Endpoint /qr -- gera QR code fresco para reconectar lorena-bot ao WhatsApp
from flask import Response as _Response
import urllib.request as _urllib_req
import json as _json

@app.route("/qr")
def qr_page():
        """Gera QR code fresco para conectar a instancia lorena-bot ao WhatsApp.
            A pagina se auto-atualiza a cada 45s (QR expira em ~60s).
                Acesse: https://web-production-18ec5.up.railway.app/qr
                    """
        evolution_url = os.getenv("EVOLUTION_API_URL", "")
        evolution_key = os.getenv("EVOLUTION_API_KEY", "")
        instance = os.getenv("EVOLUTION_INSTANCE", "lorena-bot")

    qr_img = ""
    status_msg = ""
    try:
                req = _urllib_req.Request(
                                f"{evolution_url}/instance/connect/{instance}",
                                headers={"apikey": evolution_key},
                )
                with _urllib_req.urlopen(req, timeout=10) as r:
                                data = _json.load(r)
                            qr_img = data.get("base64", "")
        if qr_img:
                        status_msg = "Escaneie com o celular da Lorena (5524988370406) > WhatsApp > Aparelhos conectados > Conectar aparelho"
else:
            status_msg = "QR nao disponivel -- instancia pode ja estar conectada ou aguarde e recarregue."
except Exception as exc:
        status_msg = f"Erro ao buscar QR: {exc}"

    img_tag = f'<img src="{qr_img}" style="width:320px;height:320px;display:block;margin:24px auto">' if qr_img else ""
    html = f"""<!DOCTYPE html>
    <html lang="pt-BR">
    <head>
      <meta charset="UTF-8">
        <meta http-equiv="refresh" content="45">
          <title>QR Code -- Lorena Bot</title>
            <style>
                body {{ background:#111; color:#eee; font-family:sans-serif; text-align:center; padding:40px; }}
                    h2 {{ color:#25D366; }}
                        p {{ max-width:500px; margin:0 auto 16px; font-size:14px; color:#aaa; }}
                            .status {{ background:#1a1a1a; border:1px solid #333; border-radius:8px;
                                           padding:16px; max-width:480px; margin:16px auto; font-size:13px; }}
                                             </style>
                                             </head>
                                             <body>
                                               <h2>Lorena Bot -- Reconexao WhatsApp</h2>
                                                 {img_tag}
                                                   <div class="status">{status_msg}</div>
                                                     <p>Esta pagina recarrega automaticamente a cada 45 segundos com um QR novo.</p>
                                                     </body>
                                                     </html>"""
    return _Response(html, mimetype="text/html")


# -- Dev
if __name__ == "__main__":
        port = int(os.getenv("PORT", 6001))
    app.run(host="0.0.0.0", port=port, debug=False)
