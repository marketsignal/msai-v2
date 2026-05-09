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
# Without these, the prod bundle ships with empty MSAL config and auth
# silently fails for every user. Caught by Council 2026-05-09.
ARG NEXT_PUBLIC_AZURE_CLIENT_ID=""
ARG NEXT_PUBLIC_AZURE_TENANT_ID=""
ARG NEXT_PUBLIC_API_URL="http://localhost:8000"
ENV NEXT_PUBLIC_AZURE_CLIENT_ID=$NEXT_PUBLIC_AZURE_CLIENT_ID
ENV NEXT_PUBLIC_AZURE_TENANT_ID=$NEXT_PUBLIC_AZURE_TENANT_ID
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

RUN pnpm build

FROM node:22-slim
WORKDIR /app
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
ENV PORT=3000
EXPOSE 3000
CMD ["node", "server.js"]
