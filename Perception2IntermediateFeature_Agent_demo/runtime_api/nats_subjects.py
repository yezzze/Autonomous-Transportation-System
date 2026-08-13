from typing import Iterable, List


def subject_pattern_covers(broad: str, narrow: str) -> bool:
    """Return whether every subject matched by narrow is also matched by broad."""
    broad_tokens = broad.split(".")
    narrow_tokens = narrow.split(".")

    for index, broad_token in enumerate(broad_tokens):
        if broad_token == ">":
            return index == len(broad_tokens) - 1 and index < len(narrow_tokens)
        if index >= len(narrow_tokens):
            return False

        narrow_token = narrow_tokens[index]
        if narrow_token == ">":
            return False
        if broad_token == "*":
            continue
        if broad_token != narrow_token:
            return False

    return len(broad_tokens) == len(narrow_tokens)


def merge_subject_patterns(*groups: Iterable[str]) -> List[str]:
    """Merge NATS subject patterns without retaining covered narrower patterns."""
    merged: List[str] = []
    for group in groups:
        for subject in group:
            if not subject:
                continue
            if any(subject_pattern_covers(existing, subject) for existing in merged):
                continue
            merged = [
                existing
                for existing in merged
                if not subject_pattern_covers(subject, existing)
            ]
            merged.append(subject)
    return merged
