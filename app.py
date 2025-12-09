from flask import Flask, render_template, request, jsonify
import requests
from datetime import datetime, timedelta, timezone
import pytz
import xml.etree.ElementTree as ET
import re
import logging
from dateutil.parser import isoparse
from math import radians, cos, sin, sqrt, atan2
import csv
import os

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Utility: convert decimal degrees to DMS string
app.jinja_env.filters['dms'] = lambda deg: f"{int(abs(deg))}°{int((abs(deg)%1)*60)}'{((abs(deg)*3600)%60):.1f}\"{'N' if deg>=0 else 'S'}" if deg else ""

# Constants
HEADERS = {"User-Agent": "UAS-Weather-Check"}
MAX_WIND_MPH = 15.7
MIN_VISIBILITY_SM = 3.0
MIN_CLOUD_BASE_FT = 500
BAD_CONDITIONS = ["rain", "snow", "fog", "thunderstorm", "mist"]
METAR_CUTOFF_HOURS = 2


class StationDatabase:
    """Dynamically load and query station data from CSVs"""
    
    def __init__(self):
        self.metar_stations = []
        self.taf_stations = []
        self.nws_points = []
        self._load_stations()
    
    def _load_stations(self):
        """Load all station data from CSV files"""
        try:
            with open("metar_stations_oklahoma.csv", newline='') as f:
                reader = csv.DictReader(f)
                self.metar_stations = [
                    (row["icao"], float(row["lat"]), float(row["lon"])) 
                    for row in reader
                ]
            logger.info(f"Loaded {len(self.metar_stations)} METAR stations")
        except Exception as e:
            logger.error(f"Failed to load METAR stations: {e}")
        
        try:
            with open("taf_stations_oklahoma.csv", newline='') as f:
                reader = csv.DictReader(f)
                self.taf_stations = [
                    (row["icao"], float(row["lat"]), float(row["lon"])) 
                    for row in reader
                ]
            logger.info(f"Loaded {len(self.taf_stations)} TAF stations")
        except Exception as e:
            logger.error(f"Failed to load TAF stations: {e}")
        
        try:
            with open("nws_gridpoints_oklahoma.csv", newline='') as f:
                reader = csv.DictReader(f)
                self.nws_points = [
                    {
                        "office": row["office"],
                        "location": row["location"],
                        "lat": float(row["lat"]),
                        "lon": float(row["lon"]),
                        "gridX": int(row["gridX"]),
                        "gridY": int(row["gridY"])
                    }
                    for row in reader
                ]
            logger.info(f"Loaded {len(self.nws_points)} NWS grid points")
        except Exception as e:
            logger.error(f"Failed to load NWS grid points: {e}")
    
    def find_nearest_metar(self, lat, lon):
        """Find nearest METAR station by distance"""
        if not self.metar_stations:
            logger.warning("No METAR stations available")
            return None
        
        closest = min(self.metar_stations, key=lambda s: self._haversine(lat, lon, s[1], s[2]))
        distance = self._haversine(lat, lon, closest[1], closest[2])
        logger.info(f"Nearest METAR: {closest[0]} ({distance:.1f} km away)")
        return closest[0]
    
    def find_nearest_taf(self, lat, lon):
        """Find nearest TAF station by distance"""
        if not self.taf_stations:
            logger.warning("No TAF stations available")
            return None
        
        closest = min(self.taf_stations, key=lambda s: self._haversine(lat, lon, s[1], s[2]))
        distance = self._haversine(lat, lon, closest[1], closest[2])
        logger.info(f"Nearest TAF: {closest[0]} ({distance:.1f} km away)")
        return closest[0]
    
    def find_nws_forecast_url(self, lat, lon):
        """Find closest NWS grid point and build forecast URL"""
        if not self.nws_points:
            logger.warning("No NWS grid points available")
            return None, None
        
        closest = min(self.nws_points, key=lambda p: self._haversine(lat, lon, p["lat"], p["lon"]))
        distance = self._haversine(lat, lon, closest["lat"], closest["lon"])
        url = f"https://api.weather.gov/gridpoints/{closest['office']}/{closest['gridX']},{closest['gridY']}/forecast/hourly"
        logger.info(f"NWS Forecast: {closest['office']} grid ({closest['gridX']},{closest['gridY']}) - {distance:.1f} km away")
        return url, closest["office"]
    
    @staticmethod
    def _haversine(lat1, lon1, lat2, lon2):
        """Calculate distance in km between two lat/lon coordinates"""
        R = 6371.0
        lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
        dlat, dlon = lat2 - lat1, lon2 - lon1
        a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
        return R * 2 * atan2(sqrt(a), sqrt(1 - a))


