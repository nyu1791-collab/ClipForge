#!/usr/bin/env bash
# Run in Cloud Shell while signed in as the explicitly authorized project owner.
set -euo pipefail
vertex_project='project-fdadb4dd-cb77-4270-9cd'
vertex_owner='n.yu1791@gmail.com'
vertex_account=$(gcloud auth list --filter=status:ACTIVE --format='value(account)')
if [[ ${vertex_account,,} != "$vertex_owner" ]]; then
  echo "Open Cloud Shell as $vertex_owner before running this setup." >&2
  exit 1
fi
vertex_number=$(gcloud projects describe "$vertex_project" --format='value(projectNumber)')
vertex_sa="clipforge-vertex@$vertex_project.iam.gserviceaccount.com"
gcloud services enable aiplatform.googleapis.com iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com --project="$vertex_project" --quiet
if ! gcloud iam service-accounts describe "$vertex_sa" --project="$vertex_project" >/dev/null 2>&1; then
  gcloud iam service-accounts create clipforge-vertex --display-name='ClipForge Vertex caller' --project="$vertex_project" --quiet
fi
vertex_permissions='aiplatform.endpoints.predict,serviceusage.services.use'
if gcloud iam roles describe clipforgeVertexGenerate --project="$vertex_project" >/dev/null 2>&1; then
  gcloud iam roles update clipforgeVertexGenerate --project="$vertex_project" --permissions="$vertex_permissions" --quiet
else
  gcloud iam roles create clipforgeVertexGenerate --project="$vertex_project" --title='ClipForge Gemini generation only' --permissions="$vertex_permissions" --stage=GA --quiet
fi
gcloud projects add-iam-policy-binding "$vertex_project" --member="serviceAccount:$vertex_sa" --role="projects/$vertex_project/roles/clipforgeVertexGenerate" --condition=None --quiet >/dev/null
if ! gcloud iam workload-identity-pools describe clipforge-actions --location=global --project="$vertex_project" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create clipforge-actions --location=global --display-name='ClipForge Actions' --project="$vertex_project" --quiet
fi
vertex_condition="assertion.repository == 'nyu1791-collab/ai-model-workers' && assertion.ref == 'refs/heads/fix/pr18-verified-transfer'"
vertex_mapping='google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref'
if gcloud iam workload-identity-pools providers describe github --workload-identity-pool=clipforge-actions --location=global --project="$vertex_project" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers update-oidc github --workload-identity-pool=clipforge-actions --location=global --project="$vertex_project" --issuer-uri='https://token.actions.githubusercontent.com' --attribute-mapping="$vertex_mapping" --attribute-condition="$vertex_condition" --quiet
else
  gcloud iam workload-identity-pools providers create-oidc github --workload-identity-pool=clipforge-actions --location=global --project="$vertex_project" --issuer-uri='https://token.actions.githubusercontent.com' --attribute-mapping="$vertex_mapping" --attribute-condition="$vertex_condition" --quiet
fi
gcloud iam service-accounts add-iam-policy-binding "$vertex_sa" --project="$vertex_project" --role=roles/iam.workloadIdentityUser --member="principalSet://iam.googleapis.com/projects/$vertex_number/locations/global/workloadIdentityPools/clipforge-actions/attribute.repository/nyu1791-collab/ai-model-workers" --quiet >/dev/null
printf '\nGoogle Cloud setup completed. No API key or service-account key was created.\n'
printf 'GitHub Actions variable name: CLIPFORGE_VERTEX_PROJECT_NUMBER\nValue: %s\n' "$vertex_number"
printf 'Register here: https://github.com/nyu1791-collab/ai-model-workers/settings/variables/actions/new\n'
