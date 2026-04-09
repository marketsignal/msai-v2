FROM node:22-slim

WORKDIR /app

RUN corepack enable

ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ARG NEXT_PUBLIC_ENTRA_CLIENT_ID=
ARG NEXT_PUBLIC_ENTRA_TENANT_ID=
ARG NEXT_PUBLIC_ENTRA_REDIRECT_URI=

ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}
ENV NEXT_PUBLIC_ENTRA_CLIENT_ID=${NEXT_PUBLIC_ENTRA_CLIENT_ID}
ENV NEXT_PUBLIC_ENTRA_TENANT_ID=${NEXT_PUBLIC_ENTRA_TENANT_ID}
ENV NEXT_PUBLIC_ENTRA_REDIRECT_URI=${NEXT_PUBLIC_ENTRA_REDIRECT_URI}

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY next.config.ts tsconfig.json postcss.config.mjs eslint.config.mjs next-env.d.ts ./
COPY public ./public
COPY src ./src

RUN pnpm build

CMD ["pnpm", "start", "--hostname", "0.0.0.0", "--port", "3000"]
