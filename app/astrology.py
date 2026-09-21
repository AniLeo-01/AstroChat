import re
from datetime import date, datetime

# ponytail: tropical sun sign only; a real engine (sidereal rashi, moon sign, nakshatra) replaces this module.
_SIGN_ENDS = [
    (1, 19, "Capricorn"), (2, 18, "Aquarius"), (3, 20, "Pisces"), (4, 19, "Aries"),
    (5, 20, "Taurus"), (6, 20, "Gemini"), (7, 22, "Cancer"), (8, 22, "Leo"),
    (9, 22, "Virgo"), (10, 22, "Libra"), (11, 21, "Scorpio"), (12, 21, "Sagittarius"),
    (12, 31, "Capricorn"),
]
_DATE_FORMATS = ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%B %d %Y", "%d/%m/%Y")


def sun_sign(dob: date) -> str:
    return next(sign for month, day, sign in _SIGN_ENDS if (dob.month, dob.day) <= (month, day))


def parse_date(text: str) -> date | None:
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text.strip())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None
