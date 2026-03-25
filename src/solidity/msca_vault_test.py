import pytest

from starkware.eth.eth_test_utils import EthAccount, EthContract, EthRevertException, EthTestUtils
from solidity.utils import load_contract
from solidity.conftest import (
    ACTIVE,
    INITIAL_BALANCE,
    L2_TOKEN_CONTRACT,
    UPGRADE_DELAY,
    ZERO_ADDRESS,
    add_implementation_and_upgrade,
    chain_hexes_to_bytes,
    deploy_proxy,
)
from solidity.test_contracts import StarknetTokenBridgeTester

TestERC20 = load_contract("TestERC20")
MscaVaultUpgradeEIC = load_contract("MscaVaultUpgradeAssistExternalInitializer")
MscaVaultDowngradeEIC = load_contract("MscaVaultDowngradeAssistExternalInitializer")

DEPOSIT_AMOUNT = 100
WITHDRAW_AMOUNT = 50
WITHDRAW = 0
MESSAGE_CANCEL_DELAY = 1000
L2_RECIPIENT = 37


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def register_l1_withdrawal(bridge: EthContract, messaging_contract: EthContract, amount: int):
    messaging_contract.mockSendMessageFromL2.transact(
        L2_TOKEN_CONTRACT,
        int(bridge.address, 16),
        [
            WITHDRAW,
            int(bridge.w3_contract.caller._contract.w3.eth.accounts[0], 16),  # unused; set per test
            0,  # token placeholder; patched below
            amount % 2**128,
            amount // 2**128,
        ],
    )


