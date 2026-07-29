# OrioSearch — Setup Guide (verified, reproducible)

Tested on **Windows 11 + Docker Desktop** on `2026-07-28`.
Repo: https://github.com/vkfolio/orio-search

This guide gets the full stack (API + SearXNG + Redis) running with AI answers
powered by your local **Ollama**, with every endpoint verified.

---

## 0. Prerequisites

- **Docker Desktop** running (daemon up). Start it and wait until `docker info` succeeds.
- **Ollama** running on the host with at least one chat model installed
  (e.g. `qwen2.5`, `llama3.2`). Check with:
  ```bash
  curl http://localhost:11434/api/tags
  ```
  If empty, pull a model: `ollama pull qwen2.5`
- Git: `git --version`

---

## 1. Clone

```bash
cd C:/Users/HP/test_open_source_tools
git clone https://github.com/vkfolio/orio-search
cd orio-search
```

---

## 2. Apply the two repo fixes (REQUIRED on Windows)

The repo has two bugs that prevent `docker compose up` from working out of the
box. Apply both before building.

### Fix A — CRLF line endings in `entrypoint.sh`
The script has Windows line endings, so the Linux container can't exec it
(`exec /entrypoint.sh: no such file or directory`).

```bash
sed -i 's/\r$//' entrypoint.sh
# verify it's now LF:
file entrypoint.sh   # -> "POSIX shell script, ASCII text executable" (no "CRLF")
```

### Fix B — `entrypoint.sh` writes to `/data` which is never created
The Dockerfile never `mkdir`s `/data`, so the `cp /app/config.yaml /data/config.yaml`
crashes the container on boot. Add the `mkdir` to `entrypoint.sh`.

Open `entrypoint.sh` and make the `if` block look like this:

```sh
if [ ! -f "$CONFIG" ]; then
    mkdir -p /data
    echo "==> Generating default config.yaml in /data"
    cp /app/config.yaml "$CONFIG"
fi
```

(One-line patch via sed, run in Git Bash with path conversion disabled:)
```bash
export MSYS_NO_PATHCONV=1
# only if re-cloning a fresh copy — insert mkdir before the cp line:
python - <<'EOF'
p='entrypoint.sh'
s=open(p).read()
s=s.replace(
'    echo "==> Generating default config.yaml in /data"\n    cp /app/config.yaml "$CONFIG"',
'    mkdir -p /data\n    echo "==> Generating default config.yaml in /data"\n    cp /app/config.yaml "$CONFIG"')
open(p,'w').write(s)
print("entrypoint patched")
EOF
```

---

## 3. Remap host ports if 8000 / 8080 are in use

`docker-compose.yml` maps API→8000 and SearXNG→8080 on the host. If those are
taken (check with `netstat -ano | findstr ":8000 "`), remap the **host** side only
(internal docker networking is unaffected):

In `docker-compose.yml`:
```yaml
  searxng:
    ports:
      - "8081:8080"     # was 8080:8080
  ...
  api:
    ports:
      - "8001:8000"     # was 8000:8000
```

This guide assumes **API on `localhost:8001`**. If you keep 8000, drop the `1`.

---

## 4. Point the LLM at a model you actually have

`config.yaml` ships with `model: "qwen3.5:9b"` and `base_url:
"http://ollama:11434/v1"`. Two changes needed:

1. The API container must reach the host's Ollama. Use `host.docker.internal`:
   ```yaml
   llm:
     enabled: true
     provider: "ollama"
     base_url: "http://host.docker.internal:11434/v1"   # must end with /v1
     api_key: "ollama"
     model: "qwen2.5:latest"        # <-- a model you pulled (check ollama list)
   ```
2. Pick a model from `ollama list` / `curl localhost:11434/api/tags`.

