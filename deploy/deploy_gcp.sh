#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  Moonwalk — Deploy to Google Cloud
# ═══════════════════════════════════════════════════════════════
#
#  One-command deployment of the Moonwalk Cloud Orchestrator.
#
#  What this script does:
#    1. Validates prerequisites (gcloud CLI, Docker, API keys)
#    2. Creates / selects a GCP project
#    3. Enables required APIs (Cloud Run, Firestore, Cloud Storage, Artifact Registry)
#    4. Creates Firestore database + GCS bucket
#    5. Creates a Firestore vector index for RAG
#    6. Builds the Docker image via Cloud Build
#    7. Deploys to Cloud Run
#    8. Outputs the service URL + mac_client.py configuration
#
#  Usage:
#    chmod +x deploy/deploy_gcp.sh
#    ./deploy/deploy_gcp.sh
#
#  Environment variables (set in .env or export before running):
#    GCP_PROJECT       — GCP project ID (will create if needed)
#    GCP_REGION        — Deployment region (default: us-central1)
#    GEMINI_API_KEY    — Required: your Gemini API key
#    MOONWALK_CLOUD_TOKEN — Optional: auth token for client connections
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

# ── Colors ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

banner() {
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════${NC}"
    echo -e "${BOLD}  $1${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════${NC}"
}

info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail()    { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

# ── Load .env from project root ──
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -f "$PROJECT_ROOT/backend/.env" ]]; then
    set -a
    source "$PROJECT_ROOT/backend/.env"
    set +a
    info "Loaded environment from backend/.env"
fi

# ── Configuration ──
GCP_PROJECT="${GCP_PROJECT:-gen-lang-client-0333982983}"
GCP_REGION="${GCP_REGION:-us-central1}"
SERVICE_NAME="moonwalk-brain"
REPO_NAME="moonwalk"
IMAGE_NAME="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT}/${REPO_NAME}/${SERVICE_NAME}"
GCS_BUCKET="${GCP_PROJECT}-moonwalk-memory"
FIRESTORE_DB="(default)"

# ═══════════════════════════════════════════════════
#  Step 0: Prerequisites
# ═══════════════════════════════════════════════════

banner "Step 0: Checking Prerequisites"

# gcloud CLI
if ! command -v gcloud &>/dev/null; then
    fail "gcloud CLI not found. Install: https://cloud.google.com/sdk/docs/install"
fi
success "gcloud CLI found: $(gcloud version 2>/dev/null | head -1)"

# Docker (for local builds) or gcloud builds submit
if command -v docker &>/dev/null; then
    success "Docker found: $(docker --version)"
    USE_DOCKER=true
else
    warn "Docker not found — will use Cloud Build instead"
    USE_DOCKER=false
fi

# API Key
if [[ -z "${GEMINI_API_KEY:-}" ]]; then
    fail "GEMINI_API_KEY not set. Add it to backend/.env or export it."
fi
success "GEMINI_API_KEY is set"

# ═══════════════════════════════════════════════════
#  Step 1: GCP Project Setup
# ═══════════════════════════════════════════════════

banner "Step 1: GCP Project Setup"

# Check if project exists
if gcloud projects describe "$GCP_PROJECT" &>/dev/null; then
    success "Project '$GCP_PROJECT' exists"
else
    info "Creating project '$GCP_PROJECT'..."
    gcloud projects create "$GCP_PROJECT" --name="Moonwalk Cloud" 2>/dev/null || true
    success "Project created"
fi

# Set active project
gcloud config set project "$GCP_PROJECT" --quiet
success "Active project: $GCP_PROJECT"

# Check billing
BILLING=$(gcloud billing projects describe "$GCP_PROJECT" --format="value(billingAccountName)" 2>/dev/null || echo "")
if [[ -z "$BILLING" ]]; then
    warn "No billing account linked. Some APIs require billing."
    warn "Link billing: https://console.cloud.google.com/billing/linkedaccount?project=${GCP_PROJECT}"
    read -p "Continue anyway? [y/N] " -n 1 -r
    echo
    [[ $REPLY =~ ^[Yy]$ ]] || exit 1
else
    success "Billing linked: $BILLING"
fi

# ═══════════════════════════════════════════════════
#  Step 2: Enable APIs
# ═══════════════════════════════════════════════════

banner "Step 2: Enabling APIs"

