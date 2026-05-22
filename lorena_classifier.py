"""
lorena_classifier.py -- Detector hibrido de intencao
Regex first: comandos obvios, keywords fortes.
LLM fallback: mensagens ambiguas.
"""
import os
import re
import json
import logging
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_core.messages import SystemMessage, HumanMessage

load_dotenv()
log = logging.getLogger("lorena.classifier")

REGEX_PATTERNS = {
    "FALAR_HUMANO": re.compile(
        r"\b(falar|conversar|atendente|humano|secret[aá]ria|jaqueline|pessoa)\b", re.IGNORECASE),
    "CANCELAR_CONSULTA": re.compile(
        r"\b(cancelar|desmarcar|desistir|n[aã]o vou)\b", re.IGNORECASE),
    "REMARCAR_CONSULTA": re.compile(
        r"\b(remarcar|reagendar|mudar (consulta|hor[aá]rio|dia))\b", re.IGNORECASE),
    "AGRADECIMENTO": re.compile(
        r"^(obrigad[oa]|valeu|tks|thank|brigad[oa]|tchau|at[eé] (logo|mais|depois))\b", re.IGNORECASE),
    "CONFIRMACAO_POSITIVA": re.compile(
        r"^(sim|s|ok|pode|claro|certo|tudo bem|t[aá] (bom|certo|ok)|confirmo|isso|perfeito|[oó]timo|quero)\b",
        re.IGNORECASE),
    "CONFIRMACAO_NEGATIVA": re.compile(
        r"^(n[aã]o?|outro|outra|pr[oó]ximo|diferente|mais tarde|n[aã]o pode|n[aã]o posso)\b",
        re.IGNORECASE),
}

CLINICAL_KEYWORDS = [
    "dor", "doi", "doendo", "incha", "inchado", "edema", "vermelho", "estala",
    "trava", "amortece", "amortecido", "formig", "fraqueza", "rigidez",
    "lesao", "ruptura", "rompi", "fratura", "menisco", "ligamento",
    "tendinite", "bursite", "artrose", "hernia", "ciatica",
    "medicamento", "remedio", "receita", "prescricao",
    "anti-inflamatorio", "fisioterapia", "fisio",
    "cirurgia", "operar", "operacao", "infiltracao",
    "ressonancia", "raio-x", "ultrassom", "tomografia",
    "pos-operatorio", "ponto", "curativo", "cicatrizacao",
    "atestado", "laudo", "afastamento", "INSS",
]
CLINICAL_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(kw) for kw in CLINICAL_KEYWORDS) + r")\b", re.IGNORECASE)

INFO_KEYWORDS = [
    "preco", "valor", "quanto", "custa", "custo", "particular",
    "plano", "convenio", "bradesco", "unimed", "sulamerica",
    "amil", "horario", "atende", "atendimento",
    "endereco", "onde fica", "localizacao",
    "estacionamento", "shopping", "rua", "torre", "sala",
    "parcelamento", "parcela", "exame", "exames",
]
INFO_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(kw) for kw in INFO_KEYWORDS) + r")\b", re.IGNORECASE)

LLM_CLASSIFIER_PROMPT = """Voce classifica intencao de paciente em mensagem de WhatsApp pra consultorio de ortopedia.
INTENCOES POSSIVEIS:
- AGENDAR_CONSULTA: quer marcar consulta
- CANCELAR_CONSULTA: quer cancelar/desmarcar
- REMARCAR_CONSULTA: quer reagendar
- DUVIDA_CLINICA: pergunta sobre sintoma, doenca, medicacao, exame, tratamento, cirurgia, atestado
- INFO_CONSULTORIO: pergunta preco, plano, horario, endereco, parcelamento
- FALAR_HUMANO: quer atendente humana
- AGRADECIMENTO: obrigado, tchau, small talk
- UNCLEAR: nao entendi

Retorne APENAS JSON (sem markdown):
{"intent": "...", "confidence": "HIGH"|"MEDIUM"|"LOW", "reasoning": "1 frase"}

EXEMPLOS:
"Bom dia, quero marcar consulta" -> {"intent":"AGENDAR_CONSULTA","confidence":"HIGH","reasoning":"pedido direto"}
"Atende Unimed?" -> {"intent":"INFO_CONSULTORIO","confidence":"HIGH","reasoning":"pergunta sobre plano"}
"Meu joelho ta doendo, posso ir ai?" -> {"intent":"DUVIDA_CLINICA","confidence":"HIGH","reasoning":"sintoma clinico"}
"Oi" -> {"intent":"UNCLEAR","confidence":"LOW","reasoning":"cumprimento sem contexto"}
"""


