"""
lorena_instructions.py — Sistema de instruções dinâmicas + controle do bot
Comandos via WhatsApp (do número pessoal da Jaqueline):
  /parar, /ativar, /status, /instrucao [texto], /instrucoes, /instrucao_off N,
  /limpar_instrucoes, /help

Regra de prioridade:
  Instruções via /instrucao (WhatsApp da Jaqueline) sempre recebem priority=10.
  O sistema ordena por priority DESC, created_at DESC — logo a instrução mais
  recente do WhatsApp sempre aparece no topo do contexto do LLM.
"""
import os
import re
import sqlite3
import logging
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("lorena.instructions")
DB_PATH = os.getenv("DB_PATH", "/data/lorena.db")
JAQUELINE_PHONE = os.getenv("JAQUELINE_PHONE", "5524999025732")
VALID_CATEGORIES = ["GERAL", "INICIAL", "PRECO", "PLANO", "HORARIO", "LOCALIZACAO", "OUTROS"]


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ===== Bot status =====

def is_bot_active() -> bool:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT is_active FROM bot_status WHERE id=1")
    row = cur.fetchone()
    conn.close()
    return bool(row and row["is_active"])


def set_bot_status(active: bool, by_phone: str, reason: str = "") -> None:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE bot_status
        SET is_active=?, last_changed_at=?, changed_by_phone=?, reason=?
        WHERE id=1
    """, (1 if active else 0, datetime.utcnow().isoformat(), by_phone, reason[:200]))
    conn.commit()
    conn.close()
    log.info("Bot %s por %s. Reason: %s", "ATIVADO" if active else "PARADO", by_phone, reason)


def get_bot_status() -> dict:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM bot_status WHERE id=1")
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else {"is_active": True}


# ===== Instruções =====

def add_instruction(text: str, category: str = "GERAL", priority: int = 5,
                    created_by_phone: str = "", created_via: str = "whatsapp") -> int:
    if len(text) < 5:
        raise ValueError("Instrução muito curta")
    if len(text) > 1000:
        raise ValueError("Instrução muito longa (max 1000)")
    if category not in VALID_CATEGORIES:
        category = "GERAL"
    if not (1 <= priority <= 10):
        priority = 5
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO lorena_instructions
            (instruction_text, category, priority, created_by_phone, created_via)
        VALUES (?, ?, ?, ?, ?)
    """, (text.strip(), category, priority, created_by_phone, created_via))
    iid = cur.lastrowid
    conn.commit()
    conn.close()
    log.info("Instrução #%d criada por %s: %s", iid, created_by_phone, text[:50])
    return iid


