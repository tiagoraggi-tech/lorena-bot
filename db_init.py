"""
db_init.py — Schema da Lorena (banco SEPARADO do prescription_bot)
"""
import sqlite3
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

DB_PATH = os.getenv("DB_PATH", "/data/lorena.db")
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

SCHEMA = """
-- ====================================================
-- TABELA: patient_sessions
-- ====================================================
CREATE TABLE IF NOT EXISTS patient_sessions (
    patient_phone_hash TEXT PRIMARY KEY,
    phone_last4 TEXT NOT NULL,
    current_state TEXT DEFAULT 'NEW',
    collected_name TEXT,
    collected_phone TEXT,
    collected_date TEXT,
    collected_birth_date TEXT,
    collected_document TEXT,
    available_slots TEXT,
    current_slot_index INTEGER DEFAULT 0,
    conversation_history TEXT,
    paused_until TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_message_at TEXT,
    confirm_failures INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_state ON patient_sessions(current_state);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON patient_sessions(updated_at);

-- ====================================================
-- TABELA: bot_status
-- ====================================================
CREATE TABLE IF NOT EXISTS bot_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    is_active INTEGER DEFAULT 1,
    is_dormindo INTEGER DEFAULT 0,
    last_changed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    changed_by_phone TEXT,
    reason TEXT
);
INSERT OR IGNORE INTO bot_status (id, is_active) VALUES (1, 1);

-- ====================================================
-- TABELA: lorena_instructions
-- ====================================================
CREATE TABLE IF NOT EXISTS lorena_instructions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instruction_text TEXT NOT NULL,
    category TEXT DEFAULT 'GERAL',
    priority INTEGER DEFAULT 5,
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    created_by_phone TEXT,
    created_via TEXT,
    deactivated_at TEXT,
    deactivated_by_phone TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_instructions_active ON lorena_instructions(active);
CREATE INDEX IF NOT EXISTS idx_instructions_category ON lorena_instructions(category, active);

-- ====================================================
-- TABELA: appointments_log
-- ====================================================
CREATE TABLE IF NOT EXISTS appointments_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_phone_hash TEXT NOT NULL,
    patient_phone_last4 TEXT NOT NULL,
    patient_name TEXT,
    appointment_datetime TEXT,
    consultorio_appointment_id TEXT,
    action TEXT NOT NULL,
    api_response TEXT,
    success INTEGER DEFAULT 0,
    error_message TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_appointments_patient ON appointments_log(patient_phone_hash);
CREATE INDEX IF NOT EXISTS idx_appointments_action ON appointments_log(action, created_at);

-- ====================================================
-- TABELA: handoffs_to_jaqueline
-- ====================================================
CREATE TABLE IF NOT EXISTS handoffs_to_jaqueline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_phone_hash TEXT NOT NULL,
    patient_phone_last4 TEXT NOT NULL,
    patient_name TEXT,
    subject TEXT,
    triggered_by TEXT,
    redirected_to TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_handoffs_created ON handoffs_to_jaqueline(created_at);

-- ====================================================
-- TABELA: manual_interventions
-- ====================================================
CREATE TABLE IF NOT EXISTS manual_interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_phone_hash TEXT NOT NULL,
    intervention_text TEXT,
    bot_state_at_intervention TEXT,
    bot_paused_until TEXT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ====================================================
-- TABELA: bot_sent_messages
-- ====================================================
CREATE TABLE IF NOT EXISTS bot_sent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_phone TEXT NOT NULL,
    message_text_hash TEXT NOT NULL,
    message_preview TEXT,
    source TEXT NOT NULL DEFAULT 'BOT_API',
    sent_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_bot_sent ON bot_sent_messages(recipient_phone, message_text_hash);

-- ====================================================
-- TABELA: magic_links
-- ====================================================
CREATE TABLE IF NOT EXISTS magic_links (
    token TEXT PRIMARY KEY,
    authorized_phone TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    used INTEGER DEFAULT 0,
    used_at TEXT
);

-- ====================================================
-- TABELA: appointment_reminders
-- ====================================================
CREATE TABLE IF NOT EXISTS appointment_reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_phone TEXT NOT NULL,
    patient_name TEXT,
    appointment_datetime TEXT NOT NULL,
    reminder_send_at TEXT NOT NULL,
    sent INTEGER DEFAULT 0,
    sent_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_reminders_pending ON appointment_reminders(sent, reminder_send_at);
"""


def migrate_db():
    """Aplica migrações incrementais no banco existente."""
    conn = sqlite3.connect(DB_PATH)
    migrations = [
        "ALTER TABLE patient_sessions ADD COLUMN collected_document TEXT",
        "ALTER TABLE patient_sessions ADD COLUMN last_appointment_id TEXT",
        "ALTER TABLE patient_sessions ADD COLUMN is_retorno INTEGER DEFAULT 0",
        # Tabela de lembretes (CREATE não precisa de ALTER, mas garantimos via executescript)
        """CREATE TABLE IF NOT EXISTS appointment_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_phone TEXT NOT NULL,
            patient_name TEXT,
            appointment_datetime TEXT NOT NULL,
            reminder_send_at TEXT NOT NULL,
            sent INTEGER DEFAULT 0,
            sent_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_reminders_pending ON appointment_reminders(sent, reminder_send_at)",
        "ALTER TABLE patient_sessions ADD COLUMN collected_price_info TEXT",
        "ALTER TABLE patient_sessions ADD COLUMN confirm_failures INTEGER DEFAULT 0",
        "ALTER TABLE bot_status ADD COLUMN is_dormindo INTEGER DEFAULT 0",
        """CREATE TABLE IF NOT EXISTS supervisors (
            phone TEXT PRIMARY KEY,
            added_by TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            paused_until TEXT,
            active INTEGER DEFAULT 1
        )""",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
            conn.commit()
            print(f"✅ Migration aplicada: {sql[:60]}")
        except sqlite3.OperationalError:
            pass  # Coluna já existe
    conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    migrate_db()
    print(f"✅ Schema criado em {DB_PATH}")


def seed_default_instructions():
    """Insere instruções iniciais com as regras atuais do consultório."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM lorena_instructions WHERE category='INICIAL'")
    if cur.fetchone()[0] > 0:
        print("ℹ️  Instruções iniciais já cadastradas")
        conn.close()
        return
    initial_instructions = [
        ("Consultas atendidas: Particulares e Bradesco Nacional", "PLANO", 8),
        ("Pacientes Unimed, SulAmérica, Amil e outros particulares têm desconto: R$ 280", "PRECO", 8),
        ("Valor consulta particular ou Bradesco: R$ 230 (retorno gratuito em até 21 dias)", "PRECO", 9),
        ("Parcelamento da consulta em até 2x sem juros", "PRECO", 7),
        ("Atendemos SOMENTE consultas — NÃO realizamos exames", "GERAL", 9),
        ("Atendimento apenas segunda e quarta-feira no período da tarde", "HORARIO", 9),
        ("Endereço: Shopping 33, torre 3, sala 1502, Vila Santa Cecília, Volta Redonda", "LOCALIZACAO", 7),
    ]
    for text, category, priority in initial_instructions:
        cur.execute("""
            INSERT INTO lorena_instructions
                (instruction_text, category, priority, created_by_phone, created_via, notes)
            VALUES (?, ?, ?, 'SYSTEM', 'seed', 'Cadastrada na inicialização')
        """, (text, category, priority))
    conn.commit()
    conn.close()
    print(f"✅ {len(initial_instructions)} instruções iniciais cadastradas")


if __name__ == "__main__":
    init_db()
    seed_default_instructions()
