#!/usr/bin/env bash
# Create a Medusa v2 application with the middleware this package needs, run its
# migrations, create the admin user, and print what goes into .env. Needs Node 20+, a reachable
# Postgres 16 (DATABASE_URL), and a Redis if you want one (optional for development).
#
#   DATABASE_URL=postgres://user:pass@localhost:5432/medusa scripts/bootstrap_medusa.sh [dir]
#
# create-medusa-app asks one question that has no flag (the Next.js starter); the answer
# is piped in. The admin user is created with the email and password you export as
# MEDUSA_ADMIN_EMAIL and MEDUSA_ADMIN_PASSWORD (defaults below, change them).
set -euo pipefail
DIR=${1:-medusa}
: "${DATABASE_URL:?set DATABASE_URL to a Postgres 16 database}"
ADMIN_EMAIL=${MEDUSA_ADMIN_EMAIL:-admin@example.com}
ADMIN_PASSWORD=${MEDUSA_ADMIN_PASSWORD:-supersecret}
HERE=$(cd "$(dirname "$0")/.." && pwd)

if [ ! -d "$DIR" ]; then
  printf 'n\n' | npx --yes create-medusa-app@latest "$DIR" --db-url "$DATABASE_URL" --no-browser --no-migrations --use-npm --skip-db
fi
# create-medusa-app currently produces a monorepo (apps/backend); older versions a flat app.
APP="$DIR"; [ -d "$DIR/apps/backend" ] && APP="$DIR/apps/backend"
mkdir -p "$APP/src/api" "$APP/src/migration-scripts"
cp "$HERE/platform/medusa-backend/src/api/middlewares.ts" "$APP/src/api/middlewares.ts"
cp "$HERE/platform/medusa-backend/src/migration-scripts/initial-data-seed.ts" "$APP/src/migration-scripts/initial-data-seed.ts"
cd "$APP"
npx medusa db:migrate
npx medusa user -e "$ADMIN_EMAIL" -p "$ADMIN_PASSWORD" 2>/dev/null || true
# create-medusa-app seeds the store itself on creation: an EUR region, a sales channel,
# a publishable key, shipping options and a few products. Nothing more is needed here;
# platform/medusa-backend/src/migration-scripts/initial-data-seed.ts is the seed the
# package was verified against, for a store created any other way (`npx medusa exec`).
echo
echo "Medusa is set up in $APP. Start it with:  cd $DIR && npm run dev"
echo "Then put in .env:"
echo "  MEDUSA_URL=http://localhost:9000"
echo "  MEDUSA_ADMIN_EMAIL=$ADMIN_EMAIL"
echo "  MEDUSA_ADMIN_PASSWORD=$ADMIN_PASSWORD"
echo "  LAB_CURRENCY=eur"
echo "  MEDUSA_PUBLISHABLE_KEY=<sign in at http://localhost:9000/app, Settings > Publishable API keys>"
