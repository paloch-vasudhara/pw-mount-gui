"""MountSnapshot schema and builder (PWI4 status -> clean snapshot with G8 state)."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from .ephem import SunMoon, angular_sep_deg
from .pwi4_io import PollResult
from .settings import SUN_KEEPOUT_DEG, TOL_ALT_M, TOL_LAT_LON_DEG
from .state import compute_state


class Pwi4Info(BaseModel):
    reachable: bool
    version: str | None = None
    latency_ms: float | None = None
    age_ms: float | None = None  # time since the last successful /status


class Site(BaseModel):
    lat: float | None = None
    lon: float | None = None
    height_m: float | None = None
    source: str = "pwi4"
    timezone: str | None = None  # Settings IANA zone; null = PC zone (client side)


class SiteTolerance(BaseModel):
    lat_lon_deg: float = TOL_LAT_LON_DEG
    alt_m: float = TOL_ALT_M


class SiteCheck(BaseModel):
    pwi4_reachable: bool = False
    ok: bool | None = None
    lat_diff_deg: float | None = None
    lon_diff_deg: float | None = None
    alt_diff_m: float | None = None
    tolerance: SiteTolerance = Field(default_factory=SiteTolerance)


class Limits(BaseModel):
    lim_el_deg: float | None = None
    sun_keepout_deg: float = SUN_KEEPOUT_DEG


class TimeInfo(BaseModel):
    jd: float | None = None
    mjd: float | None = None
    lmst_hours: float | None = None


class MountInfo(BaseModel):
    connected: bool = False
    az: float | None = None
    alt: float | None = None
    ra_j2000_h: float | None = None
    dec_j2000_deg: float | None = None
    ra_apparent_h: float | None = None
    dec_apparent_deg: float | None = None
    target_ra_apparent_h: float | None = None
    target_dec_apparent_deg: float | None = None
    is_slewing_raw: bool | None = None
    is_tracking_raw: bool | None = None
    axis0_wrap_min_deg: float | None = None
    distance_to_sun_deg_pwi4: float | None = None


class AxisInfo(BaseModel):
    enabled: bool = False
    position_deg: float | None = None
    velocity_deg_s: float | None = None  # derived from positions (P12)
    rms_arcsec: float | None = None
    dist_to_target_arcsec: float | None = None
    min_deg: float | None = None
    max_deg: float | None = None
    max_vel_deg_s: float | None = None
    accel_deg_s2: float | None = None


class AltAz(BaseModel):
    alt: float | None = None
    az: float | None = None


class MoonInfo(AltAz):
    illumination: float | None = None


class SequenceInfo(BaseModel):
    """Progress of a connect / disconnect / home sequence (T3.2). Kept after it ends (done /
    failed / cancelled) until the next sequence starts, so every window can show the outcome."""

    name: str  # connect | disconnect | home
    step: str | None = None  # human label of the current (or failing) step
    step_index: int = -1
    steps: list[str] = Field(default_factory=list)
    status: str = "running"  # running | done | failed | cancelled
    error: str | None = None  # "<step> failed: <reason>" / the cancel reason
    warning: str | None = None  # e.g. "Home skipped: path passes within 15° of the Sun"


class HomedInfo(BaseModel):
    """Not-homed guard (T3.3, Q3/Q4) for the current connect session. The whole field is null
    when the mount is not connected. `since` = UTC time the current state began."""

    state: str  # homed | not_homed | overridden
    since: str  # ISO 8601 UTC
    # T3.3b: "stalled" = the stall guard stopped the mount (physical limit): only a completed
    # home clears it, no override. null = the normal not-homed / homed / overridden states.
    reason: str | None = None


class JogInfo(BaseModel):
    """A running jog (T3.4c). `origin` = how the mount was when the jog began: "stopped" (the
    gateway turned tracking on for it) or "tracking" (a rate offset only). Informational only:
    a jog always ends STOPPED."""

    axis: int
    dir: int
    speed: float  # deg/s of the mount axis
    origin: str


class StepInfo(BaseModel):
    axis: int
    dir: int
    size_arcmin: int


class JogLimitInfo(BaseModel):
    """T3.4e: an azimuth jog stopped (or was refused) at the axis-0 mechanical limit. `dir` = the
    direction of the jog, `limit_deg` the mechanical limit, `turn_deg` the size of the wrap turn
    that would continue the move, `est_s` the estimated duration of that turn."""

    axis: int
    dir: int
    limit_deg: float
    turn_deg: float
    est_s: float


class TurnInfo(BaseModel):
    """T3.4e: a 360 deg wrap turn is running (axis-0 positions)."""

    from_deg: float
    to_deg: float


class RouteInfo(BaseModel):
    """T3.5c: a D-SUN detour route is flying (the mount avoids the Sun disc)."""

    active: bool = True
    sides: list[str] = Field(default_factory=list)  # "cw" / "ccw" per detour, [] = straight line
    t_end: str  # UTC ISO time the route (incl. the settle wait) ends


class TrackDetourInfo(BaseModel):
    t_a: float  # Unix seconds
    t_b: float
    side: str  # "cw" / "ccw"


class TrackEndedInfo(BaseModel):
    norad: int
    reason: str
    at: float  # Unix seconds


class TrackInfo(BaseModel):
    """T4.2c: the object track (null = none). Times are Unix seconds (floats). Only `ended` is set
    for 30 s after a track ended (the other fields are null then)."""

    norad: int | None = None
    name: str | None = None
    phase: str | None = None  # slewing | waiting | tracking | detour
    t_r: float | None = None  # the rendezvous time
    t_end: float | None = None  # the Lim EL exit (null: unknown yet, a never-setting object)
    t_peak: float | None = None
    peak_alt: float | None = None
    detours: list[TrackDetourInfo] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    ended: TrackEndedInfo | None = None


class MountSnapshot(BaseModel):
    seq: int
    ts_utc: str
    state: str
    state_reason: str | None = None
    pwi4: Pwi4Info
    site: Site = Field(default_factory=Site)
    time: TimeInfo = Field(default_factory=TimeInfo)
    mount: MountInfo = Field(default_factory=MountInfo)
    axes: list[AxisInfo]
    sun: AltAz = Field(default_factory=AltAz)
    moon: MoonInfo = Field(default_factory=MoonInfo)
    sun_distance_deg: float | None = None
    hour_angle_h: float | None = None
    limits: Limits = Field(default_factory=Limits)
    site_check: SiteCheck | None = None  # only when the site comes from Settings
    sequence: SequenceInfo | None = None
    homed: HomedInfo | None = None
    jog: JogInfo | None = None  # T3.4c: null = no jog running
    step: StepInfo | None = None  # T3.4c: null = no step running
    jog_limit: JogLimitInfo | None = None  # T3.4e: null = no azimuth limit prompt
    turn: TurnInfo | None = None  # T3.4e: null = no wrap turn running
    route: RouteInfo | None = None  # T3.5c: null = no D-SUN route running
    track: TrackInfo | None = None  # T4.2c: null = no object track (and none ended lately)


def site_key(result: PollResult) -> tuple[float, float, float] | None:
    if not result.reachable:
        return None
    s = result.status.site
    if s.latitude_degs is None or s.longitude_degs is None:
        return None
    return (s.latitude_degs, s.longitude_degs, s.height_meters or 0.0)


def _wrap12(x: float) -> float:
    return (x + 12.0) % 24.0 - 12.0


def jd_from_utc(when: datetime) -> float:
    return when.timestamp() / 86400.0 + 2440587.5


def lmst_hours_from(jd: float, lon_deg_east: float) -> float:
    """Local mean sidereal time (hours) from JD (UTC ~ UT1) via the IAU 1982 GMST polynomial."""
    t = (jd - 2451545.0) / 36525.0
    gmst = (
        280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * t * t - t**3 / 38710000.0
    )
    return ((gmst + lon_deg_east) % 360.0) / 15.0


def _time_info(
    when: datetime,
    lon: float | None,
    pwi4_jd: float | None,
    pwi4_lmst: float | None,
    use_pwi4: bool,
) -> TimeInfo:
    """Always valid: our own clock; PWI4's values only if connected and in agreement."""
    jd = jd_from_utc(when)
    lmst = None if lon is None else lmst_hours_from(jd, lon)
    if use_pwi4:
        if pwi4_jd and abs(pwi4_jd - jd) * 86400.0 <= 1.0:
            jd = pwi4_jd
        if pwi4_lmst is not None and lmst is not None and abs(_wrap12(pwi4_lmst - lmst)) <= 0.01:
            lmst = pwi4_lmst
    return TimeInfo(jd=jd, mjd=jd - 2400000.5, lmst_hours=lmst)


