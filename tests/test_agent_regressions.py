"""Regression checks attached to the quality-workflow proposal."""
import unittest
from agent.tools import create_itinerary, search_flights, search_hotels, get_weather

class TravelRegressions(unittest.TestCase):
    def test_flight_direction(self):
        flights = search_flights("New York", "Miami", "2026-10-02")
        self.assertEqual({f["flight_number"] for f in flights}, {"DL 883", "B6 1029"})

    def test_full_stay(self):
        self.assertEqual(search_hotels("New York", "2026-12-31", "2027-01-05"), [])

    def test_all_days(self):
        for days in (1, 3, 5):
            self.assertEqual([d["day"] for d in create_itinerary("Paris", days)["days"]], list(range(1, days+1)))

    def test_fahrenheit(self):
        weather = get_weather("Miami", "2026-10-02")
        seed = sum(map(ord, "2026-10-02"))
        self.assertEqual(weather["high_f"], 86 + seed % 5 - 2)
        self.assertEqual(weather["low_f"], 74 + seed % 4 - 2)

if __name__ == "__main__":
    unittest.main()
