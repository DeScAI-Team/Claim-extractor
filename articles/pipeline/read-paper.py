#!/usr/bin/env python3
"""
PDF → raster pages → one vLLM call per page (Nanonets-OCR2-3B via OpenAI-compatible API).

  vllm serve nanonets/Nanonets-OCR2-3B

Env: VLLM_BASE_URL, VLLM_API_KEY, READ_PAPER_MODEL (defaults match review.py + HF model id).
Output: articles/data/<pdf_stem>/page_XXX.md and full.md
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
from pathlib import Path

from openai import OpenAI

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[misc, assignment]

try:
    import pypdfium2 as pdfium
except ImportError as e:  # pragma: no cover
    print(
        "error: pip install pypdfium2 (or docling, which depends on it).",
        file=sys.stderr,
    )
    raise SystemExit(1) from e

PIPELINE = Path(__file__).resolve().parent
ARTICLES = PIPELINE.parent
REPO_ROOT = ARTICLES.parent
DEFAULT_DATA = ARTICLES / "data"

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
VLLM_API_KEY = os.environ.get("VLLM_API_KEY", "none")
DEFAULT_MODEL = os.environ.get("READ_PAPER_MODEL", "nanonets/Nanonets-OCR2-3B")

_PROMPT = (
    "Extract the text from the above document as if you were reading it naturally. "
    "Return the tables in html format. Return the equations in LaTeX representation. "
    "If there is an image in the document and image caption is not present, add a small "
    "description of the image inside the <img></img> tag; otherwise, add the image caption "
    "inside <img></img>. Watermarks should be wrapped in brackets. Ex: "
    "<watermark>OFFICIAL COPY</watermark>. Page numbers should be wrapped in brackets. Ex: "
    "<page_number>14</page_number> or <page_number>9/22</page_number>. "
    "Prefer using ☐ and ☑ for check boxes."
)


def rasterize_pdf(pdf_path: Path, scale: float = 2.0) -> list:
    """Open PDF and return one RGB PIL image per page (in order)."""
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        out = []
        for i in range(len(doc)):
            pil = doc[i].render(scale=scale).to_pil()
            if pil.mode != "RGB":
                pil = pil.convert("RGB")
            out.append(pil)
        return out
    finally:
        doc.close()


def read_page(
    client: OpenAI,
    model: str,
    pil_image,
    *,
    max_tokens: int = 15000,
    max_retries: int = 4,
) -> str:
    """One chat completion: PNG-encode the page image and return OCR markdown text."""
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    data_url = "data:image/png;base64," + base64.standard_b64encode(buf.getvalue()).decode(
        "ascii"
    )

    last: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            r = client.chat.completions.create(
                model=model,
                temperature=0.0,
                max_tokens=max_tokens,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": data_url}},
                            {"type": "text", "text": _PROMPT},
                        ],
                    }
                ],
            )
            return (r.choices[0].message.content or "").strip()
        except Exception as e:
            last = e
            if attempt < max_retries:
                time.sleep(min(2**attempt, 30))
    assert last is not None
    raise last


def _safe_stem(stem: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" ._-") or "document"
    return s[:120]


def main() -> None:
    p = argparse.ArgumentParser(description="PDF → Nanonets OCR via local vLLM.")
    p.add_argument("--pdf", type=Path, required=True)
    p.add_argument("--out-root", type=Path, default=DEFAULT_DATA)
    p.add_argument("--model", type=str, default=DEFAULT_MODEL)
    p.add_argument("--base-url", type=str, default=VLLM_BASE_URL)
    p.add_argument("--api-key", type=str, default=VLLM_API_KEY)
    p.add_argument("--render-scale", type=float, default=2.0)
    p.add_argument("--max-tokens", type=int, default=15000)
    p.add_argument("--max-retries", type=int, default=4)
    args = p.parse_args()

    if load_dotenv:
        load_dotenv(REPO_ROOT / ".env")

    pdf = args.pdf.expanduser().resolve()
    if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
        print(f"error: not a PDF file: {pdf}", file=sys.stderr)
        sys.exit(1)

    out_dir = args.out_root.expanduser().resolve() / _safe_stem(pdf.stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    client = OpenAI(base_url=args.base_url, api_key=args.api_key)

    pages = rasterize_pdf(pdf, args.render_scale)
    if not pages:
        print("error: PDF has no pages", file=sys.stderr)
        sys.exit(1)

    chunks: list[str] = []
    for i, img in enumerate(pages, start=1):
        text = read_page(
            client,
            args.model,
            img,
            max_tokens=args.max_tokens,
            max_retries=args.max_retries,
        )
        (out_dir / f"page_{i:03d}.md").write_text(text + "\n", encoding="utf-8")
        chunks.append(f"<!-- page {i} -->\n\n{text}")

    (out_dir / "full.md").write_text("\n\n---\n\n".join(chunks) + "\n", encoding="utf-8")
    (out_dir / "metadata.json").write_text(
        json.dumps(
            {
                "pdf": str(pdf),
                "output_folder": str(out_dir),
                "model": args.model,
                "base_url": args.base_url,
                "pages": len(pages),
                "render_scale": args.render_scale,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Done: {len(pages)} pages → {out_dir}")


if __name__ == "__main__":
    main()
