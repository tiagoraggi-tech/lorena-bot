"""
lorena_state.py — Gestão de estado por paciente (multi-sessão)
"""
import os
import json
import sqlite3
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("lorena.state")
DB_PATH = os.getenv("DB_PATH", "/data/lorena.db")
SESSION_TIMEOUT_HOURS = int(os.getenv("SESSION_TIMEOUT_HOURS", 2))


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def hash_phone(phone: str) -> str:
    return hashlib.sha256(phone.encode()).hexdigest()


def get_or_create_session(phone: str) -> dict:
    ph = hash_phone(phone)
    last4 = phone[-4:]
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM patient_sessions WHERE patient_phone_hash=?", (ph,))
    row = cur.fetchone()
    if not row:
        cur.execute("""
            INSERT INTO patient_sessions (patient_phone_hash, phone_last4, current_state)
            VALUES (?, ?, 'NEW')
        """, (ph, last4))
        conn.commit()
        cur.execute("SELECT * FROM patient_sessions WHERE patient_phone_hash=?", (ph,))
        row = cur.fetchone()
    if row["last_message_at"]:
        last_msg = datetime.fromisoformat(row["last_message_at"])
        if (datetime.utcnow() - last_msg).total_seconds() > SESSION_TIMEOUT_HOURS * 3600:
            log.info("Sessão expirada pra *%s, reset", last4)
            cur.execute("""
                UPDATE patient_sessions
                SET current_state='NEW', collected_name=NULL, collected_phone=NULL,
                    collected_date=NULL, available_slots=NULL, current_slot_index=0,
                    conversation_history=NULL
                WHERE patient_phone_hash=?
            """, (ph,))
            conn.commit()
            cur.execute("SELECT * FROM patient_sessions WHERE patient_phone_hash=?", (ph,))
            row = cur.fetchone()
    data = dict(row)
    if data.get("available_slots"):
        data["available_slots"] = json.loads(data["available_slots"])
    if data.get("conversation_history"):
        data["conversation_history"] = json.loads(data["conversation_history"])
    else:
        data["conversation_history"] = []
    conn.close()
    return data


def update_session(phone_hash: str, **updates) -> None:
    if not updates:
        return
    if "available_slots" in updates and updates["available_slots"] is not None:
        updates["available_slots"] = json.dumps(updates["available_slots"])
    if "conversation_history" in updates and updates["conversation_history"] is not None:
        updates["conversation_history"] = json.dumps(updates["conversation_history"])
    updates["updated_at"] = datetime.utcnow().isoformat()
    updates["last_message_at"] = datetime.utcnow().isoformat()
    fields = ", ".join(f"{k}=?" for k in updates.keys())
    values = list(updates.values()) + [phone_hash]
    conn = _conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE patient_sessions SET {fields} WHERE patient_phone_hash=?", values)
    conn.commit()
    conn.close()


def add_to_history(phone_hash: str, role: str, content: str, max_history: int = 10):
    session = get_session_by_hash(phone_hash)
    if not session:
        return
    history = session.get("conversation_history", []) or []
    history.append({"role": role, "content": content, "ts": datetime.utcnow().isoformat()})
    if len(history) > max_history:
        history = history[-max_history:]
    update_session(phone_hash, conversation_history=history)


def get_session_by_hash(phone_hash: str) -> Optional[dict]:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM patient_sessions WHERE patient_phone_hash=?", (phone_hash,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    data = dict(row)
    if data.get("available_slots"):
        data["available_slots"] = json.loads(data["available_slots"])
    if data.get("conversation_history"):
        data["conversation_history"] = json.loads(data["conversation_history"])
    return data


def is_session_paused(phone_hash: str) -> bool:
    session = get_session_by_hash(phone_hash)
    if not session or not session.get("paused_until"):
        return False
    return session["paused_until"] > datetime.utcnow().isoformat()


def pause_session(phone_hash: str, minutes: int = 30) -> None:
    paused_until = (datetime.utcnow() + timedelta(minutes=minutes)).isoformat()
    update_session(phone_hash, paused_until=paused_until)
    log.info("Sessão *%s pausada até %s", phone_hash[:8], paused_until)
