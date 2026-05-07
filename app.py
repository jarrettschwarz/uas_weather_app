"""UAS Weather Check - Part 107 Go/No-Go Decision Tool"""
from flask import Flask, render_template, request
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime, timedelta, timezone
import pytz
import re
import logging
from dateutil.parser import isoparse
from math import radians, cos, sin, sqrt, atan2
import csv
import os
import time

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)


def to_dms(deg, axis="lat"):
    """Convert decimal degrees to a DMS string with correct hemisphere.

    axis="lat" -> N/S
    axis="lon" -> E/W
    """
    if deg is None:
        return ""
    abs_deg = abs(deg)
    d = int(abs_deg)
    m_float = (abs_deg - d) * 60
    m = int(m_float)
    s = (m_float - m) * 60
    if axis == "lat":
        hemi = "N" if deg >= 0 else "S"
    else:
        hemi = "E" if deg >= 0 else "W"
    return f"{d}°{m}'{s:.1f}\"{hemi}"


app.jinja_env.filters["dms_lat"] = lambda d: to_dms(d, "lat")
app.jinja_env.filters["dms_lon"] = lambda d: to_dms(d, "lon")

# ---------------------------------------------------------------------------
# Configuration (env-overridable)
# ---------------------------------------------------------------------------
HEADERS = {"User-Agent": "UAS-Weather-Check (contact: ops@example.com)"}

MAX_WIND_MPH = float(os.environ.get("MAX_WIND_MPH", 15.7))
MIN_VISIBILITY_SM = float(os.environ.get("MIN_VISIBILITY_SM", 3.0))
MIN_CLOUD_BASE_FT = float(os.environ.get("MIN_CLOUD_BASE_FT", 500))
BAD_CONDITIONS = ["rain", "snow", "fog", "thunderstorm", "mist", "ts", "fzra"]

REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", 8))
NWS_CACHE_TTL = int(os.environ.get("NWS_CACHE_TTL", 3600))  # 1 hour

# Preset flight sites - VERIFY these coordinates against your actual sites!
# NOTE: UAFS longitude has been inconsistent across versions. The two prior
# values were -96.835504 (near Tulsa) and -98.836239 (western OK). Confirm
# the correct one for your actual site before relying on it.
FLIGHT_SITES = {
    "UAFS": {"lat": 36.162101, "lon": -96.835504},
    "CENFEX": {"lat": 36.357214, "lon": -96.861901},
    "Legion Field": {"lat": 34.723543, "lon": -98.387076},
    "SkyWay36": {"lat": 36.210521, "lon": -96.008673},
}

SITE_METAR_MAP = {
    "UAFS": "KSWO",
    "CENFEX": "KSWO",
    "Legion Field": "KLAW",
    "SkyWay36": "KRVS",
}

# ---------------------------------------------------------------------------
# Resilient HTTP session with retries
# ---------------------------------------------------------------------------
def _build_session():
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    return session


http = _build_session()