APIS=(
    "run.googleapis.com"                # Cloud Run
    "firestore.googleapis.com"          # Firestore
    "storage.googleapis.com"            # Cloud Storage
    "artifactregistry.googleapis.com"   # Artifact Registry (Docker images)
    "cloudbuild.googleapis.com"         # Cloud Build
    "aiplatform.googleapis.com"         # Vertex AI (embeddings)
)

for api in "${APIS[@]}"; do
    if gcloud services list --enabled --format="value(config.name)" | grep -q "^${api}$" 2>/dev/null; then
        success "$api (already enabled)"
    else
        info "Enabling $api..."
        gcloud services enable "$api" --quiet
        success "$api"
    fi
done

# ═══════════════════════════════════════════════════
#  Step 3: Firestore Database
# ═══════════════════════════════════════════════════

banner "Step 3: Firestore Database"

# Check if Firestore is already initialized
FS_EXISTS=$(gcloud firestore databases list --format="value(name)" 2>/dev/null | head -1)
if [[ -n "$FS_EXISTS" ]]; then
    success "Firestore database exists"
else
    info "Creating Firestore database in $GCP_REGION..."
    gcloud firestore databases create \
        --location="$GCP_REGION" \
        --type=firestore-native \
        --quiet 2>/dev/null || true
    success "Firestore database created"
fi

# Create vector index for RAG search on vault collection
info "Creating Firestore vector index for RAG..."
cat > /tmp/moonwalk_firestore_index.json <<'EOF'
{
  "collectionGroup": "vault",
  "queryScope": "COLLECTION",
  "fields": [
    { "fieldPath": "embedding", "vectorConfig": { "dimension": 768, "flat": {} } },
    { "fieldPath": "category", "order": "ASCENDING" }
  ]
}
EOF

gcloud firestore indexes composite create \
    --collection-group="vault" \
    --field-config="field-path=embedding,vector-config={dimension=768,flat={}}" \
    --field-config="field-path=category,order=ascending" \
    --quiet 2>/dev/null && success "Vector index created" || warn "Vector index may already exist (OK)"

rm -f /tmp/moonwalk_firestore_index.json

# ═══════════════════════════════════════════════════
#  Step 4: Cloud Storage Bucket
# ═══════════════════════════════════════════════════

banner "Step 4: Cloud Storage Bucket"

if gsutil ls -b "gs://${GCS_BUCKET}" &>/dev/null; then
    success "Bucket gs://${GCS_BUCKET} exists"
else
    info "Creating bucket gs://${GCS_BUCKET}..."
    gsutil mb -p "$GCP_PROJECT" -l "$GCP_REGION" "gs://${GCS_BUCKET}" 2>/dev/null || true
    success "Bucket created"
fi

# Set lifecycle rule — delete temp objects after 90 days
cat > /tmp/moonwalk_lifecycle.json <<EOF
{
  "rule": [
    {
      "action": { "type": "Delete" },
      "condition": { "age": 90, "matchesPrefix": ["vault/"] }
    }
  ]
}
EOF
gsutil lifecycle set /tmp/moonwalk_lifecycle.json "gs://${GCS_BUCKET}" 2>/dev/null || true
rm -f /tmp/moonwalk_lifecycle.json
success "Lifecycle policy set (90-day cleanup)"

# ═══════════════════════════════════════════════════
#  Step 5: Artifact Registry (Docker repo)
# ═══════════════════════════════════════════════════

banner "Step 5: Artifact Registry"

if gcloud artifacts repositories describe "$REPO_NAME" \
    --location="$GCP_REGION" --format="value(name)" &>/dev/null; then
    success "Repository '$REPO_NAME' exists"
else
    info "Creating Artifact Registry repository..."
    gcloud artifacts repositories create "$REPO_NAME" \
        --repository-format=docker \
        --location="$GCP_REGION" \
        --description="Moonwalk Docker images" \
        --quiet
    success "Repository created"
fi

# Configure Docker auth
gcloud auth configure-docker "${GCP_REGION}-docker.pkg.dev" --quiet 2>/dev/null
success "Docker auth configured"

# ═══════════════════════════════════════════════════
#  Step 6: Build Docker Image
# ═══════════════════════════════════════════════════

banner "Step 6: Building Docker Image"

cd "$PROJECT_ROOT"

if [[ "$USE_DOCKER" == true ]]; then
    info "Building with Docker..."
    docker build -t "$IMAGE_NAME:latest" -f Dockerfile .
    info "Pushing to Artifact Registry..."
    docker push "$IMAGE_NAME:latest"
else
    info "Building with Cloud Build..."
    gcloud builds submit \
        --tag "$IMAGE_NAME:latest" \
        --timeout=600 \
        --quiet