# Initialize database
db = StationDatabase()


def dms_to_decimal(deg, min_, sec, direction):
    """Convert DMS format to decimal degrees"""
    try:
        if not deg or deg == '':
            return None
        decimal = float(deg) + float(min_ or 0) / 60 + float(sec or 0) / 3600
        if direction in ['S', 'W']:
            decimal *= -1
        return decimal
    except (ValueError, TypeError) as e:
        logger.error(f"DMS conversion error: {e}")
        return None


class FlightSites:
    """Manage preset flight sites - can be extended or modified"""
    
    SITES = {
        "UAFS": {"lat": 36.162101, "lon": -96.835504},
        "CENFEX": {"lat": 36.357214, "lon": -96.861901},
        "Legion Field": {"lat": 34.723543, "lon": -98.387076},
        "SkyWay36": {"lat": 36.210521, "lon": -96.008673}
    }
    
    @classmethod
    def get_all_sites(cls):
        """Return list of available site names"""
        return list(cls.SITES.keys()) + ["Custom"]
    
    @classmethod
    def get_coordinates(cls, site_name):
        """Get coordinates for a site, or None if not found"""
        if site_name in cls.SITES:
            return cls.SITES[site_name]["lat"], cls.SITES[site_name]["lon"]
        return None, None


def parse_coordinates(site, request_form):
    """Parse coordinates from user input (preset or custom)"""
    try:
        # Try preset site first
        lat, lon = FlightSites.get_coordinates(site)
        if lat is not None:
            logger.info(f"Using preset site {site}: {lat}, {lon}")
            return lat, lon
        
        # Otherwise parse custom input
        if site == "Custom":
            format_type = request_form.get("coordFormat", "decimal")
            
            if format_type == "decimal":
                lat = float(request_form.get("lat_decimal", "").strip())
                lon = float(request_form.get("lon_decimal", "").strip())
            else:  # DMS
                lat = dms_to_decimal(
                    request_form.get("lat_deg"), 
                    request_form.get("lat_min"), 
                    request_form.get("lat_sec"), 
                    request_form.get("lat_dir", "N")
                )
                lon = dms_to_decimal(
                    request_form.get("lon_deg"), 
                    request_form.get("lon_min"), 
                    request_form.get("lon_sec"), 
                    request_form.get("lon_dir", "W")
                )
                if lat is None or lon is None:
                    raise ValueError("Invalid DMS values")
            
            # Validate ranges
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("Coordinates out of range")
            
            logger.info(f"Custom coordinates: {lat}, {lon}")
            return lat, lon
        else:
            logger.error(f"Unknown site type: {site}")
            return None, None
    except Exception as e:
        logger.error(f"Coordinate parsing error: {e}")
        return None, None


def get_sunrise_sunset(lat, lon, date):
    """Fetch sunrise/sunset times for given coordinates and date"""
    try:
        url = f"https://api.sunrise-sunset.org/json?lat={lat}&lng={lon}&date={date}&formatted=0"
        res = requests.get(url, timeout=5)
        res.raise_for_status()
        data = res.json()
        
        if data.get("status") != "OK":
            logger.warning(f"Sunrise/sunset API error: {data.get('status')}")
            return None, None
        
        results = data["results"]
        sunrise = datetime.fromisoformat(results["sunrise"]).replace(tzinfo=timezone.utc)
        sunset = datetime.fromisoformat(results["sunset"]).replace(tzinfo=timezone.utc)
        logger.info(f"Sunrise: {sunrise}, Sunset: {sunset}")
        return sunrise, sunset
    except Exception as e:
        logger.error(f"Sunrise/sunset error: {e}")
        return None, None


