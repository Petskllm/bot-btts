import unittest

import bot_btts


class TicketMarketTests(unittest.TestCase):
    def test_over25_is_kept_for_simple_predictions_but_excluded_from_tickets(self):
        self.assertIn("over25", bot_btts.CURRENT_PUBLISHED_MARKETS)
        self.assertFalse(bot_btts.ticket_market_allowed({"market": "over25"}))
        self.assertTrue(bot_btts.ticket_market_allowed({"market": "over15"}))

    def test_dayfun_no_longer_uses_over25_as_solid_or_boost(self):
        self.assertNotIn("over25", bot_btts.DAYFUN_EXTRA_MARKETS)
        self.assertNotIn("over25", bot_btts.DAYFUN_BOOST_MARKETS)


if __name__ == "__main__":
    unittest.main()