> Note: `config.yaml` is mounted read-only into the container at `/app/config.yaml`,
> but `entrypoint.sh` copies it to `/data/config.yaml` on first boot and points
> `ORIO_SEARCH_CONFIG` there. So `/data/config.yaml` is the *effective* config.
> If you change `config.yaml` after first boot, either:
> - delete `/data/config.yaml` inside the container and restart, or
> - copy it in: `docker exec orio-search-api sh -c "cp /app/config.yaml /data/config.yaml"`
>   (use `export MSYS_NO_PATHCONV=1` in Git Bash so `/data/...` isn't mangled).

### Other useful config toggles (all in `config.yaml`)
```yaml
rerank:
  enabled: true          # FlashRank semantic reranking (~4MB ONNX, CPU)
rate_limit:
  enabled: true          # Redis-backed per-IP/key limits
auth:
  enabled: true          # require Bearer token
  api_keys: ["your-secret"]
```

---

## 5. Build & start

```bash
docker compose up --build -d
```

Watch it come up:
```bash
docker compose ps
# All three should be Up. SearXNG + Redis show (healthy) once ready.
```

If `orio-search-api` shows `Restarting`, check logs:
```bash
docker logs orio-search-api
```
Common causes: CRLF not fixed (Fix A), `/data` not created (Fix B), or Ollama
unreachable (it'll still boot — LLM degrades gracefully to `answer: null`).

---

## 6. Verify every endpoint

All commands below assume API on **port 8001**. Swap to 8000 if you didn't remap.

### Health
```bash
curl http://localhost:8001/health
# {"status":"ok","service":"orio-search"}
```

### Tool schema (OpenAI function-calling defs)
```bash
curl http://localhost:8001/tool-schema
```

### OpenAPI / endpoints
```bash
curl http://localhost:8001/openapi.json | python -m json.tool
# paths: /search, /extract, /search/stream, /health, /tool-schema  (title: OrioSearch v2.0.0)
```

### Basic search
```bash
curl -X POST http://localhost:8001/search \
  -H "Content-Type: application/json" \
  -d '{"query": "what is docker", "max_results": 5}'
```
Expect: `query, results[{title,url,content,score}], images, response_time`.

### AI answer (uses your Ollama)
```bash
curl -X POST http://localhost:8001/search \
  -H "Content-Type: application/json" \
  -d '{"query": "what is docker", "include_answer": true, "max_results": 5}'
```
Expect a non-null `answer` with `[1]`/`[2]` citations. First call is slow
(model loads + generates; ~30-80s on CPU). Subsequent identical queries are
cached (answer cache TTL in config).

### Advanced depth (extracts full page content)
```bash
curl -X POST http://localhost:8001/search \
  -H "Content-Type: application/json" \
  -d '{"query": "latest AI news", "topic": "news", "search_depth": "advanced", "max_results": 2}'
```
Expect `results[].raw_content` populated (up to `max_content_length` 50000 chars).

### Image search
```bash
curl -X POST http://localhost:8001/search \
  -H "Content-Type: application/json" \
  -d '{"query": "cute cats", "include_images": true, "max_results": 3}'
```
Expect `images[]` populated alongside web results.

### SSE streaming
```bash
curl -N -X POST http://localhost:8001/search/stream \
  -H "Content-Type: application/json" \
  -d '{"query": "latest AI news", "topic": "news", "max_results": 3}'
```
Expect `event: result` lines, then `event: done` with `response_time`.

### Extract endpoint
```bash
curl -X POST http://localhost:8001/extract \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://example.com"], "format": "markdown"}'
```
Expect `results[].raw_content` clean markdown + `failed_results` for bad URLs.

### News topic
```bash
curl -X POST http://localhost:8001/search \
  -H "Content-Type: application/json" \
  -d '{"query": "latest AI news", "topic": "news", "max_results": 5}'
```

---

## 7. Run the test suite (claim: 110 tests)

Tests are **not** in the Docker image, so run them locally.

```bash
cd C:/Users/HP/test_open_source_tools/orio-search
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt pytest pytest-asyncio
.venv/Scripts/python.exe -m pytest tests/ -q
# Expected: 110 passed
```

---

## 8. Known limitation (the "Tavily alternative" caveat)

Result reliability depends entirely on **SearXNG scraping upstream engines**.
Under load or from a cloud/datacenter IP, upstream engines (Google, Bing,
DuckDuckGo) start **CAPTCHA-ing / rate-limiting SearXNG**, which then returns
**200 OK with zero results**. The advertised **DuckDuckGo fallback** triggers
correctly (logs: `falling_back: true`), but DuckDuckGo also rate-limits, so you
can still get **HTTP 503** with empty results.

Check SearXNG health:
```bash
docker logs orio-search-searxng 2>&1 | grep -iE 'captcha|rate|error' | tail
```

Symptom: searches that worked suddenly return `"results": []`.
Mitigations: rotate IP / use a residential proxy (see `proxy:` in config), or
restart SearXNG (`docker restart orio-search-searxng`) and wait a few minutes.

This is inherent to SearXNG-based stacks and is **why Tavily costs money**
(managed search-index access). OrioSearch is a legit *free, self-hosted,
Tavily-shaped* API, but it does not match Tavily's result reliability under
sustained use.

---

## 9. Day-to-day commands

```bash
# start
docker compose up -d

# stop
docker compose down

# rebuild after editing code/Dockerfile
docker compose up --build -d

# tail logs
docker logs -f orio-search-api
docker logs -f orio-search-searxng

# apply a config.yaml change after first boot
export MSYS_NO_PATHCONV=1
docker exec orio-search-api sh -c "cp /app/config.yaml /data/config.yaml"
docker compose restart api
```

---

## 10. Quick troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `exec /entrypoint.sh: no such file` | CRLF line endings | `sed -i 's/\r$//' entrypoint.sh`, rebuild |
| `cp: cannot create '/data/config.yaml'` | `/data` missing | Add `mkdir -p /data` to entrypoint.sh (Fix B) |
| `Bind for 0.0.0.0:8000 failed` | port in use | Remap host port in docker-compose.yml |
| API restarts / LLM errors | Ollama unreachable or wrong model | verify `host.docker.internal:11434` + `ollama list` |
| `results: []` (was working) | upstream engine rate-limited SearXNG | restart SearXNG, wait, or use proxy |
| `answer: null` despite `include_answer:true` | LLM disabled/unreachable | check `llm.enabled` + base_url + model name |
| Config change not picked up | `/data/config.yaml` cached from first boot | copy host config into `/data/` then restart (§9) |

---

## TL;DR — minimal fast path

```bash
git clone https://github.com/vkfolio/orio-search && cd orio-search
sed -i 's/\r$//' entrypoint.sh
# add "mkdir -p /data" before the cp line in entrypoint.sh
# set config.yaml llm.model to a model from `ollama list`,
# and llm.base_url to http://host.docker.internal:11434/v1
docker compose up --build -d
sleep 10
curl http://localhost:8000/health            # {"status":"ok",...}
curl -X POST http://localhost:8000/search -H "Content-Type: application/json" \
  -d '{"query":"what is docker","include_answer":true}'
```