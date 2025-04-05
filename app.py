from flask import Flask, render_template, request
import requests
from datetime import datetime, timezone, timedelta
import pytz
import xml.etree.ElementTree as ET

app = Flask(__name__)

HEADERS = {"User-Agent": "UAS-Weather-Check"}
MAX_WIND_MPH = 15.7
MIN_VISIBILITY_SM = 3.0
MIN_CLOUD_BASE_FT = 500
BAD_CONDITIONS = ["rain", "snow", "fog", "thunderstorm", "mist"]

# Hardcoded flight site data including METAR station and forecast office information.
FLIGHT_SITES = {
    "UAFS": {
        "lat": 36.162353,
        "lon": -98.836239,
        "metar_station": "KSWO",       # Use Stillwater Regional Airport
        "forecast_office": "Norman",    # NWS Norman (closest town: Stillwater)
        "closest_town": "Stillwater"
    },
    "CENFEX": {
        "lat": 36.360657,
        "lon": -96.860111,
        "metar_station": "KSWO",       # Use Stillwater Regional Airport
        "forecast_office": "Norman",    # NWS Norman (closest town: Pawnee)
        "closest_town": "Pawnee"
    },
    "Legion Field": {
        "lat": 34.723543,
        "lon": -98.387076,
        "metar_station": "KLAW",       # Use Lawton Airport
        "forecast_office": "Norman",    # NWS Norman (closest town: Lawton)
        "closest_town": "Lawton"
    },
    "Skyway 36": {
        "lat": 36.210521,
        "lon": -96.008673,
        "metar_station": "KTUL",       # Use Tulsa International Airport
        "forecast_office": "Tulsa",     # NWS Tulsa (closest town: Tulsa)
        "closest_town": "Tulsa"
    }
}

# Hardcoded forecast URLs by forecast office.
FORECAST_URLS = {
    "Norman": "https://api.weather.gov/gridpoints/OUN/43,34/forecast/hourly",
    "Tulsa": "https://api.weather.gov/gridpoints/TSA/28,44/forecast/hourly"
}

def get_forecast(forecast_url):
    res = requests.get(forecast_url, headers=HEADERS)
    if res.status_code == 200:
        return res.json()["properties"]["periods"]
    else:
        return []

def get_metar_conditions(metar_station):
    # Build observation URL using the hardcoded station identifier.
    obs_url = f"https://api.weather.gov/stations/{metar_station}/observations/latest"
    res = requests.get(obs_url, headers=HEADERS).json()
    props = res["properties"]

    # Cloud base calculation
    cloud_ft = None
    for layer in props.get("cloudLayers", []):
        if layer["amount"] in ["broken", "overcast"]:
            base_m = layer.get("base", {}).get("value")
            if base_m:
                cloud_ft = int(base_m * 3.281)
                break
    if cloud_ft is None:
        cloud_ft = 10000
        cloud_label = "10,000 ft (Clear Sky Assumed)"
    else:
        cloud_label = f"{cloud_ft} ft"

    # Visibility conversion (meters to statute miles)
    visibility = props.get("visibility", {}).get("value")
    vis_sm = round(visibility * 0.000621371, 1) if visibility else 0.0

    # Wind: Use sustained wind for decision making, but display gust info if available.
    wind_speed = props.get("windSpeed", {}).get("value")
    gust_speed = props.get("windGust", {}).get("value")
    sustained_wind = round(wind_speed * 2.23694, 1) if wind_speed else 0.0
    gust = round(gust_speed * 2.23694, 1) if gust_speed else 0.0
    wind_str = f"{sustained_wind:.1f} mph" + (f" gusting to {gust:.1f} mph" if gust > sustained_wind else "")

    # Condition description
    condition = props.get("textDescription", "Unknown")

    return cloud_ft, vis_sm, cloud_label, sustained_wind, wind_str, condition

