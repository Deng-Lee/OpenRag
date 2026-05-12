"""RAGFlow-compatible lazy image helpers (vendored subset, no rag.nlp dependency)."""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Any

from PIL import Image

__all__ = [
    "LazyImage",
    "LazyDocxImage",
    "ensure_pil_image",
    "is_image_like",
    "open_image",
    "open_image_for_processing",
]


def _concat_pil_vertical(img1: Image.Image, img2: Image.Image) -> Image.Image:
    """Stack two RGB PIL images vertically (same behavior as RAGFlow concat_img for PIL)."""
    width1, height1 = img1.size
    width2, height2 = img2.size
    new_width = max(width1, width2)
    new_height = height1 + height2
    new_image = Image.new("RGB", (new_width, new_height))
    new_image.paste(img1, (0, 0))
    new_image.paste(img2, (0, height1))
    return new_image


class LazyImage:
    def __init__(self, blobs, source=None):
        self._blobs = [b for b in (blobs or []) if b]
        self.source = source
        self._pil = None

    def __bool__(self):
        return bool(self._blobs)

    def to_pil(self):
        if self._pil is not None:
            try:
                self._pil.load()
                return self._pil
            except Exception:
                try:
                    self._pil.close()
                except Exception:
                    pass
                self._pil = None
        res_img = None
        for blob in self._blobs:
            try:
                image = Image.open(BytesIO(blob)).convert("RGB")
            except Exception as e:
                logging.info("LazyImage: skip bad image blob: %s", e)
                continue

            if res_img is None:
                res_img = image
                continue

            new_img = _concat_pil_vertical(res_img, image)
            if new_img is not res_img:
                try:
                    res_img.close()
                except Exception:
                    pass
            try:
                image.close()
            except Exception:
                pass
            res_img = new_img

        self._pil = res_img
        return self._pil

    def to_pil_detached(self):
        pil = self.to_pil()
        self._pil = None
        return pil

    def close(self):
        if self._pil is not None:
            try:
                self._pil.close()
            except Exception:
                pass
            self._pil = None
        return None

    def __getattr__(self, name):
        pil = self.to_pil()
        if pil is None:
            raise AttributeError(name)
        return getattr(pil, name)

    def __array__(self, dtype=None):
        import numpy as np

        pil = self.to_pil()
        if pil is None:
            return np.array([], dtype=dtype)
        return np.array(pil, dtype=dtype)

    def __enter__(self):
        return self.to_pil()

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    @staticmethod
    def merge(a, b):
        """Merge two LazyImage instances by combining their blob lists."""
        a_blobs = a._blobs if isinstance(a, LazyImage) else []
        b_blobs = b._blobs if isinstance(b, LazyImage) else []
        combined = a_blobs + b_blobs
        if not combined:
            return None
        return LazyImage(combined)


LazyDocxImage = LazyImage


def ensure_pil_image(img):
    if isinstance(img, Image.Image):
        return img
    if isinstance(img, LazyImage):
        return img.to_pil()
    return None


def is_image_like(img):
    return isinstance(img, Image.Image) or isinstance(img, LazyImage)


def open_image_for_processing(img, *, allow_bytes=False):
    if isinstance(img, Image.Image):
        return img, False
    if isinstance(img, LazyImage):
        return img.to_pil_detached(), True
    if allow_bytes and isinstance(img, (bytes, bytearray)):
        try:
            pil = Image.open(BytesIO(img)).convert("RGB")
            return pil, True
        except Exception as e:
            logging.info("open_image_for_processing: bad bytes: %s", e)
            return None, False
    return img, False


def open_image(path: str, *args: Any, **kwargs: Any) -> Any:
    """Open image from path (legacy / minimal API)."""
    return Image.open(path, *args, **kwargs)
