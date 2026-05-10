FROM node:22-slim AS builder
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@9.15.0 --activate
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .

# Next.js bakes ALL NEXT_PUBLIC_* env vars into the JS bundle at build
# time. They are NOT runtime-readable from process.env on the client.
# These build-args MUST be passed by CI:
#   docker build \
#     --build-arg NEXT_PUBLIC_AZURE_CLIENT_ID=$AZURE_CLIENT_ID \
#     --build-arg NEXT_PUBLIC_AZURE_TENANT_ID=$AZURE_TENANT_ID \
#     --build-arg NEXT_PUBLIC_API_URL=https://api.example.com \
#     -t msai-frontend:$GIT_SHA ./frontend
#
# No defaults — the args MUST be supplied by CI/operator. The RUN check
# below fails the build immediately if any are missing or empty,
# preventing a "succeeds-then-auth-broken" image from being published.
# Caught by Codex bot review on PR #50 (2026-05-09): the previous shape
# defaulted to empty string, which let CI silently produce broken
# bundles.
ARG NEXT_PUBLIC_AZURE_CLIENT_ID
ARG NEXT_PUBLIC_AZURE_TENANT_ID
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_AZURE_CLIENT_ID=$NEXT_PUBLIC_AZURE_CLIENT_ID
ENV NEXT_PUBLIC_AZURE_TENANT_ID=$NEXT_PUBLIC_AZURE_TENANT_ID
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

# Fail-fast guard: if any required NEXT_PUBLIC_* build-arg is empty,
# stop the build with a specific error pointing at the missing var.
# Better to fail in CI than to publish an image whose JS bundle has
# empty MSAL config and silently breaks auth in browsers.
RUN if [ -z "$NEXT_PUBLIC_AZURE_CLIENT_ID" ]; then \
      echo "ERROR: --build-arg NEXT_PUBLIC_AZURE_CLIENT_ID is required (currently empty). Pass the Entra app client id." >&2; exit 1; \
    fi \
 && if [ -z "$NEXT_PUBLIC_AZURE_TENANT_ID" ]; then \
      echo "ERROR: --build-arg NEXT_PUBLIC_AZURE_TENANT_ID is required (currently empty). Pass the Entra tenant id." >&2; exit 1; \
    fi \
 && if [ -z "$NEXT_PUBLIC_API_URL" ]; then \
      echo "ERROR: --build-arg NEXT_PUBLIC_API_URL is required (currently empty). Pass the public API base URL, e.g. https://api.example.com." >&2; exit 1; \
    fi

RUN pnpm build

FROM node:22-slim
WORKDIR /app
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
ENV PORT=3000
EXPOSE 3000
CMD ["node", "server.js"]
