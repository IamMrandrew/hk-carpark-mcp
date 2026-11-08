"""Complete, bounded catalogue union. No fuzzy identity merge or availability inference."""

import asyncio
import copy
import hashlib
import time
from datetime import UTC, datetime

from .client import ID_PATTERN, VEHICLE_TYPES, UpstreamError
from .hkemobility_source import ORIGIN, Client, ProbeError, js_query
from .service import CatalogSnapshot, ParkingService, digest

MAX_CATALOG_FEATURES = 5000


def text_key(value):
    return " ".join(value.casefold().split()) if isinstance(value, str) else ""


def validated_features(data):
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise ProbeError("WFS catalogue shape invalid")
    rows = data.get("features")
    if not isinstance(rows, list) or len(rows) >= MAX_CATALOG_FEATURES:
        raise ProbeError("WFS catalogue may be truncated")
    # Missing count metadata is not evidence of completeness.
    for name in ("totalFeatures", "numberMatched", "numberReturned"):
        if type(data.get(name)) is not int or data[name] != len(rows):
            raise ProbeError("WFS catalogue completeness not established")
    for row in rows:
        p = row.get("properties") if isinstance(row, dict) else None
        if not isinstance(p, dict) or not isinstance(p.get("PARK_ID"), str):
            raise ProbeError("WFS catalogue identity invalid")
        if not ID_PATTERN.fullmatch(p["PARK_ID"]):
            raise ProbeError("WFS catalogue identity invalid")
        epark = p.get("EPARK_ID")
        if epark not in (None, "") and (not isinstance(epark, str) or not ID_PATTERN.fullmatch(epark)):
            raise ProbeError("WFS catalogue epark identity invalid")
        if any(p.get(k) is not None and not isinstance(p[k], str)
               for k in ("NAME", "DISPLAY_ADDRESS", "DISTRICT")):
            raise ProbeError("WFS catalogue text invalid")
    return rows


class HKeMobilityCatalogClient(Client):
    def catalog(self):
        contract = self.load_contract()
        languages = {}
        sources = []
        for language in ("EN", "TC"):
            params = {"typeName": "DRSS:VW_CAR_PARK_" + language, "service": "WFS",
                      "version": "1.0.0", "request": "GetFeature", "outputFormat": "application/json",
                      "maxFeatures": MAX_CATALOG_FEATURES}
            data, source = self.json_request("GET", ORIGIN + "/api/drss/layer/map/?" + js_query(params))
            languages[language] = validated_features(data)
            sources.append(source)
        return languages, sources, contract


def wfs_items(languages):
    merged = {}
    for language, features in languages.items():
        lang = "en" if language == "EN" else "zh"
        for feature in features:
            p = feature["properties"]
            park = p["PARK_ID"]
            item = merged.setdefault(park, {
                "wfs_park_id": park, "epark_ids": set(), "name_en": None, "name_zh": None,
                "address_en": None, "address_zh": None, "district_en": None, "district_zh": None,
                "source_records": {"hkemobility": []}, "identity_flags": [],
            })
            if p.get("EPARK_ID"):
                item["epark_ids"].add(p["EPARK_ID"])
            for source, field in (("NAME", "name"), ("DISPLAY_ADDRESS", "address"), ("DISTRICT", "district")):
                key = field + "_" + lang
                value = p.get(source)
                if item[key] is not None and item[key] != value:
                    if "inconsistent_wfs_identity" not in item["identity_flags"]:
                        item["identity_flags"].append("inconsistent_wfs_identity")
                elif item[key] is None:
                    item[key] = value
            item["source_records"]["hkemobility"].append({
                "language": language, "park_id": park, "epark_id": p.get("EPARK_ID"),
                "name": p.get("NAME"), "address": p.get("DISPLAY_ADDRESS"),
                "raw_opening_status": p.get("OPENING_STATUS"),
                # Search observations are explicitly raw, not current availability.
                "raw_vacancy": p.get("VACANCY"), "raw_vacancy_type": p.get("VACANCY_TYPE"),
                "vehicle_type": p.get("VEHICLE_TYPE"), "category": p.get("CATEGORY"),
                "source_updated_at": p.get("LAST_UPDATE"),
            })
    for item in merged.values():
        item["epark_ids"] = sorted(item["epark_ids"])
        if len({x.casefold() for x in item["epark_ids"]}) > 1:
            item["identity_flags"].append("multiple_epark_ids")
    return list(merged.values())


