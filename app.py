import os
import uuid
import logging
import math
import re
import sys
from typing import List, Optional, Tuple
from urllib.parse import unquote, urlsplit

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
GCS_PUBLIC_HOST = "storage.googleapis.com"
MAX_GCS_BATCH_SIZE = 100
SCREENSHOT_BLOB_PATTERN = re.compile(
    r"^[0-9a-f]{8}-(?:desktop|mobile)\.png$"
)

app = FastAPI()
storage_client = None
bucket = None


class ScreenshotRequest(BaseModel):
    urls: List[str]


class CleanupResult(BaseModel):
    url: str
    desktop: Optional[str] = None
    mobile: Optional[str] = None
    error: Optional[str] = None


class CleanupRequest(BaseModel):
    results: List[CleanupResult]


def get_storage_resources():
    global storage_client, bucket

    if storage_client is None:
        storage_client = storage.Client()
    if bucket is None:
        bucket = storage_client.bucket(BUCKET_NAME)

    return storage_client, bucket


def upload_to_gcs(file_path: str) -> str:
    _, storage_bucket = get_storage_resources()
    blob_name = os.path.basename(file_path)
    blob = storage_bucket.blob(blob_name)
    blob.upload_from_filename(file_path)
    return f"https://storage.googleapis.com/{BUCKET_NAME}/{blob_name}"


def blob_name_from_screenshot_url(image_url: str) -> str:
    parsed = urlsplit(image_url)
    expected_prefix = f"/{BUCKET_NAME}/"

    if (
        parsed.scheme != "https"
        or parsed.netloc != GCS_PUBLIC_HOST
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(expected_prefix)
    ):
        raise ValueError("URL is not an image URL for the configured bucket")

    blob_name = unquote(parsed.path[len(expected_prefix):])
    if not SCREENSHOT_BLOB_PATTERN.fullmatch(blob_name):
        raise ValueError("URL does not reference an app-generated screenshot")

    return blob_name


def collect_cleanup_targets(
    results: List[CleanupResult],
) -> Tuple[List[str], int, int]:
    blob_names = []
    seen = set()
    duplicates_ignored = 0
    skipped_records = 0

    for result_index, result in enumerate(results):
        image_count = 0
        for image_type in ("desktop", "mobile"):
            image_url = getattr(result, image_type)
            if image_url is None:
                continue

            image_count += 1
            try:
                blob_name = blob_name_from_screenshot_url(image_url)
            except ValueError as exc:
                raise ValueError(
                    f"results[{result_index}].{image_type}: {exc}"
                ) from exc

            if blob_name in seen:
                duplicates_ignored += 1
                continue

            seen.add(blob_name)
            blob_names.append(blob_name)

        if image_count == 0:
            skipped_records += 1

    return blob_names, duplicates_ignored, skipped_records


def delete_blobs_in_batches(blob_names: List[str]) -> int:
    if not blob_names:
        return 0

    client, storage_bucket = get_storage_resources()
    batch_count = math.ceil(len(blob_names) / MAX_GCS_BATCH_SIZE)

    for batch_index, offset in enumerate(
        range(0, len(blob_names), MAX_GCS_BATCH_SIZE),
        start=1,
    ):
        batch_blob_names = blob_names[offset:offset + MAX_GCS_BATCH_SIZE]
        logger.info(
            "Starting cleanup batch %d/%d with %d objects from bucket %s",
            batch_index,
            batch_count,
            len(batch_blob_names),
            BUCKET_NAME,
        )
        try:
            with client.batch():
                storage_bucket.delete_blobs(
                    batch_blob_names,
                    client=client,
                )
        except Exception:
            logger.exception(
                "Cleanup batch %d/%d failed; some deletions may have succeeded",
                batch_index,
                batch_count,
            )
            raise

        logger.info(
            "Completed cleanup batch %d/%d; deleted %d objects",
            batch_index,
            batch_count,
            len(batch_blob_names),
        )

    return batch_count


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


async def capture_url(browser, url: str):
    page_id = uuid.uuid4().hex[:8]
    d_path = f"/tmp/{page_id}-desktop.png"
    m_path = f"/tmp/{page_id}-mobile.png"
    result = {"url": url}
    errors = []

    logger.info("Capturing %s", url)
    try:
        for image_type, viewport, file_path, is_mobile in (
            ("desktop", DESKTOP, d_path, False),
            ("mobile", MOBILE, m_path, True),
        ):
            try:
                await capture_screenshot(
                    browser,
                    url,
                    viewport,
                    file_path,
                    is_mobile=is_mobile,
                )
                result[image_type] = upload_to_gcs(file_path)
                logger.info(
                    "Captured and uploaded %s screenshot for %s",
                    image_type,
                    url,
                )
            except Exception as exc:
                logger.exception(
                    "Failed to capture/upload %s screenshot for %s",
                    image_type,
                    url,
                )
                errors.append(f"{image_type}: {exc}")

        if errors:
            result["error"] = "; ".join(errors)

        return result
    finally:
        for file_path in (d_path, m_path):
            try:
                os.remove(file_path)
            except FileNotFoundError:
                pass


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
                results.append(await capture_url(browser, url))
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


@app.post("/cleanup")
def cleanup(req: CleanupRequest):
    logger.info(
        "Received cleanup request with %d screenshot results",
        len(req.results),
    )

    try:
        blob_names, duplicates_ignored, skipped_records = collect_cleanup_targets(
            req.results
        )
    except ValueError as exc:
        logger.warning("Rejected cleanup request: %s", exc)
        return JSONResponse(
            status_code=400,
            content={
                "status": "rejected",
                "detail": str(exc),
            },
        )

    logger.info(
        "Validated cleanup request: %d unique objects, %d duplicates ignored, "
        "%d records without image URLs",
        len(blob_names),
        duplicates_ignored,
        skipped_records,
    )

    try:
        batch_count = delete_blobs_in_batches(blob_names)
    except Exception:
        return JSONResponse(
            status_code=502,
            content={
                "status": "failed",
                "detail": (
                    "Cloud Storage cleanup failed; some deletions may have succeeded"
                ),
                "attempted": len(blob_names),
            },
        )

    logger.info(
        "Cleanup completed: deleted %d objects in %d batches",
        len(blob_names),
        batch_count,
    )
    return {
        "status": "completed",
        "attempted": len(blob_names),
        "deleted": len(blob_names),
        "batches": batch_count,
        "duplicates_ignored": duplicates_ignored,
        "skipped_records": skipped_records,
    }
