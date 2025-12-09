from flask import Flask, render_template, request, jsonify
import requests
from datetime import datetime, timedelta, timezone
import pytz
import xml.etree.ElementTree as ET
import re
import logging
from dateutil.parser import isoparse
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.jinja_env.filters['dms'] = lambda deg: f"{int(abs(deg))}°{int((abs(deg)%1)*60)}'{((abs(deg)*3600)%60):.1f}\"{'N' if deg>=0 else 'S'}" if deg else ""

HEADERS = {"User-Agent": "UAS-Weather-Check"}
MAX_WIND_MPH = 15.7
MIN_VISIBILITY_SM = 3.0
MIN_CLOUD_BASE_FT = 500
BAD_CONDITIONS = ["rain", "snow", "fog", "thunderstorm", "mist"]

# Preset flight sites
FLIGHT_SITES = {
    "UAFS": {"lat": 36.162101, "lon": -96.835504},
    "CENFEX": {"lat": 36.357214, "lon": -96.861901},
    "Legion Field": {"lat": 34.723543, "lon": -98.387076},
    "SkyWay36": {"lat": 36.210521, "lon": -96.008673}
}

# Nearest METAR stations for preset sites
SITE_METAR_MAP = {
    "UAFS": "KSWO",
    "CENFEX": "KSWO",
    "Legion Field": "KLAW",
    "SkyWay36": "KRVS"
}

# Cache for NWS points to avoid excessive API calls
nws_cache = {}

def dms_to_decimal(deg, min_, sec, direction):
    """Convert DMS to decimal degrees"""
    try:
        if not deg or deg == '':
            return None
        decimal = float(deg) + float(min_ or 0) / 60 + float(sec or 0) / 3600
        if direction in ['S', 'W']:
            decimal *= -1
        return decimal
    except (ValueError, TypeError):
        return None

def get_nws_grid(lat, lon):
    """Get NWS grid point for coordinates"""
    cache_key = f"{lat:.2f},{lon:.2f}"
    if cache_key in nws_cache:
        logger.debug(f"NWS grid from cache: {cache_key}")
        return nws_cache[cache_key]
    
    try:
        url = f"https://api.weather.gov/points/{lat},{lon}"
        res = requests.get(url, headers=HEADERS, timeout=5)
        logger.info(f"NWS grid API: HTTP {res.status_code}")
        
        if res.status_code == 200:
            props = res.json().get('properties', {})
            grid_id = props.get('gridId', '')
            office = grid_id.split('/')[0] if '/' in grid_id else 'Unknown'
            
            data = {
                'forecast_url': props.get('forecast'),
                'forecast_hourly': props.get('forecastHourly'),
                'office': office
            }
            logger.info(f"NWS grid found: Office={office}, GridID={grid_id}")
            nws_cache[cache_key] = data
            return data
        else:
            logger.warning(f"NWS grid API error: HTTP {res.status_code}")
    except Exception as e:
        logger.error(f"NWS grid error: {e}", exc_info=True)
    
    logger.warning(f"NWS grid lookup failed for {lat},{lon}")
    return None

def find_metar_stations(lat, lon):
    """Query AVWX API to find nearest METAR station"""
    try:
        # Aviation weather API to get station info
        url = f"https://avwx.rest/api/station/search?lat={lat}&lon={lon}&limit=5"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            stations = res.json().get('results', [])
            if stations:
                # Return closest station with METAR
                for station in stations:
                    if station.get('type') in ['airport', 'reporting_station']:
                        return station.get('icao'), station.get('name', 'Unknown')
    except:
        pass
    
    return None, None

