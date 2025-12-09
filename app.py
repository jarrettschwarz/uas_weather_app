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
        return nws_cache[cache_key]
    
    try:
        url = f"https://api.weather.gov/points/{lat},{lon}"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            props = res.json().get('properties', {})
            data = {
                'forecast_url': props.get('forecast'),
                'forecast_hourly': props.get('forecastHourly'),
                'office': props.get('gridId', '').split('/')[0] if '/' in props.get('gridId', '') else 'Unknown'
            }
            nws_cache[cache_key] = data
            return data
    except Exception as e:
        logger.error(f"NWS grid error: {e}")
    
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
    """Fetch METAR for a station"""
    if not station_code:
        return None
    
    try:
        url = f"https://aviationweather.gov/api/data/metar?format=xml&ids={station_code}"
        res = requests.get(url, headers=HEADERS, timeout=5)
        root = ET.fromstring(res.content)
        metar = root.find(".//METAR")
        
        if metar is None:
            return None
        
        wind_kt = int(metar.find("wind_speed_kt").text or 0)
        wind_mph = round(wind_kt * 1.15078, 1)
        
        vis_el = metar.find("visibility_statute_mi")
        visibility = float(vis_el.text.replace('+', '')) if vis_el is not None and vis_el.text else 0.0
        
        cloud_base = 10000
        for cloud in metar.findall("sky_condition"):
            if cloud.get("sky_cover") in ["BKN", "OVC"]:
                try:
                    cloud_base = int(cloud.get("cloud_base_ft_agl"))
                    break
                except:
                    pass
        
        condition = metar.find("flight_category").text or "Unknown"
        
        return {
            "wind_mph": wind_mph,
            "visibility": visibility,
            "cloud_base": cloud_base,
            "condition": condition
        }
    except Exception as e:
        logger.error(f"METAR error for {station_code}: {e}")
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
            
            # Get METAR station
            metar_station, metar_name = find_metar_stations(lat, lon)
            
            logger.info(f"METAR: {metar_station} ({metar_name}), NWS: {nws_data}")
            
            # Fetch weather data
            source = "unavailable"
            cloud_base = 10000
            visibility = 0.0
            wind_mph = 0.0
            condition = "Unknown"
            
            # Try METAR if within 2 hours
            if delta <= timedelta(hours=2) and metar_station:
                metar_data = get_metar(metar_station)
                if metar_data:
                    cloud_base = metar_data["cloud_base"]
                    visibility = metar_data["visibility"]
                    wind_mph = metar_data["wind_mph"]
                    condition = metar_data["condition"]
                    source = "metar"
                    logger.info(f"Using METAR: {metar_station}")
            
            # Try NWS forecast if no METAR
            if source == "unavailable" and nws_data and nws_data.get('forecast_hourly'):
                periods = get_forecast(nws_data['forecast_hourly'])
                if periods:
                    closest = min(periods, key=lambda p: abs(isoparse(p["startTime"]) - selected_time))
                    wind_mph = parse_forecast_wind(closest.get("windSpeed", ""))
                    visibility = 10.0
                    cloud_base = 10000
                    condition = closest.get("shortForecast", "Unknown")
                    source = "forecast"
                    logger.info(f"Using NWS forecast: {condition}")
            
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
                "wind_metar": f"{wind_mph} mph" if source == "metar" else "N/A",
                "wind_forecast": f"{wind_mph} mph" if source != "metar" else "N/A",
                "condition": condition,
                "source": source,
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
                "taf_used": "N/A",
                "forecast_url_used": nws_data.get('forecast_hourly', 'N/A') if nws_data else "N/A"
            }
        
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            error = str(e)
    
    return render_template("index.html", result=result, selected_coords=selected_coords, flight_sites=list(FLIGHT_SITES.keys()) + ["Custom"], pytz=pytz, error=error)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)