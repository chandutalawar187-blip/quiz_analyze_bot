"""Parsing helpers for Quizcord leaderboard messages."""

import re

import discord


MAX_LEADERBOARD_LENGTH = 25_000
MAX_LEADERBOARD_ROWS = 500
FINAL_LEADERBOARD_MARKER = "FINAL_LEADERBOARD_MARKER"
DISCORD_MENTION_PATTERN = re.compile(r"<@!?\d+>")
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
        r"(?<!\S)#?(?P<rank>\d+)\.\s*(?P<username>.+?)\s+"
        r"(?:\*\*)?(?P<score>\d+)(?:\*\*)?\s*$"
    ),
)
UNRANKED_GROUP_PATTERN = re.compile(
    r"(?P<username>.+?)\s*(?:—|–|-|:)\s*"
    r"(?:\*\*)?(?P<score>\d+)(?:\*\*)?(?:\s+.*)?$"
)
RANKED_GROUP_WITHOUT_SCORE_PATTERN = re.compile(
    r"(?<!\S)#?(?P<rank>\d+)\.\s*(?P<username>.+?)\s*$"
)


def _split_grouped_usernames(username: str) -> list[str]:
    return [
        value
        for value in (
            item.strip().strip("*_`")
            for item in re.split(r",\s*(?=@|<@)", username)
        )
        if value
    ]


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
    normalized_payload = re.sub(
        r"final\s+leaderboard",
        f"\n{FINAL_LEADERBOARD_MARKER}\n",
        payload,
        flags=re.IGNORECASE,
    )
    highest_rank = 0
    after_final_marker = False
    pending_groups: list[tuple[str, list[str]]] = []
    for line in normalized_payload.splitlines():
        if line.strip() == FINAL_LEADERBOARD_MARKER:
            after_final_marker = True
            continue

        for pattern in QUIZ_ROW_PATTERNS:
            match = pattern.search(line.strip())
            if match:
                rank = match.group("rank")
                score = match.group("score")
                if (
                    after_final_marker
                    and pending_groups
                    and rank == "1"
                    and score == "0"
                ):
                    for pending_rank, usernames in pending_groups:
                        rows.extend(
                            (pending_rank, username, score)
                            for username in usernames
                        )
                    rank = pending_groups[-1][0]
                    pending_groups.clear()
                highest_rank = max(highest_rank, int(rank))
                usernames = _split_grouped_usernames(match.group("username"))
                rows.extend(
                    (rank, username, score)
                    for username in usernames
                )
                if len(rows) > MAX_LEADERBOARD_ROWS:
                    raise ValueError(
                        f"A leaderboard cannot contain more than {MAX_LEADERBOARD_ROWS} entries."
                    )
                after_final_marker = False
                break
        else:
            if not after_final_marker:
                continue
            line_value = line.strip()
            rank_only_match = RANKED_GROUP_WITHOUT_SCORE_PATTERN.fullmatch(
                line_value
            )
            if rank_only_match is not None:
                usernames = _split_grouped_usernames(
                    rank_only_match.group("username")
                )
                if len(usernames) > 1 and any(
                    DISCORD_MENTION_PATTERN.fullmatch(username)
                    for username in usernames
                ):
                    rank = rank_only_match.group("rank")
                    highest_rank = max(highest_rank, int(rank))
                    pending_groups.append((rank, usernames))
                continue

            match = UNRANKED_GROUP_PATTERN.fullmatch(line_value)
            if match is None:
                continue
            usernames = _split_grouped_usernames(match.group("username"))
            if len(usernames) < 2 or not any(
                DISCORD_MENTION_PATTERN.fullmatch(username)
                for username in usernames
            ):
                continue
            inferred_rank = (
                pending_groups[-1][0]
                if pending_groups
                else str(highest_rank + 1)
            )
            for rank, group_usernames in pending_groups:
                rows.extend(
                    (rank, username, match.group("score"))
                    for username in group_usernames
                )
            rows.extend(
                (inferred_rank, username, match.group("score"))
                for username in usernames
            )
            pending_groups.clear()
            highest_rank = max(highest_rank, int(inferred_rank))
            if len(rows) > MAX_LEADERBOARD_ROWS:
                raise ValueError(
                    f"A leaderboard cannot contain more than {MAX_LEADERBOARD_ROWS} entries."
                )
    return rows


def extract_leaderboard(message: discord.Message) -> tuple[str, str] | None:
    content = leaderboard_message_text(message)
    quiz_id = extract_quizcord_id(message)
    is_leaderboard = "leaderboard" in (message.content or "").casefold()
    for embed in message.embeds:
        title = embed.title or ""
        description = embed.description or ""
        if "leaderboard" in title.casefold() or "leaderboard" in description.casefold():
            is_leaderboard = True

    if not is_leaderboard:
        return None

    if len(content) > MAX_LEADERBOARD_LENGTH:
        raise ValueError(
            f"Quizcord embed text cannot exceed {MAX_LEADERBOARD_LENGTH} characters."
        )
    return quiz_id or f"Auto_{int(message.created_at.timestamp())}", content


def leaderboard_message_text(message: discord.Message) -> str:
    sections = [message.content or ""]
    for embed in message.embeds:
        sections.extend((embed.title or "", embed.description or ""))
        if embed.author.name:
            sections.append(embed.author.name)
        for field in embed.fields:
            sections.extend((field.name, field.value))
        if embed.footer.text:
            sections.append(embed.footer.text)
    return "\n".join(sections)


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


def is_final_leaderboard(message: discord.Message) -> bool:
    sections = [message.content or ""]
    for embed in message.embeds:
        sections.extend((embed.title or "", embed.description or ""))
        sections.extend(field.name + "\n" + field.value for field in embed.fields)
    return "final leaderboard" in "\n".join(sections).casefold()


def is_final_leaderboard_end(message: discord.Message) -> bool:
    return bool(
        re.search(
            r"\btotal\s+players\b",
            leaderboard_message_text(message),
            re.IGNORECASE,
        )
    )


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
