// Production parameter file for infra/main.bicep.
//
// Bicep BCP258 requires every parameter without a default in main.bicep to be assigned
// here, even when scripts/deploy-azure.sh passes per-operator values via --parameters
// CLI flags (CLI override semantics: parameters supplied on the command line take
// precedence over those in this file). The placeholder values below pass Bicep's static
// validation; the actual deploy values come from the `--parameters` flag.
//
// If you ever invoke `az deployment group create -p main.bicepparam` WITHOUT
// `--parameters operatorIp=... operatorPrincipalId=... vmSshPublicKey=...`, the deploy
// will use these placeholders and almost certainly fail at apply time (operatorPrincipalId
// is not a real Entra GUID; vmSshPublicKey is not a valid SSH key) — by design, to make
// missing override invocations fail loudly.

using './main.bicep'

// Per-operator overrides (CLI --parameters takes precedence).
param operatorIp = '0.0.0.0'
param operatorPrincipalId = '00000000-0000-0000-0000-000000000000'
param vmSshPublicKey = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDummyPlaceholderOverrideViaCliParametersFlag placeholder'

// Static defaults already in main.bicep — uncomment to override per-environment.
// param location = 'eastus2'
// param repoOwner = 'marketsignal'
// param repoName = 'msai-v2'
// param repoBranch = 'main'
