"""Nearby safe places, end to end against a pretend OpenStreetMap (Overpass). Nothing here is paid."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from app.domain import nearby as domain
from tests.conftest import body, client_for, make_settings

# Lucknow, near Hazratganj. 0.001 degrees of latitude is about 111 m.
LAT, LNG = 26.8467, 80.9462

# What each kind of place is on the map.
TAGS = {
    "police": {"amenity": "police"},
    "hospital": {"amenity": "hospital"},
    "fire_station": {"amenity": "fire_station"},
    "fuel": {"amenity": "fuel"},
    "mechanic": {"shop": "car_repair"},
    "toll": {"barrier": "toll_booth"},
    "parking": {"amenity": "parking"},
    "atm": {"amenity": "atm"},
    "restaurant": {"amenity": "restaurant"},
}


def node(name, dlat, dlng=0.0, kind="police", **tags):
    return {"type": "node", "id": abs(hash((name, dlat, dlng, kind))) % 10**9, "lat": LAT + dlat, "lon": LNG + dlng,
            "tags": {"name": name, **TAGS[kind], **tags}}


def ask(client, **params):
    base = {"lat": LAT, "lng": LNG, "category": "police"}
    base.update(params)
    return client.get("/api/v1/places/nearby", params=base)


def one_place():
    return [node("Hazratganj Police Station", 0.004, phone="+91 522 222 0000; +91 98100 00000")]


# ─── the answer the app reads ───

def test_places_come_back_nearest_first_with_what_the_app_shows(ola, overpass):
    overpass.elements = [
        node("Far Chowki", 0.02),
        node("Hazratganj Police Station", 0.004, phone="+91 522 222 0000; +91 98100 00000", opening_hours="24/7",
             **{"addr:housenumber": "12", "addr:street": "MG Marg", "addr:city": "Lucknow"}),
        node("Near Post", 0.001),
    ]
    with client_for(ola, overpass=overpass) as client:
        r = ask(client)
    assert r.status_code == 200
    answer = body(r)
    assert answer["status"] == "success"
    assert answer["category"] == "police"
    assert answer["radius"] == 3000
    names = [p["name"] for p in answer["places"]]
    assert names == ["Near Post", "Hazratganj Police Station", "Far Chowki"]
    station = answer["places"][1]
    assert station["phone"] == "+91 522 222 0000", "the first of two numbers"
    assert station["address"] == "12 MG Marg, Lucknow"
    assert station["open_24h"] is True
    assert station["category"] == "police"
    assert 400 < station["distance_m"] < 500
    assert answer["places"][0]["distance_m"] < answer["places"][1]["distance_m"]


def test_each_kind_gets_only_its_own_places_from_a_group_answer(ola, overpass):
    overpass.elements = [
        node("City Hospital", 0.003, kind="hospital"),
        node("Civil Lines Police", 0.002, kind="police"),
        node("Fire Brigade", 0.004, kind="fire_station"),
    ]
    with client_for(ola, overpass=overpass) as client:
        assert [p["name"] for p in body(ask(client, category="hospital"))["places"]] == ["City Hospital"]
        assert [p["name"] for p in body(ask(client, category="police"))["places"]] == ["Civil Lines Police"]
        assert [p["name"] for p in body(ask(client, category="fire_station"))["places"]] == ["Fire Brigade"]


def test_distances_are_from_the_exact_spot_not_from_the_square(ola, overpass):
    overpass.elements = [node("Post", 0.003)]
    with client_for(ola, overpass=overpass) as client:
        a = body(ask(client, lat=LAT, lng=LNG))["places"][0]["distance_m"]
        # Another person in the same ~1 km square, 80 m to the north: the same cached answer, their own distance.
        b = body(ask(client, lat=LAT + 0.0007, lng=LNG))["places"][0]["distance_m"]
    assert len(overpass.calls) == 1
    assert 300 < a < 370
    assert 230 < b < 290
    assert a - b > 50


def test_only_places_within_the_radius_are_returned(ola, overpass):
    overpass.elements = [node("Near", 0.005), node("Edge", 0.0265), node("Beyond", 0.04)]
    with client_for(ola, overpass=overpass) as client:
        names = [p["name"] for p in body(ask(client, radius=3000))["places"]]
        wider = [p["name"] for p in body(ask(client, radius=6000))["places"]]
    assert names == ["Near", "Edge"]
    assert wider == ["Near", "Edge", "Beyond"]


def test_at_most_twenty_places(ola, overpass):
    overpass.elements = [node(f"Post {i}", 0.0001 * (i + 1), 0.0001 * i) for i in range(35)]
    with client_for(ola, overpass=overpass) as client:
        places = body(ask(client))["places"]
    assert len(places) == 20
    assert [p["distance_m"] for p in places] == sorted(p["distance_m"] for p in places)


def test_a_place_with_no_name_or_phone_is_still_shown(ola, overpass):
    overpass.elements = [{"type": "node", "id": 1, "lat": LAT + 0.002, "lon": LNG, "tags": {"amenity": "police"}}]
    with client_for(ola, overpass=overpass) as client:
        place = body(ask(client))["places"][0]
    assert place["name"] == ""
    assert place["phone"] is None
    assert place["address"] is None
    assert place["open_24h"] is None


def test_buildings_use_their_centre_and_things_without_a_position_are_dropped(ola, overpass):
    overpass.elements = [
        {"type": "way", "id": 5, "center": {"lat": LAT + 0.003, "lon": LNG}, "tags": {"name": "Station Building", "amenity": "police"}},
        {"type": "relation", "id": 6, "tags": {"name": "No position", "amenity": "police"}},
        {"type": "node", "id": 7, "lat": "x", "lon": LNG, "tags": {"name": "Bad lat", "amenity": "police"}},
        {"type": "node", "id": 8, "lat": 120.0, "lon": LNG, "tags": {"name": "Off the planet", "amenity": "police"}},
        {"type": "node", "id": 9, "lat": LAT + 0.001, "lon": LNG, "tags": {"name": "Not a kind we look for", "amenity": "bench"}},
        "not an element",
    ]
    with client_for(ola, overpass=overpass) as client:
        names = [p["name"] for p in body(ask(client))["places"]]
    assert names == ["Station Building"]


def test_the_same_place_as_a_point_and_as_a_building_is_one_place(ola, overpass):
    overpass.elements = [
        node("Civil Hospital", 0.004, kind="hospital"),
        {"type": "way", "id": 9, "center": {"lat": LAT + 0.004, "lon": LNG}, "tags": {"name": "Civil Hospital", "amenity": "hospital"}},
    ]
    with client_for(ola, overpass=overpass) as client:
        places = body(ask(client, category="hospital"))["places"]
    assert len(places) == 1


@pytest.mark.parametrize(
    "tags, expected",
    [
        ({"phone": "+91 11 2345 6789; +91 98100 00000"}, "+91 11 2345 6789"),
        ({"contact:phone": "100"}, "100"),
        ({"phone": "n/a"}, None),
        ({"phone": 5}, None),
        ({"contact:mobile": "+91 98100 00000"}, "+91 98100 00000"),
    ],
)
def test_the_phone_number_is_the_first_real_one(tags, expected, ola, overpass):
    overpass.elements = [node("Post", 0.002, **tags)]
    with client_for(ola, overpass=overpass) as client:
        assert body(ask(client))["places"][0]["phone"] == expected


def test_a_hospital_with_an_emergency_department_says_so(ola, overpass):
    overpass.elements = [node("City Hospital", 0.002, kind="hospital", emergency="yes"), node("Small Hospital", 0.003, kind="hospital")]
    with client_for(ola, overpass=overpass) as client:
        places = body(ask(client, category="hospital"))["places"]
    assert places[0].get("emergency") is True
    assert "emergency" not in places[1]


def test_a_petrol_pump_that_sells_cng_carries_a_cng_badge(ola, overpass):
    overpass.elements = [
        node("Tagged Pump", 0.001, kind="fuel", **{"fuel:cng": "yes"}),
        node("Indraprastha CNG Station", 0.002, kind="fuel"),
        node("Plain Pump", 0.003, kind="fuel", **{"fuel:cng": "no"}),
    ]
    with client_for(ola, overpass=overpass) as client:
        places = {p["name"]: p for p in body(ask(client, category="fuel"))["places"]}
    assert places["Tagged Pump"]["cng"] is True
    assert places["Indraprastha CNG Station"]["cng"] is True
    assert "cng" not in places["Plain Pump"]


def test_every_roadside_kind_is_read_from_its_own_tags(ola, overpass):
    overpass.elements = [
        node("A Pump", 0.001, kind="fuel"),
        node("A Garage", 0.002, kind="mechanic"),
        {"type": "node", "id": 71, "lat": LAT + 0.003, "lon": LNG, "tags": {"name": "A Bike Garage", "shop": "motorcycle_repair"}},
        node("A Car Park", 0.004, kind="parking"),
        node("An ATM", 0.005, kind="atm"),
        node("A Diner", 0.006, kind="restaurant"),
        {"type": "node", "id": 72, "lat": LAT + 0.007, "lon": LNG, "tags": {"name": "A Shop", "shop": "bakery"}},
    ]
    with client_for(ola, overpass=overpass) as client:
        got = {k: sorted(p["name"] for p in body(ask(client, category=k))["places"]) for k in ("fuel", "mechanic", "parking", "atm", "restaurant")}
    assert got == {"fuel": ["A Pump"], "mechanic": ["A Bike Garage", "A Garage"], "parking": ["A Car Park"], "atm": ["An ATM"], "restaurant": ["A Diner"]}


def test_toll_booths_are_looked_for_from_far_away(ola, overpass):
    overpass.elements = [node("Expressway Toll Plaza", 0.15, kind="toll")]  # about 16 km
    with client_for(ola, overpass=overpass) as client:
        r = ask(client, category="toll", radius=25000)
    assert r.status_code == 200
    assert [p["name"] for p in body(r)["places"]] == ["Expressway Toll Plaza"]
    assert "around:25900" in overpass.queries()[0]


# ─── what is asked of OpenStreetMap ───

def test_openstreetmap_is_never_told_the_exact_position(ola, overpass):
    overpass.elements = one_place()
    with client_for(ola, overpass=overpass) as client:
        ask(client, lat=26.84671234, lng=80.94625678)
    query = overpass.queries()[0]
    assert "26.84671234" not in query and "80.94625678" not in query
    assert "around:3900,26.845,80.945" in query, "the centre of the ~1 km square, with the margin on the radius"


def test_one_question_names_every_kind_of_its_group_with_its_own_limit(ola, overpass):
    overpass.elements = one_place()
    with client_for(ola, overpass=overpass) as client:
        ask(client, category="police")
        ask(client, category="fuel")
        ask(client, category="toll", radius=20000)
    emergency, roadside, highway = overpass.queries()
    for wanted in ('"amenity"="police"', '"amenity"="hospital"', '"healthcare"="hospital"', '"amenity"="fire_station"'):
        assert wanted in emergency, wanted
    for wanted in ('"amenity"="fuel"', '"shop"~"car_repair|motorcycle_repair"', '"amenity"="parking"', '"amenity"="atm"', '"amenity"="restaurant"'):
        assert wanted in roadside, wanted
    assert '"barrier"="toll_booth"' in highway
    assert '"amenity"="police"' not in roadside and '"amenity"="parking"' not in emergency
    assert '"amenity"="fuel"' not in highway, "tolls are asked alone: they are wanted from far away"
    for query in (emergency, roadside, highway):
        assert query.startswith("[out:json]")
        assert "out tags center qt" in query
    # The busy kinds are capped harder, so they cannot crowd out a rare one in the same question.
    assert ".parking out tags center qt 50;" in roadside
    assert ".mechanic out tags center qt 40;" in roadside


def test_the_question_is_posted_with_an_identifying_user_agent(ola, overpass):
    overpass.elements = one_place()
    with client_for(ola, overpass=overpass) as client:
        ask(client)
    call = overpass.calls[0]
    assert call.method == "POST"
    assert call.headers["user-agent"].startswith("sosafe-backend/")


# ─── keeping answers (this is what keeps it free) ───

def test_one_lookup_serves_every_kind_of_the_group_and_everyone_in_the_square(ola, overpass):
    overpass.elements = [node("Hospital", 0.003, kind="hospital"), node("Police", 0.002), node("Brigade", 0.004, kind="fire_station")]
    with client_for(ola, overpass=overpass) as client:
        first = ask(client, category="hospital")
        second = ask(client, category="police")
        third = ask(client, category="fire_station", lat=LAT + 0.0005, lng=LNG + 0.0004)
    assert len(overpass.calls) == 1
    assert [first.headers["x-cache"], second.headers["x-cache"], third.headers["x-cache"]] == ["MISS", "HIT", "HIT"]


def test_another_group_square_or_radius_is_a_new_question(ola, overpass):
    overpass.elements = one_place()
    with client_for(ola, overpass=overpass) as client:
        ask(client)
        ask(client, category="fuel")  # another group
        ask(client, lat=LAT + 0.05)  # another square
        ask(client, radius=5000)  # another radius
    assert len(overpass.calls) == 4


def test_an_empty_answer_is_not_kept(ola, overpass):
    overpass.elements = []
    with client_for(ola, overpass=overpass) as client:
        assert body(ask(client))["places"] == []
        overpass.elements = one_place()
        assert len(body(ask(client))["places"]) == 1
    assert len(overpass.calls) == 2


def test_a_group_with_only_some_kinds_is_kept_so_the_empty_kinds_cost_nothing_either(ola, overpass):
    overpass.elements = one_place()  # police only
    with client_for(ola, overpass=overpass) as client:
        assert body(ask(client, category="hospital"))["places"] == []
        assert len(body(ask(client, category="police"))["places"]) == 1
    assert len(overpass.calls) == 1


def test_with_redis_the_answer_is_shared_and_kept_for_a_week_to_stand_in_when_the_map_servers_are_down(ola, overpass, fake_redis):
    overpass.elements = one_place()
    with client_for(ola, overpass=overpass, redis=fake_redis) as client:
        ask(client)
    keys = [k for k in fake_redis.data if ":nearby:emergency:" in k]
    assert len(keys) == 1
    assert fake_redis.ttl[keys[0]] == 7 * 24 * 3600
    assert ":nearby:emergency:" in keys[0], "kept for the group"
    assert set(json.loads(fake_redis.data[keys[0]])) == {"at", "kinds"}, "stamped with when it was fetched"
    assert str(LAT) not in keys[0], "the key names the square, not the person"


def test_the_kept_answer_can_be_switched_off(ola, overpass):
    overpass.elements = one_place()
    settings = make_settings(cache_enabled=False)
    with client_for(ola, settings, overpass=overpass) as client:
        ask(client)
        ask(client)
    assert len(overpass.calls) == 2


# ─── when OpenStreetMap does not answer ───

def server_down(status):
    return lambda request: httpx.Response(status, text="busy")


def test_the_second_server_is_used_when_the_first_fails(ola, overpass):
    overpass.elements = one_place()
    seen = []

    def first_fails(request):
        seen.append(str(request.url))
        return httpx.Response(503, text="busy") if len(seen) == 1 else None

    overpass.override = first_fails
    with client_for(ola, overpass=overpass) as client:
        r = ask(client)
    assert r.status_code == 200
    assert len(body(r)["places"]) == 1
    assert seen[0] != seen[1]


def test_both_down_is_a_clear_error_that_is_not_kept(ola, overpass):
    overpass.override = server_down(503)
    with client_for(ola, overpass=overpass) as client:
        r = ask(client)
        assert r.status_code == 502
        assert body(r)["error"]["code"] == "places_error"
        overpass.override = None
        overpass.elements = one_place()
        assert ask(client).status_code == 200, "the failure was not remembered"


def test_a_timeout_is_a_gateway_timeout(ola, overpass):
    overpass.override = lambda request: httpx.ReadTimeout("slow", request=request)
    with client_for(ola, overpass=overpass) as client:
        r = ask(client)
    assert r.status_code == 504
    assert body(r)["error"]["code"] == "places_timeout"


def test_a_busy_server_that_says_too_many_requests_is_a_failure_too(ola, overpass):
    overpass.override = server_down(429)
    with client_for(ola, overpass=overpass) as client:
        assert ask(client).status_code == 502


def test_a_connection_failure_is_an_error_too(ola, overpass):
    overpass.override = lambda request: httpx.ConnectError("no route", request=request)
    with client_for(ola, overpass=overpass) as client:
        assert ask(client).status_code == 502


def test_an_answer_cut_short_by_a_runtime_error_is_not_trusted(ola, overpass):
    overpass.override = lambda request: httpx.Response(200, json={"elements": one_place(), "remark": "runtime error: Query timed out"})
    with client_for(ola, overpass=overpass) as client:
        r = ask(client)
    assert r.status_code == 502


def test_garbage_is_not_trusted(ola, overpass):
    for response in (
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=["a list"]),
        httpx.Response(200, json={"elements": "nope"}),
    ):
        overpass.override = lambda request, response=response: response
        with client_for(ola, overpass=overpass) as client:
            assert ask(client).status_code == 502


def test_after_a_run_of_failures_it_is_left_alone_for_a_while(ola, overpass):
    overpass.override = server_down(503)
    settings = make_settings(breaker_failures=2, breaker_cooldown_seconds=60.0)
    with client_for(ola, settings, overpass=overpass) as client:
        ask(client, lat=LAT)
        ask(client, lat=LAT + 0.1)
        before = len(overpass.calls)
        r = ask(client, lat=LAT + 0.2)
    assert r.status_code == 503
    assert body(r)["error"]["code"] == "places_unavailable"
    assert r.headers["retry-after"] == "15"
    assert len(overpass.calls) == before, "no call at all while it rests"


def test_nothing_from_inside_the_server_reaches_the_person(ola, overpass):
    overpass.override = lambda request: httpx.Response(500, text="Traceback: secret.internal.host")
    with client_for(ola, overpass=overpass) as client:
        r = ask(client)
    assert "secret" not in r.text and "overpass" not in r.text.lower()


# ─── the request itself ───

@pytest.mark.parametrize(
    "params",
    [
        {"category": "casino"},
        {"category": "pharmacy"},  # no longer offered
        {"category": "ambulance"},  # no free data: not offered
        {"category": ""},
        {"lat": 91},
        {"lat": -91},
        {"lng": 181},
        {"radius": 100},
        {"radius": 50000},
        {"category": "hospital", "radius": 15000},  # only toll booths may be asked from that far
        {"category": "restaurant", "radius": 10001},
        {"category": "toll", "radius": 30001},
        {"lat": "north"},
    ],
)
def test_bad_requests_are_refused_without_asking_openstreetmap(params, ola, overpass):
    with client_for(ola, overpass=overpass) as client:
        r = ask(client, **params)
    assert r.status_code == 422
    assert body(r)["error"]["code"] == "invalid_request"
    assert overpass.calls == []


def test_every_kind_the_app_offers_is_accepted(ola, overpass):
    overpass.elements = one_place()
    with client_for(ola, overpass=overpass) as client:
        for kind in ("hospital", "police", "fire_station", "fuel", "mechanic", "toll", "parking", "atm", "restaurant"):
            assert ask(client, category=kind, radius=10000 if kind != "toll" else 25000).status_code == 200, kind


def test_missing_parts_are_refused(ola, overpass):
    with client_for(ola, overpass=overpass) as client:
        assert client.get("/api/v1/places/nearby", params={"lat": LAT, "lng": LNG}).status_code == 422
        assert client.get("/api/v1/places/nearby", params={"category": "police"}).status_code == 422
    assert overpass.calls == []


def test_a_signed_out_phone_is_refused_when_sign_in_is_required(ola, overpass):
    settings = make_settings(auth_mode="required", firebase_project_id="demo")
    with client_for(ola, settings, overpass=overpass) as client:
        r = ask(client)
    assert r.status_code == 401
    assert overpass.calls == []


def test_the_rate_limit_applies(ola, overpass):
    overpass.elements = one_place()
    settings = make_settings(rate_limit_anonymous_per_minute=3)
    with client_for(ola, settings, overpass=overpass) as client:
        codes = [ask(client).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429]


# ─── the domain pieces ───

def test_every_kind_belongs_to_exactly_one_group():
    members = [c for group in domain.GROUPS.values() for c in group]
    assert sorted(members) == sorted(domain.CATEGORY_NAMES)
    assert len(members) == len(set(members))
    for category in domain.CATEGORY_NAMES:
        assert category in domain.GROUPS[domain.group_of(category)]
    assert set(domain.MAX_RADIUS_M) == set(domain.GROUPS)


def test_things_that_cannot_be_had_for_free_are_not_offered():
    for gone in ("ambulance", "towing", "cng", "pharmacy", "transit"):
        assert gone not in domain.CATEGORY_NAMES


def test_a_place_is_sorted_into_the_first_kind_of_its_group_that_fits():
    assert domain.categorize({"amenity": "atm"}, "roadside") == "atm"
    assert domain.categorize({"amenity": "fuel", "shop": "car_repair"}, "roadside") == "fuel"
    assert domain.categorize({"shop": "motorcycle_repair"}, "roadside") == "mechanic"
    assert domain.categorize({"shop": "car_repairs"}, "roadside") is None, "an exact match, not a prefix"
    assert domain.categorize({"amenity": "police"}, "roadside") is None


def test_the_square_is_the_same_for_everyone_in_it():
    assert domain.cell_of(26.8401, 80.9401) == domain.cell_of(26.8499, 80.9499) == (26.845, 80.945)
    assert domain.cell_of(26.8501, 80.9401)[0] == 26.855
    assert domain.cell_of(-0.0001, 0.0001) == (-0.005, 0.005)


def test_the_margin_covers_the_whole_square_so_nobody_loses_part_of_their_radius():
    for lat, lng in [(8.1, 77.0), (26.8, 80.9), (34.0, 74.8), (36.9, 70.0)]:
        c_lat, c_lng = domain.cell_of(lat, lng)
        corners = [(c_lat + dy, c_lng + dx) for dy in (-0.005, 0.005) for dx in (-0.005, 0.005)]
        worst = max(domain.distance_m(c_lat, c_lng, a, b) for a, b in corners)
        assert worst < domain.CELL_MARGIN_M, (lat, lng, worst)


def test_distance_is_right():
    assert domain.distance_m(LAT, LNG, LAT, LNG) == 0
    assert 110_000 < domain.distance_m(0, 0, 1, 0) < 112_000
    assert domain.distance_m(LAT, LNG, LAT + 0.001, LNG) == pytest.approx(111.2, abs=1.0)


# ─── the map servers: which, in what order, how long ───

def test_the_servers_tried_are_the_ones_that_answered_in_testing_and_the_dead_one_is_gone():
    settings = make_settings()
    urls = settings.overpass_urls
    assert urls[0] == "https://overpass.openstreetmap.fr/api/interpreter", "the one that answered, first"
    assert "https://overpass-api.de/api/interpreter" in urls
    assert not any("kumi" in u for u in urls), "never answered in testing"
    assert settings.overpass_timeout <= 9, "the app gives up at 10 seconds"


def hedged(**overrides):
    values = {
        "overpass_urls": ("https://one.example/api", "https://two.example/api", "https://three.example/api"),
        "overpass_stagger": 0.1,
        "overpass_timeout": 3.0,
    }
    values.update(overrides)
    return make_settings(**values)


def host(request):
    return request.url.host


def test_servers_are_asked_in_order_and_a_healthy_first_one_is_never_joined(ola, overpass):
    overpass.elements = one_place()
    with client_for(ola, hedged(overpass_stagger=2.0), overpass=overpass) as client:
        r = ask(client)
    assert r.status_code == 200
    assert [host(c) for c in overpass.calls] == ["one.example"], "no extra load on a public server that is answering"


def test_a_server_that_goes_quiet_is_joined_by_the_next_and_the_first_good_answer_wins(ola, overpass):
    overpass.elements = one_place()

    async def first_hangs(request):
        if host(request) == "one.example":
            await asyncio.sleep(30)
        return None

    overpass.override = first_hangs
    started = time.monotonic()
    with client_for(ola, hedged(), overpass=overpass) as client:
        r = ask(client)
    assert r.status_code == 200
    assert len(body(r)["places"]) == 1
    assert time.monotonic() - started < 2.0, "did not wait for the quiet server"
    assert [host(c) for c in overpass.calls][:2] == ["one.example", "two.example"]


def test_a_server_that_turns_us_away_is_replaced_at_once_not_after_the_pause(ola, overpass):
    overpass.elements = one_place()
    overpass.override = lambda request: httpx.Response(429, text="slow down") if host(request) == "one.example" else None
    started = time.monotonic()
    with client_for(ola, hedged(overpass_stagger=5.0), overpass=overpass) as client:
        r = ask(client)
    assert r.status_code == 200
    assert time.monotonic() - started < 1.5, "a refusal does not cost the 5 second wait"
    assert [host(c) for c in overpass.calls] == ["one.example", "two.example"]


def test_the_whole_lookup_stops_at_the_limit_even_if_every_server_hangs(ola, overpass):
    async def everything_hangs(request):
        await asyncio.sleep(30)

    overpass.override = everything_hangs
    started = time.monotonic()
    with client_for(ola, hedged(overpass_timeout=0.6), overpass=overpass) as client:
        r = ask(client)
    assert r.status_code == 504
    assert body(r)["error"]["code"] == "places_timeout"
    assert time.monotonic() - started < 2.0
    assert len(overpass.calls) == 3, "all three were tried inside the limit"


def test_a_slow_first_server_still_wins_over_the_one_that_joined_it_if_it_answers_in_time(ola, overpass):
    overpass.elements = one_place()

    async def slow_first_hung_second(request):
        await asyncio.sleep(0.4 if host(request) == "one.example" else 30)
        return None

    overpass.override = slow_first_hung_second
    with client_for(ola, hedged(), overpass=overpass) as client:
        r = ask(client)
    assert r.status_code == 200
    assert len(body(r)["places"]) == 1
    assert [host(c) for c in overpass.calls][:2] == ["one.example", "two.example"], "the second did join, and was dropped"


def test_a_partial_answer_from_one_server_is_not_used_when_another_has_the_whole_one(ola, overpass):
    overpass.elements = one_place()
    overpass.override = lambda request: (
        httpx.Response(200, json={"elements": [], "remark": "runtime error: out of memory"}) if host(request) == "one.example" else None
    )
    with client_for(ola, hedged(), overpass=overpass) as client:
        r = ask(client)
    assert r.status_code == 200
    assert len(body(r)["places"]) == 1


def test_if_every_server_refuses_it_is_a_clear_error_after_trying_each_once(ola, overpass):
    overpass.override = lambda request: httpx.Response(429, text="no")
    with client_for(ola, hedged(), overpass=overpass) as client:
        r = ask(client)
    assert r.status_code == 502
    assert body(r)["error"]["code"] == "places_error"
    assert len(overpass.calls) == 3


# ─── an old answer stands in when the map servers cannot be reached ───

class Ticking:
    """The service's clock, moved by the test."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def test_an_answer_older_than_half_a_day_is_asked_for_again(ola, overpass):
    overpass.elements = one_place()
    clock = Ticking()
    with client_for(ola, overpass=overpass) as client:
        client.app.state.nearby._clock = clock
        assert ask(client).headers["x-cache"] == "MISS"
        clock.now += 11 * 3600
        assert ask(client).headers["x-cache"] == "HIT"
        clock.now += 2 * 3600  # 13 hours old
        overpass.elements = [node("New Post", 0.002)]
        r = ask(client)
    assert r.headers["x-cache"] == "MISS"
    assert [p["name"] for p in body(r)["places"]] == ["New Post"], "the new answer replaced the old"
    assert len(overpass.calls) == 2


