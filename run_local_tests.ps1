$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# Storage and TCP tests intentionally use separate processes because each test
# module owns a different temporary SOULTIDE_DB_PATH.
$groups = @(
    @('test_account_snapshot.py'),
    @('test_storage_compatibility.py'),
    @('test_tcp_server_guards.py'),
    @('test_protocol_registry.py', 'test_net_metadata.py'),
    @('test_protocol_codec.py'),
    @('test_battle_rules.py'),
    @('test_companion_rules.py'),
    @('test_restaurant_rules.py'),
    @('test_remaining_modules.py'),
    @('test_exchange_rules.py'),
    @('test_reward_economy_config.py'),
    @('test_resource_manifest.py'),
    @('test_sdk_server.py'),
    @('test_module_rules.py'),
    @('test_server_contract_catalog.py'),
    @('test_local_admin_gui.py'),
    @('test_config_snapshots.py')
)
foreach ($group in $groups) {
    & python -m unittest @group
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
Write-Host 'all isolated local tests passed'
