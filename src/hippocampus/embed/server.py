"""Standalone BGE-M3 embed HTTP server.

POST /embed  {"query": str, "max_length": 512}  → {"dense": [float...]}
Auth: Bearer via BGE_EMBED_TOKEN env var.
"""
import os
import sys
import contextlib
import threading
import time

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from .norm import assert_normalized, assert_batch_normalized

EMBED_TOKEN = os.environ.get("BGE_EMBED_TOKEN", "")
MODEL = None
SERVER = None
ACTIVE_REQUESTS = 0
LAST_COMPLETED_AT = time.time()
_activity_lock = threading.Lock()
_bearer = HTTPBearer()


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_model()
    _start_idle_monitor()
    yield


app = FastAPI(docs_url=None, redoc_url=None, lifespan=lifespan)


def _auth(creds: HTTPAuthorizationCredentials = Security(_bearer)):
    if not EMBED_TOKEN:
        raise HTTPException(status_code=500, detail="BGE_EMBED_TOKEN not set")
    if creds.credentials != EMBED_TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")


def get_model():
    global MODEL
    if MODEL is None:
        try:
            import torch
            if torch.cuda.is_available():
                device = "cuda"
            elif torch.backends.mps.is_available():
                device = "mps"
            else:
                device = "cpu"
        except Exception:
            device = "cpu"
        print(f"[embed_server] loading BGE-M3 on {device}...", flush=True)
        with contextlib.redirect_stdout(sys.stderr):
            from FlagEmbedding import BGEM3FlagModel
            MODEL = BGEM3FlagModel("BAAI/bge-m3", use_fp16=True, device=device)
        print("[embed_server] model ready", flush=True)
    return MODEL


def _idle_seconds() -> int:
    raw = os.environ.get("BGE_ONDEMAND_IDLE_SECONDS", "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        print("[embed_server] invalid BGE_ONDEMAND_IDLE_SECONDS; idle exit disabled",
              file=sys.stderr, flush=True)
        return 0


def _start_idle_monitor() -> None:
    idle = _idle_seconds()
    if idle <= 0:
        return

    def monitor() -> None:
        while True:
            time.sleep(min(30, max(1, idle // 3)))
            with _activity_lock:
                active = ACTIVE_REQUESTS
                last = LAST_COMPLETED_AT
            if active:
                continue
            if time.time() - last < idle:
                continue
            print(f"[embed_server] idle for {idle}s; shutting down",
                  file=sys.stderr, flush=True)
            server = SERVER
            if server is not None:
                server.should_exit = True
            return

    threading.Thread(target=monitor, name="bge-idle-monitor", daemon=True).start()


class _RequestActivity:
    def __enter__(self):
        global ACTIVE_REQUESTS
        with _activity_lock:
            ACTIVE_REQUESTS += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        global ACTIVE_REQUESTS, LAST_COMPLETED_AT
        with _activity_lock:
            ACTIVE_REQUESTS -= 1
            LAST_COMPLETED_AT = time.time()
        return False


class EmbedRequest(BaseModel):
    query: str
    max_length: int = 512


class EmbedBatchRequest(BaseModel):
    texts: list[str]
    max_length: int = 512


@app.get("/health")
def health():
    # Unauthenticated monitoring IF (parity with the pre-package server).
    # Dub pre-flight (bge_health_ok) gates on ok + model_loaded.
    with _activity_lock:
        active = ACTIVE_REQUESTS
        last = LAST_COMPLETED_AT
    return {
        "ok": True,
        "model_loaded": MODEL is not None,
        "active_request_count": active,
        "last_completed_at": last,
        "idle_seconds": _idle_seconds(),
    }


@app.get("/ready")
def ready(creds: HTTPAuthorizationCredentials = Security(_bearer)):
    _auth(creds)
    with _activity_lock:
        active = ACTIVE_REQUESTS
        last = LAST_COMPLETED_AT
    return {
        "ok": True,
        "model_loaded": MODEL is not None,
        "active_request_count": active,
        "last_completed_at": last,
        "idle_seconds": _idle_seconds(),
    }


@app.post("/embed")
def embed(req: EmbedRequest, creds: HTTPAuthorizationCredentials = Security(_bearer)):
    _auth(creds)
    if not req.query.strip():
        raise HTTPException(400, "empty query")
    with _RequestActivity():
        model = get_model()
        out = model.encode(
            [req.query], batch_size=1, max_length=req.max_length,
            return_dense=True, return_sparse=False, return_colbert_vecs=False,
        )
    vec = out["dense_vecs"][0].tolist()
    assert_normalized(vec, where="embed_server./embed")
    return {"dense": vec}


@app.post("/embed_batch")
def embed_batch(req: EmbedBatchRequest, creds: HTTPAuthorizationCredentials = Security(_bearer)):
    _auth(creds)
    if not req.texts:
        raise HTTPException(400, "empty texts")
    with _RequestActivity():
        model = get_model()
        out = model.encode(
            req.texts, batch_size=min(32, len(req.texts)), max_length=req.max_length,
            return_dense=True, return_sparse=False, return_colbert_vecs=False,
        )
    vecs = [v.tolist() for v in out["dense_vecs"]]
    assert_batch_normalized(vecs, where="embed_server./embed_batch")
    return {"dense": vecs}


def main() -> None:
    import uvicorn
    global SERVER
    # 0.0.0.0 only inside the compose `bge` container (port published to
    # localhost by compose); host runs keep the loopback default.
    host = os.environ.get("BGE_EMBED_HOST", "127.0.0.1")
    port = int(os.environ.get("BGE_EMBED_PORT", 8082))
    config = uvicorn.Config(app, host=host, port=port)
    SERVER = uvicorn.Server(config)
    SERVER.run()


if __name__ == "__main__":
    main()
