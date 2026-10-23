"""Nearby safe places, end to end against a pretend OpenStreetMap (Overpass). Nothing here is paid."""

from __future__ import annotations

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
    assert ".parking out tags center qt 80;" in roadside
    assert ".mechanic out tags center qt 60;" in roadside


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


def test_with_redis_the_answer_is_shared_and_kept_for_hours(ola, overpass, fake_redis):
    overpass.elements = one_place()
    with client_for(ola, overpass=overpass, redis=fake_redis) as client:
        ask(client)
    keys = [k for k in fake_redis.data if ":nearby:" in k]
    assert len(keys) == 1
    assert fake_redis.ttl[keys[0]] == 12 * 3600
    assert ":nearby:emergency:" in keys[0], "kept for the group"
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
