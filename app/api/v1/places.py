"""/api/v1/places/nearby: the police stations, hospitals, pharmacies and other safe places near a spot."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response

from ...domain.nearby import CATEGORY_NAMES, MAX_RADIUS_M, group_of
from ...errors import ApiError
from ..deps import Caller, enforce_rate_limit, get_caller

router = APIRouter(prefix="/api/v1/places", tags=["places"])

Category = Literal[CATEGORY_NAMES]  # type: ignore[valid-type]


@router.get("/nearby")
async def nearby(
    request: Request,
    response: Response,
    lat: Annotated[float, Query(ge=-90, le=90)],
    lng: Annotated[float, Query(ge=-180, le=180)],
    category: Annotated[Category, Query(description="hospital, police, fire_station, fuel, mechanic, toll, parking, atm or restaurant")],
    radius: Annotated[int, Query(ge=500, le=30000, description="Metres (toll booths are looked for from far, the rest within 10 km)")] = 3000,
    caller: Caller = Depends(get_caller),
) -> dict:
    if radius > MAX_RADIUS_M[group_of(category)]:
        raise ApiError(422, "invalid_request", "Invalid radius.")
    await enforce_rate_limit(request, caller, nearby=True)
    places, how = await request.app.state.nearby.find(category, lat, lng, radius)
    response.headers["X-Cache"] = how.upper()
    return {"status": "success", "category": category, "radius": radius, "places": places}
