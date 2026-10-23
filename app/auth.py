"""Who is acting on the dashboard. There is no sign-in: the header's "Acting as" picker sets the role.

Each role still only gets its own buttons (Gate 1 for Raghav, Gate 2 for Procurement, sign-off for Deepa),
and every decision records the role that made it. Anyone with the link can pick any role, so this suits a
demo or a private link; the scheduled /api/run stays protected by CRON_SECRET.
"""
from dataclasses import dataclass

from fastapi import HTTPException, Request

COOKIE = "dp_role"
MAX_AGE = 30 * 24 * 3600

ROLES = {
    "raghav": ("Raghav Kulkarni", "Senior Demand Planner"),
    "procurement": ("Procurement team", "Procurement"),
    "deepa": ("Deepa Nair", "Head of Operations"),
    "it": ("IT team", "IT"),
    "viewer": ("Viewer", "Read only"),
}
DEFAULT_ROLE = "raghav"

CAN_GATE1 = {"raghav"}
CAN_GATE2 = {"procurement"}
CAN_RUN = {"raghav", "it"}
CAN_LOG_OUTCOME = {"raghav", "procurement"}
CAN_SIGN = {"deepa"}


@dataclass
class User:
    role: str

    @property
    def name(self):
        return ROLES[self.role][0]

    @property
    def role_label(self):
        return ROLES[self.role][1]

    @property
    def email(self):
        """What decisions record as decided_by (there are no accounts)."""
        return f"{self.name} (dashboard)"

    @property
    def roles(self):
        return {r: n for r, (n, _) in ROLES.items()}


def current_user(request: Request) -> User:
    role = request.cookies.get(COOKIE, DEFAULT_ROLE)
    return User(role if role in ROLES else DEFAULT_ROLE)


class RoleDenied(HTTPException):
    """The acting role can't do this; the app shows it inside the dashboard, not as a bare error."""

    def __init__(self, user: User, allowed: set):
        self.user = user
        self.allowed = [ROLES[r][0] for r in ROLES if r in allowed]
        super().__init__(status_code=403, detail=f"{user.name} can't do this. Switch role in the header.")


def require(user: User, allowed: set) -> None:
    if user.role not in allowed:
        raise RoleDenied(user, allowed)