def get_metar_conditions(station):
    """Fetch and parse METAR data for given station"""
    try:
        url = f"https://aviationweather.gov/api/data/metar?format=xml&ids={station}"
        res = requests.get(url, headers=HEADERS, timeout=5)
        res.raise_for_status()
        root = ET.fromstring(res.content)
        metar = root.find(".//METAR")
        
        if metar is None:
            logger.warning(f"No METAR data found for {station}")
            return None
        
        # Wind speed
        wind_el = metar.find("wind_speed_kt")
        wind_kt = int(wind_el.text) if wind_el is not None and wind_el.text else 0
        wind_mph = round(wind_kt * 1.15078, 1)
        
        # Visibility
        vis_el = metar.find("visibility_statute_mi")
        if vis_el is not None and vis_el.text:
            visibility = float(vis_el.text.replace('+', ''))
        else:
            visibility = 0.0
        
        # Cloud base
        cloud_base = 10000
        for cloud in metar.findall("sky_condition"):
            if cloud.get("sky_cover") in ["BKN", "OVC"] and cloud.get("cloud_base_ft_agl"):
                try:
                    cloud_base = int(cloud.get("cloud_base_ft_agl"))
                    break
                except ValueError:
                    continue
        
        # Flight category/condition
        condition_el = metar.find("flight_category")
        condition = condition_el.text if condition_el is not None else "Unknown"
        
        logger.info(f"METAR {station}: Wind={wind_mph}mph, Vis={visibility}sm, Cloud={cloud_base}ft, Condition={condition}")
        return {
            "cloud_base": cloud_base,
            "visibility": visibility,
            "wind_mph": wind_mph,
            "condition": condition
        }
    except Exception as e:
        logger.error(f"METAR error for {station}: {e}")
        return None


def get_forecast(forecast_url):
    """Fetch hourly NWS forecast"""
    try:
        res = requests.get(forecast_url, headers=HEADERS, timeout=5)
        res.raise_for_status()
        data = res.json()
        periods = data.get("properties", {}).get("periods", [])
        logger.info(f"Retrieved {len(periods)} forecast periods")
        return periods
    except Exception as e:
        logger.error(f"NWS Forecast error: {e}")
        return []


