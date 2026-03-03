# albert-proxy

Proxy de compatibilite pour connecter des outils de vibe coding (Mistral Vibe CLI, OpenCode, etc.) a [Albert API](https://albert.api.etalab.gouv.fr), l'API IA interministerielle operee par la DINUM.

## Pourquoi ce proxy ?

Albert API est basee sur [OpenGateLLM](https://github.com/etalab-ia/OpenGateLLM), qui expose une API compatible OpenAI. En pratique, certains clients (Mistral Vibe CLI, OpenCode) envoient des champs non supportes par OpenGateLLM, notamment :

- **strict: null** dans les tool definitions (OpenGateLLM attend un booleen, pas null)
- **parallel_tool_calls**, **stream_options**, etc. non reconnus

Le proxy intercepte les requetes, corrige ces incompatibilites, et les transmet a Albert.

## Installation

```bash
git clone https://github.com/benoitvx/albert-proxy.git
cd albert-proxy
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Configuration

```bash
cp .env.example .env
# Editer .env avec votre cle Albert API
```

Ou exporter directement :

```bash
export ALBERT_API_KEY="votre-cle-albert"
```

### Variables d'environnement

| Variable | Requis | Default | Description |
|---|---|---|---|
| ALBERT_API_KEY | oui | - | Cle API Albert |
| ALBERT_BASE_URL | non | https://albert.api.etalab.gouv.fr/v1 | URL de base Albert |
| PROXY_TIMEOUT | non | 300 | Timeout en secondes |
| PROXY_DEBUG | non | 0 | Mode debug (1 pour activer) |

## Usage

### Lancer le proxy

```bash
source venv/bin/activate
ALBERT_API_KEY=$ALBERT_API_KEY uvicorn proxy:app --port 4000
```

Mode debug :

```bash
ALBERT_API_KEY=$ALBERT_API_KEY PROXY_DEBUG=1 uvicorn proxy:app --port 4000
```

### Configurer Mistral Vibe CLI

Dans ~/.vibe/config.toml :

```toml
[[providers]]
name = "albert"
api_base = "http://localhost:4000"
api_key_env_var = "ALBERT_API_KEY"
api_style = "openai"
backend = "generic"

[[models]]
name = "openai/gpt-oss-120b"
provider = "albert"
alias = "gpt-oss"
temperature = 0.2
input_price = 0.0
output_price = 0.0

[[models]]
name = "mistral-medium-2508"
provider = "albert"
alias = "mistral-medium"
temperature = 0.2
input_price = 0.0
output_price = 0.0

[[models]]
name = "Qwen/Qwen3-Coder-30B-A3B-Instruct"
provider = "albert"
alias = "qwen3-coder"
temperature = 0.2
input_price = 0.0
output_price = 0.0
```

Puis selectionner un modele :

```toml
active_model = "gpt-oss"
```

### Configurer OpenCode

Dans ~/.config/opencode/opencode.json :

```json
{
  "provider": {
    "albert": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Albert API",
      "options": {
        "baseURL": "http://localhost:4000",
        "apiKey": "{env:ALBERT_API_KEY}"
      },
      "models": {
        "openai/gpt-oss-120b": {
          "name": "GPT OSS 120B",
          "limit": { "context": 128000, "output": 16384 }
        }
      }
    }
  },
  "model": "albert/openai/gpt-oss-120b"
}
```

> Note : OpenCode peut aussi se connecter directement a Albert sans proxy si vous ne rencontrez pas d erreurs 422.

## Modeles disponibles

```bash
curl -s -H "Authorization: Bearer $ALBERT_API_KEY" \
  https://albert.api.etalab.gouv.fr/v1/models \
  | jq '.data[] | select(.type == "text-generation") | .id'
```

## Architecture

```
Vibe CLI / OpenCode
        |
        v
  albert-proxy (:4000)
   - fix strict: null -> false
   - strip unsupported fields
        |
        v
  Albert API (OpenGateLLM)
   albert.api.etalab.gouv.fr
```

## Ce que corrige le proxy

| Probleme | Source | Fix |
|---|---|---|
| strict: null dans tools | SDK OpenAI (via Vibe) | Force strict: false |
| parallel_tool_calls | SDK OpenAI | Supprime le champ |
| stream_options | SDK OpenAI | Supprime le champ |
| service_tier | SDK OpenAI | Supprime le champ |

## Contribuer

Les PR sont bienvenues. Si vous rencontrez de nouvelles incompatibilites, ouvrez une issue avec les logs du mode debug (PROXY_DEBUG=1).

## Contexte

Developpe dans le cadre de la mission EIG (Entrepreneurs d Interet General) a la DINUM, departement IAE (Intelligence Artificielle dans l Etat), pour faciliter l utilisation des outils de vibe coding avec l infrastructure IA souveraine.

## Licence

MIT - voir [LICENSE](LICENSE)
