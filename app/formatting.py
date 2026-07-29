"""Small display-string helpers shared by routes that produce
frontend-facing formatted fields (spine §7: the BFF never re-shapes
payloads, so this app must emit these strings itself)."""

from datetime import UTC, datetime


def relative_time(dt: datetime | None, *, never_label: str = "Never") -> str:
    if dt is None:
        return never_label
    seconds = (datetime.now(UTC) - dt).total_seconds()
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(hours // 24)
    if days < 7:
        return f"{days} day{'s' if days != 1 else ''} ago"
    weeks = int(days // 7)
    if weeks < 5:
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    months = int(days // 30)
    if months < 12:
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = int(days // 365)
    return f"{years} year{'s' if years != 1 else ''} ago"


def format_duration(start: datetime | None, end: datetime | None) -> str:
    if start is None:
        return "-"
    seconds = int(((end or datetime.now(UTC)) - start).total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m"
