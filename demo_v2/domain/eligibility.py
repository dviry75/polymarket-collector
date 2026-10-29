"""Pure entry-only schedule/window predicates using verified instance times."""

from zoneinfo import ZoneInfo


def scheduled(spec, at):
    local = at.astimezone(ZoneInfo("Asia/Jerusalem"))
    weekday, clock = local.weekday(), local.time().replace(tzinfo=None)
    if not spec.weekly_schedule:
        return True
    for interval in spec.weekly_schedule:
        if interval.start < interval.end:
            if weekday == interval.weekday and interval.start <= clock < interval.end:
                return True
        elif (weekday == interval.weekday and clock >= interval.start) or (
                weekday == (interval.weekday+1)%7 and clock < interval.end):
            return True
    return False


def entry_eligible(spec, instance, at):
    if not spec.enabled or spec.archived or instance.definition.key not in spec.markets:
        return False
    if not instance.verified or instance.status != "OPEN" or instance.resolution_status != "PENDING":
        return False
    if not instance.starts_at <= at < instance.ends_at or not scheduled(spec, at):
        return False
    elapsed = (at-instance.starts_at).total_seconds()
    return not spec.entry_windows or any(w.start_seconds <= elapsed < w.end_seconds for w in spec.entry_windows)
