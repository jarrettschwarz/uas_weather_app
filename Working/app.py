from flask import Flask, render_template, request
import requests
from datetime import datetime, timedelta, timezone
import pytz
import xml.etree.ElementTree as ET
import re
from dateutil.parser import isoparse

app = Flask(__name__)

def to_dms(deg):
    d = int(deg)
    m = int((abs(deg) - abs(d)) * 60)
    s = (abs(deg) - abs(d) - m / 60) * 3600
    hemi = 'N' if deg >= 0 else 'S' if d == deg else 'W'
    return f"{abs(d)}°{m}'{s:.1f}\"{hemi}"

app.jinja_env.filters['dms'] = to_dms

HEADERS = {"User-Agent": "UAS-Weather-Check"}
MAX_WIND_MPH = 15.7
MIN_VISIBILITY_SM = 3.0
MIN_CLOUD_BASE_FT = 500
BAD_CONDITIONS = ["rain", "snow", "fog", "thunderstorm", "mist"]

FLIGHT_SITES = {
    "UAFS": {"lat": 36.162353, "lon": -98.836239, "station": "KSWO", "forecast_office": "Norman"},
    "CENFEX": {"lat": 36.360657, "lon": -96.860111, "station": "KSWO", "forecast_office": "Norman"},
    "Legion Field": {"lat": 34.723543, "lon": -98.387076, "station": "KLAW", "forecast_office": "Norman"},
    "Skyway 36": {"lat": 36.210521, "lon": -96.008673, "station": "KRVS", "forecast_office": "Tulsa"},
}

FORECAST_URLS = {
    "Norman": "https://api.weather.gov/gridpoints/OUN/43,34/forecast/hourly",
    "Tulsa": "https://api.weather.gov/gridpoints/TSA/28,44/forecast/hourly"
}

def get_sunrise_sunset(lat, lon, date):
    url = f"https://api.sunrise-sunset.org/json?lat={lat}&lng={lon}&date={date}&formatted=0"
    try:
        res = requests.get(url)
        data = res.json()["results"]
        sunrise_utc = datetime.fromisoformat(data["sunrise"]).replace(tzinfo=timezone.utc)
        sunset_utc = datetime.fromisoformat(data["sunset"]).replace(tzinfo=timezone.utc)
        return sunrise_utc, sunset_utc
    except:
        return None, None

def get_metar_conditions(station):
    url = f"https://aviationweather.gov/api/data/metar?format=xml&ids={station}"
    try:
        res = requests.get(url)
        root = ET.fromstring(res.content)
        metar = root.find(".//METAR")
        wind_speed = int(metar.find("wind_speed_kt").text) if metar.find("wind_speed_kt") is not None else 0
        visibility_el = metar.find("visibility_statute_mi")
        vis_str = visibility_el.text if visibility_el is not None else "0"
        visibility = float(vis_str.replace('+', ''))
        cloud_ft = 10000
        for cloud in metar.findall("sky_condition"):
            if cloud.get("sky_cover") in ["BKN", "OVC"] and cloud.get("cloud_base_ft_agl"):
                cloud_ft = int(cloud.get("cloud_base_ft_agl"))
                break
        condition = metar.find("flight_category").text if metar.find("flight_category") is not None else "Unknown"
        wind_mph = round(wind_speed * 1.15078, 1)
        wind_str = f"{wind_mph} mph"
        return cloud_ft, visibility, f"{cloud_ft} ft", wind_mph, wind_str, condition
    except Exception as e:
        print(f"METAR error: {e}")
        return 10000, 0.0, "Unavailable", 0.0, "N/A", "Unknown"

def get_forecast(forecast_url):
    try:
        res = requests.get(forecast_url, headers=HEADERS)
        return res.json()["properties"]["periods"]
    except:
        return []

