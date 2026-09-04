# KK-Agent 容器镜像
FROM python:3.12-slim

WORKDIR /app

# 先拷贝依赖清单利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝项目代码（.env 不会被拷入：.dockerignore 已排除）
COPY agent/ agent/
COPY web/ web/
COPY cli.py webapp.py config.yaml ./

# 非root用户运行
RUN useradd -m agent && chown -R agent:agent /app
USER agent

EXPOSE 8788

ENV BIN_AGENT_WEB_HOST=0.0.0.0
# 注意：容器模式监听 0.0.0.0 以便宿主机访问；
# 安全中间件仍按 Host/Origin 头校验，外部访问请用 SSH 隧道或反向代理加认证。

CMD ["python", "webapp.py"]
