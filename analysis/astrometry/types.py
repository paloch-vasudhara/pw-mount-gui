"""
analysis/astrometry/types.py  (ported from ssa-toolkit photometry/types.py, object mode)
===================
Data contract for the detection engine — the single source of truth shared by
the backend pipeline, the ``/api/astrometry/measure`` endpoint, and the TDM
Generator Detections modal.

A frame is reduced to a list of :class:`Detection` objects wrapped in a
:class:`DetectionResult`.  Each detection is EITHER a point (``x, y``) or a
streak (``xs, ys`` -> ``xe, ye``); which one the target vs the stars are follows
the tracking ``mode`` (object-track: stars streak, target is a point;
sidereal-track: the reverse).  See docs/TDM_generator_progress.txt.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

# Vocabulary — kept as module constants so callers don't hardcode strings.
ROLE_STAR = "star"
ROLE_TARGET = "target"
SHAPE_POINT = "point"
SHAPE_STREAK = "streak"
MODE_OBJECT = "object"
MODE_SIDEREAL = "sidereal"

BRT_MIN = 1
BRT_MAX = 5


@dataclass
class Detection:
    """One detected source.

    Geometry is populated according to ``shape``:
      * ``point``  -> ``x, y`` set; streak fields ``None``
      * ``streak`` -> ``xs, ys, xe, ye`` set; ``x, y`` ``None``

    ``brt`` is a quantised 1 (dimmest) .. 5 (brightest) relative brightness that
    mirrors the modal's ``brt.`` column; ``flux``/``snr`` carry the underlying
    photometric measures.  Endpoint start/end labelling is intentionally left
    ambiguous here — it is resolved downstream by propagation.
    """

    id: str
    role: str = ROLE_STAR         # ROLE_STAR | ROLE_TARGET
    shape: str = SHAPE_POINT      # SHAPE_POINT | SHAPE_STREAK

    # point geometry
    x: Optional[float] = None
    y: Optional[float] = None
    # streak geometry
    xs: Optional[float] = None
    ys: Optional[float] = None
    xe: Optional[float] = None
    ye: Optional[float] = None

    # photometry / morphology
    brt: int = BRT_MIN
    flux: Optional[float] = None
    snr: Optional[float] = None
    fwhm: Optional[float] = None
    length_px: Optional[float] = None
    angle_deg: Optional[float] = None

    # provenance for the manual-edit UX
    confidence: float = 0.0
    flags: list[str] = field(default_factory=list)

    def midpoint(self) -> Optional[tuple[float, float]]:
        """Centre of the feature: the point itself, or the streak's mid-point
        (the star position at mid-exposure — what the object-track plate solve
        uses)."""
        if self.shape == SHAPE_POINT:
            if self.x is None or self.y is None:
                return None
            return (self.x, self.y)
        if None in (self.xs, self.ys, self.xe, self.ye):
            return None
        return ((self.xs + self.xe) / 2.0, (self.ys + self.ye) / 2.0)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DetectionResult:
    """Full outcome of running the engine on one frame in one ``mode``.

    The base source extraction is mode-independent; ``mode`` only drives role
    assignment / target selection, so switching modes re-labels this list from
    the same cached sources rather than re-extracting (see engine, Phase 4).
    """

    mode: str = MODE_OBJECT
    image_dims: Optional[tuple[int, int]] = None  # (width, height)
    epoch: Optional[str] = None                    # capture epoch, ISO/UTC
    exposure_s: Optional[float] = None

    background_rms: Optional[float] = None
    dominant_streak: Optional[dict] = None         # {theta_deg, length_px, tol}

    n_stars: int = 0
    n_target_candidates: int = 0

    warnings: list[str] = field(default_factory=list)
    detections: list[Detection] = field(default_factory=list)

    @property
    def target(self) -> Optional[Detection]:
        """The selected target detection, if any."""
        for d in self.detections:
            if d.role == ROLE_TARGET:
                return d
        return None

    @property
    def stars(self) -> list[Detection]:
        return [d for d in self.detections if d.role == ROLE_STAR]

    def to_dict(self) -> dict:
        d = asdict(self)
        # asdict already recursed into detections; keep dims as a plain list for JSON
        if self.image_dims is not None:
            d["image_dims"] = list(self.image_dims)
        return d
