"""Parsing helpers for Quizcord leaderboard messages."""

import re

import discord


MAX_LEADERBOARD_LENGTH = 6000
MAX_LEADERBOARD_ROWS = 500
QUIZ_ROW_PATTERNS = (
    re.compile(
        r"(?<!\S)#?(?P<rank>\d+)\.\s*(?P<username>.+?)\s*"
        r"(?:—|–|-)\s*(?:\*\*)?(?P<score>\d+)(?:\*\*)?(?:\s+.*)?$"
    ),
    re.compile(
        r"(?<!\S)#?(?P<rank>\d+)\s+(?P<username>.+?)\s*"
        r"(?:—|–|-|:)\s*(?:\*\*)?(?P<score>\d+)(?:\*\*)?(?:\s+.*)?$"
    ),
    re.compile(
        r"(?<!\S)#?(?P<rank>\d+)\.\s*(?P<username>.+?)"
        r".*?(?:\*\*)?(?P<score>\d+)(?:\*\*)?\s*$"
    ),
)


def is_quizcord_author(
    author: discord.abc.User,
    webhook_id: int | None,
    configured_bot_id: int | None,
    has_final_leaderboard: bool = False,
) -> bool:
    if not author.bot and webhook_id is None:
        return False
    if configured_bot_id is not None:
        return author.id == configured_bot_id
    return "quizcord" in author.name.casefold() or has_final_leaderboard


def parse_quiz_rows(payload: str) -> list[tuple[str, str, str]]:
    if len(payload) > MAX_LEADERBOARD_LENGTH:
        raise ValueError(
            f"Leaderboard text cannot exceed {MAX_LEADERBOARD_LENGTH} characters."
        )

    rows: list[tuple[str, str, str]] = []
    for line in payload.splitlines():
        for pattern in QUIZ_ROW_PATTERNS:
            match = pattern.search(line.strip())
            if match:
                rows.append(
                    (
                        match.group("rank"),
                        match.group("username").strip().strip("*_`"),
                        match.group("score"),
                    )
                )
                if len(rows) > MAX_LEADERBOARD_ROWS:
                    raise ValueError(
                        f"A leaderboard cannot contain more than {MAX_LEADERBOARD_ROWS} entries."
                    )
                break
    return rows


def extract_leaderboard(message: discord.Message) -> tuple[str, str] | None:
    sections: list[str] = []
    is_leaderboard = False
    quiz_id = extract_quizcord_id(message)

    message_content = message.content or ""
    if message_content:
        if "leaderboard" in message_content.casefold():
            is_leaderboard = True
        sections.append(message_content)

    for embed in message.embeds:
        title = embed.title or ""
        description = embed.description or ""
        if "leaderboard" in title.casefold() or "leaderboard" in description.casefold():
            is_leaderboard = True
        sections.extend((title, description))
        if embed.author.name:
            sections.append(embed.author.name)
        for field in embed.fields:
            sections.extend((field.name, field.value))
        if embed.footer.text:
            sections.append(embed.footer.text)

    if not is_leaderboard:
        return None

    payload = "\n".join(sections)
    if len(payload) > MAX_LEADERBOARD_LENGTH:
        raise ValueError(
            f"Quizcord embed text cannot exceed {MAX_LEADERBOARD_LENGTH} characters."
        )
    return quiz_id or f"Auto_{int(message.created_at.timestamp())}", payload


def extract_quizcord_id(message: discord.Message) -> str | None:
    sections = [message.content or ""]
    for embed in message.embeds:
        sections.extend((embed.title or "", embed.description or ""))
        sections.extend(field.name + "\n" + field.value for field in embed.fields)
        sections.extend((embed.author.name or "", embed.footer.text or ""))
    match = re.search(
        r"\bQuiz\s*ID\s*[:#]?\s*(\d+)\b",
        "\n".join(sections),
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def is_quiz_start_message(message: discord.Message) -> bool:
    sections = [message.content or ""]
    for embed in message.embeds:
        sections.extend((embed.title or "", embed.description or ""))
        sections.extend(field.name + "\n" + field.value for field in embed.fields)
    content = "\n".join(sections)
    return bool(
        re.search(r"\bquestion\s*#?\s*1\b", content, re.IGNORECASE)
        or re.search(r"\bquiz\s+(?:has\s+)?started\b", content, re.IGNORECASE)
    )


def is_first_question_message(message: discord.Message) -> bool:
    sections = [message.content or ""]
    for embed in message.embeds:
        sections.extend((embed.title or "", embed.description or ""))
        sections.extend(field.name + "\n" + field.value for field in embed.fields)
    return bool(
        re.search(r"\bquestion\s*#?\s*1\b", "\n".join(sections), re.IGNORECASE)
    )


def is_final_leaderboard(message: discord.Message) -> bool:
    sections = [message.content or ""]
    for embed in message.embeds:
        sections.extend((embed.title or "", embed.description or ""))
        sections.extend(field.name + "\n" + field.value for field in embed.fields)
    return "final leaderboard" in "\n".join(sections).casefold()


def leaderboard_structure(message: discord.Message) -> str:
    embed_shapes = [
        {
            "title_chars": len(embed.title or ""),
            "description_chars": len(embed.description or ""),
            "author_name_chars": len(embed.author.name or ""),
            "footer_text_chars": len(embed.footer.text or ""),
            "field_count": len(embed.fields),
            "fields": [
                {
                    "name_chars": len(field.name),
                    "value_chars": len(field.value),
                }
                for field in embed.fields
            ],
        }
        for embed in message.embeds
    ]
    return (
        f"content_chars={len(message.content or '')}, "
        f"embed_count={len(message.embeds)}, embed_shapes={embed_shapes}"
    )
