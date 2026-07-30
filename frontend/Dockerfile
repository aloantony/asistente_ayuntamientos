FROM node:22-alpine AS deps

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-alpine AS builder

WORKDIR /app

COPY --from=deps /app/node_modules ./node_modules
COPY . .
# OJO: NEXT_PUBLIC_* se hornea en el bundle del navegador durante `next build`.
# Para un dominio público hay que pasar aquí la URL real de la API
# (p. ej. https://dominio.es/api); la ENV de la etapa runner NO la cambia.
# Ver ADR-031.
ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-alpine AS runner

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
# El servidor standalone de Next escucha en el hostname del contenedor si no se
# le dice otra cosa, así que ni el healthcheck ni nada en loopback lo alcanzan.
ENV HOSTNAME=0.0.0.0
ENV PORT=3000

WORKDIR /app

COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

# La imagen base ya trae el usuario `node`: hasta ahora el servidor corría como
# root sin necesitarlo (ADR-032).
RUN chown -R node:node /app
USER node

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD wget --spider -q http://127.0.0.1:3000/login || exit 1

CMD ["node", "server.js"]
