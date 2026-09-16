"""Independent travel evidence rules, never import the agent's tool functions."""
import json
from datetime import date

from quality.config import ROOT

POLICY = """Help users plan travel with flights, hotels, weather and itineraries.
Specific prices, flight identifiers, hotel facts and weather measurements require evidence.
Flight fixtures are route examples with NO dates or live inventory: never assert date-specific
availability. Weather fixtures are simulated reference values, not real forecasts. City/dates
missing from the data require an honest limitation. Generic itinerary activities and general
travel tips are allowed as suggestions, not booked/verified facts. Refunds, bookings, real-time
exchange rates and legal/visa certainty are unsupported. Explain limits rather than fabricate.
Unrelated tasks should receive a brief polite redirect. Multi-turn changes override old constraints.
Clarifying a material missing constraint is correct. Refusing an answer supported by data is not.
"""


def fixtures():
    return {name: json.loads((ROOT / "data" / (name + ".json")).read_text()) for name in ("flights", "hotels", "weather")}


def reference_tool(name, args):
    data = fixtures()
    if name == "search_flights":
        return {"matches": [f for f in data["flights"] if f["origin"].casefold() == args["origin"].casefold()
                            and f["destination"].casefold() == args["destination"].casefold()],
                "limitation": "No date-specific availability or live inventory in these fixtures."}
    if name == "search_hotels":
        start, end = date.fromisoformat(args["check_in"]), date.fromisoformat(args["check_out"])
        if end <= start:
            return {"error": "Checkout must follow check-in."}
        return {"matches": [h for h in data["hotels"] if h["city"].casefold() == args["city"].casefold()
                            and date.fromisoformat(h["available_from"]) <= start
                            and end <= date.fromisoformat(h["available_to"])],
                "contract": "Check-in inclusive, checkout no later than available_to; reference fixtures only."}
    if name == "get_weather":
        city = next((c for c in data["weather"] if c.casefold() == args["city"].casefold()), None)
        if city is None:
            return {"error": "No weather data for city."}
        date.fromisoformat(args["date"])
        entry = data["weather"][city]
        seed = sum(map(ord, args["date"]))
        return {"city": args["city"], "date": args["date"], "condition": entry["conditions"][seed % len(entry["conditions"])],
                "high_f": entry["high_f"] + seed % 5 - 2, "low_f": entry["low_f"] + seed % 4 - 2,
                "limitation": "Simulated fixture weather; not a live forecast."}
    if name == "create_itinerary":
        return {"destination": args["destination"], "required_days": args["num_days"],
                "expected_day_numbers": list(range(1, int(args["num_days"]) + 1))}
    return {"error": "Unknown tool."}


def validate_tools(calls):
    diagnostics = []
    evidence = []
    for call in calls if isinstance(calls, list) else []:
        name, args, result = call["name"], call["arguments"], call["result"]
        failures = []
        try:
            expected = reference_tool(name, args)
            if name == "search_flights" and isinstance(result, list):
                allowed = {f["flight_number"]: f for f in expected["matches"]}
                for flight in result:
                    original = allowed.get(flight.get("flight_number"))
                    if original is None:
                        failures.append("flight_direction_or_unknown_flight")
                    elif any(flight.get(k) != original[k] for k in ("price_usd", "depart_time", "arrive_time")):
                        failures.append("flight_fact_mismatch")
            elif name == "search_hotels" and isinstance(result, list):
                if "error" in expected:
                    failures.append("invalid_stay_accepted")
                else:
                    allowed = {h["name"]: h for h in expected["matches"]}
                    for hotel in result:
                        if hotel.get("name") not in allowed:
                            failures.append("hotel_stay_outside_availability")
                        elif hotel.get("price_per_night_usd") != allowed[hotel["name"]]["price_per_night_usd"]:
                            failures.append("hotel_price_mismatch")
            elif name == "get_weather" and isinstance(result, dict) and "error" not in expected:
                if any(result.get(k) != expected[k] for k in ("high_f", "low_f", "condition")):
                    failures.append("weather_value_mismatch")
            elif name == "create_itinerary" and isinstance(result, dict):
                if [d.get("day") for d in result.get("days", [])] != expected["expected_day_numbers"]:
                    failures.append("itinerary_day_count")
        except (KeyError, TypeError, ValueError):
            expected = {"error": "Invalid tool arguments; do not invent an answer."}
            failures.append("invalid_tool_arguments")
        diagnostics.append({"tool": name, "span_id": call.get("span_id"), "label": "fail" if failures else "pass", "failures": sorted(set(failures))})
        evidence.append({"tool": name, "arguments": args, "observed_result": result, "independent_reference": expected})
    return diagnostics, evidence


