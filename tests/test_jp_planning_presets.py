from fastapi.testclient import TestClient
from panel import server
from touken import custom_formations as cf, advisor
from touken.telemetry import TelemetryStore


def test_jp_plans_presets_are_independent(tmp_path, monkeypatch):
    cn = TelemetryStore(tmp_path / 'cn.db')
    jp = TelemetryStore(tmp_path / 'jp.db')
    monkeypatch.setattr(server, 'STATUS_DIR', tmp_path / 'cn')
    monkeypatch.setattr(server, 'JP_DATA_DIR', tmp_path / 'jp')
    monkeypatch.setattr(cf, 'STATE_DIR', tmp_path / 'cn')
    monkeypatch.setattr(cf, 'JP_DATA_DIR', tmp_path / 'jp')
    monkeypatch.setattr(server, '_telemetry_store_for', lambda server='': jp if server == 'jp' else cn)
    cf.save_formations([{'id':'f1','name':'国服','slots':{},'target_team':1}])
    advisor.add_goal(tmp_path / 'cn' / advisor.GOALS_FILENAME,resource='小判',target=99,goal_mode='amount_target')
    original = (tmp_path / 'cn/custom_formations.json').read_bytes()
    client = TestClient(server.app)
    assert client.get('/api/custom-formations?server=jp').json()['formations'] == []
    created = client.post('/api/custom-formations?server=jp',json={'name':'日服','target_team':2,'slots':{'1':{'observation_id':'jp:42','name_zh':'加州清光','level':2}}}).json()
    fid = created['formation']['id']
    assert created['ok']
    assert client.put(f'/api/custom-formations/{fid}?server=jp',json={'name':'日服修改','target_team':2,'slots':{}}).status_code == 200
    assert (tmp_path / 'jp/state/custom_formations.json.bak').exists()
    assert (tmp_path / 'cn/custom_formations.json').read_bytes() == original
    assert client.get('/api/planning?server=jp').json()['goals'] == []
    goal = client.post('/api/planning/goals?server=jp',json={'resource':'小判','target':200000,'goal_mode':'amount_target'}).json()
    assert goal['ok']
    report = client.get('/api/planning?server=jp').json()
    assert len(report['goals']) == 1 and report['events'] == [] and report['acquisition'] == {}
    assert report['current'].get('小判') is None
    assert report['rates']['小判']['daily'] is None
    assert client.post('/api/planning/goals?server=jp', json={'kind': 'fragment'}).status_code == 400
    assert len(advisor.load_goals(tmp_path / 'cn' / advisor.GOALS_FILENAME)) == 1
    assert client.delete(f"/api/planning/goals/{goal['goal']['id']}?server=jp").status_code == 200
    assert (tmp_path / 'jp/state/planning_goals.json.bak').exists()
    assert client.delete(f'/api/custom-formations/{fid}?server=jp').status_code == 200
    assert client.get('/api/data/honmaru-profile?server=jp').json()['roster']['teams'] == []
    cn.close()
    jp.close()


def test_jp_preset_failed_replace_preserves_original_and_backup(tmp_path, monkeypatch):
    from pathlib import Path
    import pytest
    monkeypatch.setattr(cf, 'JP_DATA_DIR', tmp_path)
    cf.save_formations([{'name': '原阵容'}], 'jp')
    path = tmp_path / 'state/custom_formations.json'
    original = path.read_bytes()
    replace = Path.replace
    def fail_replace(self, target):
        if self == path.with_suffix('.json.tmp'):
            raise OSError('write interrupted')
        return replace(self, target)
    monkeypatch.setattr(Path, 'replace', fail_replace)
    with pytest.raises(OSError):
        cf.save_formations([{'name': '新阵容'}], 'jp')
    assert path.read_bytes() == original
    assert path.with_suffix('.json.bak').read_bytes() == original


def test_jp_candidate_pool_keeps_individual_identity(monkeypatch):
    from touken import sword_archive, honmaru_profile
    rows = [{'observation_id': f'jp:{serial}', 'serial_id': serial,
             'name_zh': '加州清光', 'sword_catalog_id': '85'} for serial in (42, 43)]
    monkeypatch.setattr(server, '_telemetry_store_for', lambda server='': object())
    monkeypatch.setattr(sword_archive, 'build_jp_sword_archive', lambda store: {
        'entries': rows, 'observed_at': 123, 'roster_complete': False})
    def no_cn_profile():
        raise AssertionError('JP must not read CN candidates')
    monkeypatch.setattr(honmaru_profile, 'get_honmaru_profile', no_cn_profile)
    pool = TestClient(server.app).get('/api/data/honmaru-profile?server=jp').json()['candidate_pool']
    assert pool['completeness'] == 'partial'
    assert [row['observation_id'] for row in pool['entries']] == ['jp:42', 'jp:43']
    assert {row['same_team_exclusion_key'] for row in pool['entries']} == {'85'}


def test_jp_goal_failed_write_retains_original_and_backup(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(server, 'JP_DATA_DIR', tmp_path)
    monkeypatch.setattr(server, '_telemetry_store_for', lambda server='': object())
    path = tmp_path / 'state' / advisor.GOALS_FILENAME
    advisor.add_goal(path, resource='小判', target=100, goal_mode='amount_target')
    original = path.read_bytes()
    def fail_replace(source, target):
        raise OSError('write interrupted')
    monkeypatch.setattr(advisor.os, 'replace', fail_replace)
    with pytest.raises(OSError):
        TestClient(server.app).post('/api/planning/goals?server=jp', json={
            'resource': '小判', 'target': 200, 'goal_mode': 'amount_target'})
    assert path.read_bytes() == original
    assert path.with_suffix('.json.bak').read_bytes() == original