def get_metar(station_code):
    """Fetch METAR for a station using FAA aviationweather.gov (no auth required)"""
    if not station_code:
        logger.warning("No station code provided for METAR")
        return None
    
    try:
        url = f"https://aviationweather.gov/api/data/metar?format=xml&ids={station_code}"
        res = requests.get(url, headers=HEADERS, timeout=5)
        logger.info(f"METAR {station_code}: HTTP {res.status_code}")
        
        if res.status_code != 200:
            logger.warning(f"METAR API error: HTTP {res.status_code}")
            return None
        
        try:
            root = ET.fromstring(res.content)
        except ET.ParseError as e:
            logger.warning(f"METAR {station_code}: XML parse error: {e}")
            return None
        
        # Find METAR element
        metar = root.find(".//METAR")
        if metar is None:
            logger.warning(f"METAR {station_code}: No METAR element in response")
            return None
        
        logger.debug(f"METAR {station_code}: Found METAR element")
        
        # Extract wind speed (in knots, convert to mph)
        wind_mph = 0.0
        try:
            wind_el = metar.find("wind_speed_kt")
            if wind_el is not None and wind_el.text:
                wind_kt = float(wind_el.text)
                wind_mph = round(wind_kt * 1.15078, 1)
                logger.debug(f"METAR {station_code} wind: {wind_kt}kt = {wind_mph}mph")
        except Exception as e:
            logger.warning(f"METAR {station_code} wind parse error: {e}")
            wind_mph = 0.0
        
        # Extract visibility (in statute miles)
        visibility = 10.0
        try:
            vis_el = metar.find("visibility_statute_mi")
            if vis_el is not None and vis_el.text:
                vis_str = vis_el.text.replace('+', '').strip()
                visibility = float(vis_str)
                logger.debug(f"METAR {station_code} visibility: {visibility}sm")
        except Exception as e:
            logger.warning(f"METAR {station_code} visibility parse error: {e}")
            visibility = 10.0
        
        # Extract cloud base (first BKN or OVC layer)
        cloud_base = 10000
        try:
            for cloud in metar.findall("sky_condition"):
                coverage = cloud.get("sky_cover", "")
                if coverage in ["BKN", "OVC"]:
                    base_el = cloud.get("cloud_base_ft_agl")
                    if base_el:
                        cloud_base = int(base_el)
                        logger.debug(f"METAR {station_code} cloud base: {cloud_base}ft ({coverage})")
                        break
        except Exception as e:
            logger.warning(f"METAR {station_code} cloud parse error: {e}")
            cloud_base = 10000
        
        # Extract flight category
        condition = "UNKN"
        try:
            cat_el = metar.find("flight_category")
            if cat_el is not None and cat_el.text:
                condition = cat_el.text
                logger.debug(f"METAR {station_code} category: {condition}")
        except Exception as e:
            logger.warning(f"METAR {station_code} category parse error: {e}")
            condition = "UNKN"
        
        # Return if we have valid data
        if condition and condition != "UNKN":
            logger.info(f"✓ METAR {station_code}: Wind={wind_mph}mph, Vis={visibility}sm, Cat={condition}")
            return {
                "wind_mph": wind_mph,
                "visibility": visibility,
                "cloud_base": cloud_base,
                "condition": condition
            }
        else:
            logger.warning(f"METAR {station_code}: Invalid condition data")
            return None
            
    except Exception as e:
        logger.error(f"METAR {station_code} exception: {e}", exc_info=True)
        return None


def get_taf(station_code):
    """Fetch TAF for a station using FAA aviationweather.gov (no auth required)"""
    if not station_code:
        logger.warning("No station code provided for TAF")
        return None
    
    try:
        url = f"https://aviationweather.gov/api/data/taf?format=xml&ids={station_code}"
        res = requests.get(url, headers=HEADERS, timeout=5)
        logger.info(f"TAF {station_code}: HTTP {res.status_code}")
        
        if res.status_code != 200:
            logger.warning(f"TAF API error: HTTP {res.status_code}")
            return None
        
        try:
            root = ET.fromstring(res.content)
        except ET.ParseError as e:
            logger.warning(f"TAF {station_code}: XML parse error: {e}")
            return None
        
        # Find TAF element
        taf = root.find(".//TAF")
        if taf is None:
            logger.warning(f"TAF {station_code}: No TAF element in response")
            return None
        
        logger.debug(f"TAF {station_code}: Found TAF element")
        
        # Get first forecast period
        forecast = taf.find("forecast")
        if forecast is None:
            logger.warning(f"TAF {station_code}: No forecast period")
            return None
        
        logger.debug(f"TAF {station_code}: Found forecast period")
        
        # Extract wind (in knots, convert to mph)
        wind_mph = 0.0
        try:
            wind_el = forecast.find("wind_speed_kt")
            if wind_el is not None and wind_el.text:
                wind_kt = float(wind_el.text)
                wind_mph = round(wind_kt * 1.15078, 1)
                logger.debug(f"TAF {station_code} wind: {wind_kt}kt = {wind_mph}mph")
        except Exception as e:
            logger.warning(f"TAF {station_code} wind parse error: {e}")
            wind_mph = 0.0
        
        # Extract visibility
        visibility = 10.0
        try:
            vis_el = forecast.find("visibility_statute_mi")
            if vis_el is not None and vis_el.text:
                vis_str = vis_el.text.replace('+', '').strip()
                visibility = float(vis_str)
                logger.debug(f"TAF {station_code} visibility: {visibility}sm")
        except Exception as e:
            logger.warning(f"TAF {station_code} visibility parse error: {e}")
            visibility = 10.0
        
        # Extract cloud base
        cloud_base = 10000
        try:
            for cloud in forecast.findall("sky_condition"):
                coverage = cloud.get("sky_cover", "")
                if coverage in ["BKN", "OVC"]:
                    base_el = cloud.get("cloud_base_ft_agl")
                    if base_el:
                        cloud_base = int(base_el)
                        logger.debug(f"TAF {station_code} cloud base: {cloud_base}ft ({coverage})")
                        break
        except Exception as e:
            logger.warning(f"TAF {station_code} cloud parse error: {e}")
            cloud_base = 10000
        
        # Extract weather description
        condition = "VFR"
        try:
            wx_el = forecast.find("wx_string")
            if wx_el is not None and wx_el.text:
                condition = wx_el.text
                logger.debug(f"TAF {station_code} weather: {condition}")
        except Exception as e:
            logger.warning(f"TAF {station_code} weather parse error: {e}")
            condition = "VFR"
        
        logger.info(f"✓ TAF {station_code}: Wind={wind_mph}mph, Vis={visibility}sm, Cloud={cloud_base}ft")
        
        return {
            "wind_mph": wind_mph,
            "visibility": visibility,
            "cloud_base": cloud_base,
            "condition": condition
        }
        
    except Exception as e:
        logger.error(f"TAF {station_code} exception: {e}", exc_info=True)
        return None