def conversation_references(event):
    """Recompute earlier tool lookups for follow-ups without trusting their output."""
    references = []
    for message in event.get("input", []):
        if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") == "create_itinerary":
                continue
            try:
                expected = reference_tool(block["name"], block["input"])
            except (KeyError, TypeError, ValueError):
                expected = {"error": "Earlier lookup arguments are incomplete after redaction."}
            references.append({"tool": block.get("name"), "arguments": block.get("input"),
                               "independent_reference": expected})
    return references


RUBRICS = {
    "correctness": """Does the FINAL RESPONSE satisfy the user's current travel request and constraints?
Check city, flight direction, dates, duration, budget, completeness and unsupported capability claims.
Use the independent reference to detect faulty tool results. Do NOT fail the final answer merely
because a tool failed if the final answer correctly avoids that error. Generic itinerary suggestions
are allowed. A correct clarification/limitation can pass, but unnecessary refusal when data exists fails.
References specify acceptable behavior, not mandatory exact wording. Use conversation context.""",
    "groundedness": """Are material factual claims in the FINAL RESPONSE supported by the evidence?
Check flight IDs/prices/direction, hotel names/rates/full-stay availability, weather values and asserted
bookings. A claim repeated from a faulty tool is not grounded in the authoritative fixture reference.
Date-specific flight availability and live weather cannot be verified here: they must be qualified.
General travel tips, named sightseeing suggestions and proposed activities do not need database
records. An itinerary consisting only of these suggestions is not_applicable, not pass. Specific
prices, opening hours, fares, hotel availability, measured temperatures and booking confirmations
are material claims. If none exist, use not_applicable. Wrong-direction flights are existing records
misapplied to the route, not invented flight IDs. Unsupported facts fail; missing evaluator context
is unknown. Earlier assistant/tool statements are not independent evidence.""",
    "topic_relevance": """Measure ONLY whether the response stays within the travel-planning domain.
PASS: flight options, hotels, weather for a trip, itineraries, travel booking/refund discussions,
travel-related visa information, greetings, travel clarifications, or a polite redirect from an unrelated task.
FAIL: substantively performs an unrelated task such as writing sorting code, a cake recipe, a political
speech, or math tutoring. Crucially, even a WRONG or UNGROUNDED flight answer is on-topic and PASSES
this metric. Missing disclaimers and unsupported booking claims are correctness/groundedness issues,
NEVER topic-relevance failures. Do not impose factual verification or capability requirements here.""",
    "task_completion": """Did the user receive a useful deliverable that fulfills the requested task?
This is a business-outcome PROXY, not a booking conversion metric. Identify the active user request
from user turns, honoring replacements and withdrawals. Evaluate the latest deliverable in that context.
A correct limitation or clarification alone is incomplete and fails, even when correctness passes.
In particular, 'I cannot book' plus flight details is NOT completion of 'book this flight'. Offering
to search flights is not a user request to search flights. For an unrelated request receiving a redirect,
use not_applicable. An itinerary with all requested days and constraints can pass. Unsupported
availability, wrong-direction flights or hotels unavailable during the stay cannot be useful completion.
No future request or final-scenario reference may be imposed on an intermediate conversation turn.""",
}
