import re
from typing import Optional

from email_validator import EmailNotValidError, validate_email


LENIENT_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+$")


def normalize_email_input(
    email: Optional[str],
    *,
    required: bool = False,
    field_label: str = "邮箱",
) -> Optional[str]:
    """Normalize user-provided emails without requiring a dotted domain."""
    if email is None:
        if required:
            raise ValueError(f"请输入{field_label}")
        return None

    normalized = str(email).strip().lower()
    if not normalized:
        if required:
            raise ValueError(f"请输入{field_label}")
        return None

    if not LENIENT_EMAIL_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_label}格式不正确，需包含 @ 且不能包含空格")

    return normalized


def normalize_invite_email_input(
    email: Optional[str],
    *,
    field_label: str = "邮箱",
) -> str:
    """Normalize emails for invite APIs that require RFC-style dotted domains."""
    normalized = normalize_email_input(email, required=True, field_label=field_label)
    try:
        result = validate_email(normalized, check_deliverability=False)
    except EmailNotValidError:
        raise ValueError(f"{field_label}不被邀请接口接受，@ 后域名必须包含 .")
    return result.normalized
