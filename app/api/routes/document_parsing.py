"""Endpoint pengaturan parsing dokumen + MinerU.

Kontrak endpoint mengikuti halaman:
- ``app/(utility)/settings/document-parsing/page.tsx``
- ``components/settings/MinerUEngineSettings.tsx``
"""

import shutil
import sys

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.services.background_job import (
    cancel_job,
    env_with,
    job_status,
    pip_install_cmd,
    start_job,
)
from app.services.document_parsing import (
    ENGINE_META,
    INSTALL_PACKAGES,
    build_payload,
    get_or_create,
    is_installed,
    merge_options,
    mineru_settings_with_token,
    save_mineru_settings,
    set_active_engine,
)

document_parsing_router = APIRouter(prefix="/settings/document-parsing", tags=["settings"])
mineru_router = APIRouter(prefix="/settings/mineru", tags=["settings"])


# ── Document Parsing ────────────────────────────────────────────────────────

class DocumentParsingUpdate(BaseModel):
    engine: str | None = None
    engines: dict[str, dict] | None = None


@document_parsing_router.get("")
async def get_document_parsing(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await build_payload(db, current_user)


@document_parsing_router.put("")
async def put_document_parsing(
    payload: DocumentParsingUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await get_or_create(db, current_user.id)
    if payload.engine is not None:
        if payload.engine not in ENGINE_META:
            raise HTTPException(status_code=400, detail=f"Engine tidak dikenal: {payload.engine}")
        await set_active_engine(db, current_user, payload.engine)
        row = await get_or_create(db, current_user.id)
    if payload.engines:
        merge_options(row, payload.engines)
        await db.commit()
        await db.refresh(row)
    return await build_payload(db, current_user)


class EngineRequest(BaseModel):
    engine: str


@document_parsing_router.post("/install")
async def install_engine(
    payload: EngineRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pasang paket engine via pip di venv server (satu job global)."""
    if payload.engine not in INSTALL_PACKAGES:
        raise HTTPException(status_code=400, detail=f"Engine tidak dapat dipasang: {payload.engine}")
    if is_installed(payload.engine):
        return {"ok": True, "message": "Paket sudah terpasang."}
    package = INSTALL_PACKAGES[payload.engine]
    ok, err = start_job(
        "install",
        pip_install_cmd(package),
        f"{ENGINE_META[payload.engine]['name']} berhasil dipasang.",
    )
    if not ok:
        raise HTTPException(status_code=409, detail=err)
    return {"ok": True, "message": "Instalasi dimulai."}


@document_parsing_router.get("/job/status")
async def get_document_parsing_job_status(cursor: int = 0):
    return job_status(cursor)


@document_parsing_router.post("/job/cancel")
async def cancel_document_parsing_job():
    cancel_job()
    return {"ok": True}


@document_parsing_router.post("/models/download")
async def download_document_parsing_models(
    payload: EngineRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Unduh model berat untuk engine yang butuh model lokal (mis. Docling)."""
    if payload.engine == "docling":
        cli = shutil.which("docling-tools")
        if not cli:
            raise HTTPException(
                status_code=409,
                detail="Perintah docling-tools tidak ditemukan di PATH. Pasang Docling dulu.",
            )
        ok, err = start_job("models", [cli, "models", "download"], "Model Docling berhasil diunduh.")
    else:
        raise HTTPException(status_code=400, detail=f"Unduhan model belum didukung untuk: {payload.engine}")
    if not ok:
        raise HTTPException(status_code=409, detail=err)
    return {"ok": True, "message": "Unduhan model dimulai."}


# ── MinerU ──────────────────────────────────────────────────────────────────

class MinerUUpdate(BaseModel):
    mode: str = "local"
    api_base_url: str = "https://mineru.net"
    local_cli_path: str = ""
    model_download_source: str = "huggingface"
    model_download_endpoint: str = ""
    model_version: str = "pipeline"
    language: str = "auto"
    enable_formula: bool = True
    enable_table: bool = True
    is_ocr: bool = False
    allow_local_model_download: bool = False
    api_token: str | None = None


def _mineru_response(row) -> dict:
    settings, token_set, _ = mineru_settings_with_token(row)
    from app.services.document_parsing import find_mineru_cli

    return {
        "settings": {**settings, "version": 1},
        "api_token_set": token_set,
        "local_cli": find_mineru_cli(settings.get("local_cli_path", "")),
    }


@mineru_router.get("")
async def get_mineru(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await get_or_create(db, current_user.id)
    return _mineru_response(row)


@mineru_router.put("")
async def put_mineru(
    payload: MinerUUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await get_or_create(db, current_user.id)
    settings = payload.model_dump(exclude={"api_token"})
    save_mineru_settings(row, settings, payload.api_token)
    await db.commit()
    await db.refresh(row)
    return _mineru_response(row)


@mineru_router.post("/test")
async def test_mineru(
    payload: MinerUUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Uji koneksi: mode cloud memanggil API, mode lokal memeriksa CLI."""
    row = await get_or_create(db, current_user.id)
    stored_settings, _, stored_token = mineru_settings_with_token(row)

    settings = {**stored_settings, **payload.model_dump(exclude={"api_token"})}
    token = payload.api_token if payload.api_token is not None else stored_token

    if settings.get("mode") == "cloud":
        if not token:
            return {"ok": False, "message": "API token belum diisi. Masukkan token MinerU dulu."}
        base = (settings.get("api_base_url") or "https://mineru.net").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{base}/api/v4/account",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError:
            return {"ok": False, "message": "Tidak dapat terhubung ke server MinerU."}
        if resp.status_code in (401, 403):
            return {"ok": False, "message": "Token ditolak oleh server MinerU."}
        return {"ok": True, "message": f"Terhubung ke {base} (HTTP {resp.status_code})."}

    # Mode lokal: periksa CLI.
    from app.services.document_parsing import find_mineru_cli

    cli = find_mineru_cli(settings.get("local_cli_path", ""))
    if cli["found"]:
        return {"ok": True, "message": f"CLI MinerU ditemukan: {cli['path']}"}
    if cli["source"] == "configured":
        return {"ok": False, "message": "Path CLI yang dikonfigurasi tidak dapat dieksekusi."}
    return {"ok": False, "message": "CLI MinerU tidak ditemukan di PATH. Instal atau atur path CLI."}


class MinerUModelsDownloadRequest(BaseModel):
    model_type: str = "pipeline"
    source: str = "huggingface"
    endpoint: str = ""
    local_cli_path: str = ""


@mineru_router.post("/models/download")
async def download_mineru_models(
    payload: MinerUModelsDownloadRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Jalankan mineru-models-download di server (satu job global)."""
    if payload.model_type not in ("pipeline", "vlm", "all"):
        raise HTTPException(status_code=400, detail="model_type harus pipeline, vlm, atau all.")
    cli = (payload.local_cli_path or "").strip() or shutil.which("mineru-models-download")
    if not cli:
        raise HTTPException(
            status_code=409,
            detail="Perintah mineru-models-download tidak ditemukan. Instal MinerU dulu atau atur path CLI.",
        )
    cmd = [cli, "--type", payload.model_type, "--source", payload.source]
    endpoint = (payload.endpoint or "").strip() if payload.source == "huggingface" else ""
    ok, err = start_job(
        "models",
        cmd,
        "Model MinerU berhasil diunduh.",
        env=env_with(endpoint) if endpoint else None,
    )
    if not ok:
        raise HTTPException(status_code=409, detail=err)
    return {"ok": True, "message": "Unduhan model dimulai."}


@mineru_router.get("/models/download/status")
async def get_mineru_download_status(cursor: int = 0):
    return job_status(cursor)


@mineru_router.post("/models/download/cancel")
async def cancel_mineru_download():
    cancel_job()
    return {"ok": True}