def confirmed_same(government, wfs):
    if wfs["identity_flags"]:
        return False
    # Name alone, approximate spelling and geographic proximity never merge records.
    return any(all(text_key(government.get(field + "_" + lang))
                       and text_key(government.get(field + "_" + lang)) == text_key(wfs.get(field + "_" + lang))
                       for field in ("name", "address")) for lang in ("en", "zh"))


def union_items(government_items, hkemobility_items):
    candidates = {}
    for w in hkemobility_items:
        for eid in {v.casefold() for v in w["epark_ids"]}:
            candidates.setdefault(eid, []).append(w)
    government_counts = {}
    for g in government_items:
        key = g["park_Id"].casefold()
        government_counts[key] = government_counts.get(key, 0) + 1
    used, result = set(), []
    for g in government_items:
        options = candidates.get(g["park_Id"].casefold(), [])
        w = options[0] if len(options) == 1 else None
        match = w is not None and government_counts[g["park_Id"].casefold()] == 1 and confirmed_same(g, w)
        row = {**g, "combined_id": "government:" + g["park_Id"],
               "sources": ["government"], "source_refs": {"government": {"park_Id": g["park_Id"]}},
               "source_records": {"government": dict(g)}, "identity_flags": [],
               "availability_lookup": {"tool": "get_carpark_vacancies", "arguments": {"carpark_ids": [g["park_Id"]]}}}
        if match:
            used.add(w["wfs_park_id"])
            row["sources"].append("hkemobility")
            row["source_refs"]["hkemobility"] = {"park_id": w["wfs_park_id"], "epark_ids": w["epark_ids"]}
            row["source_records"].update(w["source_records"])
            row["supplementary_lookup"] = {"tool": "inspect_hkemobility_carparks", "arguments": {"epark_ids": w["epark_ids"][:1]}}
            row["identity_match"] = "same_id_and_exact_name_address"
        elif options:
            row["identity_flags"].append("same_id_not_confirmed_same_location")
        result.append(row)
    for w in hkemobility_items:
        if w["wfs_park_id"] in used:
            continue
        ids = w["epark_ids"]
        row = {k: v for k, v in w.items() if k not in ("epark_ids", "wfs_park_id")}
        row.update({"park_Id": "hkemobility--" + (w["wfs_park_id"] if len(w["wfs_park_id"]) <= 51
                                else hashlib.sha256(w["wfs_park_id"].encode()).hexdigest()[:51]),
                    "combined_id": "hkemobility:" + w["wfs_park_id"],
                    "sources": ["hkemobility"], "opening_status": None,
                    "source_refs": {"hkemobility": {"park_id": w["wfs_park_id"], "epark_ids": ids}},
                    "availability_lookup": ({"tool": "inspect_hkemobility_carparks", "arguments": {"epark_ids": ids[:1]}}
                                            if len(ids) == 1 and not w["identity_flags"] else None)})
        if any(e.casefold() in government_counts for e in ids):
            row["identity_flags"] = [*row["identity_flags"], "same_id_not_confirmed_same_location"]
        if not ids:
            row["availability_lookup_note"] = "No EPARK_ID; detail-by-PARK_ID lookup required, not yet available."
        result.append(row)
    seen = set()
    for row in result:
        if row["park_Id"] in seen:
            raise UpstreamError("Combined catalogue generated an ambiguous lookup ID")
        seen.add(row["park_Id"])
        row.pop("availability_lookup", None)
        row.pop("supplementary_lookup", None)
        row.pop("availability_lookup_note", None)
        row["availability_lookups"] = [{"tool": "get_carpark_vacancies",
                                         "arguments": {"carpark_ids": [row["park_Id"]]}}]
    return tuple(sorted(result, key=lambda row: row["combined_id"]))


def availability_state(state, count):
    if state in ("closed", "CLOSED"):
        return "closed"
    if state in ("full", "FULL"):
        return "full"
    if state in ("available", "AVAILABLE"):
        return "available"
    if state in ("exact_count", "COUNT") and type(count) is int and count >= 0:
        return "available" if count > 0 else "full"
    return None


