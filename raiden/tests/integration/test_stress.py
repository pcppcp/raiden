import random
from http import HTTPStatus
from itertools import combinations, count

import gevent
import grequests
import pytest
import requests
import structlog
from eth_utils import to_canonical_address, to_checksum_address
from flask import url_for
from gevent import pool, server

from raiden import waiting
from raiden.api.python import RaidenAPI
from raiden.api.rest import APIServer, RestAPI
from raiden.app import App
from raiden.network.transport import UDPTransport
from raiden.raiden_event_handler import RaidenEventHandler
from raiden.tests.integration.api.utils import wait_for_listening_port
from raiden.tests.utils.network import CHAIN
from raiden.tests.utils.transfer import assert_synced_channel_state, wait_assert
from raiden.transfer import views

STATELESS_EVENT_HANDLER = RaidenEventHandler()

log = structlog.get_logger(__name__)


class RandomCrashEventHandler(RaidenEventHandler):
    def on_raiden_event(self, raiden, event):
        if random.random() < 0.2:
            raiden.stop()
        else:
            super().on_raiden_event(raiden, event)


def _url_for(apiserver, endpoint, **kwargs):
    # url_for() expects binary address so we have to convert here
    for key, val in kwargs.items():
        if isinstance(val, str) and val.startswith('0x'):
            kwargs[key] = to_canonical_address(val)

    with apiserver.flask_app.app_context():
        return url_for('v1_resources.{}'.format(endpoint), **kwargs)


def start_apiserver(raiden_app, rest_api_port_number):
    raiden_api = RaidenAPI(raiden_app.raiden)
    rest_api = RestAPI(raiden_api)
    api_server = APIServer(rest_api)
    api_server.flask_app.config['SERVER_NAME'] = 'localhost:{}'.format(rest_api_port_number)
    api_server.start(port=rest_api_port_number)

    wait_for_listening_port(rest_api_port_number)

    return api_server


def start_apiserver_for_network(raiden_network, port_generator):
    return [
        start_apiserver(app, next(port_generator))
        for app in raiden_network
    ]


def app_monitor(app, rest_api_port_number):
    api_server = start_apiserver(app, rest_api_port_number)

    while True:
        app.start()
        api_server.start()

        # wait for a failure to happen and then stop the rest api
        app.raiden.stop_event.wait()
        api_server.stop()

        # wait for both services to stop
        app.raiden.get()
        api_server.get()


def new_app(app, raiden_event_handler=STATELESS_EVENT_HANDLER):
    host_port = (
        app.raiden.config['transport']['udp']['host'],
        app.raiden.config['transport']['udp']['port'],
    )
    socket = server._udp_socket(host_port)  # pylint: disable=protected-access
    new_transport = UDPTransport(
        app.raiden.address,
        app.discovery,
        socket,
        app.raiden.transport.throttle_policy,
        app.raiden.config['transport']['udp'],
    )
    app = App(
        config=app.config,
        chain=app.raiden.chain,
        query_start_block=0,
        default_registry=app.raiden.default_registry,
        default_secret_registry=app.raiden.default_secret_registry,
        transport=new_transport,
        raiden_event_handler=raiden_event_handler,
        discovery=app.raiden.discovery,
    )

    app.start()

    return app


def restart_app(app):
    app.stop()
    app.raiden.get()
    app.start()


def restart_network(raiden_network, retry_timeout):
    for app in raiden_network:
        app.stop()

    wait_network = [
        gevent.spawn(restart_app, app)
        for app in raiden_network
    ]

    gevent.wait(wait_network)

    # The tests assume the nodes are available to transfer
    for app0, app1 in combinations(raiden_network, 2):
        waiting.wait_for_healthy(
            app0.raiden,
            app1.raiden.address,
            retry_timeout,
        )


def restart_network_and_apiservers(raiden_network, api_servers, retry_timeout):
    """Stop an app and start it back"""
    for rest_api in api_servers:
        rest_api.stop()
        rest_api.get()

    restart_network(raiden_network, retry_timeout)

    for rest_api in api_servers:
        rest_api.start()


def address_from_apiserver(apiserver):
    return apiserver.rest_api.raiden_api.address


