"""
lorena_webhook.py — Webhook principal da Lorena
Recebe mensagens via Evolution API, classifica, processa, responde.
"""
import os
import json
import hashlib
import logging
import sqlite3
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
from lorena_instructions import is_bot_active, handle_command, list_active_instructions
from lorena_prompt import build_system_prompt
from consultorio_api import get_available_times, create_appointment, cancel_appointment, is_api_configured, find_next_available_slot

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

        # fromMe=True: mensagem enviada PELO número da Lorena (chip do bot).
        # Ignoramos SEMPRE — seja resposta da API ou Jaqueline digitando no chip.
        # Comandos admin da Jaqueline chegam pelo número pessoal dela (JAQUELINE_PHONE) abaixo.
        if key.get("fromMe"):
            log.debug("fromMe=True ignorado (phone=*%s)", phone[-4:] if len(phone) >= 4 else phone)
            return jsonify({"status": "ignored_self"}), 200

        # Comandos admin da Jaqueline (do número pessoal dela: 5524999025732)
        # match pelos últimos 8 dígitos para tolerar variações de JID do WhatsApp
        if phone.endswith(JAQUELINE_PHONE[-8:]) or phone == JAQUELINE_PHONE:
            log.info("Mensagem da Jaqueline: %s", text[:60])
            response = handle_command(text, phone)
            send_whatsapp_tracked(JAQUELINE_PHONE, response)
            audit({"ts": datetime.utcnow().isoformat(), "event": "jaqueline_command", "command": text[:100]})
            return jsonify({"status": "command_processed"}), 200

        # Ignorar mensagens de grupos, broadcasts e status
        if len(phone) > 15 or not phone.isdigit():
            return jsonify({"status": "ignored_non_individual"}), 200

        # Mensagem do Dr. Tiago
        if phone == TIAGO_PHONE or phone.endswith(TIAGO_PHONE[-8:]):
            send_whatsapp_tracked(TIAGO_PHONE,
                "Olá Dr. Tiago! Este é o bot Lorena (agendamento).\n"
                "Pra comandos administrativos, use o número da Jaqueline.\n"
                f"Pra dúvidas clínicas dos pacientes, use o Uriel ({PRESCRIPTION_BOT_PHONE}).")
            return jsonify({"status": "tiago_redirect"}), 200

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
        return handle_slot_confirmation(phone, ph)

    if "BUSCAR_PROXIMO:" in text:
        try:
            payload = json.loads(text.split("BUSCAR_PROXIMO:", 1)[1].strip())
            nome = payload.get("nome", "").strip()
            cpf = payload.get("cpf", "").strip()
            is_retorno = bool(payload.get("retorno", False))
        except Exception:
            nome = session.get("collected_name", "")
            cpf = session.get("collected_document", "")
            is_retorno = False
        telefone = phone  # sempre usa o WhatsApp de quem está conversando
        add_to_history(ph, "assistant", "Deixa eu verificar a próxima vaga disponível... 🔍")
        send_whatsapp_tracked(phone, "Deixa eu verificar a próxima vaga disponível... 🔍")
        result = find_next_available_slot()
        if isinstance(result, dict) and "slots" in result:
            next_date = result["date"]
            next_slots = result["slots"]
            update_session(ph, current_state="AWAITING_CONFIRMATION",
                           collected_name=nome, collected_phone=telefone,
                           collected_document=cpf,
                           collected_date=next_date, available_slots=next_slots,
                           current_slot_index=0,
                           is_retorno=1 if is_retorno else 0)
            return offer_slot(phone, ph)
        else:
            send_whatsapp_tracked(phone,
                "Não encontrei vagas disponíveis nos próximos dias. "
                "Vou chamar a Jaqueline pra te ajudar! 😊")
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

    weekday_label = f"*{weekday}*, " if weekday else ""
    offer_msg = (f"O próximo horário disponível é {weekday_label}*{date_str}* às *{time_str}*. 😊\n"
                 f"Funciona pra você? (responda *sim* ou *não*)")
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
        retorno_line = "\n💚 *Consulta de retorno — gratuita!*" if is_retorno else ""
        send_whatsapp_tracked(phone,
            f"✅ Consulta confirmada!\n"
            f"📅 *{date_label}* às *{time_label}*\n"
            f"🏥 Shopping 33, Torre 3, Sala 1502 — Vila Santa Cecília, VR"
            f"{retorno_line}\n\n"
            f"Se precisar cancelar ou reagendar, é só me chamar! 😊")
        update_session(ph, current_state="NEW", available_slots=None,
                       current_slot_index=0, last_appointment_id=appt_id,
                       is_retorno=0)
        return jsonify({"status": "appointment_confirmed"}), 200
    else:
        err = result.get("error", "erro desconhecido") if isinstance(result, dict) else str(result)
        log.error("handle_slot_confirmation falhou: %s", err)
        send_whatsapp_tracked(phone,
            "Tive um problema técnico ao confirmar. Vou chamar a Jaqueline pra te ajudar! 😊")
        return handoff_to_jaqueline(phone, ph, "api_error", patient_name=nome,
                                    subject=f"Erro ao agendar {dt_raw} — {nome} ({telefone}): {err}")


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
                  f"📱 WhatsApp: *+{phone}*\n"
                  f"🔗 wa.me/{phone}\n\n"
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


@app.route("/internal/api-explore", methods=["GET"])
def api_explore():
    """Endpoint temporário para explorar a API do consultorio.me."""
    secret = request.headers.get("X-Internal-Key", "")
    valid = {os.getenv("FLASK_SECRET_KEY", ""), os.getenv("INTERNAL_API_KEY", "")} - {""}
    if not secret or secret not in valid:
        return jsonify({"error": "unauthorized"}), 401
    from consultorio_api import _get_token, BASE_URL, PRO_ID
    import requests as req
    token = _get_token()
    if not token:
        return jsonify({"error": "sem token"}), 500
    path = request.args.get("path", f"/v1/api/appointment/reminders/{PRO_ID}")
    try:
        r = req.get(f"{BASE_URL}{path}",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    timeout=15)
        try:
            data = r.json()
        except Exception:
            data = r.text[:2000]
        return jsonify({"status": r.status_code, "path": path, "data": data}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
    app.run(host="0.0.0.0", port=port, debug=False)