def get_taf_plain(metar_station):
    """
    Fetches the TAF for the given station from aviationweather.gov,
    parses the XML, and converts common abbreviations into plain English.
    """
    url = f"https://aviationweather.gov/adds/dataserver_current/taf?station={metar_station}&hours=12&format=xml"
    try:
        res = requests.get(url, headers=HEADERS)
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            # Try to directly find the raw_text element anywhere in the XML.
            raw_text_elem = root.find(".//raw_text")
            if raw_text_elem is not None and raw_text_elem.text:
                raw_text = raw_text_elem.text
                plain_text = raw_text
                # Replace common TAF abbreviations with plain English.
                replacements = {
                    "FM": "From",
                    "TEMPO": "Temporary",
                    "BECMG": "Becoming",
                    "PROB": "Probability",
                    "CAVOK": "Ceiling And Visibility OK"
                }
                for abbr, full in replacements.items():
                    plain_text = plain_text.replace(abbr, full)
                return plain_text
    except Exception as e:
        print("Error parsing TAF:", e)
    return "TAF data unavailable"


@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    selected_coords = None
    if request.method == "POST":
        site = request.form["site"]
        date_str = request.form["flight_date"]
        time_str = request.form["flight_time"]

        # Convert the provided flight time to UTC
        central = pytz.timezone("America/Chicago")
        naive_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        selected_time = central.localize(naive_time).astimezone(pytz.utc)

        site_data = FLIGHT_SITES[site]
        selected_coords = {"lat": site_data["lat"], "lon": site_data["lon"]}
        metar_station = site_data["metar_station"]
        forecast_office = site_data["forecast_office"]
        forecast_url = FORECAST_URLS[forecast_office]

        now_utc = datetime.now(timezone.utc)
        delta = selected_time - now_utc

        if delta <= timedelta(hours=2):
            # Use METAR for near-term conditions
            cloud_base, visibility, cloud_label, sustained_wind, wind_str, forecast = get_metar_conditions(metar_station)
            source = "metar"
            # For near-term flights, we might not show TAF.
            taf_text = None
            taf_condition_pass = None
        else:
            # Use forecast for future conditions
            forecast_periods = get_forecast(forecast_url)
            # Find the forecast period closest to the desired flight time
            closest_period = min(
                forecast_periods,
                key=lambda p: abs(datetime.fromisoformat(p["startTime"]) - selected_time)
            )
            wind_str = closest_period["windSpeed"]
            sustained_wind = float(wind_str.split()[0])
            forecast = closest_period["shortForecast"]
            cloud_base = 10000
            cloud_label = "N/A (Forecast data)"
            visibility = 10.0  # Assume best-case if not provided
            source = "forecast"
            # Get and process TAF data
            taf_text = get_taf_plain(metar_station)
            taf_condition_pass = not any(term in taf_text.lower() for term in BAD_CONDITIONS)

        # Evaluate flight conditions against Part 107 requirements (using sustained wind)
        wind_pass = sustained_wind <= MAX_WIND_MPH
        visibility_pass = visibility >= MIN_VISIBILITY_SM
        cloud_pass = cloud_base >= MIN_CLOUD_BASE_FT
        condition_pass = not any(term in forecast.lower() for term in BAD_CONDITIONS)

        all_pass = all([wind_pass, visibility_pass, cloud_pass, condition_pass])

        result = {
            "site": site,
            "datetime": selected_time.strftime("%Y-%m-%d %H:%M"),
            "wind": wind_str,
            "forecast": forecast,
            "cloud_base": cloud_base,
            "cloud_label": cloud_label,
            "visibility": f"{visibility:.1f} sm",
            "wind_pass": wind_pass,
            "visibility_pass": visibility_pass,
            "cloud_pass": cloud_pass,
            "condition_pass": condition_pass,
            "go_nogo": "Go" if all_pass else "No-Go",
            "source": source,
            "taf": taf_text,
            "taf_pass": taf_condition_pass
        }

    return render_template("index.html", result=result, flight_sites=FLIGHT_SITES.keys(), selected_coords=selected_coords)

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
