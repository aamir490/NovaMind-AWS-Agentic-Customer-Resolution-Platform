FROM node:24-bookworm-slim AS build
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY index.html vite.config.js ./
COPY src/ src/
COPY public/ public/
RUN npm run build

FROM nginx:stable-alpine AS runtime
COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=build /build/dist/ /usr/share/nginx/html/
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/healthz"]
# Bypass image entrypoint scripts that modify configuration at runtime.
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
