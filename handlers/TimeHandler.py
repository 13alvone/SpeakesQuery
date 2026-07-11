import datetime
from croniter import croniter

class TimeHandler:
    @staticmethod
    def timestamp_to_cron(timestamp: str) -> str:
        """Convert an ISO formatted timestamp to a cron schedule."""
        if isinstance(timestamp, str):
            dt = datetime.datetime.fromisoformat(timestamp)
        elif isinstance(timestamp, datetime.datetime):
            dt = timestamp
        else:
            raise TypeError("timestamp must be a str or datetime instance")
        return f"{dt.minute} {dt.hour} {dt.day} {dt.month} *"

    @staticmethod
    def cron_to_timestamp(cron_string: str, start_time: datetime.datetime | None = None) -> str:
        """Return the next timestamp generated from a cron string."""
        start = start_time or datetime.datetime.now()
        try:
            itr = croniter(cron_string, start)
            next_dt = itr.get_next(datetime.datetime)
        except (ValueError, KeyError) as e:
            raise ValueError(f"Invalid cron string: {e}")
        return next_dt.strftime("%Y-%m-%d %H:%M:%S")
