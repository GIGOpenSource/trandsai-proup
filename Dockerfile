FROM trandsai-base:latest

WORKDIR /app

# 确保 psycopg2 可用（基础镜像可能缺失）
RUN pip install --no-cache-dir psycopg2-binary

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
