"""Bounded HKeMobility web-source reader used by the experimental MCP tool.

The public miniapp bundle supplies its request-signing contract at run time.
The signing key and generated header values never enter the output.
"""

import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import httpx

ORIGIN = "https://www.hkemobility.gov.hk"
HK = ZoneInfo("Asia/Hong_Kong")
ID_PATTERN = re.compile(r"[A-Za-z0-9-]{1,64}\Z")
MAX_FEATURES = 50
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class ProbeError(Exception):
    """A safe, value-free error that can be shown to the caller."""


def utc_now():
    return datetime.now(UTC)


def valid_id(value):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ProbeError("An identifier does not match the bounded ID contract")
    return value


def js_query(params):
    """Match encodeURIComponent, rather than form encoding with '+' spaces."""
    return urlencode(params, quote_via=quote, safe="~()*!.'-")


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.modern_scripts = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        src = attributes.get("src", "")
        if (
            tag == "script"
            and attributes.get("type") == "module"
            and re.fullmatch(r"/miniapp_mobile/assets/index-[A-Za-z0-9_-]+\.js", src)
            and "legacy" not in src
        ):
            self.modern_scripts.append(src)


def signing_key_from_bundle(bundle):
    """Fail closed if the audited public client contract changes."""
    required = (
        'headers["X-Requested-With"]=this.generateDynamicToken()',
        "HmacSHA256(s,this.SECRET_KEY)",
        'getFullYear().toString()',
        'getMonth()+1',
        'getDate()',
        'Math.floor(1e5+Math.random()*9e5)',
        'mapLayerUrl:"/api/drss/layer/map"',
        'c6=$("1.0.0"),u6=$(1)',
        'TZ={version:c6.value,build:u6.value}',
    )
    if not all(fragment in bundle for fragment in required):
        raise ProbeError("The public miniapp signing or version contract changed")
    matches = re.findall(r'rr\(this,"SECRET_KEY",("(?:[^"\\]|\\.)*")\)', bundle)
    if len(matches) != 1:
        raise ProbeError("The public miniapp signing key contract is ambiguous")
    key = json.loads(matches[0])
    if not isinstance(key, str) or not 1 <= len(key) <= 256:
        raise ProbeError("The public miniapp signing key contract is invalid")
    return key.encode("utf-8")