def get_forecast(forecast_url):
    """Fetch NWS hourly forecast"""
    try:
        res = requests.get(forecast_url, headers=HEADERS, timeout=5)
        if res.status_code == 200:
            return res.json().get('properties', {}).get('periods', [])
    except Exception as e:
        logger.error(f"Forecast error: {e}")
    
    return []

def parse_forecast_wind(wind_str):
    """Extract wind speed from forecast description"""
    match = re.search(r'(\d+)\s*(?:mph|mi/h|knots|kt)', wind_str or '', re.IGNORECASE)
    if match:
        speed = float(match.group(1))
        # If it looks like knots, convert to mph
        if 'knot' in wind_str.lower() or 'kt' in wind_str.lower():
            speed = round(speed * 1.15078, 1)
        return speed
    return 0.0

def get_sunrise_sunset(lat, lon, date_str):
    """Get sunrise/sunset times"""
    try:
        url = f"https://api.sunrise-sunset.org/json?lat={lat}&lng={lon}&date={date_str}&formatted=0"
        res = requests.get(url, timeout=5)
        data = res.json()
        
        if data.get('status') == 'OK':
            sunrise = datetime.fromisoformat(data['results']['sunrise']).replace(tzinfo=timezone.utc)
            sunset = datetime.fromisoformat(data['results']['sunset']).replace(tzinfo=timezone.utc)
            return sunrise, sunset
    except Exception as e:
        logger.error(f"Sunrise/sunset error: {e}")
    
    return None, None

