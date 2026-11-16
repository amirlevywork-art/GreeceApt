"""Deterministic image preparation and within-listing dedupe tests."""

from io import BytesIO

import pytest
from PIL import Image

from greeceapt.vision.image_input import (
    InvalidImageError,
    dedupe_prepared,
    prepare_image,
)


def _encoded_image(
    size: tuple[int, int],
    *,
    color: tuple[int, int, int] = (120, 80, 40),
    quality: int = 95,
    orientation: int | None = None,
) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = BytesIO()
    exif = Image.Exif()
    if orientation is not None:
        exif[274] = orientation
    image.save(buffer, format="JPEG", quality=quality, exif=exif)
    return buffer.getvalue()


def _patterned_image(*, quality: int = 95, vertical: bool = True) -> bytes:
    image = Image.new("RGB", (640, 480), (230, 220, 190))
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            band = x if vertical else y
            if (band // 80) % 2:
                pixels[x, y] = (35, 80, 145)
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def test_exif_orientation_is_applied_before_resize():
    prepared = prepare_image(_encoded_image((40, 20), orientation=6))

    assert (prepared.original_width, prepared.original_height) == (20, 40)
    assert (prepared.width, prepared.height) == (20, 40)


def test_longest_edge_is_512_and_aspect_ratio_is_preserved():
    prepared = prepare_image(_encoded_image((1000, 333)))

    assert prepared.width == 512
    assert abs(prepared.height - (333 * 512 / 1000)) <= 1


def test_rgb_output_is_deterministic():
    content = _encoded_image((640, 480), color=(20, 90, 180))

    first = prepare_image(content)
    second = prepare_image(content)

    assert first.jpeg_bytes == second.jpeg_bytes
    assert first.sha256 == second.sha256
    with Image.open(BytesIO(first.jpeg_bytes)) as image:
        assert image.mode == "RGB"


def test_corrupt_bytes_raise_invalid_image_error():
    with pytest.raises(InvalidImageError):
        prepare_image(b"not an image")


def test_recompressed_visual_duplicate_is_deduplicated_first_seen():
    first = prepare_image(_patterned_image(quality=95, vertical=True))
    recompressed = prepare_image(_patterned_image(quality=65, vertical=True))
    distinct = prepare_image(_patterned_image(quality=95, vertical=False))

    unique, duplicate_of = dedupe_prepared([first, recompressed, distinct])

    assert unique == [first, distinct]
    assert duplicate_of == {1: 0}


def test_max_edge_must_be_positive():
    with pytest.raises(ValueError):
        prepare_image(_encoded_image((20, 20)), max_edge=0)
