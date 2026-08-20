import uvicorn

from app.core.config import settings

if __name__ == "__main__":
    # Port dibaca dari settings supaya URL media hasil impor (yang disusun dari
    # settings.backend_base_url) selalu menunjuk ke port yang benar-benar dipakai.
    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.BACKEND_PORT, reload=False)
