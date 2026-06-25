"""FastAPI app que expone el endpoint de la Alexa Custom Skill."""

import json
import logging

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .alexa_verifier import AlexaVerificationError, verify_request
from .handlers import handle_request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Alexa → Gemini Fallback Skill")


@app.get("/health")
async def health() -> dict:
    """Healthcheck simple para CasaOS / uptime."""
    return {"status": "ok"}


@app.post("/alexa")
async def alexa(request: Request) -> Response:
    """Endpoint principal: recibe el JSON del Alexa Skills Kit y responde."""
    # Importante: leer el body CRUDO antes de parsear (la firma se valida sobre él).
    body = await request.body()

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "JSON inválido"})

    try:
        verify_request(request.headers, body, payload)
    except AlexaVerificationError as exc:
        logger.warning("Verificación de Alexa fallida: %s", exc)
        return JSONResponse(status_code=400, content={"error": "verificación fallida"})

    response_body = await handle_request(payload)
    return JSONResponse(content=response_body)