def test_when_the_map_servers_cannot_be_reached_the_old_answer_is_served(ola, overpass):
    overpass.elements = one_place()
    clock = Ticking()
    with client_for(ola, overpass=overpass) as client:
        client.app.state.nearby._clock = clock
        ask(client)
        clock.now += 3 * 24 * 3600  # three days old
        overpass.override = lambda request: httpx.Response(503, text="down")
        r = ask(client)
    assert r.status_code == 200
    assert r.headers["x-cache"] == "STALE"
    assert [p["name"] for p in body(r)["places"]] == ["Hazratganj Police Station"]


def test_with_no_old_answer_to_fall_back_on_it_is_still_an_error(ola, overpass):
    overpass.override = lambda request: httpx.Response(503, text="down")
    with client_for(ola, overpass=overpass) as client:
        assert ask(client).status_code == 502


def test_a_stale_answer_is_for_that_square_only(ola, overpass):
    overpass.elements = one_place()
    clock = Ticking()
    with client_for(ola, overpass=overpass) as client:
        client.app.state.nearby._clock = clock
        ask(client)
        clock.now += 24 * 3600
        overpass.override = lambda request: httpx.Response(503, text="down")
        assert ask(client).headers["x-cache"] == "STALE"
        assert ask(client, lat=LAT + 0.5).status_code == 502, "nobody has asked about that square before"


