import asyncio
from datetime import UTC, datetime

import pytest

from hkparking.client import UpstreamError
from hkparking.combined_carparks import CombinedCarparkService, union_items, validated_features, wfs_items
from hkparking.hkemobility_source import ProbeError
from hkparking.service import CatalogSnapshot, digest


def gov(park='one', name='One', address='1 Road'):
    return {'park_Id': park, 'name_en': name, 'name_zh': None, 'address_en': address, 'address_zh': None,
                'district_en': 'District', 'district_zh': None, 'opening_status': 'CLOSED'}


def feature(park='w1', epark='one', name='One', address='1 Road'):
    return {'type': 'Feature', 'properties': {'PARK_ID': park, 'EPARK_ID': epark, 'NAME': name,
            'DISPLAY_ADDRESS': address, 'DISTRICT': 'District', 'OPENING_STATUS': 'OPEN', 'VACANCY': 99}}


def test_merge_requires_id_and_exact_location():
    rows = union_items([gov()], wfs_items({'EN': [feature()]}))
    assert len(rows) == 1
    assert rows[0]['sources'] == ['government', 'hkemobility']
    assert len(rows[0]['availability_lookups']) == 1
    assert rows[0]['source_records']['government']['opening_status'] == 'CLOSED'
    assert rows[0]['source_records']['hkemobility'][0]['raw_opening_status'] == 'OPEN'


@pytest.mark.parametrize('candidate', [feature(name='Other'), feature(address='2 Road'), feature(epark='other')])
def test_no_unconfirmed_merge(candidate):
    assert len(union_items([gov()], wfs_items({'EN': [candidate]}))) == 2


def test_source_exclusive_and_missing_epark_retained():
    rows = union_items([gov()], wfs_items({'EN': [feature(epark=None)]}))
    assert len(rows) == 2
    w = next(r for r in rows if r['park_Id'].startswith('hkemobility--'))
    assert w['source_refs']['hkemobility']['park_id'] == 'w1'
    assert w['availability_lookups'][0]['tool'] == 'get_carpark_vacancies'


def test_ambiguous_id_not_deduped():
    rows = union_items([gov()], wfs_items({'EN': [feature(), feature(park='w2')]}))
    assert len(rows) == 3
    assert all(r['identity_flags'] for r in rows)


def test_vehicle_rows_grouped_per_park():
    assert len(wfs_items({'EN': [feature(), feature()]})) == 1


@pytest.mark.parametrize('extra', [{}, {'totalFeatures': 2, 'numberMatched': 2, 'numberReturned': 1}])
def test_incomplete_catalog_rejected(extra):
    with pytest.raises(ProbeError):
        validated_features({'type': 'FeatureCollection', 'features': [feature()], **extra})


class Government:
    gateway = None
    async def catalog(self):
        rows = (gov(),)
        return CatalogSnapshot(rows, datetime.now(UTC), 0, digest(rows), ('government',))


def test_search_paging_and_cache():
    calls = []
    def loader():
        calls.append(True)
        return {'EN': [feature(), feature('w2', None, 'Other', '2 Road')]}, [
            {'fetched_at': datetime.now(UTC).isoformat(), 'url': 'wfs'}], {}
    async def check():
        service = CombinedCarparkService(Government(), loader)
        first = await service.search_carparks('Road', limit=1)
        second = await service.search_carparks('Road', limit=1, cursor=first['next_cursor'])
        assert first['total'] == 2
        assert first['items'][0]['combined_id'] != second['items'][0]['combined_id']
        assert not second['has_more']
        assert len(calls) == 1
        with pytest.raises(ValueError):
            await service.search_carparks('Other', cursor=first['next_cursor'])
    asyncio.run(check())


def test_source_failure_not_partial_success():
    def failed():
        raise ProbeError('unavailable')
    result = asyncio.run(CombinedCarparkService(Government(), failed).search_carparks('One'))
    assert result['total'] == 1
    assert result['coverage']['complete'] is False
    assert result['source_status']['hkemobility'] == 'unavailable'


def test_namespaced_id_bounded():
    rows = union_items([], wfs_items({'EN': [feature('x'*64, None)]}))
    assert len(rows[0]['park_Id']) <= 64
    assert rows[0]['source_refs']['hkemobility']['park_id'] == 'x'*64