class LorenaClassifier:
    def __init__(self):
        self.llm = ChatGroq(
            api_key=os.getenv("GROQ_API_KEY"),
            model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
            temperature=0.1,
            max_tokens=256,
        )

    def classify(self, message: str, current_state: str = "NEW") -> dict:
        msg = message.strip()
        msg_lower = msg.lower()
        if not msg:
            return {"intent": "UNCLEAR", "confidence": "LOW",
                    "source": "empty", "reasoning": "Mensagem vazia"}

        if current_state == "AWAITING_CONFIRMATION":
            if REGEX_PATTERNS["CONFIRMACAO_POSITIVA"].search(msg_lower):
                return {"intent": "CONFIRMACAO_POSITIVA", "confidence": "HIGH",
                        "source": "regex", "reasoning": "sim/ok no contexto de confirmacao"}
            if REGEX_PATTERNS["CONFIRMACAO_NEGATIVA"].search(msg_lower):
                return {"intent": "CONFIRMACAO_NEGATIVA", "confidence": "HIGH",
                        "source": "regex", "reasoning": "nao/outro no contexto de confirmacao"}

        if REGEX_PATTERNS["FALAR_HUMANO"].search(msg):
            return {"intent": "FALAR_HUMANO", "confidence": "HIGH",
                    "source": "regex", "reasoning": "mencao a humano/atendente"}
        if REGEX_PATTERNS["CANCELAR_CONSULTA"].search(msg):
            return {"intent": "CANCELAR_CONSULTA", "confidence": "HIGH",
                    "source": "regex", "reasoning": "verbo cancelar/desmarcar"}
        if REGEX_PATTERNS["REMARCAR_CONSULTA"].search(msg):
            return {"intent": "REMARCAR_CONSULTA", "confidence": "HIGH",
                    "source": "regex", "reasoning": "verbo remarcar"}
        if REGEX_PATTERNS["AGRADECIMENTO"].search(msg):
            return {"intent": "AGRADECIMENTO", "confidence": "HIGH",
                    "source": "regex", "reasoning": "small talk"}
        if CLINICAL_PATTERN.search(msg):
            return {"intent": "DUVIDA_CLINICA", "confidence": "HIGH",
                    "source": "keywords", "reasoning": "keyword clinica detectada"}
        if INFO_PATTERN.search(msg):
            return {"intent": "INFO_CONSULTORIO", "confidence": "MEDIUM",
                    "source": "keywords", "reasoning": "keyword administrativa"}

        try:
            response = self.llm.invoke([
                SystemMessage(content=LLM_CLASSIFIER_PROMPT),
                HumanMessage(content=msg),
            ])
            raw = re.sub(r"```(?:json)?\n?|\n?```", "", response.content).strip()
            data = json.loads(raw)
            data["source"] = "llm"
            return data
        except Exception as e:
            log.error("LLM classify failed: %s", e)
            return {"intent": "UNCLEAR", "confidence": "LOW",
                    "source": "fallback", "reasoning": f"LLM error: {e}"}


if __name__ == "__main__":
    classifier = LorenaClassifier()
    tests = [
        ("quero marcar consulta", "NEW"),
        ("atende Bradesco?", "NEW"),
        ("meu joelho ta doendo", "NEW"),
        ("sim", "AWAITING_CONFIRMATION"),
        ("nao", "AWAITING_CONFIRMATION"),
        ("outro horario", "AWAITING_CONFIRMATION"),
        ("obrigado", "NEW"),
        ("quero falar com alguem", "NEW"),
        ("preciso de atestado", "NEW"),
    ]
    for msg, state in tests:
        result = classifier.classify(msg, state)
        print(f"{msg:35} [{state:25}] -> {result['intent']:25} ({result['confidence']}, {result['source']})")
