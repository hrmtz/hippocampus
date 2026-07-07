"""Standalone BGE-M3 embed HTTP server.

POST /embed  {"query": str, "max_length": 512}  → {"dense": [float...]}
Auth: Bearer via BGE_EMBED_TOKEN env var.
"""
import os
import sys
import contextlib

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from .norm import assert_normalized, assert_batch_normalized

EMBED_TOKEN = os.environ.get("BGE_EMBED_TOKEN", "")
MODEL = None
_bearer = HTTPBearer()


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_model()
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
    return {"ok": True, "model_loaded": MODEL is not None}


@app.post("/embed")
def embed(req: EmbedRequest, creds: HTTPAuthorizationCredentials = Security(_bearer)):
    _auth(creds)
    if not req.query.strip():
        raise HTTPException(400, "empty query")
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
    # 0.0.0.0 only inside the compose `bge` container (port published to
    # localhost by compose); host runs keep the loopback default.
    host = os.environ.get("BGE_EMBED_HOST", "127.0.0.1")
    port = int(os.environ.get("BGE_EMBED_PORT", 8082))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
