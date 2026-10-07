import json
from touken.jp_items import item_reading, inventory
from touken.jp_ledger import import_transactions
from touken.netlog import Transaction
from touken.telemetry import TelemetryStore


def test_items_sparse_unknown_zero_and_reward(tmp_path):
    store = TelemetryStore(tmp_path / 'jp.db')
    assert item_reading({'item': [{'consumable_id': 8, 'num': 99}]}) is None
    assert item_reading({'item': {'bad': {'consumable_id': True, 'num': 1}}}) is None
    def feed(items, minute):
        payload = {'now': f'2026-10-07 15:{minute}:00', 'item': items}
        import_transactions(store, [Transaction(url='/login/start',path='/login/start',method='POST', request_line=None,response_body=json.dumps(payload).encode())])
    feed({'a': {'consumable_id': 1001, 'num': 212}, 'b': {'consumable_id': 9047, 'num': 6, 'expired_at': '2026-10-20 12:59:00', 'token': 'private'}}, '00')
    feed({'a': {'consumable_id': 1001, 'num': 0}}, '01')
    result = inventory(store)
    assert result['根兵糖·並']['count'] == 0
    assert result['名称待确认（编号 9047）']['count'] == 6
    assert result['名称待确认（编号 9047）']['expires_at'] == '2026-10-20 12:59:00'
    assert '御守' not in result
    assert 'private' not in str(result)
    assert inventory(store, 0) == {}
    assert store.client_item_inventory()['items'] == {}
    store.close()


def test_jp_api_uses_independent_items(tmp_path, monkeypatch):
    from panel import server
    from fastapi.testclient import TestClient
    jp = TelemetryStore(tmp_path / 'jp.db')
    cn = TelemetryStore(tmp_path / 'cn.db')
    payload = {'now': '2026-10-07 15:00:00', 'item': {'1': {'consumable_id': 8, 'num': 23}}}
    import_transactions(jp, [Transaction(url='/login/start', path='/login/start', method='POST', request_line=None, response_body=json.dumps(payload).encode())])
    monkeypatch.setattr(server, '_telemetry_store_for', lambda server='': jp if server == 'jp' else cn)
    client = TestClient(server.app)
    assert client.get('/api/data/client-inventory?server=jp').json()['items']['加速符']['count'] == 23
    assert client.get('/api/data/client-inventory').json()['items'] == {}
    jp.close()
    cn.close()