class Client:
    def __init__(self, timeout, language):
        self.timeout = timeout
        self.language = language
        self.key = None
        self.requests = []
        self.referer = (
            ORIGIN + "/miniapp_mobile/carpark/detail?lang=" + language.lower()
            + "&park_id=911202&showHeaderBar=Y&p=emweb"
        )

    def request(self, method, url, payload=None, signed=False):
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Cache-Control": "no-cache",
            "Origin": ORIGIN,
            "Referer": self.referer,
            "User-Agent": USER_AGENT,
        }
        if signed:
            if self.key is None:
                raise ProbeError("The signing contract has not been loaded")
            message = (
                datetime.now(HK).strftime("%Y%m%d") + "."
                + str(100000 + secrets.randbelow(900000))
            )
            digest = hmac.new(self.key, message.encode(), hashlib.sha256).hexdigest()
            headers["X-Requested-With"] = message + "." + digest
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        deadline = time.monotonic() + self.timeout
        try:
            with (
                httpx.Client(trust_env=False, follow_redirects=False, timeout=self.timeout) as http,
                http.stream(method, url, content=data, headers=headers) as response,
            ):
                if response.status_code != 200:
                    raise ProbeError(f"{method} HTTP {response.status_code}")
                chunks, size = [], 0
                for part in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise ProbeError("The source response exceeded the elapsed-time limit")
                    size += len(part)
                    if size > MAX_RESPONSE_BYTES:
                        raise ProbeError("The source response exceeded the size limit")
                    chunks.append(part)
                raw = b"".join(chunks)
                record = {
                    "method": method, "url": url, "status": response.status_code,
                    "fetched_at": utc_now().isoformat(), "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "content_type": response.headers.get("Content-Type"),
                }
        except httpx.HTTPError:
            raise ProbeError(f"{method} network request failed") from None
        self.requests.append(record)
        return raw, record

    def load_contract(self):
        raw, html_source = self.request("GET", self.referer)
        parser = Scripts()
        try:
            parser.feed(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ProbeError("The miniapp HTML contract is invalid") from None
        paths = list(dict.fromkeys(parser.modern_scripts))
        if len(paths) != 1:
            raise ProbeError("The modern miniapp bundle cannot be identified")
        raw, bundle_source = self.request("GET", ORIGIN + paths[0])
        try:
            self.key = signing_key_from_bundle(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProbeError("The miniapp JavaScript contract is invalid") from None
        return {"html": html_source, "javascript": bundle_source}

    def json_request(self, method, url, payload=None):
        raw, source = self.request(method, url, payload, signed=True)
        try:
            return json.loads(raw), source
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProbeError(f"{method} response was not JSON") from None

    def wfs(self, ids):
        params = {
            "typeName": "DRSS:VW_CAR_PARK_" + self.language,
            "service": "WFS", "version": "1.0.0", "request": "GetFeature",
            "outputFormat": "application/json",
            "cql_filter": "EPARK_ID IN (" + ",".join("'" + i + "'" for i in ids) + ")",
            "maxFeatures": MAX_FEATURES,
        }
        return self.json_request("GET", ORIGIN + "/api/drss/layer/map/?" + js_query(params))

    def detail(self, park_id):
        payload = {
            "api": "getCarparkFullDetail", "p": "emweb", "p2": "",
            "version": "1.0.0", "build": 1, "elderly": "N",
            "package": "hkem.web.prod",
            "param": {"lang": self.language, "park_id": park_id,
                      "isGbGrouped": "Y", "isShowStd": "Y"},
        }
        params = {"apiName": "getCarparkFullDetail", "CALL_FROM": "app",
                  "env": "PROD", "p": "emweb", "lang": self.language, "p2": ""}
        return self.json_request("POST", ORIGIN + "/api/em/?" + js_query(params), payload)


def validate_wfs(data, requested):
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise ProbeError("WFS did not return a FeatureCollection")
    features = data.get("features")
    if not isinstance(features, list) or len(features) >= MAX_FEATURES:
        raise ProbeError("WFS features are invalid or may be truncated")
    for field in ("numberMatched", "numberReturned", "totalFeatures"):
        if field in data:
            count = data[field]
            if type(count) is not int or count != len(features):
                raise ProbeError("WFS count metadata does not prove a complete result")
    identity = {}
    reverse_identity = {}
    for feature in features:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ProbeError("WFS returned an invalid feature")
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            raise ProbeError("WFS returned invalid feature properties")
        if properties.get("EPARK_ID") not in requested:
            raise ProbeError("WFS returned an identifier outside the requested set")
        valid_id(properties.get("PARK_ID"))
        epark, park = properties["EPARK_ID"], properties["PARK_ID"]
        if epark in identity and identity[epark] != park:
            raise ProbeError("WFS returned ambiguous identity mapping")
        if park in reverse_identity and reverse_identity[park] != epark:
            raise ProbeError("WFS returned ambiguous reverse identity mapping")
        identity[epark] = park
        reverse_identity[park] = epark
    return features


def nonnegative_integer(value):
    return type(value) is int and value >= 0


def freshness(value, now, stale_minutes):
    if not isinstance(value, str) or not value:
        return {"is_stale": None, "age_seconds": None, "timestamp_status": "missing"}
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return {"is_stale": None, "age_seconds": None, "timestamp_status": "invalid"}
    if timestamp.tzinfo is None:
        return {"is_stale": None, "age_seconds": None, "timestamp_status": "unknown_timezone"}
    age = (now - timestamp).total_seconds()
    if age < -60:
        return {"is_stale": None, "age_seconds": age, "timestamp_status": "future"}
    return {"is_stale": age > stale_minutes * 60, "age_seconds": max(age, 0),
            "timestamp_status": "known"}


def vacancy_state(mode, value):
    if mode == "C":
        return "CLOSED"
    if mode == "A" and nonnegative_integer(value):
        return "COUNT"
    if mode == "B" and type(value) is int and value in (0, 1):
        return "AVAILABLE" if value == 1 else "FULL"
    return "UNKNOWN"


def normalize_wfs(feature, now, stale_minutes):
    p = feature["properties"]
    fresh = freshness(p.get("LAST_UPDATE"), now, stale_minutes)
    mode, value, opening = p.get("VACANCY_TYPE"), p.get("VACANCY"), p.get("OPENING_STATUS")
    state = vacancy_state(mode, value)
    flags = []
    if opening not in ("OPEN", "CLOSED"):
        flags.append("unrecognized_opening_status")
    if opening == "CLOSED" and (state == "AVAILABLE" or state == "COUNT" and value > 0):
        flags.append("closed_with_reported_vacancy")
    current = value if (state == "COUNT" and fresh["is_stale"] is False and opening == "OPEN") else None
    return {
        "source": "hkemobility_wfs", "park_id": p["PARK_ID"], "epark_id": p["EPARK_ID"],
        "name": p.get("NAME"), "vehicle_type": p.get("VEHICLE_TYPE"),
        "category": p.get("CATEGORY"), "source_updated_at": p.get("LAST_UPDATE"),
        "raw_vacancy": value, "raw_vacancy_type": mode, "raw_opening_status": opening,
        "vacancy_state": state, "current_vacancy_count": current, **fresh, "flags": flags,
    }


def normalize_mini(data, now, stale_minutes):
    rows = []
    fresh = freshness(data.get("parking_space_vacancy_last_update"), now, stale_minutes)
    basic = data.get("basic_info") or {}
    spaces = data.get("parking_space", [])
    if not isinstance(basic, dict) or not isinstance(spaces, list):
        raise ProbeError("Miniapp detail shape is invalid")
    for space in spaces:
        if not isinstance(space, dict):
            raise ProbeError("Miniapp parking-space shape is invalid")
        for category, parking in space.items():
            if category == "car_type" or not isinstance(parking, dict):
                continue
            if "vacancy" not in parking and "space" not in parking:
                continue
            vacancy, capacity = parking.get("vacancy") or {}, parking.get("space") or {}
            if not isinstance(vacancy, dict) or not isinstance(capacity, dict):
                raise ProbeError("Miniapp vacancy/capacity shape is invalid")
            mode, value = vacancy.get("mode"), vacancy.get("val")
            state = vacancy_state(mode, value)
            current = value if (state == "COUNT" and fresh["is_stale"] is False
                                and basic.get("isClosed") is False) else None
            flags = []
            if basic.get("isClosed") is True and (state == "AVAILABLE" or state == "COUNT" and value > 0):
                flags.append("closed_with_reported_vacancy")
            rows.append({
                "source": "hkemobility_miniapp", "park_id": data["park_id"],
                "epark_id": data.get("epark_id"), "name": basic.get("name"),
                "vehicle_type": space.get("car_type"), "category": category.upper(),
                "source_updated_at": data.get("parking_space_vacancy_last_update"),
                "raw_vacancy": value, "raw_vacancy_type": mode,
                "raw_vacancy_display": vacancy.get("display"),
                "raw_capacity": capacity.get("val"), "raw_capacity_display": capacity.get("display"),
                "raw_opening_status": basic.get("isClosed"), "vacancy_state": state,
                "current_vacancy_count": current, **fresh, "flags": flags,
            })
    return rows


def compare_rows(wfs_rows, mini_rows):
    flags = []
    for wfs in wfs_rows:
        if wfs["vehicle_type"] != "P":
            continue
        for mini in mini_rows:
            if (mini["park_id"], mini["vehicle_type"], mini["category"]) != (
                wfs["park_id"], "P", wfs["category"]
            ):
                continue
            reason = None
            if ((wfs["raw_opening_status"] == "OPEN" and mini["raw_opening_status"] is True)
                    or (wfs["raw_opening_status"] == "CLOSED" and mini["raw_opening_status"] is False)):
                wfs["current_vacancy_count"] = None
                mini["current_vacancy_count"] = None
                wfs["flags"].append("cross_source_opening_conflict")
                mini["flags"].append("cross_source_opening_conflict")
                flags.append({"park_id": wfs["park_id"], "reason": "source_opening_states_differ"})
            if wfs["vacancy_state"] == "COUNT" and mini["vacancy_state"] == "UNKNOWN":
                reason = "wfs_count_but_miniapp_unknown"
            elif (wfs["vacancy_state"] == mini["vacancy_state"] == "COUNT"
                  and wfs["raw_vacancy"] != mini["raw_vacancy"]):
                reason = "source_vacancy_counts_differ"
            if reason:
                wfs["current_vacancy_count"] = None
                mini["current_vacancy_count"] = None
                wfs["flags"].append(reason)
                mini["flags"].append(reason)
                flags.append({"park_id": wfs["park_id"], "vehicle_type": "P",
                              "category": wfs["category"], "reason": reason,
                              "wfs_raw_vacancy": wfs["raw_vacancy"],
                              "miniapp_raw_vacancy": mini["raw_vacancy"]})
    return flags


def run(args):
    ids = list(dict.fromkeys(args.epark_id))
    if not 1 <= len(args.epark_id) <= 5:
        raise ProbeError("Provide between one and five epark IDs")
    for value in ids:
        valid_id(value)
    client = Client(args.timeout, args.language)
    contract = client.load_contract()
    wfs, wfs_source = client.wfs(ids)
    features = validate_wfs(wfs, ids)
    details, mini_rows = [], []
    for park_id in dict.fromkeys(f["properties"]["PARK_ID"] for f in features):
        data, source = client.detail(park_id)
        if not isinstance(data, dict) or data.get("park_id") != park_id:
            raise ProbeError("Miniapp detail returned a different park ID")
        expected_epark = {f["properties"]["EPARK_ID"].casefold() for f in features
                          if f["properties"]["PARK_ID"] == park_id}
        if not isinstance(data.get("epark_id"), str) or data["epark_id"].casefold() not in expected_epark:
            raise ProbeError("Miniapp detail returned a different epark ID")
        details.append({"source": source, "data": data})
        mini_rows.extend(normalize_mini(data, utc_now(), args.stale_minutes))
    now = utc_now()
    wfs_rows = [normalize_wfs(feature, now, args.stale_minutes) for feature in features]
    return {
        "fetched_at": now.isoformat(), "purpose": "Experimental HKeMobility MCP source inspection",
        "requested_epark_ids": ids,
        "missing_epark_ids": [value for value in ids if not any(
            f["properties"]["EPARK_ID"] == value for f in features)],
        "stale_after_minutes": args.stale_minutes,
        "provenance": {"client_file": Path(__file__).name,
                       "client_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                       "public_client_contract": contract,
                       "auth": "Public miniapp signature; key and header values omitted"},
        "wfs": {"source": wfs_source, "data": wfs}, "miniapp": details,
        "normalized": {"wfs": wfs_rows, "miniapp": mini_rows},
        "discrepancies": compare_rows(wfs_rows, mini_rows),
        "requests": client.requests,
    }



def read_park_details(park_ids, language="EN"):
    """Internal bounded detail route for IDs obtained from the combined catalogue."""
    if not isinstance(park_ids, list) or not 1 <= len(park_ids) <= 10 or language not in ("EN", "TC"):
        raise ProbeError("Invalid bounded park detail request")
    for park_id in park_ids:
        valid_id(park_id)
    client = Client(10, language)
    contract = client.load_contract()
    details, rows = [], []
    for park_id in dict.fromkeys(park_ids):
        data, source = client.detail(park_id)
        if not isinstance(data, dict) or data.get("park_id") != park_id:
            raise ProbeError("Miniapp detail returned a different park ID")
        epark = data.get("epark_id")
        if epark not in (None, ""):
            valid_id(epark)
        details.append({"source": source, "data": data})
        rows.extend(normalize_mini(data, utc_now(), 15))
    return {"fetched_at": utc_now().isoformat(), "miniapp": details,
            "normalized": {"miniapp": rows}, "requests": client.requests,
            "public_client_contract": contract}
