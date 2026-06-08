"""
lorena_webhook.py — Webhook principal da Lorena
Recebe mensagens via Evolution API, classifica, processa, responde.
"""
import os
import json
import hashlib
import logging
import sqlite3
import threading
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, request, jsonify
try:
    from pyngrok import ngrok, conf
    _PYNGROK = True
except ImportError:
    _PYNGROK = False
from dotenv import load_dotenv
from groq import Groq

from db_init import migrate_db
from lorena_state import (
    hash_phone, get_or_create_session, update_session, add_to_history,
    get_session_by_hash, is_session_paused, pause_session,
)
from lorena_classifier import LorenaClassifier
from lorena_instructions import is_bot_active, handle_command, handle_admin_command, is_supervisor, list_active_instructions
from lorena_prompt import build_system_prompt
from consultorio_api import get_available_times, create_appointment, cancel_appointment, is_api_configured, find_next_available_slot, find_next_two_slots

load_dotenv()
migrate_db()  # garante coluna collected_document no banco existente
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("lorena.webhook")
app = Flask(__name__)

# ===== Config =====
LORENA_PHONE = os.getenv("LORENA_PHONE", "5524988370406")
JAQUELINE_PHONE = os.getenv("JAQUELINE_PHONE", "5524999025732")
TIAGO_PHONE = os.getenv("TIAGO_PHONE", "5521999249903")
PRESCRIPTION_BOT_PHONE = os.getenv("PRESCRIPTION_BOT_PHONE", "5524936181108")
EVOLUTION_URL = os.getenv("EVOLUTION_API_URL")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY")
DB_PATH = os.getenv("DB_PATH", "/data/lorena.db")
AUDIT_LOG = os.getenv("AUDIT_LOG", "/data/lorena_audit.jsonl")
Path(AUDIT_LOG).parent.mkdir(parents=True, exist_ok=True)

_groq_client = None
_classifier = None

# Modelos em ordem de preferência — tenta cada um até funcionar
GROQ_MODELS = [
    os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "llama-3.1-8b-instant",
]

def _get_groq_client() -> Groq:
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    return _groq_client


def call_groq(messages: list[dict], max_tokens: int = 800) -> str:
    """Chama o Groq SDK diretamente com fallback entre modelos."""
    client = _get_groq_client()
    last_err = None
    for model in GROQ_MODELS:
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.3,
                max_tokens=max_tokens,
            )
            log.info("Groq OK com modelo: %s", model)
            return resp.choices[0].message.content
        except Exception as e:
            log.warning("Groq modelo %s falhou: %s", model, e)
            last_err = e
    raise last_err


def _get_classifier():
    global _classifier
    if _classifier is None:
        _classifier = LorenaClassifier()
    return _classifier


# ===== Helpers =====

def audit(entry: dict):
    try:
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("Audit log falhou: %s", e)


def send_whatsapp_tracked(phone: str, text: str) -> bool:
    url = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"
    headers = {"Content-Type": "application/json", "apikey": EVOLUTION_API_KEY}
    ok = False
    try:
        r = requests.post(url, headers=headers, json={"number": phone, "text": text}, timeout=15)
        r.raise_for_status()
        ok = True
    except Exception as e:
        log.error("Send failed to %s: %s", phone[-4:], e)
    if ok:
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            text_hash = hashlib.sha256(text.encode()).hexdigest()
            cur.execute("""
                INSERT INTO bot_sent_messages (recipient_phone, message_text_hash, message_preview, source)
                VALUES (?, ?, ?, 'BOT_API')
            """, (phone, text_hash, text[:100]))
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning("Tracking falhou: %s", e)
    return ok


