"""
albert-proxy - Proxy de compatibilite Vibe CLI / OpenCode -> Albert API.

Corrige les incompatibilites entre les requetes OpenAI envoyees par
Mistral Vibe CLI (ou tout client OpenAI) et l'API Albert (OpenGateLLM).

Probleme principal :
    OpenGateLLM valide le champ `strict` des tool definitions avec un
    modele Pydantic qui n'accepte que des booleens. Le SDK OpenAI utilise
    par Vibe envoie `strict: null` par defaut, ce qui provoque une erreur
    422 Unprocessable Entity.

Solution :
    Le proxy intercepte chaque requete, force `strict: false` dans chaque
    tool definition, et supprime les champs non supportes avant de
    transmettre la requete a Albert.

Usage :
    ALBERT_API_KEY=xxx uvicorn proxy:app --port 4000

Debug :
    ALBERT_API_KEY=xxx PROXY_DEBUG=1 uvicorn proxy:app --port 4000
"""

import json
import os
import sys
from datetime import datetime
from typing import Any, Dict

import httpx
from fastapi import FastAPI, Request, Response

# ---------------------------------------------------------------------------
# Configuration (variables d'environnement)
# ---------------------------------------------------------------------------
ALBERT_BASE_URL = os.getenv("ALBERT_BASE_URL", "https://albert.api.etalab.gouv.fr/v1")
ALBERT_API_KEY = os.getenv("ALBERT_API_KEY")
TIMEOUT = int(os.getenv("PROXY_TIMEOUT", "300"))
DEBUG = os.getenv("PROXY_DEBUG", "0") == "1"

app = FastAPI(
    title="albert-proxy",
    description="Proxy de compatibilite pour Albert API (OpenGateLLM)",
)

# ---------------------------------------------------------------------------
# Champs OpenAI non supportes par OpenGateLLM
# ---------------------------------------------------------------------------
UNSUPPORTED_TOP_LEVEL = {
    "parallel_tool_calls",
    "stream_options",
    "service_tier",
    "store",
}


def log(label: str, data: str) -> None:
    """Log de debug (active avec PROXY_DEBUG=1)."""
    if not DEBUG:
        return
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {label}", file=sys.stderr)
    print(data[:3000], file=sys.stderr)
    print(file=sys.stderr)


def fix_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Corrige le payload pour le rendre compatible avec Albert/OpenGateLLM.

    - Force ``strict: false`` dans chaque tool definition
    - Supprime les champs top-level non supportes
    """
    for tool in body.get("tools", []):
        fn = tool.get("function", {})
        fn["strict"] = False

    for field in UNSUPPORTED_TOP_LEVEL:
        body.pop(field, None)

    return body


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(path: str, request: Request) -> Response:
    """Forward toutes les requetes vers Albert apres nettoyage."""
    if not ALBERT_API_KEY:
        return Response(
            content=json.dumps({"error": "ALBERT_API_KEY is not set"}),
            status_code=500,
            media_type="application/json",
        )

    raw_body = await request.body()
    content_to_send = raw_body

    if raw_body:
        try:
            body = json.loads(raw_body)
            log(f">>> AVANT fix /{path}", json.dumps(body, indent=2, ensure_ascii=False)[:2000])
            body = fix_payload(body)
            log(f">>> APRES fix /{path}", json.dumps(body, indent=2, ensure_ascii=False)[:2000])
            content_to_send = json.dumps(body).encode()
        except json.JSONDecodeError:
            pass

    headers = {
        "Authorization": f"Bearer {ALBERT_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.request(
                request.method,
                f"{ALBERT_BASE_URL}/{path}",
                content=content_to_send,
                headers=headers,
            )
    except httpx.TimeoutException:
        return Response(
            content=json.dumps({"error": f"Albert API timeout ({TIMEOUT}s)"}),
            status_code=504,
            media_type="application/json",
        )
    except httpx.RequestError as e:
        return Response(
            content=json.dumps({"error": f"Network error: {e}"}),
            status_code=502,
            media_type="application/json",
        )

    log(f"<<< Albert {resp.status_code}", resp.text[:2000])

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )
