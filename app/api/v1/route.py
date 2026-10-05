"""/api/v1/route/calculate: a route and its safety check-in schedule."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from ...errors import ApiError
from ..deps import Caller, enforce_rate_limit, get_caller

router = APIRouter(prefix="/api/v1/route", tags=["route"])

# The app's travel modes and the Ola Maps mode each is routed with.
OLA_MODE: dict[str, str] = {
    "walking": "walking",
    "driving": "driving",  # cab
    "ride": "driving",  # two-wheeler
}


class RouteRequest(BaseModel):
    start_lat: float = Field(ge=-90, le=90)
    start_lng: float = Field(ge=-180, le=180)
    end_lat: float = Field(ge=-90, le=90)
    end_lng: float = Field(ge=-180, le=180)
    mode: Literal["walking", "driving", "ride"] = "walking"
    added_buffer_mins: int = Field(default=0, ge=0, le=240)
    # 0 is a real value: a trip re-planned after its last checkpoint was passed.
    checkpoint_count: int = Field(default=2, ge=0, le=50)


@router.post("/calculate")
async def calculate(
    request: Request,
    response: Response,
    body: RouteRequest,
    caller: Caller = Depends(get_caller),
) -> dict:
    await enforce_rate_limit(request, caller, route=True)
    result, how = await request.app.state.places.route(
        (body.start_lat, body.start_lng),
        (body.end_lat, body.end_lng),
        body.mode,
        OLA_MODE[body.mode],
        body.added_buffer_mins,
        body.checkpoint_count,
    )
    response.headers["X-Cache"] = how.upper()
    if result is None:
        raise ApiError(404, "no_route", "No route was found between these points.")
    return result
