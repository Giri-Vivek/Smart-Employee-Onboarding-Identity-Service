#!/usr/bin/env bash
# Usage: SENDER=you@x.com HR=hr@x.com [REMINDER=120] [REGION=ap-south-1] bash deploy.sh
set -euo pipefail
: "${SENDER:?set SENDER=verified-ses-email}" "${HR:?set HR=hr-alert-email}"
REGION="${REGION:-ap-south-1}"; STACK=smart-onboarding; REMINDER="${REMINDER:-86400}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ART="onb-artifacts-$ACCOUNT-$REGION"

aws s3 mb "s3://$ART" --region "$REGION" 2>/dev/null || true
aws cloudformation package --template-file template.yaml --s3-bucket "$ART" \
  --output-template-file packaged.yaml --region "$REGION"
aws cloudformation deploy --template-file packaged.yaml --stack-name "$STACK" --region "$REGION" \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --parameter-overrides SenderEmail="$SENDER" HrAlertEmail="$HR" ReminderSeconds="$REMINDER"

out() { aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

cat > frontend/config.js <<JS
window.CONFIG = { region: "$REGION", clientId: "$(out ClientId)", apiUrl: "$(out ApiUrl)" };
JS
aws s3 sync frontend "s3://$(out FrontendBucket)" --delete --cache-control no-cache --region "$REGION"
echo; echo "Site URL : $(out SiteUrl)"; echo "User pool: $(out UserPoolId)"
