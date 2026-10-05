"""/api/v1/location: place search, a place's coordinates, the address of a spot.

The success bodies are exactly what the app has always read; errors use the
common error shape (app/errors.py) with a proper status code.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from ...errors import ApiError
from ..deps import Caller, enforce_rate_limit, get_caller

router = APIRouter(prefix="/api/v1/location", tags=["location"])


@router.get("/search")
async def search(
    request: Request,
    response: Response,
    query: Annotated[str, Query(min_length=1, max_length=200, description="Text the person typed")],
    caller: Caller = Depends(get_caller),
) -> dict:
    await enforce_rate_limit(request, caller)
    results, how = await request.app.state.places.search(query)
    response.headers["X-Cache"] = how.upper()
    return {"status": "success", "results": results}


@router.get("/coordinates")
async def coordinates(
    request: Request,
    response: Response,
    place_id: Annotated[str, Query(min_length=1, max_length=256)],
    caller: Caller = Depends(get_caller),
) -> dict:
    await enforce_rate_limit(request, caller)
    found, how = await request.app.state.places.location(place_id)
    response.headers["X-Cache"] = how.upper()
    if found is None:
        raise ApiError(404, "place_not_found", "That place could not be located.")
    return {"status": "success", "latitude": found["lat"], "longitude": found["lng"]}


@router.get("/reverse")
async def reverse(
    request: Request,
    response: Response,
    lat: Annotated[float, Query(ge=-90, le=90)],
    lng: Annotated[float, Query(ge=-180, le=180)],
    caller: Caller = Depends(get_caller),
) -> dict:
    await enforce_rate_limit(request, caller)
    address, how = await request.app.state.places.address(lat, lng)
    response.headers["X-Cache"] = how.upper()
    if address is None:
        raise ApiError(404, "address_not_found", "No address is known for this spot.")
    return {"status": "success", "address": address}
