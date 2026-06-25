# Alexa → Gemini Flash (fallback inteligente)

Alexa Custom Skill para Echo Dot que, cuando le preguntás algo que Alexa no
resuelve, reenvía la pregunta a **Gemini 2.5 Flash** (Google AI Studio, free tier)
y lee la respuesta en voz alta.

- **Backend:** FastAPI (Python), endpoint HTTP propio (no Lambda).
- **IA:** Gemini 2.5 Flash vía el SDK unificado `google-genai`.
- **Despliegue:** home server CasaOS expuesto por Cloudflare Tunnel (HTTPS válido).
- **Conversación:** multi-turno (mantiene contexto en la sesión de Alexa).

> **Nota sobre el SDK:** se usa `google-genai`, el SDK oficial actual. El viejo
> `google-generativeai` quedó sin soporte el 30/11/2025 y no se usa.

---

## ⚠️ Cómo se captura la pregunta (importante)

`AMAZON.FallbackIntent`, por sí solo, **no entrega el texto** de lo que dijo el
usuario: solo avisa que Alexa no encontró un intent. Por eso, para reenviar la
pregunta real a Gemini, el modelo de interacción incluye un intent de captura de
texto libre, **`AskGeminiIntent`**, con un slot `AMAZON.SearchQuery`.

- **`AskGeminiIntent`** → camino feliz: trae la pregunta en el slot `query` y la
  manda a Gemini.
- **`AMAZON.FallbackIntent`** → red de seguridad: si Alexa no matchea nada, la
  skill pide reformular.

El modelo de interacción listo para pegar está en
[`alexa/interaction_model_es.json`](alexa/interaction_model_es.json).

---

## 1. Obtener la API key de Gemini (Google AI Studio)

1. Entrá a <https://aistudio.google.com/apikey> con tu cuenta de Google.
2. Click en **Create API key** → **Create API key in new project** (free tier, sin
   billing).
3. Copiá la clave y guardala. La vas a poner en `GEMINI_API_KEY` (archivo `.env`).

El free tier de `gemini-2.5-flash` alcanza de sobra para uso doméstico. No hace
falta activar facturación.

---

## 2. Configurar el proyecto

```bash
git clone <tu-repo> alexa-gemini
cd alexa-gemini

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Editá .env y completá GEMINI_API_KEY (y ALEXA_SKILL_ID cuando crees la skill)
```

Variables de `.env`:

| Variable                  | Descripción                                                        |
|---------------------------|--------------------------------------------------------------------|
| `GEMINI_API_KEY`          | API key de Google AI Studio. **Obligatoria.**                      |
| `GEMINI_MODEL`            | `gemini-2.5-flash` (default).                                      |
| `GEMINI_TIMEOUT_SECONDS`  | Timeout antes de devolver el fallback de voz (default `8`).        |
| `ALEXA_SKILL_ID`          | `applicationId` de tu skill (`amzn1.ask.skill.…`).                 |
| `VERIFY_ALEXA_SIGNATURE`  | `true` en producción. `false` solo para probar local con curl.     |
| `MAX_HISTORY_TURNS`       | Turnos de conversación recordados (default `5`).                   |

---

## 3. Correr el servidor

### Local (desarrollo, sin firma de Alexa)

```bash
VERIFY_ALEXA_SIGNATURE=false uvicorn app.main:app --reload --port 8095
```

Probá los endpoints con los JSON de ejemplo de `tests/`:

```bash
# Bienvenida
curl -s -X POST http://localhost:8095/alexa \
  -H "Content-Type: application/json" \
  -d @tests/sample_launch.json | python3 -m json.tool

# Pregunta a Gemini (requiere GEMINI_API_KEY válida en .env)
curl -s -X POST http://localhost:8095/alexa \
  -H "Content-Type: application/json" \
  -d @tests/sample_fallback.json | python3 -m json.tool

# Stop
curl -s -X POST http://localhost:8095/alexa \
  -H "Content-Type: application/json" \
  -d @tests/sample_stop.json | python3 -m json.tool

# Healthcheck
curl -s http://localhost:8095/health
```

### Producción (home server CasaOS)