def mock_l2_withdrawal(
    messaging_contract: EthContract,
    bridge_address: str,
    token_address: str,
    recipient_address: str,
    amount: int,
):
    """Registers a mock L2->L1 withdrawal message so consumeMessage succeeds."""
    messaging_contract.mockSendMessageFromL2.transact(
        L2_TOKEN_CONTRACT,
        int(bridge_address, 16),
        [
            WITHDRAW,
            int(recipient_address, 16),
            int(token_address, 16),
            amount % 2**128,
            amount // 2**128,
        ],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def messaging_contract(eth_test_utils: EthTestUtils) -> EthContract:
    from starkware.starknet.testing.contracts import MockStarknetMessaging
    return eth_test_utils.accounts[0].deploy(MockStarknetMessaging, 1000)


@pytest.fixture
def vault_token(governor: EthAccount) -> EthContract:
    token = governor.deploy(TestERC20)
    token.setBalance.transact(governor.address, INITIAL_BALANCE)
    return token


@pytest.fixture
def other_token(governor: EthAccount) -> EthContract:
    token = governor.deploy(TestERC20)
    token.setBalance.transact(governor.address, INITIAL_BALANCE)
    return token


@pytest.fixture
def vault(eth_test_utils: EthTestUtils) -> EthAccount:
    """The MSCA vault — just an EOA for testing purposes."""
    return eth_test_utils.accounts[5]


@pytest.fixture
def bridge(
    governor: EthAccount,
    messaging_contract: EthContract,
    registry_contract: EthContract,
    vault_token: EthContract,
    other_token: EthContract,
) -> EthContract:
    """Fresh StarknetTokenBridgeTester deployed via proxy, with both tokens set Active."""
    bridge_impl = governor.deploy(StarknetTokenBridgeTester)
    proxy = deploy_proxy(governor)
    init_data = chain_hexes_to_bytes(
        [ZERO_ADDRESS, registry_contract.address, messaging_contract.address]
    )
    add_implementation_and_upgrade(proxy, bridge_impl.address, init_data, governor)
    b = proxy.replace_abi(bridge_impl.abi)
    b.registerAppRoleAdmin(governor.address, transact_args={"from": governor})
    b.registerAppGovernor(governor.address, transact_args={"from": governor})
    b.setL2TokenBridge(L2_TOKEN_CONTRACT, transact_args={"from": governor})
    b.setTokenStatus(vault_token.address, ACTIVE)
    b.setTokenStatus(other_token.address, ACTIVE)
    return b


@pytest.fixture
def bridge_proxy_raw(
    governor: EthAccount,
    messaging_contract: EthContract,
    registry_contract: EthContract,
):
    """Returns (proxy EthContract, bridge_impl EthContract) before EIC is run."""
    bridge_impl = governor.deploy(StarknetTokenBridgeTester)
    proxy = deploy_proxy(governor)
    init_data = chain_hexes_to_bytes(
        [ZERO_ADDRESS, registry_contract.address, messaging_contract.address]
    )
    add_implementation_and_upgrade(proxy, bridge_impl.address, init_data, governor)
    b = proxy.replace_abi(bridge_impl.abi)
    b.registerAppRoleAdmin(governor.address, transact_args={"from": governor})
    b.registerAppGovernor(governor.address, transact_args={"from": governor})
    b.setL2TokenBridge(L2_TOKEN_CONTRACT, transact_args={"from": governor})
    return proxy, bridge_impl, b


def run_upgrade_eic(governor, bridge_proxy_raw, vault_address, vault_token_address):
    proxy, bridge_impl, bridge = bridge_proxy_raw
    upgrade_eic = governor.deploy(MscaVaultUpgradeEIC)
    eic_init_data = chain_hexes_to_bytes(
        [upgrade_eic.address, vault_address, vault_token_address]
    )
    add_implementation_and_upgrade(proxy, bridge_impl.address, eic_init_data, governor)
    return bridge


# ---------------------------------------------------------------------------
# Upgrade EIC tests
# ---------------------------------------------------------------------------


def test_upgrade_eic_sets_vault_and_token(
    governor: EthAccount,
    bridge_proxy_raw,
    vault: EthAccount,
    vault_token: EthContract,
):
    bridge = run_upgrade_eic(governor, bridge_proxy_raw, vault.address, vault_token.address)

    assert bridge.getMscaVault.call() == vault.address
    assert bridge.getMscaVaultToken.call() == vault_token.address


def test_upgrade_eic_migrates_vault_token_balance(
    governor: EthAccount,
    bridge_proxy_raw,
    vault: EthAccount,
    vault_token: EthContract,
):
    _, _, bridge = bridge_proxy_raw
    vault_token.setBalance.transact(bridge.address, DEPOSIT_AMOUNT)

    run_upgrade_eic(governor, bridge_proxy_raw, vault.address, vault_token.address)

    assert vault_token.balanceOf.call(bridge.address) == 0
    assert vault_token.balanceOf.call(vault.address) == DEPOSIT_AMOUNT


def test_upgrade_eic_zero_balance_ok(
    governor: EthAccount,
    bridge_proxy_raw,
    vault: EthAccount,
    vault_token: EthContract,
):
    """EIC completes without error when bridge holds no vault token balance."""
    bridge = run_upgrade_eic(governor, bridge_proxy_raw, vault.address, vault_token.address)

    assert bridge.getMscaVault.call() == vault.address
    assert vault_token.balanceOf.call(vault.address) == 0


def test_upgrade_eic_reverts_if_already_set(
    governor: EthAccount,
    bridge_proxy_raw,
    vault: EthAccount,
    vault_token: EthContract,
):
    proxy, bridge_impl, _ = bridge_proxy_raw
    upgrade_eic = governor.deploy(MscaVaultUpgradeEIC)
    eic_init_data = chain_hexes_to_bytes(
        [upgrade_eic.address, vault.address, vault_token.address]
    )
    add_implementation_and_upgrade(proxy, bridge_impl.address, eic_init_data, governor)

    # Deploy a second impl to allow a second upgrade
    bridge_impl2 = governor.deploy(StarknetTokenBridgeTester)
    with pytest.raises(EthRevertException, match="MSCA_VAULT_ALREADY_SET"):
        add_implementation_and_upgrade(proxy, bridge_impl2.address, eic_init_data, governor)


def test_upgrade_eic_reverts_if_zero_vault(
    governor: EthAccount,
    bridge_proxy_raw,
    vault_token: EthContract,
):
    proxy, bridge_impl, _ = bridge_proxy_raw
    upgrade_eic = governor.deploy(MscaVaultUpgradeEIC)
    eic_init_data = chain_hexes_to_bytes(
        [upgrade_eic.address, ZERO_ADDRESS, vault_token.address]
    )
    with pytest.raises(EthRevertException, match="INVALID_MSCA_VAULT"):
        add_implementation_and_upgrade(proxy, bridge_impl.address, eic_init_data, governor)


def test_upgrade_eic_reverts_if_zero_token(
    governor: EthAccount,
    bridge_proxy_raw,
    vault: EthAccount,
):
    proxy, bridge_impl, _ = bridge_proxy_raw
    upgrade_eic = governor.deploy(MscaVaultUpgradeEIC)
    eic_init_data = chain_hexes_to_bytes(
        [upgrade_eic.address, vault.address, ZERO_ADDRESS]
    )
    with pytest.raises(EthRevertException, match="INVALID_MSCA_VAULT_TOKEN"):
        add_implementation_and_upgrade(proxy, bridge_impl.address, eic_init_data, governor)


# ---------------------------------------------------------------------------
# Bridge deposit / withdraw behaviour with vault
# ---------------------------------------------------------------------------


def test_deposit_vault_token_goes_to_vault(
    governor: EthAccount,
    bridge_proxy_raw,
    vault: EthAccount,
    vault_token: EthContract,
):
    bridge = run_upgrade_eic(governor, bridge_proxy_raw, vault.address, vault_token.address)
    bridge.setTokenStatus(vault_token.address, ACTIVE)

    vault_token.setBalance.transact(governor.address, DEPOSIT_AMOUNT)
    vault_token.approve.transact(bridge.address, DEPOSIT_AMOUNT, transact_args={"from": governor})

    fee = bridge.estimateDepositFeeWei.call()
    bridge.deposit(
        vault_token.address,
        DEPOSIT_AMOUNT,
        37,
        transact_args={"from": governor, "value": fee},
    )

    assert vault_token.balanceOf.call(bridge.address) == 0
    assert vault_token.balanceOf.call(vault.address) == DEPOSIT_AMOUNT


def test_deposit_non_vault_token_stays_in_bridge(
    governor: EthAccount,
    bridge_proxy_raw,
    vault: EthAccount,
    vault_token: EthContract,
    other_token: EthContract,
):
    bridge = run_upgrade_eic(governor, bridge_proxy_raw, vault.address, vault_token.address)
    bridge.setTokenStatus(other_token.address, ACTIVE)

    other_token.setBalance.transact(governor.address, DEPOSIT_AMOUNT)
    other_token.approve.transact(bridge.address, DEPOSIT_AMOUNT, transact_args={"from": governor})

    fee = bridge.estimateDepositFeeWei.call()
    bridge.deposit(
        other_token.address,
        DEPOSIT_AMOUNT,
        37,
        transact_args={"from": governor, "value": fee},
    )

    assert other_token.balanceOf.call(bridge.address) == DEPOSIT_AMOUNT
    assert other_token.balanceOf.call(vault.address) == 0


def test_withdraw_vault_token_pulled_from_vault(
    governor: EthAccount,
    bridge_proxy_raw,
    vault: EthAccount,
    vault_token: EthContract,
    messaging_contract: EthContract,
):
    bridge = run_upgrade_eic(governor, bridge_proxy_raw, vault.address, vault_token.address)
    bridge.setTokenStatus(vault_token.address, ACTIVE)

    # Fund vault and approve bridge to pull from it
    vault_token.setBalance.transact(vault.address, WITHDRAW_AMOUNT)
    vault_token.approve.transact(
        bridge.address, WITHDRAW_AMOUNT, transact_args={"from": vault}
    )

    mock_l2_withdrawal(
        messaging_contract, bridge.address, vault_token.address, governor.address, WITHDRAW_AMOUNT
    )

    initial_balance = vault_token.balanceOf.call(governor.address)
    bridge.withdraw(vault_token.address, WITHDRAW_AMOUNT, transact_args={"from": governor})

    assert vault_token.balanceOf.call(vault.address) == 0
    assert vault_token.balanceOf.call(governor.address) == initial_balance + WITHDRAW_AMOUNT


def test_withdraw_non_vault_token_from_bridge(
    governor: EthAccount,
    bridge_proxy_raw,
    vault: EthAccount,
    vault_token: EthContract,
    other_token: EthContract,
    messaging_contract: EthContract,
):
    bridge = run_upgrade_eic(governor, bridge_proxy_raw, vault.address, vault_token.address)
    bridge.setTokenStatus(other_token.address, ACTIVE)

    other_token.setBalance.transact(bridge.address, WITHDRAW_AMOUNT)

    mock_l2_withdrawal(
        messaging_contract, bridge.address, other_token.address, governor.address, WITHDRAW_AMOUNT
    )

    initial_balance = other_token.balanceOf.call(governor.address)
    bridge.withdraw(other_token.address, WITHDRAW_AMOUNT, transact_args={"from": governor})

    assert other_token.balanceOf.call(bridge.address) == 0
    assert other_token.balanceOf.call(governor.address) == initial_balance + WITHDRAW_AMOUNT


def test_max_total_balance_checked_against_vault(
    governor: EthAccount,
    bridge_proxy_raw,
    vault: EthAccount,
    vault_token: EthContract,
):
    bridge = run_upgrade_eic(governor, bridge_proxy_raw, vault.address, vault_token.address)
    bridge.setTokenStatus(vault_token.address, ACTIVE)

    max_balance = DEPOSIT_AMOUNT - 1
    bridge.setMaxTotalBalance(
        vault_token.address, max_balance, transact_args={"from": governor}
    )

    # Pre-fill vault to the limit
    vault_token.setBalance.transact(vault.address, max_balance)

    vault_token.setBalance.transact(governor.address, DEPOSIT_AMOUNT)
    vault_token.approve.transact(bridge.address, DEPOSIT_AMOUNT, transact_args={"from": governor})

    fee = bridge.estimateDepositFeeWei.call()
    with pytest.raises(EthRevertException, match="MAX_BALANCE_EXCEEDED"):
        bridge.deposit(
            vault_token.address,
            DEPOSIT_AMOUNT,
            37,
            transact_args={"from": governor, "value": fee},
        )


# ---------------------------------------------------------------------------
# Downgrade EIC tests
# ---------------------------------------------------------------------------


def test_downgrade_eic_clears_vault_storage(
    governor: EthAccount,
    bridge_proxy_raw,
    vault: EthAccount,
    vault_token: EthContract,
):
    proxy, bridge_impl, bridge = bridge_proxy_raw
    run_upgrade_eic(governor, bridge_proxy_raw, vault.address, vault_token.address)

    assert bridge.getMscaVault.call() == vault.address

    downgrade_eic = governor.deploy(MscaVaultDowngradeEIC)
    bridge_impl2 = governor.deploy(StarknetTokenBridgeTester)
    eic_init_data = chain_hexes_to_bytes([downgrade_eic.address])
    add_implementation_and_upgrade(proxy, bridge_impl2.address, eic_init_data, governor)
    bridge2 = proxy.replace_abi(bridge_impl2.abi)

    assert bridge2.getMscaVault.call() == ZERO_ADDRESS
    assert bridge2.getMscaVaultToken.call() == ZERO_ADDRESS


def test_downgrade_eic_migrates_balance_back(
    governor: EthAccount,
    bridge_proxy_raw,
    vault: EthAccount,
    vault_token: EthContract,
):
    proxy, bridge_impl, bridge = bridge_proxy_raw
    run_upgrade_eic(governor, bridge_proxy_raw, vault.address, vault_token.address)

    vault_token.setBalance.transact(vault.address, DEPOSIT_AMOUNT)
    vault_token.approve.transact(
        bridge.address, DEPOSIT_AMOUNT, transact_args={"from": vault}
    )

    downgrade_eic = governor.deploy(MscaVaultDowngradeEIC)
    bridge_impl2 = governor.deploy(StarknetTokenBridgeTester)
    eic_init_data = chain_hexes_to_bytes([downgrade_eic.address])
    add_implementation_and_upgrade(proxy, bridge_impl2.address, eic_init_data, governor)
    bridge2 = proxy.replace_abi(bridge_impl2.abi)

    assert vault_token.balanceOf.call(vault.address) == 0
    assert vault_token.balanceOf.call(bridge2.address) == DEPOSIT_AMOUNT


def test_downgrade_eic_reverts_if_vault_not_set(
    governor: EthAccount,
    bridge_proxy_raw,
):
    proxy, bridge_impl, _ = bridge_proxy_raw
    downgrade_eic = governor.deploy(MscaVaultDowngradeEIC)
    bridge_impl2 = governor.deploy(StarknetTokenBridgeTester)
    eic_init_data = chain_hexes_to_bytes([downgrade_eic.address])

    with pytest.raises(EthRevertException, match="MSCA_VAULT_NOT_SET"):
        add_implementation_and_upgrade(proxy, bridge_impl2.address, eic_init_data, governor)


def test_full_upgrade_downgrade_roundtrip(
    governor: EthAccount,
    bridge_proxy_raw,
    vault: EthAccount,
    vault_token: EthContract,
    messaging_contract: EthContract,
):
    """
    Full roundtrip: upgrade (vault active) → deposit → withdraw → downgrade → deposit again
    with funds staying in bridge.
    """
    proxy, bridge_impl, bridge = bridge_proxy_raw
    bridge.setTokenStatus(vault_token.address, ACTIVE)

    # --- Upgrade ---
    run_upgrade_eic(governor, bridge_proxy_raw, vault.address, vault_token.address)
    assert bridge.getMscaVault.call() == vault.address

    # --- Deposit: goes to vault ---
    vault_token.setBalance.transact(governor.address, DEPOSIT_AMOUNT)
    vault_token.approve.transact(bridge.address, DEPOSIT_AMOUNT, transact_args={"from": governor})
    fee = bridge.estimateDepositFeeWei.call()
    bridge.deposit(vault_token.address, DEPOSIT_AMOUNT, 37, transact_args={"from": governor, "value": fee})
    assert vault_token.balanceOf.call(vault.address) == DEPOSIT_AMOUNT

    # --- Withdraw: pulled from vault ---
    vault_token.approve.transact(
        bridge.address, WITHDRAW_AMOUNT, transact_args={"from": vault}
    )
    mock_l2_withdrawal(
        messaging_contract, bridge.address, vault_token.address, governor.address, WITHDRAW_AMOUNT
    )
    bridge.withdraw(vault_token.address, WITHDRAW_AMOUNT, transact_args={"from": governor})
    assert vault_token.balanceOf.call(vault.address) == DEPOSIT_AMOUNT - WITHDRAW_AMOUNT

    # --- Downgrade ---
    remaining = DEPOSIT_AMOUNT - WITHDRAW_AMOUNT
    vault_token.approve.transact(
        bridge.address, remaining, transact_args={"from": vault}
    )
    downgrade_eic = governor.deploy(MscaVaultDowngradeEIC)
    bridge_impl2 = governor.deploy(StarknetTokenBridgeTester)
    eic_init_data = chain_hexes_to_bytes([downgrade_eic.address])
    add_implementation_and_upgrade(proxy, bridge_impl2.address, eic_init_data, governor)
    bridge2 = proxy.replace_abi(bridge_impl2.abi)

    assert bridge2.getMscaVault.call() == ZERO_ADDRESS
    assert vault_token.balanceOf.call(bridge2.address) == remaining

    # --- Post-downgrade deposit: stays in bridge ---
    bridge2.setTokenStatus(vault_token.address, ACTIVE)
    vault_token.setBalance.transact(governor.address, DEPOSIT_AMOUNT)
    vault_token.approve.transact(bridge2.address, DEPOSIT_AMOUNT, transact_args={"from": governor})
    bridge2.deposit(vault_token.address, DEPOSIT_AMOUNT, 37, transact_args={"from": governor, "value": fee})
    assert vault_token.balanceOf.call(bridge2.address) == remaining + DEPOSIT_AMOUNT
    assert vault_token.balanceOf.call(vault.address) == 0


# ---------------------------------------------------------------------------
# Deposit reclaim tests (cancel path with vault routing)
# ---------------------------------------------------------------------------


def test_deposit_reclaim_vault_token_pulled_from_vault(
    eth_test_utils: EthTestUtils,
    governor: EthAccount,
    bridge_proxy_raw,
    vault: EthAccount,
    vault_token: EthContract,
    messaging_contract: EthContract,
):
    """
    Deposit vault-token (goes to vault), cancel, reclaim — funds should be pulled from
    vault and returned to depositor.
    """
    bridge = run_upgrade_eic(governor, bridge_proxy_raw, vault.address, vault_token.address)
    bridge.setTokenStatus(vault_token.address, ACTIVE)

    vault_token.setBalance.transact(governor.address, DEPOSIT_AMOUNT)
    vault_token.approve.transact(bridge.address, DEPOSIT_AMOUNT, transact_args={"from": governor})

    fee = bridge.estimateDepositFeeWei.call()
    bridge.deposit(
        vault_token.address,
        DEPOSIT_AMOUNT,
        L2_RECIPIENT,
        transact_args={"from": governor, "value": fee},
    )

    assert vault_token.balanceOf.call(vault.address) == DEPOSIT_AMOUNT
    assert vault_token.balanceOf.call(governor.address) == 0

    # Vault must approve bridge to pull funds back for the reclaim
    vault_token.approve.transact(
        bridge.address, DEPOSIT_AMOUNT, transact_args={"from": vault}
    )

    # Cancel request, wait, then reclaim
    bridge.depositCancelRequest(
        vault_token.address, DEPOSIT_AMOUNT, L2_RECIPIENT, 0,
        transact_args={"from": governor},
    )
    eth_test_utils.advance_time(MESSAGE_CANCEL_DELAY)

    bridge.depositReclaim(
        vault_token.address, DEPOSIT_AMOUNT, L2_RECIPIENT, 0,
        transact_args={"from": governor},
    )

    assert vault_token.balanceOf.call(vault.address) == 0
    assert vault_token.balanceOf.call(governor.address) == DEPOSIT_AMOUNT


def test_deposit_reclaim_non_vault_token_from_bridge(
    eth_test_utils: EthTestUtils,
    governor: EthAccount,
    bridge_proxy_raw,
    vault: EthAccount,
    vault_token: EthContract,
    other_token: EthContract,
    messaging_contract: EthContract,
):
    """
    Deposit non-vault-token (stays in bridge), cancel, reclaim — funds should come
    directly from bridge, vault is not involved.
    """
    bridge = run_upgrade_eic(governor, bridge_proxy_raw, vault.address, vault_token.address)
    bridge.setTokenStatus(other_token.address, ACTIVE)

    other_token.setBalance.transact(governor.address, DEPOSIT_AMOUNT)
    other_token.approve.transact(bridge.address, DEPOSIT_AMOUNT, transact_args={"from": governor})

    fee = bridge.estimateDepositFeeWei.call()
    bridge.deposit(
        other_token.address,
        DEPOSIT_AMOUNT,
        L2_RECIPIENT,
        transact_args={"from": governor, "value": fee},
    )

    assert other_token.balanceOf.call(bridge.address) == DEPOSIT_AMOUNT

    bridge.depositCancelRequest(
        other_token.address, DEPOSIT_AMOUNT, L2_RECIPIENT, 0,
        transact_args={"from": governor},
    )
    eth_test_utils.advance_time(MESSAGE_CANCEL_DELAY)

    bridge.depositReclaim(
        other_token.address, DEPOSIT_AMOUNT, L2_RECIPIENT, 0,
        transact_args={"from": governor},
    )

    assert other_token.balanceOf.call(bridge.address) == 0
    assert other_token.balanceOf.call(governor.address) == DEPOSIT_AMOUNT
    assert other_token.balanceOf.call(vault.address) == 0


# ---------------------------------------------------------------------------
# Vault allowance monitoring
# ---------------------------------------------------------------------------


def test_get_msca_vault_allowance_no_vault(
    governor: EthAccount,
    bridge: EthContract,
):
    """When no vault is configured, getMscaVaultAllowance returns max uint256."""
    max_uint = 2**256 - 1
    assert bridge.getMscaVaultAllowance.call() == max_uint


def test_get_msca_vault_allowance_reflects_approval(
    governor: EthAccount,
    bridge_proxy_raw,
    vault: EthAccount,
    vault_token: EthContract,
):
    bridge = run_upgrade_eic(governor, bridge_proxy_raw, vault.address, vault_token.address)

    # No approval yet
    assert bridge.getMscaVaultAllowance.call() == 0

    # Approve and check
    vault_token.approve.transact(bridge.address, 500, transact_args={"from": vault})
    assert bridge.getMscaVaultAllowance.call() == 500


def test_withdraw_reverts_when_vault_allowance_insufficient(
    governor: EthAccount,
    bridge_proxy_raw,
    vault: EthAccount,
    vault_token: EthContract,
    messaging_contract: EthContract,
):
    """Withdrawal of vault token fails if vault has not approved the bridge."""
    bridge = run_upgrade_eic(governor, bridge_proxy_raw, vault.address, vault_token.address)
    bridge.setTokenStatus(vault_token.address, ACTIVE)

    vault_token.setBalance.transact(vault.address, WITHDRAW_AMOUNT)
    # Intentionally do NOT approve bridge to pull from vault

    mock_l2_withdrawal(
        messaging_contract, bridge.address, vault_token.address, governor.address, WITHDRAW_AMOUNT
    )

    with pytest.raises(EthRevertException):
        bridge.withdraw(vault_token.address, WITHDRAW_AMOUNT, transact_args={"from": governor})
