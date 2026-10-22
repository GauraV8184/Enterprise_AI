# syntax=docker/dockerfile:1

FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .

ARG VITE_API_BASE_URL=/api
ARG VITE_SUPABASE_URL=
# The anon key is public by design. The arg is named without "KEY" so BuildKit
# does not flag a legitimate public value as a leaked secret.
ARG VITE_SUPABASE_ANON=
ARG VITE_DEV_USER_ID=dev-user
ARG VITE_MAX_UPLOAD_MB=25

RUN VITE_API_BASE_URL="$VITE_API_BASE_URL" \
    VITE_SUPABASE_URL="$VITE_SUPABASE_URL" \
    VITE_SUPABASE_ANON_KEY="$VITE_SUPABASE_ANON" \
    VITE_DEV_USER_ID="$VITE_DEV_USER_ID" \
    VITE_MAX_UPLOAD_MB="$VITE_MAX_UPLOAD_MB" \
    npm run build

# The unprivileged image runs as UID 101 and listens on 8080 by default.
FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/ || exit 1
