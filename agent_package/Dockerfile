FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY main.py .
COPY fast_api/ ./fast_api/
COPY protocols/ ./protocols/
COPY utils/ ./utils/
COPY memory_store.py .
COPY search_engine.py .
COPY session_manager.py .
COPY config/ ./config/
COPY workspace/ ./workspace/

# 创建非 root 用户
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

# 暴露端口 9001（A2A 协议标准端口）
EXPOSE 9001

# NATS 默认配置
ENV NATS_SERVER_URL=nats://nats:4222
ENV NATS_IN_SUBJECT=workflow.previousagent.result
ENV NATS_IN_DURABLE=workflow-previousagent-result
ENV NATS_OUT_SUBJECT=workflow.autoagent.result

# 启动服务
CMD ["python", "main.py"]
