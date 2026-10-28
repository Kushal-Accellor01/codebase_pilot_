# Azure setup (Z-0, Z-1, T-2): commands, not yet run

These are the exact `az` commands for the resources in the build spec (section 3.3). **Nobody has run them.**
Fill in the region and names IT approved, run them in order, and check each step.

```bash
RG=rg-codebasepilot-dev; LOC=<approved-region>
az group create -n $RG -l $LOC --tags app=codebase-pilot env=dev owner=<you>

# Z-0: Azure OpenAI with two deployments (strong and fast; both must support structured outputs / JSON mode)
az cognitiveservices account create -n oai-codebasepilot-dev -g $RG -l $LOC --kind OpenAI --sku S0 --custom-domain oai-codebasepilot-dev
az cognitiveservices account deployment create -g $RG -n oai-codebasepilot-dev --deployment-name strong \
  --model-name <model> --model-version <version> --model-format OpenAI --sku-name Standard --sku-capacity 50
az cognitiveservices account deployment create -g $RG -n oai-codebasepilot-dev --deployment-name fast \
  --model-name <smaller-model> --model-version <version> --model-format OpenAI --sku-name Standard --sku-capacity 50
az cognitiveservices account keys list -g $RG -n oai-codebasepilot-dev      # hackathon: API key into backend/.env

# Key Vault for secrets
az keyvault create -n kv-codebasepilot-dev -g $RG -l $LOC
az keyvault secret set --vault-name kv-codebasepilot-dev -n github-token --value <token>

# Check (needs AZURE_OPENAI_* in backend/.env)
cd backend && python -m app.core.llm --smoke

# Z-1: build and run the API + worker image from the Dockerfile at the repo root
az acr create -n acr<org>codebasepilot -g $RG --sku Basic
az acr build -r acr<org>codebasepilot -t pilot-api:dev .
az containerapp env create -n cae-codebasepilot-dev -g $RG -l $LOC
az containerapp create -n ca-pilot-api -g $RG --environment cae-codebasepilot-dev \
  --image acr<org>codebasepilot.azurecr.io/pilot-api:dev --target-port 8000 --ingress external \
  --registry-server acr<org>codebasepilot.azurecr.io --min-replicas 1 --max-replicas 1 \
  --env-vars PILOT_EMBEDDED_WORKER=1 AZURE_OPENAI_ENDPOINT=<endpoint> AZURE_OPENAI_API_VERSION=<version> \
             AZURE_OPENAI_DEPLOYMENT_STRONG=strong AZURE_OPENAI_DEPLOYMENT_FAST=fast \
             GITHUB_TOKEN=secretref:github-token EXEC_ALLOWED_REPOS=<org>/pilot-sandbox-shop
```

Known gaps before this is production: SQLite on the container's volume (move to PostgreSQL behind the store
modules), no SSO in front of the API, tests run in the API container instead of a sandboxed job, no private
networking. See the build spec, sections 3.3 and 14.
