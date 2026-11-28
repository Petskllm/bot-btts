import unittest

import bot_btts


class TicketMarketTests(unittest.TestCase):
    def test_over25_is_kept_for_simple_predictions_but_excluded_from_tickets(self):
        self.assertIn("over25", bot_btts.CURRENT_PUBLISHED_MARKETS)
        self.assertFalse(bot_btts.ticket_market_allowed({"market": "over25"}))
        self.assertTrue(bot_btts.ticket_market_allowed({"market": "over15"}))

    def test_cymru_and_national_league_are_excluded(self):
        for league in ("Cymru Premier", "National League N / S", "National League North"):
            with self.subTest(league=league):
                fixture = {"league_name": league, "league_id": None}
                self.assertFalse(bot_btts.ticket_league_ok(fixture))

    def test_only_normal_predictions_and_dayfun_are_active(self):
        self.assertFalse(bot_btts.TICKET_ACTIF)
        self.assertFalse(bot_btts.MEGA_ACTIF)
        self.assertFalse(bot_btts.SAFE_ACTIF)
        self.assertTrue(bot_btts.DAYFUN_ACTIF)

    def test_dayfun_target_odds_is_30(self):
        self.assertEqual(bot_btts.DAYFUN_TARGET_ODDS, 30.0)

    def test_dayfun_value_band_is_limited_and_form_based(self):
        self.assertEqual(bot_btts.DAYFUN_BOOST_MIN_PROB, 0.61)
        self.assertEqual(bot_btts.DAYFUN_BOOST_MAX_ODDS, 2.20)
        self.assertEqual(bot_btts.DAYFUN_BOOST_LEGS, 2)
        self.assertEqual(bot_btts.DAYFUN_SOLID_LEGS, 8)
        self.assertEqual(bot_btts.DAYFUN_BOOST_MARKETS, ("home", "over15"))

    def test_all_ticket_families_reject_low_odds(self):
        self.assertEqual(bot_btts.MIN_LEG_ODDS, 1.25)
        self.assertEqual(bot_btts.TICKET_MIN_LEG_ODDS, 1.25)
        self.assertEqual(bot_btts.SAFE_MIN_LEG_ODDS, 1.25)
        self.assertEqual(bot_btts.DAYFUN_MIN_LEG_ODDS, 1.25)

    def test_low_real_odds_are_marked_unplayable_in_messages(self):
        self.assertIn("ne pas jouer", bot_btts.odds_text(0.80, 1.15))
        self.assertIn("ne pas jouer", bot_btts.tg_odds(0.80, 1.15))
        self.assertIn("value", bot_btts.tg_odds(0.80, 1.30))

    def test_dayfun_no_longer_uses_over25_as_solid_or_boost(self):
        self.assertNotIn("over25", bot_btts.DAYFUN_EXTRA_MARKETS)
        self.assertNotIn("over25", bot_btts.DAYFUN_BOOST_MARKETS)


if __name__ == "__main__":
    unittest.main()