def test_unified_vacancy_routes_conflicts_and_failures():
    calls = []
    class Gov(Government):
        async def get_carpark_vacancies(self, ids, vehicle_type):
            calls.append(('gov', ids))
            return {'items': [{'records': [{'category': 'HOURLY', 'reported_count': 99,
                                          'current_count': 99, 'current_status': 'exact_count'}]}]}
    class Hkem:
        async def inspect_park_ids(self, ids):
            calls.append(('hkem', ids))
            if ids == ['failed']:
                raise UpstreamError('unavailable')
            return {'miniapp': [{'data': {'epark_id': 'ONE'}}], 'normalized': {'miniapp': [{'park_id': ids[0], 'vehicle_type': 'P',
                    'category': 'HOURLY', 'raw_vacancy': -1, 'raw_opening_status': False,
                    'current_vacancy_count': None, 'flags': []}]}}
    def loader():
        return {'EN': [feature(), feature('only', None), feature('failed', None)]}, [
            {'fetched_at': datetime.now(UTC).isoformat(), 'url': 'wfs'}], {}
    async def check():
        service = CombinedCarparkService(Gov(), loader, hkemobility=Hkem())
        result = await service.get_carpark_vacancies(['one', 'hkemobility--only', 'hkemobility--failed'])
        merged, only, failed = result['items']
        assert merged['source_status'] == {'government': 'ok', 'hkemobility': 'ok'}
        assert merged['records'][0]['current_count'] is None
        assert 'source_vacancy_observations_differ' in merged['conflicts']
        assert only['source_status'] == {'hkemobility': 'ok'}
        assert failed['status'] == 'unavailable'
        assert calls == [('gov', ['one']), ('hkem', ['w1']), ('hkem', ['only']), ('hkem', ['failed'])]
        again = await service.get_carpark_vacancies(['hkemobility--only'])
        assert again['items'][0]['status'] == 'ok'
    asyncio.run(check())


@pytest.mark.parametrize('state', ['not_found', 'missing_vacancy', 'missing_vehicle_records', 'unavailable', 'malformed'])
def test_government_missing_or_failure_keeps_hkem_evidence(state):
    class Gov(Government):
        async def get_carpark_vacancies(self, ids, vehicle_type):
            if state == 'unavailable':
                raise UpstreamError('failed')
            if state == 'malformed':
                return {'items': []}
            return {'items': [{'park_Id': 'one', 'status': state, 'records': []}]}
    class Hkem:
        async def inspect_park_ids(self, ids):
            return {'miniapp': [{'data': {'epark_id': 'ONE'}}], 'normalized': {'miniapp': [{'vehicle_type': 'P', 'raw_vacancy': -1,
                    'vacancy_state': 'UNKNOWN', 'category': 'HOURLY', 'raw_capacity': 55,
                    'current_vacancy_count': None, 'flags': []}]}}
    def loader():
        return {'EN': [feature()]}, [{'fetched_at': datetime.now(UTC).isoformat(), 'url': 'wfs'}], {}
    row = asyncio.run(CombinedCarparkService(Gov(), loader, hkemobility=Hkem()).get_carpark_vacancies(['one']))['items'][0]
    assert row['source_status']['government'] == ('unavailable' if state == 'malformed' else state)
    assert row['source_status']['hkemobility'] == 'ok'
    assert row['status'] == 'partial'


@pytest.mark.parametrize('vehicle,mini_rows,expected', [
    ('privateCar', [{'vehicle_type': 'P', 'vacancy_state': 'UNKNOWN', 'raw_vacancy': -1,
                    'raw_capacity': 55, 'current_vacancy_count': None}], 'ok'),
    ('privateCar', [], 'missing_vehicle_records'),
    ('HGV', [{'vehicle_type': 'H', 'raw_vacancy': 3}], 'vehicle_mapping_unavailable'),
])
def test_wfs_only_common_records_and_mapping(vehicle, mini_rows, expected):
    class Hkem:
        async def inspect_park_ids(self, ids):
            return {'miniapp': [{'data': {'epark_id': 'ONE'}}], 'normalized': {'miniapp': mini_rows}}
    def loader():
        return {'EN': [feature('only', None)]}, [{'fetched_at': datetime.now(UTC).isoformat(), 'url': 'wfs'}], {}
    row = asyncio.run(CombinedCarparkService(Government(), loader, hkemobility=Hkem()).get_carpark_vacancies(
        ['hkemobility--only'], vehicle))['items'][0]
    assert row['source_status']['hkemobility'] == expected
    if expected == 'ok':
        assert row['records'][0]['capacity'] == 55
        assert row['records'][0]['reported_count'] is None
        assert row['records'][0]['current_count'] is None
    else:
        assert row['records'] == []