Path sugerido: `/home/genoud/alexa-gemini`. Con `VERIFY_ALEXA_SIGNATURE=true`:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8095
```

Para que quede corriendo como servicio (ejemplo systemd, usuario `genoud`):

```ini
# /etc/systemd/system/alexa-gemini.service
[Unit]
Description=Alexa Gemini Skill
After=network.target

[Service]
User=genoud
WorkingDirectory=/home/genoud/alexa-gemini
EnvironmentFile=/home/genoud/alexa-gemini/.env
ExecStart=/home/genoud/alexa-gemini/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8095
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now alexa-gemini
```

> **Puerto:** la app escucha en **8095**. Cambialo con `--port` si lo necesitás
> (y actualizá Cloudflare Tunnel en consecuencia).

---

## 4. Cloudflare Tunnel

Alexa exige un endpoint **HTTPS con certificado válido**. El túnel de Cloudflare ya
lo cubre. En tu config del túnel (`config.yml` de `cloudflared`) apuntá un hostname
público al servicio local en el puerto 8095:

```yaml
tunnel: <TU_TUNNEL_ID>
credentials-file: /home/genoud/.cloudflared/<TU_TUNNEL_ID>.json

ingress:
  - hostname: alexa.genoud-nube.com.ar
    service: http://localhost:8095
  - service: http_status:404
```

```bash
cloudflared tunnel run
```

La URL del endpoint para Alexa será: `https://alexa.genoud-nube.com.ar/alexa`

---

## 5. Crear la Custom Skill en developer.amazon.com

1. Entrá a <https://developer.amazon.com/alexa/console/ask> → **Create Skill**.
2. Nombre a elección, idioma **Spanish (ES)** (o el de tu Echo).
3. Modelo: **Custom**. Hosting: **Provision your own** (endpoint propio, no Lambda).
4. **Build → Invocation:** poné la frase de invocación (ej. *asistente inteligente*).
5. **Build → Interaction Model → JSON Editor:** pegá el contenido de
   [`alexa/interaction_model_es.json`](alexa/interaction_model_es.json) y
   **Save Model** → **Build Model**.
6. **Build → Endpoint:**
   - Tipo: **HTTPS**.
   - Default Region: `https://alexa.genoud-nube.com.ar/alexa`.
   - Certificado: **"My development endpoint is a sub-domain of a domain that has a
     wildcard certificate from a certificate authority"** (Cloudflare usa cert de CA
     válida, así que esta opción es la correcta).
   - Guardá.
7. Copiá tu **Skill ID** (`amzn1.ask.skill.…`) desde *Endpoint* o *View Skill ID* y
   ponelo en `ALEXA_SKILL_ID` del `.env`. Reiniciá el servidor.
8. **Test:** pestaña *Test* → activá *Development* → probá escribiendo o hablando.
   Después probá en el Echo Dot diciendo: *"Alexa, abrí asistente inteligente"* y
   luego tu pregunta.

> Si Amazon rechaza requests, revisá que `VERIFY_ALEXA_SIGNATURE=true`, que el
> certificado del túnel sea válido y que la hora del servidor esté sincronizada
> (NTP) — el timestamp se valida con ±150 s.

---

## Verificación de seguridad incluida

Para endpoints HTTP propios, Amazon obliga a validar cada request. La skill lo hace
en [`app/alexa_verifier.py`](app/alexa_verifier.py):

- Valida la URL del header `SignatureCertChainUrl` (https, `s3.amazonaws.com`,
  puerto 443, path `/echo.api/`).
- Descarga y cachea la cadena de certificados; valida vigencia, el SAN
  `echo-api.amazon.com` y los enlaces de la cadena.
- Verifica la firma del **body crudo** (`Signature-256` SHA-256, con fallback a
  `Signature` SHA-1).
- Valida el `timestamp` (anti-replay, ±150 s) y el `applicationId` de la skill.

---

## Estructura

```
app/
  config.py          # settings desde .env (pydantic-settings)
  main.py            # FastAPI: POST /alexa, GET /health
  alexa_verifier.py  # verificación de firma/cert/timestamp/applicationId
  gemini_client.py   # cliente google-genai + limpieza de markdown para voz
  handlers.py        # routing de intents → JSON de Alexa
alexa/
  interaction_model_es.json   # modelo de interacción para pegar en la consola
tests/
  sample_*.json      # payloads de ejemplo para probar con curl
```