def get_taf_forecast(station, selected_time):
    taf_url = f"https://aviationweather.gov/api/data/taf?format=xml&ids={station}"
    central = pytz.timezone("America/Chicago")
    try:
        res = requests.get(taf_url)
        root = ET.fromstring(res.content)
        taf = root.find(".//TAF")
        if taf is None:
            return None, []

        taf_periods = []
        for forecast in taf.findall("forecast"):
            start = forecast.find("fcst_time_from").text
            end = forecast.find("fcst_time_to").text
            start_dt = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            end_dt = datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

            wind_raw = forecast.find("wind_speed_kt")
            wind_kt = int(wind_raw.text) if wind_raw is not None else 0
            wind_mph = round(wind_kt * 1.15078, 1)
            wind = f"{wind_mph} mph"

            vis_raw = forecast.find("visibility_statute_mi")
            vis = vis_raw.text if vis_raw is not None else "10"
            visibility = float(vis.replace("+", ""))

            clouds = []
            for layer in forecast.findall("sky_condition"):
                if layer.get("cloud_base_ft_agl"):
                    clouds.append(f"{layer.get('cloud_base_ft_agl')} ft")
            cloud_str = ', '.join(clouds) if clouds else "None"

            condition = forecast.find("wx_string").text if forecast.find("wx_string") is not None else ""

            taf_periods.append({
                "start_cst": start_dt.astimezone(central),
                "end_cst": end_dt.astimezone(central),
                "start": start_dt,
                "end": end_dt,
                "wind": wind,
                "vis": f"{visibility:.1f} statute miles",
                "clouds": cloud_str,
                "condition": condition
            })

        match = next((p for p in taf_periods if p["start"] <= selected_time <= p["end"]), None)
        return match, taf_periods
    except Exception as e:
        print(f"TAF error: {e}")
        return None, []

@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    selected_coords = None
    taf_table = []

    if request.method == "POST":
        site = request.form["site"]
        date_str = request.form["flight_date"]
        time_str = request.form["flight_time"]
        central = pytz.timezone("America/Chicago")
        naive_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        selected_time = central.localize(naive_time).astimezone(pytz.utc)
        now_utc = datetime.now(timezone.utc)
        delta = selected_time - now_utc

        site_data = FLIGHT_SITES[site]
        selected_coords = {"lat": site_data["lat"], "lon": site_data["lon"]}
        station = site_data["station"]
        forecast_url = FORECAST_URLS[site_data["forecast_office"]]

        flight_time_remaining = None  # <- Make sure it's always defined

        if delta <= timedelta(hours=2):
            cloud_base, visibility, cloud_label, wind, wind_str, condition = get_metar_conditions(station)
            source = "metar"
            taf_table = []
        else:
            taf_summary, taf_periods = get_taf_forecast(station, selected_time)
            if taf_summary:
                wind_str = taf_summary["wind"]
                wind = float(wind_str.replace(" mph", ""))
                visibility = float(taf_summary["vis"].replace(" statute miles", ""))
                cloud_base_match = re.search(r"\d+", taf_summary["clouds"])
                cloud_base = int(cloud_base_match.group()) if cloud_base_match else 10000
                cloud_label = f"{cloud_base} ft"
                condition = taf_summary["condition"]
                source = "taf"
                taf_table = taf_periods
            else:
                periods = get_forecast(forecast_url)
                if periods:
                    closest = min(periods, key=lambda p: abs(isoparse(p["startTime"]) - selected_time))
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
        lat = selected_coords["lat"]
        lon = selected_coords["lon"]
        sunrise, sunset = get_sunrise_sunset(lat, lon, date_str)

        if sunrise and sunset:
            if selected_time < sunrise:
                minutes = int((sunrise - selected_time).total_seconds() / 60)
                failed_reasons.append(f"Operation is {minutes} minutes before sunrise")
            elif selected_time > sunset:
                minutes = int((selected_time - sunset).total_seconds() / 60)
                failed_reasons.append(f"Operation is {minutes} minutes after sunset")
            elif sunrise <= selected_time < sunset:
                remaining = sunset - selected_time
                hours, remainder = divmod(int(remaining.total_seconds()), 3600)
                minutes = remainder // 60
                flight_time_remaining = f"{hours} hours {minutes} minutes"

        if wind > MAX_WIND_MPH:
            failed_reasons.append("Wind above 15.7 mph")
        if visibility < MIN_VISIBILITY_SM:
            failed_reasons.append("Visibility below 3 statute miles")
        if cloud_base < MIN_CLOUD_BASE_FT:
            failed_reasons.append("Cloud base below 500 ft AGL")
        if any(term in condition.lower() for term in BAD_CONDITIONS):
            failed_reasons.append("Bad weather conditions present")

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
        }

    return render_template("index.html", result=result, flight_sites=FLIGHT_SITES.keys(), selected_coords=selected_coords, taf_table=taf_table, pytz=pytz)

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
