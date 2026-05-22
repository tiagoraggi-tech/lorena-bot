"""
consultorio_api.py — Integração com consultorio.me

⚠️  GERADO POR COWORK COM BASE EM INVESTIGAÇÃO DA API REAL VIA CHROME DEVTOOLS.
    Confirme cada endpoint testando antes de ir pra produção.

ENDPOINTS DESCOBERTOS (via Network tab, navegação read-only na agenda):
  GET  /agenda/loaddate/{YYYYMMDD}              → slots/agenda de uma data
  GET  /agenda/_listaagenda?dia=&mes=&ano=       → lista consultas agendadas
  GET  /agenda/navdata/{YYYYMMDD}               → dados de navegação
  GET  /agenda/infocalendario/?ano=&mes=         → info do mês
  GET  /clinica/getapikey                        → obtém API key da clínica
  GET  /util/gettipos/{clinica_id}?meros={pro_id}→ tipos de consulta disponíveis

ENDPOINTS NÃO CONFIRMADOS (criação/cancelamento não foram testados):
  POST /agenda/create  (ou similar) → [TODO: confirmar com Tiago]
  POST/DELETE /agenda/cancel        → [TODO: confirmar com Tiago]

MÉTODO DE AUTH:
  - Interface web usa sessão/cookie
  - ClientId + Secret via Basic Auth para API programática (a confirmar)
  - CONSULTORIO_AUTH_METHOD=basic no .env

SANDBOX:
  - INCERTO — não encontrado sandbox explícito; testar com cautela

IDs INTERNOS OBSERVADOS NA UI:
  - Clínica ID: 2782
  - Profissional (meros): 837
  Esses IDs podem variar. O PRO_ID do .env (dkvdtgk...) é o identificador externo.
"""
import os
import base64
import json
import logging
import requests
from datetime import datetime, date, timedelta
from typing import Optional
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("consultorio_api")

# ===== Configuração =====
BASE_URL = os.getenv("CONSULTORIO_API_BASE", "https://consultorio.me")
PRO_ID   = os.getenv("CONSULTORIO_PRO_ID")
CLIENT_ID = os.getenv("CONSULTORIO_CLIENT_ID")
SECRET    = os.getenv("CONSULTORIO_SECRET")
AUTH_METHOD = os.getenv("CONSULTORIO_AUTH_METHOD", "basic")

if not CLIENT_ID or CLIENT_ID == "<TIAGO_PREENCHE>":
    log.warning("⚠️ CONSULTORIO_CLIENT_ID não configurado — API em modo stub")


