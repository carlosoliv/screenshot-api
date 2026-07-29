import os
import uuid
import logging
import traceback
import sys
from typing import List
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright
from google.cloud import storage

# Force logging to stderr so PM2 sees it
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("screenshot-api")

# ====== CONFIG ======
BUCKET_NAME = os.getenv("BUCKET_NAME", "trw-automation-carlosog-01")
CHROMIUM_PATH = os.getenv("CHROMIUM_PATH", "/usr/bin/chromium")

DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}

app = FastAPI()
storage_client = storage.Client()
bucket = storage_client.bucket(BUCKET_NAME)


class ScreenshotRequest(BaseModel):
    urls: List[str]


def upload_to_gcs(file_path: str) -> str:
    blob_name = os.path.basename(file_path)
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(file_path)
    os.remove(file_path)
    return f"https://storage.googleapis.com/{BUCKET_NAME}/{blob_name}"


async def capture(urls: List[str]):
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            executable_path=CHROMIUM_PATH,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            for url in urls:
                page_id = uuid.uuid4().hex[:8]
                logger.info("Capturing %s", url)

                # Desktop
                page = await browser.new_page(viewport=DESKTOP)
                try:
                    await page.goto(url, wait_until="networkidle", timeout=60000)
                    await page.wait_for_timeout(5000)
                    d_path = f"/tmp/{page_id}-desktop.png"
                    await page.screenshot(path=d_path, full_page=True)
                finally:
                    await page.close()

                # Mobile
                page = await browser.new_page(viewport=MOBILE, is_mobile=True)
                try:
                    await page.goto(url, wait_until="networkidle", timeout=60000)
                    await page.wait_for_timeout(5000)
                    m_path = f"/tmp/{page_id}-mobile.png"
                    await page.screenshot(path=m_path, full_page=True)
                finally:
                    await page.close()

                results.append({
                    "url": url,
                    "desktop": upload_to_gcs(d_path),
                    "mobile": upload_to_gcs(m_path),
                })
        finally:
            await browser.close()
    return results


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # This guarantees the full traceback lands in PM2 error logs
    logger.error(
        "Unhandled exception on %s %s",
        request.method,
        request.url.path,
        exc_info=True,
    )
    # optional: also print raw traceback
    traceback.print_exc(file=sys.stderr)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "error": str(exc)},
    )


@app.post("/screenshot")
async def screenshot(req: ScreenshotRequest):
    logger.info("Received request for %d urls", len(req.urls))
    return {"results": await capture(req.urls)}