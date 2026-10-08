# SupportFlow Workbench frontend

This directory contains the separate TanStack Start frontend prepared for
Cloudflare Workers. The existing FastAPI application remains the authoritative
backend for authentication, database access, local Dense retrieval, the local
BGE reranker, refunds, and DeepSeek calls.

## Local development

```powershell
npm install
npm run dev
```

The Worker frontend proxies `/api/*` to the backend origin configured by the
Cloudflare `BACKEND_ORIGIN` binding. An empty binding is intentional: it makes
an incomplete deployment fail with a clear `503` instead of silently pointing
at a developer's machine.

## Build and deploy

The build is local and does not contain the BGE model, Dense model cache, or
DeepSeek API key:

```powershell
npm run build
npx wrangler login
npx wrangler deploy --var BACKEND_ORIGIN:https://<reachable-backend-host>
```

The login command must be completed by the Cloudflare account owner. Do not
put the DeepSeek key in this frontend or in `wrangler.jsonc`.

`BACKEND_ORIGIN` must be a URL that Cloudflare Workers can reach. It cannot be
`http://127.0.0.1:8001`, because that address means the Worker itself, not the
developer's computer. For a short demo, a working Cloudflare Quick Tunnel can
expose the local FastAPI process; for reliable company use, run FastAPI and
the local models on an always-on server and use its HTTPS URL.

## Architecture

```text
Browser
  -> Cloudflare Worker (TanStack Start UI + /api proxy)
  -> public BACKEND_ORIGIN
  -> FastAPI + SQLite + local Dense/BGE + DeepSeek provider
```

The old FastAPI-served UI is intentionally retained as a local fallback while
the Worker frontend is being deployed and verified.