def is_message_from_bot(recipient_phone: str, message_text: str) -> bool:
    if not message_text:
        return True
    text_hash = hashlib.sha256(message_text.encode()).hexdigest()
    threshold = (datetime.utcnow() - timedelta(minutes=10)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT id FROM bot_sent_messages
        WHERE recipient_phone=? AND message_text_hash=? AND sent_at > ?
        LIMIT 1
    """, (recipient_phone, text_hash, threshold))
    row = cur.fetchone()
    conn.close()
    return row is not None


# ===== Webhook principal =====

@app.route("/webhook/messages-upsert", methods=["POST"])
def messages_upsert():
    try:
        data = request.get_json(force=True)
        msg_data = data.get("data", {})
        key = msg_data.get("key", {})
        phone = key.get("remoteJid", "").replace("@s.whatsapp.net", "")
        if not phone:
            return jsonify({"status": "no_phone"}), 200

        msg = msg_data.get("message", {})
        text = (msg.get("conversation") or
                msg.get("extendedTextMessage", {}).get("text") or "").strip()

        # Normalizar phone: remover sufixos extras (grupos, broadcast etc)
        phone = phone.split("@")[0] if "@" in phone else phone
        phone = phone.replace("-", "").strip()
        # Remover sufixo de dispositivo multi-device WhatsApp (ex: "5524999025732:4" → "5524999025732")
        if ":" in phone:
            phone = phone.split(":")[0]

        # fromMe=True: mensagem enviada PELO chip da Lorena/Jaqueline.
        # Jaqueline e o bot usam o mesmo aparelho — comandos "/" devem ser processados normalmente.
        if key.get("fromMe"):
            if text and text.strip().startswith("/"):
                # Jaqueline digitou um comando no aparelho compartilhado
                log.info("Comando de Jaqueline (aparelho compartilhado): %s", text[:80])
                try:
                    response = handle_command(text.strip(), JAQUELINE_PHONE)
                    log.info("Comando processado: %s", response[:80])
                except Exception as e:
                    log.exception("Erro ao processar comando fromMe: %s", e)
                return jsonify({"status": "command_fromme_processed"}), 200

            if text and not is_message_from_bot(phone, text):
                # Jaqueline entrou no diálogo manualmente → pausar bot para esse paciente
                ph_manual = hash_phone(phone)
                pause_until = (datetime.utcnow() + timedelta(minutes=15)).isoformat()
                pause_session(ph_manual, pause_until)
                log.info("Jaqueline entrou manualmente com *%s — sessão pausada por 15min", phone[-4:])
            return jsonify({"status": "ignored_self"}), 200

        # Supervisores (Jaqueline hardcoded + dinâmicos via /incluir)
        if is_supervisor(phone):
            log.info("Supervisor (phone=*%s texto='%s')", phone[-4:], text[:80])
            response = handle_command(text, phone)
            log.info("Resposta para supervisor: %s", response[:80])
            send_whatsapp_tracked(phone, response)
            audit({"ts": datetime.utcnow().isoformat(), "event": "supervisor_command",
                   "phone": phone[-4:], "command": text[:100]})
            return jsonify({"status": "command_processed"}), 200

        # Ignorar mensagens de grupos, broadcasts e status
        if len(phone) > 15 or not phone.isdigit():
            return jsonify({"status": "ignored_non_individual"}), 200

        # Admin master — Dr. Tiago (acesso total + comandos exclusivos)
        _tiago_norm = TIAGO_PHONE.strip().lstrip("+")
        if phone == _tiago_norm or phone.endswith(_tiago_norm[-8:]):
            log.info("Admin master (%s): %s", phone[-4:], text[:80])
            response = handle_admin_command(text, phone)
            send_whatsapp_tracked(TIAGO_PHONE, response)
            return jsonify({"status": "admin_command_processed"}), 200

        # Bot pausado?
        if not is_bot_active():
            log.info("Bot pausado — ignorando mensagem de *%s", phone[-4:])
            return jsonify({"status": "bot_paused"}), 200

        return handle_patient_message(phone, text)

    except Exception as e:
        log.exception("Webhook error: %s", e)
        return jsonify({"status": "error", "detail": str(e)}), 500


def handle_patient_message(phone: str, text: str):
    ph = hash_phone(phone)
    session = get_or_create_session(phone)

    if is_session_paused(ph):
        log.info("Sessão *%s pausada — ignorando", phone[-4:])
        return jsonify({"status": "session_paused"}), 200
    if not text:
        return jsonify({"status": "no_text"}), 200

    add_to_history(ph, "user", text)

    # Atalho direto para confirmação/rejeição de slot — não passa pelo LLM
    if session.get("current_state") == "AWAITING_CONFIRMATION":
        _t = text.lower().strip().rstrip("!.")
        _confirm = {"sim", "s", "ok", "pode", "pode ser", "ótimo", "otimo",
                    "confirmo", "confirmado", "quero", "esse", "esse mesmo",
                    "1", "primeiro", "primeira", "opcao 1", "opção 1", "a primeira"}
        _reject  = {"não", "nao", "n", "outro", "outro dia", "outra data",
                    "2", "segundo", "segunda", "opcao 2", "opção 2", "a segunda"}
        # Fuzzy: "prefiro na quarta", "quarta", "segunda" → mapeia pelo dia do slot
        if _t not in _confirm and _t not in _reject:
            _day_map = {"segunda": 0, "terca": 1, "quarta": 2, "quinta": 3, "sexta": 4}
            _slots2 = session.get("available_slots") or []
            from datetime import datetime as _dtf
            _matched = None
            for _dw, _wn in _day_map.items():
                if _dw in _t:
                    for _si2, _sl2 in enumerate(_slots2[:2]):
                        try:
                            _sdt = _dtf.fromisoformat(_sl2["DateTime"].replace("Z", "+00:00"))
                            if _sdt.weekday() == _wn:
                                _matched = _si2
                                break
                        except Exception:
                            pass
                    if _matched is not None:
                        break
            if _matched is not None:
                update_session(ph, current_slot_index=_matched)
                return handle_slot_confirmation(phone, ph)
            # Ainda em AWAITING_CONFIRMATION mas nao entendeu — re-oferece
            return offer_slot(phone, ph)

        if _t in _confirm:
            return handle_slot_confirmation(phone, ph)
        if _t in _reject:
            # Se há duas opções na sessão e o paciente escolheu a segunda
            _slots = session.get("available_slots") or []
            if len(_slots) >= 2:
                from datetime import datetime as _dt
                try:
                    _da = _dt.fromisoformat(_slots[0]["DateTime"].replace("Z", "+00:00"))
                    _db = _dt.fromisoformat(_slots[1]["DateTime"].replace("Z", "+00:00"))
                    if _da.date() != _db.date():
                        # "não" à primeira opção → confirma a segunda
                        update_session(ph, current_slot_index=1)
                        return handle_slot_confirmation(phone, ph)
                except Exception:
                    pass
            return offer_next_slot(phone, ph)

    intent_result = _get_classifier().classify(text, session.get("current_state", "NEW"))
    intent = intent_result["intent"]

    log.info("*%s [%s] → %s (%s/%s)", phone[-4:], session.get("current_state"),
             intent, intent_result["confidence"], intent_result["source"])
    audit({"ts": datetime.utcnow().isoformat(), "event": "patient_message",
           "patient_last4": phone[-4:], "state": session.get("current_state"),
           "intent": intent, "confidence": intent_result["confidence"],
           "source": intent_result["source"], "preview": text[:80]})

    if intent == "DUVIDA_CLINICA":
        return redirect_to_prescription_bot(phone, ph, text)
    if intent == "FALAR_HUMANO":
        return handoff_to_jaqueline(phone, ph, "patient_request",
                                    subject="solicitação ao atendimento humano")
    if intent == "AGRADECIMENTO":
        send_whatsapp_tracked(phone, "😊 Foi um prazer ajudar! Qualquer coisa, estou por aqui.")
        return jsonify({"status": "small_talk"}), 200

    if intent == "CANCELAR_CONSULTA":
        return handle_cancel_intent(phone, ph, session)

    if intent == "REMARCAR_CONSULTA":
        return handle_remarcar_intent(phone, ph, session)

    return process_with_llm(phone, ph, text, session)


def process_with_llm(phone: str, ph: str, text: str, session: dict):
    history = session.get("conversation_history", []) or []
    messages = [{"role": "system", "content": build_system_prompt()}]
    for item in history[-6:]:
        role = item.get("role", "user")
        content = item.get("content", "")
        # Groq aceita "user" e "assistant"
        messages.append({"role": role if role in ("user", "assistant") else "user", "content": content})
    # Garante que a mensagem atual do paciente está sempre no final
    if not messages or messages[-1].get("role") != "user" or messages[-1].get("content") != text:
        messages.append({"role": "user", "content": text})
    try:
        raw = call_groq(messages)
    except Exception as e:
        log.error("LLM falhou (todos os modelos): %s", e)
        send_whatsapp_tracked(phone, "Desculpe, tive um problema técnico. Pode repetir em alguns segundos?")
        return jsonify({"status": "llm_error"}), 200
    return process_llm_response(phone, ph, raw, session)


def process_llm_response(phone: str, ph: str, raw: str, session: dict):
    text = raw.strip()

    if "AGENDAR:" in text:
        try:
            payload = json.loads(text.split("AGENDAR:", 1)[1].strip())
        except Exception:
            send_whatsapp_tracked(phone, "Desculpe, houve um erro. Pode repetir as informações?")
            return jsonify({"status": "agendar_parse_error"}), 200
        return handle_appointment_request(phone, ph, payload)

    if "PROXIMO_SLOT" in text:
        return offer_next_slot(phone, ph)

    if "CANCELAR:" in text:
        try:
            payload = json.loads(text.split("CANCELAR:", 1)[1].strip())
            return handle_cancel(phone, ph, payload.get("id"))
        except Exception:
            send_whatsapp_tracked(phone, "Não identifiquei o ID. Pode confirmar?")
            return jsonify({"status": "cancel_parse_error"}), 200

    if "FALAR_HUMANA:" in text:
        try:
            payload = json.loads(text.split("FALAR_HUMANA:", 1)[1].strip())
            return handoff_to_jaqueline(phone, ph, "bot_failure",
                                        patient_name=payload.get("nome"),
                                        subject=payload.get("assunto"))
        except Exception:
            return handoff_to_jaqueline(phone, ph, "bot_failure")

    if "CONFIRMAR_HORARIO" in text:
        slot_index = 0
        if "CONFIRMAR_HORARIO:" in text:
            try:
                payload = json.loads(text.split("CONFIRMAR_HORARIO:", 1)[1].strip())
                opcao = int(payload.get("opcao", 1))
                slot_index = max(0, opcao - 1)  # 1-based → 0-based
            except Exception:
                pass
        update_session(ph, current_slot_index=slot_index)
        return handle_slot_confirmation(phone, ph)

    if "BUSCAR_PROXIMO:" in text:
        try:
            payload = json.loads(text.split("BUSCAR_PROXIMO:", 1)[1].strip())
            nome = payload.get("nome", "").strip()
            cpf = payload.get("cpf", "").strip()
            is_retorno = bool(payload.get("retorno", False))
            price_info = payload.get("valor", "").strip()
            dia_preferido = payload.get("dia_preferido", "").strip().lower() or None
        except Exception:
            nome = session.get("collected_name", "")
            cpf = session.get("collected_document", "")
            is_retorno = False
            price_info = session.get("collected_price_info", "")
            dia_preferido = None
        telefone = phone
        add_to_history(ph, "assistant", "Deixa eu verificar a próxima vaga disponível...")
        send_whatsapp_tracked(phone, "Deixa eu verificar a próxima vaga disponível...")

        # Bloco C: busca 2 dias distintos respeitando preferência de dia
        two_days = find_next_two_slots(preferred_weekday=dia_preferido)
        log.info("Bloco C: find_next_two_slots=%d dia(s) (pref=%s)", len(two_days), dia_preferido)

        if two_days:
            # Monta lista: 1 slot de cada dia
            stored_slots = [d["slots"][0] for d in two_days if d.get("slots")]

            # Se só veio 1 dia, busca o 2º dia manualmente
            if len(stored_slots) < 2:
                first_date = two_days[0]["date"]
                second_day = find_next_available_slot(after_date=first_date)
                if isinstance(second_day, dict) and "slots" in second_day:
                    stored_slots.append(second_day["slots"][0])
                    log.info("Bloco C: 2º dia buscado manualmente: %s", second_day["date"])

            next_date = two_days[0]["date"]
            update_session(ph, current_state="AWAITING_CONFIRMATION",
                           collected_name=nome, collected_phone=telefone,
                           collected_document=cpf,
                           collected_price_info=price_info,
                           collected_date=next_date, available_slots=stored_slots,
                           current_slot_index=0,
                           is_retorno=1 if is_retorno else 0)
            return offer_slot(phone, ph)
        else:
            send_whatsapp_tracked(phone,
                "Não encontrei vagas disponíveis nos próximos dias. "
                "Vou chamar a Jaqueline pra te ajudar!")
            return handoff_to_jaqueline(phone, ph, "no_slots", patient_name=nome,
                                        subject="Sem vagas via busca automática")
        return jsonify({"status": "next_slot_searched"}), 200

    send_whatsapp_tracked(phone, text)
    add_to_history(ph, "assistant", text)
    return jsonify({"status": "responded"}), 200


def handle_appointment_request(phone: str, ph: str, payload: dict):
    nome = payload.get("nome", "").strip()
    telefone = phone  # sempre usa o WhatsApp de quem está conversando
    cpf = payload.get("cpf", "").strip()
    data = payload.get("data", "").strip()
    is_retorno = bool(payload.get("retorno", False))

    try:
        weekday = datetime.strptime(data, "%Y-%m-%d").weekday()
        if weekday not in [0, 2]:  # seg=0, qua=2
            send_whatsapp_tracked(phone,
                f"A data {data} não é segunda ou quarta-feira.\n"
                f"Atualmente atendemos apenas segundas e quartas à tarde.\n"
                f"Pode escolher outra data?")
            return jsonify({"status": "invalid_day"}), 200
    except Exception:
        send_whatsapp_tracked(phone,
            "Não consegui interpretar a data. Pode informar no formato YYYY-MM-DD? Ex: 2026-05-26")
        return jsonify({"status": "invalid_date_format"}), 200

    if not is_api_configured():
        send_whatsapp_tracked(phone,
            f"Anotei seus dados, {nome}! 😊\n"
            f"Vou pedir pra Jaqueline confirmar disponibilidade pra {data} e voltar pra você.")
        return handoff_to_jaqueline(phone, ph, "bot_failure", patient_name=nome,
                                    subject=f"Confirmar disponibilidade {data} pra {nome} ({telefone})")

    slots = get_available_times(data)
    if isinstance(slots, dict) and "error" in slots or not slots:
        # API indisponível ou sem slots — confirma dados ao paciente e encaminha Jaqueline
        data_br = datetime.strptime(data, "%Y-%m-%d").strftime("%d/%m/%Y")
        send_whatsapp_tracked(phone,
            f"Ótimo, {nome}! 😊 Anotei sua solicitação:\n"
            f"📅 Data desejada: *{data_br}*\n"
            f"📱 Telefone: *{telefone}*\n\n"
            f"Nossa atendente Jaqueline vai confirmar a disponibilidade e retornar pra você em breve!")
        return handoff_to_jaqueline(phone, ph, "scheduling", patient_name=nome,
                                    subject=f"Agendar {data_br} — {nome} ({telefone})")


    update_session(ph, current_state="AWAITING_CONFIRMATION",
                   collected_name=nome, collected_phone=telefone,
                   collected_document=cpf,
                   collected_date=data, available_slots=slots,
                   current_slot_index=0,
                   is_retorno=1 if is_retorno else 0)
    return offer_slot(phone, ph)


def offer_slot(phone: str, ph: str):
    session = get_session_by_hash(ph)
    slots = session.get("available_slots") or []
    idx = session.get("current_slot_index", 0)

    # Bloco C: apresenta dois horários em dias diferentes quando disponíveis
    if idx == 0 and len(slots) >= 2:
        try:
            dt_a = datetime.fromisoformat(slots[0]["DateTime"].replace("Z", "+00:00"))
            dt_b = datetime.fromisoformat(slots[1]["DateTime"].replace("Z", "+00:00"))
            if dt_a.date() != dt_b.date():
                weekday_short = {0: "segunda", 1: "terça", 2: "quarta",
                                 3: "quinta", 4: "sexta", 5: "sábado", 6: "domingo"}
                wd_a = weekday_short.get(dt_a.weekday(), "")
                wd_b = weekday_short.get(dt_b.weekday(), "")
                offer_msg = (
                    f"Tenho horário na {wd_a} {dt_a.strftime('%d/%m')} às {dt_a.strftime('%H:%M')} "
                    f"ou na {wd_b} {dt_b.strftime('%d/%m')} às {dt_b.strftime('%H:%M')}. Qual prefere?"
                )
                send_whatsapp_tracked(phone, offer_msg)
                add_to_history(ph, "assistant", offer_msg)
                update_session(ph, current_state="AWAITING_CONFIRMATION")
                return jsonify({"status": "two_slots_offered"}), 200
        except Exception as e:
            log.warning("offer_slot: erro ao montar two-options, fallback p/ single: %s", e)

    if idx >= len(slots):
        # Slots do dia esgotados — busca automaticamente o próximo dia disponível
        session = get_session_by_hash(ph)
        current_date = session.get("collected_date")
        result = find_next_available_slot(after_date=current_date)
        if isinstance(result, dict) and "slots" in result:
            next_date = result["date"]
            next_date_br = result["date_br"]
            next_weekday = result["weekday"]
            next_slots = result["slots"]
            update_session(ph, current_state="AWAITING_CONFIRMATION",
                           collected_date=next_date, available_slots=next_slots, current_slot_index=0)
            return offer_slot(phone, ph)
        # API falhou ou sem vagas — encaminha pra Jaqueline
        send_whatsapp_tracked(phone,
            "Não encontrei vagas disponíveis nos próximos dias. "
            "Vou chamar a Jaqueline pra te ajudar com o agendamento! 😊")
        update_session(ph, current_state="NEW", available_slots=None, current_slot_index=0)
        nome = session.get("collected_name", "")
        return handoff_to_jaqueline(phone, ph, "no_slots", patient_name=nome,
                                    subject="Sem vagas disponíveis via API")


    slot = slots[idx]
    try:
        dt = datetime.fromisoformat(slot["DateTime"].replace("Z", "+00:00"))
        time_str = dt.strftime("%H:%M")
        date_str = dt.strftime("%d/%m/%Y")
        weekday_names = {0: "segunda-feira", 1: "terça-feira", 2: "quarta-feira",
                         3: "quinta-feira", 4: "sexta-feira", 5: "sábado", 6: "domingo"}
        weekday = weekday_names.get(dt.weekday(), "")
    except Exception:
        time_str = slot.get("DateTime", "?")
        date_str = ""
        weekday = ""

    weekday_label = f"{weekday} " if weekday else ""
    offer_msg = f"Tenho horário na {weekday_label}{date_str} às {time_str}. Pode ser?"
    send_whatsapp_tracked(phone, offer_msg)
    add_to_history(ph, "assistant", offer_msg)
    update_session(ph, current_state="AWAITING_CONFIRMATION")
    return jsonify({"status": "slot_offered"}), 200


def handle_slot_confirmation(phone: str, ph: str):
    """Paciente confirmou o slot oferecido — cria o agendamento."""
    session = get_session_by_hash(ph)
    nome = session.get("collected_name", "")
    telefone = session.get("collected_phone", "")
    cpf = session.get("collected_document", "")
    slots = session.get("available_slots") or []
    idx = session.get("current_slot_index", 0)

    if not slots or idx >= len(slots):
        # Sem slot disponível na sessão — volta a oferecer
        return offer_slot(phone, ph)

    slot = slots[idx]
    dt_raw = slot.get("DateTime", "")
    slot_id = slot.get("TimeSlotId", "")

    if not is_api_configured():
        return handoff_to_jaqueline(phone, ph, "api_not_configured", patient_name=nome,
                                    subject=f"Confirmar agendamento {dt_raw} — {nome} ({telefone})")

    result = create_appointment(nome, telefone, dt_raw, slot_id, document=cpf)
    if isinstance(result, dict) and result.get("success"):
        try:
            dt = datetime.fromisoformat(dt_raw.replace("Z", "+00:00"))
            date_label = dt.strftime("%d/%m/%Y")
            time_label = dt.strftime("%H:%M")
        except Exception:
            date_label = dt_raw
            time_label = ""
        appt_id = result.get("appointment_id", "")
        is_retorno = bool(session.get("is_retorno", 0))
        price_info = session.get("collected_price_info", "") or ""
        tipo_label = "Retorno (gratuito)" if is_retorno else "Consulta regular"
        retorno_line = "\n💚 *Consulta de retorno — gratuita!*" if is_retorno else ""
        # Confirmacao para o paciente
        send_whatsapp_tracked(phone,
            f"✅ Consulta confirmada!\n"
            f"📅 *{date_label}* às *{time_label}*\n"
            f"🏥 Shopping 33, Torre 3, Sala 1502 — Vila Santa Cecília, VR"
            f"{retorno_line}\n\n"
            f"Se precisar cancelar ou reagendar, é só me chamar! 😊")
        # Valor para linha da notificação
        if is_retorno:
            valor_line = "💰 Valor: *Retorno gratuito*"
        elif price_info:
            valor_line = f"💰 Valor: *{price_info}*"
        else:
            valor_line = "💰 Valor: *a confirmar*"
        # Notificacao para a Jaqueline com tipo e valor
        send_whatsapp_tracked(JAQUELINE_PHONE,
            f"📋 *Agendamento confirmado pelo bot*\n\n"
            f"👤 Paciente: *{nome}*\n"
            f"📅 Data/hora: *{date_label}* às *{time_label}*\n"
            f"🔖 Tipo: *{tipo_label}*\n"
            f"{valor_line}\n"
            f"📱 WhatsApp: {phone}\n"
            f"🆔 ID: {appt_id}")
        update_session(ph, current_state="NEW", available_slots=None,
                       current_slot_index=0, last_appointment_id=appt_id,
                       is_retorno=0)
        # Agenda lembrete 1 dia antes às 8h
        schedule_reminder(phone, nome, dt_raw)
        return jsonify({"status": "appointment_confirmed"}), 200
    else:
        err = result.get("error", "erro desconhecido") if isinstance(result, dict) else str(result)
        log.warning("handle_slot_confirmation falhou: %s | slot=%s dt=%s", err, slot_id, dt_raw)

        # Conta falhas consecutivas na sessão
        failures = (session.get("confirm_failures") or 0) + 1
        update_session(ph, confirm_failures=failures)

        if failures >= 2:
            # Limite atingido — encaminha pra Jaqueline sem loop
            log.warning("confirm_failures=%d — encaminhando para Jaqueline (%s)", failures, phone[-4:])
            update_session(ph, current_state="NEW", available_slots=None,
                           current_slot_index=0, confirm_failures=0)
            send_whatsapp_tracked(phone,
                "Não consegui confirmar o agendamento pelo sistema. 😕\n"
                "A supervisora Jaqueline vai entrar em contato pra concluir seu agendamento!")
            return handoff_to_jaqueline(phone, ph, "booking_api_error",
                                        patient_name=nome,
                                        subject=f"Falha ao confirmar slot ({err[:120]}) — {nome} ({phone})")
        else:
            send_whatsapp_tracked(phone,
                "Infelizmente esse horário não está mais disponível. 😕\n"
                "Vou verificar o próximo disponível para você!")
            return offer_next_slot(phone, ph)


def offer_next_slot(phone: str, ph: str):
    session = get_session_by_hash(ph)
    new_idx = (session.get("current_slot_index", 0) or 0) + 1
    update_session(ph, current_slot_index=new_idx)
    return offer_slot(phone, ph)


def handle_cancel_intent(phone: str, ph: str, session: dict):
    """Paciente quer cancelar — usa o appointment_id salvo na sessão."""
    appt_id = session.get("last_appointment_id", "")
    nome = session.get("collected_name", "")
    if not appt_id:
        # Não temos ID salvo — encaminha para Jaqueline
        return handoff_to_jaqueline(phone, ph, "cancel_no_id",
                                    patient_name=nome,
                                    subject="Paciente quer cancelar consulta (ID não encontrado na sessão)")
    return handle_cancel(phone, ph, appt_id)


def handle_remarcar_intent(phone: str, ph: str, session: dict):
    """Paciente quer reagendar — cancela a consulta atual e inicia novo agendamento."""
    appt_id = session.get("last_appointment_id", "")
    nome = session.get("collected_name", "")
    if appt_id and is_api_configured():
        result = cancel_appointment(appt_id)
        if isinstance(result, dict) and "error" in result:
            log.warning("Remarcar: falha ao cancelar %s — %s", appt_id, result["error"])
        else:
            log.info("Remarcar: consulta %s cancelada antes de reagendar", appt_id)
    # Limpa estado anterior e inicia novo fluxo de agendamento
    update_session(ph, current_state="NEW", available_slots=None,
                   current_slot_index=0, last_appointment_id=None)
    send_whatsapp_tracked(phone,
        "Certo! Vou cancelar a consulta anterior e agendar uma nova. 😊\n"
        "Me confirma seu nome, telefone e CPF pra continuar?")
    return jsonify({"status": "remarcar_started"}), 200


def handle_cancel(phone: str, ph: str, appointment_id: str):
    if not is_api_configured():
        send_whatsapp_tracked(phone, "Pra cancelar, vou pedir pra Jaqueline cuidar do seu cancelamento.")
        return handoff_to_jaqueline(phone, ph, "admin_route",
                                    subject=f"Cancelar agendamento #{appointment_id}")
    result = cancel_appointment(appointment_id)
    if isinstance(result, dict) and "error" in result:
        log.warning("Falha ao cancelar agendamento %s: %s", appointment_id, result["error"])
        # Limpa o ID inválido/expirado da sessão e encaminha pra Jaqueline
        update_session(ph, current_state="NEW", last_appointment_id=None)
        session = get_session_by_hash(ph)
        nome = session.get("collected_name", "")
        send_whatsapp_tracked(phone,
            "Não consegui cancelar automaticamente. "
            "Vou pedir pra Jaqueline cuidar disso pra você! 😊")
        return handoff_to_jaqueline(phone, ph, "cancel_api_error",
                                    patient_name=nome,
                                    subject=f"Erro ao cancelar agendamento #{appointment_id}: {result['error']}")
    # Sucesso — limpa o ID para evitar tentativa de cancelar de novo
    update_session(ph, current_state="NEW", last_appointment_id=None)
    send_whatsapp_tracked(phone, "✅ Agendamento cancelado com sucesso.\nSe precisar reagendar, é só me avisar!")
    return jsonify({"status": "cancelled"}), 200


def redirect_to_prescription_bot(phone: str, ph: str, text: str):
    send_whatsapp_tracked(phone,
        f"Sobre sua dúvida, vou encaminhar você pro Uriel, assistente especializado do consultório:\n\n"
        f"👉 wa.me/{PRESCRIPTION_BOT_PHONE}\n\n"
        f"Aqui na Lorena cuido apenas de agendamentos. Qualquer hora que precisar marcar consulta, é só me chamar! 😊")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO handoffs_to_jaqueline
            (patient_phone_hash, patient_phone_last4, subject, triggered_by, redirected_to)
        VALUES (?, ?, ?, 'clinical_doubt', 'prescription_bot')
    """, (ph, phone[-4:], text[:200]))
    conn.commit()
    conn.close()
    return jsonify({"status": "redirected_to_prescription"}), 200


