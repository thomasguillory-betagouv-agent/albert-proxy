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

import hashlib
import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

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


@app.get("/health")
def health():
    """Health check endpoint for Docker/Kubernetes probes."""
    return {"status": "ok"}

# ---------------------------------------------------------------------------
# Champs OpenAI non supportes par OpenGateLLM
# ---------------------------------------------------------------------------
UNSUPPORTED_TOP_LEVEL = {
    "parallel_tool_calls",
    "stream_options",
    "service_tier",
    "store",
}

# ---------------------------------------------------------------------------
# Mapping de modeles : noms courants -> IDs Albert
# ---------------------------------------------------------------------------
MODEL_ALIASES = {
    # Qwen
    "Qwen/Qwen2.5-Coder-32B-Instruct-AWQ": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
    "Qwen/Qwen2.5-Coder-32B-Instruct": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
    "qwen-coder": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
    # Mistral
    "mistral-small": "mistralai/Mistral-Small-3.2-24B-Instruct-2506",
    "mistral-medium": "mistral-medium-2508",
    # Aliases pratiques
    "gpt-4o": "openai/gpt-oss-120b",
    "gpt-4o-mini": "mistralai/Mistral-Small-3.2-24B-Instruct-2506",
    "gpt-4": "openai/gpt-oss-120b",
    "gpt-3.5-turbo": "mistralai/Ministral-3-8B-Instruct-2512",
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
    # Remapping du modele si necessaire
    model = body.get("model", "")
    if model in MODEL_ALIASES:
        original = model
        body["model"] = MODEL_ALIASES[model]
        log("MODEL REMAP", f"{original} -> {body['model']}")

    # Qwen3 : desactiver le mode thinking (genere du reasoning_content vide)
    if body.get("model", "").startswith("Qwen/Qwen3"):
        body["chat_template_kwargs"] = {"enable_thinking": False}
        # Limiter max_tokens si absent (evite timeout sur gros prompts)
        body.setdefault("max_tokens", 16384)

    # Corriger les messages pour compatibilite Mistral/Albert
    for msg in body.get("messages", []):
        if msg.get("role") == "assistant":
            # Supprimer tool_calls vide (Mistral refuse [])
            if "tool_calls" in msg and not msg["tool_calls"]:
                del msg["tool_calls"]
            # S'assurer que content ou tool_calls est present
            has_content = msg.get("content") not in (None,)
            has_tools = bool(msg.get("tool_calls"))
            if not has_content and not has_tools:
                msg["content"] = " "

    # Reecrire les tool_call_id trop longs ou invalides
    # Mistral exige : [a-zA-Z0-9]{9}
    body = fix_tool_call_ids(body)

    for tool in body.get("tools", []):
        fn = tool.get("function", {})
        fn["strict"] = False

    for field in UNSUPPORTED_TOP_LEVEL:
        body.pop(field, None)

    return body


def _short_id(original: str) -> str:
    """Genere un ID de 9 caracteres alphanumeriques a partir d'un ID original."""
    if re.fullmatch(r"[a-zA-Z0-9]{9}", original):
        return original
    return hashlib.md5(original.encode()).hexdigest()[:9]


def fix_tool_call_ids(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Reecrit les tool_call_id pour respecter le format Mistral :
    exactement 9 caracteres alphanumeriques [a-zA-Z0-9].

    Maintient un mapping pour garder la coherence entre les tool_calls
    d'un message assistant et les tool_call_id des messages tool.
    """
    id_map: Dict[str, str] = {}

    for msg in body.get("messages", []):
        # Messages assistant avec tool_calls
        for tc in msg.get("tool_calls", []):
            old_id = tc.get("id", "")
            if old_id and not re.fullmatch(r"[a-zA-Z0-9]{9}", old_id):
                new_id = _short_id(old_id)
                id_map[old_id] = new_id
                tc["id"] = new_id

        # Messages tool avec tool_call_id
        if msg.get("role") == "tool" and "tool_call_id" in msg:
            old_id = msg["tool_call_id"]
            if old_id in id_map:
                msg["tool_call_id"] = id_map[old_id]
            elif not re.fullmatch(r"[a-zA-Z0-9]{9}", old_id):
                new_id = _short_id(old_id)
                id_map[old_id] = new_id
                msg["tool_call_id"] = new_id

    if id_map and DEBUG:
        log("TOOL_CALL_ID REWRITE", json.dumps(id_map, indent=2))

    return body


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(path: str, request: Request):
    """Forward toutes les requetes vers Albert apres nettoyage."""
    if not ALBERT_API_KEY:
        return Response(
            content=json.dumps({"error": "ALBERT_API_KEY is not set"}),
            status_code=500,
            media_type="application/json",
        )

    raw_body = await request.body()
    content_to_send = raw_body
    is_stream = False

    if raw_body:
        try:
            body = json.loads(raw_body)
            log(f">>> AVANT fix /{path}", json.dumps(body, indent=2, ensure_ascii=False)[:2000])
            is_stream = body.get("stream", False)
            body = fix_payload(body)
            log(f">>> APRES fix /{path}", json.dumps(body, indent=2, ensure_ascii=False)[:2000])
            content_to_send = json.dumps(body).encode()
        except json.JSONDecodeError:
            pass

    headers = {
        "Authorization": f"Bearer {ALBERT_API_KEY}",
        "Content-Type": "application/json",
    }

    if is_stream:
        return await _proxy_stream(request.method, path, content_to_send, headers)
    else:
        return await _proxy_buffered(request.method, path, content_to_send, headers)


async def _proxy_buffered(method: str, path: str, content: bytes, headers: dict) -> Response:
    """Requete classique : attend la reponse complete et la renvoie."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.request(
                method,
                f"{ALBERT_BASE_URL}/{path}",
                content=content,
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


async def _proxy_stream(method: str, path: str, content: bytes, headers: dict):
    """Requete streaming : forward les chunks SSE au fur et a mesure."""
    client = httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT, connect=10.0))

    try:
        req = client.build_request(
            method,
            f"{ALBERT_BASE_URL}/{path}",
            content=content,
            headers=headers,
        )
        resp = await client.send(req, stream=True)
    except httpx.TimeoutException:
        await client.aclose()
        return Response(
            content=json.dumps({"error": f"Albert API timeout ({TIMEOUT}s)"}),
            status_code=504,
            media_type="application/json",
        )
    except httpx.RequestError as e:
        await client.aclose()
        return Response(
            content=json.dumps({"error": f"Network error: {e}"}),
            status_code=502,
            media_type="application/json",
        )

    if resp.status_code != 200:
        body = await resp.aread()
        await resp.aclose()
        await client.aclose()
        log(f"<<< Albert {resp.status_code} (erreur)", body.decode(errors="replace")[:2000])
        return Response(
            content=body,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    async def stream_generator():
        chunk_count = 0
        last_chunk = b""
        try:
            async for chunk in resp.aiter_raw():
                chunk_count += 1
                if DEBUG and chunk_count == 1:
                    log("<<< Albert 200 (stream debut)", chunk.decode(errors="replace")[:2000])
                last_chunk = chunk
                yield chunk
        finally:
            if DEBUG:
                log("<<< stream termine", f"{chunk_count} chunks envoyes")
                log("<<< dernier chunk", last_chunk.decode(errors="replace")[:2000])
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_generator(),
        status_code=resp.status_code,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
