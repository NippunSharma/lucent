#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Lucent — Automated Cloud Run Deployment
# ---------------------------------------------------------------------------

PROJECT_ID="${GCP_PROJECT_ID:-gemini-devpost-hackathon}"
SERVICE_NAME="${SERVICE_NAME:-lucent-app}"
REGION="${GCP_REGION:-us-east1}"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

echo "==> Building container image: ${IMAGE}"
gcloud builds submit \
  --project "${PROJECT_ID}" \
  --tag "${IMAGE}" \
  --timeout 1200

echo "==> Deploying to Cloud Run: ${SERVICE_NAME} (${REGION})"
gcloud run deploy "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --platform managed \
  --memory 4Gi \
  --cpu 2 \
  --timeout 900 \
  --concurrency 40 \
  --min-instances 0 \
  --max-instances 4 \
  --allow-unauthenticated \
  --set-env-vars "\
GOOGLE_CLOUD_PROJECT=${PROJECT_ID},\
GOOGLE_GENAI_USE_VERTEXAI=TRUE,\
GOOGLE_CLOUD_LOCATION=global,\
MANIM_RENDER_BACKEND=cloudrun,\
WS_DEBUG=0"

SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --format 'value(status.url)')

echo ""
echo "==> Deployed successfully!"
echo "    Service URL: ${SERVICE_URL}"
echo "    Homepage:    ${SERVICE_URL}/"
echo "    Lucent app:  ${SERVICE_URL}/lucent/"
echo ""
echo "To map your custom domain:"
echo "    gcloud run domain-mappings create \\"
echo "      --service ${SERVICE_NAME} \\"
echo "      --domain nippun.in \\"
echo "      --region ${REGION}"