def handoff_to_jaqueline(phone: str, ph: str, triggered_by: str,
                         patient_name: str = "", subject: str = ""):
    send_whatsapp_tracked(phone,
        "👤 Vou pedir pra nossa atendente Jaqueline te atender pessoalmente.\n"
        "Ela vai responder aqui mesmo, nesta conversa, em alguns instantes.")
    last4 = phone[-4:]
    notify_msg = (f"👋 *Novo encaminhamento para atendimento humano*\n\n"
                  f"👤 Nome: *{patient_name or 'não identificado'}*\n"
                  f"📱 WhatsApp: *{phone}*\n\n"
                  f"📋 Motivo: {triggered_by}\n"
                  f"💬 Assunto: {subject or '—'}\n\n"
                  f"Responda pelo WhatsApp Web da Lorena ou entre em contato diretamente pelo link acima.")
    send_whatsapp_tracked(JAQUELINE_PHONE, notify_msg)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO handoffs_to_jaqueline
            (patient_phone_hash, patient_phone_last4, patient_name, subject, triggered_by, redirected_to)
        VALUES (?, ?, ?, ?, ?, 'jaqueline_human')
    """, (ph, last4, patient_name, subject, triggered_by))
    conn.commit()
    conn.close()
    update_session(ph, current_state="HANDED_OFF")
    return jsonify({"status": "handed_off"}), 200


@app.route("/internal/reset-session", methods=["POST"])
def reset_session():
    """Limpa histórico e estado de sessão de um número. Protegido por INTERNAL_API_KEY."""
    secret = request.headers.get("X-Internal-Key", "")
    valid = {os.getenv("FLASK_SECRET_KEY", ""), os.getenv("INTERNAL_API_KEY", "")} - {""}
    if not secret or secret not in valid:
        return jsonify({"error": "unauthorized"}), 401
    data = request.json or {}
    phone = data.get("phone", "").strip().lstrip("+")
    if not phone:
        return jsonify({"error": "phone is required"}), 400
    from lorena_state import hash_phone
    ph = hash_phone(phone)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE patient_sessions
        SET current_state='NEW', collected_name=NULL, collected_phone=NULL,
            collected_date=NULL, collected_document=NULL,
            available_slots=NULL, current_slot_index=0,
            conversation_history=NULL, is_retorno=0,
            last_appointment_id=NULL, paused_until=NULL
        WHERE patient_phone_hash=?
    """, (ph,))
    conn.commit()
    conn.close()
    log.info("reset_session: sessão de *%s resetada", phone[-4:])
    return jsonify({"status": "ok", "phone_last4": phone[-4:]}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "bot_active": is_bot_active(),
        "api_configured": is_api_configured(),
        "ts": datetime.utcnow().isoformat()
    })