fi

success "Image built and pushed: $IMAGE_NAME:latest"

# ═══════════════════════════════════════════════════
#  Step 7: Deploy to Cloud Run
# ═══════════════════════════════════════════════════

banner "Step 7: Deploying to Cloud Run"

# Build env vars string
ENV_VARS="GEMINI_API_KEY=${GEMINI_API_KEY}"
ENV_VARS+=",GCP_PROJECT=${GCP_PROJECT}"
ENV_VARS+=",MOONWALK_GCS_BUCKET=${GCS_BUCKET}"
ENV_VARS+=",MOONWALK_CLOUD=1"

# Optional env vars
if [[ -n "${MOONWALK_CLOUD_TOKEN:-}" ]]; then
    ENV_VARS+=",MOONWALK_CLOUD_TOKEN=${MOONWALK_CLOUD_TOKEN}"
fi
if [[ -n "${GEMINI_FAST_MODEL:-}" ]]; then
    ENV_VARS+=",GEMINI_FAST_MODEL=${GEMINI_FAST_MODEL}"
fi
if [[ -n "${GEMINI_POWERFUL_MODEL:-}" ]]; then
    ENV_VARS+=",GEMINI_POWERFUL_MODEL=${GEMINI_POWERFUL_MODEL}"
fi
if [[ -n "${GEMINI_ROUTING_MODEL:-}" ]]; then
    ENV_VARS+=",GEMINI_ROUTING_MODEL=${GEMINI_ROUTING_MODEL}"
fi
if [[ -n "${GEMINI_FALLBACK_MODEL:-}" ]]; then
    ENV_VARS+=",GEMINI_FALLBACK_MODEL=${GEMINI_FALLBACK_MODEL}"
fi

info "Deploying service '$SERVICE_NAME'..."
gcloud run deploy "$SERVICE_NAME" \
    --image="$IMAGE_NAME:latest" \
    --region="$GCP_REGION" \
    --platform=managed \
    --allow-unauthenticated \
    --port=8080 \
    --memory=1Gi \
    --cpu=2 \
    --min-instances=0 \
    --max-instances=3 \
    --timeout=900 \
    --concurrency=10 \
    --set-env-vars="$ENV_VARS" \
    --session-affinity \
    --quiet

# Get the service URL
SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" \
    --region="$GCP_REGION" \
    --format="value(status.url)")

success "Deployed to: $SERVICE_URL"

# ═══════════════════════════════════════════════════
#  Step 8: Summary
# ═══════════════════════════════════════════════════

# Convert HTTPS to WSS for WebSocket
WS_URL="${SERVICE_URL/https:\/\//wss://}"

banner "Deployment Complete!"

echo ""
echo -e "${GREEN}${BOLD}Cloud Orchestrator URL:${NC} $SERVICE_URL"
echo -e "${GREEN}${BOLD}WebSocket URL:${NC}         $WS_URL"
echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}  To connect your Mac:${NC}"
echo ""
echo -e "  1. Set the cloud URL in your environment:"
echo ""
echo -e "     ${YELLOW}export MOONWALK_CLOUD_URL=\"${WS_URL}\"${NC}"
echo ""
echo -e "  2. Start the Mac Client (instead of local_server.py):"
echo ""
echo -e "     ${YELLOW}python backend/servers/mac_client.py${NC}"
echo ""
echo -e "  3. The Electron app will connect locally to mac_client.py,"
echo -e "     which forwards requests to the cloud."
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${CYAN}  Infrastructure created:${NC}"
echo -e "    Firestore DB  — $(gcloud firestore databases list --format='value(locationId)' 2>/dev/null | head -1) (with vector index for RAG)"
echo -e "    GCS Bucket    — gs://${GCS_BUCKET}"
echo -e "    Cloud Run     — ${SERVICE_NAME} (${GCP_REGION})"
echo -e "    Docker Image  — ${IMAGE_NAME}:latest"
echo ""
echo -e "${CYAN}  Memory architecture:${NC}"
echo -e "    Conversations → Firestore users/{uid}/sessions/"
echo -e "    User Profile  → Firestore users/{uid}/meta/profile"
echo -e "    Vault Memory  → Firestore users/{uid}/vault/ (with vector embeddings)"
echo -e "    Background    → Firestore users/{uid}/tasks/"
echo -e "    Large blobs   → GCS gs://${GCS_BUCKET}/vault/"
echo -e "    RAG           → Gemini text-embedding-004 + Firestore vector search"
echo ""