def _auth_headers() -> dict:
    """
    Retorna headers de autenticação.
    Método confirmado: Basic Auth com ClientId:Secret em Base64.
    Se der 401, tente AUTH_METHOD=apikey e CONSULTORIO_API_KEY no .env.
    """
    if AUTH_METHOD == "basic":
        if not CLIENT_ID or not SECRET:
            raise ValueError("CONSULTORIO_CLIENT_ID e CONSULTORIO_SECRET são obrigatórios")
        raw = f"{CLIENT_ID}:{SECRET}"
        encoded = base64.b64encode(raw.encode()).decode()
        return {
            "Authorization": f"Basic {encoded}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    elif AUTH_METHOD == "bearer":
        # [TODO: confirmar com Tiago se houver OAuth2 token endpoint]
        token = os.getenv("CONSULTORIO_BEARER_TOKEN")
        if not token:
            raise ValueError("CONSULTORIO_BEARER_TOKEN não configurado")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
    elif AUTH_METHOD == "apikey":
        # Alternativa: usar a API key obtida via /clinica/getapikey
        api_key = os.getenv("CONSULTORIO_API_KEY")
        return {
            "X-API-Key": api_key,
            "Content-Type": "application/json",
        }
    else:
        raise ValueError(f"AUTH_METHOD desconhecido: {AUTH_METHOD}")


def _date_to_yyyymmdd(date_str: str) -> str:
    """Converte YYYY-MM-DD → YYYYMMDD (formato interno consultorio.me)."""
    return date_str.replace("-", "")


def get_available_times(date_str: str) -> list[dict] | dict:
    """
    Busca horários disponíveis em uma data.

    Args:
        date_str: YYYY-MM-DD (ex: "2026-05-26")

    Returns:
        Lista de slots: [{"DateTime": "2026-05-26T14:00:00", "TimeSlotId": "..."}, ...]
        Ou {"error": "..."} se falhar.

    Endpoint descoberto: GET /agenda/loaddate/{YYYYMMDD}
    [TODO: confirmar formato exato da resposta JSON com Tiago após teste real]
    """
    if not is_api_configured():
        return {"error": "API não configurada — preencha CONSULTORIO_CLIENT_ID e CONSULTORIO_SECRET no .env"}

    date_fmt = _date_to_yyyymmdd(date_str)
    url = f"{BASE_URL}/agenda/loaddate/{date_fmt}"

    try:
        r = requests.get(url, headers=_auth_headers(), timeout=10)
        r.raise_for_status()
        data = r.json()
        log.info("get_available_times(%s) → %d itens", date_str, len(data) if isinstance(data, list) else 1)

        # [TODO: ajustar parsing conforme resposta real]
        # Tentativa 1: resposta é lista direta
        if isinstance(data, list):
            return _normalize_slots(data)
        # Tentativa 2: resposta é dict com chave "slots" ou similar
        if isinstance(data, dict):
            for key in ("slots", "horarios", "available", "data", "items"):
                if key in data:
                    return _normalize_slots(data[key])
            # Retorna tudo pra Tiago inspecionar
            return {"raw": data, "error": "formato inesperado — inspecione 'raw' e ajuste o parsing"}
        return {"error": f"Resposta inesperada: {type(data)}"}

    except requests.exceptions.HTTPError as e:
        log.error("HTTP error get_available_times: %s %s", e.response.status_code, e.response.text[:200])
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except requests.exceptions.RequestException as e:
        log.error("get_available_times failed: %s", e)
        return {"error": str(e)}


def _normalize_slots(raw_slots: list) -> list[dict]:
    """
    Normaliza slots pro formato esperado pelo webhook: [{"DateTime": "...", "TimeSlotId": "..."}]

    [TODO: ajustar campos conforme resposta real da API]
    Candidatos comuns para DateTime: "datetime", "data", "horario", "start", "DataHora"
    Candidatos comuns para TimeSlotId: "id", "slotId", "idAgenda", "token"
    """
    normalized = []
    for slot in raw_slots:
        if not isinstance(slot, dict):
            continue
        dt = (slot.get("DateTime") or slot.get("datetime") or
              slot.get("DataHora") or slot.get("data") or
              slot.get("horario") or slot.get("start"))
        sid = (slot.get("TimeSlotId") or slot.get("id") or
               slot.get("slotId") or slot.get("idAgenda") or
               slot.get("token") or str(dt))
        if dt:
            normalized.append({"DateTime": dt, "TimeSlotId": str(sid), "_raw": slot})
    return normalized


def create_appointment(name: str, phone: str, date_time: str,
                       time_slot_id: str, birth_date: str = "1900-01-01") -> dict:
    """
    Cria agendamento no consultorio.me.

    Args:
        name: nome completo do paciente
        phone: telefone com DDD (sem +55), ex: "24988001234"
        date_time: ISO 8601, ex: "2026-05-26T14:00:00"
        time_slot_id: ID do slot retornado por get_available_times
        birth_date: data de nascimento YYYY-MM-DD (default placeholder)

    Returns:
        {"success": True, "appointment_id": "..."} ou {"error": "..."}

    [TODO: ENDPOINT NÃO CONFIRMADO — descobrir com Tiago]
    Candidatos baseados no padrão da UI:
      POST /agenda/create
      POST /agenda/novaConsulta
      POST /agenda/save
    Payload provável (a confirmar via DevTools ao criar consulta de teste):
      {"pro_id": PRO_ID, "nome": name, "telefone": phone, "dataHora": date_time,
       "slotId": time_slot_id, "dataNascimento": birth_date}
    """
    if not is_api_configured():
        return {"error": "API não configurada"}

    # [TODO: confirmar endpoint real com Tiago]
    # Tente: POST /agenda/create — se der 404, tente /agenda/novaConsulta
    url = f"{BASE_URL}/agenda/create"

    payload = {
        "pro_id": PRO_ID,
        "patient_name": name,
        "patient_phone": phone,
        "birth_date": birth_date,
        "appointment_datetime": date_time,
        "time_slot_id": time_slot_id,
    }

    try:
        r = requests.post(url, headers=_auth_headers(), json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        log.info("create_appointment OK para %s em %s", name[:20], date_time)
        # Normaliza resposta
        appt_id = (data.get("id") or data.get("appointment_id") or
                   data.get("idAgenda") or data.get("idConsulta") or "unknown")
        return {"success": True, "appointment_id": str(appt_id), "_raw": data}
    except requests.exceptions.HTTPError as e:
        log.error("create_appointment HTTP error: %s", e.response.text[:300])
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except requests.exceptions.RequestException as e:
        log.error("create_appointment failed: %s", e)
        return {"error": str(e)}


def cancel_appointment(appointment_id: str) -> dict:
    """
    Cancela agendamento.

    [TODO: ENDPOINT NÃO CONFIRMADO — descobrir com Tiago]
    Candidatos:
      DELETE /agenda/{appointment_id}
      POST   /agenda/cancel/{appointment_id}
      POST   /agenda/cancelar
    [COWORK_NEVER_TEST_IN_PROD] Não cancele agendamentos reais durante testes.
    """
    if not is_api_configured():
        return {"error": "API não configurada"}

    # [TODO: confirmar endpoint]
    url = f"{BASE_URL}/agenda/{appointment_id}/cancel"

    try:
        r = requests.post(url, headers=_auth_headers(), timeout=10)
        r.raise_for_status()
        log.info("cancel_appointment OK: %s", appointment_id)
        return {"success": True, "_raw": r.json() if r.text else {}}
    except requests.exceptions.HTTPError as e:
        log.error("cancel_appointment HTTP error: %s", e.response.text[:200])
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except requests.exceptions.RequestException as e:
        log.error("cancel_appointment failed: %s", e)
        return {"error": str(e)}


def get_agenda_for_month(year: int, month: int) -> dict:
    """
    [EXTRA] Retorna informações do calendário para um mês.
    Endpoint confirmado: GET /agenda/infocalendario/?ano={year}&mes={month}
    """
    if not is_api_configured():
        return {"error": "API não configurada"}
    url = f"{BASE_URL}/agenda/infocalendario/"
    try:
        r = requests.get(url, headers=_auth_headers(),
                         params={"ano": year, "mes": month}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def is_api_configured() -> bool:
    """Retorna True se temos as credenciais mínimas necessárias."""
    has_client = CLIENT_ID and CLIENT_ID != "<TIAGO_PREENCHE>"
    has_secret = SECRET and SECRET != "<TIAGO_PREENCHE>"
    return bool(BASE_URL and has_client and has_secret)


if __name__ == "__main__":
    print(f"API configurada: {is_api_configured()}")
    print(f"BASE_URL: {BASE_URL}")
    print(f"AUTH_METHOD: {AUTH_METHOD}")
    print(f"PRO_ID: {PRO_ID[:20] if PRO_ID else 'NÃO DEFINIDO'}...")

    if is_api_configured():
        # Teste leve: tenta listar slots de uma segunda-feira futura
        today = date.today()
        days_ahead = (7 - today.weekday()) % 7 or 7  # próxima segunda
        test_date = (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        print(f"\nTestando get_available_times para {test_date} (segunda-feira)...")
        result = get_available_times(test_date)
        if "error" in result:
            print(f"❌ Erro: {result['error']}")
        else:
            print(f"✅ {len(result)} slots retornados")
            if result:
                print(f"   Primeiro slot: {result[0]}")
    else:
        print("\n⚠️  Preencha CONSULTORIO_CLIENT_ID e CONSULTORIO_SECRET no .env para testar")
        print("   Depois rode: python consultorio_api.py")
