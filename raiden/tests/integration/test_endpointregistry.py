import pytest

from raiden_contracts.constants import CONTRACT_ENDPOINT_REGISTRY
from raiden.exceptions import UnknownAddress
from raiden.network.discovery import ContractDiscovery
from raiden.tests.utils.factories import make_address
from raiden.tests.utils.smartcontracts import deploy_contract_web3
from raiden.utils import privatekey_to_address


@pytest.mark.parametrize('number_of_nodes', [1])
def test_endpointregistry(private_keys, blockchain_services):
    chain = blockchain_services.blockchain_services[0]
    my_address = privatekey_to_address(private_keys[0])

    endpointregistry_address = deploy_contract_web3(
        CONTRACT_ENDPOINT_REGISTRY,
        chain.client,
    )
    discovery_proxy = chain.discovery(endpointregistry_address)

    contract_discovery = ContractDiscovery(my_address, discovery_proxy)

    unregistered_address = make_address()

    # get should raise for unregistered addresses
    with pytest.raises(UnknownAddress):
        contract_discovery.get(my_address)

    with pytest.raises(UnknownAddress):
        contract_discovery.get(unregistered_address)

    assert contract_discovery.nodeid_by_host_port(('127.0.0.1', 44444)) is None

    contract_discovery.register(my_address, '127.0.0.1', 44444)

    assert contract_discovery.nodeid_by_host_port(('127.0.0.1', 44444)) == my_address
    assert contract_discovery.get(my_address) == ('127.0.0.1', 44444)

    contract_discovery.register(my_address, '127.0.0.1', 88888)

    assert contract_discovery.nodeid_by_host_port(('127.0.0.1', 88888)) == my_address
    assert contract_discovery.get(my_address) == ('127.0.0.1', 88888)

    with pytest.raises(UnknownAddress):
        contract_discovery.get(unregistered_address)


@pytest.mark.parametrize('number_of_nodes', [5])
def test_endpointregistry_gas(private_keys, blockchain_services):
    chain = blockchain_services.blockchain_services[0]

    endpointregistry_address = deploy_contract_web3(
        CONTRACT_ENDPOINT_REGISTRY,
        chain.client,
    )

    for i in range(len(private_keys)):
        chain = blockchain_services.blockchain_services[i]
        discovery_proxy = chain.discovery(endpointregistry_address)

        my_address = privatekey_to_address(private_keys[i])
        contract_discovery = ContractDiscovery(my_address, discovery_proxy)
        contract_discovery.register(my_address, '127.0.0.{}'.format(i + 1), 44444)
        chain.next_block()
