from datetime import date, timedelta


def is_weekend(day: date) -> bool:
    return day.weekday() > 5


def next_business_day(day: date) -> date:
    day += timedelta(days=1)
    while is_weekend(day):
        day += timedelta(days=1)
    return day
