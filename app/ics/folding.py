"""Fold content lines at 75 UTF-8 octets without splitting code points."""


def fold_line(line: str) -> tuple[str, ...]:
    if len(line.encode("utf-8")) <= 75:
        return (line,)
    parts: list[str] = []
    remaining = line
    first = True
    while remaining:
        limit = 75 if first else 74
        size = 0
        index = 0
        for index, character in enumerate(remaining, start=1):
            encoded = len(character.encode("utf-8"))
            if size + encoded > limit:
                index -= 1
                break
            size += encoded
        else:
            index = len(remaining)
        if index <= 0:
            raise ValueError("one UTF-8 code point exceeds the folding limit")
        chunk, remaining = remaining[:index], remaining[index:]
        parts.append(chunk if first else f" {chunk}")
        first = False
    return tuple(parts)
