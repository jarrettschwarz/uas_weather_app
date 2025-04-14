from flask import Flask, render_template, request
import requests
from datetime import datetime, timedelta, timezone
import pytz
import xml.etree.ElementTree as ET
import re
from dateutil.parser import isoparse
from math import radians, cos, sin, sqrt, atan2
import csv
import os

app = Flask(__name__)

# Utility: convert decimal degrees to DMS string
app.jinja_env.filters['dms'] = lambda deg: f"{int(abs(deg))}°{int((abs(deg)%1)*60)}'{((abs(deg)*3600)%60):.1f}\"{'N' if deg>=0 else 'S'}" if deg else ""

# Constants
HEADERS = {"User-Agent": "UAS-Weather-Check"}
MAX_WIND_MPH = 15.7
MIN_VISIBILITY_SM = 3.0
MIN_CLOUD_BASE_FT = 500
BAD_CONDITIONS = ["rain", "snow", "fog", "thunderstorm", "mist"]

# Load local METAR stations
METAR_STATIONS = []
with open("metar_stations_oklahoma.csv", newline='') as f:
    reader = csv.DictReader(f)
    METAR_STATIONS = [(row["icao"], float(row["lat"]), float(row["lon"])) for row in reader]

# Load local TAF stations
TAF_STATIONS = []
with open("taf_stations_oklahoma.csv", newline='') as f:
    reader = csv.DictReader(f)
    TAF_STATIONS = [(row["icao"], float(row["lat"]), float(row["lon"])) for row in reader]

# Load local NWS grid points
NWS_POINTS = []
with open("nws_gridpoints_oklahoma.csv", newline='') as f:
    reader = csv.DictReader(f)
    NWS_POINTS = [{
        "office": row["office"],
        "location": row["location"],
        "lat": float(row["lat"]),
        "lon": float(row["lon"]),
        "gridX": int(row["gridX"]),
        "gridY": int(row["gridY"])
    } for row in reader]

# DMS parser
def dms_to_decimal(deg, min_, sec, direction):
    try:
        if not deg or not min_ or not sec:
            return None
        decimal = float(deg) + float(min_) / 60 + float(sec) / 3600
        if direction in ['S', 'W']:
            decimal *= -1
        return decimal
    except ValueError:
        return None

# Haversine distance
def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))

def find_nearest_metar_station(lat, lon):
    closest = min(METAR_STATIONS, key=lambda s: haversine(lat, lon, s[1], s[2]))
    print(f"DEBUG: Closest METAR station = {closest[0]}")
    return closest[0]

def find_nearest_taf_station(lat, lon):
    closest = min(TAF_STATIONS, key=lambda s: haversine(lat, lon, s[1], s[2]))
    print(f"DEBUG: Closest TAF station = {closest[0]}")
    return closest[0]

def get_nws_forecast_url(lat, lon):
    closest = min(NWS_POINTS, key=lambda p: haversine(lat, lon, p["lat"], p["lon"]))
    print(f"DEBUG: Closest NWS office = {closest['office']} at grid {closest['gridX']},{closest['gridY']}")
    url = f"https://api.weather.gov/gridpoints/{closest['office']}/{closest['gridX']},{closest['gridY']}/forecast/hourly"
    return url, closest["office"]

def get_sunrise_sunset(lat, lon, date):
    url = f"https://api.sunrise-sunset.org/json?lat={lat}&lng={lon}&date={date}&formatted=0"
    try:
        res = requests.get(url)
        data = res.json()["results"]
        sunrise = datetime.fromisoformat(data["sunrise"]).replace(tzinfo=timezone.utc)
        sunset = datetime.fromisoformat(data["sunset"]).replace(tzinfo=timezone.utc)
        return sunrise, sunset
    except:
        return None, None

def get_metar_conditions(station):
    url = f"https://aviationweather.gov/api/data/metar?format=xml&ids={station}"
    try:
        res = requests.get(url)
        root = ET.fromstring(res.content)
        metar = root.find(".//METAR")
        wind_speed = int(metar.find("wind_speed_kt").text or 0)
        vis_el = metar.find("visibility_statute_mi")
        visibility = float(vis_el.text.replace('+', '')) if vis_el is not None else 0
        cloud_ft = 10000
        for cloud in metar.findall("sky_condition"):
            if cloud.get("sky_cover") in ["BKN", "OVC"] and cloud.get("cloud_base_ft_agl"):
                cloud_ft = int(cloud.get("cloud_base_ft_agl"))
                break
        condition = metar.find("flight_category").text if metar.find("flight_category") is not None else "Unknown"
        wind_mph = round(wind_speed * 1.15078, 1)
        return cloud_ft, visibility, f"{cloud_ft} ft", wind_mph, f"{wind_mph} mph", condition
    except:
        return 10000, 0.0, "Unavailable", 0.0, "N/A", "Unknown"

def get_forecast(forecast_url):
    try:
        res = requests.get(forecast_url, headers=HEADERS)
        return res.json()["properties"]["periods"]
    except:
        return []

