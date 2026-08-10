import logging
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.services.model_selection import ModelSelectionError, resolve_llm
from app.services.preferences import build_http_client, get_preferences

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/visualize", tags=["visualize"])

class VisualizeRequest(BaseModel):
    data_description: str
    chart_type: str = "auto"
    data_json: str | None = None

@router.post("")
async def generate_visualization(
    payload: VisualizeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        llm = await resolve_llm(db, current_user.id)
    except ModelSelectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    prefs = await get_preferences(db, current_user.id)
    api_key = llm.api_key
    proxy_client = build_http_client(prefs)
    client = AsyncOpenAI(
        api_key=api_key or "dummy",
        base_url=llm.base_url,
        timeout=float(prefs.request_timeout),
        http_client=proxy_client,
    )

    prompt = f"""Berdasarkan deskripsi data berikut, buat kode HTML interaktif yang memvisualisasikan data tersebut.

Deskripsi: {payload.data_description}
Jenis chart yang diminta: {payload.chart_type}
{f'Data JSON: {payload.data_json}' if payload.data_json else ''}

Gunakan Chart.js (CDN: https://cdn.jsdelivr.net/npm/chart.js) untuk membuat grafik.
Kembalikan HANYA kode HTML lengkap yang bisa langsung dirender di browser. Sertakan Chart.js dari CDN.
Buat visualisasi yang menarik dengan warna modern, judul, dan label sumbu.
Pastikan HTML responsif dan siap dimasukkan ke dalam iframe.
JANGAN gunakan markdown fence atau pembungkus apapun, langsung kode HTML murni."""

    try:
        response = await client.chat.completions.create(
            model=llm.model_name,
            messages=[
                {"role": "system", "content": "Kamu adalah ahli visualisasi data. Buat kode HTML/Chart.js yang bersih dan fungsional."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=4000,
        )
        html_content = (response.choices[0].message.content or "").strip()
        if html_content.startswith("```html"):
            html_content = html_content.replace("```html", "").replace("```", "").strip()
        elif html_content.startswith("```"):
            html_content = html_content.replace("```", "").strip()

        return {"html": html_content, "chart_type": payload.chart_type}
    except Exception as e:
        logger.error(f"Visualize generation failed: {e}")
        raise HTTPException(status_code=500, detail=f"Gagal membuat visualisasi: {e}")
    finally:
        if proxy_client:
            await proxy_client.aclose()
