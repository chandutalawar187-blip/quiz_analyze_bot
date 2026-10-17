"""Quizcord leaderboard parsing tests."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from quizcord_bot.quizcord import (
    MAX_LEADERBOARD_LENGTH,
    MAX_LEADERBOARD_ROWS,
    extract_leaderboard,
    extract_quizcord_id,
    is_final_leaderboard,
    is_first_question_message,
    is_quizcord_author,
    parse_quiz_rows,
)


class QuizcordParserTests(unittest.TestCase):
    def test_detects_quizcord_bot_by_name_without_configured_id(self) -> None:
        author = SimpleNamespace(id=12345, bot=True, name="Quizcord App")
        self.assertTrue(is_quizcord_author(author, None, None))

    def test_detects_quizcord_webhook_by_name_without_configured_id(self) -> None:
        author = SimpleNamespace(id=12345, bot=False, name="Quizcord")
        self.assertTrue(is_quizcord_author(author, 67890, None))

    def test_detects_renamed_quizcord_bot_from_leaderboard_embed(self) -> None:
        author = SimpleNamespace(id=12345, bot=True, name="Quiz App")
        self.assertTrue(
            is_quizcord_author(author, None, None, has_final_leaderboard=True)
        )

    def test_ignores_regular_user_with_quizcord_in_name(self) -> None:
        author = SimpleNamespace(id=12345, bot=False, name="Quizcord")
        self.assertFalse(is_quizcord_author(author, None, None))

    def test_configured_id_overrides_name_detection(self) -> None:
        matching_author = SimpleNamespace(id=12345, bot=True, name="Other Bot")
        other_author = SimpleNamespace(id=67890, bot=True, name="Quizcord")
        self.assertTrue(is_quizcord_author(matching_author, None, 12345))
        self.assertFalse(is_quizcord_author(other_author, None, 12345))

    def test_parses_supported_leaderboard_formats(self) -> None:
        self.assertEqual(
            parse_quiz_rows(
                "#1. student_one — 950\n2. student.two - 825\n3 student_three: 700"
            ),
            [
                ("1", "student_one", "950"),
                ("2", "student.two", "825"),
                ("3", "student_three", "700"),
            ],
        )

    def test_rejects_oversized_input(self) -> None:
        with self.assertRaises(ValueError):
            parse_quiz_rows("x" * (MAX_LEADERBOARD_LENGTH + 1))

    def test_rejects_too_many_rows(self) -> None:
        payload = "\n".join(
            f"{rank}. student_{rank} - 100"
            for rank in range(1, MAX_LEADERBOARD_ROWS + 2)
        )
        with self.assertRaises(ValueError):
            parse_quiz_rows(payload)

    def test_extracts_quiz_id_and_embed_fields(self) -> None:
        embed = Mock()
        embed.title = "Final leaderboard"
        embed.description = ""
        embed.author.name = ""
        embed.fields = [SimpleField("Results", "1. student_one - 950")]
        embed.footer.text = "Quiz ID: 12345"
        message = Mock()
        message.content = ""
        message.embeds = [embed]
        message.created_at.timestamp.return_value = 100

        result = extract_leaderboard(message)

        self.assertIsNotNone(result)
        self.assertEqual(result[0], "12345")
        self.assertIn("1. student_one - 950", result[1])

    def test_extracts_nonfinal_leaderboard_update(self) -> None:
        embed = Mock()
        embed.title = "Leaderboard"
        embed.description = "1. student_one — 500"
        embed.author.name = ""
        embed.fields = []
        embed.footer.text = ""
        message = Mock()
        message.content = ""
        message.embeds = [embed]
        message.created_at.timestamp.return_value = 100

        result = extract_leaderboard(message)

        self.assertIsNotNone(result)
        self.assertEqual(
            parse_quiz_rows(result[1]), [("1", "student_one", "500")]
        )
        self.assertFalse(is_final_leaderboard(message))

    def test_reads_quiz_id_from_followup_embed(self) -> None:
        embed = Mock()
        embed.title = "Quiz complete"
        embed.description = ""
        embed.author.name = ""
        embed.fields = [SimpleField("Quiz ID", "123456")]
        embed.footer.text = ""
        message = Mock()
        message.content = ""
        message.embeds = [embed]

        self.assertEqual(extract_quizcord_id(message), "123456")

    def test_detects_question_one_as_quiz_start(self) -> None:
        embed = Mock()
        embed.title = "Question 1"
        embed.description = "Choose your answer"
        embed.fields = []
        message = Mock()
        message.content = ""
        message.embeds = [embed]

        self.assertTrue(is_first_question_message(message))

    def test_extracts_leaderboard_rows_from_message_content(self) -> None:
        message = Mock()
        message.content = "Final leaderboard #1. student_one — 805 Total players 1"
        message.embeds = []
        message.created_at.timestamp.return_value = 100

        result = extract_leaderboard(message)

        self.assertIsNotNone(result)
        self.assertEqual(
            parse_quiz_rows(result[1]), [("1", "student_one", "805")]
        )

    def test_extracts_bold_score_from_embed_description(self) -> None:
        embed = Mock()
        embed.title = "Final leaderboard"
        embed.description = "#1. chandu04381 — **799**"
        embed.author.name = ""
        embed.fields = [SimpleField("Total players", "1")]
        embed.footer.text = ""
        message = Mock()
        message.content = ""
        message.embeds = [embed]
        message.created_at.timestamp.return_value = 100

        result = extract_leaderboard(message)

        self.assertIsNotNone(result)
        self.assertEqual(len(embed.description), 25)
        self.assertEqual(
            parse_quiz_rows(result[1]), [("1", "chandu04381", "799")]
        )

    def test_parses_inline_leaderboard_with_multiword_nickname(self) -> None:
        self.assertEqual(
            parse_quiz_rows(
                "Final leaderboard #1. Chandu Nickname — 805 Total players 1"
            ),
            [("1", "Chandu Nickname", "805")],
        )

    def test_parses_bold_score_from_quizcord_embed(self) -> None:
        self.assertEqual(
            parse_quiz_rows("#1. chandu04381 — **799**"),
            [("1", "chandu04381", "799")],
        )

    def test_parses_at_prefixed_nickname_and_discord_mention(self) -> None:
        self.assertEqual(
            parse_quiz_rows(
                "#1. @Likith — **799**\n#2. <@!12345> — **650**"
            ),
            [
                ("1", "@Likith", "799"),
                ("2", "<@!12345>", "650"),
            ],
        )


class SimpleField:
    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value


if __name__ == "__main__":
    unittest.main()
