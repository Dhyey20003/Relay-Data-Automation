"""PDF image/text extraction, page rendering and manual cropping (pypdfium2).

Nothing here guesses silently: every image taken from a PDF is staged for
review with its page number, position and nearby text, and pages that look
scanned or have no extractable photos are flagged so the operator can crop
manually from the rendered page.
"""
from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

PREVIEW_SCALE = 1.5  # 108 DPI page previews
CROP_SCALE = 300 / 72  # crops are taken from a 300 DPI render
MIN_IMAGE_PX = 48


class PdfError(ValueError):
    pass


def open_pdf(path: Path) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        msg = str(exc)
        if "password" in msg.lower():
            raise PdfError("The PDF is password protected. Remove the password and try again.") from None
        raise PdfError(f"The PDF could not be opened ({msg}).") from None


def analyse(path: Path, out_dir: Path, max_pages: int) -> dict:
    """Extract embedded images and text, render page previews.

    Returns {"pages": [...], "images": [...]} with file paths inside out_dir.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = open_pdf(path)
    try:
        n = len(pdf)
        if n > max_pages:
            raise PdfError(f"The PDF has {n} pages; the limit is {max_pages}.")
        pages, images = [], []
        for pno in range(n):
            page = pdf[pno]
            try:
                pw, ph = page.get_size()
                textpage = page.get_textpage()
                text = (textpage.get_text_range() or "").strip()
                render = page.render(scale=PREVIEW_SCALE).to_pil().convert("RGB")
                render_path = out_dir / f"page_{pno + 1:04d}.png"
                render.save(render_path, "PNG")
                flags = []
                page_images = []
                for idx, obj in enumerate(page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,))):
                    try:
                        left, bottom, right, top = obj.get_bounds()
                    except Exception:  # noqa: BLE001 - malformed object
                        left = bottom = right = top = 0
                    try:
                        pil = obj.get_bitmap(render=False).to_pil()
                    except Exception:  # noqa: BLE001 - unsupported encoding
                        try:
                            pil = obj.get_bitmap(render=True).to_pil()
                        except Exception:  # noqa: BLE001
                            flags.append("unreadable_image")
                            continue
                    if pil.width < MIN_IMAGE_PX or pil.height < MIN_IMAGE_PX:
                        continue  # icons, bullets, logos
                    if pil.mode not in ("RGB", "L"):
                        pil = pil.convert("RGB")
                    img_path = out_dir / f"p{pno + 1:04d}_img{idx + 1:02d}.png"
                    pil.save(img_path, "PNG")
                    near = textpage.get_text_bounded(left=max(0, left - 30), bottom=max(0, bottom - 60),
                                                     right=min(pw, right + 30), top=min(ph, top + 20)) or ""
                    area = max(0.0, (right - left) * (top - bottom)) / max(1.0, pw * ph)
                    item_flags = []
                    if area > 0.8:
                        item_flags.append("full_page_image")
                    page_images.append({
                        "path": img_path, "page_no": pno + 1, "bbox": [left, bottom, right, top],
                        "px_bbox": [left * PREVIEW_SCALE, (ph - top) * PREVIEW_SCALE, right * PREVIEW_SCALE,
                                    (ph - bottom) * PREVIEW_SCALE],
                        "context_text": " ".join(near.split())[:300], "width": pil.width, "height": pil.height,
                        "flags": item_flags,
                    })
                if not text:
                    flags.append("no_text")
                if not page_images:
                    flags.append("no_images")
                if any("full_page_image" in i["flags"] for i in page_images):
                    flags.append("scanned")
                if len(page_images) > 1:
                    flags.append("multiple_images")
                images.extend(page_images)
                pages.append({"page_no": pno + 1, "render_path": render_path, "width_px": render.width,
                              "height_px": render.height, "page_w_pt": pw, "page_h_pt": ph, "text": text[:4000],
                              "image_count": len(page_images), "flags": flags})
            finally:
                page.close()
        return {"pages": pages, "images": images}
    finally:
        pdf.close()


def crop(pdf_path: Path, page_no: int, rect_preview_px: tuple[float, float, float, float], out_path: Path) -> Path:
    """Crop a rectangle (x, y, w, h in preview-render pixels) from a 300 DPI render."""
    x, y, w, h = rect_preview_px
    if w < 10 or h < 10:
        raise PdfError("The selected area is too small.")
    pdf = open_pdf(pdf_path)
    try:
        if page_no < 1 or page_no > len(pdf):
            raise PdfError("Page not found in PDF.")
        page = pdf[page_no - 1]
        try:
            img = page.render(scale=CROP_SCALE).to_pil().convert("RGB")
        finally:
            page.close()
    finally:
        pdf.close()
    k = CROP_SCALE / PREVIEW_SCALE
    box = (max(0, int(x * k)), max(0, int(y * k)), min(img.width, int((x + w) * k)), min(img.height, int((y + h) * k)))
    if box[2] - box[0] < 10 or box[3] - box[1] < 10:
        raise PdfError("The selected area is outside the page.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.crop(box).save(out_path, "PNG")
    return out_path


def page_count(path: Path) -> int:
    pdf = open_pdf(path)
    try:
        return len(pdf)
    finally:
        pdf.close()

