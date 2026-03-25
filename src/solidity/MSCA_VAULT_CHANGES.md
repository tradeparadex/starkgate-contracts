# MSCA Vault Integration — StarkGate Token Bridge

## Overview

These changes route a designated token (USDC) through an external Circle MSCA
(Modular Smart Contract Account) vault, while leaving all other bridged tokens
unaffected.

- **Deposits** of the vault token are forwarded from the bridge to the vault.
- **Withdrawals** (and deposit reclaims) pull funds from the vault back through
  the bridge to the recipient.
- The vault is configured and migrated via proxy-upgrade EICs, with a matching
  downgrade path.

## Changed Files

| File | Description |
|---|---|
| `StarknetTokenStorage.sol` | Two new named-storage slots (`mscaVault`, `mscaVaultToken`) with internal getters/setters using `NamedStorage.setAddressValue` (re-writable, required for downgrade). |
| `StarknetTokenBridge.sol` | `_vaultFor(token)` private helper resolves whether a token should route through the vault. `acceptDeposit` checks `maxTotalBalance` against the vault's balance (not the bridge's) for the vault token, then forwards deposited funds to the vault. `transferOutFunds` pulls from the vault before sending to the recipient. New view functions: `getMscaVault()`, `getMscaVaultToken()`, `getMscaVaultAllowance()`. New event: `VaultRouted`. |
| `MscaVaultUpgradeAssistExternalInitializer.sol` | **New.** EIC run via `delegatecall` during proxy upgrade. Sets vault address and token in storage, migrates any existing bridge balance of that token to the vault. Guards against double-init (`MSCA_VAULT_ALREADY_SET`). |
| `MscaVaultDowngradeAssistExternalInitializer.sol` | **New.** EIC that reverses the upgrade: pulls vault-token balance back to the bridge, then clears both storage slots. |
| `msca_vault_test.py` | **New.** Test suite (20 tests) covering upgrade EIC, downgrade EIC, deposit/withdraw routing, max-balance enforcement, deposit cancel/reclaim with vault routing, vault allowance monitoring, and a full upgrade-downgrade roundtrip. |
| `files_to_compile.txt` | Added both EIC contracts to the compilation list. |

## Key Design Decisions

1. **Single vault, single token** — only one `(vault, token)` pair is supported
   at a time. Generalizing to multiple vaults was not required.
2. **Vault set only through EIC** — there is no governance-callable setter;
   changes require a full proxy upgrade cycle with time delay.
3. **Vault approval is an external dependency** — the vault must `approve` the
   bridge for the vault token. `getMscaVaultAllowance()` is provided for
   off-chain monitoring.
4. **Storage setters use `setAddressValue`** (not `setAddressValueOnce`) so that
   the downgrade EIC can clear them and a subsequent re-upgrade can set them
   again.

## Operational Requirements

- Before running the upgrade EIC, the MSCA vault owner must call
  `approve(bridge, type(uint256).max)` on the vault token contract from the
  vault address. If this approval lapses, **all vault-token withdrawals and
  deposit reclaims will revert**.
- If the Circle ColdStorageAddressBookPlugin is installed on the vault, the
  bridge address must be in the plugin's allowlist.
- Monitor `getMscaVaultAllowance()` in production to detect approval issues
  before they cause withdrawal failures.

## Audit Notes

Areas an external auditor should focus on:

1. **Vault approval liveness** — the bridge has no fallback if the vault's
   allowance is revoked. Withdrawals and deposit reclaims halt silently.
2. **No emergency vault rotation** — rotating the vault requires a full proxy
   upgrade cycle (with time delay). Evaluate whether an emergency governance
   path is needed.
3. **EIC trust model** — the storage setters are freely re-writable by any code
   running via `delegatecall`. Any EIC added to the proxy can overwrite the
   vault address.
4. **Double external call in `acceptDeposit`** — `transferIn` then
   `transferOut` in sequence. Safe for USDC (no transfer hooks) but worth
   reviewing if the vault token is ever changed.
5. **Deposit reclaim timing** — reclaims may happen long after the original
   deposit; the vault's approval must be sufficient at reclaim time.
