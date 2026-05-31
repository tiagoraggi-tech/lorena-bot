"""
consultorio_api.py — Integração com api.consultoriome.com

API REST oficial (documentação Postman: documenter.getpostman.com/view/1116511/2sA2rAyN3Y)

FLUXO DE AUTH:
  1. POST /v1/api/authorization/token com Basic Auth (base64(clientId:secret)) → Bearer token (string pura)
  2. Usar Bearer token nos demais endpoints
  Token válido por 24 horas.

ENDPOINTS USADOS:
  POST /v1/api/authorization/token                      → obtém Bearer token (string pura)
  GET  /v1/api/appointment/available-times/{proId}      → todos os horários disponíveis do profissional
  POST /v1/api/appointment/create-appointment           → cria agendamento
  POST /v1/api/appointment/cancel-appointment/{id}      → cancela agendamento

VARIÁVEIS DE AMBIENTE:
  CONSULTORIO_CLIENT_ID   → clientId fornecido pelo consultorio.me
  CONSULTORIO_SECRET      → secret fornecido pelo consultorio.me
  CONSULTORIO_PRO_ID      → ID do profissional (Dr. Tiago) na plataforma
  CONSULTORIO_API_BASE    → base URL (padrão: https://api.consultoriome.com)
"""
import os
import base64
import logging
import requests
from datetime import datetime, date, timedelta
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("consultorio_api")

# ===== Configuração =====
BASE_URL  = os.getenv("CONSULTORIO_API_BASE", "https://api.consultoriome.com")
PRO_ID    = os.getenv("CONSULTORIO_PRO_ID", "")
CLIENT_ID = os.getenv("CONSULTORIO_CLIENT_ID", "")
SECRET    = os.getenv("CONSULTORIO_SECRET", "")

# Cache do token em memória — válido por 24h conforme docs
_token_cache = {"token": None, "expires_at": None}


def is_api_configured() -> bool:
    return bool(CLIENT_ID and SECRET and CLIENT_ID != "<TIAGO_PREENCHE>")


def _basic_auth_header() -> str:
    """Gera header Basic Auth com base64(clientId:secret)."""
    credentials = f"{CLIENT_ID}:{SECRET}"
    encoded = base64.b64encode(credentials.encode()).decode()
    return f"Basic {encoded}"


def _get_token() -> str | None:
    """
    Obtém Bearer token via POST /v1/api/authorization/token.
    Auth: Basic base64(clientId:secret)
    Resposta: string pura (o token)
    Usa cache em memória — válido por 24h.
    """
    global _token_cache
    now = datetime.utcnow()

    # Retorna token cacheado se ainda válido (com margem de 5 min)
    if _token_cache["token"] and _token_cache["expires_at"]:
        if now < _token_cache["expires_at"]:
            return _token_cache["token"]

    if not is_api_configured():
        log.error("_get_token: API não configurada (CLIENT_ID ou SECRET ausente)")
        return None

    try:
        r = requests.post(
            f"{BASE_URL}/v1/api/authorization/token",
            headers={
                "Authorization": _basic_auth_header(),
                "Accept": "text/plain",
            },
            timeout=15,
        )
        r.raise_for_status()

        # A API retorna o token como string pura (não JSON)
        token = r.text.strip().strip('"')  # remove aspas se vier como JSON string

        if not token:
            log.error("_get_token: token vazio na resposta: %r", r.text[:200])
            return None

        # Token válido 24h — cacheamos por 23h55m
        _token_cache = {
            "token": token,
            "expires_at": now + timedelta(hours=23, minutes=55),
        }
        log.info("_get_token: token obtido com sucesso (válido 24h)")
        return token

    except requests.exceptions.HTTPError as e:
        log.error("_get_token HTTP error %s: %s", e.response.status_code, e.response.text[:200])
        return None
    except Exception as e:
        log.error("_get_token falhou: %s", e)
        return None


def _auth_headers(content_type: str = "application/json") -> dict:
    token = _get_token()
    if not token:
        raise ValueError("Não foi possível obter token de autenticação")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
        "Accept": "text/plain",
    }


