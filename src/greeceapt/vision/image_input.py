"""Deterministic decoding, resizing, hashing, and within-listing dedupe."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO
from typing import Sequence

import imagehash
from PIL import Image, ImageOps, UnidentifiedImageError


class InvalidImageError(ValueError):
    """Raised when image bytes cannot be decoded into a usable raster image."""


@dataclass(frozen=True)
class PreparedImage:
    jpeg_bytes: bytes
    sha256: str
    phash: str
    width: int
    height: int
    original_width: int
    original_height: int


def prepare_image(content: bytes, *, max_edge: int = 512) -> PreparedImage:
    """Decode and normalize an image without cropping or changing its aspect ratio."""
    if max_edge <= 0:
        raise ValueError("max_edge must be positive")

    try:
        with Image.open(BytesIO(content)) as opened:
            opened.load()
            oriented = ImageOps.exif_transpose(opened)
            original_width, original_height = oriented.size
            if original_width <= 0 or original_height <= 0:
                raise InvalidImageError("image has no usable pixels")
            prepared = oriented.convert("RGB")
    except InvalidImageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImageError("image bytes could not be decoded") from exc

    prepared.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    width, height = prepared.size
    output = BytesIO()
    prepared.save(
        output,
        format="JPEG",
        quality=88,
        optimize=False,
        progressive=False,
        subsampling=2,
    )
    jpeg_bytes = output.getvalue()
    return PreparedImage(
        jpeg_bytes=jpeg_bytes,
        sha256=hashlib.sha256(jpeg_bytes).hexdigest(),
        phash=str(imagehash.phash(prepared)),
        width=width,
        height=height,
        original_width=original_width,
        original_height=original_height,
    )


def dedupe_prepared(
    images: Sequence[PreparedImage],
    *,
    max_distance: int = 4,
) -> tuple[list[PreparedImage], dict[int, int]]:
    """Retain the first image in each perceptual cluster, preserving input order.

    The returned mapping contains duplicate input index -> first-seen input index.
    """
    if max_distance < 0:
        raise ValueError("max_distance must be non-negative")

    unique: list[PreparedImage] = []
    unique_input_indices: list[int] = []
    unique_hashes: list[imagehash.ImageHash] = []
    duplicate_of: dict[int, int] = {}

    for input_index, candidate in enumerate(images):
        candidate_hash = imagehash.hex_to_hash(candidate.phash)
        matched_index: int | None = None
        for position, existing_hash in enumerate(unique_hashes):
            if candidate_hash - existing_hash <= max_distance:
                matched_index = unique_input_indices[position]
                break
        if matched_index is not None:
            duplicate_of[input_index] = matched_index
            continue
        unique.append(candidate)
        unique_input_indices.append(input_index)
        unique_hashes.append(candidate_hash)

    return unique, duplicate_of