@app.route("/internal/set-instruction", methods=["POST"])
def set_instruction_internal():
    """Endpoint interno protegido — atualiza instruções do bot sem precisar do WhatsApp."""
    secret = request.headers.get("X-Internal-Key", "")
    valid = {os.getenv("FLASK_SECRET_KEY", ""), os.getenv("INTERNAL_API_KEY", "")} - {""}
    if not secret or secret not in valid:
        return jsonify({"error": "unauthorized"}), 401
    data = request.json or {}
    text = data.get("text", "").strip()
    category = data.get("category", "GERAL")
    clear_first = data.get("clear_first", True)
    if not text:
        return jsonify({"error": "text is required"}), 400
    from lorena_instructions import add_instruction, clear_whatsapp_instructions
    cleared = 0
    if clear_first:
        cleared = clear_whatsapp_instructions("internal")
    iid = add_instruction(text, category, priority=10,
                          created_by_phone="internal", created_via="whatsapp")
    log.info("set_instruction_internal: cleared=%d, new_id=%d", cleared, iid)
    return jsonify({"status": "ok", "cleared": cleared, "instruction_id": iid}), 200


@app.route("/test-llm", methods=["GET"])
def test_llm():
    """Diagnóstico completo do Groq SDK."""
    groq_key = os.getenv("GROQ_API_KEY", "")
    groq_model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    result = {
        "version": "2026-05-23-v3",
        "groq_key_present": bool(groq_key),
        "groq_key_prefix": (groq_key[:8] + "...") if groq_key else "MISSING",
        "groq_model_env": groq_model,
        "models_tried": [],
    }
    # Testa cada modelo individualmente para ver quais funcionam
    client = _get_groq_client()
    working_model = None
    for model in GROQ_MODELS:
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "responda apenas: ok"}],
                temperature=0.3,
                max_tokens=5,
            )
            result["models_tried"].append({"model": model, "status": "ok", "response": resp.choices[0].message.content})
            if not working_model:
                working_model = model
        except Exception as e:
            result["models_tried"].append({"model": model, "status": "error", "error": str(e)})
    result["working_model"] = working_model
    ok = working_model is not None
    return jsonify(result), 200 if ok else 500


