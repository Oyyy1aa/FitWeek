"""Small strict validator for the Phase 6B ICS subset."""


def validate_ics(content: bytes) -> None:
    if content.startswith(b"\xef\xbb\xbf"):
        raise ValueError("ICS must not contain a BOM")
    text = content.decode("utf-8")
    if not text.endswith("\r\n") or "\n" in text.replace("\r\n", ""):
        raise ValueError("ICS must use CRLF line endings")
    lines = text.split("\r\n")[:-1]
    if not lines or lines[0] != "BEGIN:VCALENDAR" or lines[-1] != "END:VCALENDAR":
        raise ValueError("ICS calendar envelope is invalid")
    if any(len(line.encode("utf-8")) > 75 for line in lines):
        raise ValueError("ICS content line exceeds 75 octets")
    if lines.count("BEGIN:VEVENT") != lines.count("END:VEVENT"):
        raise ValueError("ICS VEVENT envelope is invalid")
    for required in ("VERSION:2.0", "CALSCALE:GREGORIAN"):
        if required not in lines:
            raise ValueError(f"ICS is missing {required}")