@pytest.mark.parametrize('gov_state,mini_count', [('full', 99), ('available', 0)])
def test_qualitative_conflict(gov_state, mini_count):
    class Gov(Government):
        async def get_carpark_vacancies(self, ids, vehicle_type):
            return {'items': [{'records': [{'category': 'HOURLY', 'reported_status': gov_state,
                    'reported_count': None, 'current_count': None, 'current_status': gov_state}]}]}
    class Hkem:
        async def inspect_park_ids(self, ids):
            return {'miniapp': [{'data': {'epark_id': 'ONE'}}], 'normalized': {'miniapp': [
                {'vehicle_type': 'P', 'category': 'HOURLY', 'vacancy_state': 'COUNT',
                 'raw_vacancy': mini_count, 'current_vacancy_count': mini_count, 'flags': []}]}}
    def loader():
        return {'EN': [feature()]}, [{'fetched_at': datetime.now(UTC).isoformat(), 'url': 'wfs'}], {}
    row = asyncio.run(CombinedCarparkService(Gov(), loader, hkemobility=Hkem()).get_carpark_vacancies(['one']))['items'][0]
    assert 'source_availability_states_differ' in row['conflicts']
    assert row['records'][0]['current_status'] == 'unknown'
    assert row['source_results']['hkemobility']['normalized']['miniapp'][0]['current_vacancy_count'] is None


@pytest.mark.parametrize('epark', [None, 'WRONG'])
def test_wrong_detail_identity_not_combined(epark):
    class Gov(Government):
        async def get_carpark_vacancies(self, ids, vehicle_type):
            return {'items': [{'records': [{'current_count': 9}]}]}
    class Hkem:
        async def inspect_park_ids(self, ids):
            return {'miniapp': [{'data': {'epark_id': epark}}], 'normalized': {'miniapp': []}}
    def loader():
        return {'EN': [feature()]}, [{'fetched_at': datetime.now(UTC).isoformat(), 'url': 'wfs'}], {}
    row = asyncio.run(CombinedCarparkService(Gov(), loader, hkemobility=Hkem()).get_carpark_vacancies(['one']))['items'][0]
    assert row['source_status']['hkemobility'] == 'identity_mismatch'
    assert 'hkemobility' not in row['source_results']
    assert row['records'][0]['current_count'] is None


def test_government_catalogue_failure_retains_wfs():
    class FailedGovernment(Government):
        async def catalog(self):
            raise UpstreamError('offline')
    def loader():
        return {'EN': [feature()]}, [{'fetched_at': datetime.now(UTC).isoformat(), 'url': 'wfs'}], {}
    async def check():
        service = CombinedCarparkService(FailedGovernment(), loader)
        result = await service.search_carparks('One')
        assert result['total'] == 1
        assert result['items'][0]['sources'] == ['hkemobility']
        assert result['coverage']['missing_sources'] == ['government']
        unknown = await service.get_carpark_vacancies(['one'])
        assert unknown['items'][0]['status'] == 'unknown_catalogue_incomplete'
    asyncio.run(check())


def test_both_catalogues_failed_errors():
    class FailedGovernment(Government):
        async def catalog(self):
            raise UpstreamError('offline')
    def failed():
        raise ProbeError('offline')
    with pytest.raises(UpstreamError):
        asyncio.run(CombinedCarparkService(FailedGovernment(), failed).catalog())


def test_partial_cache_short_ttl_and_coverage_bound_cursor():
    clock = [0]
    calls = []
    def loader():
        calls.append(True)
        if len(calls) == 1:
            raise ProbeError('offline')
        return {'EN': [feature()]}, [{'fetched_at': datetime.now(UTC).isoformat(), 'url': 'wfs'}], {}
    async def check():
        service = CombinedCarparkService(Government(), loader, monotonic=lambda: clock[0])
        partial = await service.catalog()
        assert not partial.coverage['complete']
        clock[0] = 59
        assert await service.catalog() is partial
        clock[0] = 61
        complete = await service.catalog()
        assert complete.coverage['complete']
        assert complete.fingerprint != partial.fingerprint
    asyncio.run(check())