def safe_get(url, **kwargs):
    """GET with timeout, retries, and exception logging. Returns Response or None."""
    timeout = kwargs.pop("timeout", REQUEST_TIMEOUT)
    try:
        res = http.get(url, timeout=timeout, **kwargs)
        return res
    except requests.RequestException as e:
        logger.warning(f"HTTP request failed for {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Local fallback station data (loaded from CSV if present)
# ---------------------------------------------------------------------------
def _load_csv(path, mapper):
    if not os.path.exists(path):
        logger.info(f"Optional CSV not found: {path}")
        return []
    try:
        with open(path, newline="") as f:
            return [mapper(row) for row in csv.DictReader(f)]
    except (OSError, KeyError, ValueError) as e:
        logger.error(f"Failed to load {path}: {e}")
        return []


METAR_STATIONS = _load_csv(
    "metar_stations_oklahoma.csv",
    lambda r: (r["icao"], float(r["lat"]), float(r["lon"])),
)
TAF_STATIONS = _load_csv(
    "taf_stations_oklahoma.csv",
    lambda r: (r["icao"], float(r["lat"]), float(r["lon"])),
)


def haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance in km."""
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def nearest_from_csv(lat, lon, stations):
    if not stations:
        return None
    closest = min(stations, key=lambda s: haversine(lat, lon, s[1], s[2]))
    return closest[0]


# ---------------------------------------------------------------------------
# DMS parsing
# ---------------------------------------------------------------------------
def dms_to_decimal(deg, min_, sec, direction):
    try:
        if deg in (None, ""):
            return None
        decimal = float(deg) + float(min_ or 0) / 60 + float(sec or 0) / 3600
        if direction in ("S", "W"):
            decimal *= -1
        return decimal
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# NWS grid lookup with TTL cache
# ---------------------------------------------------------------------------
_nws_cache = {}  # key -> (timestamp, data)


def get_nws_grid(lat, lon):
    cache_key = f"{lat:.2f},{lon:.2f}"
    now = time.time()
    cached = _nws_cache.get(cache_key)
    if cached and (now - cached[0]) < NWS_CACHE_TTL:
        return cached[1]

    # Bound cache size
    if len(_nws_cache) > 200:
        _nws_cache.clear()

    res = safe_get(f"https://api.weather.gov/points/{lat},{lon}")
    if res is None or res.status_code != 200:
        logger.warning(f"NWS grid lookup failed for {lat},{lon}")
        return None

    try:
        props = res.json().get("properties", {})
    except ValueError as e:
        logger.warning(f"NWS grid JSON parse error: {e}")
        return None

    grid_id = props.get("gridId", "")
    data = {
        "forecast_url": props.get("forecast"),
        "forecast_hourly": props.get("forecastHourly"),
        "office": grid_id,
    }
    _nws_cache[cache_key] = (now, data)
    return data


# ---------------------------------------------------------------------------
# METAR / TAF station discovery
# ---------------------------------------------------------------------------
def find_metar_station(lat, lon):
    """Find nearest METAR-reporting station. Tries avwx.rest, falls back to local CSV."""
    res = safe_get(f"https://avwx.rest/api/station/search?lat={lat}&lon={lon}&n=5")
    if res is not None and res.status_code == 200:
        try:
            stations = res.json().get("results", [])
            for station in stations:
                if station.get("type") in ("airport", "reporting_station"):
                    icao = station.get("icao")
                    if icao:
                        return icao, station.get("name", "Unknown")
        except ValueError as e:
            logger.warning(f"avwx.rest JSON parse error: {e}")

    # Fallback: local CSV
    icao = nearest_from_csv(lat, lon, METAR_STATIONS)
    if icao:
        logger.info(f"METAR station from local CSV: {icao}")
        return icao, "Local lookup"
    return None, None


def find_taf_station(lat, lon):
    """Find nearest TAF-issuing station from local CSV."""
    icao = nearest_from_csv(lat, lon, TAF_STATIONS)
    if icao:
        return icao
    return None


# ---------------------------------------------------------------------------
# METAR / TAF fetching (JSON API)
# ---------------------------------------------------------------------------
def _parse_visibility(vis_raw):
    """Parse visibility values like '10+', '6', '1 1/2', etc."""
    if vis_raw is None:
        return 10.0
    if isinstance(vis_raw, (int, float)):
        return float(vis_raw)
    s = str(vis_raw).replace("+", "").strip()
    # Handle fractional like "1 1/2"
    if " " in s and "/" in s:
        whole, frac = s.split(" ", 1)
        try:
            num, den = frac.split("/")
            return float(whole) + float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            pass
    if "/" in s:
        try:
            num, den = s.split("/")
            return float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            pass
    try:
        return float(s)
    except ValueError:
        return 10.0


def _extract_cloud_base(clouds):
    """Find lowest BKN/OVC layer in a clouds list."""
    if not clouds:
        return 10000
    lowest = 10000
    for layer in clouds:
        cover = (layer.get("cover") or "").upper()
        base = layer.get("base")
        if cover in ("BKN", "OVC") and base is not None:
            try:
                base_ft = int(base) * 100  # JSON API reports in 100s of feet
                if base_ft < lowest:
                    lowest = base_ft
            except (ValueError, TypeError):
                continue
    return lowest


def get_metar(station):
    """Fetch current METAR using JSON API. Returns dict or None."""
    if not station:
        return None
    url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json&taf=false"
    res = safe_get(url)
    if res is None or res.status_code != 200:
        logger.warning(f"METAR {station}: HTTP {getattr(res, 'status_code', 'N/A')}")
        return None
    try:
        data = res.json()
    except ValueError as e:
        logger.warning(f"METAR {station}: JSON parse error: {e}")
        return None
    if not data:
        logger.warning(f"METAR {station}: empty response")
        return None

    m = data[0] if isinstance(data, list) else data
    try:
        wind_kt = float(m.get("wspd") or 0)
        wind_mph = round(wind_kt * 1.15078, 1)
        visibility = _parse_visibility(m.get("visib"))
        cloud_base = _extract_cloud_base(m.get("clouds") or [])
        condition = m.get("wxString") or m.get("rawOb", "").split()[-1] or "VFR"

        # If wxString is empty, use flight category
        if not m.get("wxString"):
            condition = m.get("fltCat") or "VFR"

        logger.info(
            f"METAR {station}: wind={wind_mph}mph vis={visibility}sm "
            f"cloud={cloud_base}ft cond={condition}"
        )
        return {
            "wind_mph": wind_mph,
            "visibility": visibility,
            "cloud_base": cloud_base,
            "condition": condition,
        }
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        logger.warning(f"METAR {station} parse error: {e}")
        return None


def get_taf(station, target_time=None):
    """Fetch TAF using JSON API. Returns dict for the period covering target_time, or first period."""
    if not station:
        return None
    url = f"https://aviationweather.gov/api/data/taf?ids={station}&format=json"
    res = safe_get(url)
    if res is None or res.status_code != 200:
        logger.warning(f"TAF {station}: HTTP {getattr(res, 'status_code', 'N/A')}")
        return None
    try:
        data = res.json()
    except ValueError as e:
        logger.warning(f"TAF {station}: JSON parse error: {e}")
        return None
    if not data:
        return None

    taf = data[0] if isinstance(data, list) else data
    forecasts = taf.get("fcsts") or []
    if not forecasts:
        return None

    # Pick forecast period containing target_time, else first
    chosen = forecasts[0]
    if target_time is not None:
        target_epoch = int(target_time.timestamp())
        for f in forecasts:
            start = f.get("timeFrom")
            end = f.get("timeTo")
            if start and end and start <= target_epoch <= end:
                chosen = f
                break

    try:
        wind_kt = float(chosen.get("wspd") or 0)
        wind_mph = round(wind_kt * 1.15078, 1)
        visibility = _parse_visibility(chosen.get("visib"))
        cloud_base = _extract_cloud_base(chosen.get("clouds") or [])
        condition = chosen.get("wxString") or "VFR"

        logger.info(
            f"TAF {station}: wind={wind_mph}mph vis={visibility}sm "
            f"cloud={cloud_base}ft cond={condition}"
        )
        return {
            "wind_mph": wind_mph,
            "visibility": visibility,
            "cloud_base": cloud_base,
            "condition": condition,
        }
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        logger.warning(f"TAF {station} parse error: {e}")
        return None


# ---------------------------------------------------------------------------
# NWS forecast (no visibility/ceiling — caller must mark unknown)
# ---------------------------------------------------------------------------
def get_nws_forecast(forecast_url):
    res = safe_get(forecast_url)
    if res is None or res.status_code != 200:
        return []
    try:
        return res.json().get("properties", {}).get("periods", [])
    except ValueError:
        return []


def parse_forecast_wind(wind_str):
    """Parse '10 mph' or '10 to 15 mph'. Returns the upper bound for safety."""
    if not wind_str:
        return 0.0
    matches = re.findall(r"(\d+)", wind_str)
    if not matches:
        return 0.0
    speed = float(matches[-1])  # upper bound is more conservative
    if "knot" in wind_str.lower() or " kt" in wind_str.lower():
        speed = round(speed * 1.15078, 1)
    return speed


# ---------------------------------------------------------------------------
# Sunrise/Sunset
# ---------------------------------------------------------------------------
def get_sunrise_sunset(lat, lon, date_str):
    url = f"https://api.sunrise-sunset.org/json?lat={lat}&lng={lon}&date={date_str}&formatted=0"
    res = safe_get(url)
    if res is None or res.status_code != 200:
        return None, None
    try:
        data = res.json()
    except ValueError:
        return None, None
    if data.get("status") != "OK":
        return None, None
    try:
        sunrise = datetime.fromisoformat(data["results"]["sunrise"]).replace(tzinfo=timezone.utc)
        sunset = datetime.fromisoformat(data["results"]["sunset"]).replace(tzinfo=timezone.utc)
        return sunrise, sunset
    except (KeyError, ValueError):
        return None, None


# ---------------------------------------------------------------------------
# Form validation
# ---------------------------------------------------------------------------
def validate_form(form):
    """Return (lat, lon, location_name, naive_dt, error_msg)."""
    site = form.get("site", "")
    date_str = form.get("flight_date", "")
    time_str = form.get("flight_time", "")

    if not date_str or not time_str:
        return None, None, None, None, "Date and time are required."

    try:
        naive_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None, None, None, None, "Invalid date/time format."

    # Sanity bound: within ±2 years
    now = datetime.now()
    if abs((naive_time - now).days) > 730:
        return None, None, None, None, "Flight date out of reasonable range (±2 years)."

    if site in FLIGHT_SITES:
        lat = FLIGHT_SITES[site]["lat"]
        lon = FLIGHT_SITES[site]["lon"]
        return lat, lon, site, naive_time, None

    if site == "Custom":
        coord_format = form.get("coordFormat", "decimal")
        if coord_format == "decimal":
            lat_str = (form.get("lat_decimal") or "").strip()
            lon_str = (form.get("lon_decimal") or "").strip()
            if not lat_str or not lon_str:
                return None, None, None, None, "Latitude and longitude required."
            try:
                lat = float(lat_str)
                lon = float(lon_str)
            except ValueError:
                return None, None, None, None, "Coordinates must be numeric."
        else:
            lat = dms_to_decimal(
                form.get("lat_deg"), form.get("lat_min"),
                form.get("lat_sec"), form.get("lat_dir", "N"),
            )
            lon = dms_to_decimal(
                form.get("lon_deg"), form.get("lon_min"),
                form.get("lon_sec"), form.get("lon_dir", "W"),
            )
            if lat is None or lon is None:
                return None, None, None, None, "Invalid DMS coordinates."

        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            return None, None, None, None, "Coordinates out of range."

        return lat, lon, f"{lat:.4f}, {lon:.4f}", naive_time, None

    return None, None, None, None, "Invalid site selection."


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    selected_coords = None
    error = None
    flight_sites_list = list(FLIGHT_SITES.keys()) + ["Custom"]

    if request.method == "POST":
        lat, lon, location_name, naive_time, error = validate_form(request.form)
        if error:
            return render_template(
                "index.html",
                result=None, selected_coords=None,
                flight_sites=flight_sites_list, pytz=pytz, error=error,
            )

        site = request.form.get("site", "Custom")
        selected_coords = {"lat": lat, "lon": lon}

        central = pytz.timezone("America/Chicago")
        selected_time = central.localize(naive_time).astimezone(pytz.utc)
        delta = selected_time - datetime.now(timezone.utc)
        logger.info(f"Query: ({lat:.4f},{lon:.4f}) at {naive_time}, delta={delta}")

        # Resolve stations
        nws_data = get_nws_grid(lat, lon)
        if site in SITE_METAR_MAP:
            metar_station = SITE_METAR_MAP[site]
            metar_name = site
        else:
            metar_station, metar_name = find_metar_station(lat, lon)
        taf_station = metar_station or find_taf_station(lat, lon)

        # Decide source by time horizon
        source = "unavailable"
        cloud_base = None
        visibility = None
        wind_mph = None
        condition = "Unknown"
        data_caveat = None

        metar_window = delta <= timedelta(hours=2)
        taf_window = delta <= timedelta(hours=30)

        if metar_station and metar_window:
            metar_data = get_metar(metar_station)
            if metar_data:
                cloud_base = metar_data["cloud_base"]
                visibility = metar_data["visibility"]
                wind_mph = metar_data["wind_mph"]
                condition = metar_data["condition"]
                source = "metar"

        if source == "unavailable" and taf_station and taf_window:
            taf_data = get_taf(taf_station, target_time=selected_time)
            if taf_data:
                cloud_base = taf_data["cloud_base"]
                visibility = taf_data["visibility"]
                wind_mph = taf_data["wind_mph"]
                condition = taf_data["condition"]
                source = "taf"

        if source == "unavailable" and nws_data and nws_data.get("forecast_hourly"):
            periods = get_nws_forecast(nws_data["forecast_hourly"])
            if periods:
                closest = min(
                    periods,
                    key=lambda p: abs(isoparse(p["startTime"]) - selected_time),
                )
                wind_mph = parse_forecast_wind(closest.get("windSpeed", ""))
                condition = closest.get("shortForecast", "Unknown")
                # NWS hourly does not provide visibility or ceiling
                visibility = None
                cloud_base = None
                source = "forecast"
                data_caveat = (
                    "NWS hourly forecast does not include visibility or cloud base. "
                    "Pilot must visually verify these before flight."
                )

        # Sunrise/sunset and time-of-day check
        sunrise, sunset = get_sunrise_sunset(lat, lon, request.form.get("flight_date"))
        flight_time_remaining = None
        failed_reasons = []

        if sunrise and sunset:
            if selected_time < sunrise:
                minutes = int((sunrise - selected_time).total_seconds() / 60)
                failed_reasons.append(f"{minutes} minutes before sunrise")
            elif selected_time > sunset:
                minutes = int((selected_time - sunset).total_seconds() / 60)
                failed_reasons.append(f"{minutes} minutes after sunset")
            else:
                rem = sunset - selected_time
                hours = rem.seconds // 3600
                mins = (rem.seconds % 3600) // 60
                flight_time_remaining = f"{hours} hours {mins} minutes"

        # Weather checks - treat None as "unknown" rather than auto-pass
        wind_pass = wind_mph is not None and wind_mph <= MAX_WIND_MPH
        vis_pass = visibility is not None and visibility >= MIN_VISIBILITY_SM
        cloud_pass = cloud_base is not None and cloud_base >= MIN_CLOUD_BASE_FT
        cond_pass = not any(t in (condition or "").lower() for t in BAD_CONDITIONS)

        if wind_mph is None:
            failed_reasons.append("Wind data unavailable")
        elif wind_mph > MAX_WIND_MPH:
            failed_reasons.append(f"Wind {wind_mph} mph > {MAX_WIND_MPH} mph limit")

        if visibility is None:
            if source == "forecast":
                # Don't fail go/no-go on this alone, but warn
                pass
            else:
                failed_reasons.append("Visibility data unavailable")
        elif visibility < MIN_VISIBILITY_SM:
            failed_reasons.append(f"Visibility {visibility} sm < {MIN_VISIBILITY_SM} sm")

        if cloud_base is None:
            if source == "forecast":
                pass
            else:
                failed_reasons.append("Cloud base data unavailable")
        elif cloud_base < MIN_CLOUD_BASE_FT:
            failed_reasons.append(f"Cloud base {cloud_base} ft < {MIN_CLOUD_BASE_FT} ft")

        if not cond_pass:
            failed_reasons.append(f"Adverse conditions: {condition}")

        if source == "unavailable":
            failed_reasons.append("No weather data could be retrieved")

        result = {
            "site": location_name,
            "datetime_cst": naive_time.strftime("%m/%d/%Y at %I:%M %p CST"),
            "cloud_base": cloud_base,
            "cloud_label": (
                f"{cloud_base} ft" if cloud_base and cloud_base < 10000
                else "Clear" if cloud_base is not None
                else "Unknown"
            ),
            "visibility": (
                f"{visibility:.1f} statute miles" if visibility is not None
                else "Unknown"
            ),
            "wind_mph": wind_mph,
            "wind_display": f"{wind_mph} mph" if wind_mph is not None else "Unknown",
            "wind_metar": f"{wind_mph} mph" if source == "metar" and wind_mph is not None else "N/A",
            "wind_forecast": f"{wind_mph} mph" if source != "metar" and wind_mph is not None else "N/A",
            "forecast": condition,
            "source": source,
            "data_caveat": data_caveat,
            "wind_pass": wind_pass,
            "visibility_pass": vis_pass,
            "cloud_pass": cloud_pass,
            "condition_pass": cond_pass,
            "go_nogo": "Go" if not failed_reasons else "No-Go",
            "failed_reasons": failed_reasons,
            "sunrise": sunrise.astimezone(central).strftime("%I:%M %p CST") if sunrise else "N/A",
            "sunset": sunset.astimezone(central).strftime("%I:%M %p CST") if sunset else "N/A",
            "flight_time_remaining": flight_time_remaining,
            "station_used": (
                f"{metar_station} ({metar_name})" if metar_station else "N/A"
            ),
            "taf_used": taf_station if source == "taf" else "N/A",
            "forecast_url_used": (
                nws_data.get("forecast_hourly") if nws_data else "N/A"
            ),
            "thresholds": {
                "max_wind": MAX_WIND_MPH,
                "min_vis": MIN_VISIBILITY_SM,
                "min_cloud": MIN_CLOUD_BASE_FT,
            },
        }

    return render_template(
        "index.html",
        result=result,
        selected_coords=selected_coords,
        flight_sites=flight_sites_list,
        pytz=pytz,
        error=error,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV", "production").lower() == "development"
    if debug:
        logger.warning("Running in DEBUG mode - not for production")
    app.run(host="0.0.0.0", port=port, debug=debug)