@app.route("/", methods=["GET", "POST"])
def index():
    result, taf_table, selected_coords = None, [], None

    if request.method == "POST":
        site_coords = {
            "UAFS": {"lat": 36.162101, "lon": -96.835504},
            "CENFEX": {"lat": 36.357214, "lon": -96.861901},
            "Legion Field": {"lat": 34.723543, "lon": -98.387076},
            "SkyWay36": {"lat": 36.210521, "lon": -96.008673}
        }

        site = request.form.get("site")
        date_str = request.form["flight_date"]
        time_str = request.form["flight_time"]
        central = pytz.timezone("America/Chicago")
        naive_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        selected_time = central.localize(naive_time).astimezone(pytz.utc)
        delta = selected_time - datetime.now(timezone.utc)

        if site in site_coords:
            lat = site_coords[site]["lat"]
            lon = site_coords[site]["lon"]
            selected_coords = {"lat": lat, "lon": lon, "name": site}
            station = find_nearest_metar_station(lat, lon)
            taf_station = find_nearest_taf_station(lat, lon)
            forecast_url, forecast_office = get_nws_forecast_url(lat, lon)

        elif site == "Custom":
            format_type = request.form.get("coordFormat")
            try:
                if format_type == "decimal":
                    lat = float(request.form.get("lat_decimal", "").strip())
                    lon = float(request.form.get("lon_decimal", "").strip())
                else:
                    lat = dms_to_decimal(request.form.get("lat_deg"), request.form.get("lat_min"), request.form.get("lat_sec"), request.form.get("lat_dir"))
                    lon = dms_to_decimal(request.form.get("lon_deg"), request.form.get("lon_min"), request.form.get("lon_sec"), request.form.get("lon_dir"))
                    if lat is None or lon is None:
                        raise ValueError("Invalid DMS")
                selected_coords = {"lat": lat, "lon": lon, "name": "Custom Location"}
                station = find_nearest_metar_station(lat, lon)
                taf_station = find_nearest_taf_station(lat, lon)
                forecast_url, forecast_office = get_nws_forecast_url(lat, lon)
            except:
                return render_template("index.html", result=None, flight_sites=["Custom"], selected_coords=None, taf_table=[], pytz=pytz)
        else:
            return render_template("index.html", result=None, flight_sites=["Custom"], selected_coords=None, taf_table=[], pytz=pytz)

        if delta <= timedelta(hours=2):
            cloud_base, visibility, cloud_label, wind, wind_str, condition = get_metar_conditions(station)
            source = "metar"
        else:
            forecast = get_forecast(forecast_url)
            if forecast:
                closest = min(forecast, key=lambda p: abs(isoparse(p["startTime"]) - selected_time))
                wind_match = re.search(r"\d+", closest["windSpeed"])
                wind = float(wind_match.group()) if wind_match else 0.0
                wind_str = f"{wind:.1f} mph"
                visibility = 10.0
                cloud_base = 10000
                cloud_label = "N/A (Forecast)"
                condition = closest["shortForecast"]
                source = "forecast"
            else:
                cloud_base = 10000
                visibility = 0.0
                wind = 0.0
                wind_str = "N/A"
                cloud_label = "N/A"
                condition = "Unknown"
                source = "none"

        failed_reasons = []
        sunrise, sunset = get_sunrise_sunset(lat, lon, date_str)
        flight_time_remaining = None
        if sunrise and sunset:
            if selected_time < sunrise:
                failed_reasons.append(f"Operation is {int((sunrise - selected_time).total_seconds() / 60)} minutes before sunrise")
            elif selected_time > sunset:
                failed_reasons.append(f"Operation is {int((selected_time - sunset).total_seconds() / 60)} minutes after sunset")
            else:
                rem = sunset - selected_time
                flight_time_remaining = f"{rem.seconds // 3600} hours {(rem.seconds % 3600) // 60} minutes"

        if wind > MAX_WIND_MPH: failed_reasons.append("Wind above 15.7 mph")
        if visibility < MIN_VISIBILITY_SM: failed_reasons.append("Visibility below 3 statute miles")
        if cloud_base < MIN_CLOUD_BASE_FT: failed_reasons.append("Cloud base below 500 ft AGL")
        if any(term in condition.lower() for term in BAD_CONDITIONS): failed_reasons.append("Bad weather conditions present")

        result = {
            "site": site,
            "datetime": selected_time.strftime("%Y-%m-%d %H:%M"),
            "datetime_cst": naive_time.strftime("%m/%d/%Y at %I:%M %p CST"),
            "cloud_base": cloud_base,
            "cloud_label": cloud_label,
            "visibility": f"{visibility:.1f} statute miles",
            "wind_metar": wind_str if source == "metar" else "N/A",
            "wind_forecast": wind_str if source != "metar" else "N/A",
            "forecast": condition,
            "source": source,
            "wind_pass": wind <= MAX_WIND_MPH,
            "visibility_pass": visibility >= MIN_VISIBILITY_SM,
            "cloud_pass": cloud_base >= MIN_CLOUD_BASE_FT,
            "condition_pass": not any(term in condition.lower() for term in BAD_CONDITIONS),
            "go_nogo": "Go" if not failed_reasons else "No-Go",
            "failed_reasons": failed_reasons,
            "icon": "🌤️",
            "taf_table": taf_table,
            "sunrise": sunrise.astimezone(central).strftime('%I:%M %p CST') if sunrise else "N/A",
            "sunset": sunset.astimezone(central).strftime('%I:%M %p CST') if sunset else "N/A",
            "flight_time_remaining": flight_time_remaining,
            "station_used": station,
            "taf_used": taf_station,
            "forecast_url_used": forecast_url
        }

    return render_template("index.html", result=result, flight_sites=["Custom"], selected_coords=selected_coords, taf_table=taf_table, pytz=pytz)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
