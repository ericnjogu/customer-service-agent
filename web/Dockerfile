FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html ./
COPY public ./public
COPY src ./src
ARG VITE_API_BASE_URL=/api
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
ARG VITE_REQUIRE_ADMIN_EMAIL_DOMAIN_MATCH=true
ENV VITE_REQUIRE_ADMIN_EMAIL_DOMAIN_MATCH=$VITE_REQUIRE_ADMIN_EMAIL_DOMAIN_MATCH
RUN npm run build

FROM nginx:1.30.6-alpine-otel@sha256:53cf1f780c33f6e599e9a001e9425fc6ba33e513c31855e3448f75613e95656f
COPY nginx-main.conf /etc/nginx/nginx.conf
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