def list_active_instructions(category: Optional[str] = None) -> list[dict]:
    conn = _conn()
    cur = conn.cursor()
    if category:
        cur.execute("""
            SELECT * FROM lorena_instructions
            WHERE active=1 AND category=?
            ORDER BY priority DESC, created_at DESC
        """, (category,))
    else:
        cur.execute("""
            SELECT * FROM lorena_instructions
            WHERE active=1
            ORDER BY priority DESC, created_at DESC
        """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def deactivate_instruction(instruction_id: int, by_phone: str) -> bool:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE lorena_instructions
        SET active=0, deactivated_at=?, deactivated_by_phone=?
        WHERE id=? AND active=1
    """, (datetime.utcnow().isoformat(), by_phone, instruction_id))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def clear_whatsapp_instructions(by_phone: str) -> int:
    """Desativa todas as instruções adicionadas via WhatsApp (created_via='whatsapp').
    Retorna o número de instruções desativadas."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE lorena_instructions
        SET active=0, deactivated_at=?, deactivated_by_phone=?
        WHERE active=1 AND created_via='whatsapp'
    """, (datetime.utcnow().isoformat(), by_phone))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    log.info("clear_whatsapp_instructions: %d instrução(ões) desativada(s) por %s", affected, by_phone)
    return affected


# ===== Parser de comandos =====

def parse_command(text: str) -> dict:
    text = text.strip()
    text_lower = text.lower()

    if text_lower in ("/parar", "/pare"):
        return {"action": "PARAR"}
    if text_lower in ("/ativar", "/ativa", "/iniciar"):
        return {"action": "ATIVAR"}
    if text_lower in ("/status", "/estado"):
        return {"action": "STATUS"}
    if text_lower in ("/instrucoes", "/instruções", "/lista"):
        return {"action": "LIST"}
    if text_lower in ("/help", "/ajuda", "ver comandos", "comandos"):
        return {"action": "HELP"}

    m = re.match(r"^/resetar\s+(\d+)\s*$", text_lower)
    if m:
        return {"action": "RESETAR", "phone": m.group(1)}

    # /limpar_instrucoes — apaga da memória todas as instruções enviadas via WhatsApp
    if re.match(r"^/limpar[_\s]instru[cç][oõ]es$", text_lower):
        return {"action": "LIMPAR"}

    m = re.match(r"^/instru[cç][aã]o_off\s+(\d+)\s*$", text_lower)
    if m:
        return {"action": "DEACTIVATE", "instruction_id": int(m.group(1))}

    m = re.match(r"^/instru[cç][aã]o\s+(.+)$", text, re.IGNORECASE | re.DOTALL)
    if m:
        body = m.group(1).strip()
        # Instruções via WhatsApp da Jaqueline sempre têm priority=10 (máxima).
        # A mais recente aparece primeiro pois a query ordena por priority DESC, created_at DESC.
        priority = 10
        category = "GERAL"
        cat_m = re.match(r"^categoria=(\w+)\s+(.+)$", body, re.IGNORECASE | re.DOTALL)
        if cat_m:
            cat_candidate = cat_m.group(1).upper()
            if cat_candidate in VALID_CATEGORIES:
                category = cat_candidate
                body = cat_m.group(2).strip()
        # Nota: priority=X prefixo ignorado intencionalmente — sempre 10 para manter hierarquia
        if not body or len(body) < 5:
            return {"action": "INVALID", "error": "Instrução muito curta (mínimo 5 chars)"}
        return {"action": "ADD", "instruction_text": body, "category": category, "priority": priority}

    return {"action": "INVALID", "error": "Comando não reconhecido"}


def handle_command(text: str, from_phone: str) -> str:
    if from_phone != JAQUELINE_PHONE:
        return "⛔ Você não tem permissão pra executar comandos administrativos."

    parsed = parse_command(text)
    action = parsed.get("action")

    if action == "PARAR":
        set_bot_status(False, from_phone, reason="Parado por Jaqueline via WhatsApp")
        return ("🛑 *Bot Lorena PARADO.*\n\n"
                "Você assumiu o atendimento manual.\n"
                "Mensagens dos pacientes não serão respondidas pelo bot até /ativar.")
    if action == "ATIVAR":
        set_bot_status(True, from_phone, reason="Ativado por Jaqueline")
        try:
            from lorena_state import resume_all_sessions
            count = resume_all_sessions()
            sessions_msg = f"\n{count} sessão(ões) de paciente reativada(s)." if count else ""
        except Exception:
            sessions_msg = ""
        return (f"✅ *Bot Lorena ATIVADO.*\n\n"
                f"Voltei a responder mensagens dos pacientes automaticamente.{sessions_msg}")
    if action == "STATUS":
        status = get_bot_status()
        active = "✅ ATIVO" if status["is_active"] else "🛑 PARADO"
        return (f"📊 *Status do Bot Lorena*\n\n"
                f"Estado: {active}\n"
                f"Última alteração: {status.get('last_changed_at', '?')}\n"
                f"Motivo: {status.get('reason', '—')}")
    if action == "ADD":
        try:
            iid = add_instruction(parsed["instruction_text"], parsed["category"],
                                  parsed["priority"], from_phone, "whatsapp")
            return (f"✅ *Instrução #{iid} salva com prioridade máxima*\n\n"
                    f"📁 Categoria: {parsed['category']}\n"
                    f"⭐ Prioridade: {parsed['priority']}/10 (máxima — prevalece sobre as demais)\n\n"
                    f"_A instrução mais recente sempre tem precedência.\n"
                    f"Para desativar esta: /instrucao_off {iid}\n"
                    f"Para limpar todas as instruções do WhatsApp: /limpar_instrucoes_")
        except Exception as e:
            return f"❌ Erro: {e}"
    if action == "LIMPAR":
        try:
            count = clear_whatsapp_instructions(from_phone)
            if count == 0:
                return "📋 Nenhuma instrução do WhatsApp estava ativa para limpar."
            return (f"🗑️ *{count} instrução(ões) do WhatsApp removida(s) da memória do bot.*\n\n"
                    f"As instruções de sistema (seed) permanecem intactas.\n"
                    f"Use /instrucao [texto] pra adicionar novas orientações.")
        except Exception as e:
            return f"❌ Erro ao limpar: {e}"
    if action == "LIST":
        instructions = list_active_instructions()
        if not instructions:
            return "📋 Nenhuma instrução ativa no momento."
        lines = [f"📋 *{len(instructions)} instruções ativas:*\n"]
        for inst in instructions:
            preview = inst["instruction_text"][:80]
            if len(inst["instruction_text"]) > 80:
                preview += "..."
            lines.append(f"*#{inst['id']}* [{inst['category']}, prio {inst['priority']}]: {preview}")
        lines.append("\n_Use /instrucao_off N pra desativar_")
        return "\n".join(lines)
    if action == "DEACTIVATE":
        iid = parsed["instruction_id"]
        ok = deactivate_instruction(iid, from_phone)
        return (f"✅ Instrução #{iid} desativada." if ok
                else f"⚠️ Instrução #{iid} não encontrada ou já desativada.")
    if action == "RESETAR":
        patient_phone = parsed["phone"]
        try:
            from lorena_state import hash_phone, update_session
            ph = hash_phone(patient_phone)
            update_session(ph, current_state="NEW", collected_name=None,
                           collected_phone=None, collected_document=None,
                           collected_date=None, available_slots=None,
                           current_slot_index=0, conversation_history=None,
                           paused_until=None)
            return (f"✅ Sessão do paciente *+{patient_phone}* resetada.\n"
                    f"O bot vai tratá-lo como novo paciente na próxima mensagem.")
        except Exception as e:
            return f"❌ Erro ao resetar sessão: {e}"
    if action == "HELP":
        return (
            "🤖 *Comandos da Lorena — Jaqueline*\n\n"
            "──────────────────────\n"
            "🔛 *Controle do bot*\n"
            "• `/ativar` — liga o bot\n"
            "• `/parar` — pausa o bot\n"
            "• `/status` — vê se o bot está ligado\n\n"
            "📝 *Instruções (memória do bot)*\n"
            "• `/instrucao texto` — salva info no bot (ex: valor, planos, endereço)\n"
            "• `/instrucoes` — lista todas as instruções ativas\n"
            "• `/instrucao_off N` — desativa a instrução de número N\n"
            "• `/limpar_instrucoes` — apaga todas as instruções salvas\n\n"
            "──────────────────────\n"
            "💡 *Categorias opcionais:*\n"
            "PRECO · PLANO · HORARIO · LOCALIZACAO · GERAL\n\n"
            "Exemplo com categoria:\n"
            "`/instrucao categoria=PRECO Consulta: R$ 250`\n\n"
            "🔄 *Sessão de paciente*\n"
            "• `/resetar 5524XXXXXXXX` — reseta sessão de um paciente\n\n"
            "📋 *Ver lista de instruções ativas:* /instrucoes\n"
            "❓ *Ver esta ajuda:* ver comandos"
        )
    return f"❌ {parsed.get('error', 'Comando desconhecido')}.\nEnvie *ver comandos* pra ver a lista."
