# WebPilot 前端镜像：多阶段（node 构建 → nginx 托管）
#
# 为什么分两阶段：
# 构建阶段要 node + npm + devDependencies（tsc、vite 全在 dev 里），
# 运行阶段只需要一个 nginx 和三个静态文件。合成一个阶段的话
# 交付镜像会带上 node_modules（~200MB）和一个根本用不到的 node 运行时。
#
# 构建上下文是**项目根**（见 docker-compose.yml 的 context: .），
# 所以下面所有 COPY 都带 frontend/ 前缀；这样只需一份根目录的 .dockerignore
# 就能同时挡住 node_modules 和 .venv。
FROM node:20-alpine AS build

WORKDIR /app

# 先装依赖再拷源码：源码改了不会重装 node_modules
COPY frontend/package.json frontend/package-lock.json ./
# 用 ci 而不是 install：锁文件存在时 ci 严格按 lock 装，构建可复现；
# install 会顺手把版本往上抬，今天能构建明天就不能了
RUN npm ci

COPY frontend/ ./

# 注意 build 脚本是 `tsc --noEmit && vite build`：
# 类型错误会让构建失败。这是有意的——CI/镜像构建比本地 IDE 更该拦住类型问题
RUN npm run build


FROM nginx:alpine

# 静态产物 + 反代配置
COPY --from=build /app/dist /usr/share/nginx/html
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
