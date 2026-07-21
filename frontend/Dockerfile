FROM node:22-alpine AS build

WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN npm install -g pnpm@10.17.1 --no-audit --no-fund && pnpm install --frozen-lockfile --prod=false
COPY . .
RUN pnpm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
