import os
import uuid
import logging
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
    return f"https://storage.googleapis.com/{BUCKET_NAME}/{blob_name}"


async def capture_screenshot(
    browser,
    url: str,
    viewport: dict,
    file_path: str,
    *,
    is_mobile: bool = False,
) -> None:
    page = await browser.new_page(viewport=viewport, is_mobile=is_mobile)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)
        await page.screenshot(path=file_path, full_page=True)
    finally:
        await page.close()


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
                d_path = f"/tmp/{page_id}-desktop.png"
                m_path = f"/tmp/{page_id}-mobile.png"
                logger.info("Capturing %s", url)

                try:
                    await capture_screenshot(browser, url, DESKTOP, d_path)
                    await capture_screenshot(
                        browser,
                        url,
                        MOBILE,
                        m_path,
                        is_mobile=True,
                    )

                    results.append({
                        "url": url,
                        "desktop": upload_to_gcs(d_path),
                        "mobile": upload_to_gcs(m_path),
                    })
                except Exception as exc:
                    logger.exception("Failed to capture %s", url)
                    results.append({
                        "url": url,
                        "error": str(exc),
                    })
                finally:
                    for file_path in (d_path, m_path):
                        try:
                            os.remove(file_path)
                        except FileNotFoundError:
                            pass
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
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "error": str(exc)},
    )


@app.post("/screenshot")
async def screenshot(req: ScreenshotRequest):
    logger.info("Received request for %d urls", len(req.urls))
    return {"results": await capture(req.urls)}