def test_a_kept_entry_in_an_unknown_shape_is_a_miss_not_a_crash(ola, overpass, fake_redis):
    overpass.elements = one_place()
    with client_for(ola, overpass=overpass, redis=fake_redis) as client:
        ask(client)
        key = next(k for k in fake_redis.data if ":nearby:emergency:" in k)
        fake_redis.data[key] = json.dumps({"emergency": "an older shape"})
        r = ask(client)
    assert r.status_code == 200
    assert len(body(r)["places"]) == 1
    assert len(overpass.calls) == 2


def test_the_stale_answer_still_measures_distance_from_the_person(ola, overpass):
    overpass.elements = [node("Post", 0.003)]
    clock = Ticking()
    with client_for(ola, overpass=overpass) as client:
        client.app.state.nearby._clock = clock
        near = body(ask(client))["places"][0]["distance_m"]
        clock.now += 24 * 3600
        overpass.override = lambda request: httpx.Response(503, text="down")
        r = ask(client, lat=LAT + 0.0007)
    assert r.headers["x-cache"] == "STALE"
    assert body(r)["places"][0]["distance_m"] < near - 50


# ─── many people asking at once ───

class SlowOverpass:
    """Stands in for the client: counts questions, answers after a moment."""

    def __init__(self, fail=False) -> None:
        self.calls = 0
        self.fail = fail

    async def places(self, group, cell_lat, cell_lng, radius_m):
        from app.clients.overpass import PlacesUnavailable

        self.calls += 1
        await asyncio.sleep(0.05)
        if self.fail:
            raise PlacesUnavailable("timeout")
        police = {"name": "P", "category": "police", "lat": LAT, "lng": LNG, "phone": None, "address": None, "open_24h": None}
        return {"police": [police], "hospital": [], "fire_station": []}