def get_taf_forecast(station, selected_time):
    """Fetch and parse TAF data for given station and time"""
    try:
        url = f"https://aviationweather.gov/api/data/taf?format=xml&ids={station}"
        res = requests.get(url, headers=HEADERS, timeout=5)
        res.raise_for_status()
        root = ET.fromstring(res.content)
        taf = root.find(".//TAF")
        
        if taf is None:
            logger.warning(f"No TAF data found for {station}")
            return None, []
        
        taf_periods = []
        for forecast in taf.findall("forecast"):
            try:
                start_str = forecast.find("fcst_time_from").text
                end_str = forecast.find("fcst_time_to").text
                start_dt = datetime.strptime(start_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                end_dt = datetime.strptime(end_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                
                # Wind
                wind_el = forecast.find("wind_speed_kt")
                wind_kt = int(wind_el.text) if wind_el is not None and wind_el.text else 0
                wind_mph = round(wind_kt * 1.15078, 1)
                
                # Visibility
                vis_el = forecast.find("visibility_statute_mi")
                visibility = float(vis_el.text.replace("+", "")) if vis_el is not None and vis_el.text else 10.0
                
                # Clouds
                clouds = []
                for layer in forecast.findall("sky_condition"):
                    base = layer.get("cloud_base_ft_agl")
                    if base:
                        clouds.append(f"{base} ft")
                
                # Condition
                wx_el = forecast.find("wx_string")
                condition = wx_el.text if wx_el is not None else ""
                
                taf_periods.append({
                    "start": start_dt,
                    "end": end_dt,
                    "wind": wind_mph,
                    "visibility": visibility,
                    "clouds": ", ".join(clouds) if clouds else "None",
                    "condition": condition
                })
            except Exception as e:
                logger.error(f"Error parsing TAF period: {e}")
                continue
        
        # Find period matching selected time
        matching_period = None
        for period in taf_periods:
            if period["start"] <= selected_time <= period["end"]:
                matching_period = period
                break
        
        if matching_period:
            logger.info(f"TAF match found for {station} at {selected_time}")
        else:
            logger.warning(f"No TAF period matches {selected_time} for {station}")
        
        return matching_period, taf_periods
    except Exception as e:
        logger.error(f"TAF error for {station}: {e}")
        return None, []


def extract_wind_from_forecast(wind_speed_str):
    """Extract numeric wind speed from forecast string like '10 mph'"""
    match = re.search(r"\d+", wind_speed_str or "")
    return float(match.group()) if match else 0.0


@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    selected_coords = None
    
    if request.method == "POST":
        try:
            site = request.form.get("site", "")
            date_str = request.form.get("flight_date", "")
            time_str = request.form.get("flight_time", "")
            
            # Parse datetime
            central = pytz.timezone("America/Chicago")
            naive_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            selected_time = central.localize(naive_time).astimezone(pytz.utc)
            now_utc = datetime.now(timezone.utc)
            delta = selected_time - now_utc
            
            logger.info(f"Query: Site={site}, Time={selected_time}, Delta={delta}")
            
            # Parse coordinates
            lat, lon = parse_coordinates(site, request.form)
            if lat is None or lon is None:
                logger.error("Failed to parse coordinates")
                flight_sites = FlightSites.get_all_sites()
                return render_template("index.html", result=None, selected_coords=None, flight_sites=flight_sites, pytz=pytz, error="Invalid coordinates")
            
            selected_coords = {"lat": lat, "lon": lon}
            
            # Find nearest stations
            metar_station = db.find_nearest_metar(lat, lon)
            taf_station = db.find_nearest_taf(lat, lon)
            forecast_url, forecast_office = db.find_nws_forecast_url(lat, lon)
            
            if not metar_station:
                logger.error("No METAR station found")
                flight_sites = FlightSites.get_all_sites()
                return render_template("index.html", result=None, selected_coords=selected_coords, flight_sites=flight_sites, pytz=pytz, error="No nearby weather stations found")
            
            # Determine weather source and fetch data
            source = None
            cloud_base = 10000
            visibility = 0.0
            wind_mph = 0.0
            condition = "Unknown"
            
            if delta <= timedelta(hours=METAR_CUTOFF_HOURS):
                # Use METAR for current/near-future flights
                metar_data = get_metar_conditions(metar_station)
                if metar_data:
                    cloud_base = metar_data["cloud_base"]
                    visibility = metar_data["visibility"]
                    wind_mph = metar_data["wind_mph"]
                    condition = metar_data["condition"]
                    source = "metar"
                else:
                    logger.warning("METAR fetch failed, trying forecast")
            
            # Fallback to TAF or forecast
            if source != "metar" and taf_station:
                taf_match, taf_all = get_taf_forecast(taf_station, selected_time)
                if taf_match:
                    wind_mph = taf_match["wind"]
                    visibility = taf_match["visibility"]
                    condition = taf_match["condition"]
                    source = "taf"
                    # Cloud base from TAF is trickier; extract first number or use default
                    clouds_str = taf_match["clouds"]
                    cloud_match = re.search(r"\d+", clouds_str)
                    cloud_base = int(cloud_match.group()) if cloud_match else 10000
            
            # Last resort: NWS forecast
            if source is None and forecast_url:
                periods = get_forecast(forecast_url)
                if periods:
                    closest = min(periods, key=lambda p: abs(isoparse(p["startTime"]) - selected_time))
                    wind_mph = extract_wind_from_forecast(closest.get("windSpeed", ""))
                    visibility = 10.0
                    cloud_base = 10000
                    condition = closest.get("shortForecast", "Unknown")
                    source = "forecast"
            
            if source is None:
                logger.error("All weather sources failed")
                source = "unavailable"
            
            logger.info(f"Using {source} data: Wind={wind_mph}mph, Vis={visibility}sm, Cloud={cloud_base}ft")
            
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
                "site": site,
                "datetime_cst": naive_time.strftime("%m/%d/%Y at %I:%M %p CST"),
                "cloud_base": cloud_base,
                "cloud_label": f"{cloud_base} ft" if cloud_base < 10000 else "Clear",
                "visibility": f"{visibility:.1f} statute miles",
                "wind_mph": wind_mph,
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
                "station_used": metar_station,
                "taf_used": taf_station,
                "forecast_url_used": forecast_url
            }
        
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            flight_sites = FlightSites.get_all_sites()
            return render_template("index.html", result=None, selected_coords=None, flight_sites=flight_sites, pytz=pytz, error=f"Error: {str(e)}")
    
    flight_sites = FlightSites.get_all_sites()
    return render_template("index.html", result=result, selected_coords=selected_coords, flight_sites=flight_sites, pytz=pytz, error=None)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)