def get_available_times(date_str: str) -> list[dict] | dict:
    """
    Busca horários disponíveis em uma data específica.
    A API retorna TODOS os slots do profissional; filtramos pela data aqui.

    Args:
        date_str: YYYY-MM-DD (ex: "2026-05-26")

    Returns:
        Lista de slots: [{"DateTime": "2026-05-26T14:00:00Z", "TimeSlotId": "..."}, ...]
        Ou {"error": "..."} se falhar.
    """
    if not is_api_configured():
        return {"error": "API não configurada"}

    if not PRO_ID:
        log.error("get_available_times: CONSULTORIO_PRO_ID não configurado")
        return {"error": "CONSULTORIO_PRO_ID não configurado"}

    try:
        r = requests.get(
            f"{BASE_URL}/v1/api/appointment/available-times/{PRO_ID}",
            headers=_auth_headers(),
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        log.info("get_available_times → %s", str(data)[:300])

        # Resposta: {"Slots": [{"TimeSlotId": "...", "DateTime": "..."}, ...]}
        slots = []
        if isinstance(data, dict) and "Slots" in data:
            slots = data["Slots"]
        elif isinstance(data, list):
            slots = data

        # Filtra pela data solicitada
        filtered = []
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            dt_raw = (slot.get("DateTime") or slot.get("dateTime") or
                      slot.get("datetime") or slot.get("DataHora") or "")
            slot_id = (slot.get("TimeSlotId") or slot.get("timeSlotId") or
                       slot.get("id") or str(dt_raw))
            if dt_raw and dt_raw.startswith(date_str):
                filtered.append({"DateTime": dt_raw, "TimeSlotId": str(slot_id), "_raw": slot})

        log.info("get_available_times(%s): %d slot(s) filtrado(s)", date_str, len(filtered))
        return filtered

    except ValueError as e:
        return {"error": str(e)}
    except requests.exceptions.HTTPError as e:
        log.error("get_available_times HTTP %s: %s", e.response.status_code, e.response.text[:200])
        return {"error": f"HTTP {e.response.status_code}"}
    except Exception as e:
        log.error("get_available_times falhou: %s", e)
        return {"error": str(e)}


def find_next_available_slot(after_date: str = None, weeks_ahead: int = 8) -> dict:
    """
    Busca automaticamente o próximo horário disponível nas próximas segundas/quartas.
    Busca todos os slots de uma vez e filtra por dia da semana.

    Returns:
        {"date": "YYYY-MM-DD", "date_br": "DD/MM/YYYY", "weekday": "segunda/quarta", "slots": [...]}
        ou {"error": "..."} se nenhum slot encontrado
    """
    if not is_api_configured():
        return {"error": "API não configurada"}

    if not PRO_ID:
        return {"error": "CONSULTORIO_PRO_ID não configurado"}

    try:
        start = (datetime.strptime(after_date, "%Y-%m-%d").date()
                 if after_date else date.today())
    except Exception:
        start = date.today()

    try:
        # Busca todos os slots disponíveis de uma vez
        r = requests.get(
            f"{BASE_URL}/v1/api/appointment/available-times/{PRO_ID}",
            headers=_auth_headers(),
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()

        all_slots = []
        if isinstance(data, dict) and "Slots" in data:
            all_slots = data["Slots"]
        elif isinstance(data, list):
            all_slots = data

    except Exception as e:
        log.error("find_next_available_slot: erro ao buscar slots: %s", e)
        return {"error": str(e)}

    # Agrupa slots por data e filtra por Mon/Wed após start
    from collections import defaultdict
    slots_by_date = defaultdict(list)
    for slot in all_slots:
        if not isinstance(slot, dict):
            continue
        dt_raw = (slot.get("DateTime") or slot.get("dateTime") or
                  slot.get("DataHora") or "")
        if not dt_raw:
            continue
        try:
            dt = datetime.fromisoformat(dt_raw.replace("Z", "+00:00"))
            slot_date = dt.date()
        except Exception:
            continue
        slot_id = (slot.get("TimeSlotId") or slot.get("timeSlotId") or
                   slot.get("id") or str(dt_raw))
        slots_by_date[slot_date].append({"DateTime": dt_raw, "TimeSlotId": str(slot_id), "_raw": slot})

    weekday_names = {0: "segunda-feira", 2: "quarta-feira"}
    max_date = start + timedelta(weeks=weeks_ahead)

    for slot_date in sorted(slots_by_date.keys()):
        if slot_date <= start:
            continue
        if slot_date > max_date:
            break
        if slot_date.weekday() in [0, 2]:
            slots = slots_by_date[slot_date]
            date_str = slot_date.strftime("%Y-%m-%d")
            log.info("find_next_available_slot: %d slot(s) em %s", len(slots), date_str)
            return {
                "date": date_str,
                "date_br": slot_date.strftime("%d/%m/%Y"),
                "weekday": weekday_names.get(slot_date.weekday(), ""),
                "slots": slots,
            }

    return {"error": "Nenhum horário disponível nas próximas semanas"}


def find_next_two_slots(after_date: str = None, weeks_ahead: int = 8, preferred_weekday: str = None) -> list:
    """
    Faz UMA chamada à API e retorna até 2 dicts de dias diferentes.
    Cada dict: {"date": "YYYY-MM-DD", "date_br": "...", "weekday": "...", "slots": [...]}
    preferred_weekday: "segunda" ou "quarta" para filtrar apenas esse dia.
    Retorna lista vazia se API falhar.
    """
    if not is_api_configured() or not PRO_ID:
        return []

    try:
        start = (datetime.strptime(after_date, "%Y-%m-%d").date()
                 if after_date else date.today())
    except Exception:
        start = date.today()

    try:
        r = requests.get(
            f"{BASE_URL}/v1/api/appointment/available-times/{PRO_ID}",
            headers=_auth_headers(),
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.error("find_next_two_slots: erro ao buscar slots: %s", e)
        return []

    all_slots = []
    if isinstance(data, dict) and "Slots" in data:
        all_slots = data["Slots"]
    elif isinstance(data, list):
        all_slots = data

    from collections import defaultdict
    slots_by_date = defaultdict(list)
    for slot in all_slots:
        if not isinstance(slot, dict):
            continue
        dt_raw = (slot.get("DateTime") or slot.get("dateTime") or
                  slot.get("DataHora") or "")
        if not dt_raw:
            continue
        try:
            dt = datetime.fromisoformat(dt_raw.replace("Z", "+00:00"))
            slot_date = dt.date()
        except Exception:
            continue
        slot_id = (slot.get("TimeSlotId") or slot.get("timeSlotId") or
                   slot.get("id") or str(dt_raw))
        slots_by_date[slot_date].append({"DateTime": dt_raw, "TimeSlotId": str(slot_id), "_raw": slot})

    weekday_names = {0: "segunda-feira", 2: "quarta-feira"}
    max_date = start + timedelta(weeks=weeks_ahead)
    results = []

    # Mapeamento de preferência de dia para número do dia da semana
    day_map = {"segunda": 0, "segunda-feira": 0, "quarta": 2, "quarta-feira": 2}
    preferred_wd = day_map.get((preferred_weekday or "").lower().strip())

    # Se há preferência, filtra só esse dia; senão aceita segunda e quarta
    allowed_weekdays = [preferred_wd] if preferred_wd is not None else [0, 2]

    for slot_date in sorted(slots_by_date.keys()):
        if slot_date <= start:
            continue
        if slot_date > max_date:
            break
        if slot_date.weekday() in allowed_weekdays:
            results.append({
                "date": slot_date.strftime("%Y-%m-%d"),
                "date_br": slot_date.strftime("%d/%m/%Y"),
                "weekday": weekday_names.get(slot_date.weekday(), ""),
                "slots": slots_by_date[slot_date],
            })
            if len(results) == 2:
                break

    log.info("find_next_two_slots: %d dia(s) encontrado(s) (filtro=%s)", len(results), preferred_weekday)
    return results


def create_appointment(name: str, phone: str, date_time: str,
                       time_slot_id: str, birth_date: str = "",
                       document: str = "") -> dict:
    """
    Cria agendamento via POST /v1/api/appointment/create-appointment.

    Args:
        name: nome do paciente
        phone: telefone do paciente
        date_time: datetime ISO 8601 (ex: "2026-05-26T14:00:00Z")
        time_slot_id: ID do slot (TimeSlotId retornado pela API)
        birth_date: data de nascimento ISO 8601 (opcional)
        document: CPF do paciente (obrigatório pela API)

    Returns:
        {"success": True, "appointment_id": "..."} ou {"error": "..."}
    """
    if not is_api_configured():
        return {"error": "API não configurada"}

    # Garante formato ISO 8601 com Z
    if date_time and "T" in date_time and not date_time.endswith("Z") and "+" not in date_time:
        date_time = date_time + "Z"

    # Normaliza CPF: remove pontos e traços
    doc_clean = "".join(c for c in document if c.isdigit()) if document else ""

    payload = {
        "ProId": PRO_ID,
        "Name": name,
        "Phone1": phone,
        "DateTime": date_time,
        "TimeSlotId": time_slot_id,
        "Document": doc_clean,
    }
    if birth_date:
        if "T" not in birth_date:
            birth_date = birth_date + "T00:00:00Z"
        payload["BirthDate"] = birth_date

    try:
        r = requests.post(
            f"{BASE_URL}/v1/api/appointment/create-appointment",
            headers=_auth_headers(),
            json=payload,
            timeout=20,
        )
        r.raise_for_status()

        # Resposta pode ser string pura ou JSON
        try:
            data = r.json()
        except Exception:
            data = r.text.strip()

        log.info("create_appointment OK: %s em %s", name[:20], date_time)
        if isinstance(data, dict):
            appt_id = (data.get("id") or data.get("appointmentId") or
                       data.get("Id") or data.get("idAgenda") or "ok")
        else:
            appt_id = str(data) if data else "ok"
        return {"success": True, "appointment_id": str(appt_id), "_raw": data}

    except ValueError as e:
        return {"error": str(e)}
    except requests.exceptions.HTTPError as e:
        log.error("create_appointment HTTP %s: %s", e.response.status_code, e.response.text[:300])
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        log.error("create_appointment falhou: %s", e)
        return {"error": str(e)}


def cancel_appointment(appointment_id: str) -> dict:
    """
    Cancela agendamento via POST /v1/api/appointment/cancel-appointment/{id}.
    Sem body — o ID vai na URL.
    """
    if not is_api_configured():
        return {"error": "API não configurada"}

    try:
        r = requests.post(
            f"{BASE_URL}/v1/api/appointment/cancel-appointment/{appointment_id}",
            headers=_auth_headers(),
            timeout=15,
        )
        r.raise_for_status()
        log.info("cancel_appointment OK: %s", appointment_id)
        return {"success": True}
    except ValueError as e:
        return {"error": str(e)}
    except requests.exceptions.HTTPError as e:
        log.error("cancel_appointment HTTP %s: %s", e.response.status_code, e.response.text[:200])
        return {"error": f"HTTP {e.response.status_code}"}
    except Exception as e:
        log.error("cancel_appointment falhou: %s", e)
        return {"error": str(e)}
