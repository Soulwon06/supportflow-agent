FROM python:3.11-slim

WORKDIR /app

# CPU-only, offline-at-runtime deployment defaults. Models are mounted at run
# time and are deliberately not part of the image.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/root/.cache/huggingface \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    SUPPORTFLOW_ENABLE_DENSE_RETRIEVAL=true \
    SUPPORTFLOW_RERANKER_PATH=/models/bge-reranker-v2-m3 \
    SUPPORTFLOW_RERANKER_DEVICE=cpu \
    SUPPORTFLOW_EMBEDDING_DEVICE=cpu \
    SUPPORTFLOW_PORT=8001

COPY requirements.txt .

# SupportFlow 当前只需要 CPU embedding。
# 先从 PyTorch 官方 CPU 仓库安装 CPU-only Torch，
# 避免 sentence-transformers 拉取 CUDA/NVIDIA 依赖。
RUN pip install --no-cache-dir "torch==2.14.0+cpu" --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8001

CMD ["sh", "-c", "exec python -m uvicorn app.api:api --host 0.0.0.0 --port \"${SUPPORTFLOW_PORT:-8001}\""]
