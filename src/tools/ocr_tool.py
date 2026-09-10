"""OCR tool — extract text from images via PaddleOCR (paddlepaddle backend).

Complements ``vision_analyze``: unlike the vision tool, this one does **not**
depend on a configured ``vision_model``. It runs OCR locally via PaddleOCR on
the paddlepaddle backend, so it works whenever ``vision_model`` is empty.

Why PaddleOCR (paddlepaddle) and not onnxruntime/torch-backed OCR: on this
machine an enterprise EDR (UniEDRAgent) blocks the DllMain of native
extensions like onnxruntime/torch, so rapidocr-onnxruntime / PaddleOCR-with-
torch / easyocr all fail to load. paddlepaddle's DLLs are NOT blocked.
PaddleOCR 3.x runs purely on paddlepaddle; its paddlex/modelscope deps only
touch torch lazily, so in a torch-free venv it imports and infers fine.

Offline models: this machine has no internet to modelscope/aistudio, so the
PP-OCR models must be obtained elsewhere and placed in
``~/.paddlex/official_models/`` (copy the whole ``~/.paddlex`` from a
machine that ran PaddleOCR once online). See project memory.

Use it for text-heavy images — screenshots, chat logs, documents, tables.
It **cannot** describe photos or interpret charts; for those, configure a
``vision_model`` and use ``vision_analyze``.

Design:
  - Lazy load: paddleocr is imported + the engine is built on first call and
    cached as a module-level singleton. ``check_fn`` uses
    ``importlib.util.find_spec`` so availability is decided *without*
    triggering the (slower) import — agent startup is unaffected.
  - Version-compatible: PaddleOCR 2.x (``ocr(cls=True)``) and 3.x
    (``predict()``) differ in both call API and result shape; we handle both.
"""

import atexit
import importlib.util
import json
import logging
import multiprocessing as mp
import os
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)


def _check_ocr() -> bool:
    """Return True if paddleocr is importable, without importing it.

    Never consults ``vision_model`` — the tool stays visible exactly when
    ``vision_analyze`` is hidden.
    """
    return importlib.util.find_spec("paddleocr") is not None


def _ocr_worker_main(conn) -> None:
    """Resident OCR subprocess: receive image paths, infer, reply.

    Must stay module-level (spawn pickles by qualified name). The engine is
    built lazily on the first job and kept alive for the process lifetime —
    PaddleOCR instances are not thread-safe and are expensive to (re)build.
    Runs through one Pipe, so requests are naturally serialized.
    """
    # Offline machine: use local models only, skip the model-hoster
    # connectivity check that would otherwise try to reach the internet
    # (modelscope/aistudio/huggingface) and time out on every startup.
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

    engine = None
    init_error: Optional[str] = None

    def _init():
        nonlocal engine, init_error
        if init_error is not None:
            return ("init", init_error)
        if engine is not None:
            return None
        try:
            from paddleocr import PaddleOCR
            try:
                # mkldnn disabled: paddlepaddle's oneDNN executor hits a PIR
                # attribute bug (ConvertPirAttribute2RuntimeAttribute not
                # support ArrayAttribute<DoubleAttribute>) on PP-OCRv5 det.
                engine = PaddleOCR(lang="ch", enable_mkldnn=False)   # 3.x
            except TypeError:
                engine = PaddleOCR(use_angle_cls=True, lang="ch")     # 2.x
        except Exception as e:  # remembered: no retry storm per request
            init_error = f"PaddleOCR init failed: {type(e).__name__}: {e}"
            return ("init", init_error)
        return None

    conn.send("ready")
    while True:
        try:
            img = conn.recv()
        except (EOFError, OSError):
            break
        if img is None:  # shutdown signal
            break
        err = _init()
        if err is not None:
            conn.send(("err", err[1]))
            continue
        try:
            conn.send(("ok", _run_ocr(engine, img)))
        except Exception as e:
            conn.send(("err", f"{type(e).__name__}: {e}"))
    try:
        conn.close()
    except Exception:
        pass