def mini_record(row):
    state = row.get("vacancy_state", "UNKNOWN")
    reported = {"COUNT": "exact_count", "AVAILABLE": "available", "FULL": "full", "CLOSED": "closed"}.get(state, "missing_data")
    count = row.get("raw_vacancy") if state == "COUNT" else None
    current = row.get("current_vacancy_count")
    return {"source": "hkemobility", "category": row.get("category"),
            "vacancy_type": row.get("raw_vacancy_type"), "vacancy": row.get("raw_vacancy"),
            "lastupdate": row.get("source_updated_at"), "lastupdate_iso": None,
            "freshness": "fresh" if row.get("is_stale") is False else "stale" if row.get("is_stale") is True else "unknown",
            "age_seconds": row.get("age_seconds"), "reported_status": reported, "reported_count": count,
            "current_count": current, "current_status": "exact_count" if current is not None else "unknown",
            "capacity": row.get("raw_capacity")}


class CombinedCarparkService(ParkingService):
    def __init__(self, government: ParkingService, catalog_loader=None, catalog_ttl_seconds=3600,
                 monotonic=time.monotonic, hkemobility=None):
        super().__init__(government.gateway, catalog_ttl_seconds, monotonic)
        from .hkemobility import HKeMobilityService
        self.hkemobility = hkemobility or HKeMobilityService(government.gateway)
        self.government = government
        self.catalog_loader = catalog_loader or (lambda: HKeMobilityCatalogClient(10, "EN").catalog())

    async def catalog(self):
        await self._lock.acquire()
        release_deferred = False
        try:
            ttl = self.catalog_ttl_seconds
            if self._catalog and not (self._catalog.coverage or {}).get("complete", True):
                ttl = min(ttl, 60)
            if self._catalog and self.monotonic() - self._catalog.cached_at < ttl:
                return self._catalog
            status = {"government": "ok", "hkemobility": "ok"}
            government = None
            try:
                government = await self.government.catalog()
            except UpstreamError:
                status["government"] = "unavailable"
            languages, sources = {}, []
            task = asyncio.create_task(asyncio.to_thread(self.catalog_loader))
            try:
                languages, sources, _ = await asyncio.shield(task)
            except asyncio.CancelledError:
                release_deferred = True

                def finished(worker):
                    self._lock.release()
                    if not worker.cancelled():
                        worker.exception()

                task.add_done_callback(finished)
                raise
            except (ProbeError, UpstreamError, ValueError, TypeError, KeyError):
                status["hkemobility"] = "unavailable"
            if all(value == "unavailable" for value in status.values()):
                raise UpstreamError("Combined search unavailable: both catalogues unavailable")
            items = union_items(government.items if government else (), wfs_items(languages))
            times = ([government.fetched_at] if government else []) + [datetime.fromisoformat(s["fetched_at"]) for s in sources]
            urls = (*(government.sources if government else ()), *(s["url"] for s in sources))
            missing = [source for source, value in status.items() if value != "ok"]
            coverage = {"complete": not missing, "missing_sources": missing,
                        "total_scope": "available source identity records, not all Hong Kong facilities"}
            self._catalog = CatalogSnapshot(items, min(times), self.monotonic(), digest([items, status]),
                                            urls, status, coverage)
            return self._catalog
        finally:
            if not release_deferred:
                self._lock.release()

    async def get_carpark_vacancies(self, carpark_ids, vehicle_type="privateCar"):
        if (not isinstance(carpark_ids, list) or not 1 <= len(carpark_ids) <= 10
                or any(not isinstance(i, str) or not ID_PATTERN.fullmatch(i) for i in carpark_ids)
                or len(set(carpark_ids)) != len(carpark_ids) or vehicle_type not in VEHICLE_TYPES):
            raise ValueError("Require 1..10 unique search IDs and a supported vehicle type")
        snapshot = await self.catalog()
        known = {r["park_Id"]: r for r in snapshot.items}
        items = []
        for lookup_id in carpark_ids:
            item = known.get(lookup_id)
            if item is None:
                items.append({"park_Id": lookup_id, "status": "not_found" if (snapshot.coverage or {}).get("complete", True)
                              else "unknown_catalogue_incomplete", "records": [],
                              "vehicle_type": vehicle_type})
                continue
            result = {**copy.deepcopy(item), "records": [], "vehicle_type": vehicle_type,
                      "default_category": "HOURLY", "source_results": {}, "source_status": {}, "conflicts": []}
            refs = item["source_refs"]
            for source in refs:
                try:
                    if source == "government":
                        evidence = await self.government.get_carpark_vacancies(
                            [refs[source]["park_Id"]], vehicle_type)
                        entries = evidence.get("items") if isinstance(evidence, dict) else None
                        if (not isinstance(entries, list) or len(entries) != 1
                                or not isinstance(entries[0], dict)
                                or entries[0].get("park_Id", refs[source]["park_Id"]) != refs[source]["park_Id"]
                                or not isinstance(entries[0].get("records"), list)
                                or any(not isinstance(r, dict) for r in entries[0]["records"])):
                            raise UpstreamError("Government vacancy response shape invalid")
                        result["records"] = copy.deepcopy(entries[0]["records"])
                        source_state = entries[0].get("status", "ok")
                        if source_state not in ("ok", "not_found", "missing_vacancy", "missing_vehicle_records"):
                            raise UpstreamError("Government vacancy status invalid")
                        if source_state == "ok" and not result["records"]:
                            source_state = "missing_vehicle_records"
                    else:
                        evidence = await self.hkemobility.inspect_park_ids([refs[source]["park_id"]])
                        expected_ids = {v.casefold() for v in refs[source].get("epark_ids", [])}
                        if expected_ids:
                            details = evidence.get("miniapp", [])
                            if (not details or any(not isinstance(entry.get("data", {}).get("epark_id"), str)
                                    or entry["data"]["epark_id"].casefold() not in expected_ids
                                    for entry in details)):
                                result["source_status"][source] = "identity_mismatch"
                                continue
                        source_state = "ok"
                    result["source_results"][source] = evidence
                    result["source_status"][source] = source_state
                except (UpstreamError, ProbeError):
                    result["source_status"][source] = "unavailable"
            hkem = result["source_results"].get("hkemobility", {})
            mini = hkem.get("normalized", {}).get("miniapp", [])
            relevant = [r for r in mini if vehicle_type == "privateCar" and r.get("vehicle_type") == "P"]
            if "hkemobility" in refs and result["source_status"].get("hkemobility") == "ok":
                if vehicle_type != "privateCar":
                    result["source_status"]["hkemobility"] = "vehicle_mapping_unavailable"
                elif not relevant:
                    result["source_status"]["hkemobility"] = "missing_vehicle_records"
                if "government" not in refs:
                    result["records"] = [mini_record(row) for row in relevant]
            states = list(result["source_status"].values())
            result["status"] = ("ok" if all(v == "ok" for v in states)
                                else "unavailable" if all(v == "unavailable" for v in states)
                                else "partial" if "ok" in states
                                else "missing_vacancy")
            if result["status"] != "ok":
                result["conflicts"].append("source_unavailable_current_claim_suppressed")
            if any(r.get("raw_opening_status") is True for r in mini):
                result["conflicts"].append("hkemobility_reports_closed")
            if item.get("opening_status") == "CLOSED":
                result["conflicts"].append("government_catalogue_reports_closed")
            for record in result["records"]:
                for observation in relevant:
                    if record.get("category") != observation.get("category"):
                        continue
                    if observation.get("raw_opening_status") is True:
                        result["conflicts"].append("hkemobility_reports_closed")
                    government_state = availability_state(record.get("reported_status"), record.get("reported_count"))
                    mini_state = availability_state(observation.get("vacancy_state"), observation.get("raw_vacancy"))
                    if government_state is not None and mini_state is not None and government_state != mini_state:
                        result["conflicts"].append("source_availability_states_differ")
                    if (record.get("reported_count") is not None
                            and observation.get("raw_vacancy") != record["reported_count"]):
                        result["conflicts"].append("source_vacancy_observations_differ")
            result["conflicts"] = sorted(set(result["conflicts"]))
            if result["conflicts"]:
                for record in result["records"]:
                    record["current_count"] = None
                    record["current_status"] = "unknown"
                for evidence_item in result["source_results"].get("government", {}).get("items", []):
                    for record in evidence_item.get("records", []):
                        record["current_count"] = None
                        record["current_status"] = "unknown"
                for row in mini:
                    row["current_vacancy_count"] = None
                    row.setdefault("flags", []).extend(result["conflicts"])
            items.append(result)
        return {"items": items, "fetched_at": datetime.now(UTC).isoformat(),
                "source_url": next((u for u in snapshot.sources if u.startswith("https://api.data.gov.hk")), None),
                "source_urls": list(snapshot.sources),
                "catalogue_source_status": snapshot.source_status,
                "coverage": snapshot.coverage or {"complete": True},
                "disclaimer": "Source-specific observations are not guaranteed availability. Unknown is not zero; conflicts suppress current claims."}
