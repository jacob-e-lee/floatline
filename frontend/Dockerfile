# ---------------------------------------------------------------------------
# Floatline frontend - Astro static build (Tailwind 4 + React island)
#
# Stage 1 compiles `dist/`; stage 2 is a tiny image whose only job is to publish
# those assets into a shared volume that the Caddy container serves. Keeping the
# runtime stage free of Node means the production surface stays minimal.
# ---------------------------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /app

# Install dependencies first so Docker can cache this layer across code edits.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
# The browser always calls same-origin /api, which Caddy proxies to FastAPI.
ENV PUBLIC_API_BASE=/api
RUN npm run build

FROM alpine:3.20

RUN apk add --no-cache rsync
COPY --from=build /app/dist /app/dist

# The compose service overrides `command` to rsync these assets into the
# `frontend_dist` volume mounted by Caddy.
CMD ["sh", "-c", "rsync -a --delete /app/dist/ /srv/"]
