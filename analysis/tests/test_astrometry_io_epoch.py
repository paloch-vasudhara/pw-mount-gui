# ruff: noqa: E402, E501, E702, E741, B905
"""EXIF capture-epoch + exposure extraction for raster images (photometry.io)."""
import numpy as np
from PIL import Image

from analysis.astrometry.io import _exif_epoch, _exif_exposure, load_image


def _write_jpeg(path, exif=None):
    arr = (np.random.default_rng(0).random((32, 32)) * 255).astype("uint8")
    im = Image.fromarray(arr, mode="L")
    im.save(str(path), format="JPEG", exif=exif) if exif is not None else im.save(str(path), format="JPEG")


def test_exif_datetimeoriginal_becomes_epoch(tmp_path):
    p = tmp_path / "shot.jpg"
    exif = Image.Exif()
    exif[36867] = "2025:01:14 19:10:30"        # DateTimeOriginal
    exif[37521] = "742"                         # SubsecTimeOriginal
    _write_jpeg(p, exif)
    img = load_image(str(p))
    assert img.epoch == "2025-01-14 19:10:30.742"
    assert not any("supply it manually" in w for w in img.warnings)


def test_exif_falls_back_to_datetime_tag(tmp_path):
    p = tmp_path / "shot.jpg"
    exif = Image.Exif()
    exif[306] = "2024:12:31 23:59:59"          # DateTime (no Original)
    _write_jpeg(p, exif)
    assert load_image(str(p)).epoch == "2024-12-31 23:59:59"


def test_no_exif_epoch_leaves_none_and_warns(tmp_path):
    p = tmp_path / "plain.jpg"
    _write_jpeg(p, None)
    img = load_image(str(p))
    assert img.epoch is None
    assert any("supply it manually" in w for w in img.warnings)


def test_exif_epoch_helper_handles_missing_getexif():
    assert _exif_epoch(object()) is None


def test_exif_in_sub_ifd_is_read(tmp_path):
    """Real cameras keep ExposureTime + DateTimeOriginal in the Exif sub-IFD
    (0x8769), not the main IFD — both must still be found."""
    p = tmp_path / "camera.jpg"
    exif = Image.Exif()
    exif[0x8769] = {
        33434: 10.0,                    # ExposureTime
        36867: "2025:01:14 19:10:30",   # DateTimeOriginal
        37521: "742",                   # SubsecTimeOriginal
    }
    _write_jpeg(p, exif)
    img = load_image(str(p))
    assert img.exposure_s == 10.0
    assert img.epoch == "2025-01-14 19:10:30.742"


def test_exif_exposure_main_ifd(tmp_path):
    p = tmp_path / "exp.jpg"
    exif = Image.Exif()
    exif[33434] = 0.5                    # ExposureTime on the main IFD
    _write_jpeg(p, exif)
    assert _exif_exposure(Image.open(str(p))) == 0.5


def test_exif_exposure_helper_handles_missing_getexif():
    assert _exif_exposure(object()) is None
