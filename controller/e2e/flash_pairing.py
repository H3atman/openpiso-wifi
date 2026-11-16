"""Flash-time provisioning and real VLAN-sourced first contact against CHR."""
import base64
import json
import time
import urllib.error


def board_report(client, ip, url, frame):
    # Check the source-policy route before sending a signed request. A fetch
    # alone can reuse a connection whose route has since become unavailable.
    deadline = time.monotonic() + 30
    detail = 'source-policy route inactive'
    while time.monotonic() < deadline:
        rules = [r for r in client('/routing/rule') if r.get('src-address') == ip + '/32']
        routes = client('/ip/route')
        ready = rules and all(any(r.get('routing-table') == rule['table'] and
                                  r.get('dst-address') == '0.0.0.0/0' and
                                  r.get('active') == 'true' and r.get('disabled') != 'true'
                                  for r in routes) for rule in rules)
        if ready:
            try:
                client('/tool/fetch', 'POST', {'url': url + '/healthz', 'src-address': ip, 'output': 'none'})
                break
            except urllib.error.HTTPError as error:
                if error.code != 400:
                    raise
                detail = error.read().decode()
        time.sleep(0.5)
    else:
        raise AssertionError(f'Board route from {ip} did not become ready: {detail}')
    # Decode hex escapes on RouterOS, rather than UTF-8 encoding binary via JSON.
    body = ''.join(f'\\{byte:02X}' for byte in frame)
    script = client('/system/script', 'PUT', {
        'name': 'opnw-first-report',
        'source': f'/tool fetch url="{url}/vendo/messages" src-address={ip} http-method=post '
                  f'http-header-field="Content-Type:application/octet-stream" http-data="{body}" output=none',
    })
    try:
        client('/system/script/run', 'POST', {'number': script['.id']})
    except urllib.error.HTTPError as error:
        # PairVendo clears existing connections, including the first report.
        # Assert rejection/acceptance through admin and Router observations.
        print("Board report fetch:", error.read().decode())
    finally:
        client('/system/script/' + script['.id'], 'DELETE')


def adopt(e, opener, client, ip, vendo):
    deadline = time.monotonic() + 40
    while True:
        # A real board re-reports every second while unacked, with a new rseq.
        board_report(client, ip, e.PORTAL_URL, vendo.frame())
        state = json.load(e.request(opener, '/admin/state'))
        stored = next(v for v in state['vendos'] if v['id'] == vendo.id)
        if stored['status'] == 'paired':
            return stored
        assert stored['status'] in ('provisioned', 'pending'), stored
        if time.monotonic() >= deadline:
            raise AssertionError(f"Vendo {vendo.id} did not finish pairing: {stored['status']}")
        time.sleep(1)