@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    selected_coords = None
    error = None
    
    if request.method == "POST":
        try:
            site = request.form.get("site", "Custom")
            date_str = request.form.get("flight_date", "")
            time_str = request.form.get("flight_time", "")
            
            # Parse coordinates
            if site in FLIGHT_SITES:
                lat = FLIGHT_SITES[site]["lat"]
                lon = FLIGHT_SITES[site]["lon"]
                location_name = site
            elif site == "Custom":
                coord_format = request.form.get("coordFormat", "decimal")
                
                if coord_format == "decimal":
                    lat = float(request.form.get("lat_decimal", "").strip())
                    lon = float(request.form.get("lon_decimal", "").strip())
                else:
                    lat = dms_to_decimal(
                        request.form.get("lat_deg"),
                        request.form.get("lat_min"),
                        request.form.get("lat_sec"),
                        request.form.get("lat_dir", "N")
                    )
                    lon = dms_to_decimal(
                        request.form.get("lon_deg"),
                        request.form.get("lon_min"),
                        request.form.get("lon_sec"),
                        request.form.get("lon_dir", "W")
                    )
                
                if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                    error = "Invalid coordinates"
                    return render_template("index.html", result=None, selected_coords=None, flight_sites=list(FLIGHT_SITES.keys()) + ["Custom"], pytz=pytz, error=error)
                
                location_name = f"{lat:.4f}, {lon:.4f}"
            else:
                error = "Invalid site selection"
                return render_template("index.html", result=None, selected_coords=None, flight_sites=list(FLIGHT_SITES.keys()) + ["Custom"], pytz=pytz, error=error)
            
            selected_coords = {"lat": lat, "lon": lon}
            
            # Parse datetime
            central = pytz.timezone("America/Chicago")
            naive_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            selected_time = central.localize(naive_time).astimezone(pytz.utc)
            delta = selected_time - datetime.now(timezone.utc)
            
            logger.info(f"Query: Lat={lat}, Lon={lon}, Time delta={delta}")
            
            # Get location name
            location_name = f"{lat:.4f}, {lon:.4f}"
            
            # Get NWS grid
            nws_data = get_nws_grid(lat, lon)
            
            # Get METAR station (use preset map if available, otherwise find nearest)
            if site in SITE_METAR_MAP:
                metar_station = SITE_METAR_MAP[site]
                metar_name = site
                logger.info(f"🔵 Site: {site} → METAR station (preset): {metar_station}")
            else:
                metar_station, metar_name = find_metar_stations(lat, lon)
                logger.info(f"🔵 Site: Custom → METAR station (API lookup): {metar_station} ({metar_name})")
            
            logger.info(f"🔵 Coordinates: {lat:.4f}, {lon:.4f}")
            logger.info(f"🔵 Flight time: {naive_time.strftime('%Y-%m-%d %H:%M %Z')}")
            logger.info(f"🔵 Time delta: {delta}")
            logger.info(f"🔵 NWS Grid: {nws_data.get('office') if nws_data else 'None'}")
            
            # Fetch weather data with priority: METAR -> TAF -> NWS
            source = "unavailable"
            cloud_base = 10000
            visibility = 0.0
            wind_mph = 0.0
            condition = "Unknown"
            taf_station = metar_station
            
            logger.info(f"Starting weather data fetch. METAR station: {metar_station}, TAF station: {taf_station}")
            
            # 1. Try METAR first - but only if within 2 hours (METAR is current conditions)
            metar_valid_for_future = delta <= timedelta(hours=2)
            if metar_station and metar_valid_for_future:
                logger.info(f"[1/3] Attempting METAR from {metar_station}... (within 2hr window)")
                metar_data = get_metar(metar_station)
                if metar_data:
                    cloud_base = metar_data["cloud_base"]
                    visibility = metar_data["visibility"]
                    wind_mph = metar_data["wind_mph"]
                    condition = metar_data["condition"]
                    source = "metar"
                    logger.info(f"[✓] SUCCESS: Using METAR from {metar_station}")
                else:
                    logger.info(f"[✗] FAILED: METAR from {metar_station} returned None, moving to TAF")
            elif metar_station and not metar_valid_for_future:
                logger.info(f"[1/3] SKIPPED: METAR query outside 2-hour window (delta: {delta}), using TAF for future time")
            else:
                logger.warning("[✗] SKIPPED: No METAR station available")
            
            # 2. Try TAF if METAR failed or was invalid for future time
            # TAF is valid for approximately 30 hours
            taf_valid_for_future = delta <= timedelta(hours=30)
            if source == "unavailable" and taf_station and taf_valid_for_future:
                logger.info(f"[2/3] Attempting TAF from {taf_station}... (within 30hr window)")
                taf_data = get_taf(taf_station)
                if taf_data:
                    cloud_base = taf_data["cloud_base"]
                    visibility = taf_data["visibility"]
                    wind_mph = taf_data["wind_mph"]
                    condition = taf_data["condition"]
                    source = "taf"
                    logger.info(f"[✓] SUCCESS: Using TAF from {taf_station}")
                else:
                    logger.info(f"[✗] FAILED: TAF from {taf_station} returned None, moving to NWS")
            elif source == "unavailable" and taf_station and not taf_valid_for_future:
                logger.info(f"[2/3] SKIPPED: TAF query outside 30-hour window (delta: {delta}), using NWS for extended forecast")
            elif source != "unavailable":
                logger.info(f"[2/3] SKIPPED: Already have {source} data")
            else:
                logger.warning("[✗] SKIPPED: No TAF station available")
            
            # 3. Try NWS forecast if METAR and TAF failed
            if source == "unavailable":
                if nws_data and nws_data.get('forecast_hourly'):
                    logger.info(f"[3/3] Attempting NWS forecast from {nws_data.get('office', 'unknown')}...")
                    periods = get_forecast(nws_data['forecast_hourly'])
                    if periods:
                        logger.info(f"[3/3] NWS returned {len(periods)} forecast periods")
                        closest = min(periods, key=lambda p: abs(isoparse(p["startTime"]) - selected_time))
                        wind_mph = parse_forecast_wind(closest.get("windSpeed", ""))
                        visibility = 10.0
                        cloud_base = 10000
                        condition = closest.get("shortForecast", "Unknown")
                        source = "forecast"
                        logger.info(f"[✓] SUCCESS: Using NWS forecast")
                    else:
                        logger.warning("[✗] NWS forecast returned empty periods")
                else:
                    logger.warning("[✗] SKIPPED: No NWS data available")
            
            # Get sunrise/sunset
            sunrise, sunset = get_sunrise_sunset(lat, lon, date_str)
            
            # Evaluate go/no-go
            failed_reasons = []
            flight_time_remaining = None
            
            if sunrise and sunset:
                if selected_time < sunrise:
                    minutes = int((sunrise - selected_time).total_seconds() / 60)
                    failed_reasons.append(f"Operation is {minutes} minutes before sunrise")
                elif selected_time > sunset:
                    minutes = int((selected_time - sunset).total_seconds() / 60)
                    failed_reasons.append(f"Operation is {minutes} minutes after sunset")
                else:
                    remaining = sunset - selected_time
                    hours = remaining.seconds // 3600
                    minutes = (remaining.seconds % 3600) // 60
                    flight_time_remaining = f"{hours} hours {minutes} minutes"
            
            if wind_mph > MAX_WIND_MPH:
                failed_reasons.append(f"Wind {wind_mph} mph (limit: {MAX_WIND_MPH} mph)")
            if visibility < MIN_VISIBILITY_SM:
                failed_reasons.append(f"Visibility {visibility} sm (minimum: {MIN_VISIBILITY_SM} sm)")
            if cloud_base < MIN_CLOUD_BASE_FT:
                failed_reasons.append(f"Cloud base {cloud_base} ft (minimum: {MIN_CLOUD_BASE_FT} ft)")
            if any(term in condition.lower() for term in BAD_CONDITIONS):
                failed_reasons.append(f"Adverse conditions: {condition}")
            
            result = {
                "site": location_name,
                "datetime_cst": naive_time.strftime("%m/%d/%Y at %I:%M %p CST"),
                "cloud_base": cloud_base,
                "cloud_label": f"{cloud_base} ft" if cloud_base < 10000 else "Clear",
                "visibility": f"{visibility:.1f} statute miles",
                "wind_mph": wind_mph,
                "wind_metar": f"{wind_mph} mph" if source == "metar" else "N/A",
                "wind_forecast": f"{wind_mph} mph" if source != "metar" else "N/A",
                "condition": condition,
                "source": "NWS" if source == "forecast" else source.upper(),
                "wind_pass": wind_mph <= MAX_WIND_MPH,
                "visibility_pass": visibility >= MIN_VISIBILITY_SM,
                "cloud_pass": cloud_base >= MIN_CLOUD_BASE_FT,
                "condition_pass": not any(term in condition.lower() for term in BAD_CONDITIONS),
                "go_nogo": "Go" if not failed_reasons else "No-Go",
                "failed_reasons": failed_reasons,
                "sunrise": sunrise.astimezone(central).strftime('%I:%M %p CST') if sunrise else "N/A",
                "sunset": sunset.astimezone(central).strftime('%I:%M %p CST') if sunset else "N/A",
                "flight_time_remaining": flight_time_remaining,
                "station_used": f"{metar_station} ({metar_name})" if metar_station else "N/A",
                "taf_used": taf_station if source == "taf" else "N/A",
                "forecast_url_used": nws_data.get('forecast_hourly', 'N/A') if nws_data else "N/A",
                "metar_taf_link": f"https://metar-taf.com/{metar_station}" if metar_station else ""
            }
        
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            error = str(e)
    
    return render_template("index.html", result=result, selected_coords=selected_coords, flight_sites=list(FLIGHT_SITES.keys()) + ["Custom"], pytz=pytz, error=error)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)