import io

import pytest
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from conftest import EX, ROOT
from mathocr.pipeline import PipelineConfig, run
from mathocr.stages.photo_quality import PhotoRejected, check

ORIG = ImageOps.exif_transpose(Image.open(EX / "copie_p1.webp")).convert("RGB")


def _save(tmp_path, img, name):
    p = tmp_path / f"{name}.png"
    img.save(p)
    return p


def test_example_photo_passes():
    q = check(EX / "copie_p1.webp")
    assert q.ok, q.problems


@pytest.mark.parametrize("name,make,word", [
    ("flou", lambda im: im.filter(ImageFilter.GaussianBlur(3)), "floue"),
    ("sombre", lambda im: ImageEnhance.Brightness(im).enhance(0.25), "sombre"),
    ("surexpose", lambda im: ImageEnhance.Brightness(im).enhance(2.5), "surexposée"),
    ("petite", lambda im: im.resize((300, 400)), "résolution"),
    ("blanche", lambda im: Image.new("RGB", im.size, (235, 235, 230)), "aucune écriture"),
])
def test_bad_photos_rejected(tmp_path, name, make, word):
    q = check(_save(tmp_path, make(ORIG), name))
    assert not q.ok and any(word in p for p in q.problems), q


def test_mild_blur_and_heavy_jpeg_still_accepted(tmp_path):
    assert check(_save(tmp_path, ORIG.filter(ImageFilter.GaussianBlur(1.0)), "leger")).ok
    b = io.BytesIO()
    ORIG.save(b, "JPEG", quality=10)
    p = tmp_path / "q10.jpg"
    p.write_bytes(b.getvalue())
    assert check(p).ok


def test_pipeline_rejects_before_any_paid_call(tmp_path):
    bad = _save(tmp_path, ORIG.filter(ImageFilter.GaussianBlur(4)), "flou")
    cfg = PipelineConfig(workspace=ROOT / "lean_workspace", ocr_engines=["anthropic:claude-opus-5-5"],
                         cache_dir=tmp_path / "c", memory_dir=None)
    with pytest.raises(PhotoRejected):
        run(EX / "exercice.json", [bad], tmp_path / "out", cfg)
