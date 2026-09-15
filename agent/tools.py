from datetime import date
import json

from agent.config import DATA_DIR

with open(DATA_DIR / "flights.json") as f:
    FLIGHTS = json.load(f)
with open(DATA_DIR / "hotels.json") as f:
    HOTELS = json.load(f)
with open(DATA_DIR / "weather.json") as f:
    WEATHER = json.load(f)


def search_flights(origin: str, destination: str, date: str) -> list:
    return [
        {
            "airline": f["airline"],
            "flight_number": f["flight_number"],
            "depart_time": f["depart_time"],
            "arrive_time": f["arrive_time"],
            "price_usd": f["price_usd"],
        }
        for f in FLIGHTS
        if f["origin"].lower() == origin.lower()
        and f["destination"].lower() == destination.lower()
    ]


def search_hotels(city: str, check_in: str, check_out: str) -> list:
    return [
        {
            "name": h["name"],
            "city": h["city"],
            "price_per_night_usd": h["price_per_night_usd"],
            "rating": h["rating"],
        }
        for h in HOTELS
        if h["city"].lower() == city.lower()
        and h["available_from"] <= check_in
        and check_out <= h["available_to"]
    ]


def get_weather(city: str, date: str) -> dict:
    entry = next((v for k, v in WEATHER.items() if k.lower() == city.lower()), None)
    if entry is None:
        return {"error": f"No weather data available for {city}"}
    seed = sum(ord(c) for c in date)
    high = entry["high_f"] + seed % 5 - 2
    low = entry["low_f"] + seed % 4 - 2
    return {
        "city": city,
        "date": date,
        "condition": entry["conditions"][seed % len(entry["conditions"])],
        "high_f": round(high),
        "low_f": round(low),
    }


def create_itinerary(destination: str, num_days: int, notes: str = "") -> dict:
    days = []
    for day in range(1, int(num_days) + 1):
        days.append(
            {
                "day": day,
                "morning": f"Explore {destination}",
                "afternoon": "Activities / free time",
                "evening": "Dinner at a local restaurant",
            }
        )
    return {
        "destination": destination,
        "num_days": num_days,
        "notes": notes,
        "days": days,
    }


TOOLS = [
    {
        "name": "search_flights",
        "description": "Search for flights between two cities on a given date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "Departure city"},
                "destination": {"type": "string", "description": "Arrival city"},
                "date": {"type": "string", "description": "Travel date"},
            },
            "required": ["origin", "destination", "date"],
        },
    },
    {
        "name": "search_hotels",
        "description": "Search for hotels in a city for a date range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City to search in"},
                "check_in": {"type": "string", "description": "Check-in date (YYYY-MM-DD)"},
                "check_out": {"type": "string", "description": "Check-out date (YYYY-MM-DD)"},
            },
            "required": ["city", "check_in", "check_out"],
        },
    },
    {
        "name": "get_weather",
        "description": "Get the weather forecast for a city on a given date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
                "date": {"type": "string", "description": "Forecast date"},
            },
            "required": ["city", "date"],
        },
    },
    {
        "name": "create_itinerary",
        "description": "Create a day-by-day itinerary for a trip.",
        "input_schema": {
            "type": "object",
            "properties": {
                "destination": {"type": "string", "description": "Trip destination"},
                "num_days": {"type": "integer", "description": "Number of days"},
                "notes": {"type": "string", "description": "Extra details to include"},
            },
            "required": ["destination", "num_days"],
        },
    },
]

TOOL_FUNCTIONS = {
    "search_flights": search_flights,
    "search_hotels": search_hotels,
    "get_weather": get_weather,
    "create_itinerary": create_itinerary,
}


def execute_tool(name: str, tool_input: dict):
    try:
        return TOOL_FUNCTIONS[name](**tool_input)
    except Exception as e:
        return {"error": str(e)}
