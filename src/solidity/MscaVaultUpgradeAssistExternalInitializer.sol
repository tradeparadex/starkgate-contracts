// SPDX-License-Identifier: Apache-2.0.
pragma solidity ^0.8.20;

import "src/solidity/StarknetTokenStorage.sol";
import "starkware/solidity/interfaces/ExternalInitializer.sol";
import "starkware/solidity/tokens/ERC20/IERC20.sol";
import "starkware/solidity/libraries/Transfers.sol";

/*
  External initializer run (via delegatecall) during a bridge upgrade to:
    1. Set the MSCA vault address and the token it manages in bridge storage.
    2. Transfer the bridge's existing balance of that token to the vault,
       so that future withdrawals are sourced from the vault rather than the bridge.

  The vault must already have approved the bridge for the vault token before this
  initializer is run (required for the modified transferOutFunds path post-upgrade).

  Init data ABI: abi.encode(address vault, address vaultToken)
*/
contract MscaVaultUpgradeAssistExternalInitializer is ExternalInitializer, StarknetTokenStorage {
    event MscaVaultInitialized(address indexed vault, address indexed vaultToken);
    event BridgeFundsMigratedToVault(address indexed token, address indexed vault, uint256 amount);

    function initialize(bytes calldata data) external virtual override {
        (address vault, address vaultToken) = abi.decode(data, (address, address));

        require(vault != address(0), "INVALID_MSCA_VAULT");
        require(vaultToken != address(0), "INVALID_MSCA_VAULT_TOKEN");
        require(mscaVault() == address(0), "MSCA_VAULT_ALREADY_SET");

        mscaVault(vault);
        mscaVaultToken(vaultToken);
        emit MscaVaultInitialized(vault, vaultToken);

        uint256 balance = IERC20(vaultToken).balanceOf(address(this));
        if (balance > 0) {
            Transfers.transferOut(vaultToken, vault, balance);
            emit BridgeFundsMigratedToVault(vaultToken, vault, balance);
        }

        emit LogExternalInitialize(data);
    }
}