# ===== LEMBRETES DE CONSULTA =================================================

def schedule_reminder(phone: str, name: str, appointment_dt_str: str):
    """Salva lembrete no banco para ser enviado 1 dia antes às 8h (ou imediato se consulta hoje/amanhã cedo)."""
    try:
        appt_dt = datetime.fromisoformat(appointment_dt_str.replace("Z", "+00:00")).replace(tzinfo=None)
        # Lembrete = dia anterior às 8:00
        reminder_dt = (appt_dt - timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        now = datetime.now()
        # Se o lembrete já passou, envia nas próximas 5 minutos
        if reminder_dt < now:
            reminder_dt = now + timedelta(minutes=5)
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO appointment_reminders (patient_phone, patient_name, appointment_datetime, reminder_send_at) VALUES (?,?,?,?)",
            (phone, name, appointment_dt_str, reminder_dt.isoformat())
        )
        conn.commit()
        conn.close()
        log.info("Lembrete agendado para %s em %s", phone[-4:], reminder_dt.strftime("%d/%m %H:%M"))
    except Exception as e:
        log.warning("Erro ao agendar lembrete: %s", e)


def _build_reminder_msg(name: str, appointment_dt_str: str) -> str:
    try:
        dt = datetime.fromisoformat(appointment_dt_str.replace("Z", "+00:00")).replace(tzinfo=None)
        weekday_names = {0: "segunda-feira", 1: "terça-feira", 2: "quarta-feira",
                         3: "quinta-feira", 4: "sexta-feira", 5: "sábado", 6: "domingo"}
        weekday = weekday_names.get(dt.weekday(), "")
        date_str = dt.strftime("%d/%m")
        time_str = dt.strftime("%H:%M")
        today = datetime.now().date()
        if dt.date() == today:
            when = f"hoje às *{time_str}*"
        elif dt.date() == today + timedelta(days=1):
            when = f"amanhã, *{weekday}* dia *{date_str}* às *{time_str}*"
        else:
            when = f"*{weekday}*, dia *{date_str}* às *{time_str}*"
    except Exception:
        when = appointment_dt_str
    first = name.split()[0] if name else "Olá"
    return (
        f"Olá, {first}! 👋\n"
        f"Lembrete: sua consulta com o Dr. Tiago é {when}.\n"
        f"📍 Shopping 33, Torre 3, Sala 1502 — Vila Santa Cecília, VR\n\n"
        f"Qualquer dúvida é só chamar! 😊"
    )


