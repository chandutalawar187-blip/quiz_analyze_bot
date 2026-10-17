"""Interactive scorecard view tests."""

import unittest
import tempfile
from pathlib import Path

from quizcord_bot.database import Database
from quizcord_bot.views import (
    QuizSessionDashboardView,
    ResultsView,
    quiz_session_embed,
)


class ResultsViewTests(unittest.TestCase):
    def test_pagination_stays_within_discord_component_limit(self) -> None:
        profiles = [
            {"usn": f"USN{index}", "name": f"Student {index}"}
            for index in range(60)
        ]

        view = ResultsView(Database(Path("unused.db")), profiles, "quiz-1")

        self.assertEqual(len(view.children), 25)
        self.assertEqual(view.children[-2].label, "Previous")
        self.assertEqual(view.children[-1].label, "Next")

    def test_live_dashboard_displays_every_roster_usn_without_mapping_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "results.db")
            database.initialize()
            session = database.start_quiz_session(
                1, 2, 3, "2026-10-17T15:00:00+00:00"
            )
            database.record_quiz_leaderboard(
                session.session_id,
                4,
                "revision-1",
                [("1", "KNOWN_USN", "500"), ("2", "UNKNOWN_USN", "400")],
                False,
            )

            embed = quiz_session_embed(database, session.session_id)
            view = QuizSessionDashboardView(database, session.session_id, 3)
            rendered_text = " ".join(
                [embed.title or "", embed.description or ""]
                + [field.name + " " + field.value for field in embed.fields]
            ).casefold()

            self.assertIn("known\\_usn", rendered_text)
            self.assertIn("unknown\\_usn", rendered_text)
            self.assertNotIn("registered", rendered_text)
            self.assertNotIn("unregistered", rendered_text)
            self.assertEqual(len(view.children), 2)
            self.assertTrue(view.is_persistent())

            router = QuizSessionDashboardView(
                database, session.session_id, 3, persistent_router=True
            )
            self.assertTrue(router.is_persistent())
            self.assertEqual(
                {child.action for child in router.children},
                {"overview", "participants", "analytics", "previous", "next", "export"},
            )


if __name__ == "__main__":
    unittest.main()
