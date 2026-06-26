"""Cliente de Gemini 2.5 Flash (SDK unificado google-genai) para uso por voz.

Las respuestas se piden cortas, naturales y sin markdown para que Alexa las lea
bien en voz alta. Se incluye un sanitizador defensivo por si el modelo igual
devuelve símbolos de formato.
"""

import asyncio
import logging
import re

from google import genai
from google.genai import types

from .config import get_settings

logger = logging.getLogger(__name__)

# Instrucción de sistema: tono conversacional, en español, sin formato de texto.
SYSTEM_INSTRUCTION = (
    "Sos un asistente de voz integrado en un parlante Alexa. "
    "Tus respuestas las va a leer en voz alta una voz sintética, así que: "
    "respondé en español, de forma breve (2 o 3 frases como máximo), natural y "
    "conversacional. No uses markdown, ni asteriscos, ni guiones de lista, ni "
    "numeraciones, ni emojis, ni tablas, ni bloques de código. No uses URLs ni "
    "direcciones largas. Si la pregunta es ambigua, pedí una aclaración corta. "
    "Si no sabés algo, decilo con honestidad en una frase."
)

# Caracteres y patrones de markdown que no deben llegar a la voz.
_MARKDOWN_CHARS = re.compile(r"[*_`#>~]+")
_BULLET_LINE = re.compile(r"^\s*([-*•]|\d+[.)])\s+", re.MULTILINE)
_MULTISPACE = re.compile(r"[ \t]+")
_MULTINEWLINE = re.compile(r"\n{2,}")


class GeminiError(Exception):
    """Error al consultar Gemini (timeout, API caída, respuesta vacía, etc.)."""


def sanitize_for_speech(text: str) -> str:
    """Limpia markdown y formato para que el texto suene bien leído en voz alta."""
    if not text:
        return ""
    text = _BULLET_LINE.sub("", text)
    text = _MARKDOWN_CHARS.sub("", text)
    text = _MULTINEWLINE.sub(". ", text)
    text = text.replace("\n", " ")
    text = _MULTISPACE.sub(" ", text)
    return text.strip()


# El cliente de google-genai es reutilizable y thread-safe; se crea una sola vez.
_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.gemini_api_key:
            raise GeminiError("GEMINI_API_KEY no está configurada.")
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def _build_contents(question: str, history: list[dict]) -> list[types.Content]:
    """Arma la lista de contents incluyendo el historial de la sesión.

    `history` es una lista de turnos {"role": "user"|"model", "text": "..."}.
    """
    contents: list[types.Content] = []
    for turn in history:
        role = turn.get("role")
        text = turn.get("text", "")
        if role in ("user", "model") and text:
            contents.append(
                types.Content(role=role, parts=[types.Part(text=text)])
            )
    contents.append(types.Content(role="user", parts=[types.Part(text=question)]))
    return contents


async def ask_gemini(question: str, history: list[dict] | None = None) -> str:
    """Consulta a Gemini y devuelve la respuesta lista para leer en voz.

    Lanza GeminiError ante timeout, fallo del SDK o respuesta vacía, para que
    el handler pueda dar una respuesta de voz amigable.
    """
    settings = get_settings()
    history = history or []
    client = _get_client()

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        max_output_tokens=300,
        temperature=0.7,
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )

    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=settings.gemini_model,
                contents=_build_contents(question, history),
                config=config,
            ),
            timeout=settings.gemini_timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        logger.warning("Gemini tardó más de %ss", settings.gemini_timeout_seconds)
        raise GeminiError("timeout") from exc
    except Exception as exc:  # noqa: BLE001 - cualquier fallo del SDK → voz amigable
        logger.exception("Fallo al consultar Gemini")
        raise GeminiError(str(exc)) from exc

    text = (response.text or "").strip()
    if not text:
        raise GeminiError("respuesta vacía de Gemini")

    return sanitize_for_speech(text)