def scenario(e, opener, address, password, client_address, report):
    fixture, router, client = e.vlan_fixture(address, password, client_address)
    ids = []

    def vendos():
        return {v['id']: v for v in json.load(e.request(opener, '/admin/state'))['vendos']}

    def owned():
        return {path: [row for row in router(path) if any(row.get('comment', '') == f'openpiso-vendo-{i}' or row.get('comment', '').startswith(f'openpiso-vendo-{i}-') for i in ids)]
                for path in ('/ip/dhcp-server/lease', '/ip/hotspot/ip-binding', '/ip/firewall/filter', '/ipv6/firewall/filter')}

    try:
        fixture.setup()
        fixture.hosts()
        board = fixture.board(10, 1, hostname='opnw-0c01')
        other = fixture.board(10, 2)
        credentials = base64.b64encode(f'admin:{e.ADMIN_PASSWORD}'.encode()).decode()
        anonymous = e.urllib.request.build_opener()
        assert e.request(anonymous, '/admin/vendos/pair', 'POST', {'mac': board['mac']}).status == 401
        response = e.request(anonymous, '/admin/vendos/pair', 'POST', {'mac': board['mac']}, headers={'Authorization': 'Basic ' + credentials})
        assert response.status == 201, response.read().decode()
        paired = json.load(response)
        assert set(paired) == {'id', 'secret', 'controller_url'}
        assert paired['controller_url'] == e.PORTAL_URL
        assert len(bytes.fromhex(paired['secret'])) == 32
        ids.append(paired['id'])
        assert e.request(opener, '/admin/vendos/pair', 'POST', {'mac': board['mac']}).status == 409
        second = json.load(e.request(opener, '/admin/vendos/pair', 'POST', {'mac': other['mac']}))
        ids.append(second['id'])
        initial = vendos()
        for i in ids:
            assert initial[i]['status'] == 'provisioned' and initial[i]['ip'] == '' and initial[i]['hotspot_server'] == ''
        assert not any(owned().values())
        page = e.request(opener, '/admin/vendos').read().decode()
        assert 'Waiting for first contact' in page and 'Fixed IP' not in page
        vendo = e.SimulatedVendo(e.BASE_URL, paired['id'], bytes.fromhex(paired['secret']))
        baseline = owned()
        board_report(client, other['ip'], e.PORTAL_URL, vendo.frame())
        assert vendos() == initial and owned() == baseline
        bad = e.SimulatedVendo(e.BASE_URL, paired['id'], bytes(32))
        board_report(client, board['ip'], e.PORTAL_URL, bad.frame())
        unknown = e.SimulatedVendo(e.BASE_URL, 0xffffffff, bytes(32))
        board_report(client, board['ip'], e.PORTAL_URL, unknown.frame())
        assert vendos() == initial and owned() == baseline
        audit = json.load(e.request(opener, '/admin/state'))['vendo_protocol']['audit']
        assert {row['reason'] for row in audit} >= {'first_contact_host_mismatch', 'bad_hmac', 'unknown_vendo'}
        assert e.request(opener, '/admin/vendos/unpair', 'POST', {'id': second['id']}).status == 204
        assert owned() == baseline
        try:
            adopted = adopt(e, opener, client, board['ip'], vendo)
        except AssertionError:
            report.snapshot('flash-adoption-failure', address, password, opener, hosts=router('/ip/hotspot/host'))
            raise
        assert adopted['ip'] == board['ip'] and adopted['hotspot_server'] == 'opnw-vlan10', adopted
        comment = f"openpiso-vendo-{paired['id']}"
        lease = router('/ip/dhcp-server/lease?comment=' + comment)
        assert len(lease) == 1 and lease[0]['dynamic'] == 'false'
        assert lease[0]['mac-address'].upper() == board['mac'].upper() and lease[0]['address'] == board['ip']
        assert lease[0]['host-name'] == 'opnw-0c01', lease
        bypass = router('/ip/hotspot/ip-binding?comment=' + comment)
        assert len(bypass) == 1 and bypass[0]['type'] == 'bypassed' and bypass[0]['address'] == board['ip']
        rules = [r for r in router('/ip/firewall/filter') if r.get('comment', '').startswith(comment + '-')]
        assert len(rules) == 5
        assert next(r for r in rules if r['comment'] == comment + '-controller')['src-address'] == board['ip']
        assert len([r for r in router('/ipv6/firewall/filter') if r.get('comment', '').startswith(comment + '-')]) == 2
        page = e.request(opener, '/admin/vendos').read().decode()
        assert 'opnw-0c01' in page and board['ip'] in page and 'opnw-vlan10' in page
        assert vendo.heartbeat()[0] == 200
        report.snapshot('flash-pairing-adopted-from-real-vlan-source', address, password, opener,
                        source_ip=board['ip'], source_mac=board['mac'], hostname='opnw-0c01', lease=lease, bypass=bypass, firewall=rules,
                        mismatched_mac_rejected=True, bad_hmac_rejected=True, unknown_id_rejected=True)
    finally:
        report.capture_lab_failure()
        for i in ids:
            assert e.request(opener, '/admin/vendos/unpair', 'POST', {'id': i}).status == 204
        for path in ('/routing/rule', '/ip/route'):
            for row in client(path):
                if row.get('comment', '').startswith('opnw-vlan'):
                    client(path + '/' + row['.id'], 'DELETE')
        fixture.teardown()