def transfer_and_assert(post_url, identifier, amount):
    json = {'amount': amount, 'identifier': identifier}

    log.debug('PAYMENT REQUEST', url=post_url, json=json)

    request = grequests.post(post_url, json=json)
    response = request.send().response

    exception = getattr(request, 'exception', None)
    if exception:
        raise exception

    assert response is not None
    assert response.headers['Content-Type'] == 'application/json', response.headers['Content-Type']
    assert response.status_code == HTTPStatus.OK, response.json()


def _wait_for_server(post_url, identifier, amount, port_number):
    while True:
        try:
            transfer_and_assert(
                post_url,
                identifier,
                amount,
            )
            return
        except requests.RequestException:
            wait_for_listening_port(port_number)


def parallel_bounded_requests(
        post_url,
        identifier_generator,
        number_of_concurrent_requests,
        number_of_requests,
        port_number,
):
    throttled_pool = pool.Pool(size=number_of_concurrent_requests)

    for _ in range(number_of_requests):
        throttled_pool.spawn(
            _wait_for_server,
            post_url=post_url,
            identifier=next(identifier_generator),
            amount=1,
            port_number=port_number,
        )


def sequential_transfers(
        server_from,
        server_to,
        number_of_transfers,
        token_address,
        payment_identifier,
):
    post_url = _url_for(
        server_from,
        'token_target_paymentresource',
        token_address=to_checksum_address(token_address),
        target_address=to_checksum_address(address_from_apiserver(server_to)),
    )

    for _ in range(number_of_transfers):
        transfer_and_assert(
            post_url=post_url,
            identifier=payment_identifier,
            amount=1,
        )


def stress_send_serial_transfers(rest_apis, token_address, identifier_generator, deposit):
    """Send `deposit` transfers of value `1` one at a time, without changing
    the initial capacity.
    """
    pairs = list(zip(rest_apis, rest_apis[1:] + [rest_apis[0]]))

    # deplete the channels in one direction
    for server_from, server_to in pairs:
        sequential_transfers(
            server_from=server_from,
            server_to=server_to,
            number_of_transfers=deposit,
            token_address=token_address,
            payment_identifier=next(identifier_generator),
        )

    # deplete the channels in the backwards direction
    for server_to, server_from in pairs:
        sequential_transfers(
            server_from=server_from,
            server_to=server_to,
            number_of_transfers=deposit * 2,
            token_address=token_address,
            payment_identifier=next(identifier_generator),
        )

    # reset the balances balances by sending the "extra" deposit forward
    for server_from, server_to in pairs:
        sequential_transfers(
            server_from=server_from,
            server_to=server_to,
            number_of_transfers=deposit,
            token_address=token_address,
            payment_identifier=next(identifier_generator),
        )


def stress_send_parallel_transfers(rest_apis, token_address, identifier_generator, deposit):
    """Send `deposit` transfers in parallel, without changing the initial capacity.
    """
    pairs = list(zip(rest_apis, rest_apis[1:] + [rest_apis[0]]))

    # deplete the channels in one direction
    gevent.wait([
        gevent.spawn(
            sequential_transfers,
            server_from=server_from,
            server_to=server_to,
            number_of_transfers=deposit,
            token_address=token_address,
            payment_identifier=next(identifier_generator),
        )
        for server_from, server_to in pairs
    ])

    # deplete the channels in the backwards direction
    gevent.wait([
        gevent.spawn(
            sequential_transfers,
            server_from=server_from,
            server_to=server_to,
            number_of_transfers=deposit * 2,
            token_address=token_address,
            payment_identifier=next(identifier_generator),
        )
        for server_to, server_from in pairs
    ])

    # reset the balances balances by sending the "extra" deposit forward
    gevent.wait([
        gevent.spawn(
            sequential_transfers,
            server_from=server_from,
            server_to=server_to,
            number_of_transfers=deposit,
            token_address=token_address,
            payment_identifier=next(identifier_generator),
        )
        for server_from, server_to in pairs
    ])


def stress_send_and_receive_parallel_transfers(
        rest_apis,
        token_address,
        identifier_generator,
        deposit,
):
    """Send transfers of value one in parallel"""
    pairs = list(zip(rest_apis, rest_apis[1:] + [rest_apis[0]]))

    foward_transfers = [
        gevent.spawn(
            sequential_transfers,
            server_from=server_from,
            server_to=server_to,
            number_of_transfers=deposit,
            token_address=token_address,
            payment_identifier=next(identifier_generator),
        )
        for server_from, server_to in pairs
    ]

    backwards_transfers = [
        gevent.spawn(
            sequential_transfers,
            server_from=server_from,
            server_to=server_to,
            number_of_transfers=deposit,
            token_address=token_address,
            payment_identifier=next(identifier_generator),
        )
        for server_to, server_from in pairs
    ]

    gevent.wait(foward_transfers + backwards_transfers)


