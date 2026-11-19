"""Flash-time provisioning and real VLAN-sourced first contact against CHR."""
import base64
import json
import time


def adopt(e, opener, lab, ip, vendo):
    deadline = time.monotonic() + 40
    while True:
        # A real board re-reports every second while unacked, with a new rseq.
        lab.vendo_report(ip, vendo.frame())
        state = json.load(e.request(opener, '/admin/state'))
        stored = next(v for v in state['vendos'] if v['id'] == vendo.id)
        if stored['status'] == 'paired':
            return stored
        assert stored['status'] in ('provisioned', 'pending'), stored
        if time.monotonic() >= deadline:
            raise AssertionError(f"Vendo {vendo.id} did not finish pairing: {stored['status']}")
        time.sleep(1)


def scenario(e, opener, address, password, lab, report):
    router = lambda path, method="GET": e.router_rest(address, "admin", password, path, method)
    ids = []

    def vendos():
        return {v['id']: v for v in json.load(e.request(opener, '/admin/state'))['vendos']}

    def owned():
        return {path: [row for row in router(path) if any(row.get('comment', '') == f'openpiso-vendo-{i}' or row.get('comment', '').startswith(f'openpiso-vendo-{i}-') for i in ids)]
                for path in ('/ip/dhcp-server/lease', '/ip/hotspot/ip-binding', '/ip/firewall/filter', '/ipv6/firewall/filter')}

    try:
        lab.vlan_up()
        lab.vlan_hosts()
        board = lab.vlan_board(10, 1, hostname='opnw-0c01')
        other = lab.vlan_board(10, 2)
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
        lab.vendo_report(other['ip'], vendo.frame())
        assert vendos() == initial and owned() == baseline
        bad = e.SimulatedVendo(e.BASE_URL, paired['id'], bytes(32))
        lab.vendo_report(board['ip'], bad.frame())
        unknown = e.SimulatedVendo(e.BASE_URL, 0xffffffff, bytes(32))
        lab.vendo_report(board['ip'], unknown.frame())
        assert vendos() == initial and owned() == baseline
        audit = json.load(e.request(opener, '/admin/state'))['vendo_protocol']['audit']
        assert {row['reason'] for row in audit} >= {'first_contact_host_mismatch', 'bad_hmac', 'unknown_vendo'}
        assert e.request(opener, '/admin/vendos/unpair', 'POST', {'id': second['id']}).status == 204
        assert owned() == baseline
        try:
            adopted = adopt(e, opener, lab, board['ip'], vendo)
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

        # A Vendo with no lease on a Router with a DHCP server per HotSpot server
        # (#16): its static lease goes on the server of its own network.
        servers = sorted(row['name'] for row in router('/ip/dhcp-server'))
        assert {'opnw-vlan10', 'opnw-vlan20'} <= set(servers), servers
        unleased = lab.vlan_board(20, 3)
        for row in router('/ip/dhcp-server/lease'):
            if row['mac-address'].upper() == unleased['mac'].upper():
                router('/ip/dhcp-server/lease/' + row['.id'], 'DELETE')
        assert not [row for row in router('/ip/dhcp-server/lease') if row['mac-address'].upper() == unleased['mac'].upper()]
        third = json.load(e.request(opener, '/admin/vendos/pair', 'POST', {'mac': unleased['mac']}))
        ids.append(third['id'])
        try:
            adopted = adopt(e, opener, lab, unleased['ip'], e.SimulatedVendo(e.BASE_URL, third['id'], bytes.fromhex(third['secret'])))
        except AssertionError:
            report.snapshot('vendo-without-lease-pairing-failure', address, password, opener,
                            dhcp_servers=servers, hosts=router('/ip/hotspot/host'))
            raise
        assert adopted['ip'] == unleased['ip'] and adopted['hotspot_server'] == 'opnw-vlan20', adopted
        lease = router(f"/ip/dhcp-server/lease?comment=openpiso-vendo-{third['id']}")
        assert len(lease) == 1 and lease[0]['dynamic'] == 'false', lease
        assert lease[0]['server'] == 'opnw-vlan20', lease
        assert lease[0]['mac-address'].upper() == unleased['mac'].upper() and lease[0]['address'] == unleased['ip'], lease
        report.snapshot('vendo-without-lease-paired-on-its-networks-dhcp-server', address, password, opener,
                        dhcp_servers=servers, source_ip=unleased['ip'], lease=lease)
    finally:
        report.capture_lab_failure()
        for i in ids:
            assert e.request(opener, '/admin/vendos/unpair', 'POST', {'id': i}).status == 204
        lab.vlan_down()
