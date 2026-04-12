FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖（STRM 监控需要 inotifywait；ffprobe 使用指定静态构建）
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip inotify-tools \
    && rm -rf /var/lib/apt/lists/*

# 安装指定架构的静态 ffprobe
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) ffprobe_url="https://github.com/sjtuross/StrmAssistant.Releases/raw/refs/heads/main/static-ffprobe/linux_x64/ffprobe_7.1.1.zip" ;; \
      arm64) ffprobe_url="https://github.com/sjtuross/StrmAssistant.Releases/raw/refs/heads/main/static-ffprobe/linux_arm64/ffprobe_7.1.1.zip" ;; \
      *) echo "Unsupported architecture: $arch"; exit 1 ;; \
    esac; \
    curl -L "$ffprobe_url" -o /tmp/ffprobe.zip; \
    unzip -j /tmp/ffprobe.zip -d /usr/local/bin/; \
    chmod +x /usr/local/bin/ffprobe; \
    /usr/local/bin/ffprobe -version >/dev/null; \
    rm -f /tmp/ffprobe.zip

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY *.py .

# 运行应用
CMD ["python", "main.py"]
