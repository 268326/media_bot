FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖（STRM 监控需要 inotifywait；ASS 处理需要 7z/fontforge/assfonts；字幕内封需要 mkvmerge）
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip inotify-tools p7zip-full fontforge-nox mkvtoolnix \
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

# 安装 assfonts 官方预编译 CLI
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) assfonts_arch="x86_64" ;; \
      arm64) assfonts_arch="aarch64" ;; \
      *) echo "Unsupported architecture for assfonts: $arch"; exit 1 ;; \
    esac; \
    assfonts_ver="v0.7.3"; \
    assfonts_pkg="assfonts-${assfonts_ver}-${assfonts_arch}-Linux.tar.gz"; \
    curl -L "https://github.com/wyzdwdz/assfonts/releases/download/${assfonts_ver}/${assfonts_pkg}" -o /tmp/assfonts.tar.gz; \
    tar -xzf /tmp/assfonts.tar.gz -C /tmp; \
    cp /tmp/bin/assfonts /usr/local/bin/assfonts; \
    chmod +x /usr/local/bin/assfonts; \
    /usr/local/bin/assfonts --help >/dev/null; \
    rm -rf /tmp/assfonts.tar.gz /tmp/bin /tmp/share

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY *.py .

# 运行应用
CMD ["python", "main.py"]