def check_and_send_reminders():
    """Verifica lembretes pendentes e envia os que estão no prazo."""
    try:
        now = datetime.now().isoformat()
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT id, patient_phone, patient_name, appointment_datetime FROM appointment_reminders "
            "WHERE sent=0 AND reminder_send_at <= ?", (now,)
        ).fetchall()
        for row_id, phone, name, appt_dt in rows:
            msg = _build_reminder_msg(name or "", appt_dt)
            ok = send_whatsapp_raw(phone, msg)
            sent_at = datetime.now().isoformat()
            conn.execute("UPDATE appointment_reminders SET sent=1, sent_at=? WHERE id=?", (sent_at, row_id))
            conn.commit()
            log.info("Lembrete enviado para %s (ok=%s)", phone[-4:], ok)
        conn.close()
    except Exception as e:
        log.warning("Erro em check_and_send_reminders: %s", e)


def send_whatsapp_raw(phone: str, message: str) -> bool:
    """Envia mensagem direta via Evolution API (sem rastrear duplicatas)."""
    if not EVOLUTION_URL or not EVOLUTION_INSTANCE or not EVOLUTION_API_KEY:
        return False
    try:
        url = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"
        r = requests.post(url, headers={"Content-Type": "application/json", "apikey": EVOLUTION_API_KEY},
                          json={"number": phone, "text": message}, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("send_whatsapp_raw falhou: %s", e)
        return False


def _reminder_worker():
    """Thread de background: verifica lembretes a cada 30 minutos."""
    log.info("Reminder worker iniciado.")
    while True:
        time.sleep(1800)  # 30 minutos
        check_and_send_reminders()


def start_reminder_thread():
    t = threading.Thread(target=_reminder_worker, daemon=True, name="reminder-worker")
    t.start()
    log.info("Thread de lembretes iniciada.")


# Inicia thread de lembretes ao carregar o módulo (gunicorn / Railway)
start_reminder_thread()

# =============================================================================

if __name__ == "__main__":
    port = int(os.getenv("WEBHOOK_PORT", 6001))
    ngrok_token = os.getenv("NGROK_AUTHTOKEN")
    if ngrok_token and _PYNGROK:
        conf.get_default().auth_token = ngrok_token
        public_url = ngrok.connect(port).public_url
        log.info("=" * 70)
        log.info("Webhook Lorena: %s/webhook/messages-upsert", public_url)
        log.info("Configure essa URL no painel Evolution -> instancia 'lorena-bot'")
        log.info("=" * 70)
    log.info("Lorena Bot iniciando — porta %d", port)
    log.info("Bot status: %s", "ATIVO" if is_bot_active() else "PARADO")
    log.info("API consultorio.me: %s", "OK" if is_api_configured() else "NÃO CONFIGURADA")
    log.info("Instruções ativas: %d", len(list_active_instructions()))
    start_reminder_thread()
    app.run(host="0.0.0.0", port=port, debug=False)