def new_service(fake):
    from app import cache as c
    from app.services.nearby import NearbyService

    return NearbyService(fake, c.LookupCache(memory_store=c.MemoryStore(), ttl_seconds={"nearby": 7 * 86400}))


@pytest.mark.anyio
async def test_people_asking_at_the_same_moment_cost_one_question():
    fake = SlowOverpass()
    service = new_service(fake)
    results = await asyncio.gather(*[service.find("police", LAT, LNG, 3000) for _ in range(5)])
    assert fake.calls == 1
    assert sorted(how for _, how in results) == ["miss", "shared", "shared", "shared", "shared"]
    assert all(len(places) == 1 for places, _ in results)


@pytest.mark.anyio
async def test_one_person_giving_up_does_not_cancel_the_question_for_the_others():
    fake = SlowOverpass()
    service = new_service(fake)
    quitter = asyncio.ensure_future(service.find("police", LAT, LNG, 3000))
    await asyncio.sleep(0.01)
    stayer = asyncio.ensure_future(service.find("police", LAT, LNG, 3000))
    await asyncio.sleep(0.01)
    quitter.cancel()
    places, how = await stayer
    assert how == "shared"
    assert len(places) == 1
    assert fake.calls == 1


@pytest.mark.anyio
async def test_a_failed_question_is_shared_too_and_not_remembered():
    from app.clients.overpass import PlacesUnavailable

    fake = SlowOverpass(fail=True)
    service = new_service(fake)
    results = await asyncio.gather(*[service.find("police", LAT, LNG, 3000) for _ in range(3)], return_exceptions=True)
    assert all(isinstance(r, PlacesUnavailable) for r in results)
    assert fake.calls == 1
    fake.fail = False
    places, how = await service.find("police", LAT, LNG, 3000)
    assert how == "miss", "the failure was not kept"
    assert len(places) == 1