class _WorkerHandle:
    """Lazily-started, crash-self-healing proxy for the OCR subprocess.

    The engine must live outside the serve process: inference is CPU-bound
    and PaddleOCR's python pipeline holds the GIL long enough to starve the
    aiohttp event loop (desktop shows "connection lost"), plus ~600 MB of
    model memory would otherwise stay resident in serve. A resident spawn
    subprocess fixes both; PaddleOCR issue #17144 blesses exactly this shape.
    """

    def __init__(self):
        self.proc: Optional[mp.process.BaseProcess] = None
        self.conn = None

    def _cleanup(self):
        if self.proc is not None:
            try:
                if self.conn is not None:
                    try:
                        self.conn.send(None)  # polite shutdown first
                    except Exception:
                        pass
                self.proc.join(timeout=2)
                if self.proc.is_alive():
                    self.proc.kill()
                self.proc.join(timeout=2)  # reap — never leave a zombie
            except Exception:
                pass
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.proc, self.conn = None, None

    def _ensure(self, start_timeout: int = 180):
        if self.proc is not None and self.proc.is_alive() and self.conn is not None:
            return
        self._cleanup()
        ctx = mp.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        proc = ctx.Process(
            target=_ocr_worker_main, args=(child_conn,),
            daemon=True, name="xihe-ocr-worker",
        )
        proc.start()
        child_conn.close()  # parent side only from here on
        try:
            if not parent_conn.poll(start_timeout):
                raise TimeoutError(f"OCR worker start timed out after {start_timeout}s")
            parent_conn.recv()  # blocks until "ready"
        except Exception:
            proc.kill()
            proc.join(timeout=2)
            parent_conn.close()
            raise
        self.proc, self.conn = proc, parent_conn

    def run(self, img: str, timeout: int = 300) -> list[tuple[str, float]]:
        self._ensure()
        try:
            self.conn.send(img)
            if not self.conn.poll(timeout):
                # Worker wedged mid-inference: kill (daemon=True already
                # covers parent death; this covers a hung child). Next call
                # restarts it via _ensure.
                self._cleanup()
                raise TimeoutError(f"OCR timed out after {timeout}s")
            status, payload = self.conn.recv()
        except (BrokenPipeError, EOFError, OSError):
            self._cleanup()
            raise RuntimeError("OCR worker died unexpectedly")
        if status == "err":
            raise RuntimeError(payload)
        return payload


_worker = _WorkerHandle()
atexit.register(_worker._cleanup)


def _run_ocr(engine, img: str) -> list[tuple[str, float]]:
    """Run OCR on a local image path; return normalized [(text, confidence)].

    Prefers 3.x ``predict()``, falls back to 2.x ``ocr(cls=True)`` (then
    ``ocr()`` without ``cls`` for 3.x's back-compat method).
    """
    result = None
    predict = getattr(engine, "predict", None)
    if callable(predict):
        try:
            result = predict(img)
        except Exception as e:
            logger.debug("predict() failed (%s); trying ocr()", e)
            result = None

    if result is None:
        try:
            result = engine.ocr(img, cls=True)
        except TypeError:
            result = engine.ocr(img)

    return _normalize(result)


def _get(obj, key):
    """Dict-like get that also works on objects exposing attributes."""
    if hasattr(obj, "get"):
        try:
            return obj.get(key)
        except Exception:
            pass
    return getattr(obj, key, None)


def _normalize(result) -> list[tuple[str, float]]:
    """Normalize PaddleOCR output (2.x or 3.x) into [(text, confidence)].

    3.x ``predict``/``ocr`` returns (per image) a dict-like / OCRResult with
    ``rec_texts`` and ``rec_scores``. 2.x ``ocr`` returns a list of
    ``[box, (text, conf)]``.
    """
    if not result:
        return []

    first = result[0] if isinstance(result, list) and result else result
    if first is None:
        return []

    # 3.x dict / OCRResult shape
    rec_texts = _get(first, "rec_texts")
    if rec_texts is None and not hasattr(first, "get") and hasattr(first, "rec_texts"):
        rec_texts = getattr(first, "rec_texts")

    if rec_texts is None and hasattr(first, "json"):
        try:
            j = first.json
            if isinstance(j, str):
                j = json.loads(j)
            if isinstance(j, dict):
                rec_texts = j.get("rec_texts")
        except Exception:
            pass

    if rec_texts is not None:
        scores = _get(first, "rec_scores") or []
        if not isinstance(scores, (list, tuple)):
            scores = []
        pairs = []
        for i, text in enumerate(rec_texts):
            if not text:
                continue
            s = scores[i] if i < len(scores) else 0.0
            try:
                s = float(s)
            except (TypeError, ValueError):
                s = 0.0
            pairs.append((str(text), s))
        return pairs

    # 2.x list shape: [[box, (text, conf)], ...]
    pairs = []
    if isinstance(first, list):
        for item in first:
            try:
                tc = item[1]
                if isinstance(tc, (list, tuple)) and len(tc) >= 1:
                    text, conf = tc[0], (tc[1] if len(tc) >= 2 else 0.0)
                else:
                    text, conf = str(tc), 0.0
                if text:
                    pairs.append((str(text), float(conf)))
            except Exception:
                continue
    return pairs