def build_snapshot(
    *,
    seq: int,
    result: PollResult,
    vel: tuple[float, float],
    moving: bool,
    sunmoon: SunMoon | None,
    age_ms: float | None = None,
    down_reason: str | None = None,
    now_utc: datetime | None = None,
    site: Site | None = None,
    site_check: SiteCheck | None = None,
    limits: Limits | None = None,
    sequence: SequenceInfo | None = None,
    homed: HomedInfo | None = None,
    jog: JogInfo | None = None,
    step: StepInfo | None = None,
    jog_limit: JogLimitInfo | None = None,
    turn: TurnInfo | None = None,
    route: RouteInfo | None = None,
    track: TrackInfo | None = None,
) -> MountSnapshot:
    """result = last good fetch (or the failed one). down_reason forces DISCONNECTED.

    site: the Settings location (source "settings"); when given it replaces PWI4's site
    everywhere (snapshot.site, LMST), so Sun/Moon/time work with PWI4 down.
    """
    when = now_utc or datetime.now(UTC)
    ts = when.isoformat(timespec="milliseconds")
    if not result.reachable or down_reason is not None:
        down_site = site
        if down_site is None:
            down_site = Site()
            if result.reachable:  # stale: keep the last known site
                down_site = Site(
                    lat=result.status.site.latitude_degs,
                    lon=result.status.site.longitude_degs,
                    height_m=result.status.site.height_meters,
                )
        kw = {"unreachable_reason": down_reason} if down_reason else {}
        state, reason = compute_state(
            reachable=False,
            mount_connected=None,
            axis_enabled=(None, None),
            slewing_raw=None,
            tracking_raw=None,
            moving=False,
            **kw,
        )
        return MountSnapshot(
            seq=seq,
            ts_utc=ts,
            state=state,
            state_reason=reason,
            pwi4=Pwi4Info(reachable=False, latency_ms=result.latency_ms, age_ms=age_ms),
            site=down_site,
            time=_time_info(when, down_site.lon, None, None, False),
            axes=[AxisInfo(), AxisInfo()],
            sun=AltAz(alt=sunmoon.sun_alt, az=sunmoon.sun_az) if sunmoon else AltAz(),
            moon=MoonInfo(
                alt=sunmoon.moon_alt, az=sunmoon.moon_az, illumination=sunmoon.moon_illumination
            )
            if sunmoon
            else MoonInfo(),
            limits=limits or Limits(),
            site_check=site_check,
            sequence=sequence,
        )

    st = result.status
    m = st.mount
    v0, v1 = vel
    connected = bool(m.is_connected)
    state, reason = compute_state(
        reachable=True,
        mount_connected=m.is_connected,
        axis_enabled=(m.axis0.is_enabled, m.axis1.is_enabled),
        slewing_raw=m.is_slewing,
        tracking_raw=m.is_tracking,
        moving=moving,
    )

    axes = []
    for ax, v in ((m.axis0, v0), (m.axis1, v1)):
        axes.append(
            AxisInfo(
                enabled=bool(ax.is_enabled),
                position_deg=ax.position_degs if connected else None,
                velocity_deg_s=v if connected else None,
                rms_arcsec=ax.rms_error_arcsec if connected else None,
                dist_to_target_arcsec=ax.dist_to_target_arcsec if connected else None,
                min_deg=ax.min_mech_position_degs,
                max_deg=ax.max_mech_position_degs,
                max_vel_deg_s=ax.max_velocity_degs_per_sec,
                accel_deg_s2=ax.acceleration_degs_per_sec_sqr,
            )
        )

    site_lon = site.lon if site is not None else st.site.longitude_degs
    time_info = _time_info(when, site_lon, m.julian_date, st.site.lmst_hours, connected)
    ha = None
    if connected and time_info.lmst_hours is not None and m.ra_apparent_hours is not None:
        ha = _wrap12(time_info.lmst_hours - m.ra_apparent_hours)

    sun = AltAz()
    moon = MoonInfo()
    sep = None
    if sunmoon is not None:
        sun = AltAz(alt=sunmoon.sun_alt, az=sunmoon.sun_az)
        moon = MoonInfo(
            alt=sunmoon.moon_alt, az=sunmoon.moon_az, illumination=sunmoon.moon_illumination
        )
        if connected and m.azimuth_degs is not None and m.altitude_degs is not None:
            sep = angular_sep_deg(m.azimuth_degs, m.altitude_degs, sunmoon.sun_az, sunmoon.sun_alt)

    return MountSnapshot(
        seq=seq,
        ts_utc=ts,
        state=state,
        state_reason=reason,
        pwi4=Pwi4Info(
            reachable=True, version=st.pwi4.version, latency_ms=result.latency_ms, age_ms=age_ms
        ),
        site=site
        or Site(
            lat=st.site.latitude_degs,
            lon=st.site.longitude_degs,
            height_m=st.site.height_meters,
            source="pwi4",
        ),
        time=time_info,
        mount=MountInfo(
            connected=connected,
            az=m.azimuth_degs if connected else None,
            alt=m.altitude_degs if connected else None,
            ra_j2000_h=m.ra_j2000_hours if connected else None,
            dec_j2000_deg=m.dec_j2000_degs if connected else None,
            ra_apparent_h=m.ra_apparent_hours if connected else None,
            dec_apparent_deg=m.dec_apparent_degs if connected else None,
            target_ra_apparent_h=m.target_ra_apparent_hours if connected else None,
            target_dec_apparent_deg=m.target_dec_apparent_degs if connected else None,
            is_slewing_raw=m.is_slewing,
            is_tracking_raw=m.is_tracking,
            axis0_wrap_min_deg=m.axis0_wrap_range_min_degs,
            distance_to_sun_deg_pwi4=m.distance_to_sun_degs if connected else None,
        ),
        axes=axes,
        sun=sun,
        moon=moon,
        sun_distance_deg=sep,
        hour_angle_h=ha,
        limits=limits or Limits(),
        site_check=site_check,
        sequence=sequence,
        homed=homed if connected else None,
        jog=jog if connected else None,
        step=step if connected else None,
        jog_limit=jog_limit if connected else None,
        turn=turn if connected else None,
        route=route if connected else None,
        track=track if connected else None,
    )
