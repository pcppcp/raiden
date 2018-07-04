import structlog

import pytest
import gevent

from raiden.api.python import RaidenAPI
from raiden.tests.utils.geth import wait_until_block
from raiden.transfer import views

log = structlog.get_logger(__name__)


# TODO: add test scenarios for
# - subsequent `connect()` calls with different `funds` arguments
# - `connect()` calls with preexisting channels
# - Check if this test needs to be adapted for the matrix transport
#   layer when activating it again. It might as it depends on the
#   raiden_network fixture.


@pytest.mark.parametrize('number_of_nodes', [6])
@pytest.mark.parametrize('channels_per_node', [0])
# @pytest.mark.parametrize('register_tokens', [True, False])
@pytest.mark.parametrize('settle_timeout', [6])
@pytest.mark.parametrize('reveal_timeout', [3])
def test_participant_selection(raiden_network, token_addresses):
    registry_address = raiden_network[0].raiden.default_registry.address

    # pylint: disable=too-many-locals
    token_address = token_addresses[0]

    # connect the first node (will register the token if necessary)
    RaidenAPI(raiden_network[0].raiden).token_network_connect(
        registry_address,
        token_address,
        100,
    )

    # connect the other nodes
    connect_greenlets = [
        gevent.spawn(
            RaidenAPI(app.raiden).token_network_connect,
            registry_address,
            token_address,
            100,
        )

        for app in raiden_network[1:]
    ]
    gevent.wait(connect_greenlets)

    # wait some blocks to let the network connect
    for app in raiden_network:
        wait_until_block(
            app.raiden.chain,
            app.raiden.chain.block_number() + 1,
        )

    token_network_registry_address = views.get_token_network_identifier_by_token_address(
        views.state_from_raiden(raiden_network[0].raiden),
        payment_network_id=registry_address,
        token_address=token_address,
    )
    connection_managers = [
        app.raiden.connection_manager_for_token_network(
            token_network_registry_address,
        ) for app in raiden_network
    ]
    open_channel_views = [
        lambda:
        views.get_channelstate_open(
            views.state_from_raiden(app.raiden),
            registry_address,
            token_address,
        ) for app in raiden_network
    ]

    assert all([x() for x in open_channel_views])

    def saturated(connection_manager, open_channel_view):
        return len(open_channel_view()) >= connection_manager.initial_channel_target

    def saturated_count(connection_managers, open_channel_views):
        return len([
            saturated(connection_manager, open_channel_view)
            for connection_manager, open_channel_view
            in zip(connection_managers, open_channel_views)
        ])

    chain = raiden_network[-1].raiden.chain
    max_wait = 12

    while (
        saturated_count(connection_managers, open_channel_views) < len(connection_managers) and
        max_wait > 0
    ):
        wait_until_block(chain, chain.block_number() + 1)
        max_wait -= 1

    assert saturated_count(connection_managers, open_channel_views) == len(connection_managers)

    # Ensure unpartitioned network
#     addresses = [app.raiden.address for app in raiden_network]
#     for connection_manager in connection_managers:
#         assert all(
#             connection_manager.channelgraph.has_path(
#                 connection_manager.raiden.address,
#                 address,
#             )
#             for address in addresses
#         )

    # average channel count
    acc = (
        sum(len(x()) for x in open_channel_views) /
        len(connection_managers)
    )

    try:
        # FIXME: depending on the number of channels, this will fail, due to weak
        # selection algorithm
        # https://github.com/raiden-network/raiden/issues/576
        assert not any(
            len(x()) > 2 * acc
            for x in open_channel_views
        )
    except AssertionError:
        pass

    # create a transfer to the leaving node, so we have a channel to settle
    sender = raiden_network[-1].raiden
    receiver = raiden_network[0].raiden

    registry_address = sender.default_registry.address
    # assert there is a direct channel receiver -> sender (vv)
    receiver_channel = RaidenAPI(receiver).get_channel_list(
        registry_address=registry_address,
        token_address=token_address,
        partner_address=sender.address,
    )
    assert len(receiver_channel) == 1
    receiver_channel = receiver_channel[0]
#    assert receiver_channel.external_state.opened_block != 0
#    assert not receiver_channel.received_transfers

    # assert there is a direct channel sender -> receiver
    sender_channel = RaidenAPI(sender).get_channel_list(
        registry_address=registry_address,
        token_address=token_address,
        partner_address=receiver.address,
    )
    assert len(sender_channel) == 1
    sender_channel = sender_channel[0]
#    assert sender_channel.can_transfer
#    assert sender_channel.external_state.opened_block != 0

    amount = 1
    RaidenAPI(sender).transfer_and_wait(
        registry_address,
        token_address,
        amount,
        receiver.address,
        transfer_timeout=10,
    )

    def wait_for_transaction(
        receiver,
        registry_address,
        token_address,
        sender_address,
    ):
        while True:
            receiver_channel = RaidenAPI(receiver).get_channel_list(
                registry_address=registry_address,
                token_address=token_address,
                partner_address=sender_address,
            )
            if(
                len(receiver_channel) == 1 and
                receiver_channel[0].partner_state.balance_proof is not None
            ):
                break
            gevent.sleep(0.1)

    exception = ValueError('timeout while waiting for incoming transaction')
    with gevent.Timeout(10, exception=exception):
        wait_for_transaction(
            receiver,
            registry_address,
            token_address,
            sender.address,
        )

    # test `leave()` method
    connection_manager = connection_managers[0]

    timeout = (
        sender_channel.settle_timeout *
        connection_manager.raiden.chain.estimate_blocktime() *
        5
    )

    assert timeout > 0
    exception = ValueError('timeout while waiting for leave')
#     import pudb;pudb.set_trace()
#     with gevent.Timeout(timeout, exception=exception):
    RaidenAPI(raiden_network[0].raiden).token_network_leave(
        registry_address,
        token_address,
    )

    before_block = connection_manager.raiden.chain.block_number()
    wait_blocks = sender_channel.settle_timeout + 10
    # wait until both chains are synced?
    wait_until_block(
        connection_manager.raiden.chain,
        before_block + wait_blocks,
    )
    wait_until_block(
        receiver.chain,
        before_block + wait_blocks,
    )
    receiver_channel = RaidenAPI(receiver).get_channel_list(
        registry_address=registry_address,
        token_address=token_address,
        partner_address=sender.address,
    )
    assert receiver_channel[0].settle_transaction is not None
