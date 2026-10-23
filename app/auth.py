"""Who is using the dashboard. It's Raghav Kulkarni's dashboard: no sign-in and no role picker.

Raghav (Senior Demand Planner) reviews each Mon + Thu run, decides the exceptions (Gate 1), approves the
reorder and pause lines (Gate 2) and logs write-offs. Every decision records his name. Deepa Nair (Head of
Operations) still signs off the rules; that stays outside the dashboard.
The scheduled /api/run stays protected by CRON_SECRET.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    name: str = "Raghav Kulkarni"
    role_label: str = "Senior Demand Planner"

    @property
    def email(self):
        """What decisions record as decided_by (there are no accounts)."""
        return f"{self.name} (dashboard)"


RAGHAV = User()


def current_user(request=None) -> User:
    return RAGHAV
