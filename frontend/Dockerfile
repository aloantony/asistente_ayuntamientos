FROM node:22-alpine AS deps

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-alpine AS builder

WORKDIR /app

COPY --from=deps /app/node_modules ./node_modules
COPY . .
ARG NEXT_PUBLIC_API_BASE_URL=/api
ARG API_PROXY_TARGET=http://127.0.0.1:8000
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
ENV API_PROXY_TARGET=$API_PROXY_TARGET
RUN npm run build

FROM node:22-alpine AS runner

ENV NODE_ENV=production
ENV NEXT_PUBLIC_API_BASE_URL=/api
ENV API_PROXY_TARGET=http://127.0.0.1:8000
ENV HOSTNAME=127.0.0.1

WORKDIR /app

COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

# El servidor de Next solo sirve ficheros ya construidos: no necesita root ni
# escribe nada. La imagen base ya trae el usuario `node`.
RUN chown -R node:node /app
USER node

EXPOSE 3000

CMD ["node", "server.js"]
