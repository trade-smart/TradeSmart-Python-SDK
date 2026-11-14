"""``UserDetails`` endpoint."""

from __future__ import annotations

from tradesmart.endpoints.base import Call, EndpointSpec, parse_object
from tradesmart.models.user import UserDetails

USER_DETAILS: EndpointSpec[UserDetails] = EndpointSpec(
    name="UserDetails",
    parse=parse_object(UserDetails, "UserDetails"),
)


def user_details_call(uid: str) -> Call[UserDetails]:
    """Build the ``UserDetails`` call."""
    return Call(USER_DETAILS, {"uid": uid})
