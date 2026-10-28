#!/usr/bin/env bash
# Run in Cloud Shell under the Google Cloud project owner account.
set -euo pipefail
translation_project="${GOOGLE_CLOUD_PROJECT:-project-fdadb4dd-cb77-4270-9cd}"
translation_repo="${TRANSLATION_GITHUB_REPO:-nyu1791-collab/Translation}"
translation_number=$(gcloud projects describe "$translation_project" --format='value(projectNumber)')
translation_sa="translation-vertex@$translation_project.iam.gserviceaccount.com"
translation_pool='translation-actions'
translation_provider='github'
gcloud services enable aiplatform.googleapis.com iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com --project="$translation_project" --quiet
if ! gcloud iam service-accounts describe "$translation_sa" --project="$translation_project" >/dev/null 2>&1; then
  gcloud iam service-accounts create translation-vertex --display-name='Translation Vertex caller' --project="$translation_project" --quiet
fi
translation_permissions='aiplatform.endpoints.predict,serviceusage.services.use'
if gcloud iam roles describe translationVertexGenerate --project="$translation_project" >/dev/null 2>&1; then
  gcloud iam roles update translationVertexGenerate --project="$translation_project" --permissions="$translation_permissions" --quiet
else
  gcloud iam roles create translationVertexGenerate --project="$translation_project" --title='Translation Gemini generation only' --permissions="$translation_permissions" --stage=GA --quiet
fi
gcloud projects add-iam-policy-binding "$translation_project" --member="serviceAccount:$translation_sa" --role="projects/$translation_project/roles/translationVertexGenerate" --condition=None --quiet >/dev/null
if ! gcloud iam workload-identity-pools describe "$translation_pool" --location=global --project="$translation_project" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "$translation_pool" --location=global --display-name='Translation Actions' --project="$translation_project" --quiet
fi
translation_mapping='google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref'
translation_condition="assertion.repository == '$translation_repo' && assertion.ref == 'refs/heads/main'"
if gcloud iam workload-identity-pools providers describe "$translation_provider" --workload-identity-pool="$translation_pool" --location=global --project="$translation_project" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers update-oidc "$translation_provider" --workload-identity-pool="$translation_pool" --location=global --project="$translation_project" --issuer-uri='https://token.actions.githubusercontent.com' --attribute-mapping="$translation_mapping" --attribute-condition="$translation_condition" --quiet
else
  gcloud iam workload-identity-pools providers create-oidc "$translation_provider" --workload-identity-pool="$translation_pool" --location=global --project="$translation_project" --issuer-uri='https://token.actions.githubusercontent.com' --attribute-mapping="$translation_mapping" --attribute-condition="$translation_condition" --quiet
fi
gcloud iam service-accounts add-iam-policy-binding "$translation_sa" --project="$translation_project" --role=roles/iam.workloadIdentityUser --member="principalSet://iam.googleapis.com/projects/$translation_number/locations/global/workloadIdentityPools/$translation_pool/attribute.repository/$translation_repo" --quiet >/dev/null
translation_wif="projects/$translation_number/locations/global/workloadIdentityPools/$translation_pool/providers/$translation_provider"
echo "TRANSLATION_PROJECT_ID=$translation_project"
echo "TRANSLATION_SERVICE_ACCOUNT=$translation_sa"
echo "TRANSLATION_WIF_PROVIDER=$translation_wif"
echo "TRANSLATION_STORAGE_REPO=${TRANSLATION_STORAGE_REPO:-nyu1791-collab/ai-model-workers}"
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  if gh variable set TRANSLATION_PROJECT_ID --repo "$translation_repo" --body "$translation_project" && \
     gh variable set TRANSLATION_SERVICE_ACCOUNT --repo "$translation_repo" --body "$translation_sa" && \
     gh variable set TRANSLATION_WIF_PROVIDER --repo "$translation_repo" --body "$translation_wif" && \
     gh variable set TRANSLATION_STORAGE_REPO --repo "$translation_repo" --body "${TRANSLATION_STORAGE_REPO:-nyu1791-collab/ai-model-workers}"; then
    echo 'GitHub Actions variables registered.'
  else
    echo 'Could not register all GitHub variables; set the values printed above in repository settings.' >&2
  fi
else
  echo 'Set the values printed above in GitHub Actions variables.'
fi
echo 'Set TRANSLATION_STORAGE_TOKEN as an Actions secret with access to the private results repository.'
