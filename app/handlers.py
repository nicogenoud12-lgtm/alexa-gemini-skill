"""Routing de requests de Alexa a respuestas JSON, con integración a Gemini.

Modelo de interacción (ver README): la pregunta del usuario llega por un intent
de captura con un slot custom FREE_TEXT (`AskGeminiIntent`). El sample `{query}`
permite capturar cualquier frase sin necesidad de carrier phrases.
AMAZON.FallbackIntent queda como red de seguridad que pide reformular.
"""

import logging

from .config import get_settings
from .gemini_client import GeminiError, ask_gemini

logger = logging.getLogger(__name__)

ASK_INTENT = "AskGeminiIntent"
QUERY_SLOT = "query"

WELCOME = (
    "Hola. Soy tu asistente con inteligencia artificial. "
    "Preguntame lo que quieras."
)
WELCOME_REPROMPT = "¿Qué te gustaría preguntar?"
GOODBYE = "Hasta luego."
REPROMPT = "¿Querés preguntar algo más?"
FALLBACK_REPROMPT = "Probá preguntándomelo de otra forma."
ERROR_SPEECH = (
    "Perdón, no pude consultarlo en este momento. ¿Probamos de nuevo?"
)
NO_QUESTION_SPEECH = "No te entendí bien. ¿Podés repetir la pregunta?"


def _response(speech: str, *, reprompt: str | None = None,
              end_session: bool = False, session_attributes: dict | None = None) -> dict:
    """Construye una respuesta JSON con el formato que espera Alexa."""
    response: dict = {
        "outputSpeech": {"type": "PlainText", "text": speech},
        "shouldEndSession": end_session,
    }
    if reprompt is not None:
        response["reprompt"] = {
            "outputSpeech": {"type": "PlainText", "text": reprompt}
        }
    return {
        "version": "1.0",
        "sessionAttributes": session_attributes or {},
        "response": response,
    }


def _history_from(payload: dict) -> list[dict]:
    """Recupera el historial de conversación desde los sessionAttributes."""
    attrs = payload.get("session", {}).get("attributes") or {}
    history = attrs.get("history")
    return history if isinstance(history, list) else []


def _trim_history(history: list[dict]) -> list[dict]:
    """Recorta el historial a los últimos MAX_HISTORY_TURNS intercambios."""
    max_messages = get_settings().max_history_turns * 2
    return history[-max_messages:]


def _extract_query(payload: dict) -> str:
    """Extrae el texto de la pregunta desde el slot del intent de captura."""
    intent = payload.get("request", {}).get("intent", {})
    slots = intent.get("slots") or {}
    slot = slots.get(QUERY_SLOT) or {}
    return (slot.get("value") or "").strip()


async def _handle_question(question: str, history: list[dict]) -> dict:
    """Consulta a Gemini y arma la respuesta de voz + historial actualizado."""
    try:
        answer = await ask_gemini(question, history)
    except GeminiError:
        return _response(
            ERROR_SPEECH,
            reprompt=REPROMPT,
            end_session=False,
            session_attributes={"history": history},
        )

    new_history = _trim_history(
        history
        + [{"role": "user", "text": question}, {"role": "model", "text": answer}]
    )
    return _response(
        answer,
        reprompt=REPROMPT,
        end_session=False,
        session_attributes={"history": new_history},
    )


async def handle_request(payload: dict) -> dict:
    """Despacha una request de Alexa según su tipo / intent."""
    request = payload.get("request", {})
    request_type = request.get("type")

    if request_type == "LaunchRequest":
        return _response(WELCOME, reprompt=WELCOME_REPROMPT, end_session=False)

    if request_type == "SessionEndedRequest":
        return _response("", end_session=True)

    if request_type == "IntentRequest":
        intent_name = request.get("intent", {}).get("name")
        history = _history_from(payload)

        if intent_name in ("AMAZON.StopIntent", "AMAZON.CancelIntent"):
            return _response(GOODBYE, end_session=True)

        if intent_name == "AMAZON.HelpIntent":
            return _response(WELCOME, reprompt=WELCOME_REPROMPT, end_session=False)

        if intent_name == ASK_INTENT:
            question = _extract_query(payload)
            if not question:
                return _response(
                    NO_QUESTION_SPEECH,
                    reprompt=FALLBACK_REPROMPT,
                    end_session=False,
                    session_attributes={"history": history},
                )
            return await _handle_question(question, history)

        if intent_name == "AMAZON.FallbackIntent":
            return _response(
                NO_QUESTION_SPEECH,
                reprompt=FALLBACK_REPROMPT,
                end_session=False,
                session_attributes={"history": history},
            )

    logger.info("Tipo de request no manejado: %s", request_type)
    return _response(NO_QUESTION_SPEECH, reprompt=REPROMPT, end_session=False)
