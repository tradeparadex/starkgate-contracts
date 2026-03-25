// SPDX-License-Identifier: Apache-2.0.
pragma solidity ^0.8.20;

import "src/solidity/StarknetTokenStorage.sol";
import "starkware/solidity/interfaces/ExternalInitializer.sol";
import "starkware/solidity/tokens/ERC20/IERC20.sol";
import "starkware/solidity/libraries/Transfers.sol";

/*
  External initializer run (via delegatecall) during a bridge downgrade to reverse
  MscaVaultUpgradeAssistExternalInitializer:
    1. Transfers the vault token balance back from the MSCA vault to the bridge.
    2. Clears mscaVault and mscaVaultToken from bridge storage.

  The vault must still have the bridge approved as a spender for the vault token.

  Init data ABI: abi.encode() — no arguments required; vault and token are read from storage.
*/
contract MscaVaultDowngradeAssistExternalInitializer is ExternalInitializer, StarknetTokenStorage {
    event MscaVaultCleared(address indexed vault, address indexed vaultToken);
    event VaultFundsMigratedToBridge(address indexed token, address indexed vault, uint256 amount);

    function initialize(bytes calldata data) external virtual override {
        address vault = mscaVault();
        address vaultToken = mscaVaultToken();

        require(vault != address(0), "MSCA_VAULT_NOT_SET");
        require(vaultToken != address(0), "MSCA_VAULT_TOKEN_NOT_SET");

        uint256 balance = IERC20(vaultToken).balanceOf(vault);
        if (balance > 0) {
            Transfers.transferIn(vaultToken, vault, balance);
            emit VaultFundsMigratedToBridge(vaultToken, vault, balance);
        }

        mscaVault(address(0));
        mscaVaultToken(address(0));
        emit MscaVaultCleared(vault, vaultToken);

        emit LogExternalInitialize(data);
    }
}