_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_CT_TO_SUFFIX = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
}


def _suffix_for(content_type: str, url: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _CT_TO_SUFFIX:
        return _CT_TO_SUFFIX[ct]
    ext = Path(urlparse(url).path).suffix.lower()
    return ext if ext in _CT_TO_SUFFIX.values() else ".png"


def _download_to_temp(url: str) -> str:
    """Download a URL to a temp file; return its path. Raises ValueError."""
    try:
        resp = httpx.get(
            url, timeout=30, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; XiheAgent/1.0)",
                     "Accept": "image/*,*/*;q=0.8"},
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise ValueError(f"Failed to download image: {e}")

    data = resp.content
    if len(data) > _MAX_BYTES:
        raise ValueError(f"Image too large: {len(data)} bytes (max 10MB)")

    suffix = _suffix_for(resp.headers.get("content-type", ""), str(resp.url))
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(data)
    tmp.close()
    return tmp.name


def _ocr(args: dict, **kw) -> str:
    image = args.get("image", "")
    if not image:
        return tool_error("image (path or URL) is required")

    if not _check_ocr():
        return tool_error(
            "OCR not available — paddleocr is not installed",
            hint="pip install paddleocr paddlepaddle",
        )

    tmp_path: Optional[str] = None
    try:
        if image.startswith(("http://", "https://")):
            tmp_path = _download_to_temp(image)
            local = tmp_path
        else:
            local_path = Path(image).expanduser().resolve()
            if not local_path.is_file():
                return tool_error(f"Image file not found: {image}")
            local = str(local_path)

        size = Path(local).stat().st_size
        if size > _MAX_BYTES:
            return tool_error(f"Image too large: {size} bytes (max 10MB)")

        try:
            pairs = _worker.run(local, timeout=300)
        except RuntimeError as e:
            msg = str(e)
            hint = None
            if "model" in msg.lower() or "hosting" in msg.lower() or "download" in msg.lower():
                hint = ("Models missing/offline — copy a pre-populated "
                        "~/.paddlex dir from an online machine")
            return tool_error(f"OCR engine unavailable: {e}", hint=hint)
    except ValueError as e:
        return tool_error(str(e))
    except Exception as e:
        logger.exception("OCR failed for %s", image)
        return tool_error(f"OCR failed: {type(e).__name__}: {e}")
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

    if not pairs:
        return tool_result(text="", lines=[], count=0, no_text=True)

    lines = [{"text": t, "confidence": round(c, 3)} for t, c in pairs]
    full_text = "\n".join(t for t, _ in pairs)

    from core.support.redact import redact_sensitive_text
    full_text = redact_sensitive_text(full_text)

    return tool_result(text=full_text, lines=lines, count=len(lines))


registry.register(
    name="image_ocr",
    schema={
        "type": "function",
        "function": {
            "name": "image_ocr",
            "description": (
                "Extract text from an image using OCR (PaddleOCR). Provide an "
                "image file path or URL. Best for screenshots, chat logs, "
                "documents, and tables — images that are mostly text. Supports "
                "Chinese and English. Cannot describe photo content or "
                "interpret charts; for that use vision_analyze. Use this to "
                "read text in an image when vision_analyze is unavailable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image": {"type": "string", "description": "Image file path or URL"},
                },
                "required": ["image"],
            },
        },
    },
    handler=lambda args, **kw: _ocr(args, **kw),
    path_params=("image",),
    check_fn=_check_ocr,
    toolset="media",
    read_only=True,
)
