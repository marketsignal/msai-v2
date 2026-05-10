// Production parameter file for infra/main.bicep.
//
// Most params have defaults in main.bicep (location, vmAdminUsername, repoOwner, repoName,
// repoBranch, tags). Per-operator inputs (operatorIp, operatorPrincipalId, vmSshPublicKey)
// are passed at deploy time via --parameters in scripts/deploy-azure.sh.
//
// Override here if you want a non-default for a specific deployment.

using './main.bicep'

// param location = 'eastus2'
// param repoOwner = 'marketsignal'
// param repoName = 'msai-v2'
// param repoBranch = 'main'
