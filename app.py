from flask import Flask, render_template, request
import requests
from datetime import datetime, timedelta, timezone
import pytz
import os

app = Flask(__name__)

HEADERS = {"User-Agent": "UAS-Weather-Check"}
MIN_VISIBILITY_SM = 3
MAX_WIND_MPS = 7
EXCLUDE_CONDITIONS = ["thunderstorm", "rain", "snow", "fog", "mist"]

# Define 4 flight areas
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

def get_hourly_forecast(url):
    res = requests.get(url, headers=HEADERS)
    return res.json()["properties"]["periods"] if res.status_code == 200 else []

def get_cloud_base(stations_url):
    res = requests.get(stations_url, headers=HEADERS)
    station_id = res.json()["features"][0]["properties"]["stationIdentifier"]
    obs_url = f"https://api.weather.gov/stations/{station_id}/observations/latest"
    obs = requests.get(obs_url, headers=HEADERS).json()
    clouds = obs["properties"].get("cloudLayers", [])
    for layer in clouds:
        if layer["amount"] in ["broken", "overcast"]:
            return layer.get("base", {}).get("value", None)
    return None

def get_station_id(stations_url):
    res = requests.get(stations_url, headers=HEADERS)
    return res.json()["features"][0]["properties"]["stationIdentifier"]

def get_metar_taf(station_id):
    try:
        metar_url = f"https://aviationweather.gov/api/data/metar?ids={station_id}&format=json"
        taf_url = f"https://aviationweather.gov/api/data/taf?ids={station_id}&format=json"

        metar_data = requests.get(metar_url, headers=HEADERS).json()
        taf_data = requests.get(taf_url, headers=HEADERS).json()

        latest_metar = metar_data[0]['rawText'] if metar_data else "N/A"
        latest_taf = taf_data[0]['rawText'] if taf_data else "N/A"

        return latest_metar, latest_taf
    except Exception as e:
        print(f"❌ Error fetching METAR/TAF for station {station_id}: {e}")
        return "Unavailable", "Unavailable"

def check_part107(period, cloud_base_m=None):
    reasons = []
    try:
        wind = float(period["windSpeed"].split()[0]) * 0.44704  # mph to m/s
    except:
        wind = 0

    if wind > MAX_WIND_MPS:
        reasons.append(f"High wind: {wind:.1f} m/s")

    if any(term in period["shortForecast"].lower() for term in EXCLUDE_CONDITIONS):
        reasons.append(f"Hazardous: {period['shortForecast']}")

    if cloud_base_m and cloud_base_m < 152.4:  # 500 feet
        reasons.append(f"Clouds too low: {cloud_base_m:.0f}m")

    return "Yes" if not reasons else f"No — {'; '.join(reasons)}"

@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    metar, taf = None, None
    selected_coords = None
    selected_time = None

    if request.method == "POST":
        try:
            site = request.form["site"]
            date_str = request.form["flight_date"]
            time_str = request.form["flight_time"]
            print(f"✅ FORM RECEIVED — Site: {site}, Date: {date_str}, Time: {time_str}")
        except Exception as e:
            print("❌ Error getting form data:", e)
            raise

        try:
            coords = FLIGHT_SITES[site]
            selected_coords = coords
            central = pytz.timezone("America/Chicago")  # Central Time Zone
            naive_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            local_time = central.localize(naive_time)         # Make timezone-aware (Central)
            selected_time = local_time.astimezone(pytz.utc)   # Convert to UTC
            print(f"🕓 Local time: {local_time}, UTC: {selected_time}")
            print(f"⏱️ Parsed datetime: {selected_time}")
        except Exception as e:
            print("❌ Error with site lookup or datetime parsing:", e)
            raise

        try:
            forecast_url, stations_url = get_nws_point(coords["lat"], coords["lon"])
            cloud_base = get_cloud_base(stations_url)
            forecast_periods = get_hourly_forecast(forecast_url)

            # Get closest forecast hour to selected datetime
            closest_period = min(forecast_periods, key=lambda p: abs(datetime.fromisoformat(p["startTime"]) - selected_time))

            station_id = get_station_id(stations_url)
            metar, taf = get_metar_taf(station_id)

            go_nogo = check_part107(closest_period, cloud_base)

            result = {
                "site": site,
                "datetime": selected_time.strftime("%Y-%m-%d %H:%M"),
                "forecast": closest_period["shortForecast"],
                "wind": closest_period["windSpeed"],
                "go_nogo": go_nogo
            }

            print(f"✅ Final Result: {result}")
        except Exception as e:
            print("❌ Error during weather data fetch or go/no-go check:", e)
            raise

    return render_template("index.html", result=result, metar=metar, taf=taf,
                           flight_sites=FLIGHT_SITES.keys(),
                           selected_coords=selected_coords)

import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