@pytest.mark.anyio
async def test_a_group_with_nothing_in_it_is_not_kept_as_an_answer():
    fake = SlowOverpass()

    async def empty(group, cell_lat, cell_lng, radius_m):
        fake.calls += 1
        return {"police": [], "hospital": [], "fire_station": []}

    fake.places = empty
    service = new_service(fake)
    await service.find("police", LAT, LNG, 3000)
    await service.find("police", LAT, LNG, 3000)
    assert fake.calls == 2



# ─── not hammering the free public map servers ───

def fresh_cell(i):
    """A latitude in its own ~1 km square, so every call is an answer nobody has kept."""
    return 20.0 + i * 0.05


def test_one_person_is_held_to_thirty_place_searches_a_minute(ola, overpass):
    overpass.elements = one_place()
    settings = make_settings(rate_limit_nearby_per_minute=3, nearby_upstream_per_minute=1000)
    with client_for(ola, settings, overpass=overpass) as client:
        codes = [ask(client, lat=fresh_cell(i)).status_code for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_the_personal_cap_counts_answers_that_were_already_kept_too(ola, overpass):
    overpass.elements = one_place()
    settings = make_settings(rate_limit_nearby_per_minute=2, nearby_upstream_per_minute=1000)
    with client_for(ola, settings, overpass=overpass) as client:
        codes = [ask(client).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert len(overpass.calls) == 1


def test_the_personal_cap_is_separate_from_the_one_for_routes(ola, overpass):
    overpass.elements = one_place()
    settings = make_settings(rate_limit_nearby_per_minute=1, rate_limit_route_per_minute=1)
    with client_for(ola, settings, overpass=overpass) as client:
        assert ask(client).status_code == 200
        assert ask(client).status_code == 429
        r = client.post("/api/v1/route/calculate", json={"waypoints": [{"lat": 26.8, "lng": 80.9}, {"lat": 26.9, "lng": 81.0}]})
    assert r.status_code != 429, "routes have their own count"


def test_the_whole_service_asks_the_public_servers_at_most_so_often(ola, overpass):
    overpass.elements = one_place()
    settings = make_settings(rate_limit_nearby_per_minute=1000, nearby_upstream_per_minute=2)
    with client_for(ola, settings, overpass=overpass) as client:
        results = [ask(client, lat=fresh_cell(i)) for i in range(4)]
    assert [r.status_code for r in results] == [200, 200, 429, 429]
    assert body(results[2])["error"]["code"] == "places_busy"
    assert results[2].headers["retry-after"] == "15"
    assert len(overpass.calls) == 2, "the map servers were not asked beyond the cap"


def test_answers_already_kept_are_never_held_back_by_the_service_cap(ola, overpass):
    overpass.elements = one_place()
    settings = make_settings(rate_limit_nearby_per_minute=1000, nearby_upstream_per_minute=1)
    with client_for(ola, settings, overpass=overpass) as client:
        first = ask(client)
        again = [ask(client).status_code for _ in range(5)]
    assert first.status_code == 200
    assert again == [200] * 5
    assert len(overpass.calls) == 1


def test_over_the_service_cap_an_old_answer_is_still_served(ola, overpass):
    overpass.elements = one_place()
    clock = Ticking()
    settings = make_settings(rate_limit_nearby_per_minute=1000, nearby_upstream_per_minute=1)
    with client_for(ola, settings, overpass=overpass) as client:
        client.app.state.nearby._clock = clock
        ask(client)
        clock.now += 24 * 3600  # too old to be fresh: it would be asked for again, but the cap is used up
        r = ask(client)
    assert r.status_code == 200
    assert r.headers["x-cache"] == "STALE"
    assert len(overpass.calls) == 1


def test_a_busy_refusal_is_not_remembered(ola, overpass):
    overpass.elements = one_place()
    settings = make_settings(rate_limit_nearby_per_minute=1000, nearby_upstream_per_minute=1)
    with client_for(ola, settings, overpass=overpass) as client:
        ask(client, lat=fresh_cell(0))
        assert ask(client, lat=fresh_cell(1)).status_code == 429
        assert client.app.state.nearby._inflight == {}, "nothing is left waiting"


def test_the_caps_can_be_set_from_the_environment():
    from app.config import Settings

    s = Settings.from_env({"OLA_API_KEY": "k", "RATE_LIMIT_NEARBY_PER_MINUTE": "12", "NEARBY_UPSTREAM_PER_MINUTE": "40"})
    assert (s.rate_limit_nearby_per_minute, s.nearby_upstream_per_minute) == (12, 40)
    d = Settings.from_env({"OLA_API_KEY": "k"})
    assert (d.rate_limit_nearby_per_minute, d.nearby_upstream_per_minute) == (30, 60)


def test_the_deploy_config_turns_sign_in_on():
    import pathlib

    text = (pathlib.Path(__file__).resolve().parent.parent / "render.yaml").read_text(encoding="utf-8")
    block = text.split("key: AUTH_MODE", 1)[1].split("- key:", 1)[0]
    assert "value: required" in block, "production must not accept anonymous callers"
