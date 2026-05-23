"""
consultorio_api.py — Integração com api.consultoriome.com

API REST oficial (documentação Postman: documenter.getpostman.com/view/1116511/2sA2rAyN3Y)

FLUXO DE AUTH:
  1. POST /token com clientId + secret → Bearer token
  2. Usar Bearer token em todos os demais endpoints

ENDPOINTS USADOS:
  POST /token                  → obtém Bearer token
  GET  /available-times        → horários disponíveis de um profissional
  POST /create-appointment     → cria agendamento
  POST /cancel-appointment     → cancela agendamento

VARIÁVEIS DE AMBIENTE:
  CONSULTORIO_CLIENT_ID   → clientId fornecido pelo consultorio.me
  CONSULTORIO_SECRET      → secret fornecido pelo consultorio.me
  CONSULTORIO_PRO_ID      → ID do profissional (Dr. Tiago) na plataforma
  CONSULTORIO_API_BASE    → base URL (padrão: https://api.consultoriome.com)
"""
import os
import json
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

# Cache do token em memória (renovado automaticamente quando expira)
_token_cache = {"token": None, "expires_at": None}


def is_api_configured() -> bool:
    return bool(CLIENT_ID and SECRET and CLIENT_ID != "<TIAGO_PREENCHE>")


def _get_token() -> str | None:
    """
    Obtém Bearer token via POST /token.
    Usa cache em memória — renova automaticamente quando expira.
    """
    global _token_cache
    now = datetime.utcnow()

    # Retorna token cacheado se ainda válido (com margem de 60s)
    if _token_cache["token"] and _token_cache["expires_at"]:
        if now < _token_cache["expires_at"]:
            return _token_cache["token"]

    if not is_api_configured():
        log.error("_get_token: API não configurada (CLIENT_ID ou SECRET ausente)")
        return None

    try:
        r = requests.post(
            f"{BASE_URL}/token",
            json={"clientId": CLIENT_ID, "secret": SECRET},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        log.debug("_get_token response: %s", str(data)[:200])

        # Extrai token — tenta campos comuns
        token = (data.get("token") or data.get("access_token") or
                 data.get("accessToken") or data.get("bearer"))
        if not token and isinstance(data, str):
            token = data  # Algumas APIs retornam o token direto como string

        if not token:
            log.error("_get_token: token não encontrado na resposta: %s", data)
            return None

        # Calcula expiração (padrão: 1 hora se não informado)
        expires_in = data.get("expiresIn") or data.get("expires_in") or 3600
        _token_cache = {
            "token": token,
            "expires_at": now + timedelta(seconds=int(expires_in) - 60),
        }
        log.info("_get_token: token obtido, expira em %ds", expires_in)
        return token

    except requests.exceptions.HTTPError as e:
        log.error("_get_token HTTP error %s: %s", e.response.status_code, e.response.text[:200])
        return None
    except Exception as e:
        log.error("_get_token falhou: %s", e)
        return None


def _auth_headers() -> dict:
    token = _get_token()
    if not token:
        raise ValueError("Não foi possível obter token de autenticação")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def get_available_times(date_str: str) -> list[dict] | dict:
    """
    Busca horários disponíveis em uma data.

    Args:
        date_str: YYYY-MM-DD (ex: "2026-05-26")

    Returns:
        Lista de slots: [{"DateTime": "2026-05-26T14:00:00", "TimeSlotId": "..."}, ...]
        Ou {"error": "..."} se falhar.
    """
    if not is_api_configured():
        return {"error": "API não configurada"}

    try:
        params = {"date": date_str}
        if PRO_ID:
            params["professionalId"] = PRO_ID

        r = requests.get(
            f"{BASE_URL}/available-times",
            headers=_auth_headers(),
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        log.info("get_available_times(%s) → %s", date_str, str(data)[:200])

        if isinstance(data, list):
            return _normalize_slots(data)
        if isinstance(data, dict):
            for key in ("slots", "horarios", "available", "data", "items", "times"):
                if key in data and isinstance(data[key], list):
                    return _normalize_slots(data[key])
            return {"error": "formato inesperado", "raw": data}

        return {"error": f"Resposta inesperada: {type(data)}"}

    except ValueError as e:
        # Falha ao obter token
        return {"error": str(e)}
    except requests.exceptions.HTTPError as e:
        log.error("get_available_times HTTP %s: %s", e.response.status_code, e.response.text[:200])
        return {"error": f"HTTP {e.response.status_code}"}
    except Exception as e:
        log.error("get_available_times falhou: %s", e)
        return {"error": str(e)}


def _normalize_slots(raw_slots: list) -> list[dict]:
    """Normaliza slots para o formato interno: [{"DateTime": "...", "TimeSlotId": "..."}]"""
    normalized = []
    for slot in raw_slots:
        if not isinstance(slot, dict):
            continue
        dt = (slot.get("DateTime") or slot.get("datetime") or slot.get("startTime") or
              slot.get("DataHora") or slot.get("data") or slot.get("start") or
              slot.get("time") or slot.get("horario"))
        sid = (slot.get("TimeSlotId") or slot.get("id") or slot.get("slotId") or
               slot.get("scheduleId") or slot.get("idAgenda") or str(dt))
        if dt:
            normalized.append({"DateTime": dt, "TimeSlotId": str(sid), "_raw": slot})
    return normalized


def find_next_available_slot(after_date: str = None, weeks_ahead: int = 8) -> dict:
    """
    Busca automaticamente o próximo horário disponível nas próximas segundas/quartas.

    Returns:
        {"date": "YYYY-MM-DD", "date_br": "DD/MM/YYYY", "weekday": "segunda/quarta", "slots": [...]}
        ou {"error": "..."} se nenhum slot encontrado
    """
    if not is_api_configured():
        return {"error": "API não configurada"}

    try:
        start = (datetime.strptime(after_date, "%Y-%m-%d").date()
                 if after_date else date.today())
    except Exception:
        start = date.today()

    current = start + timedelta(days=1)
    max_days = weeks_ahead * 7
    checked = 0
    weekday_names = {0: "segunda-feira", 2: "quarta-feira"}

    while checked < max_days:
        if current.weekday() in [0, 2]:
            date_str = current.strftime("%Y-%m-%d")
            slots = get_available_times(date_str)
            if isinstance(slots, list) and len(slots) > 0:
                log.info("find_next_available_slot: %d slot(s) em %s", len(slots), date_str)
                return {
                    "date": date_str,
                    "date_br": current.strftime("%d/%m/%Y"),
                    "weekday": weekday_names.get(current.weekday(), ""),
                    "slots": slots,
                }
            checked += 1
        current += timedelta(days=1)

    return {"error": "Nenhum horário disponível nas próximas semanas"}


def create_appointment(name: str, phone: str, date_time: str,
                       time_slot_id: str, birth_date: str = "") -> dict:
    """
    Cria agendamento via POST /create-appointment.

    Returns:
        {"success": True, "appointment_id": "..."} ou {"error": "..."}
    """
    if not is_api_configured():
        return {"error": "API não configurada"}

    payload = {
        "professionalId": PRO_ID,
        "patientName": name,
        "patientPhone": phone,
        "dateTime": date_time,
        "timeSlotId": time_slot_id,
    }
    if birth_date:
        payload["birthDate"] = birth_date

    try:
        r = requests.post(
            f"{BASE_URL}/create-appointment",
            headers=_auth_headers(),
            json=payload,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        log.info("create_appointment OK: %s em %s", name[:20], date_time)
        appt_id = (data.get("id") or data.get("appointmentId") or
                   data.get("appointment_id") or data.get("idAgenda") or "ok")
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
    Cancela agendamento via POST /cancel-appointment.
    """
    if not is_api_configured():
        return {"error": "API não configurada"}

    try:
        r = requests.post(
            f"{BASE_URL}/cancel-appointment",
            headers=_auth_headers(),
            json={"appointmentId": appointment_id},
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
