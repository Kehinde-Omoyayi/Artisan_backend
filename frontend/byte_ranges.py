"""Single byte-range support; authorization always happens before calling this."""

import re


def bounds(value, size):
    if not value:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or not any(match.groups()) or size <= 0:
        raise ValueError("Unsatisfiable range")
    left, right = match.groups()
    if left:
        start = int(left)
        end = min(int(right), size - 1) if right else size - 1
    else:
        suffix = int(right)
        if suffix <= 0:
            raise ValueError("Unsatisfiable range")
        start = max(0, size - suffix)
        end = size - 1
    if start >= size or start > end:
        raise ValueError("Unsatisfiable range")
    return start, end
