"""A stand-in for Ola Maps for load tests: answers like it, after OLA_DELAY_MS.

    uvicorn loadtest.fake_ola:app --port 9001
"""

from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI

DELAY = float(os.environ.get("OLA_DELAY_MS", "200")) / 1000
app = FastAPI()


@app.get("/places/v1/autocomplete")
async def autocomplete(input: str):
    await asyncio.sleep(DELAY)
    return {"predictions": [{"description": f"{input} Road, Lucknow", "place_id": f"id-{input}"}]}


@app.get("/places/v1/details")
async def details(place_id: str):
    await asyncio.sleep(DELAY)
    return {"result": {"geometry": {"location": {"lat": 26.85, "lng": 80.94}}}}


@app.get("/places/v1/reverse-geocode")
async def reverse(latlng: str):
    await asyncio.sleep(DELAY)
    return {"results": [{"formatted_address": "1 Mall Road, Lucknow"}]}


@app.post("/routing/v1/directions")
async def directions(origin: str, destination: str, mode: str):
    await asyncio.sleep(DELAY)
    return {"routes": [{"legs": [{"duration": 1200, "distance": 5400}], "overview_polyline": "a" * 800}]}
