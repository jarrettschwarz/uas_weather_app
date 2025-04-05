from flask import Flask, render_template, request
import requests
from datetime import datetime, timezone
import pytz

app = Flask(__name__)

HEADERS = {"User-Agent": "UAS-Weather-Check"}
MAX_WIND_MPH = 15.7
MIN_VISIBILITY_SM = 3.0
MIN_CLOUD_BASE_FT = 500
BAD_CONDITIONS = ["rain", "snow", "fog", "thunderstorm", "mist"]

FLIGHT_SITES = {
    "UAFS": {"lat": 36.162353, "lon": -98.836239},
    "CENFEX": {"lat": 36.360657, "lon": -96.860111},
    "Legion Field": {"lat": 34.723543, "lon": -98.387076},
    "Skyway 36": {"lat": 36.210521, "lon": -96.008673}
}

def get_nws_point(lat, lon):
    url = f"https://api.weather.gov/points/{lat},{lon}"
    res = requests.get(url, headers=HEADERS).json()
    return res["properties"]["forecastHourly"], res["properties"]["observationStations"]

def get_forecast(forecast_url):
    res = requests.get(forecast_url, headers=HEADERS)
    return res.json()["properties"]["periods"] if res.status_code == 200 else []

def get_cloud_base_and_visibility(station_url):
    res = requests.get(station_url, headers=HEADERS)
    station_id = res.json()["features"][0]["properties"]["stationIdentifier"]
    obs_url = f"https://api.weather.gov/stations/{station_id}/observations/latest"
    obs = requests.get(obs_url, headers=HEADERS).json()
    props = obs["properties"]

    cloud_ft = None
    for layer in props.get("cloudLayers", []):
        if layer["amount"] in ["broken", "overcast"]:
            base_m = layer.get("base", {}).get("value", 0)
            cloud_ft = int(base_m * 3.281) if base_m else None

    visibility = props.get("visibility", {}).get("value", None)
    vis_sm = round(visibility * 0.000621371, 1) if visibility else None

    return cloud_ft or 0, vis_sm or 0.0

@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    selected_coords = None
    if request.method == "POST":
        site = request.form["site"]
        date_str = request.form["flight_date"]
        time_str = request.form["flight_time"]

        central = pytz.timezone("America/Chicago")
        naive_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        selected_time = central.localize(naive_time).astimezone(pytz.utc)

        coords = FLIGHT_SITES[site]
        selected_coords = coords

        forecast_url, station_url = get_nws_point(coords["lat"], coords["lon"])
        forecast_periods = get_forecast(forecast_url)
        cloud_base, visibility = get_cloud_base_and_visibility(station_url)

        closest_period = min(forecast_periods, key=lambda p: abs(datetime.fromisoformat(p["startTime"]) - selected_time))
        wind_str = closest_period["windSpeed"]
        wind_mph = float(wind_str.split()[0])
        forecast = closest_period["shortForecast"]

        # Evaluate each condition
        wind_pass = wind_mph <= MAX_WIND_MPH
        visibility_pass = visibility >= MIN_VISIBILITY_SM
        cloud_pass = cloud_base >= MIN_CLOUD_BASE_FT
        condition_pass = not any(term in forecast.lower() for term in BAD_CONDITIONS)

        all_pass = all([wind_pass, visibility_pass, cloud_pass, condition_pass])

        result = {
            "site": site,
            "datetime": selected_time.strftime("%Y-%m-%d %H:%M"),
            "wind": f"{wind_mph:.1f} mph",
            "forecast": forecast,
            "cloud_base": cloud_base,
            "visibility": f"{visibility:.1f} sm",
            "wind_pass": wind_pass,
            "visibility_pass": visibility_pass,
            "cloud_pass": cloud_pass,
            "condition_pass": condition_pass,
            "go_nogo": "Go" if all_pass else "No-Go"
        }

    return render_template("index.html", result=result, flight_sites=FLIGHT_SITES.keys(), selected_coords=selected_coords)