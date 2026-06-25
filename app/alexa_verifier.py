"""Verificación de requests de Alexa para endpoints HTTP propios (no Lambda).

Amazon EXIGE validar cada request o rechaza la certificación de la skill. Pasos
implementados (según la documentación oficial de Alexa Skills Kit):

  1. Validar la URL del header SignatureCertChainUrl.
  2. Descargar (y cachear) la cadena de certificados PEM.
  3. Validar el certificado de hoja: vigencia + SAN echo-api.amazon.com + cadena.
  4. Verificar la firma del body crudo con la clave pública del certificado.
  5. Validar el timestamp de la request (anti-replay, ±150 s).
  6. Validar el applicationId de la skill.

La verificación criptográfica (1-5) se puede desactivar con
VERIFY_ALEXA_SIGNATURE=false SOLO para pruebas locales con curl. El chequeo de
applicationId (6) se mantiene siempre que ALEXA_SKILL_ID esté configurado.
"""

import base64
import datetime as dt
import logging
from urllib.parse import urlparse, urljoin

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509.oid import ExtensionOID

from .config import get_settings

logger = logging.getLogger(__name__)

ALEXA_SAN = "echo-api.amazon.com"
MAX_TIMESTAMP_SKEW_SECONDS = 150
_CERT_CACHE: dict[str, list[x509.Certificate]] = {}


class AlexaVerificationError(Exception):
    """La request no pasó la verificación de Alexa → debe rechazarse (HTTP 400)."""


def _validate_cert_url(cert_url: str) -> None:
    """Valida que SignatureCertChainUrl cumpla las reglas de Amazon."""
    parsed = urlparse(cert_url)
    if parsed.scheme.lower() != "https":
        raise AlexaVerificationError("SignatureCertChainUrl no es https")
    if parsed.hostname is None or parsed.hostname.lower() != "s3.amazonaws.com":
        raise AlexaVerificationError("SignatureCertChainUrl host inválido")
    port = parsed.port or 443
    if port != 443:
        raise AlexaVerificationError("SignatureCertChainUrl puerto inválido")
    # Normalizar el path para evitar trucos con /../ y exigir el prefijo correcto.
    normalized = urljoin(cert_url, parsed.path)
    if not urlparse(normalized).path.startswith("/echo.api/"):
        raise AlexaVerificationError("SignatureCertChainUrl path inválido")


def _load_cert_chain(cert_url: str) -> list[x509.Certificate]:
    """Descarga (con caché) y parsea la cadena de certificados PEM."""
    if cert_url in _CERT_CACHE:
        return _CERT_CACHE[cert_url]

    try:
        resp = httpx.get(cert_url, timeout=5.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AlexaVerificationError(f"No se pudo descargar el certificado: {exc}") from exc

    certs = x509.load_pem_x509_certificates(resp.content)
    if not certs:
        raise AlexaVerificationError("La cadena de certificados está vacía")

    _CERT_CACHE[cert_url] = certs
    return certs


def _validate_cert_chain(certs: list[x509.Certificate]) -> x509.Certificate:
    """Valida vigencia, SAN del certificado de hoja y los enlaces de la cadena."""
    leaf = certs[0]

    now = dt.datetime.now(dt.timezone.utc)
    if now < leaf.not_valid_before_utc or now > leaf.not_valid_after_utc:
        raise AlexaVerificationError("Certificado de Alexa expirado o aún no válido")

    san = leaf.extensions.get_extension_for_oid(
        ExtensionOID.SUBJECT_ALTERNATIVE_NAME
    ).value
    dns_names = san.get_values_for_type(x509.DNSName)
    if ALEXA_SAN not in dns_names:
        raise AlexaVerificationError("El certificado no contiene el SAN de Alexa")

    # Verificar que cada certificado esté firmado por el siguiente de la cadena.
    for cert, issuer in zip(certs, certs[1:]):
        try:
            issuer.public_key().verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                padding.PKCS1v15(),
                cert.signature_hash_algorithm,
            )
        except InvalidSignature as exc:
            raise AlexaVerificationError("Cadena de certificados inválida") from exc

    return leaf


def _verify_signature(leaf: x509.Certificate, signature_b64: str, body: bytes,
                      hash_algo: hashes.HashAlgorithm) -> None:
    """Verifica la firma del body crudo con la clave pública del certificado."""
    try:
        signature = base64.b64decode(signature_b64)
    except (ValueError, TypeError) as exc:
        raise AlexaVerificationError("Firma mal formada") from exc

    try:
        leaf.public_key().verify(signature, body, padding.PKCS1v15(), hash_algo)
    except InvalidSignature as exc:
        raise AlexaVerificationError("La firma de la request no es válida") from exc


def _validate_timestamp(payload: dict) -> None:
    """Valida que el timestamp de la request esté dentro de la ventana permitida."""
    timestamp_str = (payload.get("request") or {}).get("timestamp")
    if not timestamp_str:
        raise AlexaVerificationError("La request no tiene timestamp")
    try:
        timestamp = dt.datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AlexaVerificationError("Timestamp con formato inválido") from exc

    skew = abs((dt.datetime.now(dt.timezone.utc) - timestamp).total_seconds())
    if skew > MAX_TIMESTAMP_SKEW_SECONDS:
        raise AlexaVerificationError("Timestamp fuera de la ventana permitida")


def _validate_application_id(payload: dict) -> None:
    """Valida que el applicationId coincida con la skill configurada."""
    settings = get_settings()
    if not settings.alexa_skill_id:
        return  # Sin skill id configurado no se valida (no recomendado en prod).

    app_id = (
        (payload.get("session") or {}).get("application", {}).get("applicationId")
        or (payload.get("context") or {}).get("System", {}).get("application", {}).get("applicationId")
    )
    if app_id != settings.alexa_skill_id:
        raise AlexaVerificationError("applicationId no coincide con la skill")


def verify_request(headers, body: bytes, payload: dict) -> None:
    """Punto de entrada: valida una request de Alexa. Lanza AlexaVerificationError.

    `headers` es un mapping case-insensitive (request.headers de FastAPI/Starlette).
    `body` son los bytes crudos del cuerpo. `payload` es el JSON ya parseado.
    """
    settings = get_settings()

    # El applicationId se valida siempre (es barato y evita uso cruzado de skills).
    _validate_application_id(payload)

    if not settings.verify_alexa_signature:
        logger.warning("VERIFY_ALEXA_SIGNATURE=false — firma NO verificada (sólo dev)")
        return

    cert_url = headers.get("SignatureCertChainUrl")
    if not cert_url:
        raise AlexaVerificationError("Falta el header SignatureCertChainUrl")

    # Preferir la firma SHA-256 (header Signature-256); fallback a SHA-1 (Signature).
    signature_b64 = headers.get("Signature-256")
    hash_algo: hashes.HashAlgorithm = hashes.SHA256()
    if not signature_b64:
        signature_b64 = headers.get("Signature")
        hash_algo = hashes.SHA1()
    if not signature_b64:
        raise AlexaVerificationError("Falta el header de firma")

    _validate_cert_url(cert_url)
    certs = _load_cert_chain(cert_url)
    leaf = _validate_cert_chain(certs)
    _verify_signature(leaf, signature_b64, body, hash_algo)
    _validate_timestamp(payload)