def assert_channels(raiden_network, token_network_identifier, deposit):
    pairs = list(zip(raiden_network, raiden_network[1:] + [raiden_network[0]]))

    for first, second in pairs:
        wait_assert(
            assert_synced_channel_state,
            token_network_identifier,
            first, deposit, [],
            second, deposit, [],
        )


@pytest.mark.parametrize('number_of_nodes', [3])
@pytest.mark.parametrize('number_of_tokens', [1])
@pytest.mark.parametrize('channels_per_node', [2])
@pytest.mark.parametrize('deposit', [5])
@pytest.mark.parametrize('reveal_timeout', [15])
@pytest.mark.parametrize('settle_timeout', [120])
def test_stress_happy_case(
        raiden_network,
        deposit,
        retry_timeout,
        token_addresses,
        port_generator,
        skip_if_not_udp,
):
    token_address = token_addresses[0]
    rest_apis = start_apiserver_for_network(raiden_network, port_generator)
    identifier_generator = count()
    timeout = 120

    token_network_identifier = views.get_token_network_identifier_by_token_address(
        views.state_from_app(raiden_network[0]),
        raiden_network[0].raiden.default_registry.address,
        token_address,
    )

    for _ in range(3):
        with gevent.Timeout(timeout):
            assert_channels(
                raiden_network,
                token_network_identifier,
                deposit,
            )

        with gevent.Timeout(timeout):
            stress_send_serial_transfers(
                rest_apis,
                token_address,
                identifier_generator,
                deposit,
            )

        restart_network_and_apiservers(
            raiden_network,
            rest_apis,
            retry_timeout,
        )

        with gevent.Timeout(timeout):
            assert_channels(
                raiden_network,
                token_network_identifier,
                deposit,
            )

        with gevent.Timeout(timeout):
            stress_send_parallel_transfers(
                rest_apis,
                token_address,
                identifier_generator,
                deposit,
            )

        restart_network_and_apiservers(
            raiden_network,
            rest_apis,
            retry_timeout,
        )

        with gevent.Timeout(timeout):
            assert_channels(
                raiden_network,
                token_network_identifier,
                deposit,
            )

        with gevent.Timeout(timeout):
            stress_send_and_receive_parallel_transfers(
                rest_apis,
                token_address,
                identifier_generator,
                deposit,
            )

        restart_network_and_apiservers(
            raiden_network,
            rest_apis,
            retry_timeout,
        )

    restart_network(raiden_network, retry_timeout)


@pytest.mark.parametrize('number_of_nodes', [3])
@pytest.mark.parametrize('number_of_tokens', [1])
@pytest.mark.parametrize('channels_per_node', [CHAIN])
@pytest.mark.parametrize('deposit', [50])
@pytest.mark.parametrize('reveal_timeout', [15])
@pytest.mark.parametrize('settle_timeout', [120])
def test_stress_unhappy_case(
        raiden_chain,
        deposit,
        token_addresses,
        port_generator,
):
    random_crash_handler = RandomCrashEventHandler()
    initiator, mediator, target = raiden_chain

    initiator.raiden.raiden_event_handler = random_crash_handler
    mediator.raiden.raiden_event_handler = random_crash_handler
    target.raiden.raiden_event_handler = random_crash_handler

    token_address = token_addresses[0]
    identifier_generator = count()

    initiator_api_port = next(port_generator)
    gevent.spawn(app_monitor, initiator, initiator_api_port)
    gevent.spawn(app_monitor, mediator, next(port_generator))
    gevent.spawn(app_monitor, target, next(port_generator))

    post_url = (
        f'http://localhost:{initiator_api_port}'
        f'/payments'
        f'/{to_checksum_address(token_address)}'
        f'/{to_checksum_address(target.raiden.address)}'
    )

    runner = gevent.spawn(
        parallel_bounded_requests,
        post_url=post_url,
        identifier_generator=identifier_generator,
        number_of_concurrent_requests=10,
        number_of_requests=deposit,
        port_number=initiator_api_port,
    )
    runner.join()
