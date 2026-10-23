"""Who is using the dashboard. It's Deepa Nair's dashboard: no sign-in and no role picker.

Deepa (Head of Operations) reviews each Mon + Thu run, decides the exceptions (Gate 1) and approves the
reorder and pause lines (Gate 2), signs off the rules and logs write-offs. Every decision records her name.
The scheduled /api/run stays protected by CRON_SECRET.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    name: str = "Deepa Nair"
    role_label: str = "Head of Operations"

    @property
    def email(self):
        """What decisions record as decided_by (there are no accounts)."""
        return f"{self.name} (dashboard)"


DEEPA = User()


def current_user(request=None) -> User:
    return DEEPA
